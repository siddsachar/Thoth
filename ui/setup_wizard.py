"""Thoth UI — first-launch setup wizard.

Builds the full-screen ``ui.dialog`` that walks the user through
model selection and API key configuration on first launch.

The wizard is self-contained except for two callbacks:

* **on_finish** — called (no args) when the user clicks *Get Started*.
  The caller should use this to e.g. call ``_rebuild_main()`` to
  transition to the main UI.
"""

from __future__ import annotations

import json
import ipaddress
import logging
import sys
from typing import Callable, TYPE_CHECKING
from urllib.parse import urlparse

from nicegui import run, ui

if TYPE_CHECKING:
    from ui.state import AppState

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


def build_custom_endpoint_setup_payload(base_url: str, api_key: str = "") -> dict[str, str | bool]:
    clean_url = str(base_url or "").strip().rstrip("/")
    clean_key = str(api_key or "").strip()
    host_label = _custom_endpoint_host_label(clean_url)
    name = f"Self-hosted ({host_label})"
    return {
        "id": host_label,
        "name": name,
        "base_url": clean_url,
        "api_key": clean_key,
        "auth_required": bool(clean_key),
        "execution_location": _custom_endpoint_execution_location(clean_url),
        "transport": "openai_chat",
    }


def custom_endpoint_model_options(model_infos: list) -> dict[str, str]:
    return {
        info.selection_ref: f"↔ {info.display_name or info.model_id}"
        for info in model_infos
        if getattr(info, "selection_ref", "") and getattr(info, "model_id", "")
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
    from models import (
        POPULAR_MODELS,
        DEFAULT_MODEL,
        validate_openrouter_key,
        validate_anthropic_key,
        validate_google_key,
        validate_xai_key,
        validate_minimax_key,
        refresh_cloud_models,
        list_cloud_models,
        get_provider_emoji,
        list_cloud_vision_models,
        list_all_models,
        is_model_local,
        pull_model,
        set_model,
        list_local_models,
        _ollama_reachable,
    )
    from vision import DEFAULT_VISION_MODEL, POPULAR_VISION_MODELS
    from api_keys import set_key
    from agent import clear_agent_cache
    from ui.helpers import mark_setup_complete
    from ui.onboarding_state import (
        INTENT_OPTIONS,
        mark_onboarding_step,
        request_setup_center_on_next_load,
        save_onboarding_profile,
    )
    from providers.selection import add_quick_choice_for_model
    from providers.custom import (
        endpoint_id_from_provider_id,
        refresh_custom_endpoint_models,
        save_custom_endpoint,
    )
    from providers.codex import (
        codex_runtime_available,
        exchange_codex_device_authorization,
        list_codex_model_infos,
        poll_codex_device_authorization,
        save_codex_oauth_tokens,
        start_codex_device_flow,
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
                            "ui.migration_wizard",
                            fromlist=["build_migration_wizard_tab"],
                        ).build_migration_wizard_tab()
        migration_dlg.open()

    setup_dlg = ui.dialog().props(
        "persistent maximized transition-show=fade transition-hide=fade"
    )

    with setup_dlg:
        with ui.card().classes("w-full max-w-4xl mx-auto q-pa-lg").style(
            "border-radius: 10px;"
        ):
            selected_intents: set[str] = set()

            # ── Header ───────────────────────────────────────────────
            with ui.row().classes("w-full items-center justify-between gap-3"):
                with ui.column().classes("gap-0"):
                    ui.label("Welcome to Thoth").classes("text-h4 text-weight-medium")
                    ui.label(
                        "Connect one working model first. Everything else can wait."
                    ).classes("text-grey-6 text-sm")
                ui.badge("1 Model  ·  2 Import  ·  3 Ready", color="blue-grey").props("outline")

            ui.separator()

            # ── Setup Path Toggle ────────────────────────────────────
            ui.label("Connect your first model").classes("text-h6")
            ui.label(
                "Pick the path you want to use first. Thoth only needs one working model to get started."
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
            with _cloud_section:
                ui.label(
                    "Use ChatGPT / Codex, or save one API key and fetch available models."
                ).classes("text-grey-6 text-sm")

                setup_openai_key = ui.input(
                    "OpenAI API Key (optional)",
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
                setup_minimax_key = ui.input(
                    "MiniMax API Key (optional)",
                    password=True, password_toggle_button=True,
                ).classes("w-full")
                setup_or_key = ui.input(
                    "OpenRouter API Key (optional)",
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

                async def _validate_cloud_keys():
                    oai_val = setup_openai_key.value.strip()
                    anth_val = setup_anth_key.value.strip()
                    goog_val = setup_goog_key.value.strip()
                    xai_val = setup_xai_key.value.strip()
                    minimax_val = setup_minimax_key.value.strip()
                    or_val = setup_or_key.value.strip()
                    if not oai_val and not anth_val and not goog_val and not xai_val and not minimax_val and not or_val:
                        ui.notify("Enter at least one API key", type="warning")
                        return
                    cloud_status.text = "⏳ Validating key(s)…"
                    cloud_status.visible = True
                    if or_val:
                        or_valid = await run.io_bound(validate_openrouter_key, or_val)
                        if not or_valid:
                            cloud_status.text = "❌ Invalid OpenRouter API key."
                            cloud_done["value"] = False
                            _update_finish()
                            return
                        set_key("OPENROUTER_API_KEY", or_val)
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
                    if oai_val:
                        set_key("OPENAI_API_KEY", oai_val)
                    cloud_status.text = "⏳ Fetching available models…"
                    count = await run.io_bound(refresh_cloud_models)
                    if count == 0:
                        cloud_status.text = "❌ No models found. Check your API key(s)."
                        cloud_done["value"] = False
                        _update_finish()
                        return
                    codex_models_by_ref.clear()
                    models = list_cloud_models()
                    opts = {m: f"{get_provider_emoji(m)} {m}" for m in models}
                    cloud_model_select.options = opts
                    cloud_model_select.visible = True
                    first = "gpt-5" if "gpt-5" in models else models[0]
                    cloud_model_select.set_value(first)
                    vision_models = list_cloud_vision_models()
                    if vision_models:
                        v_opts = {m: f"{get_provider_emoji(m)} {m}" for m in vision_models}
                        cloud_vision_select.options = v_opts
                        v_first = "gpt-5" if "gpt-5" in vision_models else vision_models[0]
                        cloud_vision_select.set_value(v_first)
                        cloud_vision_select.visible = True
                    cloud_status.text = f"✅ Found {count} models"
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

                custom_url_input = ui.input(
                    "Base URL",
                    placeholder="http://127.0.0.1:8000/v1",
                ).classes("w-full")
                custom_api_key_input = ui.input(
                    "API Key (optional)",
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
                    payload = build_custom_endpoint_setup_payload(base_url, api_key)
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

                local_now = local_now or []
                setup_all_models = list_all_models()
                brain_default = state.current_model
                if brain_default not in setup_all_models:
                    brain_default = DEFAULT_MODEL

                setup_brain_opts = {
                    m: f"{'✅' if m in local_now else '⬇️'}  {m}"
                    for m in setup_all_models
                }
                setup_brain_select = ui.select(
                    label="Brain model",
                    options=setup_brain_opts,
                    value=brain_default,
                ).classes("w-full").props("use-input input-debounce=300")

                brain_status = ui.label("").classes("text-sm")
                brain_status.visible = False
                brain_done: dict[str, bool] = {"value": brain_default in local_now}

                setup_brain_dl = ui.button(f"⬇️ Download {brain_default}").props(
                    "color=primary"
                )
                setup_brain_dl.visible = brain_default not in local_now
                if brain_default in local_now:
                    brain_status.text = f"✅ {brain_default} is ready"
                    brain_status.visible = True

                async def _setup_dl_brain():
                    sel = setup_brain_select.value
                    if is_model_local(sel):
                        brain_status.text = f"✅ {sel} is already downloaded"
                        brain_status.visible = True
                        brain_done["value"] = True
                        setup_brain_dl.visible = False
                        _update_finish()
                        return
                    if not _ollama_reachable():
                        brain_status.text = (
                            "❌ Ollama is not running. Install and start Ollama first."
                        )
                        brain_status.visible = True
                        return
                    setup_brain_dl.disable()
                    brain_status.text = f"⏳ Downloading {sel}… this may take a few minutes"
                    brain_status.visible = True
                    n = ui.notification(
                        f"Downloading {sel}…",
                        type="ongoing",
                        spinner=True,
                        timeout=None,
                    )
                    await run.io_bound(lambda: list(pull_model(sel)))
                    n.dismiss()
                    brain_status.text = f"✅ {sel} downloaded successfully!"
                    setup_brain_dl.visible = False
                    setup_brain_dl.enable()
                    brain_done["value"] = True
                    set_model(sel)
                    state.current_model = sel
                    clear_agent_cache()
                    _update_finish()

                setup_brain_dl.on_click(_setup_dl_brain)

                def _on_setup_brain_change(e):
                    sel = e.value
                    setup_brain_dl.text = f"⬇️ Download {sel}"
                    already = is_model_local(sel)
                    setup_brain_dl.visible = not already
                    brain_done["value"] = already
                    if already:
                        brain_status.text = f"✅ {sel} is ready"
                        brain_status.visible = True
                        set_model(sel)
                        state.current_model = sel
                        clear_agent_cache()
                    else:
                        brain_status.visible = False
                    _update_finish()

                setup_brain_select.on_value_change(_on_setup_brain_change)

                ui.separator()

                # ── Vision Model ─────────────────────────────────────
                ui.label("👁️ Vision Model").classes("text-h6")
                ui.label(
                    "Used for camera and screen capture analysis. "
                    "Optional — you can skip this and download it later."
                ).classes("text-grey-6 text-sm")

                vsvc = state.vision_service
                setup_vision_opts = {
                    m: f"{'✅' if m in local_now else '⬇️'}  {m}"
                    for m in sorted(
                        set(
                            POPULAR_VISION_MODELS
                            + (
                                [vsvc.model]
                                if vsvc.model not in POPULAR_VISION_MODELS
                                else []
                            )
                        )
                    )
                }
                setup_vision_select = ui.select(
                    label="Vision model",
                    options=setup_vision_opts,
                    value=vsvc.model,
                ).classes("w-full").props("use-input input-debounce=300")

                vision_status = ui.label("").classes("text-sm")
                vision_status.visible = False

                setup_vision_dl = ui.button(f"⬇️ Download {vsvc.model}").props(
                    "color=primary outline"
                )
                setup_vision_dl.visible = vsvc.model not in local_now
                if vsvc.model in local_now:
                    vision_status.text = f"✅ {vsvc.model} is ready"
                    vision_status.visible = True

                async def _setup_dl_vision():
                    sel = setup_vision_select.value
                    if is_model_local(sel):
                        vision_status.text = f"✅ {sel} is already downloaded"
                        vision_status.visible = True
                        setup_vision_dl.visible = False
                        return
                    if not _ollama_reachable():
                        vision_status.text = (
                            "❌ Ollama is not running. Install and start Ollama first."
                        )
                        vision_status.visible = True
                        return
                    setup_vision_dl.disable()
                    vision_status.text = f"⏳ Downloading {sel}… this may take a few minutes"
                    vision_status.visible = True
                    n = ui.notification(
                        f"Downloading {sel}…",
                        type="ongoing",
                        spinner=True,
                        timeout=None,
                    )
                    await run.io_bound(lambda: list(pull_model(sel)))
                    n.dismiss()
                    vision_status.text = f"✅ {sel} downloaded successfully!"
                    setup_vision_dl.visible = False
                    setup_vision_dl.enable()
                    vsvc.model = sel

                setup_vision_dl.on_click(_setup_dl_vision)

                def _on_setup_vision_change(e):
                    sel = e.value
                    setup_vision_dl.text = f"⬇️ Download {sel}"
                    already = is_model_local(sel)
                    setup_vision_dl.visible = not already
                    if already:
                        vision_status.text = f"✅ {sel} is ready"
                        vision_status.visible = True
                        vsvc.model = sel
                    else:
                        vision_status.visible = False

                setup_vision_select.on_value_change(_on_setup_vision_change)

            ui.separator()

            # ── Optional import ──────────────────────────────────────
            ui.label("Import existing setup?").classes("text-h6")
            ui.label(
                "Bring over a previous setup now, or skip and import later from Settings."
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
                "Open Thoth now, or continue into the Setup Center for documents, workflows, Designer Studio, channels, accounts, tools, plugins, and voice."
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
                open_btn = ui.button("Open Thoth", icon="home").props(
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

            async def _finish_setup(*, continue_setup: bool = False):
                if setup_path["mode"] == "cloud":
                    sel = cloud_model_select.value
                    if sel:
                        set_model(sel)
                        state.current_model = sel
                        info = codex_models_by_ref.get(sel)
                        if info:
                            add_quick_choice_for_model(
                                info.model_id,
                                provider_id=info.provider_id,
                                display_name=info.display_name,
                                source="setup_default",
                                capabilities_snapshot=info.capability_snapshot(),
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
                mark_onboarding_step("models")
                if continue_setup:
                    setattr(state, "open_setup_center_on_next_load", True)
                    request_setup_center_on_next_load()
                mark_setup_complete()
                setup_dlg.close()
                await on_finish()

            async def _open_thoth():
                await _finish_setup(continue_setup=False)

            async def _continue_setup():
                await _finish_setup(continue_setup=True)

            open_btn.on_click(_open_thoth)
            continue_btn.on_click(_continue_setup)

    setup_dlg.open()
