from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import Callable

from row_bot.brand import APP_DISPLAY_NAME
from nicegui import run, ui

from row_bot.ui.timer_utils import defer_ui, safe_ui_task


_xai_oauth_vision_probe_task: asyncio.Task | None = None


@dataclass(frozen=True)
class _ApiKeyProviderUi:
    key_label: str
    help_text: str = ""
    help_url: str = ""
    validator_name: str = ""
    normalizer_name: str = ""
    validation_failure: str = "block"


_API_KEY_PROVIDER_UI: dict[str, _ApiKeyProviderUi] = {
    "openai": _ApiKeyProviderUi(
        key_label="OpenAI API key",
        help_text="Create keys from the OpenAI Platform API keys page.",
        help_url="https://platform.openai.com/api-keys",
    ),
    "ollama_cloud": _ApiKeyProviderUi(
        key_label="Ollama Cloud API key",
        help_text="Paste a direct Ollama Cloud API key. Local daemon cloud models still use ollama signin.",
        help_url="https://ollama.com",
        validator_name="validate_ollama_cloud_key",
        normalizer_name="normalize_ollama_cloud_api_key",
    ),
    "openrouter": _ApiKeyProviderUi(
        key_label="OpenRouter API key",
        help_text="Create keys from OpenRouter account settings.",
        help_url="https://openrouter.ai/settings/keys",
        validator_name="validate_openrouter_key",
    ),
    "requesty": _ApiKeyProviderUi(
        key_label="Requesty API key",
        help_text="Create keys from Requesty's routed model gateway settings.",
        help_url="https://router.requesty.ai",
        validator_name="validate_requesty_key",
    ),
    "opencode_zen": _ApiKeyProviderUi(
        key_label="OpenCode Zen API key",
        help_text="Use a Zen key for pay-per-request coding-agent models.",
        help_url="https://opencode.ai",
    ),
    "opencode_go": _ApiKeyProviderUi(
        key_label="OpenCode Go API key",
        help_text="Use a Go key for OpenCode's open coding model subscription.",
        help_url="https://opencode.ai",
    ),
    "atlascloud": _ApiKeyProviderUi(
        key_label="Atlas Cloud API key",
        help_text="Create keys from Atlas Cloud and use them with the OpenAI-compatible gateway.",
        help_url="https://atlascloud.ai",
        validator_name="validate_atlascloud_key",
    ),
    "anthropic": _ApiKeyProviderUi(
        key_label="Anthropic API key",
        help_text="Create keys from the Anthropic Console API keys page.",
        help_url="https://console.anthropic.com/settings/keys",
        validator_name="validate_anthropic_key",
    ),
    "google": _ApiKeyProviderUi(
        key_label="Google AI API key",
        help_text="Create keys from Google AI Studio.",
        help_url="https://aistudio.google.com/apikey",
        validator_name="validate_google_key",
    ),
    "xai": _ApiKeyProviderUi(
        key_label="xAI API key",
        help_text="Create keys from the xAI Console API keys page.",
        help_url="https://console.x.ai",
        validator_name="validate_xai_key",
        validation_failure="warn",
    ),
    "minimax": _ApiKeyProviderUi(
        key_label="MiniMax API key",
        help_text="Create keys from MiniMax Platform account settings.",
        help_url="https://platform.minimax.io",
        validator_name="validate_minimax_key",
        validation_failure="warn",
    ),
}


def _api_key_provider_ids() -> list[str]:
    from row_bot.providers.catalog import PROVIDER_DEFINITIONS
    from row_bot.providers.models import AuthMethod

    return [
        definition.id
        for definition in PROVIDER_DEFINITIONS.values()
        if AuthMethod.API_KEY in definition.auth_methods
    ]


def _api_key_provider_ui(provider_id: str) -> _ApiKeyProviderUi:
    from row_bot.providers.catalog import get_provider_definition

    provider_id = str(provider_id or "").strip()
    configured = _API_KEY_PROVIDER_UI.get(provider_id)
    if configured is not None:
        return configured
    definition = get_provider_definition(provider_id)
    display_name = definition.display_name if definition is not None else provider_id or "Provider"
    return _ApiKeyProviderUi(key_label=f"{display_name} API key")


def _api_key_provider_action_state(card: dict) -> dict[str, bool]:
    from row_bot.providers.catalog import get_provider_definition
    from row_bot.providers.models import AuthMethod

    provider_id = str(card.get("provider_id") or "").strip()
    definition = get_provider_definition(provider_id)
    can_manage = bool(definition and AuthMethod.API_KEY in definition.auth_methods)
    return {"can_manage_api_key": can_manage}


def _provider_api_key_validator(provider_id: str) -> Callable[[str], bool] | None:
    validator_name = _api_key_provider_ui(provider_id).validator_name
    if not validator_name:
        return None
    from row_bot import models

    validator = getattr(models, validator_name, None)
    return validator if callable(validator) else None


def _normalize_provider_api_key(provider_id: str, raw_value: object) -> str:
    value = str(raw_value or "").strip()
    normalizer_name = _api_key_provider_ui(provider_id).normalizer_name
    if normalizer_name == "normalize_ollama_cloud_api_key":
        from row_bot.providers.transports.ollama_cloud import normalize_ollama_cloud_api_key

        return normalize_ollama_cloud_api_key(value)
    return value


def _clear_provider_runtime_cache() -> None:
    try:
        from row_bot.agent import clear_agent_cache

        clear_agent_cache()
    except Exception:
        pass
    try:
        from row_bot.models import clear_llm_cache

        clear_llm_cache()
    except Exception:
        pass


def _save_provider_api_key_value(provider_id: str, value: str) -> dict:
    from row_bot.providers.auth_store import delete_provider_secret, provider_secret_status, set_provider_secret
    from row_bot.providers.models import AuthMethod

    delete_provider_secret(provider_id, "api_key")
    set_provider_secret(provider_id, "api_key", value, auth_method=AuthMethod.API_KEY)
    _clear_provider_runtime_cache()
    return provider_secret_status(provider_id, "api_key")


def _clear_provider_api_key_value(provider_id: str) -> dict:
    from row_bot.providers.auth_store import delete_provider_secret, provider_secret_status

    delete_provider_secret(provider_id, "api_key")
    _clear_provider_runtime_cache()
    return provider_secret_status(provider_id, "api_key")


def _credential_status_text(status: dict) -> str:
    source = str(status.get("source") or "")
    fingerprint = str(status.get("fingerprint") or "")
    if not status.get("configured"):
        return "No saved API key"
    label = _source_label(source)
    return f"{label} ({fingerprint})" if fingerprint else label


def _start_provider_catalog_refresh_ui(
    *,
    reason: str,
    provider_id: str,
    force: bool = True,
    on_done: Callable[[], object] | None = None,
) -> bool:
    from row_bot.providers.model_catalog_cache import (
        is_model_catalog_refresh_running,
        model_catalog_refresh_state,
        start_model_catalog_refresh_background,
    )

    started = start_model_catalog_refresh_background(reason=reason, provider_id=provider_id, force=force)
    if not started:
        ui.notify("Model catalog refresh is already running", type="info")
        if on_done:
            defer_ui(on_done)
        return False
    ui.notify("Refreshing model catalog in the background...", type="info")

    async def _watch() -> None:
        while is_model_catalog_refresh_running():
            await asyncio.sleep(0.75)
        state_info = model_catalog_refresh_state()
        result = state_info.get("last_result") if isinstance(state_info.get("last_result"), dict) else {}
        if result.get("ok"):
            warnings = result.get("warnings") if isinstance(result.get("warnings"), list) else []
            rows = int(result.get("rows") or 0)
            provider_statuses = result.get("provider_status") if isinstance(result.get("provider_status"), dict) else {}
            provider_result = provider_statuses.get(provider_id) if isinstance(provider_statuses.get(provider_id), dict) else {}
            refresh_status = str(provider_result.get("status") or "")
            model_count = int(provider_result.get("count") or 0)
            message = str(provider_result.get("message") or "")
            if refresh_status == "live":
                ui.notify(f"Provider catalog refreshed: {model_count} models", type="positive")
            elif refresh_status == "cached":
                ui.notify(message or f"No live update; kept {model_count} cached models", type="warning")
            elif refresh_status == "fallback":
                ui.notify(message or f"Using {model_count} fallback models", type="warning")
            elif refresh_status == "empty":
                ui.notify(message or "No provider models were discovered", type="warning")
            elif warnings:
                ui.notify(f"Model catalog refreshed: {rows} models, {len(warnings)} warning(s)", type="warning")
            else:
                ui.notify(f"Model catalog refreshed: {rows} models", type="positive")
        else:
            ui.notify("Catalog refresh failed. Showing last cached catalog.", type="negative")
        if on_done:
            result = on_done()
            if asyncio.iscoroutine(result):
                await result

    safe_ui_task(_watch, context="provider catalog refresh watcher")
    return True


