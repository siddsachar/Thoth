"""Row-Bot UI — first-launch setup wizard.

Builds the full-screen ``ui.dialog`` that walks the user through
model selection and API key configuration on first launch.

The wizard is self-contained except for two callbacks:

* **on_finish** — called (no args) when the user clicks *Get Started*.
  The caller should use this to e.g. call ``_rebuild_main()`` to
  transition to the main UI.
"""

from __future__ import annotations

import asyncio
import json
import ipaddress
import logging
import sys
import threading
from typing import Any, Callable, TYPE_CHECKING
from urllib.parse import urlparse

from row_bot.brand import APP_DISPLAY_NAME
from row_bot.docs_capture import docs_capture_fake_provider_status, is_docs_capture
from nicegui import run, ui

if TYPE_CHECKING:
    from row_bot.ui.state import AppState

logger = logging.getLogger(__name__)


def _custom_endpoint_host_label(base_url: str) -> str:
    parsed = urlparse(base_url if "://" in str(base_url) else f"http://{base_url}")
    host = parsed.hostname or str(base_url or "").strip().rstrip("/")
    if parsed.port and parsed.hostname:
        return f"{parsed.hostname}:{parsed.port}"
    return host or "endpoint"


def _custom_endpoint_execution_location(base_url: str) -> str:
    host = urlparse(base_url if "://" in str(base_url) else f"http://{base_url}").hostname or ""
    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"} or normalized.endswith(".local"):
        return "local"
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return "remote"
    return "local" if ip.is_private or ip.is_loopback else "remote"


def build_custom_endpoint_setup_payload(
    base_url: str,
    api_key: str = "",
    profile: str = "generic_openai",
    *,
    name: str = "",
    execution_location: str | None = None,
    auth_required: bool | None = None,
) -> dict[str, str | bool]:
    clean_url = str(base_url or "").strip().rstrip("/")
    clean_key = str(api_key or "").strip()
    host_label = _custom_endpoint_host_label(clean_url)
    provided_name = str(name or "").strip()
    clean_name = provided_name or f"Self-hosted ({host_label})"
    clean_location = str(execution_location or "").strip().lower()
    if clean_location not in {"local", "remote"}:
        clean_location = _custom_endpoint_execution_location(clean_url)
    return {
        "id": clean_name if provided_name else host_label,
        "name": clean_name,
        "base_url": clean_url,
        "api_key": clean_key,
        "auth_required": bool(clean_key) if auth_required is None else bool(auth_required),
        "execution_location": clean_location,
        "profile": profile or "generic_openai",
        "transport": "openai_chat",
    }


def custom_endpoint_model_options(model_infos: list) -> dict[str, str]:
    return {
        info.selection_ref: f"↔ {info.display_name or info.model_id}"
        for info in model_infos
        if getattr(info, "selection_ref", "") and getattr(info, "model_id", "")
    }


def cloud_model_setup_option(
    cache_key: str,
    cache_entry: dict[str, Any] | None = None,
    *,
    emoji_lookup: Callable[[str], str] | None = None,
) -> dict[str, str]:
    """Build a setup-wizard option from a legacy or provider-qualified cache row."""
    from row_bot.providers.selection import model_choice_value, parse_model_ref

    raw_key = str(cache_key or "").strip()
    entry = cache_entry if isinstance(cache_entry, dict) else {}
    parsed = parse_model_ref(raw_key)
    if parsed:
        provider_id, model_id = parsed
    else:
        provider_id = str(entry.get("provider") or "")
        model_id = raw_key
    value = model_choice_value(raw_key if parsed else model_id, provider_id=provider_id)
    display_name = str(entry.get("label") or model_id)
    emoji = emoji_lookup(value) if emoji_lookup else ""
    label = f"{emoji} {display_name}".strip()
    return {
        "value": value,
        "label": label,
        "provider_id": provider_id,
        "model_id": model_id,
        "display_name": display_name,
    }


