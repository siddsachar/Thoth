from __future__ import annotations

import json

from nicegui import run, ui

from ui.timer_utils import defer_ui


def _source_label(source: str) -> str:
    labels = {
        "environment": "Using environment variable",
        "keyring": "Saved in keyring",
        "session": "Using session key",
        "legacy_plaintext": "Using legacy plaintext key",
        "api_keys": "Saved API key",
        "external_cli": "Using external CLI login",
        "external_cli_detected": "External CLI login detected",
        "oauth_device": "Signed in with ChatGPT",
        "no_auth": "No API key required",
        "local_daemon": "Local daemon running",
        "not_running": "Not running",
    }
    return labels.get(source, f"Using {source}" if source else "Connected")


def _codex_action_state(card: dict) -> dict[str, bool]:
    if card.get("provider_id") != "codex":
        return {}
    configured = bool(card.get("configured"))
    source = str(card.get("source") or "")
    external_exists = bool(card.get("external_reference_exists"))
    return {
        "can_connect": source != "oauth_device" or not configured,
        "can_reference": external_exists and not configured,
        "can_disconnect": configured or source == "external_cli",
        "runtime_enabled": bool(card.get("runtime_enabled")),
    }


def build_provider_summary_cards() -> None:
    from providers.status import provider_status_cards

    ui.label("Connection Status").classes("text-subtitle2")
    container = ui.column().classes("w-full gap-2")

    def _status_style(configured: bool, source: str) -> tuple[str, str]:
        if configured:
            if source == "external_cli":
                return "#38bdf8", "Referenced"
            if source == "oauth_device":
                return "#22c55e", "Connected"
            return "#22c55e", "Connected"
        if source == "external_cli_detected":
            return "#38bdf8", "Detected"
        if source == "not_running":
            return "#f59e0b", "Not running"
        return "#71717a", "Not connected"

    def _metadata_label(card: dict) -> str:
        parts: list[str] = []
        plan_type = str(card.get("plan_type") or "")
        if plan_type:
            parts.append(f"{plan_type} plan")
        model_count = card.get("model_count")
        if model_count is not None:
            parts.append(f"{model_count} models")
        chat_count = int(card.get("chat_count") or 0)
        media_count = int(card.get("media_count") or 0)
        if chat_count:
            parts.append(f"{chat_count} chat")
        if media_count:
            parts.append(f"{media_count} media")
        return " · ".join(parts)

    def _summary_chip(label: str, value: int | str, color: str = "blue-grey") -> None:
        with ui.row().classes("items-center gap-1 px-2 py-1 rounded-borders").style("border: 1px solid rgba(148, 163, 184, 0.24); background: rgba(148, 163, 184, 0.08);"):
            ui.label(str(value)).classes("text-weight-bold text-xs")
            ui.label(label).classes("text-grey-6 text-xs")

    def _reference_codex_login() -> None:
        from providers.codex import save_external_reference

        saved = save_external_reference()
        if saved.get("configured"):
            ui.notify("Referenced existing Codex login", type="positive")
        else:
            ui.notify("No Codex auth cache found to reference", type="warning")
        defer_ui(_load)

    async def _connect_codex_login() -> None:
        from providers.codex import start_codex_device_flow

        notification = ui.notification("Starting ChatGPT sign-in...", type="ongoing", spinner=True, timeout=None)
        try:
            flow = await run.io_bound(start_codex_device_flow)
        except Exception as exc:
            notification.dismiss()
            ui.notify(f"Could not start ChatGPT sign-in: {exc}", type="negative")
            return
        notification.dismiss()
        _show_codex_device_dialog(flow)

    def _show_codex_device_dialog(flow) -> None:
        from providers.codex import exchange_codex_device_authorization, poll_codex_device_authorization, save_codex_oauth_tokens

        with ui.dialog() as dialog:
            with ui.card().classes("w-full").style("max-width: 30rem;"):
                ui.label("Connect ChatGPT / Codex").classes("text-h6")
                ui.label("Open the verification page, enter this code, then return here.").classes("text-grey-6 text-sm")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.link("Open OpenAI Login", flow.verification_uri, new_tab=True).classes("text-primary text-sm")
                    ui.badge("Codex", color="blue-grey").props("outline dense")
                with ui.row().classes("items-center gap-2 no-wrap w-full q-my-sm"):
                    code_input = ui.input(value=flow.user_code).props("readonly outlined dense").classes("text-h5 text-weight-bold").style("letter-spacing: 0; max-width: 16rem;")
                    ui.button(
                        icon="content_copy",
                        on_click=lambda: (
                            ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(flow.user_code)})"),
                            ui.notify("Code copied", type="positive"),
                        ),
                    ).props("flat dense round size=sm color=primary").tooltip("Copy code")
                ui.label(f"Expires: {flow.expires_at}").classes("text-grey-6 text-xs")
                status_label = ui.label("").classes("text-grey-6 text-sm")

                async def _check_login() -> None:
                    check_note = ui.notification("Checking ChatGPT sign-in...", type="ongoing", spinner=True, timeout=None)
                    try:
                        authorization = await run.io_bound(poll_codex_device_authorization, flow)
                        if authorization is None:
                            check_note.dismiss()
                            status_label.text = "Still waiting for OpenAI confirmation."
                            status_label.update()
                            ui.notify("Login is still pending", type="info")
                            return
                        token_set = await run.io_bound(exchange_codex_device_authorization, authorization)
                        await run.io_bound(save_codex_oauth_tokens, token_set)
                    except Exception as exc:
                        check_note.dismiss()
                        status_label.text = f"Sign-in failed: {exc}"
                        status_label.update()
                        ui.notify(f"ChatGPT sign-in failed: {exc}", type="negative")
                        return
                    check_note.dismiss()
                    dialog.close()
                    ui.notify("ChatGPT / Codex connected", type="positive")
                    defer_ui(_load)

                with ui.row().classes("w-full items-center justify-end gap-2"):
                    ui.button("Cancel", icon="close", on_click=dialog.close).props("flat dense")
                    ui.button("Check Login", icon="check", on_click=_check_login).props("flat dense color=primary")
        dialog.open()

    def _disconnect_codex() -> None:
        from providers.codex import disconnect_codex_metadata

        disconnect_codex_metadata()
        ui.notify("Disconnected Thoth Codex metadata", type="info")
        defer_ui(_load)

    def _render_row(card: dict) -> None:
        source = str(card.get("source") or "")
        dot_color, state_label = _status_style(bool(card.get("configured")), source)
        metadata = _metadata_label(card)
        fingerprint = str(card.get("fingerprint") or "")
        account_hash = str(card.get("account_id_hash") or "")
        with ui.row().classes("items-center gap-2 no-wrap w-full q-px-sm q-py-xs").style("min-height: 42px; border-bottom: 1px solid rgba(148, 163, 184, 0.14);"):
            ui.label(card.get("icon") or "AI").classes("text-base").style("width: 22px; text-align: center;")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.element("span").style(f"width: 8px; height: 8px; border-radius: 999px; background: {dot_color}; display: inline-block; flex: 0 0 auto;")
                    ui.label(card["display_name"]).classes("text-sm text-weight-medium").style("line-height: 1.15;")
                    ui.label(state_label).classes("text-grey-6 text-xs")
                sub = _source_label(source) if source else "Add credentials to enable this provider"
                if metadata:
                    sub = f"{sub} · {metadata}"
                ui.label(sub).classes("text-grey-6 text-xs").style("line-height: 1.15; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;")
            with ui.row().classes("items-center gap-1 no-wrap"):
                if fingerprint:
                    ui.badge(fingerprint, color="blue-grey").props("outline dense").tooltip("Credential fingerprint")
                if account_hash:
                    ui.badge(account_hash, color="blue-grey").props("outline dense").tooltip("ChatGPT account fingerprint")
                ui.badge(str(card.get("risk_label") or "api_key"), color="grey").props("outline dense")
                codex_actions = _codex_action_state(card)
                if codex_actions.get("can_connect"):
                    ui.button(icon="login", on_click=_connect_codex_login).props("flat dense round size=sm color=primary").tooltip("Connect ChatGPT in app")
                if codex_actions.get("can_reference"):
                    ui.button(icon="link", on_click=_reference_codex_login).props("flat dense round size=sm").tooltip("Reference existing Codex CLI login")
                if codex_actions.get("can_disconnect"):
                    ui.button(icon="link_off", on_click=_disconnect_codex).props("flat dense round size=sm color=negative").tooltip("Disconnect Thoth Codex metadata")
                ui.button(icon="refresh", on_click=lambda: defer_ui(_load)).props("flat dense round size=sm").tooltip("Refresh status")

    def _render(cards: list[dict]) -> None:
        container.clear()
        with container:
            if not cards:
                ui.label("No provider definitions available.").classes("text-grey-6 text-sm")
                return
            connected = sum(1 for card in cards if card.get("configured"))
            local = sum(1 for card in cards if card.get("group") == "Local")
            api = sum(1 for card in cards if card.get("group") == "API Providers")
            subscription = sum(1 for card in cards if card.get("group") == "Subscription Accounts")
            media = sum(1 for card in cards if int(card.get("media_count") or 0) > 0)
            with ui.row().classes("items-center gap-2 q-mb-xs"):
                _summary_chip("connected", connected)
                _summary_chip("local", local)
                _summary_chip("API", api)
                _summary_chip("subscription", subscription)
                _summary_chip("media-capable", media)
            for group_name in ("Local", "Subscription Accounts", "API Providers", "Custom Endpoints"):
                group_cards = [card for card in cards if card.get("group") == group_name]
                if not group_cards:
                    continue
                ui.label(group_name).classes("text-grey-5 text-xs text-uppercase q-mt-xs")
                with ui.column().classes("w-full gap-0 rounded-borders").style("border: 1px solid rgba(148, 163, 184, 0.18); overflow: hidden; background: rgba(148, 163, 184, 0.035);"):
                    for card in group_cards:
                        _render_row(card)

    async def _load() -> None:
        container.clear()
        with container:
            with ui.row().classes("items-center gap-2 text-grey-6 text-sm"):
                ui.spinner(size="sm")
                ui.label("Checking provider connections...")
        try:
            _render(await run.io_bound(provider_status_cards))
        except Exception as exc:
            container.clear()
            with container:
                ui.label(f"Could not load provider status: {exc}").classes("text-warning text-sm")
                ui.button(icon="refresh", on_click=lambda: defer_ui(_load)).props("flat dense round size=sm").tooltip("Retry")

    with container:
        with ui.row().classes("items-center gap-2 text-grey-6 text-sm"):
            ui.spinner(size="sm")
            ui.label("Preparing provider status...")
    defer_ui(_load)