def _probe_detail(last_probe: dict) -> str:
    try:
        from row_bot.providers.custom import custom_probe_summary

        return str(custom_probe_summary(last_probe).get("text") or "No probe details recorded yet.")
    except Exception:
        errors = last_probe.get("errors") if isinstance(last_probe, dict) else []
        if isinstance(errors, list) and errors:
            return "\n".join(str(error) for error in errors[:4])
        return "No probe details recorded yet."


def _probe_chip_color(status: str) -> str:
    return {
        "ok": "green",
        "failed": "orange",
        "inconclusive": "amber",
        "not_probed": "grey",
    }.get(str(status or ""), "grey")


def _probe_chip_label(component: dict) -> str:
    name = str(component.get("name") or component.get("id") or "probe")
    status = str(component.get("status") or "unknown")
    label = {
        "ok": "ok",
        "failed": "failed",
        "inconclusive": "inconclusive",
        "not_probed": "not probed",
        "unknown": "unknown",
    }.get(status, status)
    return f"{name}: {label}"


_INLINE_PROBE_COMPONENT_IDS = {"chat", "tools", "tool_round_trip", "streaming", "streaming_tools", "vision"}


def _probe_checks_summary(summary: dict) -> dict[str, str]:
    components = [
        component
        for component in summary.get("components", [])
        if isinstance(component, dict) and str(component.get("id") or "") in _INLINE_PROBE_COMPONENT_IDS
    ]
    total = len(components)
    if not total:
        return {"label": "checks", "color": "grey"}
    statuses = [str(component.get("status") or "unknown") for component in components]
    ok_count = sum(1 for status in statuses if status == "ok")
    if any(status == "failed" for status in statuses):
        color = "orange"
    elif any(status == "inconclusive" for status in statuses):
        color = "amber"
    elif any(status in {"not_probed", "unknown"} for status in statuses):
        color = "blue-grey"
    else:
        color = "green"
    return {"label": f"{ok_count}/{total} checks", "color": color}


def _manual_capabilities_from_ui(vision_mode: str, tool_mode: str, context_window: object = "") -> dict:
    manual: dict[str, object] = {}
    if vision_mode == "on":
        manual["vision"] = True
    elif vision_mode == "off":
        manual["vision"] = False
    if tool_mode == "on":
        manual["tool_calling"] = True
    elif tool_mode == "off":
        manual["tool_calling"] = False
    try:
        context = int(str(context_window or "").strip())
    except (TypeError, ValueError):
        context = 0
    if context > 0:
        manual["context_window"] = context
    return manual


def _capability_mode(manual: dict, key: str) -> str:
    if manual.get(key) is True:
        return "on"
    if manual.get(key) is False:
        return "off"
    return "auto"


def _custom_endpoint_edit_payload(
    endpoint: dict,
    *,
    display_name: object,
    base_url: object,
    no_auth: bool,
    api_key: object = "",
    vision_mode: str = "auto",
    tool_mode: str = "auto",
    context_window: object = "",
) -> tuple[dict, bool]:
    payload = dict(endpoint)
    payload["id"] = str(endpoint.get("id") or "").strip()
    payload["name"] = str(display_name or "").strip()
    payload["display_name"] = payload["name"]
    payload["base_url"] = str(base_url or "").strip().rstrip("/")
    payload["auth_required"] = not bool(no_auth)
    secret = str(api_key or "").strip()
    if secret:
        payload["api_key"] = secret
    else:
        payload.pop("api_key", None)

    manual_caps = _manual_capabilities_from_ui(vision_mode, tool_mode, context_window)
    old_manual = endpoint.get("manual_capabilities") if isinstance(endpoint.get("manual_capabilities"), dict) else {}
    if manual_caps:
        payload["manual_capabilities"] = manual_caps
    else:
        payload.pop("manual_capabilities", None)

    stale_probe = (
        str(endpoint.get("base_url") or "").strip().rstrip("/") != payload["base_url"]
        or bool(endpoint.get("auth_required")) != payload["auth_required"]
        or dict(old_manual) != manual_caps
    )
    if stale_probe:
        payload.pop("last_probe", None)
        payload.pop("models", None)
    return payload, stale_probe


def _source_label(source: str) -> str:
    labels = {
        "environment": "Using environment variable",
        "secret_file": "Using read-only server secret",
        "conflict": "Server secret configuration conflict",
        "keyring": "Saved in keyring",
        "session": "Using session key",
        "legacy_plaintext": "Using legacy plaintext key",
        "api_keys": "Saved API key",
        "external_cli": "Using external CLI login",
        "external_cli_detected": "External CLI login detected",
        "oauth_device": "Signed in with ChatGPT",
        "oauth_pkce": "Connected with Row-Bot OAuth",
        "no_auth": "No API key required",
        "local_daemon": "Local daemon running",
        "not_running": "Not running",
    }
    return labels.get(source, f"Using {source}" if source else "Connected")