async def show_setup_wizard(
    state: AppState,
    *,
    on_finish: Callable[[], None],
) -> None:
    """Build and open the first-launch setup wizard dialog.

    Must be called inside a NiceGUI page context (``@ui.page``).
    """
    # Lazy imports — only needed when the wizard actually runs
    from row_bot.models import (
        validate_ollama_cloud_key,
        validate_openrouter_key,
        validate_requesty_key,
        validate_anthropic_key,
        validate_google_key,
        validate_xai_key,
        validate_minimax_key,
        validate_atlascloud_key,
        refresh_cloud_models,
        list_cloud_models,
        get_provider_emoji,
        list_cloud_vision_models,
        set_model,
        list_local_models,
        _ollama_reachable,
        _cloud_model_cache,
    )
    from row_bot.api_keys import set_key
    from row_bot.agent import clear_agent_cache
    from row_bot.ui.helpers import mark_setup_complete
    from row_bot.ui.onboarding_state import (
        INTENT_OPTIONS,
        mark_onboarding_step,
        request_setup_center_on_next_load,
        save_onboarding_profile,
    )
    from row_bot.providers.selection import add_quick_choice_for_model, model_choice_value, model_id_from_choice_value, parse_model_ref
    from row_bot.providers.custom import (
        CUSTOM_ENDPOINT_PROFILES,
        endpoint_id_from_provider_id,
        refresh_custom_endpoint_models,
        save_custom_endpoint,
    )
    from row_bot.providers.auth_store import get_storage_warning as get_provider_storage_warning
    from row_bot.providers.codex import (
        codex_runtime_available,
        exchange_codex_device_authorization,
        list_codex_model_infos,
        poll_codex_device_authorization,
        save_codex_oauth_tokens,
        start_codex_device_flow,
    )
    from row_bot.providers.xai_oauth import (
        XAIOAuthError,
        authorization_from_xai_oauth_callback,
        exchange_xai_oauth_authorization,
        list_xai_oauth_model_infos,
        run_xai_oauth_vision_probe,
        save_xai_oauth_client_id,
        save_xai_oauth_tokens,
        seed_recommended_xai_oauth_quick_choices,
        start_xai_oauth_flow,
        wait_for_xai_oauth_loopback_authorization,
        xai_oauth_runtime_available,
    )

    def _open_first_run_migration_wizard() -> None:
        with ui.dialog().props("maximized") as migration_dlg:
            with ui.card().classes("w-full h-full no-shadow").style(
                "max-width: 64rem; margin: 0 auto;"
            ):
                with ui.row().classes("w-full items-center justify-between px-4 pt-3 pb-1"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("move_up", size="sm")
                        ui.label("Import from Hermes/OpenClaw").classes("text-h5")
                    ui.button(icon="close", on_click=migration_dlg.close).props("flat round size=sm")
                ui.separator()
                with ui.scroll_area().classes("w-full").style("height: calc(100vh - 76px);"):
                    with ui.column().classes("w-full px-6 py-4"):
                        __import__(
                            "row_bot.ui.migration_wizard",
                            fromlist=["build_migration_wizard_tab"],
                        ).build_migration_wizard_tab()
        migration_dlg.open()

    setup_dlg = ui.dialog().props(
        "persistent maximized transition-show=fade transition-hide=fade"
    )

    with setup_dlg:
        with ui.card().classes("w-full max-w-4xl mx-auto q-pa-lg").props("data-docs-id=setup-wizard").style(
            "border-radius: 10px;"
        ):
            selected_intents: set[str] = set()

            # ── Header ───────────────────────────────────────────────
            with ui.row().classes("w-full items-center justify-between gap-3"):
                with ui.column().classes("gap-0"):
                    ui.label(f"Welcome to {APP_DISPLAY_NAME}").classes("text-h4 text-weight-medium")
                    ui.label(
                        "Connect one working model first. Everything else can wait."
                    ).classes("text-grey-6 text-sm")
                ui.badge("1 Model  ·  2 Import  ·  3 Ready", color="blue-grey").props("outline")

            ui.separator()

            # ── Setup Path Toggle ────────────────────────────────────
            ui.label("Connect your first model").classes("text-h6")
            ui.label(
                f"Pick the path you want to use first. {APP_DISPLAY_NAME} only needs one working model to get started."
            ).classes("text-grey-6 text-sm")

            setup_path: dict[str, str | None] = {"mode": None}

            with ui.row().classes("w-full gap-4 q-my-sm"):
                def _pick_local():
                    setup_path["mode"] = "local"
                    _local_section.visible = True
                    _cloud_section.visible = False
                    _custom_section.visible = False
                    _update_finish()

                def _pick_cloud():
                    setup_path["mode"] = "cloud"
                    _local_section.visible = False
                    _cloud_section.visible = True
                    _custom_section.visible = False
                    _update_finish()

                def _pick_custom():
                    setup_path["mode"] = "custom"
                    _local_section.visible = False
                    _cloud_section.visible = False
                    _custom_section.visible = True
                    _update_finish()

                ui.button("Local (Ollama)", icon="desktop_windows", on_click=_pick_local).props(
                    "color=primary outline"
                ).classes("flex-grow")
                ui.button("API providers", icon="key", on_click=_pick_cloud).props(
                    "color=cyan outline"
                ).classes("flex-grow")
                ui.button("Custom endpoint", icon="hub", on_click=_pick_custom).props(
                    "color=teal outline"
                ).classes("flex-grow")

            _cloud_section = ui.column().classes("w-full")
            _cloud_section.visible = False
            _custom_section = ui.column().classes("w-full")
            _custom_section.visible = False
            _local_section = ui.column().classes("w-full")
            _local_section.visible = False

            # ── Provider Setup Path ──────────────────────────────────
            cloud_done: dict[str, bool] = {"value": False}
            codex_models_by_ref: dict[str, object] = {}
            xai_oauth_models_by_ref: dict[str, object] = {}
            xai_oauth_vision_probe_task: asyncio.Task | None = None
            with _cloud_section:
                ui.label(
                    "Use ChatGPT / Codex, xAI Grok, or save one API key and fetch available models."
                ).classes("text-grey-6 text-sm")

                setup_openai_key = ui.input(
                    "OpenAI API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_ollama_cloud_key = ui.input(
                    "Ollama Cloud API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_anth_key = ui.input(
                    "Anthropic API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_goog_key = ui.input(
                    "Google AI API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_xai_key = ui.input(
                    "xAI API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_xai_oauth_client_id = ui.input(
                    "xAI OAuth Client ID override (optional)",
                ).classes("w-full")
                setup_minimax_key = ui.input(
                    "MiniMax API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_atlascloud_key = ui.input(
                    "Atlas Cloud API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_or_key = ui.input(
                    "OpenRouter API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_requesty_key = ui.input(
                    "Requesty API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_opencode_zen_key = ui.input(
                    "OpenCode Zen API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_opencode_go_key = ui.input(
                    "OpenCode Go API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")

                cloud_status = ui.label("").classes("text-sm")
                cloud_status.visible = False
                cloud_model_select = ui.select(
                    label="Default provider model",
                    options=[],
                ).classes("w-full").props("use-input input-debounce=300")
                cloud_model_select.visible = False

                cloud_vision_select = ui.select(
                    label="Vision model (optional — for camera/screenshot analysis)",
                    options=[],
                ).classes("w-full").props("use-input input-debounce=300")
                cloud_vision_select.visible = False

                def _update_xai_oauth_vision_options(infos: list) -> bool:
                    vision_opts = {
                        info.selection_ref: f"X {info.display_name or info.model_id}"
                        for info in infos
                        if "image" in getattr(info, "input_modalities", frozenset())
                    }
                    if not vision_opts:
                        cloud_vision_select.set_value(None)
                        cloud_vision_select.visible = False
                        return False
                    cloud_vision_select.options = vision_opts
                    cloud_vision_select.set_value(next(iter(vision_opts)))
                    cloud_vision_select.visible = True
                    return True

                def _queue_xai_oauth_vision_probe_if_needed() -> None:
                    nonlocal xai_oauth_vision_probe_task
                    if xai_oauth_vision_probe_task is not None and not xai_oauth_vision_probe_task.done():
                        return

                    async def _probe() -> None:
                        try:
                            await run.io_bound(run_xai_oauth_vision_probe)
                            infos = await run.io_bound(list_xai_oauth_model_infos)
                        except Exception:
                            return
                        xai_oauth_models_by_ref.update({
                            info.selection_ref: info
                            for info in infos
                            if getattr(info, "selection_ref", "")
                        })
                        _update_xai_oauth_vision_options(infos)

                    xai_oauth_vision_probe_task = asyncio.create_task(_probe())

                async def _load_codex_models() -> bool:
                    try:
                        infos = await run.io_bound(lambda: list_codex_model_infos(force_refresh=True))
                    except Exception as exc:
                        logger.warning("Codex setup check failed", exc_info=True)
                        cloud_status.text = f"Could not use ChatGPT / Codex: {exc}"
                        cloud_done["value"] = False
                        _update_finish()
                        return False
                    opts = {
                        info.selection_ref: f"C {info.display_name or info.model_id}"
                        for info in infos
                        if getattr(info, "selection_ref", "")
                    }
                    if not opts:
                        cloud_status.text = "No ChatGPT / Codex models were found."
                        cloud_done["value"] = False
                        _update_finish()
                        return False
                    codex_models_by_ref.clear()
                    xai_oauth_models_by_ref.clear()
                    codex_models_by_ref.update({info.selection_ref: info for info in infos})
                    first = next(iter(opts))
                    cloud_model_select.options = opts
                    cloud_model_select.set_value(first)
                    cloud_model_select.visible = True
                    cloud_status.text = f"Found {len(opts)} ChatGPT / Codex model(s)"
                    info = codex_models_by_ref.get(first)
                    if info:
                        add_quick_choice_for_model(
                            info.model_id,
                            provider_id=info.provider_id,
                            display_name=info.display_name,
                            source="setup_default",
                            capabilities_snapshot=info.capability_snapshot(),
                        )
                    cloud_done["value"] = True
                    _update_finish()
                    return True

                async def _load_xai_oauth_models() -> bool:
                    try:
                        infos = await run.io_bound(lambda: list_xai_oauth_model_infos(force_refresh=True))
                    except Exception as exc:
                        logger.warning("xAI Grok setup check failed", exc_info=True)
                        cloud_status.text = f"Could not use xAI Grok: {exc}"
                        cloud_done["value"] = False
                        _update_finish()
                        return False
                    opts = {
                        info.selection_ref: f"X {info.display_name or info.model_id}"
                        for info in infos
                        if getattr(info, "selection_ref", "")
                    }
                    if not opts:
                        cloud_status.text = "No xAI Grok models were found."
                        cloud_done["value"] = False
                        _update_finish()
                        return False
                    codex_models_by_ref.clear()
                    xai_oauth_models_by_ref.clear()
                    xai_oauth_models_by_ref.update({info.selection_ref: info for info in infos})
                    first = next(iter(opts))
                    cloud_model_select.options = opts
                    cloud_model_select.set_value(first)
                    cloud_model_select.visible = True
                    if not _update_xai_oauth_vision_options(infos):
                        _queue_xai_oauth_vision_probe_if_needed()
                    cloud_status.text = f"Found {len(opts)} xAI Grok model(s)"
                    info = xai_oauth_models_by_ref.get(first)
                    if info:
                        add_quick_choice_for_model(
                            info.model_id,
                            provider_id=info.provider_id,
                            display_name=info.display_name,
                            source="setup_default",
                            capabilities_snapshot=info.capability_snapshot(),
                        )
                    cloud_done["value"] = True
                    _update_finish()
                    return True

                def _show_codex_device_dialog(flow) -> None:
                    with ui.dialog() as codex_dialog:
                        with ui.card().classes("w-full").style("max-width: 30rem;"):
                            ui.label("Connect ChatGPT / Codex").classes("text-h6")
                            ui.label(
                                "Open the verification page, enter this code, then return here."
                            ).classes("text-grey-6 text-sm")
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.link("Open OpenAI Login", flow.verification_uri, new_tab=True).classes(
                                    "text-primary text-sm"
                                )
                                ui.badge("Codex", color="blue-grey").props("outline dense")
                            with ui.row().classes("items-center gap-2 no-wrap w-full q-my-sm"):
                                ui.input(value=flow.user_code).props("readonly outlined dense").classes(
                                    "text-h5 text-weight-bold"
                                ).style("letter-spacing: 0; max-width: 16rem;")
                                ui.button(
                                    icon="content_copy",
                                    on_click=lambda: (
                                        ui.run_javascript(
                                            f"navigator.clipboard.writeText({json.dumps(flow.user_code)})"
                                        ),
                                        ui.notify("Code copied", type="positive"),
                                    ),
                                ).props("flat dense round size=sm color=primary").tooltip("Copy code")
                            ui.label(f"Expires: {flow.expires_at}").classes("text-grey-6 text-xs")
                            status_label = ui.label("Waiting for OpenAI confirmation.").classes(
                                "text-grey-6 text-sm"
                            )

                            async def _check_login() -> None:
                                check_note = ui.notification(
                                    "Checking ChatGPT sign-in...",
                                    type="ongoing",
                                    spinner=True,
                                    timeout=None,
                                )
                                try:
                                    authorization = await run.io_bound(
                                        poll_codex_device_authorization,
                                        flow,
                                    )
                                    if authorization is None:
                                        check_note.dismiss()
                                        status_label.text = "Still waiting for OpenAI confirmation."
                                        status_label.update()
                                        ui.notify("Login is still pending", type="info")
                                        return
                                    token_set = await run.io_bound(
                                        exchange_codex_device_authorization,
                                        authorization,
                                    )
                                    await run.io_bound(save_codex_oauth_tokens, token_set)
                                    storage_warning = get_provider_storage_warning()
                                except Exception as exc:
                                    check_note.dismiss()
                                    logger.warning("Codex setup sign-in failed", exc_info=True)
                                    status_label.text = f"Sign-in failed: {exc}"
                                    status_label.update()
                                    ui.notify(f"ChatGPT sign-in failed: {exc}", type="negative")
                                    return
                                check_note.dismiss()
                                codex_dialog.close()
                                ui.notify("ChatGPT / Codex connected", type="positive")
                                if storage_warning:
                                    ui.notify(storage_warning, type="warning", close_button=True)
                                cloud_status.text = "ChatGPT / Codex connected. Fetching models..."
                                cloud_status.visible = True
                                await _load_codex_models()

                            with ui.row().classes("w-full items-center justify-end gap-2"):
                                ui.button("Cancel", icon="close", on_click=codex_dialog.close).props(
                                    "flat dense"
                                )
                                ui.button("Check Login", icon="check", on_click=_check_login).props(
                                    "flat dense color=primary"
                                )
                    codex_dialog.open()

                async def _use_codex_provider():
                    cloud_status.text = "Checking ChatGPT / Codex provider..."
                    cloud_status.visible = True
                    if codex_runtime_available():
                        await _load_codex_models()
                        return
                    notification = ui.notification(
                        "Starting ChatGPT sign-in...",
                        type="ongoing",
                        spinner=True,
                        timeout=None,
                    )
                    try:
                        flow = await run.io_bound(start_codex_device_flow)
                    except Exception as exc:
                        notification.dismiss()
                        logger.warning("Codex setup sign-in start failed", exc_info=True)
                        cloud_status.text = f"Could not start ChatGPT sign-in: {exc}"
                        cloud_done["value"] = False
                        _update_finish()
                        return
                    notification.dismiss()
                    cloud_status.text = "Complete ChatGPT sign-in, then check login here."
                    _show_codex_device_dialog(flow)

                ui.button(
                    "Use ChatGPT / Codex",
                    icon="login",
                    on_click=_use_codex_provider,
                ).props("outline color=blue-grey").classes("q-mb-sm")

                async def _use_xai_oauth_provider():
                    cloud_status.text = "Checking xAI Grok provider..."
                    cloud_status.visible = True
                    if xai_oauth_runtime_available():
                        await _load_xai_oauth_models()
                        return
                    xai_oauth_client_id = str(setup_xai_oauth_client_id.value or "").strip()
                    notification = ui.notification(
                        "Starting xAI Grok sign-in...",
                        type="ongoing",
                        spinner=True,
                        timeout=None,
                    )
                    wait_task: asyncio.Task | None = None
                    listener_cancel = threading.Event()
                    completed = {"value": False}
                    cancelled = {"value": False}

                    def _consume_wait_task(task: asyncio.Task) -> None:
                        try:
                            task.result()
                        except BaseException:
                            pass

                    def _cancel_xai_login_dialog(dialog) -> None:
                        cancelled["value"] = True
                        listener_cancel.set()
                        if wait_task is not None and not wait_task.done():
                            wait_task.add_done_callback(_consume_wait_task)
                        dialog.close()

                    async def _complete_xai_authorization(authorization) -> None:
                        token_set = await run.io_bound(exchange_xai_oauth_authorization, authorization)
                        await run.io_bound(save_xai_oauth_tokens, token_set)
                        await run.io_bound(seed_recommended_xai_oauth_quick_choices)

                    try:
                        if xai_oauth_client_id:
                            await run.io_bound(save_xai_oauth_client_id, xai_oauth_client_id)
                        flow = await run.io_bound(
                            lambda: start_xai_oauth_flow(client_id=xai_oauth_client_id or None)
                        )
                        cloud_status.text = "Complete xAI Grok sign-in in the browser..."
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
                        with ui.dialog() as xai_login_dialog:
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
                                    finish_note = ui.notification(
                                        "Completing xAI Grok sign-in...",
                                        type="ongoing",
                                        spinner=True,
                                        timeout=None,
                                    )
                                    try:
                                        authorization = await run.io_bound(
                                            authorization_from_xai_oauth_callback,
                                            flow,
                                            callback_value,
                                        )
                                        await _complete_xai_authorization(authorization)
                                    except Exception as exc:
                                        finish_note.dismiss()
                                        logger.warning("xAI Grok setup pasted callback failed", exc_info=True)
                                        status_label.text = f"Sign-in failed: {exc}"
                                        status_label.update()
                                        ui.notify(f"xAI Grok sign-in failed: {exc}", type="negative")
                                        return
                                    finish_note.dismiss()
                                    completed["value"] = True
                                    listener_cancel.set()
                                    if wait_task is not None and not wait_task.done():
                                        wait_task.add_done_callback(_consume_wait_task)
                                    xai_login_dialog.close()
                                    ui.notify("xAI Grok connected", type="positive")
                                    cloud_status.text = "xAI Grok connected. Fetching models..."
                                    await _load_xai_oauth_models()

                                with ui.row().classes("w-full items-center justify-end gap-2"):
                                    ui.button("Cancel", icon="close", on_click=lambda: _cancel_xai_login_dialog(xai_login_dialog)).props("flat dense")
                                    ui.button("Connect with pasted code", icon="check", on_click=_finish_with_pasted_callback).props("flat dense color=primary")
                        xai_login_dialog.open()
                        notification.dismiss()
                        ui.run_javascript(f"window.open({json.dumps(flow.authorization_url)}, '_blank', 'noopener,noreferrer')")
                        try:
                            authorization = await wait_task
                        except XAIOAuthError as exc:
                            if (completed["value"] or cancelled["value"]) and exc.kind == "loopback_cancelled":
                                return
                            logger.warning("xAI Grok setup automatic callback failed", exc_info=True)
                            cloud_status.text = "Paste the xAI callback URL or authorization code to finish sign-in."
                            status_label.text = f"Automatic browser callback failed: {exc}"
                            status_label.update()
                            ui.notify("Paste the xAI callback URL or authorization code to finish sign-in.", type="warning")
                            cloud_done["value"] = False
                            _update_finish()
                            return
                        if completed["value"]:
                            return
                        await _complete_xai_authorization(authorization)
                        completed["value"] = True
                        listener_cancel.set()
                        xai_login_dialog.close()
                    except XAIOAuthError as exc:
                        notification.dismiss()
                        listener_cancel.set()
                        if wait_task is not None and not wait_task.done():
                            wait_task.add_done_callback(_consume_wait_task)
                        logger.warning("xAI Grok setup sign-in start failed", exc_info=True)
                        if exc.kind == "missing_client_id":
                            cloud_status.text = (
                                "Enter an xAI OAuth Client ID override here, "
                                "then start xAI Grok sign-in again."
                            )
                        else:
                            cloud_status.text = f"Could not start xAI Grok sign-in: {exc}"
                        cloud_done["value"] = False
                        _update_finish()
                        return
                    except Exception as exc:
                        notification.dismiss()
                        listener_cancel.set()
                        if wait_task is not None and not wait_task.done():
                            wait_task.add_done_callback(_consume_wait_task)
                        logger.warning("xAI Grok setup sign-in start failed", exc_info=True)
                        cloud_status.text = f"Could not start xAI Grok sign-in: {exc}"
                        cloud_done["value"] = False
                        _update_finish()
                        return
                    ui.notify("xAI Grok connected", type="positive")
                    cloud_status.text = "xAI Grok connected. Fetching models..."
                    await _load_xai_oauth_models()

                ui.button(
                    "Use xAI Grok",
                    icon="login",
                    on_click=_use_xai_oauth_provider,
                ).props("outline color=blue-grey").classes("q-mb-sm")

                async def _validate_cloud_keys():
                    oai_val = setup_openai_key.value.strip()
                    from row_bot.providers.transports.ollama_cloud import normalize_ollama_cloud_api_key
                    ollama_cloud_val = normalize_ollama_cloud_api_key(setup_ollama_cloud_key.value)
                    anth_val = setup_anth_key.value.strip()
                    goog_val = setup_goog_key.value.strip()
                    xai_val = setup_xai_key.value.strip()
                    minimax_val = setup_minimax_key.value.strip()
                    atlascloud_val = setup_atlascloud_key.value.strip()
                    or_val = setup_or_key.value.strip()
                    requesty_val = setup_requesty_key.value.strip()
                    opencode_zen_val = setup_opencode_zen_key.value.strip()
                    opencode_go_val = setup_opencode_go_key.value.strip()
                    if not oai_val and not ollama_cloud_val and not anth_val and not goog_val and not xai_val and not minimax_val and not atlascloud_val and not or_val and not requesty_val and not opencode_zen_val and not opencode_go_val:
                        ui.notify("Enter at least one API key", type="warning")
                        return
                    entered_providers = [
                        provider_id
                        for provider_id, value in (
                            ("openai", oai_val),
                            ("ollama_cloud", ollama_cloud_val),
                            ("anthropic", anth_val),
                            ("google", goog_val),
                            ("xai", xai_val),
                            ("minimax", minimax_val),
                            ("atlascloud", atlascloud_val),
                            ("openrouter", or_val),
                            ("requesty", requesty_val),
                            ("opencode_zen", opencode_zen_val),
                            ("opencode_go", opencode_go_val),
                        )
                        if value
                    ]
                    cloud_status.text = "⏳ Validating key(s)…"
                    cloud_status.visible = True
                    if ollama_cloud_val:
                        ollama_cloud_valid = await run.io_bound(validate_ollama_cloud_key, ollama_cloud_val)
                        if not ollama_cloud_valid:
                            cloud_status.text = "Invalid Ollama Cloud API key."
                            cloud_done["value"] = False
                            _update_finish()
                            return
                        set_key("OLLAMA_API_KEY", ollama_cloud_val)
                    if or_val:
                        or_valid = await run.io_bound(validate_openrouter_key, or_val)
                        if not or_valid:
                            cloud_status.text = "❌ Invalid OpenRouter API key."
                            cloud_done["value"] = False
                            _update_finish()
                            return
                        set_key("OPENROUTER_API_KEY", or_val)
                    if requesty_val:
                        requesty_valid = await run.io_bound(validate_requesty_key, requesty_val)
                        if not requesty_valid:
                            cloud_status.text = "Invalid Requesty API key."
                            cloud_done["value"] = False
                            _update_finish()
                            return
                        set_key("REQUESTY_API_KEY", requesty_val)
                    if anth_val:
                        anth_valid = await run.io_bound(validate_anthropic_key, anth_val)
                        if not anth_valid:
                            cloud_status.text = "❌ Invalid Anthropic API key."
                            cloud_done["value"] = False
                            _update_finish()
                            return
                        set_key("ANTHROPIC_API_KEY", anth_val)
                    if goog_val:
                        goog_valid = await run.io_bound(validate_google_key, goog_val)
                        if not goog_valid:
                            cloud_status.text = "❌ Invalid Google AI API key."
                            cloud_done["value"] = False
                            _update_finish()
                            return
                        set_key("GOOGLE_API_KEY", goog_val)
                    if xai_val:
                        xai_valid = await run.io_bound(validate_xai_key, xai_val)
                        if not xai_valid:
                            cloud_status.text = "❌ Invalid xAI API key."
                            cloud_done["value"] = False
                            _update_finish()
                            return
                        set_key("XAI_API_KEY", xai_val)
                    if minimax_val:
                        minimax_valid = await run.io_bound(validate_minimax_key, minimax_val)
                        if not minimax_valid:
                            ui.notify(
                                "MiniMax key validation failed — saving anyway so the catalog can be prepared.",
                                type="warning",
                                timeout=5000,
                            )
                        set_key("MINIMAX_API_KEY", minimax_val)
                    if atlascloud_val:
                        atlascloud_valid = await run.io_bound(validate_atlascloud_key, atlascloud_val)
                        if not atlascloud_valid:
                            cloud_status.text = "Invalid Atlas Cloud API key."
                            cloud_done["value"] = False
                            _update_finish()
                            return
                        set_key("ATLASCLOUD_API_KEY", atlascloud_val)
                    if oai_val:
                        set_key("OPENAI_API_KEY", oai_val)
                    if opencode_zen_val:
                        set_key("OPENCODE_ZEN_API_KEY", opencode_zen_val)
                    if opencode_go_val:
                        set_key("OPENCODE_GO_API_KEY", opencode_go_val)
                    cloud_status.text = "⏳ Fetching available models…"
                    count = await run.io_bound(refresh_cloud_models)
                    if count == 0:
                        cloud_status.text = "❌ No models found. Check your API key(s)."
                        cloud_done["value"] = False
                        _update_finish()
                        return
                    codex_models_by_ref.clear()
                    xai_oauth_models_by_ref.clear()
                    models = list_cloud_models()
                    model_options_by_key = {
                        m: cloud_model_setup_option(
                            m,
                            _cloud_model_cache.get(m),
                            emoji_lookup=get_provider_emoji,
                        )
                        for m in models
                    }
                    opts = {option["value"]: option["label"] for option in model_options_by_key.values()}
                    cloud_model_select.options = opts
                    cloud_model_select.visible = True
                    preferred_models = [
                        m for m in models
                        if model_options_by_key[m]["provider_id"] in entered_providers
                    ]
                    first_model = preferred_models[0] if preferred_models else ("gpt-5" if "gpt-5" in models else models[0])
                    first = model_options_by_key[first_model]["value"]
                    cloud_model_select.set_value(first)
                    vision_models = list_cloud_vision_models()
                    if vision_models:
                        vision_options_by_key = {
                            m: cloud_model_setup_option(
                                m,
                                _cloud_model_cache.get(m),
                                emoji_lookup=get_provider_emoji,
                            )
                            for m in vision_models
                        }
                        v_opts = {option["value"]: option["label"] for option in vision_options_by_key.values()}
                        cloud_vision_select.options = v_opts
                        preferred_vision = [
                            m for m in vision_models
                            if vision_options_by_key[m]["provider_id"] in entered_providers
                        ]
                        if preferred_vision:
                            v_first_model = preferred_vision[0]
                            v_first = vision_options_by_key[v_first_model]["value"]
                            cloud_vision_select.set_value(v_first)
                            cloud_vision_select.visible = True
                        else:
                            cloud_vision_select.set_value(None)
                            cloud_vision_select.visible = False
                    cloud_status.text = f"✅ Found {count} models"
                    first_parsed = parse_model_ref(first)
                    if first_parsed:
                        add_quick_choice_for_model(first_parsed[1], provider_id=first_parsed[0], source="setup_default")
                    else:
                        add_quick_choice_for_model(first, source="setup_default")
                    cloud_done["value"] = True
                    _update_finish()

                ui.button("Validate & Fetch Models", icon="key", on_click=_validate_cloud_keys).props(
                    "color=cyan"
                )

                def _on_cloud_model_change(e):
                    if e.value:
                        cloud_done["value"] = True
                        _update_finish()

                cloud_model_select.on_value_change(_on_cloud_model_change)

            # ── Custom/Self-hosted Provider Setup Path ───────────────
            custom_done: dict[str, bool] = {"value": False}
            custom_models_by_ref: dict[str, object] = {}
            with _custom_section:
                ui.label(
                    "Connect an OpenAI-compatible endpoint such as vLLM, LocalAI, LM Studio, "
                    "or a private gateway. Leave the API key empty for no-auth endpoints."
                ).classes("text-grey-6 text-sm")

                custom_name_input = ui.input(
                    "Name",
                    placeholder="oMLX Mac Test",
                ).classes("w-full")
                custom_url_input = ui.input(
                    "Base URL",
                    placeholder="http://127.0.0.1:8000/v1",
                ).classes("w-full")
                with ui.row().classes("items-center gap-3 w-full"):
                    custom_profile_select = ui.select(
                        {key: str(value.get("display_name") or key) for key, value in CUSTOM_ENDPOINT_PROFILES.items()},
                        value="generic_openai",
                        label="Endpoint profile",
                    ).classes("min-w-[200px]").props("dense outlined")
                    custom_no_auth = ui.checkbox("No API key required", value=True)
                    custom_location_select = ui.select(
                        {"local": "Local/private", "remote": "Remote/proxy"},
                        value="local",
                        label="Execution location",
                    ).classes("min-w-[180px]").props("dense outlined")
                custom_api_key_input = ui.input(
                    "API key or token",
                    value="",
                    placeholder="Optional for local servers",
                    password=True,
                    password_toggle_button=True,
                ).classes("w-full")

                custom_status = ui.label("").classes("text-sm")
                custom_status.visible = False
                custom_model_select = ui.select(
                    label="Default self-hosted model",
                    options=[],
                ).classes("w-full").props("use-input input-debounce=300")
                custom_model_select.visible = False

                async def _connect_custom_endpoint():
                    base_url = str(custom_url_input.value or "").strip()
                    api_key = str(custom_api_key_input.value or "").strip()
                    if not base_url:
                        ui.notify("Enter a custom endpoint base URL", type="warning")
                        return
                    auth_required = not bool(custom_no_auth.value)
                    if auth_required and not api_key:
                        ui.notify("Enter an API key or enable no-auth for this endpoint", type="warning")
                        return
                    payload = build_custom_endpoint_setup_payload(
                        base_url,
                        api_key,
                        str(custom_profile_select.value or "generic_openai"),
                        name=str(custom_name_input.value or ""),
                        execution_location=str(custom_location_select.value or ""),
                        auth_required=auth_required,
                    )
                    custom_status.text = "⏳ Connecting to custom endpoint…"
                    custom_status.visible = True
                    custom_model_select.visible = False
                    custom_done["value"] = False
                    _update_finish()
                    try:
                        await run.io_bound(save_custom_endpoint, payload)
                        infos = await run.io_bound(
                            refresh_custom_endpoint_models,
                            endpoint_id_from_provider_id(str(payload["id"])),
                        )
                    except Exception as exc:
                        logger.warning("Custom endpoint setup failed", exc_info=True)
                        custom_status.text = f"❌ Could not fetch models: {exc}"
                        _update_finish()
                        return
                    opts = custom_endpoint_model_options(infos)
                    if not opts:
                        custom_status.text = "❌ No models found at this endpoint."
                        _update_finish()
                        return
                    custom_models_by_ref.clear()
                    custom_models_by_ref.update({info.selection_ref: info for info in infos})
                    custom_model_select.options = opts
                    first = next(iter(opts))
                    custom_model_select.set_value(first)
                    custom_model_select.visible = True
                    storage_warning = get_provider_storage_warning() if api_key else ""
                    if storage_warning:
                        custom_status.text = f"✅ Found {len(opts)} models. API key is saved for this session only."
                    else:
                        custom_status.text = f"✅ Found {len(opts)} models"
                    custom_done["value"] = True
                    _update_finish()

                ui.button(
                    "Connect & Fetch Models",
                    icon="hub",
                    on_click=_connect_custom_endpoint,
                ).props("color=teal")

                def _on_custom_model_change(e):
                    custom_done["value"] = bool(e.value)
                    _update_finish()

                custom_model_select.on_value_change(_on_custom_model_change)

            # ── Brain Model (Local path) ─────────────────────────────
            with _local_section:
                def _wiz_probe():
                    up = _ollama_reachable()
                    local = list_local_models() if up else []
                    return up, local

                if docs_capture_fake_provider_status():
                    _wiz_ollama_up, local_now = True, ["llama3.1:8b", "nomic-embed-text"]
                else:
                    _wiz_ollama_up, local_now = await run.io_bound(_wiz_probe)

                if sys.platform == "win32":
                    _wiz_install = (
                        "1. Download Ollama from ollama.com/download\n"
                        "2. Run the installer — Ollama starts automatically\n"
                        "3. Come back here and click 🖥️ Local again"
                    )
                elif sys.platform == "darwin":
                    _wiz_install = (
                        "1. Download from ollama.com/download (or: brew install ollama)\n"
                        "2. Run: ollama serve\n"
                        "3. Come back here and click 🖥️ Local again"
                    )
                else:
                    _wiz_install = (
                        "1. Install: curl -fsSL https://ollama.com/install.sh | sh\n"
                        "2. Run: ollama serve\n"
                        "3. Come back here and click 🖥️ Local again"
                    )

                with ui.card().classes("w-full q-pa-md bg-amber-1") as wiz_ollama_notice:
                    ui.label("⚠️ Ollama is not running").classes(
                        "text-weight-bold text-body1 text-brown-9"
                    )
                    ui.label(
                        "Local models need Ollama installed and running on your machine. "
                        "Install it, then come back to this wizard."
                    ).classes("text-grey-8 text-sm q-mb-xs")
                    ui.label(_wiz_install).classes("text-grey-8 text-xs").style(
                        "white-space: pre-line"
                    )
                    if not is_docs_capture():
                        ui.link(
                            "Download Ollama →",
                            "https://ollama.com/download",
                            new_tab=True,
                        ).classes("text-sm text-weight-bold")
                wiz_ollama_notice.visible = not _wiz_ollama_up

                ui.label("🧠 Brain Model").classes("text-h6")
                ui.label(
                    "The main reasoning model that powers conversations and tool use. "
                    "14B+ recommended for best accuracy."
                ).classes("text-grey-6 text-sm")

                local_now = sorted(set(local_now or []))
                brain_default = model_id_from_choice_value(state.current_model)
                if brain_default not in local_now:
                    brain_default = local_now[0] if local_now else None

                setup_brain_opts = {m: f"✅  {m}" for m in local_now}
                setup_brain_select = ui.select(
                    label="Brain model",
                    options=setup_brain_opts,
                    value=brain_default,
                ).classes("w-full").props("use-input input-debounce=300")
                if not setup_brain_opts:
                    setup_brain_select.disable()

                brain_status = ui.label("").classes("text-sm")
                brain_status.visible = True
                brain_done: dict[str, bool] = {"value": bool(brain_default)}
                if brain_default:
                    brain_status.text = f"✅ {brain_default} is ready"
                    selection = model_choice_value(brain_default, provider_id="ollama")
                    set_model(selection)
                    state.current_model = selection
                    clear_agent_cache()
                else:
                    brain_status.text = "No local models are exposed by Ollama. Manage models in Ollama, then come back here."

                def _on_setup_brain_change(e):
                    sel = e.value
                    brain_done["value"] = bool(sel)
                    if sel:
                        brain_status.text = f"✅ {sel} is ready"
                        brain_status.visible = True
                        selection = model_choice_value(sel, provider_id="ollama")
                        set_model(selection)
                        state.current_model = selection
                        clear_agent_cache()
                    else:
                        brain_status.text = "No local models are exposed by Ollama."
                        brain_status.visible = True
                    _update_finish()

                setup_brain_select.on_value_change(_on_setup_brain_change)

                ui.separator()

                # ── Vision Model ─────────────────────────────────────
                ui.label("👁️ Vision Model").classes("text-h6")
                ui.label(
                    "Used for camera and screen capture analysis. "
                    "Optional — you can skip this and manage local models in Ollama later."
                ).classes("text-grey-6 text-sm")

                vsvc = state.vision_service
                vision_default = model_id_from_choice_value(vsvc.model)
                if vision_default not in local_now:
                    vision_default = local_now[0] if local_now else None
                setup_vision_opts = {m: f"✅  {m}" for m in local_now}
                setup_vision_select = ui.select(
                    label="Vision model",
                    options=setup_vision_opts,
                    value=vision_default,
                ).classes("w-full").props("use-input input-debounce=300")
                if not setup_vision_opts:
                    setup_vision_select.disable()

                vision_status = ui.label("").classes("text-sm")
                vision_status.visible = True
                if vision_default:
                    vision_status.text = f"✅ {vision_default} is ready"
                    vsvc.model = model_choice_value(vision_default, provider_id="ollama")
                else:
                    vision_status.text = "No local vision model is exposed by Ollama."

                def _on_setup_vision_change(e):
                    sel = e.value
                    if sel:
                        vision_status.text = f"✅ {sel} is ready"
                        vision_status.visible = True
                        vsvc.model = model_choice_value(sel, provider_id="ollama")
                    else:
                        vision_status.text = "No local vision model is exposed by Ollama."
                        vision_status.visible = True

                setup_vision_select.on_value_change(_on_setup_vision_change)

            ui.separator()

            # ── Private knowledge model ──────────────────────────────
            from row_bot.embedding_config import DEFAULT_CONFIG, LOCAL_MODELS
            from row_bot.embedding_providers import get_local_embedding_status

            default_embedding_key = str(DEFAULT_CONFIG["local_model"])
            default_embedding = LOCAL_MODELS[default_embedding_key]
            default_embedding_status = get_local_embedding_status()
            default_embedding_cached = default_embedding_status.get("state") in {
                "cached",
                "ready",
            }

            ui.label("Private knowledge model").classes("text-h6")
            ui.label(
                "Row-Bot uses a separate local model for semantic memory and document search. "
                "Normal recall remains offline after setup."
            ).classes("text-grey-6 text-sm")
            install_default_embedding = ui.checkbox(
                f"Install {default_embedding['label']} now (recommended, about "
                f"{int(default_embedding.get('download_size_mb') or 700)} MB)",
                value=True,
            ).classes("text-sm")
            ui.label(
                "Downloaded from Hugging Face into Row-Bot's private local cache when you finish setup. "
                "Documents and memories are not sent anywhere. Uncheck to skip and use lexical/graph "
                "fallback until you install it from Settings → Documents."
            ).classes("text-grey-6 text-xs")
            embedding_setup_status = ui.label(
                "Already installed in the local cache."
                if default_embedding_cached
                else "Ready to download when setup finishes."
            ).classes("text-sm text-positive" if default_embedding_cached else "text-sm text-blue-3")
            if default_embedding_cached:
                install_default_embedding.disable()

            ui.separator()

            # ── Optional import ──────────────────────────────────────
            ui.label("Migrate from OpenClaw or Hermes Agent?").classes("text-h6")
            ui.label(
                "Bring over an OpenClaw or Hermes Agent setup now, or skip and import later from Settings."
            ).classes("text-grey-6 text-sm")
            with ui.row().classes("w-full gap-2 q-my-sm"):
                ui.button(
                    "Open migration",
                    icon="move_up",
                    on_click=_open_first_run_migration_wizard,
                ).props("outline color=primary")
                ui.button(
                    "Skip for now",
                    icon="skip_next",
                    on_click=lambda: ui.notify("Import skipped. You can run it later from Settings.", type="info"),
                ).props("flat color=grey")

            ui.separator()

            # ── Ready / setup checklist ──────────────────────────────
            ui.label("You're ready").classes("text-h6")
            ui.label(
                f"Open {APP_DISPLAY_NAME} now, or continue into the Setup Center for documents, workflows, Designer Studio, Developer Studio, channels, accounts, tools, plugins, and voice."
            ).classes("text-grey-6 text-sm")
            ui.label("What should Setup Center prioritize?").classes("text-subtitle2 q-mt-sm")

            def _toggle_intent(e, key: str) -> None:
                if e.value:
                    selected_intents.add(key)
                else:
                    selected_intents.discard(key)
                save_onboarding_profile(list(selected_intents))

            with ui.row().classes("w-full gap-2 q-my-sm flex-wrap"):
                for intent_key, intent_label in INTENT_OPTIONS.items():
                    ui.checkbox(
                        intent_label,
                        on_change=lambda e, key=intent_key: _toggle_intent(e, key),
                    ).classes("text-sm")

            with ui.row().classes("w-full gap-2 q-mt-sm"):
                for icon, title, desc in (
                    ("description", "Knowledge", "Upload docs and choose embeddings."),
                    ("bolt", "Workflows", "Starter workflows are added disabled."),
                    ("design_services", "Designer Studio", "Create decks, pages, and mockups."),
                    ("code", "Developer Studio", "Connect repos and create Custom Tools."),
                    ("forum", "Channels", "Connect messaging channels when ready."),
                ):
                    with ui.card().classes("q-pa-sm").style(
                        "flex: 1 1 180px; min-width: 170px; border-radius: 8px; background: rgba(255,255,255,0.035);"
                    ):
                        ui.icon(icon).classes("text-blue-3")
                        ui.label(title).classes("text-subtitle2")
                        ui.label(desc).classes("text-grey-6 text-xs")

            ui.separator()

            # ── Finish ───────────────────────────────────────────────
            with ui.row().classes("w-full gap-2"):
                open_btn = ui.button(f"Open {APP_DISPLAY_NAME}", icon="home").props(
                    "color=primary size=lg"
                ).classes("col")
                continue_btn = ui.button("Continue setup", icon="checklist").props(
                    "outline color=primary size=lg"
                ).classes("col")

            def _update_finish():
                if setup_path["mode"] is None:
                    open_btn.set_enabled(False)
                    continue_btn.set_enabled(False)
                elif setup_path["mode"] == "cloud":
                    open_btn.set_enabled(cloud_done["value"])
                    continue_btn.set_enabled(cloud_done["value"])
                elif setup_path["mode"] == "custom":
                    open_btn.set_enabled(custom_done["value"])
                    continue_btn.set_enabled(custom_done["value"])
                else:
                    open_btn.set_enabled(brain_done["value"])
                    continue_btn.set_enabled(brain_done["value"])

            _update_finish()

            async def _install_default_embedding_for_setup() -> bool:
                if not bool(install_default_embedding.value):
                    return True
                if default_embedding_cached:
                    return True

                from row_bot.embedding_providers import download_local_embedding_model

                notice = ui.notification(
                    f"Downloading {default_embedding['label']} for private knowledge search...",
                    type="ongoing",
                    spinner=True,
                    timeout=None,
                )
                embedding_setup_status.text = "Downloading the private knowledge model..."
                try:
                    await run.io_bound(
                        download_local_embedding_model,
                        default_embedding_key,
                        repair=False,
                    )
                except Exception as exc:
                    notice.dismiss()
                    logger.error("Default embedding model setup download failed", exc_info=True)
                    embedding_setup_status.text = "Download failed. Check the connection and try again."
                    ui.notify(
                        f"Private knowledge model download failed: {exc}",
                        type="negative",
                        close_button=True,
                    )
                    return False
                notice.dismiss()
                embedding_setup_status.text = "Private knowledge model installed."
                ui.notify("Private knowledge model installed", type="positive")
                return True

            setup_finish_busy = {"value": False}

            async def _finish_setup(*, continue_setup: bool = False):
                if setup_finish_busy["value"]:
                    return
                setup_finish_busy["value"] = True
                open_btn.disable()
                continue_btn.disable()
                if setup_path["mode"] == "cloud":
                    sel = cloud_model_select.value
                    if sel:
                        set_model(sel)
                        state.current_model = sel
                        info = codex_models_by_ref.get(sel) or xai_oauth_models_by_ref.get(sel)
                        if info:
                            add_quick_choice_for_model(
                                info.model_id,
                                provider_id=info.provider_id,
                                display_name=info.display_name,
                                source="setup_default",
                                capabilities_snapshot=info.capability_snapshot(),
                            )
                        else:
                            parsed = parse_model_ref(sel)
                            if parsed:
                                cached = _cloud_model_cache.get(sel) or _cloud_model_cache.get(parsed[1]) or {}
                                add_quick_choice_for_model(
                                    parsed[1],
                                    provider_id=parsed[0],
                                    display_name=str(cached.get("label") or parsed[1]),
                                    source="setup_default",
                                    capabilities_snapshot=cached.get("capabilities_snapshot") if isinstance(cached.get("capabilities_snapshot"), dict) else None,
                                )
                            else:
                                add_quick_choice_for_model(sel, source="setup_default")
                        clear_agent_cache()
                    vsel = cloud_vision_select.value
                    if vsel:
                        state.vision_service.model = vsel
                elif setup_path["mode"] == "custom":
                    sel = custom_model_select.value
                    info = custom_models_by_ref.get(sel)
                    if sel and info:
                        set_model(sel)
                        state.current_model = sel
                        add_quick_choice_for_model(
                            info.model_id,
                            provider_id=info.provider_id,
                            display_name=info.display_name,
                            source="setup_default",
                            capabilities_snapshot=info.capability_snapshot(),
                        )
                        clear_agent_cache()
                if not await _install_default_embedding_for_setup():
                    setup_finish_busy["value"] = False
                    _update_finish()
                    return
                mark_onboarding_step("models")
                if continue_setup:
                    setattr(state, "open_setup_center_on_next_load", True)
                    request_setup_center_on_next_load()
                mark_setup_complete()
                setup_dlg.close()
                await on_finish()

            async def _open_row_bot():
                await _finish_setup(continue_setup=False)

            async def _continue_setup():
                await _finish_setup(continue_setup=True)

            open_btn.on_click(_open_row_bot)
            continue_btn.on_click(_continue_setup)

    setup_dlg.open()