def build_custom_endpoints_section(on_change=None) -> None:
    from providers.custom import delete_custom_endpoint, list_custom_endpoints, refresh_custom_endpoint_models, save_custom_endpoint

    endpoints = list_custom_endpoints()
    ui.label("Custom / Self-Hosted Endpoints").classes("text-subtitle2")
    if endpoints:
        with ui.column().classes("w-full gap-1"):
            for endpoint in endpoints:
                with ui.row().classes("items-center gap-2 no-wrap w-full"):
                    ui.icon("hub", size="sm")
                    with ui.column().classes("gap-0"):
                        ui.label(endpoint.get("display_name") or endpoint.get("id")).classes("text-sm text-weight-medium")
                        ui.label(endpoint.get("base_url") or "No base URL").classes("text-grey-6 text-xs")
                    ui.badge(endpoint.get("execution_location") or "remote", color="blue-grey").props("outline dense")
                    ui.badge(endpoint.get("transport") or "openai_chat", color="grey").props("outline dense")
                    models = endpoint.get("models") if isinstance(endpoint.get("models"), list) else []
                    if models:
                        ui.badge(f"{len(models)} models", color="grey").props("outline dense")
                    ui.space()

                    def _delete(endpoint_id=endpoint["id"]):
                        delete_custom_endpoint(endpoint_id)
                        ui.notify("Custom endpoint removed", type="info")
                        if on_change:
                            on_change()

                    async def _refresh(endpoint_id=endpoint["id"]):
                        notification = ui.notification("Refreshing endpoint models...", type="ongoing", spinner=True, timeout=None)
                        try:
                            infos = await run.io_bound(refresh_custom_endpoint_models, endpoint_id)
                            notification.dismiss()
                            ui.notify(f"Found {len(infos)} model(s)", type="positive")
                            if on_change:
                                on_change()
                        except Exception as exc:
                            notification.dismiss()
                            ui.notify(f"Refresh failed: {exc}", type="negative")

                    ui.button(icon="refresh", on_click=_refresh).props("flat dense round size=sm").tooltip("Refresh models")
                    ui.button(icon="delete", on_click=_delete).props("flat dense round size=sm color=negative").tooltip("Remove endpoint")
    else:
        ui.label("Connect vLLM, llama.cpp, LM Studio, LocalAI, LiteLLM, or another OpenAI-compatible API.").classes("text-grey-6 text-sm")

    with ui.expansion("Add custom endpoint", icon="add", value=False).classes("w-full"):
        name_input = ui.input("Name", placeholder="Local vLLM").classes("w-full")
        base_url_input = ui.input("Base URL", placeholder="http://127.0.0.1:8000/v1").classes("w-full")
        with ui.row().classes("items-center gap-3"):
            no_auth = ui.checkbox("No API key required", value=True)
            location = ui.select(
                {"local": "Local/private", "remote": "Remote/proxy"},
                value="local",
                label="Execution location",
            ).classes("min-w-[180px]")
        api_key_input = ui.input(
            "API key or token",
            value="",
            placeholder="Optional for local servers",
            password=True,
            password_toggle_button=True,
        ).classes("w-full")

        def _save() -> None:
            name = str(name_input.value or "").strip()
            base_url = str(base_url_input.value or "").strip()
            if not name or not base_url:
                ui.notify("Name and base URL are required", type="warning")
                return
            save_custom_endpoint({
                "id": name,
                "name": name,
                "base_url": base_url,
                "api_key": str(api_key_input.value or "").strip(),
                "auth_required": not bool(no_auth.value),
                "execution_location": location.value or "remote",
                "transport": "openai_chat",
            })
            ui.notify("Custom endpoint saved", type="positive")
            if on_change:
                on_change()

        ui.button("Save Endpoint", icon="save", on_click=_save).props("flat dense")