def _open_provider_api_key_dialog(card: dict, *, on_change: Callable[[], object] | None = None) -> None:
    from row_bot.providers.auth_store import PROVIDER_API_KEY_ENV, get_storage_warning, provider_secret_status
    from row_bot.providers.catalog import get_provider_definition

    provider_id = str(card.get("provider_id") or "").strip()
    definition = get_provider_definition(provider_id)
    display_name = str(card.get("display_name") or (definition.display_name if definition else provider_id))
    api_key_ui = _api_key_provider_ui(provider_id)
    env_var = PROVIDER_API_KEY_ENV.get(provider_id, "")
    current_status = provider_secret_status(provider_id, "api_key")
    state_label = "Connected" if card.get("configured") or current_status.get("configured") else "Not connected"
    source = str(current_status.get("source") or "")
    fingerprint = str(current_status.get("fingerprint") or "")
    externally_managed = bool(current_status.get("externally_managed"))

    def _queue_reload() -> None:
        if on_change:
            defer_ui(on_change)

    with ui.dialog() as dialog:
        with ui.card().classes("w-full").style("max-width: 34rem;"):
            with ui.row().classes("items-start justify-between gap-2 no-wrap w-full"):
                with ui.column().classes("gap-0").style("min-width: 0;"):
                    ui.label(f"{display_name} API Key").classes("text-h6")
                    ui.label(state_label).classes("text-grey-6 text-sm")
                ui.badge(str(card.get("risk_label") or "api_key"), color="grey").props("outline dense")

            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.label(_credential_status_text(current_status)).classes("text-grey-6 text-xs")
                if fingerprint:
                    ui.badge(fingerprint, color="blue-grey").props("outline dense").tooltip("Credential fingerprint")
            if env_var:
                if source == "environment":
                    ui.label(f"{env_var} is set by the environment; local saves are stored but the environment value stays active.").classes("text-grey-6 text-xs")
                else:
                    ui.label(f"Compatible environment variable: {env_var}").classes("text-grey-6 text-xs")

            if api_key_ui.help_text:
                ui.label(api_key_ui.help_text).classes("text-grey-6 text-sm")
            if api_key_ui.help_url:
                ui.link("Provider key page", api_key_ui.help_url, new_tab=True).classes("text-primary text-sm")

            key_input = ui.input(
                api_key_ui.key_label,
                value="",
                placeholder="Paste a new value",
                password=True,
                password_toggle_button=True,
            ).props("outlined dense").classes("w-full")
            if externally_managed:
                key_input.disable()
                ui.label(
                    "This credential is mounted by the server operator and is read-only here."
                ).classes("text-grey-6 text-xs")
            status_label = ui.label("").classes("text-grey-6 text-sm")

            async def _save_key() -> None:
                value = _normalize_provider_api_key(provider_id, key_input.value)
                if not value:
                    ui.notify(f"{api_key_ui.key_label} unchanged - enter a new value to save it", type="info")
                    return

                validator = _provider_api_key_validator(provider_id)
                if validator is not None:
                    validation_note = ui.notification(f"Validating {display_name} key...", type="ongoing", spinner=True, timeout=None)
                    try:
                        valid = await run.io_bound(validator, value)
                    except Exception as exc:
                        valid = False
                        status_label.text = f"Validation failed: {exc}"
                        status_label.update()
                    validation_note.dismiss()
                    if not valid and api_key_ui.validation_failure == "block":
                        ui.notify(f"Invalid {display_name} API key", type="negative")
                        return
                    if not valid:
                        ui.notify(
                            f"{display_name} key validation failed; saving anyway. Models will appear if the key is valid.",
                            type="warning",
                            timeout=5000,
                        )

                save_note = ui.notification(f"Saving {display_name} key...", type="ongoing", spinner=True, timeout=None)
                try:
                    await run.io_bound(_save_provider_api_key_value, provider_id, value)
                except Exception as exc:
                    save_note.dismiss()
                    status_label.text = f"Save failed: {exc}"
                    status_label.update()
                    ui.notify(f"Could not save {display_name} key: {exc}", type="negative")
                    return
                save_note.dismiss()
                warning = get_storage_warning()
                if warning:
                    ui.notify(warning, type="warning", close_button=True)
                dialog.close()
                ui.notify(f"{display_name} key saved", type="positive")
                _queue_reload()
                _start_provider_catalog_refresh_ui(
                    reason="provider_key_saved",
                    provider_id=provider_id,
                    force=True,
                    on_done=on_change,
                )

            async def _clear_key() -> None:
                before = await run.io_bound(provider_secret_status, provider_id, "api_key")
                clear_note = ui.notification(f"Clearing {display_name} key...", type="ongoing", spinner=True, timeout=None)
                try:
                    after = await run.io_bound(_clear_provider_api_key_value, provider_id)
                except Exception as exc:
                    clear_note.dismiss()
                    status_label.text = f"Clear failed: {exc}"
                    status_label.update()
                    ui.notify(f"Could not clear {display_name} key: {exc}", type="negative")
                    return
                clear_note.dismiss()
                dialog.close()
                if before.get("source") == "environment" and after.get("configured"):
                    ui.notify(f"Local {display_name} key cleared; {env_var} is still active", type="info")
                else:
                    ui.notify(f"{display_name} key cleared", type="info")
                _queue_reload()
                _start_provider_catalog_refresh_ui(
                    reason="provider_key_cleared",
                    provider_id=provider_id,
                    force=True,
                    on_done=on_change,
                )

            with ui.row().classes("w-full items-center justify-end gap-2"):
                if not externally_managed:
                    ui.button("Clear key", icon="delete", on_click=_clear_key).props("flat dense color=negative")
                ui.button("Close" if externally_managed else "Cancel", icon="close", on_click=dialog.close).props("flat dense")
                if not externally_managed:
                    action = "Replace key" if current_status.get("configured") else "Save key"
                    ui.button(action, icon="save", on_click=_save_key).props("flat dense color=primary")
    dialog.open()


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


def _claude_subscription_action_state(card: dict) -> dict[str, bool]:
    if card.get("provider_id") != "claude_subscription":
        return {}
    configured = bool(card.get("configured"))
    source = str(card.get("source") or "")
    external_exists = bool(card.get("external_reference_exists"))
    return {
        "can_connect": source != "oauth_pkce" or not configured,
        "can_import_setup_token": True,
        "can_reference": external_exists and not configured,
        "can_disconnect": configured or source == "external_cli",
        "can_test_runtime": configured and bool(card.get("runtime_enabled")),
        "runtime_enabled": bool(card.get("runtime_enabled")),
    }


def _xai_oauth_action_state(card: dict) -> dict[str, bool]:
    if card.get("provider_id") != "xai_oauth":
        return {}
    configured = bool(card.get("configured"))
    client_id_configured = bool(card.get("oauth_client_id_configured"))
    runtime_enabled = bool(card.get("runtime_enabled"))
    token_health = str(card.get("token_health") or "")
    needs_reconnect = (not configured) or (not runtime_enabled) or token_health in {"missing", "expired", "error", "revoked"}
    return {
        "can_configure_client_id": True,
        "client_id_configured": client_id_configured,
        "can_connect": client_id_configured and needs_reconnect,
        "can_disconnect": configured,
        "can_test_runtime": configured and runtime_enabled,
        "runtime_enabled": runtime_enabled,
        "needs_reconnect": needs_reconnect,
    }


def build_provider_summary_cards() -> None:
    from row_bot.providers.status import provider_status_cards

    ui.label("Connection Status").classes("text-subtitle2")
    container = ui.column().classes("w-full gap-2")

    def _status_style(configured: bool, source: str) -> tuple[str, str]:
        if configured:
            if source == "external_cli":
                return "#38bdf8", "Referenced"
            if source in {"oauth_device", "oauth_pkce"}:
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
            source = str(card.get("model_count_source") or "")
            if "fallback" in source:
                parts.append(f"{model_count} known models")
            else:
                parts.append(f"{model_count} models")
        elif card.get("configured") or card.get("runtime_enabled"):
            parts.append("catalog count unknown")
        chat_count = int(card.get("chat_count") or 0)
        media_count = int(card.get("media_count") or 0)
        if chat_count:
            parts.append(f"{chat_count} chat")
        if media_count:
            parts.append(f"{media_count} media")
        return " · ".join(parts)

    def _queue_xai_oauth_vision_probe_if_needed(card: dict) -> None:
        global _xai_oauth_vision_probe_task
        if card.get("provider_id") != "xai_oauth":
            return
        if not card.get("configured") or not card.get("runtime_enabled"):
            return
        last_probe = card.get("last_vision_probe") if isinstance(card.get("last_vision_probe"), dict) else {}
        if last_probe:
            try:
                from row_bot.providers.xai_oauth import XAI_OAUTH_VISION_PROBE_VERSION

                if last_probe.get("probe_version") == XAI_OAUTH_VISION_PROBE_VERSION:
                    return
            except Exception:
                return
        if _xai_oauth_vision_probe_task is not None and not _xai_oauth_vision_probe_task.done():
            return

        async def _probe() -> None:
            global _xai_oauth_vision_probe_task
            try:
                from row_bot.providers.xai_oauth import run_xai_oauth_vision_probe

                await run.io_bound(run_xai_oauth_vision_probe)
            except Exception:
                pass
            finally:
                _xai_oauth_vision_probe_task = None
                defer_ui(_load)

        _xai_oauth_vision_probe_task = asyncio.create_task(_probe())

    def _summary_chip(label: str, value: int | str, color: str = "blue-grey") -> None:
        with ui.row().classes("items-center gap-1 px-2 py-1 rounded-borders").style("border: 1px solid rgba(148, 163, 184, 0.24); background: rgba(148, 163, 184, 0.08);"):
            ui.label(str(value)).classes("text-weight-bold text-xs")
            ui.label(label).classes("text-grey-6 text-xs")

    def _reference_codex_login() -> None:
        from row_bot.providers.codex import save_external_reference

        saved = save_external_reference()
        if saved.get("configured"):
            ui.notify("Referenced existing Codex login", type="positive")
        else:
            ui.notify("No Codex auth cache found to reference", type="warning")
        defer_ui(_load)

    async def _connect_codex_login() -> None:
        from row_bot.providers.codex import start_codex_device_flow

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
        from row_bot.providers.codex import exchange_codex_device_authorization, poll_codex_device_authorization, save_codex_oauth_tokens

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
        from row_bot.providers.codex import disconnect_codex_metadata

        disconnect_codex_metadata()
        ui.notify(f"Disconnected {APP_DISPLAY_NAME} Codex metadata", type="info")
        defer_ui(_load)

    def _reference_claude_subscription_login() -> None:
        from row_bot.providers.claude_subscription import save_external_reference

        saved = save_external_reference()
        if saved.get("configured"):
            ui.notify("Referenced external Claude Code login as metadata only", type="positive")
        else:
            ui.notify("No Claude Code credential cache found to reference", type="warning")
        defer_ui(_load)

    async def _connect_claude_subscription_login() -> None:
        from row_bot.providers.claude_subscription import start_claude_subscription_oauth_flow

        notification = ui.notification("Starting Claude Subscription sign-in...", type="ongoing", spinner=True, timeout=None)
        try:
            flow = await run.io_bound(start_claude_subscription_oauth_flow)
        except Exception as exc:
            notification.dismiss()
            ui.notify(f"Could not start Claude Subscription sign-in: {exc}", type="negative")
            return
        notification.dismiss()
        _show_claude_subscription_oauth_dialog(flow)

    def _show_claude_subscription_oauth_dialog(flow) -> None:
        from row_bot.providers.claude_subscription import (
            ClaudeSubscriptionAuthorization,
            exchange_claude_subscription_authorization,
            save_claude_subscription_oauth_tokens,
            seed_recommended_claude_subscription_quick_choices,
        )

        with ui.dialog() as dialog:
            with ui.card().classes("w-full").style("max-width: 32rem;"):
                ui.label("Connect Claude Subscription").classes("text-h6")
                ui.label("Open the authorization page, complete Claude sign-in, then paste the returned authorization code.").classes("text-grey-6 text-sm")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.link("Open Claude Login", flow.authorization_url, new_tab=True).classes("text-primary text-sm")
                    ui.badge("OAuth", color="blue-grey").props("outline dense")
                code_input = ui.input(label="Authorization code").props("outlined dense").classes("w-full")
                ui.label(f"Expires: {flow.expires_at}").classes("text-grey-6 text-xs")
                status_label = ui.label("").classes("text-grey-6 text-sm")

                async def _finish_login() -> None:
                    authorization_code = str(code_input.value or "").strip()
                    if not authorization_code:
                        ui.notify("Paste the Claude authorization code first", type="warning")
                        return
                    check_note = ui.notification("Completing Claude Subscription sign-in...", type="ongoing", spinner=True, timeout=None)
                    try:
                        token_set = await run.io_bound(
                            exchange_claude_subscription_authorization,
                            ClaudeSubscriptionAuthorization(
                                authorization_code=authorization_code,
                                code_verifier=flow.code_verifier,
                                redirect_uri=flow.redirect_uri,
                                token_url=flow.token_url,
                                client_id=flow.client_id,
                                state=flow.state,
                            ),
                        )
                        await run.io_bound(save_claude_subscription_oauth_tokens, token_set)
                        await run.io_bound(seed_recommended_claude_subscription_quick_choices)
                    except Exception as exc:
                        check_note.dismiss()
                        status_label.text = f"Sign-in failed: {exc}"
                        status_label.update()
                        ui.notify(f"Claude Subscription sign-in failed: {exc}", type="negative")
                        return
                    check_note.dismiss()
                    dialog.close()
                    ui.notify("Claude Subscription connected", type="positive")
                    defer_ui(_load)

                with ui.row().classes("w-full items-center justify-end gap-2"):
                    ui.button("Cancel", icon="close", on_click=dialog.close).props("flat dense")
                    ui.button("Connect", icon="check", on_click=_finish_login).props("flat dense color=primary")
        dialog.open()

    def _import_claude_subscription_setup_token_dialog() -> None:
        from row_bot.providers.claude_subscription import (
            import_claude_subscription_setup_token,
            seed_recommended_claude_subscription_quick_choices,
        )

        with ui.dialog() as dialog:
            with ui.card().classes("w-full").style("max-width: 32rem;"):
                ui.label("Import Claude Setup Token").classes("text-h6")
                ui.label("Paste the token printed by claude setup-token. Row-Bot will store it as Claude Subscription OAuth.").classes("text-grey-6 text-sm")
                token_input = ui.input(
                    label="Setup token",
                    password=True,
                    password_toggle_button=True,
                ).props("outlined dense").classes("w-full")
                status_label = ui.label("").classes("text-grey-6 text-sm")

                async def _save_token() -> None:
                    token = str(token_input.value or "").strip()
                    if not token:
                        ui.notify("Paste the Claude setup token first", type="warning")
                        return
                    check_note = ui.notification("Importing Claude setup token...", type="ongoing", spinner=True, timeout=None)
                    try:
                        await run.io_bound(import_claude_subscription_setup_token, token)
                        await run.io_bound(seed_recommended_claude_subscription_quick_choices)
                    except Exception as exc:
                        check_note.dismiss()
                        status_label.text = f"Import failed: {exc}"
                        status_label.update()
                        ui.notify(f"Claude setup token import failed: {exc}", type="negative")
                        return
                    check_note.dismiss()
                    dialog.close()
                    ui.notify("Claude Subscription connected", type="positive")
                    defer_ui(_load)

                with ui.row().classes("w-full items-center justify-end gap-2"):
                    ui.button("Cancel", icon="close", on_click=dialog.close).props("flat dense")
                    ui.button("Import", icon="key", on_click=_save_token).props("flat dense color=primary")
        dialog.open()

    def _disconnect_claude_subscription() -> None:
        from row_bot.providers.claude_subscription import disconnect_claude_subscription_metadata

        disconnect_claude_subscription_metadata()
        ui.notify("Disconnected Claude Subscription metadata", type="info")
        defer_ui(_load)

    async def _test_claude_subscription_runtime() -> None:
        from row_bot.providers.claude_subscription import run_claude_subscription_runtime_probe

        notification = ui.notification("Testing Claude Subscription runtime...", type="ongoing", spinner=True, timeout=None)
        try:
            probe = await run.io_bound(run_claude_subscription_runtime_probe)
        except Exception as exc:
            notification.dismiss()
            ui.notify(f"Claude Subscription runtime test failed: {exc}", type="negative")
            defer_ui(_load)
            return
        notification.dismiss()
        if probe.get("ok"):
            ui.notify("Claude Subscription runtime and tool calls work", type="positive")
        else:
            errors = probe.get("errors") if isinstance(probe.get("errors"), list) else []
            detail = str(errors[0]) if errors else "runtime test did not pass"
            ui.notify(f"Claude Subscription runtime test failed: {detail}", type="warning")
        defer_ui(_load)

    def _configure_xai_oauth_client_id_dialog() -> None:
        from row_bot.providers.xai_oauth import (
            clear_xai_oauth_client_id_override,
            save_xai_oauth_client_id,
            xai_oauth_client_id_status,
            xai_oauth_configured_client_id,
        )

        status = xai_oauth_client_id_status()
        source = str(status.get("source") or "")
        resolved_client_id = xai_oauth_configured_client_id()
        if source == "override":
            source_note = "Using saved OAuth client ID override."
        elif source == "default":
            source_note = "Using Row-Bot default OAuth client ID."
        elif source == "environment":
            source_note = "Using a development OAuth client ID override from the environment."
        else:
            source_note = "No OAuth client ID is available."

        with ui.dialog() as dialog:
            with ui.card().classes("w-full").style("max-width: 32rem;"):
                ui.label("xAI OAuth Client ID Override").classes("text-h6")
                ui.label(
                    "Most users can use Row-Bot's default OAuth client ID. Save an override only if you have your own xAI OAuth app."
                ).classes("text-grey-6 text-sm")
                ui.label(source_note).classes("text-grey-6 text-xs")
                client_id_input = ui.input(label="OAuth client ID override", value=resolved_client_id).props("outlined dense").classes("w-full")
                status_label = ui.label(str(status.get("detail") or "")).classes("text-grey-6 text-sm")

                async def _save_client_id() -> None:
                    client_id = str(client_id_input.value or "").strip()
                    if not client_id:
                        ui.notify("Enter an xAI OAuth client ID override first, or reset to the default.", type="warning")
                        return
                    try:
                        await run.io_bound(save_xai_oauth_client_id, client_id)
                    except Exception as exc:
                        status_label.text = f"Could not save client ID: {exc}"
                        status_label.update()
                        ui.notify(f"Could not save xAI OAuth client ID: {exc}", type="negative")
                        return
                    dialog.close()
                    ui.notify("xAI OAuth client ID saved", type="positive")
                    defer_ui(_load)

                async def _reset_client_id() -> None:
                    try:
                        await run.io_bound(clear_xai_oauth_client_id_override)
                    except Exception as exc:
                        status_label.text = f"Could not reset client ID override: {exc}"
                        status_label.update()
                        ui.notify(f"Could not reset xAI OAuth client ID override: {exc}", type="negative")
                        return
                    dialog.close()
                    ui.notify("Using Row-Bot default OAuth client ID", type="positive")
                    defer_ui(_load)

                with ui.row().classes("w-full items-center justify-end gap-2"):
                    ui.button("Reset to default", icon="restart_alt", on_click=_reset_client_id).props("flat dense")
                    ui.button("Cancel", icon="close", on_click=dialog.close).props("flat dense")
                    ui.button("Save override", icon="key", on_click=_save_client_id).props("flat dense color=primary")
        dialog.open()

    async def _connect_xai_oauth_login() -> None:
        from row_bot.providers.xai_oauth import (
            XAIOAuthError,
            authorization_from_xai_oauth_callback,
            exchange_xai_oauth_authorization,
            save_xai_oauth_tokens,
            seed_recommended_xai_oauth_quick_choices,
            start_xai_oauth_flow,
            wait_for_xai_oauth_loopback_authorization,
        )

        async def _complete_authorization(authorization) -> None:
            token_set = await run.io_bound(exchange_xai_oauth_authorization, authorization)
            await run.io_bound(save_xai_oauth_tokens, token_set)
            try:
                from row_bot.providers.model_catalog_cache import refresh_model_catalog_cache

                await run.io_bound(
                    lambda: refresh_model_catalog_cache(
                        reason="xai_oauth_connected",
                        provider_id="xai_oauth",
                        force=True,
                    )
                )
            except Exception:
                pass
            await run.io_bound(seed_recommended_xai_oauth_quick_choices)

        notification = ui.notification("Opening xAI Grok sign-in...", type="ongoing", spinner=True, timeout=None)
        wait_task: asyncio.Task | None = None
        listener_cancel = threading.Event()
        completed = {"value": False}
        cancelled = {"value": False}

        def _consume_wait_task(task: asyncio.Task) -> None:
            try:
                task.result()
            except BaseException:
                pass

        def _cancel_login_dialog(dialog) -> None:
            cancelled["value"] = True
            listener_cancel.set()
            if wait_task is not None and not wait_task.done():
                wait_task.add_done_callback(_consume_wait_task)
            dialog.close()

        try:
            flow = await run.io_bound(start_xai_oauth_flow)
            listener_ready = threading.Event()
            wait_task = asyncio.create_task(run.io_bound(
                lambda: wait_for_xai_oauth_loopback_authorization(
                    flow,
                    open_browser=False,
                    ready_callback=listener_ready.set,
                    cancel_event=listener_cancel,
                )
            ))
            ready = await run.io_bound(lambda: listener_ready.wait(5))
            if not ready:
                listener_cancel.set()
                wait_task.cancel()
                raise XAIOAuthError("xAI OAuth callback listener did not become ready.", kind="loopback_not_ready")
            with ui.dialog() as login_dialog:
                with ui.card().classes("w-full").style("max-width: 32rem;"):
                    ui.label("Connect xAI Grok").classes("text-h6")
                    ui.label("Waiting for xAI approval in your browser.").classes("text-grey-6 text-sm")
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.spinner(size="sm")
                        ui.link("Open xAI Login", flow.authorization_url, new_tab=True).classes("text-primary text-sm")
                    ui.label("If the page did not open automatically, use the link above.").classes("text-grey-6 text-xs")
                    ui.label(
                        "If the browser shows a 127.0.0.1 error, paste the full callback URL or authorization code below."
                    ).classes("text-grey-6 text-xs")
                    callback_input = ui.input(label="Callback URL or authorization code").props("outlined dense").classes("w-full")
                    status_label = ui.label("").classes("text-grey-6 text-sm")

                    async def _finish_with_pasted_callback() -> None:
                        callback_value = str(callback_input.value or "").strip()
                        if not callback_value:
                            ui.notify("Paste the xAI callback URL or authorization code first", type="warning")
                            return
                        finish_note = ui.notification("Completing xAI Grok sign-in...", type="ongoing", spinner=True, timeout=None)
                        try:
                            authorization = await run.io_bound(authorization_from_xai_oauth_callback, flow, callback_value)
                            await _complete_authorization(authorization)
                        except Exception as exc:
                            finish_note.dismiss()
                            status_label.text = f"Sign-in failed: {exc}"
                            status_label.update()
                            ui.notify(f"xAI Grok sign-in failed: {exc}", type="negative")
                            return
                        finish_note.dismiss()
                        completed["value"] = True
                        listener_cancel.set()
                        if wait_task is not None and not wait_task.done():
                            wait_task.add_done_callback(_consume_wait_task)
                        login_dialog.close()
                        ui.notify("xAI Grok connected", type="positive")
                        defer_ui(_load)

                    with ui.row().classes("w-full items-center justify-end gap-2"):
                        ui.button("Cancel", icon="close", on_click=lambda: _cancel_login_dialog(login_dialog)).props("flat dense")
                        ui.button("Connect with pasted code", icon="check", on_click=_finish_with_pasted_callback).props("flat dense color=primary")
            login_dialog.open()
            notification.dismiss()
            ui.run_javascript(f"window.open({json.dumps(flow.authorization_url)}, '_blank', 'noopener,noreferrer')")
            try:
                authorization = await wait_task
            except XAIOAuthError as exc:
                if (completed["value"] or cancelled["value"]) and exc.kind == "loopback_cancelled":
                    return
                status_label.text = f"Automatic browser callback failed: {exc}"
                status_label.update()
                ui.notify("Paste the xAI callback URL or authorization code to finish sign-in.", type="warning")
                return
            if completed["value"]:
                return
            await _complete_authorization(authorization)
            completed["value"] = True
            listener_cancel.set()
            login_dialog.close()
        except XAIOAuthError as exc:
            notification.dismiss()
            listener_cancel.set()
            if wait_task is not None and not wait_task.done():
                wait_task.add_done_callback(_consume_wait_task)
            ui.notify(f"Could not start xAI Grok sign-in: {exc}", type="negative")
            if exc.kind == "missing_client_id":
                _configure_xai_oauth_client_id_dialog()
            return
        except Exception as exc:
            notification.dismiss()
            listener_cancel.set()
            if wait_task is not None and not wait_task.done():
                wait_task.add_done_callback(_consume_wait_task)
            ui.notify(f"xAI Grok sign-in failed: {exc}", type="negative")
            return
        ui.notify("xAI Grok connected", type="positive")
        defer_ui(_load)

    def _disconnect_xai_oauth() -> None:
        from row_bot.providers.xai_oauth import disconnect_xai_oauth_metadata

        disconnect_xai_oauth_metadata()
        ui.notify("Disconnected xAI Grok metadata", type="info")
        defer_ui(_load)

    async def _test_xai_oauth_runtime() -> None:
        from row_bot.providers.xai_oauth import run_xai_oauth_runtime_probe

        notification = ui.notification("Testing xAI Grok runtime...", type="ongoing", spinner=True, timeout=None)
        try:
            probe = await run.io_bound(run_xai_oauth_runtime_probe)
        except Exception as exc:
            notification.dismiss()
            ui.notify(f"xAI Grok runtime test failed: {exc}", type="negative")
            defer_ui(_load)
            return
        notification.dismiss()
        if probe.get("ok"):
            ui.notify("xAI Grok runtime, tools, and available vision probes work", type="positive")
        else:
            errors = probe.get("errors") if isinstance(probe.get("errors"), list) else []
            detail = str(errors[0]) if errors else "runtime test did not pass"
            ui.notify(f"xAI Grok runtime test failed: {detail}", type="warning")
        defer_ui(_load)

    def _refresh_provider_row(card: dict) -> None:
        provider_id = str(card.get("provider_id") or "").strip()
        if not provider_id:
            defer_ui(_load)
            return
        defer_ui(_load)
        _start_provider_catalog_refresh_ui(
            reason="manual",
            provider_id=provider_id,
            force=True,
            on_done=_load,
        )

    def _render_row(card: dict) -> None:
        _queue_xai_oauth_vision_probe_if_needed(card)
        source = str(card.get("source") or "")
        dot_color, state_label = _status_style(bool(card.get("configured")), source)
        if card.get("provider_id") == "codex" and card.get("configured") and not card.get("runtime_enabled"):
            dot_color, state_label = "#f59e0b", "Reconnect"
        if card.get("provider_id") == "claude_subscription" and card.get("configured") and not card.get("runtime_enabled"):
            dot_color, state_label = "#f59e0b", "Reconnect"
        if card.get("provider_id") == "xai_oauth" and card.get("configured") and not card.get("runtime_enabled"):
            dot_color, state_label = "#f59e0b", "Reconnect"
        metadata = _metadata_label(card)
        fingerprint = str(card.get("fingerprint") or "")
        account_hash = str(card.get("account_id_hash") or "")
        user_hash = str(card.get("user_hash") or "")
        runtime_probe = card.get("last_runtime_probe") if isinstance(card.get("last_runtime_probe"), dict) else {}
        with ui.row().classes("items-center gap-2 no-wrap w-full q-px-sm q-py-xs").style("min-height: 42px; border-bottom: 1px solid rgba(148, 163, 184, 0.14);"):
            ui.label(card.get("icon") or "AI").classes("text-base").style("width: 22px; text-align: center;")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.element("span").style(f"width: 8px; height: 8px; border-radius: 999px; background: {dot_color}; display: inline-block; flex: 0 0 auto;")
                    ui.label(card["display_name"]).classes("text-sm text-weight-medium").style("line-height: 1.15;")
                    ui.label(state_label).classes("text-grey-6 text-xs")
                sub = _source_label(source) if source else "Add credentials to enable this provider"
                if card.get("provider_id") == "codex" and card.get("configured") and not card.get("runtime_enabled"):
                    sub = "Reconnect ChatGPT to use Codex models in chat"
                if card.get("provider_id") == "claude_subscription" and card.get("configured") and not card.get("runtime_enabled"):
                    sub = "Claude Code login found, but Row-Bot runtime is not connected"
                if card.get("provider_id") == "xai_oauth" and card.get("configured") and not card.get("runtime_enabled"):
                    sub = str(card.get("token_health_detail") or "") or "Reconnect xAI Grok to use OAuth models in chat"
                if card.get("provider_id") == "xai_oauth" and not card.get("configured"):
                    if card.get("oauth_client_id_configured"):
                        client_source = str(card.get("oauth_client_id_source") or "")
                        if client_source == "override":
                            sub = "Using saved OAuth client ID override; connect xAI Grok"
                        elif client_source == "default":
                            sub = "Using Row-Bot default OAuth client ID; connect xAI Grok"
                        elif client_source == "environment":
                            sub = "Using development OAuth client ID override; connect xAI Grok"
                        else:
                            sub = "OAuth client ID available; connect xAI Grok"
                    else:
                        sub = "Set an OAuth client ID override to connect xAI Grok"
                if metadata:
                    sub = f"{sub} · {metadata}"
                ui.label(sub).classes("text-grey-6 text-xs").style("line-height: 1.15; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;")
            with ui.row().classes("items-center gap-1 no-wrap"):
                if fingerprint:
                    ui.badge(fingerprint, color="blue-grey").props("outline dense").tooltip("Credential fingerprint")
                if card.get("provider_id") == "xai_oauth" and card.get("oauth_client_id_configured"):
                    client_fingerprint = str(card.get("oauth_client_id_fingerprint") or "")
                    if client_fingerprint:
                        ui.badge(client_fingerprint, color="blue-grey").props("outline dense").tooltip("OAuth client ID fingerprint")
                if account_hash:
                    account_tooltip = "Account fingerprint"
                    if card.get("provider_id") == "codex":
                        account_tooltip = "ChatGPT account fingerprint"
                    elif card.get("provider_id") == "xai_oauth":
                        account_tooltip = "xAI account fingerprint"
                    ui.badge(account_hash, color="blue-grey").props("outline dense").tooltip(account_tooltip)
                if user_hash and not account_hash:
                    ui.badge(user_hash, color="blue-grey").props("outline dense").tooltip("Account fingerprint")
                if card.get("provider_id") == "claude_subscription" and runtime_probe:
                    probe_ok = bool(runtime_probe.get("ok"))
                    errors = runtime_probe.get("errors") if isinstance(runtime_probe.get("errors"), list) else []
                    tooltip = "Claude Subscription runtime test passed" if probe_ok else (str(errors[0]) if errors else "Claude Subscription runtime test failed")
                    ui.badge("runtime ok" if probe_ok else "runtime failed", color="green" if probe_ok else "orange").props("outline dense").tooltip(tooltip)
                if card.get("provider_id") == "xai_oauth" and runtime_probe:
                    probe_ok = bool(runtime_probe.get("ok"))
                    errors = runtime_probe.get("errors") if isinstance(runtime_probe.get("errors"), list) else []
                    tooltip = "xAI Grok runtime test passed" if probe_ok else (str(errors[0]) if errors else "xAI Grok runtime test failed")
                    ui.badge("runtime ok" if probe_ok else "runtime failed", color="green" if probe_ok else "orange").props("outline dense").tooltip(tooltip)
                ui.badge(str(card.get("risk_label") or "api_key"), color="grey").props("outline dense")
                codex_actions = _codex_action_state(card)
                if codex_actions.get("can_connect"):
                    ui.button(icon="login", on_click=_connect_codex_login).props("flat dense round size=sm color=primary").tooltip("Connect ChatGPT in app")
                if codex_actions.get("can_reference"):
                    ui.button(icon="link", on_click=_reference_codex_login).props("flat dense round size=sm").tooltip("Reference existing Codex CLI login")
                if codex_actions.get("can_disconnect"):
                    ui.button(icon="link_off", on_click=_disconnect_codex).props("flat dense round size=sm color=negative").tooltip(f"Disconnect {APP_DISPLAY_NAME} Codex metadata")
                claude_actions = _claude_subscription_action_state(card)
                if claude_actions.get("can_connect"):
                    ui.button(icon="login", on_click=_connect_claude_subscription_login).props("flat dense round size=sm color=primary").tooltip("Connect Claude Subscription in app")
                if claude_actions.get("can_import_setup_token"):
                    ui.button(icon="key", on_click=_import_claude_subscription_setup_token_dialog).props("flat dense round size=sm").tooltip("Import Claude setup token")
                if claude_actions.get("can_test_runtime"):
                    ui.button(icon="science", on_click=_test_claude_subscription_runtime).props("flat dense round size=sm").tooltip("Test Claude Subscription runtime")
                if claude_actions.get("can_reference"):
                    ui.button(icon="link", on_click=_reference_claude_subscription_login).props("flat dense round size=sm").tooltip("Check external Claude Code login")
                if claude_actions.get("can_disconnect"):
                    ui.button(icon="link_off", on_click=_disconnect_claude_subscription).props("flat dense round size=sm color=negative").tooltip("Disconnect Row-Bot Claude Subscription metadata")
                xai_oauth_actions = _xai_oauth_action_state(card)
                if xai_oauth_actions.get("can_configure_client_id"):
                    ui.button(icon="key", on_click=_configure_xai_oauth_client_id_dialog).props("flat dense round size=sm").tooltip("OAuth client ID override")
                if xai_oauth_actions.get("can_connect"):
                    connect_tip = "Reconnect xAI Grok" if xai_oauth_actions.get("needs_reconnect") and card.get("configured") else "Connect xAI Grok"
                    ui.button(icon="login", on_click=_connect_xai_oauth_login).props("flat dense round size=sm color=primary").tooltip(connect_tip)
                if xai_oauth_actions.get("can_test_runtime"):
                    ui.button(icon="science", on_click=_test_xai_oauth_runtime).props("flat dense round size=sm").tooltip("Test xAI Grok runtime")
                if xai_oauth_actions.get("can_disconnect"):
                    ui.button(icon="link_off", on_click=_disconnect_xai_oauth).props("flat dense round size=sm color=negative").tooltip("Disconnect xAI Grok metadata")
                api_key_actions = _api_key_provider_action_state(card)
                if api_key_actions.get("can_manage_api_key"):
                    ui.button(
                        icon="key",
                        on_click=lambda card=card: _open_provider_api_key_dialog(card, on_change=_load),
                    ).props("flat dense round size=sm").tooltip("Manage API key")
                ui.button(
                    icon="refresh",
                    on_click=lambda card=card: _refresh_provider_row(card),
                ).props("flat dense round size=sm").tooltip("Refresh provider status and catalog")

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
        from row_bot.docs_capture import docs_capture_provider_cards

        capture_cards = docs_capture_provider_cards()
        if capture_cards:
            _render(capture_cards)
            return
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
    from row_bot.providers.custom import CUSTOM_ENDPOINT_PROFILES, custom_probe_summary, delete_custom_endpoint, list_custom_endpoints, probe_custom_endpoint, refresh_custom_endpoint_models, save_custom_endpoint

    endpoints = list_custom_endpoints()
    ui.label("Custom / Self-Hosted Endpoints").classes("text-subtitle2")
    if endpoints:
        with ui.column().classes("w-full gap-1"):
            for endpoint in endpoints:
                def _show_probe_details(endpoint=endpoint):
                    last_probe = endpoint.get("last_probe") if isinstance(endpoint.get("last_probe"), dict) else {}
                    summary = custom_probe_summary(last_probe)
                    with ui.dialog() as dialog:
                        with ui.card().classes("w-full").style("max-width: 34rem;"):
                            with ui.row().classes("w-full items-start justify-between gap-2 no-wrap"):
                                with ui.column().classes("gap-0").style("min-width: 0;"):
                                    ui.label(endpoint.get("display_name") or endpoint.get("id") or "Custom endpoint").classes("text-subtitle2")
                                    ui.label(endpoint.get("base_url") or "No base URL").classes("text-grey-6 text-xs").style("overflow-wrap: anywhere;")
                                classification = str(summary.get("classification") or "unknown")
                                ui.badge(
                                    {
                                        "agent_ready": "agent ready",
                                        "chat_only": "chat only",
                                        "unavailable": "unavailable",
                                    }.get(classification, "unknown"),
                                    color="green" if classification == "agent_ready" else "orange" if classification == "chat_only" else "grey",
                                ).props("outline dense")
                            ui.separator()
                            with ui.column().classes("w-full gap-1"):
                                for component in summary.get("components", []):
                                    if not isinstance(component, dict):
                                        continue
                                    status = str(component.get("status") or "unknown")
                                    with ui.row().classes("w-full items-center justify-between gap-3 no-wrap"):
                                        ui.label(str(component.get("name") or component.get("id") or "probe")).classes("text-sm")
                                        ui.badge(
                                            {
                                                "ok": "ok",
                                                "failed": "failed",
                                                "inconclusive": "inconclusive",
                                                "not_probed": "not probed",
                                                "unknown": "unknown",
                                            }.get(status, status),
                                            color=_probe_chip_color(status),
                                        ).props("outline dense")
                                    detail = str(component.get("detail") or "")
                                    if detail:
                                        ui.label(detail).classes("text-grey-6 text-xs q-ml-md").style("overflow-wrap: anywhere;")
                            if last_probe.get("vision_model") or last_probe.get("vision_content_format"):
                                ui.separator()
                                if last_probe.get("vision_model"):
                                    ui.label(f"Vision model: {last_probe.get('vision_model')}").classes("text-grey-6 text-xs").style("overflow-wrap: anywhere;")
                                if last_probe.get("vision_content_format"):
                                    ui.label(f"Vision format: {last_probe.get('vision_content_format')}").classes("text-grey-6 text-xs")
                            errors = last_probe.get("errors") if isinstance(last_probe.get("errors"), list) else []
                            if errors:
                                ui.separator()
                                for error in errors[:4]:
                                    ui.label(str(error)).classes("text-warning text-xs").style("overflow-wrap: anywhere;")
                            with ui.row().classes("w-full justify-end"):
                                ui.button("Close", icon="close", on_click=dialog.close).props("flat dense")
                    dialog.open()

                def _show_endpoint_editor(endpoint=endpoint):
                    manual = endpoint.get("manual_capabilities") if isinstance(endpoint.get("manual_capabilities"), dict) else {}
                    with ui.dialog() as dialog:
                        with ui.card().classes("w-full").style("max-width: 38rem;"):
                            ui.label("Edit Custom Endpoint").classes("text-subtitle2")
                            ui.label("Safe connection details are editable. Profile, location, and endpoint id are fixed after creation.").classes("text-grey-6 text-xs")
                            name_input = ui.input(
                                "Display name",
                                value=str(endpoint.get("display_name") or endpoint.get("name") or endpoint.get("id") or ""),
                            ).classes("w-full").props("dense outlined")
                            base_url_input = ui.input(
                                "Base URL",
                                value=str(endpoint.get("base_url") or ""),
                            ).classes("w-full").props("dense outlined")
                            with ui.row().classes("items-center gap-2"):
                                ui.input("Endpoint id", value=str(endpoint.get("id") or "")).classes("min-w-[160px]").props("dense outlined readonly")
                                ui.input("Profile", value=str(endpoint.get("profile") or "generic_openai")).classes("min-w-[160px]").props("dense outlined readonly")
                                ui.input("Location", value=str(endpoint.get("execution_location") or "remote")).classes("min-w-[140px]").props("dense outlined readonly")
                            no_auth_input = ui.checkbox("No API key required", value=not bool(endpoint.get("auth_required")))
                            api_key_input = ui.input(
                                "API key or token",
                                value="",
                                placeholder="Leave blank to keep the saved key",
                                password=True,
                                password_toggle_button=True,
                            ).classes("w-full")
                            api_key_input.visible = bool(endpoint.get("auth_required"))

                            def _toggle_key_visibility() -> None:
                                api_key_input.visible = not bool(no_auth_input.value)
                                api_key_input.update()

                            no_auth_input.on_value_change(lambda _: _toggle_key_visibility())

                            with ui.expansion("Advanced", icon="tune", value=False).classes("w-full"):
                                ui.label("Use Auto unless endpoint metadata is missing or wrong. Overrides apply to all models exposed by this endpoint.").classes("text-grey-6 text-xs")
                                vision_mode = ui.select(
                                    {"auto": "Auto", "on": "Force Vision on", "off": "Force Vision off"},
                                    value=_capability_mode(manual, "vision"),
                                    label="Vision input",
                                ).classes("w-full").props("dense outlined")
                                ui.label("Force on makes this endpoint's models appear in Vision-capable pickers.").classes("text-grey-6 text-xs")
                                tool_mode = ui.select(
                                    {"auto": "Auto", "on": "Force tools on", "off": "Force tools off"},
                                    value=_capability_mode(manual, "tool_calling"),
                                    label="Tool calling",
                                ).classes("w-full").props("dense outlined")
                                ui.label("Agent readiness still depends on the live probe proving tool round-trip.").classes("text-grey-6 text-xs")
                                context_input = ui.input(
                                    "Native context limit",
                                    value=str(manual.get("context_window") or ""),
                                    placeholder="Auto",
                                ).classes("w-full").props("dense outlined")
                                ui.label(f"Used as {APP_DISPLAY_NAME}'s provider ceiling for trimming and readiness. The app-wide context setting still caps actual usage.").classes("text-grey-6 text-xs")

                            async def _save_edit(endpoint=endpoint):
                                name = str(name_input.value or "").strip()
                                base_url = str(base_url_input.value or "").strip()
                                if not name or not base_url:
                                    ui.notify("Display name and base URL are required", type="warning")
                                    return
                                payload, stale = _custom_endpoint_edit_payload(
                                    endpoint,
                                    display_name=name,
                                    base_url=base_url,
                                    no_auth=bool(no_auth_input.value),
                                    api_key=api_key_input.value,
                                    vision_mode=str(vision_mode.value or "auto"),
                                    tool_mode=str(tool_mode.value or "auto"),
                                    context_window=context_input.value,
                                )
                                save_custom_endpoint(payload)
                                storage_warning = ""
                                if payload.get("api_key"):
                                    from row_bot.providers.auth_store import get_storage_warning as get_provider_storage_warning

                                    storage_warning = get_provider_storage_warning()
                                notification = ui.notification("Endpoint saved; refreshing models...", type="ongoing", spinner=True, timeout=None)
                                try:
                                    await run.io_bound(refresh_custom_endpoint_models, endpoint["id"])
                                    try:
                                        from row_bot.providers.selection import refresh_quick_choice_capability_snapshots

                                        await run.io_bound(refresh_quick_choice_capability_snapshots)
                                    except Exception:
                                        pass
                                    notification.dismiss()
                                    suffix = "; probe again to verify readiness" if stale else ""
                                    if storage_warning:
                                        ui.notify(
                                            "Endpoint saved for this session only; configure secure storage to persist it",
                                            type="warning",
                                            close_button=True,
                                        )
                                    else:
                                        ui.notify(f"Endpoint saved{suffix}", type="positive", close_button=True)
                                except Exception as exc:
                                    notification.dismiss()
                                    ui.notify(f"Endpoint saved, but model refresh failed: {exc}", type="warning", close_button=True)
                                dialog.close()
                                if on_change:
                                    on_change()

                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button("Cancel", icon="close", on_click=dialog.close).props("flat dense")
                                ui.button("Save", icon="save", on_click=_save_edit).props("flat dense color=primary")
                    dialog.open()

                with ui.row().classes("items-center gap-2 no-wrap w-full"):
                    ui.icon("hub", size="sm")
                    with ui.column().classes("gap-0"):
                        ui.label(endpoint.get("display_name") or endpoint.get("id")).classes("text-sm text-weight-medium")
                        ui.label(endpoint.get("base_url") or "No base URL").classes("text-grey-6 text-xs")
                    ui.badge(endpoint.get("execution_location") or "remote", color="blue-grey").props("outline dense")
                    ui.badge(endpoint.get("profile") or "generic_openai", color="cyan").props("outline dense")
                    ui.badge(endpoint.get("transport") or "openai_chat", color="grey").props("outline dense")
                    last_probe = endpoint.get("last_probe") if isinstance(endpoint.get("last_probe"), dict) else {}
                    if last_probe:
                        summary = custom_probe_summary(last_probe)
                        classification = str(summary.get("classification") or "")
                        primary_label = {
                            "agent_ready": "agent ready",
                            "chat_only": "chat only",
                            "unavailable": "unavailable",
                        }.get(classification, "probe unknown")
                        ui.badge(
                            primary_label,
                            color="green" if classification == "agent_ready" else "orange" if classification == "chat_only" else "grey",
                        ).props("outline dense").tooltip(_probe_detail(last_probe))
                        checks = _probe_checks_summary(summary)
                        ui.button(
                            icon="fact_check",
                            on_click=_show_probe_details,
                        ).props(f"flat dense round size=sm color={checks['color']}").tooltip(f"Show probe details ({checks['label']})")
                    models = endpoint.get("models") if isinstance(endpoint.get("models"), list) else []
                    if models:
                        ui.badge(f"{len(models)} models", color="grey").props("outline dense")
                    ui.space()

                    def _delete(endpoint_id=endpoint["id"]):
                        removed_pins = delete_custom_endpoint(endpoint_id) or 0
                        if removed_pins:
                            ui.notify(
                                f"Custom endpoint removed; cleared {removed_pins} stale model picker entr{'y' if removed_pins == 1 else 'ies'}",
                                type="info",
                            )
                        else:
                            ui.notify("Custom endpoint removed", type="info")
                        if on_change:
                            on_change()

                    async def _refresh(endpoint_id=endpoint["id"]):
                        notification = ui.notification("Refreshing endpoint models...", type="ongoing", spinner=True, timeout=None)
                        try:
                            infos = await run.io_bound(refresh_custom_endpoint_models, endpoint_id)
                            notification.dismiss()
                            stale_pin_count = int(getattr(infos, "stale_pin_count", 0) or 0)
                            default_reset = bool(getattr(infos, "default_reset", False))
                            suffix = ""
                            if stale_pin_count:
                                suffix += f"; removed {stale_pin_count} stale picker entr{'y' if stale_pin_count == 1 else 'ies'}"
                            if default_reset:
                                suffix += "; reset stale Brain default"
                            ui.notify(f"Found {len(infos)} model(s){suffix}", type="positive")
                            if on_change:
                                on_change()
                        except Exception as exc:
                            notification.dismiss()
                            ui.notify(f"Refresh failed: {exc}", type="negative")

                    async def _probe(endpoint_id=endpoint["id"]):
                        notification = ui.notification("Probing endpoint...", type="ongoing", spinner=True, timeout=None)
                        try:
                            result = await run.io_bound(probe_custom_endpoint, endpoint_id)
                            notification.dismiss()
                            if result.get("ok"):
                                ui.notify(f"Endpoint probe passed: {custom_probe_summary(result).get('text', '')}", type="positive", close_button=True, timeout=9000)
                            else:
                                ui.notify(
                                    f"Endpoint probe failed: {_probe_detail(result)}",
                                    type="warning",
                                    close_button=True,
                                    timeout=12000,
                                )
                            if on_change:
                                on_change()
                        except Exception as exc:
                            notification.dismiss()
                            ui.notify(f"Probe failed: {exc}", type="negative")

                    ui.button(icon="refresh", on_click=_refresh).props("flat dense round size=sm").tooltip("Refresh models")
                    ui.button(icon="science", on_click=_probe).props("flat dense round size=sm").tooltip("Probe endpoint")
                    ui.button(icon="edit", on_click=_show_endpoint_editor).props("flat dense round size=sm").tooltip("Edit endpoint")
                    ui.button(icon="delete", on_click=_delete).props("flat dense round size=sm color=negative").tooltip("Remove endpoint")
    else:
        ui.label("Connect vLLM, llama.cpp, LM Studio, LocalAI, LiteLLM, or another OpenAI-compatible API.").classes("text-grey-6 text-sm")

    with ui.expansion("Add custom endpoint", icon="add", value=False).classes("w-full"):
        name_input = ui.input("Name", placeholder="Local vLLM").classes("w-full")
        base_url_input = ui.input("Base URL", placeholder="http://127.0.0.1:8000/v1").classes("w-full")
        with ui.row().classes("items-center gap-3"):
            profile_options = {
                key: str(value.get("display_name") or key)
                for key, value in CUSTOM_ENDPOINT_PROFILES.items()
            }
            profile = ui.select(
                profile_options,
                value="generic_openai",
                label="Endpoint profile",
            ).classes("min-w-[200px]")
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
        with ui.expansion("Advanced", icon="tune", value=False).classes("w-full"):
            ui.label("Use Auto unless endpoint metadata is missing or wrong. Overrides apply to all models exposed by this endpoint.").classes("text-grey-6 text-xs")
            with ui.row().classes("items-center gap-3"):
                vision_override = ui.select(
                    {"auto": "Auto", "on": "Force Vision on", "off": "Force Vision off"},
                    value="auto",
                    label="Vision input",
                ).classes("min-w-[180px]")
                tool_override = ui.select(
                    {"auto": "Auto", "on": "Force tools on", "off": "Force tools off"},
                    value="auto",
                    label="Tool calling",
                ).classes("min-w-[180px]")
                context_override = ui.input("Native context limit", placeholder="Auto").classes("min-w-[180px]")
            ui.label("Agent readiness still depends on live probe results. The app-wide context setting still caps actual context usage.").classes("text-grey-6 text-xs")

        def _save() -> None:
            name = str(name_input.value or "").strip()
            base_url = str(base_url_input.value or "").strip()
            if not name or not base_url:
                ui.notify("Name and base URL are required", type="warning")
                return
            payload = {
                "id": name,
                "name": name,
                "base_url": base_url,
                "api_key": str(api_key_input.value or "").strip(),
                "auth_required": not bool(no_auth.value),
                "execution_location": location.value or "remote",
                "profile": profile.value or "generic_openai",
                "transport": "openai_chat",
            }
            manual_caps = _manual_capabilities_from_ui(
                str(vision_override.value or "auto"),
                str(tool_override.value or "auto"),
                context_override.value,
            )
            if manual_caps:
                payload["manual_capabilities"] = manual_caps
            save_custom_endpoint(payload)
            storage_warning = ""
            if payload.get("api_key"):
                from row_bot.providers.auth_store import get_storage_warning as get_provider_storage_warning

                storage_warning = get_provider_storage_warning()
            if storage_warning:
                ui.notify(
                    "Custom endpoint saved for this session only; configure secure storage to persist it",
                    type="warning",
                    close_button=True,
                )
            else:
                ui.notify("Custom endpoint saved", type="positive")
            if on_change:
                on_change()

        ui.button("Save Endpoint", icon="save", on_click=_save).props("flat dense")
