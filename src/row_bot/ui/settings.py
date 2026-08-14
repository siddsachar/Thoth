"""Row-Bot UI — Settings dialog with all configuration tabs.

Contains ``open_settings()`` plus 13+ tab builder helpers.
Receives ``state`` and ``p`` explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import pathlib
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from row_bot.brand import APP_DISPLAY_NAME, DEFAULT_DATA_DIR_NAME
from row_bot.data_paths import get_row_bot_data_dir
from nicegui import events, run, ui

from row_bot.stability import log_performance_snapshot
from row_bot.ui.state import AppState, P
from row_bot.ui.iconography import icon_select_options, material_icon_for
from row_bot.ui.helpers import browse_folder, browse_file
from row_bot.ui.performance import (
    LoadGeneration,
    UI_DATA_WARN_MS,
    UI_SHELL_WARN_MS,
    log_ui_perf,
    safe_ui_callback,
    timed_ui_section,
)
from row_bot.ui.timer_utils import defer_ui, safe_ui_task

if TYPE_CHECKING:
    from row_bot.access.runtime_policy import RuntimeAccessPolicy

logger = logging.getLogger(__name__)

_DOCUMENT_JOB_STATUS_LABELS = {
    "staging": "Queued",
    "queued": "Queued",
    "indexing": "Indexing",
    "searchable": "Searchable",
    "extracting": "Extracting",
    "completed": "Complete",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "skipped_duplicate": "Duplicate skipped",
}

_PROCESS_TAILSCALE_STATUS_CACHE: Any | None = None


def _process_tailscale_status_cache() -> Any:
    """Return the one command-free Tailscale status cache for this process."""

    global _PROCESS_TAILSCALE_STATUS_CACHE
    if _PROCESS_TAILSCALE_STATUS_CACHE is None:
        from row_bot.access.tailscale import TailscaleStatusCache

        _PROCESS_TAILSCALE_STATUS_CACHE = TailscaleStatusCache()
    return _PROCESS_TAILSCALE_STATUS_CACHE


def document_job_status_label(status: str) -> str:
    """Return the agreed user-facing document-ingestion milestone."""
    return _DOCUMENT_JOB_STATUS_LABELS.get(
        str(status),
        str(status).replace("_", " ").title(),
    )

_MOBILE_SETTINGS_CSS = """
<style>
.row-bot-settings-mobile-shell {
    max-width: none !important;
    width: 100vw;
    height: 100dvh;
    border-radius: 0 !important;
    margin: 0 !important;
    background: #071113;
    color: #ecf4f5;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.row-bot-settings-mobile-header {
    flex: 0 0 auto;
    padding: calc(8px + env(safe-area-inset-top)) 12px 10px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    background: #10191c;
}
.row-bot-settings-mobile-picker {
    flex: 0 0 auto;
    padding: 10px 12px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    background: #0d171a;
}
.row-bot-settings-mobile-content {
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
    padding: 12px;
}
.row-bot-settings-mobile-shell .q-field,
.row-bot-settings-mobile-shell .q-select,
.row-bot-settings-mobile-shell .q-input,
.row-bot-settings-mobile-shell .q-textarea,
.row-bot-settings-mobile-shell .q-card,
.row-bot-settings-mobile-shell .q-table__container {
    min-width: 0;
    max-width: 100%;
}
.row-bot-settings-mobile-shell .no-wrap {
    flex-wrap: wrap !important;
}
.row-bot-settings-mobile-shell .w-64,
.row-bot-settings-mobile-shell .w-48,
.row-bot-settings-mobile-shell .w-36,
.row-bot-settings-mobile-shell .w-32,
.row-bot-settings-mobile-shell .w-28,
.row-bot-settings-mobile-shell .w-24,
.row-bot-settings-mobile-shell .w-20 {
    width: 100% !important;
}
.row-bot-settings-mobile-shell .q-badge,
.row-bot-settings-mobile-shell .q-chip {
    max-width: 100%;
    white-space: normal;
}
.row-bot-settings-mobile-shell .q-btn {
    min-width: 0;
}
.row-bot-agent-runtime-expansion {
    border: 1px solid rgba(144, 202, 249, 0.24);
    border-radius: 12px;
    background: rgba(33, 150, 243, 0.055);
    overflow: hidden;
}
.row-bot-agent-runtime-expansion > .q-expansion-item__container > .q-item {
    min-height: 82px;
    padding: 14px 16px;
}
.row-bot-agent-runtime-expansion .row-bot-agent-runtime-title {
    font-size: 1rem;
    font-weight: 600;
    line-height: 1.25rem;
}
.row-bot-agent-runtime-expansion .row-bot-agent-runtime-subtitle {
    color: rgba(255, 255, 255, 0.62);
    font-size: 0.78rem;
    line-height: 1.1rem;
}
.row-bot-agent-runtime-expansion .row-bot-agent-runtime-content {
    border-top: 1px solid rgba(144, 202, 249, 0.16);
    padding: 16px;
}
@media (max-width: 700px) {
    .row-bot-agent-runtime-expansion > .q-expansion-item__container > .q-item {
        padding: 12px;
    }
}
</style>
"""


def _agent_mode_badge_state(
    model_value: str,
    *,
    capability_snapshot: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
    context_window_override: int | None = None,
) -> dict[str, str | bool]:
    """Return a compact UI badge for the selected Brain runtime."""
    try:
        from row_bot.providers.readiness import evaluate_runtime_readiness

        runtime = evaluate_runtime_readiness(
            str(model_value or ""),
            capability_snapshot=capability_snapshot,
            status=status,
            context_window_override=context_window_override,
            probe_ollama_tools=False,
        )
    except Exception as exc:
        return {
            "visible": True,
            "label": "runtime unknown",
            "color": "orange",
            "tooltip": f"Could not evaluate runtime readiness: {exc}",
        }

    if runtime.selected_mode == "agent":
        return {"visible": False, "label": "", "color": "green", "tooltip": runtime.agent.user_message()}
    if runtime.selected_mode == "chat_only":
        blocked_details = "; ".join(runtime.agent.errors)
        if runtime.agent.provider_id == "ollama" and "tool" in blocked_details.lower():
            return {
                "visible": True,
                "label": "tools unverified",
                "color": "orange",
                "tooltip": (
                    "Normal chat can use this model. Agent Mode will run an Ollama tool probe when selected; "
                    "tools, workflows, Developer, and Designer require that probe to pass."
                ),
            }
        suffix = f" Agent Mode is unavailable: {blocked_details}" if blocked_details else ""
        return {
            "visible": True,
            "label": "chat only",
            "color": "blue",
            "tooltip": "Normal chat can use this model, but tools, workflows, Developer, and Designer require Agent Mode." + suffix,
        }

    details = "; ".join(runtime.chat.errors or runtime.agent.errors) or runtime.selection_reason
    lower = details.lower()
    if "context window" in lower:
        label = "context too small"
    elif "credentials" in lower or "not configured" in lower:
        label = "connect provider"
    elif "tool-result round trip" in lower or "proven by endpoint probe" in lower:
        label = "probe required"
    elif "openrouter tool metadata" in lower:
        label = "tools uncertain"
    elif "does not support structured tools" in lower or "tool-capable catalog" in lower:
        label = "no tools"
    elif "tool" in lower:
        label = "tools uncertain"
    else:
        label = "agent blocked"
    return {"visible": True, "label": label, "color": "orange", "tooltip": details}


def _quick_choice_media_config_value(choice: dict[str, Any]) -> str:
    provider_id = str(choice.get("provider_id") or "")
    model_id = str(choice.get("model_id") or "")
    if not (provider_id and model_id):
        try:
            from row_bot.providers.selection import parse_model_ref

            parsed = parse_model_ref(str(choice.get("id") or ""))
        except Exception:
            parsed = None
        if parsed:
            provider_id, model_id = parsed
    if provider_id and model_id:
        return f"{provider_id}/{model_id}"
    return ""


def _quick_choice_matches_media_surface(choice: dict[str, Any], surface: str) -> bool:
    if choice.get("kind") != "model":
        return False
    visibility = choice.get("visibility")
    if isinstance(visibility, (list, tuple, set)) and surface in {str(item) for item in visibility}:
        return True
    try:
        from row_bot.providers.capabilities import snapshot_supports_surface

        return snapshot_supports_surface(choice.get("capabilities_snapshot"), surface)
    except Exception:
        return False


def _pinned_media_options(
    surface: str,
    available: dict[str, str],
    current_value: str,
    quick_choices: list[dict[str, Any]],
) -> dict[str, str]:
    allowed = {
        value
        for choice in quick_choices
        if _quick_choice_matches_media_surface(choice, surface)
        for value in [_quick_choice_media_config_value(choice)]
        if value
    }
    options = {key: label for key, label in available.items() if key in allowed}
    if current_value in available:
        options[current_value] = available[current_value]
    return options


def _model_options_map(options: list[dict[str, Any]] | dict[str, str]) -> dict[str, str]:
    if isinstance(options, dict):
        return {str(key): str(value) for key, value in options.items() if str(key)}
    safe_options: dict[str, str] = {}
    for option in options or []:
        if not isinstance(option, dict):
            continue
        value = str(option.get("value") or "")
        if not value:
            continue
        safe_options[value] = str(option.get("label") or value)
    return safe_options


def _unavailable_model_label(value: str, surface: str) -> str:
    raw = str(value or "").strip()
    provider_id = ""
    model_id = raw
    try:
        from row_bot.providers.selection import parse_model_ref, provider_display_label

        parsed = parse_model_ref(raw)
        if parsed:
            provider_id, model_id = parsed
        elif "/" in raw:
            provider_id, model_id = raw.split("/", 1)
        provider_label = provider_display_label(provider_id) if provider_id else ""
    except Exception:
        provider_label = provider_id
    label = model_id or raw or surface
    return f"Unavailable: {label} - {provider_label}" if provider_label else f"Unavailable: {label}"


def _safe_model_select_state(
    surface: str,
    desired_value: str | None,
    options: list[dict[str, Any]] | dict[str, str],
    *,
    warning: str = "",
    force_unavailable: bool = False,
) -> dict[str, Any]:
    safe_options = _model_options_map(options)
    value = str(desired_value or "").strip()
    invalid = False
    if value:
        invalid = force_unavailable or value not in safe_options
        if invalid:
            safe_options[value] = _unavailable_model_label(value, surface)
        return {
            "options": safe_options,
            "value": value,
            "invalid": invalid,
            "warning": warning if invalid or warning else "",
        }
    return {
        "options": safe_options,
        "value": None,
        "invalid": False,
        "warning": warning,
    }


def open_settings(
    state: AppState,
    p: P,
    initial_tab: str = "Providers",
    *,
    mobile: bool = False,
    runtime_access_policy: RuntimeAccessPolicy | None = None,
) -> None:
    """Build and open the maximised settings dialog.

    Every tab builder is defined locally so it closes over ``state``
    and ``p``.  External deps are imported inside the tab builders to
    keep startup fast.
    """
    # ── imports used across multiple tabs ──
    from row_bot.api_keys import get_key, set_key, delete_key, key_status, get_cloud_config
    from row_bot.tools import registry as tool_registry
    from row_bot.models import (
        _ollama_reachable,
        list_local_models,
        list_cloud_models,
        list_cloud_vision_models,
        get_current_model,
        is_cloud_model,
        set_model,
        get_provider_emoji,
        get_context_policy,
        set_context_size,
        set_context_size_auto,
        get_local_context_mode,
        star_cloud_model,
        unstar_cloud_model,
        CONTEXT_SIZE_OPTIONS,
        CONTEXT_SIZE_LABELS,
        CLOUD_CONTEXT_SIZE_OPTIONS,
        CLOUD_CONTEXT_SIZE_LABELS,
        _coerce_context_size,
        get_cloud_context_size,
        get_cloud_context_override,
        set_cloud_context_size,
        clear_cloud_context_override,
        is_cloud_available,
        _cloud_model_cache,
    )
    _load_context_policy = get_context_policy
    from row_bot.providers.model_catalog_cache import (
        cache_age_label,
        is_model_catalog_refresh_running,
        model_catalog_refresh_state,
        read_model_catalog_cache,
        start_model_catalog_refresh_background,
    )
    from row_bot.documents import (
        document_vector_status,
        load_processed_files,
        rebuild_vector_store_from_vault,
        release_document_embedding_resources,
        remove_document,
        reset_vector_store,
    )
    from row_bot.embedding_config import (
        CLOUD_MODELS,
        LOCAL_MODELS,
        describe_active_embedding,
        get_embedding_config,
        save_embedding_config,
    )

    shell_started = time.perf_counter()
    settings_generation = LoadGeneration()
    _load_generation = settings_generation
    p.settings_child_modal_open = False

    # ── Recursive reopen helper ──
    def _reopen(tab: str = initial_tab):
        if getattr(p, "settings_child_modal_open", False):
            logger.info("Settings reopen deferred while child modal is open: %s", tab)
            return
        p.settings_dlg.close()
        if tab == "Cloud":
            tab = "Providers"
        open_settings(
            state,
            p,
            initial_tab=tab,
            mobile=mobile,
            runtime_access_policy=runtime_access_policy,
        )

    def _close_settings() -> None:
        settings_generation.invalidate()
        p.settings_child_modal_open = False
        p.settings_dlg.close()

    # ── Lazy helpers (deferred to avoid slow import on panel open) ──
    def clear_agent_cache():
        from row_bot.agent import clear_agent_cache as _cac
        from row_bot.threads import clear_all_context_usage
        from row_bot.ui.state import clear_all_context_usage_state

        _cac()
        clear_all_context_usage_state(state)
        clear_all_context_usage()
        if p.context_meter:
            p.context_meter.update(None)

    def clear_provider_runtime_cache():
        clear_agent_cache()
        try:
            from row_bot.models import clear_llm_cache
            clear_llm_cache()
        except Exception:
            logger.debug("Could not clear cached LLM clients", exc_info=True)

    _model_tab_sync = {"callback": None}

    def _secret_status_text(env_var: str) -> str:
        status = key_status(env_var)
        if not status.get("configured"):
            return "Not saved"
        source = status.get("source") or "saved"
        fingerprint = status.get("fingerprint") or "saved"
        if source == "keyring":
            return f"Saved securely ({fingerprint})"
        if source == "environment":
            return f"Set by environment ({fingerprint})"
        if source == "secret_file":
            return f"Mounted read-only secret ({fingerprint})"
        if source == "conflict":
            return "Secret file conflicts with the environment"
        if source == "legacy_plaintext":
            return f"Saved in legacy plaintext ({fingerprint})"
        return f"Saved for this session ({fingerprint})"

    def _secret_input(label: str, env_var: str):
        status_label = ui.label(_secret_status_text(env_var)).classes("text-grey-6 text-xs")
        inp = ui.input(
            label,
            value="",
            placeholder="Paste a new value to replace the saved one",
            password=True,
            password_toggle_button=True,
        ).classes("w-full")

        def refresh_status() -> None:
            status_label.text = _secret_status_text(env_var)
            status_label.update()

        return inp, refresh_status

    def _channel_secret_status_text(channel_name: str, env_var: str) -> str:
        from row_bot.channels.auth_store import channel_secret_status
        status = channel_secret_status(channel_name, env_var)
        if not status.get("configured"):
            return "Not saved"
        source = status.get("source") or "saved"
        fingerprint = status.get("fingerprint") or "saved"
        if source == "channel keyring":
            return f"Saved securely ({fingerprint})"
        if source == "environment":
            return f"Set by environment ({fingerprint})"
        if source == "legacy api_keys":
            return f"Saved in legacy key store ({fingerprint})"
        return f"Saved ({fingerprint})"

    def _channel_secret_input(
        label: str,
        channel_name: str,
        env_var: str,
        *,
        password: bool = True,
    ):
        from row_bot.channels.auth_store import channel_secret_status

        secret_status = channel_secret_status(channel_name, env_var)
        status_label = ui.label(
            _channel_secret_status_text(channel_name, env_var)
        ).classes("text-grey-6 text-xs")
        inp_kwargs = {
            "label": label,
            "value": "",
            "placeholder": "Paste a new value to replace the saved one",
        }
        if password:
            inp_kwargs.update({"password": True, "password_toggle_button": True})
        inp = ui.input(**inp_kwargs).classes("w-full")
        if secret_status.get("externally_managed"):
            inp.disable()
            inp.props("hint='Managed by the server operator'")

        def refresh_status() -> None:
            status_label.text = _channel_secret_status_text(channel_name, env_var)
            status_label.update()

        return inp, refresh_status

    def _import_channel_secret(
        channel_name: str,
        env_var: str,
        display: str,
        refresh: Callable[[], None] | None = None,
    ) -> None:
        from row_bot.channels.auth_store import import_channel_secret_from_fallback
        if not import_channel_secret_from_fallback(channel_name, env_var):
            ui.notify(
                f"{display} has no environment or legacy value to save",
                type="info",
            )
            return
        clear_agent_cache()
        if refresh:
            refresh()
        ui.notify(f"{display} saved securely", type="positive")

    def _clear_channel_secret(
        channel_name: str,
        env_var: str,
        display: str,
        refresh: Callable[[], None] | None = None,
    ) -> None:
        from row_bot.channels.auth_store import delete_channel_secret
        delete_channel_secret(channel_name, env_var)
        clear_agent_cache()
        if refresh:
            refresh()
        ui.notify(f"{display} cleared", type="info")

    def _secret_value_or_notify(raw: object, display: str) -> str:
        val = raw.strip() if isinstance(raw, str) else ""
        if not val:
            ui.notify(f"{display} unchanged — enter a new value to replace it", type="info")
        return val

    def _start_catalog_refresh_ui(
        *,
        reason: str = "manual",
        provider_id: str | None = None,
        force: bool = True,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        started = start_model_catalog_refresh_background(reason=reason, provider_id=provider_id, force=force)
        if not started:
            ui.notify("Model catalog refresh is already running", type="info")
            return
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
                provider_result = (
                    provider_statuses.get(provider_id)
                    if provider_id and isinstance(provider_statuses.get(provider_id), dict)
                    else {}
                )
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
                if inspect.isawaitable(result):
                    await result

        safe_ui_task(_watch, context="model catalog refresh watcher")

    def _clear_secret(env_var: str, display: str, refresh: Callable[[], None] | None = None) -> None:
        delete_key(env_var)
        clear_provider_runtime_cache()
        if refresh:
            refresh()
        ui.notify(f"{display} cleared", type="info")

    def _settings_header(title: str, subtitle: str, icon: str | None = None) -> None:
        with ui.row().classes("items-start gap-3 w-full q-mb-sm"):
            if icon:
                ui.icon(icon, size="1.55rem").classes("text-primary q-mt-xs")
            with ui.column().classes("gap-0").style("min-width: 0;"):
                ui.label(title).classes("text-h6")
                if subtitle:
                    ui.label(subtitle).classes("text-grey-6 text-sm")

    @contextlib.contextmanager
    def _settings_section(
        title: str,
        subtitle: str | None = None,
        *,
        icon: str | None = None,
        tone: str = "default",
        docs_id: str | None = None,
    ):
        border = "rgba(148, 163, 184, 0.22)"
        background = "rgba(148, 163, 184, 0.045)"
        if tone == "warning":
            border = "rgba(245, 158, 11, 0.42)"
            background = "rgba(245, 158, 11, 0.08)"
        elif tone == "danger":
            border = "rgba(239, 68, 68, 0.42)"
            background = "rgba(239, 68, 68, 0.06)"
        section = ui.column().classes(
            "w-full gap-3 q-pa-md rounded-borders q-mb-md"
        ).style(
            f"border: 1px solid {border}; background: {background};"
        )
        if docs_id:
            section.props(f'data-docs-id="{docs_id}"')
        with section:
            with ui.row().classes("items-start gap-2 w-full no-wrap"):
                if icon:
                    ui.icon(icon, size="1.15rem").classes("text-grey-5 q-mt-xs")
                with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                    ui.label(title).classes("text-subtitle2 text-weight-medium")
                    if subtitle:
                        ui.label(subtitle).classes("text-grey-6 text-xs")
            yield

    def _metric_chip(label: str, value: Any, icon: str | None = None, color: str = "blue-grey") -> None:
        with ui.row().classes("items-center gap-1 px-2 py-1 rounded-borders no-wrap").style(
            "border: 1px solid rgba(148, 163, 184, 0.24); "
            "background: rgba(148, 163, 184, 0.08);"
        ):
            if icon:
                ui.icon(icon, size="0.95rem").classes(f"text-{color}")
            ui.label(str(value)).classes("text-weight-bold text-xs")
            ui.label(label).classes("text-grey-6 text-xs")

    def _status_dot(label: str, state_name: str, detail: str | None = None) -> None:
        colors = {
            "ok": "#22c55e",
            "running": "#22c55e",
            "warn": "#f59e0b",
            "warning": "#f59e0b",
            "error": "#ef4444",
            "off": "#64748b",
            "inactive": "#64748b",
            "neutral": "#94a3b8",
        }
        dot = colors.get(state_name, colors["neutral"])
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.element("span").style(
                f"width: 8px; height: 8px; border-radius: 999px; "
                f"background: {dot}; display: inline-block; flex: 0 0 auto;"
            )
            ui.label(label).classes("text-sm")
            if detail:
                ui.label(detail).classes("text-grey-6 text-xs")

    def _build_window_mode_section() -> None:
        from row_bot.ui.helpers import load_app_config, save_app_config

        with _settings_section(
            "Window Mode",
            f"Choose how {APP_DISPLAY_NAME} opens on launch. Takes effect next time the app starts.",
            icon="web_asset",
        ):
            _wm_cfg = load_app_config()
            _current_mode = _wm_cfg.get("window_mode", "ask")

            def _on_window_mode_change(e):
                cfg = load_app_config()
                cfg["window_mode"] = e.value
                save_app_config(cfg)
                ui.notify("Window mode saved for next launch", type="info")

            with ui.row().classes("items-center gap-3 w-full"):
                ui.select(
                    {"ask": "Ask on Launch", "native": "Native Window", "browser": "System Browser"},
                    value=_current_mode,
                    label="Window mode",
                    on_change=_on_window_mode_change,
                ).classes("w-64").props("dense outlined").tooltip("Takes effect on next launch")
                ui.label(
                    f"Native Window gives {APP_DISPLAY_NAME} its own app window. System Browser uses your default browser."
                ).classes("text-grey-6 text-xs")

    def _build_dream_cycle_section() -> None:
        import row_bot.dream_cycle as dream_cycle

        dream_cfg = dream_cycle.get_config()
        dream_status = dream_cycle.get_dream_status()
        enabled = bool(dream_cfg.get("enabled", True))
        last_run = dream_status.get("last_run")
        with _settings_section(
            "Dream Cycle",
            "Idle background cleanup for memory: merge duplicates, enrich sparse notes, and infer missing links.",
            icon="nightlight",
        ):
            with ui.row().classes("items-center gap-2 q-mb-xs"):
                _status_dot(
                    "Enabled" if enabled else "Disabled",
                    "ok" if enabled else "inactive",
                    "Runs only during the configured idle window.",
                )
                if last_run:
                    try:
                        last_dt = datetime.fromisoformat(last_run)
                        _metric_chip("last run", last_dt.strftime("%b %d, %I:%M %p"), icon="history")
                    except (ValueError, TypeError):
                        _metric_chip("last run", "unknown", icon="history")
                else:
                    _metric_chip("last run", "never", icon="history")

            def _toggle_dream(e):
                dream_cycle.set_enabled(e.value)
                ui.notify(
                    "Dream cycle enabled." if e.value else "Dream cycle disabled.",
                    type="info",
                )
                _reopen("Preferences")

            ui.switch(
                "Enable Dream Cycle",
                value=enabled,
                on_change=_toggle_dream,
            )

            with ui.row().classes("gap-4 items-center"):
                ui.label("Idle window").classes("text-sm")

                _start_val = f"{dream_cfg.get('window_start', 1):02d}:00"
                with ui.input("Start", value=_start_val).props(
                    "dense outlined"
                ).classes("w-28") as _dream_start_input:
                    with ui.menu().props("no-parent-event") as _start_menu:
                        ui.time(value=_start_val, mask="HH:00").props(
                            "format24h"
                        ).bind_value(_dream_start_input)
                    with _dream_start_input.add_slot("append"):
                        ui.icon("schedule").on("click", _start_menu.open).classes(
                            "cursor-pointer"
                        )

                ui.label("-").classes("text-sm")

                _end_val = f"{dream_cfg.get('window_end', 5):02d}:00"
                with ui.input("End", value=_end_val).props(
                    "dense outlined"
                ).classes("w-28") as _dream_end_input:
                    with ui.menu().props("no-parent-event") as _end_menu:
                        ui.time(value=_end_val, mask="HH:00").props(
                            "format24h"
                        ).bind_value(_dream_end_input)
                    with _dream_end_input.add_slot("append"):
                        ui.icon("schedule").on("click", _end_menu.open).classes(
                            "cursor-pointer"
                        )

                def _on_dream_window_change(_=None):
                    try:
                        s = int(_dream_start_input.value.split(":")[0])
                        e = int(_dream_end_input.value.split(":")[0])
                    except (ValueError, AttributeError):
                        return
                    dream_cycle.set_window(s, e)
                    ui.notify(f"Dream window updated: {s:02d}:00 - {e:02d}:00", type="info")

                _dream_start_input.on("update:model-value", _on_dream_window_change)
                _dream_end_input.on("update:model-value", _on_dream_window_change)

            summary = dream_status.get("last_summary") or ""
            if summary:
                ui.label(summary).classes("text-xs text-grey-6")
            elif not last_run:
                ui.label("No dream cycles have run yet.").classes("text-xs text-grey-6")

    def _build_tunnel_settings_section() -> None:
        from row_bot.channels import config as _ch_config
        from row_bot.tunnel import tunnel_manager

        with _settings_section(
            "Tunnel Settings",
            "Securely expose local webhook ports to the internet for channels and workflow webhooks.",
            icon="lan",
        ):
            tunnel_status, tunnel_detail = tunnel_manager.status()
            tunnel_ready = tunnel_manager.is_available()
            with ui.row().classes("items-center gap-2 q-mb-xs"):
                _status_dot(
                    "ngrok available" if tunnel_ready else tunnel_detail,
                    "ok" if tunnel_ready else ("error" if tunnel_status == "error" else "warn"),
                    tunnel_detail or "The ngrok binary downloads automatically on first use.",
                )
                active_count = len(tunnel_manager.active_tunnels())
                _metric_chip("active tunnel" if active_count == 1 else "active tunnels", active_count, icon="hub")

            provider_val = _ch_config.get("tunnel", "provider", "ngrok")
            provider_select = ui.select(
                label="Provider",
                options=["ngrok"],
                value=provider_val,
            ).classes("w-full").style("max-width: 300px").props("dense outlined")

            token_input, token_refresh = _secret_input("Authtoken", "NGROK_AUTHTOKEN")
            token_input.tooltip("Your ngrok authtoken from https://dashboard.ngrok.com/")

            tunnel_container = ui.column().classes("w-full q-mt-sm")

            def _refresh_active_tunnels():
                tunnel_container.clear()
                with tunnel_container:
                    active = tunnel_manager.active_tunnels()
                    if active:
                        ui.label("Active tunnels").classes("text-weight-medium text-sm")
                        for port, url in active.items():
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.badge(f"Port {port}", color="blue-grey").props("outline dense")
                                ui.label(url).classes("text-sm text-primary").style("word-break: break-all;")
                                ui.button(
                                    icon="content_copy",
                                    on_click=lambda u=url: (
                                        ui.run_javascript(f"navigator.clipboard.writeText('{u}')"),
                                        ui.notify("Copied", type="info"),
                                    ),
                                ).props("flat dense round size=sm").tooltip("Copy URL")
                    else:
                        status_code, status_detail = tunnel_manager.status()
                        ui.label(
                            "No active tunnels. Start a webhook channel or expose the task webhook endpoint to open one."
                            if tunnel_manager.is_available()
                            else status_detail or "Paste and save your authtoken to enable tunnels."
                        ).classes("text-grey-6 text-sm")

            def _save_tunnel_settings():
                _ch_config.set("tunnel", "provider", provider_select.value)
                raw = token_input.value.strip() if isinstance(token_input.value, str) else ""
                if raw:
                    set_key("NGROK_AUTHTOKEN", raw)
                    token_input.value = ""
                    token_input.update()
                    token_refresh()
                status_code, status_detail = tunnel_manager.status()
                if status_code == "error":
                    ui.notify(f"Tunnel settings saved, but {status_detail}", type="warning")
                else:
                    ui.notify("Tunnel settings saved", type="positive")
                _refresh_active_tunnels()

            with ui.row().classes("gap-2"):
                ui.button("Save", icon="save", on_click=_save_tunnel_settings).props("flat dense no-caps color=primary")
                ui.button(
                    icon="delete",
                    on_click=lambda: _clear_secret("NGROK_AUTHTOKEN", "ngrok authtoken", token_refresh),
                ).props("flat dense round color=negative").tooltip("Clear authtoken")

            _refresh_active_tunnels()

            with ui.expansion("Tunnel Setup", icon="help_outline").classes("w-full q-mt-sm"):
                ui.markdown(
                    "1. Sign up at [ngrok.com](https://ngrok.com/) (free tier available)\n"
                    "2. Copy your **authtoken** from the "
                    "[dashboard](https://dashboard.ngrok.com/get-started/your-authtoken)\n"
                    "3. Paste it above and click **Save**\n"
                    "4. Start a webhook channel or expose task webhooks when needed\n\n"
                    "*The ngrok binary is downloaded automatically on first use.*",
                    extras=["code-friendly", "fenced-code-blocks"],
                ).classes("text-sm")

            ui.separator().classes("q-mt-sm")
            raw_main_app_val = _ch_config.get("tunnel", "tunnel_main_app", False)
            main_app_val = raw_main_app_val is True
            if isinstance(raw_main_app_val, list) and raw_main_app_val:
                main_app_val = raw_main_app_val[0] is True
                _ch_config.set("tunnel", "tunnel_main_app", main_app_val)
            main_app_switch = ui.switch(
                "Expose task webhook endpoint",
                value=main_app_val,
            )
            main_app_switch.tooltip(
                f"Tunnel the main {APP_DISPLAY_NAME} port so external services can trigger "
                "task webhooks via /api/webhook/{task_id}. This also exposes "
                "the web UI via the tunnel URL."
            )

            main_app_url_container = ui.column().classes("w-full")

            async def _on_main_app_toggle(e):
                from row_bot.app_port import get_app_port

                enabled = bool(getattr(e, "value", False))
                _ch_config.set("tunnel", "tunnel_main_app", enabled)
                if enabled and not tunnel_manager.is_available():
                    status_code, status_detail = tunnel_manager.status()
                    logger.warning("Main-app tunnel requested but unavailable: %s", status_detail)
                    _ch_config.set("tunnel", "tunnel_main_app", False)
                    main_app_switch.value = False
                    main_app_switch.update()
                    ui.notify(
                        f"Tunnel cannot start: {status_detail}",
                        type="negative",
                    )
                    _refresh_active_tunnels()
                    return
                if enabled:
                    try:
                        app_port = get_app_port()
                        url = tunnel_manager.start_tunnel(app_port, label="main_app")
                        main_app_url_container.clear()
                        with main_app_url_container:
                            webhook_url = f"{url}/api/webhook/{{task_id}}"
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.label(webhook_url).classes("text-sm text-primary").style("word-break: break-all;")
                                ui.button(
                                    icon="content_copy",
                                    on_click=lambda u=webhook_url: (
                                        ui.run_javascript(f"navigator.clipboard.writeText('{u}')"),
                                        ui.notify("Copied", type="info"),
                                    ),
                                ).props("flat dense round size=sm").tooltip("Copy webhook URL")
                        _refresh_active_tunnels()
                    except Exception as exc:
                        logger.warning("Main-app tunnel failed to start: %s", exc)
                        _ch_config.set("tunnel", "tunnel_main_app", False)
                        main_app_switch.value = False
                        main_app_switch.update()
                        main_app_url_container.clear()
                        _refresh_active_tunnels()
                        ui.notify(f"Tunnel error: {exc}", type="negative")
                elif not enabled:
                    try:
                        app_port = get_app_port()
                        tunnel_manager.stop_tunnel(app_port)
                    except Exception:
                        pass
                    main_app_url_container.clear()
                    _refresh_active_tunnels()

            main_app_switch.on("update:model-value", _on_main_app_toggle)

    # ══════════════════════════════════════════════════════════════════
    # TAB BUILDERS
    # ══════════════════════════════════════════════════════════════════

    def _build_documents_tab() -> None:
        _settings_header(
            "Documents",
            "Upload files, choose embedding engines, rebuild indexes, and manage indexed source material.",
            "description",
        )

        emb_cfg = get_embedding_config()
        doc_status = document_vector_status()
        from row_bot.embedding_providers import get_local_embedding_status
        import row_bot.knowledge_graph as kg

        local_embedding_status = get_local_embedding_status()
        memory_status = kg.memory_vector_status()
        processed = load_processed_files()
        from row_bot.document_jobs import (
            MAX_UPLOAD_BYTES,
            DocumentJobService,
            ensure_document_supervisor,
        )

        document_job_service = DocumentJobService()
        ensure_document_supervisor(document_job_service)
        durable_records = document_job_service.list_document_records()
        with ui.row().classes("items-center gap-2 q-mb-sm"):
            _metric_chip("indexed", len(processed), icon="library_books")
            _metric_chip("embedding", describe_active_embedding(emb_cfg), icon="hub")
            _status_dot(
                "Vectors stale" if doc_status["stale"] else "Vectors current",
                "warn" if doc_status["stale"] else "ok",
            )
        local_options = {key: val["label"] for key, val in LOCAL_MODELS.items()}
        cloud_options = {key: val["label"] for key, val in CLOUD_MODELS.items()}
        with _settings_section(
            "Embedding Engine",
            "Local models are private but use RAM; cloud models reduce local memory and send text to the provider.",
            icon="hub",
        ):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Embedding engine").classes("text-subtitle2")
                ui.badge("Index rebuild required after changes", color="blue-grey").props("outline dense")
            ui.label(
                f"Current: {describe_active_embedding(emb_cfg)}. "
                "Local models load from the existing on-device cache during normal use. "
                "Cloud models reduce local memory and send chunks to the provider."
            ).classes("text-grey-6 text-xs")
            if doc_status["stale"]:
                ui.label(
                    "Document vectors were built with a different embedding setting. Rebuild document vectors before relying on document search."
                ).classes("text-warning text-xs")

            provider_sel = ui.select(
                label="Provider",
                options={"local": "Local runtime model", "cloud": "Cloud embedding model"},
                value=emb_cfg["provider"],
            ).classes("w-full").props("dense outlined")
            local_sel = ui.select(
                label="Local model",
                options=local_options,
                value=emb_cfg["local_model"],
            ).classes("w-full").props("dense outlined")
            cloud_sel = ui.select(
                label="Cloud model",
                options=cloud_options,
                value=emb_cfg["cloud_model"],
            ).classes("w-full").props("dense outlined")
            dimension_input = ui.number(
                label="Dimension override",
                value=emb_cfg.get("dimension"),
                min=0,
                step=1,
            ).classes("w-full").props("dense outlined").tooltip("Leave blank or 0 for the model default.")
            unload_switch = ui.switch(
                "Auto-unload local embedding resources after heavy work",
                value=bool(emb_cfg.get("auto_unload", False)),
            ).tooltip("Disabled by default so a loaded local model stays ready for later recall.")
            privacy_label = ui.label(
                "Cloud embeddings send document chunks and memory text to the selected provider."
            ).classes("text-warning text-xs")

            local_status_panel = (
                ui.column()
                .classes("w-full gap-1 q-pa-sm rounded-borders")
                .style(
                    "border: 1px solid rgba(148, 163, 184, 0.24); "
                    "background: rgba(148, 163, 184, 0.08);"
                )
            )
            with local_status_panel:
                ui.label(
                    f"Local model: {local_embedding_status['state']} — "
                    f"{local_embedding_status.get('detail') or local_embedding_status['label']}"
                ).classes("text-xs")
                ui.label(
                    f"Memory index: {memory_status['state']} — {memory_status['detail']}"
                ).classes("text-xs")
                ui.label(
                    "Normal recall never downloads a model. Downloads and repairs happen only from the actions below."
                ).classes("text-grey-6 text-xs")

            def _sync_embedding_controls():
                is_cloud = provider_sel.value == "cloud"
                local_sel.visible = not is_cloud
                cloud_sel.visible = is_cloud
                privacy_label.visible = is_cloud
                local_status_panel.visible = not is_cloud

            provider_sel.on("update:model-value", lambda _: _sync_embedding_controls())
            _sync_embedding_controls()

            def _save_embedding_settings():
                save_embedding_config({
                    "provider": provider_sel.value,
                    "local_model": local_sel.value,
                    "cloud_model": cloud_sel.value,
                    "dimension": int(dimension_input.value or 0) or None,
                    "auto_unload": bool(unload_switch.value),
                })
                release_document_embedding_resources("embedding settings changed")
                ui.notify("Embedding settings saved. Rebuild indexes when ready.", type="positive")
                _reopen("Documents")

            async def _rebuild_document_vectors():
                n = ui.notification("Rebuilding document vectors...", type="ongoing", spinner=True, timeout=None)
                try:
                    count = await run.io_bound(rebuild_vector_store_from_vault)
                    n.dismiss()
                    ui.notify(f"Rebuilt document vectors for {count} document(s)", type="positive")
                    _reopen("Documents")
                except Exception as exc:
                    n.dismiss()
                    logger.error("Document vector rebuild failed", exc_info=True)
                    ui.notify(f"Document vector rebuild failed: {exc}", type="negative", close_button=True)

            async def _rebuild_memory_vectors():
                n = ui.notification("Rebuilding memory vectors...", type="ongoing", spinner=True, timeout=None)
                try:
                    import row_bot.knowledge_graph as kg

                    await run.io_bound(kg.rebuild_index)
                    n.dismiss()
                    ui.notify("Memory vectors rebuilt", type="positive")
                except Exception as exc:
                    n.dismiss()
                    logger.error("Memory vector rebuild failed", exc_info=True)
                    ui.notify(f"Memory vector rebuild failed: {exc}", type="negative", close_button=True)

            async def _retry_local_load():
                from row_bot.embedding_providers import (
                    get_embedding_provider_for_recall,
                    retry_local_embedding_load,
                )

                n = ui.notification(
                    "Loading local embedding model from cache...",
                    type="ongoing",
                    spinner=True,
                    timeout=None,
                )
                try:
                    await run.io_bound(retry_local_embedding_load)
                    await run.io_bound(get_embedding_provider_for_recall)
                    n.dismiss()
                    ui.notify("Local embedding model is ready", type="positive")
                    _reopen("Documents")
                except Exception as exc:
                    n.dismiss()
                    logger.error("Local embedding model retry failed", exc_info=True)
                    ui.notify(
                        f"Local embedding model retry failed: {exc}",
                        type="negative",
                        close_button=True,
                    )

            async def _download_or_repair_local_model(*, repair: bool) -> None:
                from row_bot.embedding_providers import download_local_embedding_model

                action = "Repairing" if repair else "Downloading"
                n = ui.notification(
                    f"{action} local embedding model...",
                    type="ongoing",
                    spinner=True,
                    timeout=None,
                )
                try:
                    await run.io_bound(
                        download_local_embedding_model,
                        str(local_sel.value),
                        repair=repair,
                    )
                    n.dismiss()
                    ui.notify(
                        "Local embedding model repaired" if repair else "Local embedding model downloaded",
                        type="positive",
                    )
                    _reopen("Documents")
                except Exception as exc:
                    n.dismiss()
                    logger.error("Local embedding model download failed", exc_info=True)
                    ui.notify(
                        f"Local embedding model {'repair' if repair else 'download'} failed: {exc}",
                        type="negative",
                        close_button=True,
                    )

            with ui.row().classes("items-center gap-2"):
                ui.button("Save embedding settings", icon="save", on_click=_save_embedding_settings).props("flat dense no-caps color=primary")
                ui.button("Rebuild document vectors", icon="refresh", on_click=_rebuild_document_vectors).props("flat dense no-caps")
                ui.button("Rebuild memory index", icon="hub", on_click=_rebuild_memory_vectors).props("flat dense no-caps")
            with ui.row().classes("items-center gap-2") as local_action_row:
                ui.button("Retry local load", icon="replay", on_click=_retry_local_load).props("flat dense no-caps")
                ui.button(
                    "Download model",
                    icon="download",
                    on_click=lambda: _download_or_repair_local_model(repair=False),
                ).props("flat dense no-caps")
                ui.button(
                    "Repair local model",
                    icon="build",
                    on_click=lambda: _download_or_repair_local_model(repair=True),
                ).props("flat dense no-caps color=warning")
            local_action_row.visible = provider_sel.value == "local"
            provider_sel.on(
                "update:model-value",
                lambda _: setattr(local_action_row, "visible", provider_sel.value == "local"),
            )

        async def _handle_doc_upload_batch(e: events.MultiUploadEventArguments):
            from row_bot.document_uploads import stage_upload

            batch_id = document_job_service.create_batch()
            accepted = 0
            for sequence, upload in enumerate(e.files):
                try:
                    await stage_upload(
                        document_job_service,
                        batch_id,
                        sequence,
                        upload.name,
                        upload,
                        declared_size=upload.size(),
                    )
                    accepted += 1
                except Exception as exc:
                    logger.error(
                        "Document upload staging rejected for %s",
                        upload.name,
                        exc_info=True,
                    )
                    ui.notify(
                        f"{upload.name} was not accepted: {exc}",
                        type="negative",
                        close_button=True,
                    )
            try:
                document_job_service.finish_batch_staging(batch_id)
            except Exception:
                logger.error("Document batch staging finalization failed", exc_info=True)
                ui.notify("The document batch could not be queued.", type="negative")
                return
            doc_upload.reset()
            if accepted:
                ui.notify(
                    f"Accepted {accepted} document{'s' if accepted != 1 else ''} for processing.",
                    type="positive",
                )
            queue_panel.refresh()

        with _settings_section(
            "Upload & Index",
            "Supported files: PDF, DOC, DOCX, TXT, MD, HTML, HTM, and EPUB.",
            icon="upload_file",
        ):
            doc_upload = ui.upload(
                label="Upload documents",
                on_multi_upload=_handle_doc_upload_batch,
                auto_upload=True,
                multiple=True,
                max_file_size=MAX_UPLOAD_BYTES,
            ).classes("w-full").props('flat bordered hide-upload-btn')

        with _settings_section(
            "Ingestion Queue",
            "Documents become searchable before knowledge extraction finishes.",
            icon="pending_actions",
        ):
            @ui.refreshable
            def queue_panel() -> None:
                batches = document_job_service.list_batches()
                jobs_by_batch: dict[str, list] = {}
                for job in document_job_service.list_jobs():
                    jobs_by_batch.setdefault(job.batch_id, []).append(job)
                if not batches:
                    ui.label("No queued document work.").classes("text-grey-6 text-sm")
                for batch in batches:
                    with ui.card().classes("w-full q-pa-sm"):
                        with ui.row().classes("w-full items-center gap-2"):
                            ui.label(
                                f"Batch · {batch.status.replace('_', ' ').title()}"
                            ).classes("text-sm font-bold")
                            ui.space()
                            if batch.status not in {
                                "completed",
                                "completed_with_errors",
                                "cancelled",
                            }:
                                if batch.pause_requested:
                                    ui.button(
                                        "Resume",
                                        icon="play_arrow",
                                        on_click=lambda batch_id=batch.id: (
                                            document_job_service.pause_batch(batch_id, False),
                                            queue_panel.refresh(),
                                        ),
                                    ).props("flat dense no-caps")
                                else:
                                    ui.button(
                                        "Pause",
                                        icon="pause",
                                        on_click=lambda batch_id=batch.id: (
                                            document_job_service.pause_batch(batch_id, True),
                                            queue_panel.refresh(),
                                        ),
                                    ).props("flat dense no-caps")
                                ui.button(
                                    "Cancel remaining",
                                    icon="stop",
                                    on_click=lambda batch_id=batch.id: (
                                        document_job_service.cancel_batch(batch_id),
                                        queue_panel.refresh(),
                                    ),
                                ).props("flat dense no-caps color=negative")
                        for job in jobs_by_batch.get(batch.id, []):
                            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                                ui.icon("description", size="xs").classes("text-grey-6")
                                ui.label(job.original_name).classes("text-sm").style(
                                    "min-width:0; flex:1; overflow:hidden; "
                                    "text-overflow:ellipsis; white-space:nowrap;"
                                )
                                ui.badge(document_job_status_label(job.status)).props(
                                    "outline dense"
                                ).style("flex-shrink:0;")
                                current = (
                                    job.index_progress_current
                                    if job.status == "indexing"
                                    else job.extraction_progress_current
                                )
                                total = (
                                    job.index_progress_total
                                    if job.status == "indexing"
                                    else job.extraction_progress_total
                                )
                                if total and job.status in {"indexing", "extracting"}:
                                    ui.label(f"{current}/{total}").classes(
                                        "text-xs text-grey-6"
                                    )
                                if job.status in {
                                    "staging",
                                    "queued",
                                    "indexing",
                                    "searchable",
                                    "extracting",
                                }:
                                    ui.button(
                                        icon="cancel",
                                        on_click=lambda job_id=job.id: (
                                            document_job_service.cancel_job(job_id),
                                            queue_panel.refresh(),
                                        ),
                                    ).props(
                                        "flat dense round size=xs color=negative"
                                    ).style("flex-shrink:0;").tooltip(
                                        f"Cancel {job.original_name}"
                                    )
                                elif job.status == "failed":
                                    ui.button(
                                        icon="replay",
                                        on_click=lambda job_id=job.id: (
                                            document_job_service.retry_failed(job_id),
                                            queue_panel.refresh(),
                                        ),
                                    ).props("flat dense round size=xs").style(
                                        "flex-shrink:0;"
                                    ).tooltip(f"Retry {job.original_name}")
                if any(
                    batch.status
                    in {"completed", "completed_with_errors", "cancelled"}
                    for batch in batches
                ):
                    ui.button(
                        "Clear finished",
                        icon="clear_all",
                        on_click=lambda: (
                            document_job_service.clear_finished(),
                            queue_panel.refresh(),
                        ),
                    ).props("flat dense no-caps")

            queue_panel()
            ui.timer(1.0, queue_panel.refresh)

        with _settings_section("Indexed Documents", "Remove individual sources when they are no longer needed.", icon="inventory_2"):
            durable_names = {
                str(record["original_name"]) for record in durable_records
            }
            indexed_rows = [
                (
                    str(record["document_id"]),
                    str(record["original_name"]),
                    False,
                )
                for record in durable_records
            ] + [
                (name, name, True)
                for name in sorted(set(processed) - durable_names)
            ]
            if indexed_rows:
                ui.label(f"{len(indexed_rows)} indexed document(s)").classes("font-bold")
                name_counts: dict[str, int] = {}
                for _identifier, display_name, _legacy in indexed_rows:
                    name_counts[display_name] = name_counts.get(display_name, 0) + 1
                for identifier, display_name, is_legacy in indexed_rows:
                    with ui.row().classes("items-center gap-1 w-full no-wrap"):
                        ui.icon("description", size="sm").classes("text-grey-6")
                        shown_name = display_name
                        if name_counts[display_name] > 1 and not is_legacy:
                            shown_name = f"{display_name} · {identifier[:8]}"
                        ui.label(shown_name).classes("text-sm").style("min-width: 0; flex: 1;")

                        def _make_delete(
                            doc_id=identifier,
                            name=display_name,
                            legacy=is_legacy,
                        ):
                            async def _do_delete():
                                import row_bot.knowledge_graph as kg
                                n = ui.notification(f"Removing {name}...", type="ongoing", spinner=True, timeout=None)
                                try:
                                    await run.io_bound(remove_document, doc_id)
                                    source = (
                                        f"document:{name}"
                                        if legacy
                                        else f"document:{doc_id}"
                                    )
                                    await run.io_bound(kg.delete_entities_by_source, source)
                                    n.dismiss()
                                    ui.notify(f"Removed {name}", type="info")
                                    _reopen("Documents")
                                except Exception as exc:
                                    n.dismiss()
                                    ui.notify(f"Delete failed: {exc}", type="negative")
                            return _do_delete

                        ui.button(icon="delete", on_click=_make_delete()).props(
                            "flat dense round size=xs color=negative"
                        ).tooltip(f"Remove {display_name}")
            else:
                ui.label("No documents indexed yet.").classes("text-grey-6")

        with _settings_section(
            "Danger Zone",
            "Clear document vectors and document-derived knowledge.",
            icon="warning",
            tone="danger",
        ):
            _clearing_docs = False

            async def _clear_docs():
                nonlocal _clearing_docs
                if _clearing_docs:
                    return
                _clearing_docs = True
                try:
                    confirm = await ui.run_javascript(
                        "confirm('Clear ALL documents? This will remove all indexed files and their extracted knowledge. This cannot be undone.')",
                        timeout=30,
                    )
                    if confirm:
                        import row_bot.knowledge_graph as kg
                        reset_vector_store()
                        document_job_service.clear_document_records()
                        kg.delete_entities_by_source_prefix("document:")
                        ui.notify("All documents and extracted knowledge cleared.", type="info")
                        _reopen("Documents")
                finally:
                    _clearing_docs = False

            ui.button("Clear all documents", icon="delete", on_click=_clear_docs).props("flat color=negative no-caps")

    # ── Models Tab ───────────────────────────────────────────────────

    def _render_agent_runtime_settings() -> None:
        from row_bot.agent_settings import (
            AgentRuntimeSettings,
            load_agent_runtime_settings,
            reset_agent_runtime_settings,
            save_agent_runtime_settings,
        )

        settings = load_agent_runtime_settings()

        def _summary(value: AgentRuntimeSettings) -> tuple[str, ...]:
            timeout_text = (
                f"{value.child_timeout_seconds}s child limit"
                if value.child_timeout_seconds
                else "No child timeout"
            )
            return (
                f"{value.max_iterations} work rounds",
                f"{value.max_spawn_depth} nested level{'s' if value.max_spawn_depth != 1 else ''}",
                f"{value.max_concurrent_children} per parent",
                f"{value.max_active_children_global} active total",
                timeout_text,
            )

        expansion = ui.expansion(value=False).classes(
            "row-bot-agent-runtime-expansion w-full q-mb-md"
        ).props("expand-separator")
        summary_badges: list[Any] = []
        with expansion.add_slot("header"):
            with ui.row().classes("w-full items-center gap-3 no-wrap"):
                ui.icon("account_tree", size="sm").classes("text-primary")
                with ui.column().classes("min-w-0 gap-1"):
                    ui.label("Agent runtime & delegation").classes(
                        "row-bot-agent-runtime-title"
                    )
                    ui.label(
                        "Optional limits for long-running work and delegated child agents"
                    ).classes("row-bot-agent-runtime-subtitle")
                    with ui.row().classes("items-center gap-1 q-mt-xs"):
                        for summary in _summary(settings):
                            summary_badges.append(
                                ui.badge(summary, color="blue-grey").props("outline")
                            )
        with expansion:
            with ui.column().classes("row-bot-agent-runtime-content w-full gap-3"):
                ui.label(
                    "These application-wide limits apply to new runs. Active work keeps "
                    "the limits it started with; the recommended defaults suit normal use."
                ).classes("text-grey-6 text-sm")

                def _with_help(control: Any, explanation: str) -> Any:
                    with control.add_slot("append"):
                        ui.icon("help_outline", size="xs").classes(
                            "cursor-help text-grey-6"
                        ).tooltip(explanation)
                    return control

                with ui.grid().classes("w-full gap-3 grid-cols-1 md:grid-cols-2"):
                    max_iterations = _with_help(
                        ui.number(
                            "Maximum work rounds",
                            value=settings.max_iterations,
                            min=1,
                            step=1,
                        ).props("dense outlined"),
                        "Maximum model-and-tool work rounds in one agent run before it "
                        "stops gracefully. This is not a per-tool-call timeout.",
                    )
                    max_depth = _with_help(
                        ui.number(
                            "Maximum nested agent levels",
                            value=settings.max_spawn_depth,
                            min=1,
                            step=1,
                        ).props("dense outlined"),
                        "How many child-agent levels may exist below the main agent. "
                        "A value of 1 allows children but not grandchildren.",
                    )
                    per_parent = _with_help(
                        ui.number(
                            "Active children per parent",
                            value=settings.max_concurrent_children,
                            min=1,
                            step=1,
                        ).props("dense outlined"),
                        "Maximum children one parent may run at the same time. Extra "
                        "children wait in the queue instead of being rejected.",
                    )
                    global_active = _with_help(
                        ui.number(
                            "Active children across the app",
                            value=settings.max_active_children_global,
                            min=1,
                            step=1,
                        ).props("dense outlined"),
                        "Application-wide child-agent concurrency cap. Extra children "
                        "queue until capacity is available.",
                    )
                    timeout = _with_help(
                        ui.number(
                            "Child active-time limit (seconds; 0 disables)",
                            value=settings.child_timeout_seconds,
                            min=0,
                            step=1,
                        ).props("dense outlined"),
                        "Optional active execution limit for each child. Queue time is "
                        "not counted; 0 leaves the child without a time limit.",
                    )

                def _apply(saved: AgentRuntimeSettings) -> None:
                    values = (
                        (max_iterations, saved.max_iterations),
                        (max_depth, saved.max_spawn_depth),
                        (per_parent, saved.max_concurrent_children),
                        (global_active, saved.max_active_children_global),
                        (timeout, saved.child_timeout_seconds),
                    )
                    for control, value in values:
                        control.value = value
                        control.update()
                    for badge, summary in zip(
                        summary_badges, _summary(saved), strict=True
                    ):
                        badge.text = summary
                        badge.update()

                def _save() -> None:
                    try:
                        saved = save_agent_runtime_settings(
                            AgentRuntimeSettings(
                                max_iterations=int(max_iterations.value),
                                max_spawn_depth=int(max_depth.value),
                                max_concurrent_children=int(per_parent.value),
                                max_active_children_global=int(global_active.value),
                                child_timeout_seconds=int(timeout.value),
                            )
                        )
                    except (TypeError, ValueError) as exc:
                        ui.notify(str(exc), type="negative")
                        return
                    _apply(saved)
                    ui.notify(
                        "Agent runtime settings saved for new runs.", type="positive"
                    )

                def _restore() -> None:
                    saved = reset_agent_runtime_settings()
                    _apply(saved)
                    ui.notify(
                        "Recommended agent runtime defaults restored.", type="positive"
                    )

                with ui.row().classes("items-center gap-2 q-mt-sm"):
                    ui.button("Save", icon="save", on_click=_save).props(
                        "color=primary no-caps"
                    )
                    ui.button("Restore recommended defaults", on_click=_restore).props(
                        "flat no-caps"
                    )

    def _render_models_tab_content(preloaded: dict | None = None) -> None:
        from row_bot.ui.chat_components import context_policy_presentation
        from row_bot.providers.selection import (
            list_model_choice_options,
            list_quick_choices,
            model_choice_options_map,
            model_choice_value,
            model_id_from_choice_value,
        )

        snapshot = preloaded or {}
        _ollama_up = bool(snapshot.get("ollama_up"))
        trending: list[str] = []
        local_models = list(snapshot.get("local") or [])
        chat_options = list(snapshot.get("chat_options") or [])
        vision_options = list(snapshot.get("vision_options") or [])

        local = local_models
        local_ref = [set(local_models)]
        current = str(snapshot.get("current_model") or state.current_model or get_current_model())
        state.current_model = current
        current_value = model_choice_value(current)

        def _is_local_runtime(runtime_model: str | None, local_override=None) -> bool:
            runtime = model_id_from_choice_value(runtime_model or "")
            if not runtime:
                return False
            loc = set(local_override) if local_override is not None else local_ref[0]
            return any(
                runtime == model
                or f"{runtime}:latest" == model
                or runtime == str(model).split(":", 1)[0]
                for model in loc
            )

        def _is_local_selection(value: object, local_override=None) -> bool:
            selected = str(value or "")
            if selected.startswith("model:"):
                parts = selected.split(":", 2)
                if len(parts) == 3 and parts[1] not in {"local", "ollama"}:
                    return False
            return _is_local_runtime(model_id_from_choice_value(selected), local_override)

        def _model_label(m, local_override=None):
            runtime = model_id_from_choice_value(m)
            if is_cloud_model(m):
                return f"{get_provider_emoji(m)}  {m}"
            if _is_local_runtime(runtime, local_override):
                return f"✅  {runtime}"
            return f"⚠️  {runtime}"

        model_opts = _model_options_map(chat_options)

        initial_policy = snapshot.get("context_policy")
        _policy_ref = [initial_policy]
        _is_cloud_ctx = getattr(initial_policy, "policy_kind", "local") == "provider"
        ctx_opts = {v: CONTEXT_SIZE_LABELS.get(v, str(v)) for v in CONTEXT_SIZE_OPTIONS}
        cloud_ctx_opts = {v: CLOUD_CONTEXT_SIZE_LABELS.get(v, str(v))
                         for v in CLOUD_CONTEXT_SIZE_OPTIONS}

        def _fmt_ctx(val):
            if val and val >= 1_000_000:
                return f"{val // 1_000_000}M"
            if val and val >= 1_000:
                return f"{val // 1_000}K"
            return "?"

        def _model_source_label(model_id: str) -> str:
            runtime = model_id_from_choice_value(model_id)
            if _provider_unavailable_warning("Brain", model_id):
                return "Disconnected"
            if is_cloud_model(model_id):
                return "Provider"
            if _is_local_runtime(runtime):
                return "Local"
            return "Missing"

        def _model_source_color(model_id: str) -> str:
            runtime = model_id_from_choice_value(model_id)
            if _provider_unavailable_warning("Brain", model_id):
                return "orange"
            if is_cloud_model(model_id):
                return "blue-grey"
            if _is_local_runtime(runtime):
                return "green"
            return "orange"

        def _agent_status_for(model_id: str) -> dict[str, Any] | None:
            try:
                from row_bot.providers.resolution import resolve_provider_config

                resolved = resolve_provider_config(model_id, allow_legacy_local=True)
            except Exception:
                return None
            if resolved.provider_id == "ollama":
                return {
                    "configured": _ollama_up,
                    "source": "local_daemon" if _ollama_up else "not_running",
                }
            return None

        def _agent_context_override_for(model_id: str) -> int | None:
            policy = _policy_ref[0]
            if policy is None:
                return None
            try:
                if model_choice_value(policy.model_ref) != model_choice_value(model_id):
                    return None
            except Exception:
                return None
            return int(policy.effective_context or 0) or None

        def _provider_unavailable_warning(surface: str, selection: str | None) -> str:
            selected = model_choice_value(selection or "")
            if not selected or not is_cloud_model(selected):
                return ""
            try:
                from row_bot.providers.runtime import provider_status
                from row_bot.providers.selection import parse_model_ref

                parsed = parse_model_ref(selected)
                if not parsed:
                    return ""
                provider_id, _ = parsed
                status = provider_status(provider_id, refresh_tokens=False)
            except Exception:
                return f"Current {surface} default is unavailable. Connect the provider or choose another model."
            if bool(status.get("runtime_enabled")):
                return ""
            if provider_id == "xai_oauth" and surface == "Brain":
                return "Current Brain default is unavailable because xAI Grok is disconnected. Connect the provider or choose another model."
            return f"Current {surface} default is unavailable because its provider is disconnected. Connect the provider or choose another model."

        brain_unavailable_warning = _provider_unavailable_warning("Brain", current_value)
        brain_select_state = _safe_model_select_state(
            "Brain",
            current_value,
            model_opts,
            warning=brain_unavailable_warning,
            force_unavailable=bool(brain_unavailable_warning),
        )
        model_opts = dict(brain_select_state["options"])

        brain_readiness_slot_ref = [None]

        def _render_brain_readiness_badge(model_id: str | None = None) -> None:
            slot = brain_readiness_slot_ref[0]
            if slot is None:
                return
            selected_model = model_id or state.current_model
            badge = _agent_mode_badge_state(
                selected_model,
                status=_agent_status_for(selected_model),
                context_window_override=_agent_context_override_for(selected_model),
            )
            slot.clear()
            if not badge.get("visible"):
                return
            with slot:
                ui.badge(str(badge.get("label") or "agent blocked"), color=str(badge.get("color") or "orange")).props(
                    "outline dense"
                ).tooltip(str(badge.get("tooltip") or "Agent Mode readiness requirements were not met."))

        def _surface_row(icon: str, title: str, subtitle: str):
            with ui.column().classes("w-full gap-1 q-pa-sm rounded-borders").style(
                "border: 1px solid rgba(148, 163, 184, 0.16); "
                "background: rgba(15, 23, 42, 0.10);"
            ):
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    ui.icon(icon, size="sm").classes("text-primary")
                    with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                        ui.label(title).classes("text-sm text-weight-medium")
                        ui.label(subtitle).classes("text-grey-6 text-xs")
                    header_actions = ui.row().classes("items-center gap-1 no-wrap")
                controls = ui.row().classes("items-end gap-2 w-full")
            return header_actions, controls

        def _on_cloud_ctx_change(e):
            if str(e.value or "").lower() == "auto":
                clear_cloud_context_override()
                clear_agent_cache()
                _policy_ref[0] = _load_context_policy(state.current_model)
                _update_ctx_note()
                _render_brain_readiness_badge(state.current_model)
                return
            value = _coerce_context_size(e.value, get_cloud_context_size(), allowed=CLOUD_CONTEXT_SIZE_OPTIONS)
            set_cloud_context_size(value)
            clear_agent_cache()
            _policy_ref[0] = _load_context_policy(state.current_model)
            _update_ctx_note()
            _render_brain_readiness_badge(state.current_model)

        def _on_ctx_change(e):
            if str(e.value or "").lower() == "auto":
                set_context_size_auto()
                state.context_size = 32_768
                clear_agent_cache()
                _policy_ref[0] = _load_context_policy(state.current_model)
                _update_ctx_note()
                _render_brain_readiness_badge(state.current_model)
                return
            value = _coerce_context_size(e.value, state.context_size, allowed=CONTEXT_SIZE_OPTIONS)
            set_context_size(value)
            state.context_size = value
            clear_agent_cache()
            _policy_ref[0] = _load_context_policy(state.current_model)
            _update_ctx_note()
            policy = _policy_ref[0]
            if policy is not None and policy.native_max is not None and value > policy.native_max:
                max_lbl = CONTEXT_SIZE_LABELS.get(policy.native_max, f"{policy.native_max:,}")
                usr_lbl = CONTEXT_SIZE_LABELS.get(value, f"{value:,}")
                ui.notify(
                    f"Context capped: model max is {max_lbl} (you selected {usr_lbl}).",
                    type="warning", close_button=True, timeout=8000,
                )
            _render_brain_readiness_badge(state.current_model)

        vsvc = state.vision_service
        vision_value = model_choice_value(vsvc.model)
        from row_bot.vision import vision_model_compatibility
        vision_compat = vision_model_compatibility(vsvc.model)
        vision_invalid_reason = "" if vision_compat.get("usable") else str(vision_compat.get("reason") or "Selected model is not Vision-capable.")

        def _vision_label(m, local_override=None):
            runtime = model_id_from_choice_value(m)
            if is_cloud_model(m):
                return f"{get_provider_emoji(m)}  {m}"
            if _is_local_runtime(runtime, local_override):
                return f"✅  {runtime}"
            return f"⚠️  {runtime}"

        vision_opts = _model_options_map(vision_options)
        vision_warning = (
            vision_invalid_reason
            or _provider_unavailable_warning("Vision", vision_value)
            or (
                "Current Vision default is unavailable or not Vision-capable."
                if vision_value and vision_value not in vision_opts
                else ""
            )
        )
        vision_select_state = _safe_model_select_state(
            "Vision",
            vision_value,
            vision_opts,
            warning=vision_warning,
            force_unavailable=bool(vision_warning),
        )
        vision_opts = dict(vision_select_state["options"])
        vision_select_value = vision_select_state["value"]

        def _has_pinned_picker_choice(options: list[dict]) -> bool:
            return any(str(option.get("source") or "") != "included_value" for option in options)

        from row_bot.tools.image_gen_tool import DEFAULT_MODEL
        from row_bot.providers.selection import seed_configured_media_quick_choices
        _ig_tool = tool_registry.get_tool("image_gen")
        _ig_enabled = tool_registry.is_enabled("image_gen") if _ig_tool else False
        _ig_model = str(snapshot.get("image_model") or (_ig_tool.get_config("model", DEFAULT_MODEL) if _ig_tool else DEFAULT_MODEL))
        image_select_ref = [None]
        image_empty_ref = [None]
        image_missing_ref = [None]

        def _set_image_model(value: str) -> None:
            if _ig_tool and value:
                _ig_tool.set_config("model", value)
                seed_configured_media_quick_choices()

        _ig_valid_model_opts = _model_options_map(dict(snapshot.get("image_options") or {}))
        image_warning = (
            "Current Image default is unavailable. Connect the provider, refresh the catalog, or choose another pinned image model."
            if _ig_model and _ig_model not in _ig_valid_model_opts
            else ""
        )
        image_select_state = _safe_model_select_state("Image", _ig_model, _ig_valid_model_opts, warning=image_warning)
        _ig_model_opts = dict(image_select_state["options"])

        from row_bot.tools.video_gen_tool import DEFAULT_MODEL as _VG_DEFAULT
        _vg_tool = tool_registry.get_tool("video_gen")
        _vg_enabled = tool_registry.is_enabled("video_gen") if _vg_tool else False
        _vg_model = str(snapshot.get("video_model") or (_vg_tool.get_config("model", _VG_DEFAULT) if _vg_tool else _VG_DEFAULT))
        video_select_ref = [None]
        video_empty_ref = [None]
        video_missing_ref = [None]

        def _set_video_model(value: str) -> None:
            if _vg_tool and value:
                _vg_tool.set_config("model", value)
                seed_configured_media_quick_choices()

        _vg_valid_model_opts = _model_options_map(dict(snapshot.get("video_options") or {}))
        video_warning = (
            "Current Video default is unavailable. Connect the provider, refresh the catalog, or choose another pinned video model."
            if _vg_model and _vg_model not in _vg_valid_model_opts
            else ""
        )
        video_select_state = _safe_model_select_state("Video", _vg_model, _vg_valid_model_opts, warning=video_warning)
        _vg_model_opts = dict(video_select_state["options"])

        ui.label("Models").classes("text-h6 q-mb-xs")
        with ui.row().classes("items-center justify-between w-full q-mb-sm"):
            ui.label("Defaults and pinned picker choices").classes("text-grey-6 text-sm")
            with ui.row().classes("items-center gap-1"):
                ui.button(icon="refresh", on_click=lambda: _reopen("Models")).props("flat dense round size=sm").tooltip("Refresh model settings")
                ui.button(icon="hub", on_click=lambda: _reopen("Providers")).props("flat dense round size=sm").tooltip("Provider connections")

        with ui.column().classes("w-full gap-2 q-pa-sm rounded-borders q-mb-md").style(
            "border: 1px solid rgba(148, 163, 184, 0.22); "
            "background: rgba(148, 163, 184, 0.045);"
        ):
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("tune", size="sm")
                    ui.label("Defaults").classes("text-subtitle2")
                ui.badge("Catalog-backed", color="blue-grey").props("outline dense")
            ui.label("Pickers show pinned catalog choices plus the current default. Pin models in the catalog below before looking for them here.").classes("text-grey-6 text-xs")

            brain_actions, brain_controls = _surface_row("psychology", "Brain", "Conversation, tool use, memory, and workflows")
            with brain_actions:
                brain_source_badge = ui.badge(_model_source_label(current), color=_model_source_color(current)).props("outline dense")
                brain_readiness_slot_ref[0] = ui.row().classes("items-center gap-1 no-wrap")
                _render_brain_readiness_badge(current)
            with brain_controls:
                model_select = ui.select(
                    label="Default model",
                    options=model_opts,
                    value=brain_select_state["value"],
                ).classes("col-grow").props('use-input input-debounce=300 dense outlined')
                brain_refresh_btn = ui.button(icon="refresh", on_click=lambda: _reopen("Models")).props("flat dense round size=sm color=primary").tooltip(f"Refresh after managing Ollama models outside {APP_DISPLAY_NAME}")
                brain_empty = ui.label("No pinned Brain choices yet. Pin Chat models in the catalog below.").classes("text-grey-6 text-xs q-pb-sm")
                brain_empty.visible = not _has_pinned_picker_choice(chat_options)
                brain_missing = ui.label(str(brain_select_state.get("warning") or "")).classes("text-warning text-xs q-pb-sm")
                brain_missing.visible = bool(brain_select_state.get("warning"))
            ctx_note = ui.label("").classes("text-xs text-grey-6 q-ml-lg")
            ctx_note.visible = False
            with ui.expansion("Advanced context", icon="tune").classes("w-full"):
                ui.label(
                    "Auto uses the provider/model limit when known. If Row-Bot cannot determine it, "
                    "it uses Row-Bot's disclosed 128K application fallback. Set an Advanced override "
                    "after verifying the model's actual limit."
                ).classes("text-grey-6 text-xs q-mb-sm")
                cloud_advanced_options = {"auto": "Auto (recommended)", **cloud_ctx_opts}
                cloud_ctx_select = ui.select(
                    label="Cloud Context",
                    options=cloud_advanced_options,
                    value=get_cloud_context_override() or "auto",
                    on_change=_on_cloud_ctx_change,
                ).classes("min-w-[220px]").props("dense outlined").tooltip(
                    "Optional verified cap. Auto prefers provider metadata and otherwise uses the 128K fallback."
                )
                cloud_ctx_select.visible = _is_cloud_ctx
                local_advanced_options = {"auto": "Auto (requested 32K)", **ctx_opts}
                ctx_select = ui.select(
                    label="Local model context",
                    options=local_advanced_options,
                    value="auto" if get_local_context_mode() == "auto" else state.context_size,
                    on_change=_on_ctx_change,
                ).classes("min-w-[220px]").props("dense outlined").tooltip(
                    "Auto requests 32K and is capped by the native or observed Ollama allocation."
                )
                ctx_select.visible = not _is_cloud_ctx

            vision_actions, vision_controls = _surface_row("visibility", "Vision", "Camera and screen capture analysis")
            with vision_actions:
                ui.switch("Enabled", value=vsvc.enabled,
                    on_change=lambda e: setattr(vsvc, "enabled", e.value)
                ).props("dense")
            with vision_controls:
                vision_select = ui.select(label="Vision model", options=vision_opts, value=vision_select_value).classes("col-grow").props('use-input input-debounce=300 dense outlined')
                camera_controls = ui.row().classes("items-end gap-2")
                with camera_controls:
                    camera_select = ui.select(
                        label="Camera",
                        options={vsvc.camera_index: f"Camera {vsvc.camera_index}"},
                        value=vsvc.camera_index,
                        on_change=lambda e: setattr(vsvc, "camera_index", e.value),
                    ).classes("min-w-[150px]").props("dense outlined")
                    camera_status = ui.label("Camera list not loaded").classes("text-grey-6 text-xs q-pb-sm")
                    refresh_cameras_btn = ui.button(icon="refresh").props("flat dense round size=sm").tooltip("Refresh camera list")

                async def _refresh_cameras() -> None:
                    from row_bot.vision import list_cameras

                    refresh_cameras_btn.disable()
                    camera_status.text = "Checking cameras..."
                    camera_status.update()
                    try:
                        cameras = await run.io_bound(list_cameras)
                    except Exception as exc:
                        logger.warning("Camera list refresh failed: %s", exc, exc_info=True)
                        cameras = []
                    if cameras:
                        camera_select.options = {i: f"Camera {i}" for i in cameras}
                        if vsvc.camera_index not in cameras:
                            camera_select.value = cameras[0]
                            vsvc.camera_index = cameras[0]
                        camera_status.text = f"{len(cameras)} camera(s) detected"
                    else:
                        camera_select.options = {vsvc.camera_index: f"Camera {vsvc.camera_index}"}
                        camera_select.value = vsvc.camera_index
                        camera_status.text = "No cameras detected"
                    camera_select.update()
                    camera_status.update()
                    refresh_cameras_btn.enable()

                refresh_cameras_btn.on_click(_refresh_cameras)
                vision_refresh_btn = ui.button(icon="refresh", on_click=lambda: _reopen("Models")).props("flat dense round size=sm color=primary").tooltip(f"Refresh after managing Ollama models outside {APP_DISPLAY_NAME}")
                vision_empty = ui.label("No pinned Vision choices yet. Pin Vision models in the catalog below.").classes("text-grey-6 text-xs q-pb-sm")
                vision_empty.visible = not _has_pinned_picker_choice(vision_options)
                vision_missing = ui.label(
                    str(vision_select_state.get("warning") or "")
                    or "Current local Vision model is not available. Manage local models in Ollama, then refresh or pin another Vision model below."
                ).classes("text-warning text-xs q-pb-sm")
                vision_missing.visible = bool(vision_select_state.get("warning")) or (
                    bool(vsvc.model) and not is_cloud_model(vsvc.model) and not _is_local_selection(vsvc.model)
                )

            image_actions, image_controls = _surface_row("palette", "Image", "Image generation and editing")
            with image_actions:
                ui.switch("Enabled", value=_ig_enabled,
                    on_change=lambda e: tool_registry.set_enabled("image_gen", e.value),
                ).props("dense")
            with image_controls:
                image_select = ui.select(
                    label="Image model",
                    options=_ig_model_opts,
                    value=image_select_state["value"],
                    on_change=lambda e: _set_image_model(e.value),
                ).classes("w-full").props("dense outlined")
                image_select_ref[0] = image_select
                if not _ig_model_opts and not image_select_state["value"]:
                    image_select.disable()
                image_empty = ui.label("No pinned image models. Pin one in the catalog below.").classes("text-grey-6 text-xs q-pb-sm")
                image_empty.visible = not bool(_ig_valid_model_opts)
                image_empty_ref[0] = image_empty
                image_missing = ui.label(str(image_select_state.get("warning") or "")).classes("text-warning text-xs q-pb-sm")
                image_missing.visible = bool(image_select_state.get("warning"))
                image_missing_ref[0] = image_missing

            video_actions, video_controls = _surface_row("movie", "Video", "Video generation and image animation")
            with video_actions:
                ui.switch("Enabled", value=_vg_enabled,
                    on_change=lambda e: tool_registry.set_enabled("video_gen", e.value),
                ).props("dense")
            with video_controls:
                video_select = ui.select(
                    label="Video model",
                    options=_vg_model_opts,
                    value=video_select_state["value"],
                    on_change=lambda e: _set_video_model(e.value),
                ).classes("w-full").props("dense outlined")
                video_select_ref[0] = video_select
                if not _vg_model_opts and not video_select_state["value"]:
                    video_select.disable()
                video_empty = ui.label("No pinned video models. Pin one in the catalog below.").classes("text-grey-6 text-xs q-pb-sm")
                video_empty.visible = not bool(_vg_valid_model_opts)
                video_empty_ref[0] = video_empty
                video_missing = ui.label(str(video_select_state.get("warning") or "")).classes("text-warning text-xs q-pb-sm")
                video_missing.visible = bool(video_select_state.get("warning"))
                video_missing_ref[0] = video_missing

        import sys as _sys
        if _sys.platform == "win32":
            _ollama_install_steps = (
                "1. Download Ollama from ollama.com/download\n"
                "2. Run the installer\n"
                "3. Ollama starts automatically — re-open Settings → Models"
            )
        elif _sys.platform == "darwin":
            _ollama_install_steps = (
                "1. Download Ollama from ollama.com/download (or: brew install ollama)\n"
                "2. Run: ollama serve\n"
                "3. Re-open Settings → Models"
            )
        else:
            _ollama_install_steps = (
                "1. Install: curl -fsSL https://ollama.com/install.sh | sh\n"
                "2. Run: ollama serve\n"
                "3. Re-open Settings → Models"
            )
        with ui.card().classes("w-full q-pa-md bg-amber-1") as ollama_guide:
            ui.label("🖥️ Want to use local models?").classes("text-weight-bold text-body1 text-brown-9")
            ui.label(
                "Local models run on your GPU with full privacy — no data leaves your machine. "
                "You need Ollama installed and running."
            ).classes("text-grey-8 text-sm q-mb-xs")
            ui.label(_ollama_install_steps).classes("text-grey-8 text-xs").style("white-space: pre-line")
            ui.link("Download Ollama →", "https://ollama.com/download", new_tab=True).classes("text-sm text-weight-bold")
        def _model_needs_ollama(value: object) -> bool:
            selected = str(value or "")
            if selected.startswith("model:"):
                parts = selected.split(":", 2)
                if len(parts) == 3 and parts[1] != "ollama":
                    return False
            runtime_model = model_id_from_choice_value(selected)
            return bool(selected and not is_cloud_model(selected) and not _is_local_runtime(runtime_model))

        ollama_guide.visible = (not _ollama_up) and _model_needs_ollama(model_select.value)

        _ctx_note_updater = [None]

        def _sync_models_tab_current_model(model_id: str | None = None) -> None:
            current_model = model_id or get_current_model()
            state.current_model = current_model
            current_model_value = model_choice_value(current_model)
            if current_model_value not in model_select.options:
                updated_options = dict(model_select.options)
                updated_options.update(model_choice_options_map("chat", include_values=[current_model]))
                model_select.options = updated_options
            model_select.value = current_model_value
            model_select.update()
            brain_source_badge.text = _model_source_label(current_model)
            brain_source_badge.update()
            _render_brain_readiness_badge(current_model)
            ollama_guide.visible = (not _ollama_up) and _model_needs_ollama(model_choice_value(current_model))
            if _ctx_note_updater[0]:
                _ctx_note_updater[0]()

        _model_tab_sync["callback"] = _sync_models_tab_current_model

        async def _on_model_change(e):
            sel = e.value
            if sel == model_choice_value(state.current_model):
                return
            prev = state.current_model
            brain_source_badge.text = _model_source_label(sel)
            brain_source_badge.update()
            _render_brain_readiness_badge(sel)
            ollama_guide.visible = (not _ollama_up) and _model_needs_ollama(sel)
            runtime_model = model_id_from_choice_value(sel)
            if not is_cloud_model(sel) and not _is_local_runtime(runtime_model):
                ui.notify(
                    f"{runtime_model} is not exposed by the Ollama daemon. Manage local models in Ollama, then refresh.",
                    type="warning", close_button=True, timeout=9000,
                )
                model_select.value = model_choice_value(prev)
                model_select.update()
                _render_brain_readiness_badge(prev)
                return
            try:
                from row_bot.providers.readiness import evaluate_runtime_readiness

                runtime_readiness = await run.io_bound(lambda: evaluate_runtime_readiness(sel))
            except Exception as exc:
                ui.notify(
                    f"Could not evaluate {runtime_model}: {exc}. Reverting to {prev}.",
                    type="negative", close_button=True, timeout=10000,
                )
                model_select.value = model_choice_value(prev)
                model_select.update()
                _render_brain_readiness_badge(prev)
                return
            if runtime_readiness.selected_mode == "blocked":
                context_blocked = any(
                    "context window could not be determined" in str(error).lower()
                    for result in (runtime_readiness.agent, runtime_readiness.chat)
                    for error in result.errors
                )
                blocked_reason = (
                    runtime_readiness.chat.user_message()
                    if context_blocked
                    else runtime_readiness.selection_reason
                )
                ui.notify(
                    f"{runtime_model} is unavailable: {blocked_reason} Reverting to {prev}.",
                    type="negative", close_button=True, timeout=10000,
                )
                model_select.value = model_choice_value(prev)
                model_select.update()
                _render_brain_readiness_badge(prev)
                return
            set_model(sel)
            state.current_model = sel
            clear_agent_cache()
            if runtime_readiness.selected_mode == "chat_only":
                ui.notify(
                    f"{runtime_model} set as Chat Only. Tools, workflows, Developer, and Designer require an Agent-ready model.",
                    type="info", close_button=True, timeout=9000,
                )
            try:
                policy = await run.io_bound(lambda: _load_context_policy(sel))
            except Exception:
                logger.debug("Could not refresh context policy for %s", sel, exc_info=True)
                policy = None
            _policy_ref[0] = policy
            _render_brain_readiness_badge(sel)
            if policy is not None and policy.native_max is not None and policy.user_cap > policy.native_max:
                max_lbl = CONTEXT_SIZE_LABELS.get(policy.native_max, f"{policy.native_max:,}")
                usr_lbl = CONTEXT_SIZE_LABELS.get(policy.user_cap, f"{policy.user_cap:,}")
                ui.notify(
                    f"Context capped: {runtime_model} max is {max_lbl} (you selected {usr_lbl}).",
                    type="warning", close_button=True, timeout=8000,
                )
            if _ctx_note_updater[0]:
                _ctx_note_updater[0]()

        model_select.on_value_change(_on_model_change)

        def _update_ctx_note():
            policy = _policy_ref[0]
            if policy is None:
                cloud_ctx_select.visible = False
                ctx_select.visible = True
                ctx_note.visible = False
                return
            provider_policy = policy.policy_kind == "provider"
            cloud_ctx_select.visible = provider_policy
            ctx_select.visible = not provider_policy
            if provider_policy:
                presentation = context_policy_presentation(policy)
                ctx_note.text = str(presentation.get("settings_note") or "")
                ctx_note.classes(
                    remove="text-grey-6 text-warning",
                    add="text-warning" if presentation.get("warning") else "text-grey-6",
                )
                ctx_note.visible = True
            elif policy.native_max is not None and policy.user_cap > policy.native_max:
                max_label = CONTEXT_SIZE_LABELS.get(policy.native_max, f"{policy.native_max:,}")
                ctx_note.text = f"Model max {max_label}; trimming applies"
                ctx_note.visible = True
            else:
                ctx_note.visible = False

        _update_ctx_note()
        _ctx_note_updater[0] = _update_ctx_note

        async def _on_vision_change(e):
            sel = e.value
            is_cloud = is_cloud_model(sel)
            if sel != model_choice_value(vsvc.model):
                if is_cloud:
                    vsvc.model = sel
                    clear_agent_cache()
                    return
                runtime_model = model_id_from_choice_value(sel)
                if not _is_local_runtime(runtime_model):
                    ui.notify(
                        f"{runtime_model} is not exposed by the Ollama daemon. Manage local models in Ollama, then refresh.",
                        type="warning", close_button=True, timeout=9000,
                    )
                    vision_select.value = model_choice_value(vsvc.model)
                    vision_select.update()
                    return
                vsvc.model = sel
                clear_agent_cache()

        vision_select.on_value_change(_on_vision_change)

        from row_bot.ui.model_catalog import build_lazy_model_catalog_section

        _render_agent_runtime_settings()
        catalog_container = ui.column().classes("w-full gap-2 q-mt-md")

        def _collect_top_picker_options() -> dict:
            current_chat = state.current_model
            try:
                refreshed_chat_options = list_model_choice_options("chat", include_values=[current_chat])
            except Exception:
                logger.debug("Could not refresh Brain picker options", exc_info=True)
                refreshed_chat_options = []
            current_vision = vsvc.model
            try:
                refreshed_vision_options = list_model_choice_options("vision", include_values=[current_vision])
            except Exception:
                logger.debug("Could not refresh Vision picker options", exc_info=True)
                refreshed_vision_options = []
            try:
                quick_choices = list_quick_choices("", include_inactive=True)
            except Exception:
                logger.debug("Could not refresh picker quick choices", exc_info=True)
                quick_choices = []

            image_options = {}
            current_image = DEFAULT_MODEL
            try:
                from row_bot.tools.image_gen_tool import get_available_image_models

                available_image = get_available_image_models()
                current_image = _ig_tool.get_config("model", DEFAULT_MODEL) if _ig_tool else DEFAULT_MODEL
                image_options = _pinned_media_options("image", available_image, current_image, quick_choices)
            except Exception:
                logger.debug("Could not refresh image picker options", exc_info=True)

            video_options = {}
            current_video = _VG_DEFAULT
            try:
                from row_bot.tools.video_gen_tool import get_available_video_models

                available_video = get_available_video_models()
                current_video = _vg_tool.get_config("model", _VG_DEFAULT) if _vg_tool else _VG_DEFAULT
                video_options = _pinned_media_options("video", available_video, current_video, quick_choices)
            except Exception:
                logger.debug("Could not refresh video picker options", exc_info=True)

            return {
                "current_chat": current_chat,
                "chat_options": refreshed_chat_options,
                "current_vision": current_vision,
                "vision_options": refreshed_vision_options,
                "current_image": current_image,
                "image_options": image_options,
                "current_video": current_video,
                "video_options": video_options,
            }

        async def _refresh_top_picker_options() -> None:
            try:
                data = await run.io_bound(_collect_top_picker_options)
            except Exception as exc:
                logger.warning("Could not refresh model picker options", exc_info=True)
                ui.notify(f"Could not refresh picker options: {exc}", type="negative")
                return

            current_chat = str(data.get("current_chat") or state.current_model)
            refreshed_chat_options = list(data.get("chat_options") or [])
            current_chat_value = model_choice_value(current_chat)
            refreshed_chat_map = _model_options_map(refreshed_chat_options)
            refreshed_brain_warning = (
                _provider_unavailable_warning("Brain", current_chat_value)
                or (
                    "Current Brain default is unavailable. Connect the provider or choose another model."
                    if current_chat_value and current_chat_value not in refreshed_chat_map
                    else ""
                )
            )
            refreshed_brain_state = _safe_model_select_state(
                "Brain",
                current_chat_value,
                refreshed_chat_map,
                warning=refreshed_brain_warning,
                force_unavailable=bool(refreshed_brain_warning),
            )
            model_select.options = dict(refreshed_brain_state["options"])
            model_select.value = refreshed_brain_state["value"]
            model_select.update()
            brain_source_badge.text = _model_source_label(current_chat_value)
            brain_source_badge.update()
            brain_empty.visible = not _has_pinned_picker_choice(refreshed_chat_options)
            brain_empty.update()
            brain_missing.text = str(refreshed_brain_state.get("warning") or "")
            brain_missing.visible = bool(refreshed_brain_state.get("warning"))
            brain_missing.update()

            current_vision = str(data.get("current_vision") or vsvc.model)
            refreshed_vision_options = list(data.get("vision_options") or [])
            current_vision_value = model_choice_value(current_vision)
            refreshed_vision_map = _model_options_map(refreshed_vision_options)
            current_compat = vision_model_compatibility(current_vision)
            invalid_reason = "" if current_compat.get("usable") else str(current_compat.get("reason") or "Selected model is not Vision-capable.")
            refreshed_vision_warning = (
                invalid_reason
                or _provider_unavailable_warning("Vision", current_vision_value)
                or (
                    "Current Vision default is unavailable or not Vision-capable."
                    if current_vision_value and current_vision_value not in refreshed_vision_map
                    else ""
                )
            )
            refreshed_vision_state = _safe_model_select_state(
                "Vision",
                current_vision_value,
                refreshed_vision_map,
                warning=refreshed_vision_warning,
                force_unavailable=bool(refreshed_vision_warning),
            )
            vision_select.options = dict(refreshed_vision_state["options"])
            vision_select.value = refreshed_vision_state["value"]
            vision_select.update()
            vision_empty.visible = not _has_pinned_picker_choice(refreshed_vision_options)
            vision_empty.update()
            vision_missing.text = (
                str(refreshed_vision_state.get("warning") or "")
                or "Current local Vision model is not available. Manage local models in Ollama, then refresh or pin another Vision model below."
            )
            vision_missing.visible = bool(refreshed_vision_state.get("warning")) or (
                bool(current_vision) and not is_cloud_model(current_vision) and not _is_local_selection(current_vision)
            )
            vision_missing.update()

            image_select = image_select_ref[0]
            if image_select is not None:
                current_image = str(data.get("current_image") or DEFAULT_MODEL)
                image_options = _model_options_map(dict(data.get("image_options") or {}))
                refreshed_image_warning = (
                    "Current Image default is unavailable. Connect the provider, refresh the catalog, or choose another pinned image model."
                    if current_image and current_image not in image_options
                    else ""
                )
                refreshed_image_state = _safe_model_select_state(
                    "Image",
                    current_image,
                    image_options,
                    warning=refreshed_image_warning,
                )
                image_select.options = dict(refreshed_image_state["options"])
                image_select.value = refreshed_image_state["value"]
                if image_select.options or image_select.value:
                    image_select.enable()
                else:
                    image_select.disable()
                image_empty = image_empty_ref[0]
                if image_empty is not None:
                    image_empty.visible = not bool(image_options)
                    image_empty.update()
                image_missing = image_missing_ref[0]
                if image_missing is not None:
                    image_missing.text = str(refreshed_image_state.get("warning") or "")
                    image_missing.visible = bool(refreshed_image_state.get("warning"))
                    image_missing.update()
                image_select.update()
            video_select = video_select_ref[0]
            if video_select is not None:
                current_video = str(data.get("current_video") or _VG_DEFAULT)
                video_options = _model_options_map(dict(data.get("video_options") or {}))
                refreshed_video_warning = (
                    "Current Video default is unavailable. Connect the provider, refresh the catalog, or choose another pinned video model."
                    if current_video and current_video not in video_options
                    else ""
                )
                refreshed_video_state = _safe_model_select_state(
                    "Video",
                    current_video,
                    video_options,
                    warning=refreshed_video_warning,
                )
                video_select.options = dict(refreshed_video_state["options"])
                video_select.value = refreshed_video_state["value"]
                if video_select.options or video_select.value:
                    video_select.enable()
                else:
                    video_select.disable()
                video_empty = video_empty_ref[0]
                if video_empty is not None:
                    video_empty.visible = not bool(video_options)
                    video_empty.update()
                video_missing = video_missing_ref[0]
                if video_missing is not None:
                    video_missing.text = str(refreshed_video_state.get("warning") or "")
                    video_missing.visible = bool(refreshed_video_state.get("warning"))
                    video_missing.update()
                video_select.update()

        def _set_catalog_default(surface: str, row) -> None:
            if surface == "chat":
                value = model_choice_value(row.selection_ref)
                set_model(value)
                state.current_model = value
                clear_agent_cache()
                _sync_models_tab_current_model(value)
                ui.notify(f"Default Brain model set to {row.display_name}", type="positive")
            elif surface == "vision":
                value = model_choice_value(row.selection_ref)
                vsvc.model = value
                if value not in vision_select.options:
                    included = model_choice_options_map("vision", include_values=[value])
                    if value not in included:
                        ui.notify("That model is not currently marked as Vision-capable.", type="warning")
                        return
                    vision_select.options = {**dict(vision_select.options), **included}
                vision_select.value = value
                vision_select.update()
                clear_agent_cache()
                ui.notify(f"Default Vision model set to {row.display_name}", type="positive")
            elif surface == "image":
                value = f"{row.provider_id}/{row.model_id}"
                _set_image_model(value)
                image_select = image_select_ref[0]
                if image_select is not None:
                    if value not in image_select.options:
                        image_select.options = {**dict(image_select.options), value: row.display_name}
                    image_select.value = value
                    image_select.enable()
                    image_empty = image_empty_ref[0]
                    if image_empty is not None:
                        image_empty.visible = False
                        image_empty.update()
                    image_select.update()
                ui.notify(f"Default Image model set to {row.display_name}", type="positive")
            elif surface == "video":
                value = f"{row.provider_id}/{row.model_id}"
                _set_video_model(value)
                video_select = video_select_ref[0]
                if video_select is not None:
                    if value not in video_select.options:
                        video_select.options = {**dict(video_select.options), value: row.display_name}
                    video_select.value = value
                    video_select.enable()
                    video_empty = video_empty_ref[0]
                    if video_empty is not None:
                        video_empty.visible = False
                        video_empty.update()
                    video_select.update()
                ui.notify(f"Default Video model set to {row.display_name}", type="positive")

        def _catalog_defaults() -> dict[str, str]:
            image_model = ""
            video_model = ""
            try:
                from row_bot.tools.image_gen_tool import DEFAULT_MODEL as _IMAGE_DEFAULT
                image_tool = tool_registry.get_tool("image_gen")
                image_model = image_tool.get_config("model", _IMAGE_DEFAULT) if image_tool else _IMAGE_DEFAULT
            except Exception:
                image_model = ""
            try:
                from row_bot.tools.video_gen_tool import DEFAULT_MODEL as _VIDEO_DEFAULT
                video_tool = tool_registry.get_tool("video_gen")
                video_model = video_tool.get_config("model", _VIDEO_DEFAULT) if video_tool else _VIDEO_DEFAULT
            except Exception:
                video_model = ""
            return {
                "chat": get_current_model(),
                "vision": state.vision_service.model,
                "image": image_model,
                "video": video_model,
            }

        def _load_catalog_rows_from_cache():
            from row_bot.providers.model_catalog_cache import build_cached_model_catalog_rows
            from row_bot.providers.selection import list_quick_choices

            return build_cached_model_catalog_rows(
                defaults=_catalog_defaults(),
                quick_choices=list_quick_choices("", include_inactive=True),
            )

        async def _refresh_catalog_status_and_pickers() -> None:
            _render_catalog_status()
            await _refresh_top_picker_options()

        def _render_catalog_status() -> None:
            snapshot = read_model_catalog_cache()
            catalog_container.clear()
            with catalog_container:
                with ui.row().classes("items-center justify-between w-full q-mb-xs"):
                    with ui.column().classes("gap-0"):
                        ui.label("Catalog").classes("text-subtitle2")
                        ui.label("Open only when you need to browse or pin models. The cached catalog stays off the initial Settings render path.").classes("text-grey-6 text-xs")
                    with ui.row().classes("items-center gap-2"):
                        if is_model_catalog_refresh_running():
                            with ui.row().classes("items-center gap-1 text-grey-6 text-xs"):
                                ui.spinner(size="xs")
                                ui.label("Refreshing")
                        elif snapshot.warnings:
                            ui.badge("provider warnings", color="orange").props("outline dense").tooltip("\n".join(snapshot.warnings[:5]))
                        elif snapshot.is_stale:
                            ui.badge("stale", color="orange").props("outline dense")
                        else:
                            ui.badge("fresh", color="positive").props("outline dense")
                        ui.label(cache_age_label(snapshot)).classes("text-grey-6 text-xs")
                        ui.button(
                            "Refresh catalog",
                            icon="refresh",
                            on_click=lambda: _start_catalog_refresh_ui(
                                reason="manual",
                                force=True,
                                on_done=_refresh_catalog_status_and_pickers,
                            ),
                        ).props("flat dense color=primary no-caps")
                if snapshot.is_empty:
                    ui.label(f"Model catalog cache is warming up. You can keep using the selectors above while {APP_DISPLAY_NAME} refreshes in the background.").classes("text-grey-6 text-sm")
                build_lazy_model_catalog_section(
                    _load_catalog_rows_from_cache,
                    on_set_default=_set_catalog_default,
                    on_change=_refresh_top_picker_options,
                )

        _render_catalog_status()

    def _collect_models_tab_data() -> dict:
        from row_bot.providers.selection import list_model_choice_options, list_quick_choices

        started = time.perf_counter()
        quick_started = time.perf_counter()
        try:
            quick_choices = list_quick_choices("", include_inactive=True)
        except Exception:
            logger.debug("Could not collect model picker quick choices", exc_info=True)
            quick_choices = []
        quick_elapsed = time.perf_counter() - quick_started
        try:
            ollama_up = _ollama_reachable()
        except Exception:
            logger.debug("Could not check Ollama status for model settings", exc_info=True)
            ollama_up = False
        ollama_elapsed = time.perf_counter() - started
        local_started = time.perf_counter()
        try:
            local_models = list_local_models()
        except Exception:
            logger.debug("Could not collect local models for model settings", exc_info=True)
            local_models = []
        local_elapsed = time.perf_counter() - local_started
        current_model = get_current_model()
        vision_model = state.vision_service.model
        try:
            from row_bot.tools.image_gen_tool import DEFAULT_MODEL as _IMAGE_DEFAULT, get_available_image_models
            image_tool = tool_registry.get_tool("image_gen")
            image_model = image_tool.get_config("model", _IMAGE_DEFAULT) if image_tool else _IMAGE_DEFAULT
            available_image = get_available_image_models()
        except Exception:
            image_model = ""
            available_image = {}
        try:
            from row_bot.tools.video_gen_tool import DEFAULT_MODEL as _VIDEO_DEFAULT, get_available_video_models
            video_tool = tool_registry.get_tool("video_gen")
            video_model = video_tool.get_config("model", _VIDEO_DEFAULT) if video_tool else _VIDEO_DEFAULT
            available_video = get_available_video_models()
        except Exception:
            video_model = ""
            available_video = {}
        image_options = _pinned_media_options("image", available_image, image_model, quick_choices)
        video_options = _pinned_media_options("video", available_video, video_model, quick_choices)
        options_started = time.perf_counter()
        try:
            chat_options = list_model_choice_options("chat", include_values=[current_model])
        except Exception:
            logger.debug("Could not collect Brain model options", exc_info=True)
            chat_options = []
        try:
            vision_options = list_model_choice_options("vision", include_values=[vision_model])
        except Exception:
            logger.debug("Could not collect Vision model options", exc_info=True)
            vision_options = []
        options_elapsed = time.perf_counter() - options_started
        context_started = time.perf_counter()
        try:
            context_policy = get_context_policy(current_model)
        except Exception:
            logger.debug("Could not collect context policy for %s", current_model, exc_info=True)
            context_policy = None
        context_elapsed = time.perf_counter() - context_started
        total_elapsed = time.perf_counter() - started
        logger.info(
            "perf: models settings collect took %.3fs "
            "(quick=%.3fs ollama=%.3fs local=%.3fs options=%.3fs context=%.3fs local_count=%d chat_options=%d vision_options=%d)",
            total_elapsed,
            quick_elapsed,
            ollama_elapsed,
            local_elapsed,
            options_elapsed,
            context_elapsed,
            len(local_models),
            len(chat_options),
            len(vision_options),
        )
        log_performance_snapshot("models-settings-collected")
        return {
            "ollama_up": ollama_up,
            "trending": [],
            "local": local_models,
            "current_model": current_model,
            "chat_options": chat_options,
            "vision_options": vision_options,
            "image_model": image_model,
            "image_options": image_options,
            "video_model": video_model,
            "video_options": video_options,
            "context_policy": context_policy,
        }

    def _build_models_tab() -> None:
        load_started = time.perf_counter()
        container = ui.column().classes("w-full gap-4")

        with container:
            _settings_header(
                "Models",
                "Choose defaults, tune context, manage local models, and browse the cached model catalog.",
                "smart_toy",
            )
            with ui.row().classes("items-center gap-3 text-grey-6"):
                ui.spinner(size="sm")
                ui.label("Loading cached model settings...").classes("text-sm")
            _render_agent_runtime_settings()

        async def _load_models() -> None:
            try:
                data = await run.io_bound(_collect_models_tab_data)
                collected_elapsed = time.perf_counter() - load_started
                render_started = time.perf_counter()
                container.clear()
                with container:
                    _render_models_tab_content(data)
                logger.info(
                    "perf: models settings loaded collect=%.3fs render=%.3fs total=%.3fs",
                    collected_elapsed,
                    time.perf_counter() - render_started,
                    time.perf_counter() - load_started,
                )
                log_performance_snapshot("models-settings-rendered")
            except Exception as exc:
                logger.warning("Could not load model settings", exc_info=True)
                container.clear()
                with container:
                    _settings_header(
                        "Models",
                        "Choose defaults, tune context, manage local models, and browse the cached model catalog.",
                        "smart_toy",
                    )
                    ui.label(f"Could not load model settings: {exc}").classes("text-warning text-sm")
                    _render_agent_runtime_settings()
                    with ui.row().classes("items-center gap-2"):
                        ui.button(icon="hub", on_click=lambda: _reopen("Providers")).props("flat dense round size=sm").tooltip("Provider connections")
                        ui.button(
                            "Refresh catalog",
                            icon="refresh",
                            on_click=lambda: _start_catalog_refresh_ui(reason="models_error_recovery", force=True),
                        ).props("flat dense color=primary no-caps")
                    try:
                        from row_bot.tools.image_gen_tool import DEFAULT_MODEL as _IMAGE_DEFAULT
                        from row_bot.tools.video_gen_tool import DEFAULT_MODEL as _VIDEO_DEFAULT

                        image_tool = tool_registry.get_tool("image_gen")
                        video_tool = tool_registry.get_tool("video_gen")
                        defaults = {
                            "Brain": get_current_model(),
                            "Vision": state.vision_service.model,
                            "Image": image_tool.get_config("model", _IMAGE_DEFAULT) if image_tool else _IMAGE_DEFAULT,
                            "Video": video_tool.get_config("model", _VIDEO_DEFAULT) if video_tool else _VIDEO_DEFAULT,
                        }
                        with ui.column().classes("w-full gap-1"):
                            for label, value in defaults.items():
                                ui.label(f"{label}: {value or 'not set'}").classes("text-grey-6 text-xs")
                    except Exception:
                        logger.debug("Could not render model defaults in recovery view", exc_info=True)

        safe_ui_task(_load_models, context="models settings load")


    # ── Providers Tab ────────────────────────────────────────────────

    def _build_cloud_tab() -> None:
        from row_bot.ui.provider_settings import build_custom_endpoints_section, build_provider_summary_cards

        ui.label("Providers").classes("text-h6")
        ui.label(
            "Connect model providers, review credential sources, refresh catalogs, and check provider health. Model pinning and defaults live in the Models tab."
        ).classes("text-grey-6 text-sm")
        build_provider_summary_cards()

        ui.separator()
        build_custom_endpoints_section(on_change=lambda: _reopen("Providers"))


    # ── Skills Tab ───────────────────────────────────────────────────

    def _build_skills_tab() -> None:
        import row_bot.skills as skills_mod
        from row_bot.skills_activation import get_skill_telemetry

        def _open_hub_browser() -> None:
            try:
                from row_bot.skills_hub.ui import open_skills_hub_dialog

                open_skills_hub_dialog(on_change=lambda: _reopen("Skills"))
            except Exception as exc:
                logger.warning("Skills hub not available: %s", exc, exc_info=True)
                ui.notify("Skills hub is not available yet", type="warning")

        _settings_header(
            "Skills",
            "Manage skill availability, pinned defaults, and installed skills.",
            "auto_fix_high",
        )

        metrics_row = ui.row().classes("items-center gap-2 q-mb-sm")

        with _settings_section(
            "Skill library",
            "Pinned skills start active in new chats, tasks, Designer, and Developer.",
            icon="library_books",
        ):
            with ui.row().classes("w-full items-center gap-2 q-mb-xs flex-wrap"):
                search_input = ui.input(
                    placeholder="Search skills",
                ).props("dense outlined clearable").classes("col").style(
                    "min-width: 220px; max-width: 420px;"
                )
                filter_select = ui.select(
                    ["All", "Pinned", "Available", "Custom", "Public"],
                    value="All",
                    label="Filter",
                ).props("dense outlined").classes("w-36")
                sort_select = ui.select(
                    ["Name", "Recently used", "Token cost", "Source"],
                    value="Name",
                    label="Sort",
                ).props("dense outlined").classes("w-44")
                ui.space()
                ui.button(
                    "Browse Skills",
                    icon="travel_explore",
                    on_click=_open_hub_browser,
                ).props("flat dense no-caps")
                ui.button(
                    "Create Skill",
                    icon="add",
                    on_click=lambda: _open_skill_editor(),
                ).props("color=primary dense no-caps")

            skills_container = ui.column().classes("w-full gap-0")

        def _refresh_skills_list():
            from row_bot.skills_hub.provenance import load_records

            metrics_row.clear()
            skills_container.clear()
            all_skills = skills_mod.get_manual_skills()
            skill_telemetry = get_skill_telemetry()
            hub_records = load_records()
            enabled_count = sum(1 for sk in all_skills if skills_mod.is_enabled(sk.name))
            pinned_count = sum(1 for sk in all_skills if skills_mod.is_pinned(sk.name))
            custom_count = sum(1 for sk in all_skills if sk.source == "user")
            public_count = sum(1 for sk in all_skills if sk.name in hub_records)
            with metrics_row:
                _metric_chip("available", enabled_count, icon="toggle_on")
                _metric_chip("pinned", pinned_count, icon="push_pin")
                _metric_chip("custom", custom_count, icon="edit")
                _metric_chip("public", public_count, icon="public")
            if not all_skills:
                with skills_container:
                    ui.label("No skills found. Create one to get started!").classes("text-grey-5 italic")
                return

            query = str(search_input.value or "").strip().lower()
            filter_value = str(filter_select.value or "All")
            sort_value = str(sort_select.value or "Name")

            def _source_label(sk) -> str:
                if sk.source == "bundled":
                    return "Bundled"
                if hub_records.get(sk.name):
                    return "Public"
                return "Custom"

            def _search_text(sk) -> str:
                return " ".join([
                    sk.name,
                    sk.display_name,
                    sk.description,
                    " ".join(sk.tags or []),
                    _source_label(sk),
                ]).lower()

            def _include_skill(sk) -> bool:
                if query and query not in _search_text(sk):
                    return False
                if filter_value == "Pinned":
                    return skills_mod.is_pinned(sk.name)
                if filter_value == "Available":
                    return skills_mod.is_enabled(sk.name)
                if filter_value == "Custom":
                    return sk.source == "user"
                if filter_value == "Public":
                    return sk.name in hub_records
                return True

            def _sort_key(sk):
                tel = skill_telemetry.get(sk.name, {})
                if sort_value == "Recently used":
                    return (str(tel.get("last_used") or ""), sk.display_name.lower())
                if sort_value == "Token cost":
                    return (skills_mod.estimate_skill_tokens(sk.name), sk.display_name.lower())
                if sort_value == "Source":
                    return (_source_label(sk), sk.display_name.lower())
                return (sk.display_name.lower(), sk.name)

            visible_skills = [sk for sk in all_skills if _include_skill(sk)]
            reverse = sort_value == "Recently used"
            visible_skills.sort(key=_sort_key, reverse=reverse)

            with skills_container:
                if not visible_skills:
                    ui.label("No skills match the current filters.").classes("text-grey-6 text-sm q-pa-sm")
                    return

                for index, sk in enumerate(visible_skills):
                    hub_record = hub_records.get(sk.name)
                    _tel = skill_telemetry.get(sk.name, {})
                    _uses = int(_tel.get("usage_count", 0) or 0)
                    _last_used = str(_tel.get("last_used") or "")
                    tokens = skills_mod.estimate_skill_tokens(sk.name)
                    _is_pinned = skills_mod.is_pinned(sk.name)

                    def _set_available(e, n=sk.name):
                        skills_mod.set_enabled(n, bool(e.value))
                        _refresh_skills_list()

                    def _toggle_pin(n=sk.name):
                        try:
                            skills_mod.set_pinned(n, not skills_mod.is_pinned(n))
                            _refresh_skills_list()
                        except Exception as exc:
                            logger.warning("Could not update skill pin: %s", exc, exc_info=True)
                            ui.notify(str(exc), type="warning")

                    with ui.column().classes("w-full gap-1 q-px-sm q-py-sm").style(
                        "border-bottom: 1px solid rgba(148, 163, 184, 0.14);"
                        + ("border-top: 1px solid rgba(148, 163, 184, 0.14);" if index == 0 else "")
                    ):
                        with ui.row().classes("w-full items-center no-wrap gap-2"):
                            ui.switch(
                                "",
                                value=skills_mod.is_enabled(sk.name),
                                on_change=_set_available,
                            ).props("dense").tooltip(
                                f"Available skills can be selected in the chat skill picker and suggested by {APP_DISPLAY_NAME}."
                            )
                            ui.button(
                                icon="push_pin",
                                on_click=lambda _=None, n=sk.name: _toggle_pin(n),
                            ).props(
                                "flat dense round size=sm "
                                f"color={'primary' if _is_pinned else 'grey'}"
                            ).tooltip(
                                "Pinned skills start active in new chats, tasks, Designer, and Developer."
                            )
                            with ui.column().classes("gap-0").style("min-width: 0; flex: 1 1 auto;"):
                                with ui.row().classes("items-center gap-2 no-wrap w-full"):
                                    ui.label(f"{sk.icon} {sk.display_name}").classes(
                                        "text-sm text-weight-medium"
                                    ).style(
                                        "min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                                    )
                                    if _is_pinned:
                                        ui.badge("Pinned", color="primary").props("outline dense")
                                ui.label(sk.description or "No description.").classes("text-grey-6 text-xs").style(
                                    "overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%;"
                                )
                            with ui.row().classes("items-center justify-end gap-1 flex-wrap").style(
                                "flex: 0 1 360px; min-width: 0;"
                            ):
                                source = _source_label(sk)
                                source_color = (
                                    "blue-grey"
                                    if source == "Bundled"
                                    else "orange"
                                    if source == "Public"
                                    else "teal"
                                )
                                ui.badge(source, color=source_color).props("outline dense")
                                if _uses:
                                    ui.label(f"{_uses} uses").classes("text-grey-6 text-xs")
                                if _last_used:
                                    ui.label(_last_used[:10]).classes("text-grey-6 text-xs")
                                if tokens > 0:
                                    token_class = "text-orange text-xs" if tokens >= 1800 else "text-grey-6 text-xs"
                                    ui.label(f"~{tokens} tok").classes(token_class).tooltip(
                                        "Approximate tokens in this skill's instructions"
                                    )
                            with ui.button(icon="more_vert").props("flat dense round size=sm").tooltip("More actions"):
                                with ui.menu().classes("q-pa-xs"):
                                    if hub_record:
                                        ui.menu_item("Audit", lambda n=sk.name: _open_hub_audit(n))
                                        ui.menu_item("Check update", lambda n=sk.name: _check_hub_update(n))
                                        ui.menu_item("Update", lambda n=sk.name: _update_hub_skill(n))
                                        _url = str(hub_record.metadata.get("url") or "")
                                        if _url:
                                            ui.menu_item(
                                                "Open source",
                                                lambda url=_url: ui.run_javascript(
                                                    "window.open("
                                                    + json.dumps(url)
                                                    + ", '_blank', 'noopener,noreferrer')"
                                                ),
                                            )
                                        ui.separator()
                                        ui.menu_item(
                                            "Uninstall",
                                            lambda n=sk.name: _confirm_uninstall_hub_skill(n),
                                        ).classes("text-negative")
                                    elif sk.source == "user":
                                        ui.menu_item("Edit", lambda n=sk.name: _open_skill_editor(n))
                                        ui.separator()
                                        ui.menu_item(
                                            "Delete",
                                            lambda n=sk.name: _confirm_delete_skill(n),
                                        ).classes("text-negative")
                                    else:
                                        ui.menu_item("Duplicate & Customize", lambda n=sk.name: _duplicate_skill(n))

        search_input.on("update:model-value", lambda _: _refresh_skills_list())
        filter_select.on_value_change(lambda _: _refresh_skills_list())
        sort_select.on_value_change(lambda _: _refresh_skills_list())

        def _open_hub_audit(name: str) -> None:
            from row_bot.skills_hub.provenance import get_record

            record = get_record(name)
            if not record:
                ui.notify("No hub provenance found for this skill", type="warning")
                return
            with ui.dialog() as dlg, ui.card().classes("w-[760px] max-w-full"):
                ui.label(f"Audit: {record.local_name}").classes("text-h6")
                ui.label("Public skill provenance and last scan summary.").classes("text-caption text-grey-6")
                ui.markdown(
                    "```json\n" + json.dumps(record.as_dict(), indent=2, default=str) + "\n```",
                    extras=["fenced-code-blocks"],
                ).classes("w-full")
                with ui.row().classes("justify-end w-full"):
                    ui.button("Close", on_click=dlg.close).props("flat")
            dlg.open()

        async def _check_hub_update(name: str) -> None:
            from row_bot.skills_hub.installer import check_update

            note = ui.notification("Checking public skill source...", type="ongoing", spinner=True, timeout=None)
            try:
                result = await run.io_bound(check_update, name)
                note.dismiss()
                ui.notify(result.message, type="positive" if result.success else "warning")
            except Exception as exc:
                note.dismiss()
                ui.notify(f"Update check failed: {exc}", type="negative")

        async def _update_hub_skill(name: str) -> None:
            from row_bot.skills_hub.installer import update_skill

            note = ui.notification("Updating public skill...", type="ongoing", spinner=True, timeout=None)
            try:
                result = await run.io_bound(update_skill, name)
                note.dismiss()
                ui.notify(result.message, type="positive" if result.success else "warning")
                if result.success:
                    _refresh_skills_list()
            except Exception as exc:
                note.dismiss()
                ui.notify(f"Update failed: {exc}", type="negative")

        def _confirm_uninstall_hub_skill(name: str) -> None:
            sk = skills_mod.get_skill(name)
            if not sk:
                return
            with ui.dialog() as dlg, ui.card():
                ui.label(f"Uninstall public skill '{sk.display_name}'?").classes("text-body1")
                ui.label("This removes the local skill folder and hub provenance.").classes("text-grey-6 text-sm")
                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")

                    def _do_uninstall():
                        from row_bot.skills_hub.installer import uninstall_skill

                        result = uninstall_skill(name)
                        ui.notify(result.message, type="positive" if result.success else "warning")
                        dlg.close()
                        _refresh_skills_list()

                    ui.button("Uninstall", on_click=_do_uninstall).props("color=negative")
            dlg.open()

        def _open_skill_editor(name=None):
            skill = skills_mod.get_skill(name) if name else None
            is_edit = skill is not None

            with ui.dialog().props("persistent maximized=false") as dlg, ui.card().classes(
                "w-full"
            ).style("min-width: 600px; max-width: 800px;"):
                ui.label(f"{'Edit' if is_edit else 'Create'} Skill").classes("text-h6")
                name_input = ui.input(
                    "Name (identifier)",
                    value=skill.name if skill else "",
                    validation={"Required": lambda v: bool(v.strip())},
                ).classes("w-full")
                if is_edit:
                    name_input.props("readonly")

                display_input = ui.input(
                    "Display Name", value=skill.display_name if skill else "",
                ).classes("w-full")

                _icon = material_icon_for(
                    skill.icon if skill else "auto_awesome",
                    fallback="auto_awesome",
                )
                _wf_icon_opts = icon_select_options(_icon, fallback="auto_awesome")
                with ui.row().classes("w-full items-end gap-4"):
                    icon_sel = ui.select(label="Icon", options=_wf_icon_opts, value=_icon).classes("w-20")
                    desc_input = ui.input(
                        "Description (one line)", value=skill.description if skill else "",
                    ).classes("flex-grow")

                tags_input = ui.input(
                    "Tags (comma-separated)",
                    value=", ".join(skill.tags) if skill and skill.tags else "",
                ).classes("w-full")

                ui.label("Instructions").classes("text-sm font-bold mt-4")
                instructions_input = ui.textarea(
                    value=skill.instructions if skill else "",
                ).classes("w-full").props('rows="12"')

                def _update_token_est():
                    txt = instructions_input.value or ""
                    est = skills_mod.estimate_text_tokens(txt)
                    token_label.text = f"~{est} tokens"

                with ui.row().classes("w-full items-center"):
                    token_label = ui.label("~0 tokens").classes("text-grey-5 text-sm")
                    _update_token_est()
                    instructions_input.on("blur", lambda: _update_token_est())

                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")

                    def _save():
                        _name = name_input.value.strip()
                        _display = display_input.value.strip() or _name.replace("_", " ").title()
                        _desc = desc_input.value.strip()
                        _icon_val = icon_sel.value or "auto_awesome"
                        _instr = instructions_input.value.strip()
                        _tags = [t.strip() for t in tags_input.value.split(",") if t.strip()]
                        if not _name:
                            ui.notify("Name is required", type="warning")
                            return
                        if not _instr:
                            ui.notify("Instructions are required", type="warning")
                            return
                        if is_edit:
                            skills_mod.update_skill(
                                name=_name, display_name=_display, icon=_icon_val,
                                description=_desc, instructions=_instr, tags=_tags,
                            )
                            ui.notify(f"✅ Skill '{_display}' updated", type="positive")
                        else:
                            skills_mod.create_skill(
                                name=_name, display_name=_display, icon=_icon_val,
                                description=_desc, instructions=_instr, tags=_tags,
                            )
                            ui.notify(f"✅ Skill '{_display}' created", type="positive")
                        dlg.close()
                        _refresh_skills_list()

                    ui.button("Save", icon="save", on_click=_save).props("color=primary")

            dlg.open()

        def _confirm_delete_skill(name):
            sk = skills_mod.get_skill(name)
            if not sk:
                return
            with ui.dialog() as dlg, ui.card():
                ui.label(f"Delete skill '{sk.display_name}'?").classes("text-body1")
                ui.label("This will permanently remove the skill files.").classes("text-grey-6 text-sm")
                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")
                    def _do_delete():
                        skills_mod.delete_skill(name)
                        ui.notify(f"Skill '{sk.display_name}' deleted", type="info")
                        dlg.close()
                        _refresh_skills_list()
                    ui.button("Delete", on_click=_do_delete).props("color=negative")
            dlg.open()

        def _duplicate_skill(name):
            result = skills_mod.duplicate_skill(name)
            if result:
                ui.notify(f"✅ Duplicated as '{result.display_name}'", type="positive")
                _refresh_skills_list()
            else:
                ui.notify("Failed to duplicate skill", type="negative")

        skills_mod.load_skills()
        _refresh_skills_list()

    # ── Search / Tools Tab ───────────────────────────────────────────

    def _build_tools_tab() -> None:
        _settings_header(
            "Tools",
            "Configure capability loading, retrieval compression, and research tools.",
            "search",
        )
        with _settings_section(
            "Capability loading",
            "Choose how enabled external capabilities are exposed to the model.",
            icon="inventory_2",
        ):
            ui.radio(
                options={
                    "auto": "Auto-select external tools (recommended)",
                    "eager": "Load all external tools",
                },
                value=tool_registry.get_external_tool_loading_mode(),
                on_change=lambda e: tool_registry.set_global_config(
                    "external_tool_loading_mode",
                    e.value if e.value in {"auto", "eager"} else "auto",
                ),
            ).props("dense data-docs-id=settings-external-tool-loading-mode")
            ui.label(
                "Core tools stay available. Row-Bot searches enabled MCP, plugin, "
                "Custom Tool, and channel tools when needed."
            ).classes("text-grey-6 text-caption")
            ui.label(
                "Compatibility mode. Sends every enabled external tool schema to the "
                "model and can use more context."
            ).classes("text-grey-6 text-caption")
        with _settings_section(
            "Retrieval Compression",
            "Controls how search results are filtered before reaching the model.",
            icon="compress",
        ):
            _comp_options = {"off": "Off (default)", "deep": "Deep (LLM)"}
            ui.select(
                label="Compression mode",
                options=_comp_options,
                value=tool_registry.get_global_config("compression_mode", "off"),
                on_change=lambda e: tool_registry.set_global_config("compression_mode", e.value),
            ).classes("w-60").props("dense outlined")

        search_tools = {
            "web_search", "duckduckgo", "wolfram_alpha", "arxiv",
            "wikipedia", "youtube",
        }
        enabled = [
            tool for tool in tool_registry.get_all_tools()
            if tool.name in search_tools and tool_registry.is_enabled(tool.name)
        ]
        with _settings_section(
            "Search & Knowledge Tools",
            "Enable or disable web, research, and reference lookup tools.",
            icon="travel_explore",
        ):
            _metric_chip("enabled", len(enabled), icon="toggle_on")
            for tool in tool_registry.get_all_tools():
                if tool.name not in search_tools:
                    continue
                with ui.column().classes("w-full gap-1 q-pa-sm rounded-borders").style(
                    "border: 1px solid rgba(148, 163, 184, 0.14);"
                ):
                    _build_tool_toggle(tool)

    def _build_tool_toggle(tool) -> None:
        ui.switch(
            tool.display_name,
            value=tool_registry.is_enabled(tool.name),
            on_change=lambda e, n=tool.name: tool_registry.set_enabled(n, e.value),
        ).tooltip(tool.description)

        if tool.name == "web_search":
            with ui.expansion("📋 Tavily Setup Instructions"):
                ui.markdown(
                    "1. Go to [app.tavily.com](https://app.tavily.com/) and sign up.\n"
                    "2. Create an API key.\n"
                    "3. Paste the key below.",
                    extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                )
        elif tool.name == "wolfram_alpha":
            with ui.expansion("📋 Wolfram Alpha Setup Instructions"):
                ui.markdown(
                    "1. Go to [developer.wolframalpha.com](https://developer.wolframalpha.com/) and sign up.\n"
                    "2. Click **Get an AppID** and create an app.\n"
                    "3. Paste the AppID below.",
                    extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                )

        if tool.required_api_keys:
            for label, env_var in tool.required_api_keys.items():
                inp, refresh = _secret_input(label, env_var)
                with ui.row().classes("gap-2"):
                    def _save_tool_key(ev=env_var, widget=inp, refresh_status=refresh, display=label):
                        val = _secret_value_or_notify(widget.value, display)
                        if not val:
                            return
                        set_key(ev, val)
                        widget.value = ""
                        widget.update()
                        refresh_status()
                        ui.notify(f"{display} saved", type="positive")

                    ui.button("Save", icon="save", on_click=_save_tool_key).props("flat dense")
                    ui.button(
                        "Clear",
                        icon="delete",
                        on_click=lambda ev=env_var, display=label, refresh_status=refresh: _clear_secret(ev, display, refresh_status),
                    ).props("flat dense color=negative")

        schema = tool.config_schema
        if schema:
            for cfg_key, spec in schema.items():
                cfg_type = spec.get("type", "text")
                cfg_label = spec.get("label", cfg_key)
                cfg_default = spec.get("default")
                current_cfg = tool.get_config(cfg_key, cfg_default)
                if cfg_type == "text":
                    ui.input(
                        cfg_label, value=current_cfg or "",
                        on_change=lambda e, t=tool, k=cfg_key: t.set_config(k, e.value),
                    ).classes("w-full")
                elif cfg_type == "select":
                    options = spec.get("options", [])
                    labels_map = spec.get("labels", {})
                    option_labels = {o: labels_map.get(o, o) for o in options}
                    ui.select(
                        option_labels,
                        value=current_cfg or cfg_default,
                        label=cfg_label,
                        on_change=lambda e, t=tool, k=cfg_key: t.set_config(k, e.value),
                    ).classes("w-full")
                elif cfg_type == "multicheck":
                    options = spec.get("options", [])
                    current_list = current_cfg if isinstance(current_cfg, list) else (cfg_default or [])
                    ui.label(cfg_label).classes("text-sm font-bold mt-2")
                    for opt in options:
                        ui.checkbox(
                            opt, value=opt in current_list,
                            on_change=lambda e, t=tool, k=cfg_key, o=opt, cl=current_list: (
                                cl.append(o) if e.value and o not in cl else (cl.remove(o) if not e.value and o in cl else None),
                                t.set_config(k, list(cl)),
                            ),
                        )

    def _build_ops_checkboxes(groups, current_ops, tool, cfg_key="selected_operations"):
        ui.label("Allowed operations").classes("text-sm font-bold mt-2")
        selected = list(current_ops)

        def _toggle(op, val):
            if val and op not in selected:
                selected.append(op)
            elif not val and op in selected:
                selected.remove(op)
            tool.set_config(cfg_key, list(selected))

        with ui.row().classes("w-full gap-8"):
            for header, ops in groups:
                with ui.column():
                    ui.label(header).classes("font-bold text-sm")
                    for op in ops:
                        ui.checkbox(op, value=op in current_ops,
                                    on_change=lambda e, o=op: _toggle(o, e.value))

    # ── System Access Tab ────────────────────────────────────────────

    def _build_system_access_tab() -> None:
        from row_bot.tools.filesystem_tool import _SAFE_OPS, _WRITE_OPS, _DESTRUCTIVE_OPS

        _settings_header(
            "System",
            "Control local access, command execution, browser automation, tunnels, and logs.",
            "terminal",
        )

        fs_tool = tool_registry.get_tool("filesystem")
        if not fs_tool:
            ui.label("Filesystem tool not found.").classes("text-negative")
            return

        with _settings_section(
            "Workspace Folder",
            "The filesystem tool is sandboxed to this folder.",
            icon="folder",
        ):
            fs_root_default = fs_tool.config_schema.get("workspace_root", {}).get("default", "")
            current_root = fs_tool.get_config("workspace_root", fs_root_default)
            root_input = ui.input(
                "Workspace folder", value=current_root or "",
                on_change=lambda e: fs_tool.set_config("workspace_root", e.value),
            ).classes("w-full").props("dense outlined")

            async def _browse_ws():
                folder = await browse_folder("Select Workspace folder", current_root)
                if folder:
                    root_input.value = folder
                    fs_tool.set_config("workspace_root", folder)

            ui.button("Browse", icon="folder_open", on_click=_browse_ws).props("flat dense no-caps")

            if current_root and not os.path.isdir(current_root):
                ui.label(f"Folder not found: {current_root}").classes("text-warning text-sm")

        # Shell Access
        with _settings_section(
            "Shell Access",
            "Run shell commands directly on your system.",
            icon="terminal",
            tone="warning",
        ):
            shell_tool = tool_registry.get_tool("shell")
            if shell_tool:
                ui.switch(
                    "Enable Shell tool",
                    value=tool_registry.is_enabled("shell"),
                    on_change=lambda e: tool_registry.set_enabled("shell", e.value),
                ).tooltip(shell_tool.description)

                shell_blocked = shell_tool.get_config("blocked_commands", "")
                ui.input(
                    "Additional blocked patterns (comma-separated)",
                    value=shell_blocked or "",
                    on_change=lambda e: shell_tool.set_config("blocked_commands", e.value),
                ).classes("w-full").props("dense outlined")
            else:
                ui.label("Shell tool not found.").classes("text-grey-6 text-sm")

        # Browser Automation
        with _settings_section(
            "Browser & Computer Use",
            "Separate web and native-app automation engines with explicit runtime setup.",
            icon="public",
        ):
            browser_tool = tool_registry.get_tool("browser")
            if browser_tool:
                ui.switch(
                    "Enable Browser tool",
                    value=tool_registry.is_enabled("browser"),
                    on_change=lambda e: tool_registry.set_enabled("browser", e.value),
                ).tooltip(browser_tool.description)
                from row_bot.mcp_client.requirements import _requirement_from_id, check_requirement

                browser_requirement = _requirement_from_id("playwright-chrome", source="settings")
                browser_check = check_requirement(browser_requirement) if browser_requirement else None
                ui.label(
                    "Browser runtime ready." if browser_check and browser_check.available
                    else "Installed Chrome/Edge is preferred; otherwise install Row-Bot's managed Playwright Chromium explicitly."
                ).classes("text-xs text-grey-6")

                async def _install_browser_runtime() -> None:
                    from row_bot.mcp_client.requirements import install_managed_runtime

                    result = await run.io_bound(install_managed_runtime, "playwright-chrome")
                    ui.notify(result.message, type="positive" if result.ok else "negative")

                ui.button("Install browser runtime", icon="download", on_click=_install_browser_runtime).props("flat dense no-caps")
            else:
                ui.label("Browser tool not found.").classes("text-grey-6 text-sm")

            ui.separator()
            from row_bot.ui.computer_use import build_computer_use_settings_card

            build_computer_use_settings_card(tool_registry)

        # File Operations
        with _settings_section(
            "File Operations",
            "Read, write, search, copy, move, and delete files.",
            icon="draft",
        ):
            ui.switch(
                "Enable Filesystem tool",
                value=tool_registry.is_enabled("filesystem"),
                on_change=lambda e: tool_registry.set_enabled("filesystem", e.value),
            ).tooltip(fs_tool.description)

            ops_default = fs_tool.config_schema.get("selected_operations", {}).get("default", [])
            current_ops = fs_tool.get_config("selected_operations", ops_default)
            if not isinstance(current_ops, list):
                current_ops = ops_default
            _build_ops_checkboxes(
                [("Read-only", _SAFE_OPS), ("Write", _WRITE_OPS), ("Destructive", _DESTRUCTIVE_OPS)],
                current_ops, fs_tool,
            )

        _build_tunnel_settings_section()

        from row_bot.access.access_routes import (
            AccessRouteConfigStore,
            build_route_inventory,
            discover_private_lan_addresses,
        )
        from row_bot.access.launcher_control import request_launcher_restart
        from row_bot.access.tailscale import (
            TailscaleOwnershipStore,
            TailscaleServeController,
        )
        from row_bot.app_port import get_app_port
        from row_bot.tunnel import tunnel_manager
        from row_bot.ui.remote_access_settings import (
            build_remote_access_settings_section,
        )

        _access_route_store = AccessRouteConfigStore()
        _access_port = get_app_port()
        _tailscale_ownership_store = TailscaleOwnershipStore()
        _tailscale_instance_key = str(
            _tailscale_ownership_store.path.resolve(strict=False)
        )
        _tailscale_status_cache = _process_tailscale_status_cache()
        from row_bot.access.config import AccessConfig, DeploymentMode
        from row_bot.ui.access_context import current_access_context

        _validated_access_context = current_access_context()
        _current_server_origin = (
            _validated_access_context.origin
            if _validated_access_context
            and (
                _validated_access_context.deployment_mode is DeploymentMode.SERVER
                or not _validated_access_context.is_local_owner
            )
            else None
        )
        _environment_access_origins = AccessConfig.from_env().public_origins
        _host_admission_managed_externally = (
            "ROW_BOT_ALLOWED_HOSTS" in os.environ
        )

        def _tailscale_status_snapshot():
            return _tailscale_status_cache.get(
                instance_key=_tailscale_instance_key,
                port=_access_port,
                ownership=_tailscale_ownership_store.load(),
            )

        def _remember_tailscale_status(status: object | None) -> None:
            _tailscale_status_cache.remember(
                instance_key=_tailscale_instance_key,
                port=_access_port,
                status=status,
                ownership=_tailscale_ownership_store.load(),
            )

        def _route_inventory_provider():
            tailscale_snapshot = _tailscale_status_snapshot()
            route_config = _access_route_store.load_or_default()
            return build_route_inventory(
                port=_access_port,
                config=route_config,
                lan_addresses=discover_private_lan_addresses(),
                tailscale_state=(
                    tailscale_snapshot.status if tailscale_snapshot else None
                ),
                ngrok_url=tunnel_manager.get_url(_access_port),
                reverse_proxy_origins=(
                    *route_config.configured_origins,
                    *_environment_access_origins,
                ),
                current_server_origin=_current_server_origin,
            )

        _access_inventory = _route_inventory_provider()
        _initial_tailscale_snapshot = _tailscale_status_snapshot()

        def _request_access_restart() -> bool:
            return request_launcher_restart().accepted

        build_remote_access_settings_section(
            settings_section=_settings_section,
            route_store=_access_route_store,
            route_inventory=_access_inventory,
            route_inventory_provider=_route_inventory_provider,
            tailscale_status_sink=_remember_tailscale_status,
            tailscale_status=(
                _initial_tailscale_snapshot.status
                if _initial_tailscale_snapshot
                else None
            ),
            tailscale_verified_at=(
                _initial_tailscale_snapshot.verified_at
                if _initial_tailscale_snapshot
                else None
            ),
            restart_child=_request_access_restart,
            tailscale_controller=TailscaleServeController(),
            runtime_access_policy=runtime_access_policy,
            environment_origins=_environment_access_origins,
            host_admission_managed_externally=(
                _host_admission_managed_externally
            ),
            port=_access_port,
            status_dot=_status_dot,
            metric_chip=_metric_chip,
        )

        # ── Logging ──────────────────────────────────────────────────
        with _settings_section(
            "📝 Logging",
            f"Structured logs are saved daily to ~/{DEFAULT_DATA_DIR_NAME}/logs/ with 7-day retention.",
        ):
            from row_bot.logging_config import get_file_log_level, set_file_log_level, get_log_dir

            _level_options = ["DEBUG", "INFO", "WARNING", "ERROR"]
            ui.select(
                _level_options,
                value=get_file_log_level(),
                label="File log level",
                on_change=lambda e: set_file_log_level(e.value),
            ).classes("w-48").props("dense outlined").tooltip("Minimum severity written to log files")

            async def _open_log_folder():
                import subprocess, sys
                log_dir = str(get_log_dir())
                if sys.platform == "win32":
                    subprocess.Popen(["explorer", log_dir])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", log_dir])
                else:
                    subprocess.Popen(["xdg-open", log_dir])

            ui.button("Open Log Folder", icon="folder_open", on_click=_open_log_folder).props(
                "flat dense no-caps"
            )

    # ── Google Account Tab (unified Gmail + Calendar) ──────────────

    def _build_google_account_panel() -> None:
        import shutil
        gmail_tool = tool_registry.get_tool("gmail")
        cal_tool = tool_registry.get_tool("calendar")
        if not gmail_tool or not cal_tool:
            ui.label("Gmail or Calendar tool not found.").classes("text-negative")
            return

        # Canonical credentials location
        from row_bot.tools.gmail_tool import _GMAIL_DIR, DEFAULT_CREDENTIALS_PATH as _GMAIL_CREDS_DEFAULT
        from row_bot.tools.calendar_tool import DEFAULT_TOKEN_PATH as _CAL_TOKEN_PATH

        def _google_status_text():
            _gmail_ok = gmail_tool.is_authenticated()
            _cal_ok = cal_tool.is_authenticated()
            if _gmail_ok and _cal_ok:
                try:
                    s1, _ = gmail_tool.check_token_health()
                    s2, _ = cal_tool.check_token_health()
                    if s1 in ("valid", "refreshed") and s2 in ("valid", "refreshed"):
                        return "✅ Connected"
                    return "⚠️ Token issue"
                except Exception:
                    return "✅ Connected"
            if not gmail_tool.has_credentials_file():
                return "⚠️ Not configured"
            return "🔑 Not authenticated"

        with ui.expansion(
            f"Google (Gmail & Calendar) — {_google_status_text()}",
            icon="account_circle",
        ).classes("w-full") as google_panel:

            # ── Enable switches ──
            with ui.row().classes("gap-8 items-center"):
                ui.switch(
                    "Gmail",
                    value=tool_registry.is_enabled("gmail"),
                    on_change=lambda e: tool_registry.set_enabled("gmail", e.value),
                ).tooltip(gmail_tool.description)
                ui.switch(
                    "Calendar",
                    value=tool_registry.is_enabled("calendar"),
                    on_change=lambda e: tool_registry.set_enabled("calendar", e.value),
                ).tooltip(cal_tool.description)

            ui.separator()

            # ── Setup wizard (stepper) ──
            with ui.expansion("Setup Guide — first-time setup", icon="help_outline").classes("w-full"):
                with ui.stepper().props("vertical").classes("w-full") as stepper:
                    with ui.step("Create Google Cloud Project"):
                        ui.markdown(
                            "1. Open [Google Cloud Console](https://console.cloud.google.com)\n"
                            "2. Click the project dropdown (top bar) → **New Project**\n"
                            f"3. Name it anything (e.g. *{APP_DISPLAY_NAME}*) → **Create**\n"
                            "4. Make sure the new project is selected in the dropdown",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                    with ui.step("Enable APIs"):
                        ui.markdown(
                            "1. Go to **APIs & Services → Library**\n"
                            "2. Search for **Gmail API** → click it → **Enable**\n"
                            "3. Search for **Google Calendar API** → click it → **Enable**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Configure OAuth Consent"):
                        ui.markdown(
                            "1. Go to **APIs & Services → OAuth consent screen**\n"
                            '2. Select **External** → **Create**\n'
                            f"3. Fill in App name (e.g. *{APP_DISPLAY_NAME}*), your email → **Save and Continue**\n"
                            "4. On **Scopes** page → just click **Save and Continue**\n"
                            "5. On **Test users** → **Add Users** → add your Gmail address → **Save**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Create OAuth Client ID"):
                        ui.markdown(
                            "1. Go to **APIs & Services → Credentials**\n"
                            "2. Click **+ Create Credentials → OAuth client ID**\n"
                            "3. Application type → **Desktop app**\n"
                            "4. Name it anything → **Create**\n"
                            "5. Click **Download JSON** (saves as `client_secret_...json`)",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Select Credentials & Authenticate"):
                        ui.markdown(
                            "Use the **Browse** button below to select the downloaded JSON file, "
                            "then click **Authenticate Google**. A browser window will open for sign-in.",
                        )
                        with ui.stepper_navigation():
                            ui.button("Back", on_click=stepper.previous).props("flat")

            ui.separator()

            # ── Credentials path + browse + auto-copy ──
            creds_default = gmail_tool.config_schema.get("credentials_path", {}).get("default", "")
            current_creds = gmail_tool.get_config("credentials_path", creds_default)
            creds_input = ui.input(
                "credentials.json path", value=current_creds or "",
            ).classes("w-full").props("readonly")

            async def _browse_and_copy():
                path = await browse_file(
                    "Select credentials.json (or client_secret_*.json)",
                    os.path.dirname(current_creds) if current_creds else "",
                    [("JSON files", "*.json")],
                )
                if not path:
                    return
                src = pathlib.Path(path)
                dest = _GMAIL_DIR / "credentials.json"
                # Auto-copy to canonical location if not already there
                if src.resolve() != dest.resolve():
                    try:
                        shutil.copy2(str(src), str(dest))
                        ui.notify(f"Copied to {dest}", type="info")
                    except Exception as exc:
                        ui.notify(f"Copy failed: {exc}", type="negative")
                        # Fall back to using the original path
                        creds_input.value = path
                        gmail_tool.set_config("credentials_path", path)
                        cal_tool.set_config("credentials_path", path)
                        return
                canonical = str(dest)
                creds_input.value = canonical
                gmail_tool.set_config("credentials_path", canonical)
                cal_tool.set_config("credentials_path", canonical)
                ui.notify("Credentials ready — click Authenticate Google", type="positive")

            ui.button("Browse…", on_click=_browse_and_copy, icon="folder_open").props("flat dense")

            ui.separator()

            # ── Combined auth status ──
            _has_creds = gmail_tool.has_credentials_file()
            _gmail_authed = gmail_tool.is_authenticated()
            _cal_authed = cal_tool.is_authenticated()
            _both_authed = _gmail_authed and _cal_authed

            def _show_token_status(label: str, tool, authed: bool):
                if not authed:
                    ui.label(f"⬜ {label} — not authenticated").classes("text-grey-6 text-sm")
                    return
                try:
                    status, detail = tool.check_token_health()
                except Exception:
                    status, detail = "valid", ""
                if status in ("valid", "refreshed"):
                    ui.label(f"✅ {label} — token healthy").classes("text-positive text-sm")
                elif status == "expired":
                    ui.label(f"⚠️ {label} — token expired").classes("text-warning text-sm")
                elif status == "error":
                    ui.label(f"⚠️ {label} — {detail}").classes("text-warning text-sm")
                else:
                    ui.label(f"✅ {label} — connected").classes("text-positive text-sm")

            with ui.column().classes("gap-1"):
                _show_token_status("Gmail", gmail_tool, _gmail_authed)
                _show_token_status("Calendar", cal_tool, _cal_authed)

            # ── Combined authenticate / re-authenticate ──
            def _do_combined_auth():
                """Single OAuth flow with both Gmail + Calendar scopes."""
                from google_auth_oauthlib.flow import InstalledAppFlow
                from row_bot.tools.gmail_tool import GMAIL_SCOPES, DEFAULT_TOKEN_PATH as _GMAIL_TOKEN
                from row_bot.tools.calendar_tool import CALENDAR_SCOPES

                creds_path = gmail_tool._get_credentials_path()
                combined_scopes = GMAIL_SCOPES + CALENDAR_SCOPES

                flow = InstalledAppFlow.from_client_secrets_file(creds_path, combined_scopes)
                creds = flow.run_local_server(port=0)

                # Write token to both locations
                pathlib.Path(_GMAIL_TOKEN).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(_GMAIL_TOKEN).write_text(creds.to_json())
                pathlib.Path(_CAL_TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(_CAL_TOKEN_PATH).write_text(creds.to_json())

            if _has_creds:
                if _both_authed:
                    async def _reauth_google():
                        try:
                            # Remove both tokens
                            for tp in (gmail_tool._get_token_path(), cal_tool._get_token_path()):
                                if os.path.isfile(tp):
                                    os.remove(tp)
                            await run.io_bound(_do_combined_auth)
                            clear_agent_cache()
                            ui.notify("✅ Google account re-authenticated!", type="positive")
                            _reopen("Accounts")
                        except Exception as e:
                            ui.notify(f"Auth failed: {e}", type="negative")

                    ui.button("Re-authenticate Google", on_click=_reauth_google, icon="refresh").props("flat dense")
                else:
                    async def _auth_google():
                        try:
                            await run.io_bound(_do_combined_auth)
                            clear_agent_cache()
                            ui.notify("✅ Google account authenticated!", type="positive")
                            _reopen("Accounts")
                        except Exception as e:
                            ui.notify(f"Auth failed: {e}", type="negative")

                    ui.button("Authenticate Google", on_click=_auth_google, icon="login").props("outlined")
            else:
                ui.label(
                    "Select your credentials file above to get started."
                ).classes("text-grey-6 text-sm")

            # ── Gmail operation checkboxes ──
            ui.separator()
            ui.label("Gmail Operations").classes("text-subtitle2")
            from row_bot.tools.gmail_tool import _READ_OPS, _COMPOSE_OPS, _SEND_OPS
            ops_default = gmail_tool.config_schema.get("selected_operations", {}).get("default", [])
            current_ops = gmail_tool.get_config("selected_operations", ops_default)
            if not isinstance(current_ops, list):
                current_ops = ops_default
            _build_ops_checkboxes(
                [("Read", _READ_OPS), ("Compose", _COMPOSE_OPS), ("⚠️ Send", _SEND_OPS)],
                current_ops, gmail_tool,
            )

            # ── Calendar operation checkboxes ──
            ui.separator()
            ui.label("Calendar Operations").classes("text-subtitle2")
            from row_bot.tools.calendar_tool import (
                _READ_OPS as CAL_READ_OPS,
                _WRITE_OPS as CAL_WRITE_OPS,
                _DESTRUCTIVE_OPS as CAL_DESTRUCTIVE_OPS,
            )
            cal_ops_default = cal_tool.config_schema.get("selected_operations", {}).get("default", [])
            current_cal_ops = cal_tool.get_config("selected_operations", cal_ops_default)
            if not isinstance(current_cal_ops, list):
                current_cal_ops = cal_ops_default
            _build_ops_checkboxes(
                [("Read", CAL_READ_OPS), ("Write", CAL_WRITE_OPS), ("⚠️ Destructive", CAL_DESTRUCTIVE_OPS)],
                current_cal_ops, cal_tool,
            )

    # ── Accounts Tab ─────────────────────────────────────────────────

    def _build_github_account_panel() -> None:
        import row_bot.github_account as github_account

        github_generation = LoadGeneration()
        github_status_state = {"status": None}

        def _github_status_text(status=None, *, loading: bool = False) -> str:
            if loading or status is None:
                return "GitHub — Checking..."
            if status.connected:
                suffix = f" as {status.user}" if status.user else ""
                source = status.source.replace("_", " ") or "GitHub"
                return f"GitHub — ✅ Connected via {source}{suffix}"
            if status.state == github_account.GITHUB_STATE_ANONYMOUS:
                return "GitHub — Anonymous public access"
            if status.state == github_account.GITHUB_STATE_INVALID_TOKEN:
                return "GitHub — Reconnect needed"
            if status.state in {
                github_account.GITHUB_STATE_RATE_LIMITED,
                github_account.GITHUB_STATE_SECONDARY_LIMITED,
            }:
                return "GitHub — Rate limited"
            if status.state == github_account.GITHUB_STATE_OFFLINE:
                return "GitHub — Unable to verify"
            return "GitHub — Not connected"

        with ui.expansion(_github_status_text(loading=True), icon="code").classes("w-full") as github_panel:
            ui.label(
                "GitHub access improves public skill browsing, private GitHub imports, Developer Studio, MCP setup, plugin fetches, and release checks."
            ).classes("text-grey-6 text-sm")
            status_col = ui.column().classes("gap-1")

            def _update_github_header(status=None, *, loading: bool = False) -> None:
                github_panel._props["label"] = _github_status_text(status, loading=loading)
                github_panel.update()

            def _set_github_header_text(text: str) -> None:
                github_panel._props["label"] = text
                github_panel.update()

            def _format_rate(rate, label: str) -> str:
                if not rate:
                    return ""
                reset = f", resets at {rate.reset_display}" if rate.reset_display else ""
                if rate.limit:
                    return f"{label}: {rate.remaining}/{rate.limit} requests remaining{reset}."
                if rate.limited:
                    return github_account.rate_limit_message(rate)
                return ""

            def _state_color(status) -> str:
                if status.connected:
                    return "text-positive"
                if status.state == github_account.GITHUB_STATE_ANONYMOUS:
                    return "text-grey-6"
                if status.state in {
                    github_account.GITHUB_STATE_INVALID_TOKEN,
                    github_account.GITHUB_STATE_OFFLINE,
                }:
                    return "text-negative"
                return "text-warning"

            def _render_github_status(status=None) -> None:
                status_col.clear()
                if status is None:
                    with status_col:
                        with ui.row().classes("items-center gap-2 text-grey-6"):
                            ui.spinner(size="xs")
                            ui.label("Checking GitHub status...").classes("text-sm")
                    _update_github_header(loading=True)
                    return
                github_status_state["status"] = status
                with status_col:
                    if status.connected:
                        with ui.row().classes("items-center gap-2"):
                            ui.icon("check_circle", color="positive").classes("text-lg")
                            source = status.source.replace("_", " ") or "GitHub"
                            user = f" as {status.user}" if status.user else ""
                            ui.label(f"Connected via {source}{user}.").classes("text-positive text-sm")
                    ui.label(status.settings_message or status.message).classes(f"{_state_color(status)} text-sm")
                    if status.source:
                        source = status.source.replace("_", " ")
                        verified = "verified" if status.connected else "not currently usable"
                        ui.label(f"Credential source: {source} ({verified}).").classes("text-grey-6 text-xs")
                    else:
                        ui.label("No GitHub token is configured; public sources can still use anonymous access.").classes("text-grey-6 text-xs")
                    if status.gh_installed:
                        gh_line = "GitHub CLI authenticated" if status.gh_authenticated else "GitHub CLI installed, not authenticated"
                        ui.label(gh_line).classes("text-grey-6 text-xs")
                    else:
                        ui.label("GitHub CLI not found. Token fallback is available.").classes("text-grey-6 text-xs")
                    for line in (
                        _format_rate(status.rate_limit, "Authenticated" if status.source else "Anonymous"),
                        _format_rate(status.anonymous_rate_limit, "Anonymous fallback"),
                    ):
                        if line:
                            ui.label(line).classes("text-grey-6 text-xs")
                    if status.last_checked:
                        checked = datetime.fromtimestamp(status.last_checked).strftime("%H:%M:%S")
                        ui.label(f"Last checked: {checked}.").classes("text-grey-6 text-xs")
                _update_github_header(status)

            _render_github_status(None)

            with ui.expansion("Setup Guide", icon="help_outline").classes("w-full"):
                ui.markdown(
                    "Preferred: install GitHub CLI, then run `gh auth login -h github.com` in a terminal. "
                    f"{APP_DISPLAY_NAME} can reuse that authentication for read-only GitHub API calls.\n\n"
                    "If GitHub CLI already exists but the token is stale, run `gh auth refresh -h github.com` "
                    "or use the reconnect buttons below.\n\n"
                    "Token fallback: create a fine-grained personal access token. Public skill browsing only needs "
                    "public read access. Private skill imports need repository Contents read access for selected "
                    "repositories. Skills Hub does not need write permissions.",
                    extras=["code-friendly", "fenced-code-blocks"],
                ).classes("text-sm")

            token_input, token_refresh = _secret_input("GitHub Personal Access Token", "GITHUB_TOKEN")

            def _refresh_github_panel(status=None) -> None:
                if status is None:
                    _schedule_github_status_load(force=True)
                    return
                _render_github_status(status)

            def _save_github_token() -> None:
                val = _secret_value_or_notify(token_input.value, "GitHub token")
                if not val:
                    return
                set_key("GITHUB_TOKEN", val)
                token_input.value = ""
                token_input.update()
                token_refresh()
                _refresh_github_panel()
                ui.notify("GitHub token saved", type="positive")

            async def _check_github_access() -> None:
                try:
                    status = await run.io_bound(github_account.check_github_access)
                    _refresh_github_panel(status)
                    ui.notify(status.message, type="positive" if status.connected else "warning")
                except Exception as exc:
                    ui.notify(f"GitHub access check failed: {exc}", type="negative")

            def _start_github_cli_auth(mode: str) -> None:
                try:
                    import os
                    import subprocess
                    from row_bot.developer.executables import resolve_github_cli

                    gh_path = resolve_github_cli()
                    if not gh_path:
                        ui.notify("GitHub CLI was not found. Install gh or save a token instead.", type="warning")
                        return
                    args = [gh_path, "auth", mode, "-h", "github.com"]
                    kwargs = {}
                    if os.name == "nt":
                        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                    subprocess.Popen(args, **kwargs)
                    ui.notify("GitHub CLI auth started. Return here and click Check GitHub when it completes.", type="info")
                except Exception as exc:
                    ui.notify(f"Could not start GitHub CLI auth: {exc}", type="negative")

            def _use_anonymous_public_sources() -> None:
                github_account.clear_github_status_cache()
                ui.notify("Public Skills Hub sources will use anonymous GitHub access while auth is invalid or limited.", type="info")

            def _clear_github_token() -> None:
                _clear_secret("GITHUB_TOKEN", "GitHub token", token_refresh)
                _refresh_github_panel()

            async def _load_github_status(token: int, *, force: bool = False) -> None:
                try:
                    if force:
                        github_account.clear_github_caches()
                    status = await run.io_bound(
                        lambda: github_account.get_verified_github_account_status(use_cache=not force)
                    )
                    if not github_generation.is_current(token):
                        return
                    _render_github_status(status)
                except Exception as exc:
                    if not github_generation.is_current(token):
                        return
                    status_col.clear()
                    with status_col:
                        with ui.row().classes("items-center gap-2"):
                            ui.icon("error_outline", color="warning")
                            ui.label(f"Could not check GitHub status: {exc}").classes("text-warning text-sm")
                    _set_github_header_text("GitHub — Unable to verify")

            def _schedule_github_status_load(*, force: bool = False) -> None:
                token = github_generation.next()
                _render_github_status(None)
                safe_ui_task(lambda token=token, force=force: _load_github_status(token, force=force), context="github account status load")

            with ui.row().classes("gap-2 items-center"):
                ui.button("Save Token", icon="save", on_click=_save_github_token).props("outlined dense no-caps")
                ui.button("Check GitHub", icon="check_circle", on_click=_check_github_access).props("flat dense no-caps")
                ui.button("Reconnect CLI", icon="login", on_click=lambda: _start_github_cli_auth("login")).props("flat dense no-caps")
                ui.button("Refresh CLI Auth", icon="refresh", on_click=lambda: _start_github_cli_auth("refresh")).props("flat dense no-caps")
                ui.button("Use Anonymous For Public Sources", icon="public", on_click=_use_anonymous_public_sources).props("flat dense no-caps")
                ui.button("Clear Saved Token", icon="delete", on_click=_clear_github_token).props(
                    "flat dense color=negative no-caps"
                )

            _schedule_github_status_load()

    def _build_accounts_tab() -> None:
        _settings_header(
            "Accounts",
            "Connect GitHub, Google, X, and other personal accounts used by tools.",
            "group",
        )

        _build_github_account_panel()
        _build_google_account_panel()
        _build_x_account_panel()

    def _build_x_account_panel() -> None:
        """Render the X (Twitter) account settings panel."""
        from row_bot.tools.x_tool import (
            XTool, _READ_OPS as X_READ_OPS, _POST_OPS as X_POST_OPS,
            _ENGAGE_OPS as X_ENGAGE_OPS,
        )

        x_tool = tool_registry.get_tool("x")
        if not x_tool:
            ui.label("X tool not found.").classes("text-negative")
            return

        def _x_status_text():
            if not x_tool.has_credentials():
                return "⚠️ Not configured"
            if not x_tool.is_authenticated():
                return "🔑 Not authenticated"
            status, _ = x_tool.check_token_health()
            if status in ("valid", "refreshed"):
                return "✅ Connected"
            if status == "expired":
                return "⚠️ Token expired"
            return "⚠️ Check status"

        with ui.expansion(
            f"𝕏 X (Twitter) — {_x_status_text()}",
            icon="tag",
        ).classes("w-full") as panel:

            # ── Enable switch ────────────────────────────────────────
            ui.switch(
                "Enable X tool",
                value=tool_registry.is_enabled("x"),
                on_change=lambda e: (
                    tool_registry.set_enabled("x", e.value),
                    clear_agent_cache(),
                ),
            ).tooltip(x_tool.description)

            # ── Setup Guide (collapsible) ────────────────────────────
            with ui.expansion("📖 Setup Guide", icon="help_outline").classes("w-full mt-2"):
                with ui.stepper().props("vertical").classes("w-full") as stepper:
                    with ui.step("Create X Developer Account"):
                        ui.markdown(
                            "1. Go to [developer.x.com](https://developer.x.com)\n"
                            "2. Sign in with your X account\n"
                            "3. Apply for a developer account if you haven't already\n"
                            "4. Go to the **Developer Portal Dashboard**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                    with ui.step("Create a Project & App"):
                        ui.markdown(
                            "1. In the Developer Portal, click **+ Create Project**\n"
                            f"2. Name it (e.g. *{APP_DISPLAY_NAME}*) → select a use case → **Next**\n"
                            "3. An App will be created automatically\n"
                            "4. Go to your App's **Settings** tab",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Configure OAuth 2.0"):
                        ui.markdown(
                            "1. Under **User authentication settings**, click **Set up**\n"
                            "2. Enable **OAuth 2.0**\n"
                            "3. App type: **Web App** (or Native App)\n"
                            "4. Callback URL: **`http://127.0.0.1:17638/callback`**\n"
                            "   *(this must match exactly — including the port)*\n"
                            "5. Website URL: any URL (e.g. `https://example.com`)\n"
                            "6. Click **Save**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Copy Client ID & Secret"):
                        ui.markdown(
                            "1. Go to your App's **Keys and tokens** tab\n"
                            "2. Under **OAuth 2.0 Client ID and Client Secret**:\n"
                            "   - Copy the **Client ID** and paste below\n"
                            "   - Copy the **Client Secret** and paste below\n"
                            "3. Click **Save** below, then **Authenticate**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Back", on_click=stepper.previous).props("flat")

            ui.separator()

            # ── Client ID / Secret fields ────────────────────────────
            id_input, id_refresh = _secret_input("Client ID", "X_CLIENT_ID")
            secret_input, secret_refresh = _secret_input("Client Secret", "X_CLIENT_SECRET")

            # ── Auth status ──────────────────────────────────────────
            status_container = ui.column().classes("gap-1 mt-2")

            def _update_auth_status():
                status_container.clear()
                with status_container:
                    if not x_tool.has_credentials():
                        ui.label("⬜ Not configured — enter Client ID and Secret above").classes(
                            "text-grey-6 text-sm"
                        )
                        return
                    if not x_tool.is_authenticated():
                        ui.label("🔑 Credentials saved — click Authenticate below").classes(
                            "text-info text-sm"
                        )
                        return
                    status, detail = x_tool.check_token_health()
                    if status in ("valid", "refreshed"):
                        username = x_tool.get_authenticated_username()
                        if username:
                            ui.label(f"✅ Authenticated as @{username}").classes(
                                "text-positive text-sm"
                            )
                        else:
                            ui.label("✅ Token healthy").classes("text-positive text-sm")
                    elif status == "expired":
                        ui.label(f"⚠️ Token expired — {detail}").classes(
                            "text-warning text-sm"
                        )
                    else:
                        ui.label(f"⚠️ {detail}").classes("text-warning text-sm")

            _update_auth_status()

            # ── Save / Auth / Re-auth buttons ────────────────────────
            def _save_x_credentials():
                cid = (id_input.value or "").strip()
                csecret = (secret_input.value or "").strip()
                has_id = key_status("X_CLIENT_ID").get("configured")
                has_secret = key_status("X_CLIENT_SECRET").get("configured")
                if not cid and not has_id:
                    ui.notify("Please enter your Client ID", type="warning")
                    return
                if not csecret and not has_secret:
                    ui.notify("Please enter your Client Secret", type="warning")
                    return
                if cid:
                    set_key("X_CLIENT_ID", cid)
                    id_input.value = ""
                    id_input.update()
                    id_refresh()
                if csecret:
                    set_key("X_CLIENT_SECRET", csecret)
                    secret_input.value = ""
                    secret_input.update()
                    secret_refresh()
                clear_agent_cache()
                _update_auth_status()
                _update_buttons()
                _refresh_x_header()
                ui.notify("X credentials saved", type="positive")

            async def _do_x_auth():
                if not x_tool.has_credentials():
                    ui.notify("Please save your Client ID and Secret first", type="warning")
                    return
                try:
                    ui.notify("Opening browser for X authentication…", type="info")
                    await run.io_bound(x_tool.authenticate)
                    clear_agent_cache()
                    _update_auth_status()
                    _update_buttons()
                    _refresh_x_header()
                    ui.notify("✅ X authentication successful!", type="positive")
                except Exception as exc:
                    logger.error("X authentication failed: %s", exc, exc_info=True)
                    ui.notify(f"X authentication failed: {exc}", type="negative")

            async def _do_x_reauth():
                # Remove existing token
                from row_bot.tools.x_tool import _TOKEN_PATH
                if _TOKEN_PATH.is_file():
                    _TOKEN_PATH.unlink()
                await _do_x_auth()

            buttons_container = ui.row().classes("gap-2 items-center mt-2")

            def _update_buttons():
                buttons_container.clear()
                with buttons_container:
                    ui.button("💾 Save", on_click=_save_x_credentials)
                    if x_tool.has_credentials():
                        if x_tool.is_authenticated():
                            ui.button("🔄 Re-authenticate", on_click=_do_x_reauth).props("flat")
                        else:
                            ui.button("🔑 Authenticate", on_click=_do_x_auth).props("color=positive")

            _update_buttons()

            # ── Operations checkboxes ────────────────────────────────
            ui.separator()

            ui.label("X Operations").classes("text-subtitle2")
            ui.label("Allowed operations").classes("text-sm font-bold mt-2")

            def _make_toggle(selected, cfg_key):
                def _toggle(op, val):
                    if val and op not in selected:
                        selected.append(op)
                    elif not val and op in selected:
                        selected.remove(op)
                    x_tool.set_config(cfg_key, list(selected))
                return _toggle

            with ui.row().classes("w-full gap-8"):
                # Read column
                read_default = x_tool.config_schema.get("read_operations", {}).get("default", [])
                current_read = list(x_tool.get_config("read_operations", read_default))
                if not isinstance(current_read, list):
                    current_read = list(read_default)
                toggle_read = _make_toggle(current_read, "read_operations")
                with ui.column():
                    ui.label("📖 Read").classes("font-bold text-sm")
                    for op in X_READ_OPS:
                        ui.checkbox(op, value=op in current_read,
                                    on_change=lambda e, o=op: toggle_read(o, e.value))

                # Post column
                post_default = x_tool.config_schema.get("post_operations", {}).get("default", [])
                current_post = list(x_tool.get_config("post_operations", post_default))
                if not isinstance(current_post, list):
                    current_post = list(post_default)
                toggle_post = _make_toggle(current_post, "post_operations")
                with ui.column():
                    ui.label("⚠️ Post (requires approval)").classes("font-bold text-sm")
                    for op in X_POST_OPS:
                        ui.checkbox(op, value=op in current_post,
                                    on_change=lambda e, o=op: toggle_post(o, e.value))

                # Engage column
                engage_default = x_tool.config_schema.get("engage_operations", {}).get("default", [])
                current_engage = list(x_tool.get_config("engage_operations", engage_default))
                if not isinstance(current_engage, list):
                    current_engage = list(engage_default)
                toggle_engage = _make_toggle(current_engage, "engage_operations")
                with ui.column():
                    ui.label("👍 Engage").classes("font-bold text-sm")
                    for op in X_ENGAGE_OPS:
                        ui.checkbox(op, value=op in current_engage,
                                    on_change=lambda e, o=op: toggle_engage(o, e.value))

            def _refresh_x_header():
                panel._props["label"] = f"𝕏 X (Twitter) — {_x_status_text()}"
                panel.update()

    # ── Utilities Tab ────────────────────────────────────────────────

    def _build_utilities_tab() -> None:
        _settings_header(
            "Utilities",
            "Lightweight productivity tools available to the assistant.",
            "build",
        )
        util_names = [
            "task",
            "timer",
            "url_reader",
            "calculator",
            "weather",
            "chart",
            "system_info",
            "conversation_search",
            "custom_tool_builder",
        ]
        enabled_count = sum(1 for name in util_names if tool_registry.is_enabled(name))
        with ui.row().classes("items-center gap-2 q-mb-sm"):
            _metric_chip("enabled", enabled_count, icon="toggle_on")
            _metric_chip("available", len(util_names), icon="apps")
        with _settings_section("Utility Tools", "Toggle small tools used for everyday tasks.", icon="apps"):
            for uname in util_names:
                utool = tool_registry.get_tool(uname)
                if utool is None:
                    continue
                with ui.row().classes("items-center w-full no-wrap q-py-xs").style(
                    "border-bottom: 1px solid rgba(148, 163, 184, 0.12);"
                ):
                    ui.switch(
                        "",
                        value=tool_registry.is_enabled(uname),
                        on_change=lambda e, n=uname: tool_registry.set_enabled(n, e.value),
                    ).tooltip(utool.description)
                    with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                        ui.label(utool.display_name).classes("text-sm text-weight-medium")
                        ui.label(utool.description).classes("text-grey-6 text-xs")

    # ── Tracker Tab ──────────────────────────────────────────────────

    def _build_tracker_tab() -> None:
        from row_bot.tools.tracker_tool import _get_db, _get_all_trackers, _DB_PATH

        _settings_header(
            "Tracker",
            "Track recurring activities, habits, symptoms, and health events.",
            "checklist",
        )

        tracker_tool = tool_registry.get_tool("tracker")
        if not tracker_tool:
            ui.label("Tracker tool not found.").classes("text-negative")
            return

        with _settings_section("Tracker Tool", "Enable the tool and review stored tracker data.", icon="checklist"):
            ui.switch(
                "Enable Habit Tracker",
                value=tool_registry.is_enabled("tracker"),
                on_change=lambda e: tool_registry.set_enabled("tracker", e.value),
            ).tooltip(tracker_tool.description)

        try:
            conn = _get_db()
            trackers = _get_all_trackers(conn)
            total_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.close()
        except Exception:
            trackers = []
            total_entries = 0

        with ui.row().classes("items-center gap-2 q-mb-sm"):
            _metric_chip("active trackers", len(trackers), icon="fact_check")
            _metric_chip("entries", total_entries, icon="data_usage")

        if trackers:
            tracker_container = ui.column().classes("w-full")

            def _refresh_trackers():
                tracker_container.clear()
                try:
                    c = _get_db()
                    tlist = _get_all_trackers(c)
                    with tracker_container:
                        if not tlist:
                            ui.label("No trackers yet.").classes("text-grey-6")
                        else:
                            for t in tlist:
                                entry_count = c.execute(
                                    "SELECT COUNT(*) FROM entries WHERE tracker_id = ?",
                                    (t["id"],),
                                ).fetchone()[0]
                                last_entry = c.execute(
                                    "SELECT timestamp FROM entries WHERE tracker_id = ? ORDER BY timestamp DESC LIMIT 1",
                                    (t["id"],),
                                ).fetchone()
                                last_str = last_entry[0][:10] if last_entry else "never"
                                type_badge = t["type"]
                                if t.get("unit"):
                                    type_badge += f" ({t['unit']})"
                                with ui.row().classes("w-full items-center gap-2"):
                                    ui.label(f"● {t['name']}").classes("font-bold")
                                    ui.badge(type_badge).props("outline")
                                    ui.label(f"{entry_count} entries · last: {last_str}").classes("text-xs text-grey-6")
                                ui.separator()
                    c.close()
                except Exception as exc:
                    with tracker_container:
                        ui.label(f"Error loading trackers: {exc}").classes("text-negative")

            _refresh_trackers()

            ui.separator()

            async def _delete_all_tracker_data():
                confirm = await ui.run_javascript(
                    "confirm('Delete ALL tracker data? This cannot be undone.')",
                    timeout=30,
                )
                if confirm:
                    try:
                        c = _get_db()
                        c.execute("DELETE FROM entries")
                        c.execute("DELETE FROM trackers")
                        c.commit()
                        c.close()
                        ui.notify("All tracker data deleted.", type="info")
                        _refresh_trackers()
                    except Exception as exc:
                        ui.notify(f"Error: {exc}", type="negative")

            with _settings_section(
                "Danger Zone",
                "Delete all habit and health tracker rows.",
                icon="warning",
                tone="danger",
            ):
                ui.button("Delete All Tracker Data", icon="delete", on_click=_delete_all_tracker_data).props("flat dense color=negative no-caps")
        else:
            ui.label("No trackers yet.").classes("text-grey-6 mt-2")

    # ── Knowledge Tab ─────────────────────────────────────────────────

    def _build_knowledge_tab() -> None:
        import row_bot.knowledge_graph as kg
        import row_bot.memory as memory_db
        import row_bot.wiki_vault as wiki_vault
        from row_bot.documents import reset_vector_store

        _settings_header(
            "Knowledge",
            "Manage memory, graph health, wiki vault export, and stored knowledge.",
            "psychology",
        )

        mem_tool = tool_registry.get_tool("memory")
        total = memory_db.count_memories()
        rel_count = kg.count_relations()

        with _settings_section(
            "Memory Graph",
            "Conversation and document entities used for recall and relationship browsing.",
            icon="hub",
        ):
            if mem_tool:
                ui.switch(
                    "Enable Memory",
                    value=tool_registry.is_enabled("memory"),
                    on_change=lambda e: tool_registry.set_enabled("memory", e.value),
                )
            with ui.row().classes("gap-2"):
                _metric_chip("entities", total, icon="account_tree")
                _metric_chip("relations", rel_count, icon="share")

            if total > 0:
                try:
                    stats = kg.get_graph_stats()
                    type_parts = [f"{t}: {c}" for t, c in sorted(stats.get("entity_types", {}).items())]
                    if type_parts:
                        ui.label(f"Types: {', '.join(type_parts)}").classes("text-xs text-grey-6")
                    if stats.get("connected_components", 0) > 0:
                        ui.label(
                            f"Knowledge graph: {stats['connected_components']} component(s), "
                            f"largest {stats['largest_component']} entities, "
                            f"{stats['isolated_entities']} isolated"
                        ).classes("text-xs text-grey-6")
                except Exception:
                    pass

        # ── Wiki Vault section ───────────────────────────────────────
        cfg = wiki_vault._load_config()
        vault_enabled = cfg.get("enabled", False)
        vault_path = cfg.get("vault_path", str(wiki_vault._DATA_DIR / "vault"))

        def _toggle_vault(e):
            wiki_vault.set_enabled(e.value)
            tool_registry.set_enabled("wiki", e.value)
            if e.value:
                ui.notify("Wiki vault enabled — rebuilding…", type="info")
                try:
                    vstats = wiki_vault.rebuild_vault()
                    ui.notify(
                        f"✅ Vault rebuilt: {vstats['exported']} articles",
                        type="positive",
                    )
                except Exception as exc:
                    ui.notify(f"Rebuild failed: {exc}", type="negative")
            else:
                ui.notify("Wiki vault disabled.", type="info")

        with _settings_section(
            "Wiki Vault",
            "Export your knowledge graph as Obsidian-compatible markdown files.",
            icon="menu_book",
        ):
            ui.switch("Enable Wiki Vault", value=vault_enabled, on_change=_toggle_vault)

            ui.label("Vault Path").classes("font-bold")
            with ui.row().classes("w-full items-center gap-2"):
                path_input = ui.input(value=vault_path).classes("flex-grow").props("dense outlined")

                async def _browse_vault():
                    folder = await browse_folder("Select vault folder")
                    if folder:
                        path_input.value = folder

                ui.button("Browse", icon="folder_open", on_click=_browse_vault).props("flat dense no-caps")

                def _apply_path():
                    new_path = path_input.value.strip()
                    if new_path:
                        wiki_vault.set_vault_path(new_path)
                        ui.notify(f"Vault path set to: {new_path}", type="info")

                ui.button("Apply", icon="save", on_click=_apply_path).props("flat dense color=primary no-caps")

            if vault_enabled:
                vstats = wiki_vault.get_vault_stats()
                with ui.row().classes("gap-2"):
                    _metric_chip("articles", vstats.get("articles", 0), icon="article")
                    conv_count = vstats.get("conversations", 0)
                    if conv_count > 0:
                        _metric_chip("conversations", conv_count, icon="forum")

            # ── Vault sync detection ──────────────────────────────
            edited = []
            sync_container = ui.column().classes("w-full")

            def _check_vault_sync() -> None:
                sync_container.clear()
                with sync_container:
                    try:
                        with timed_ui_section("settings.knowledge.wiki_sync_check"):
                            edited_now = wiki_vault.check_vault_sync()
                    except Exception as exc:
                        ui.label(f"Could not check vault sync: {exc}").classes("text-warning text-sm")
                        return
                    if not edited_now:
                        ui.label("Vault files are in sync.").classes("text-grey-6 text-sm")
                        return
                    ui.label(
                        f"{len(edited_now)} file{'s' if len(edited_now) != 1 else ''} edited in vault."
                    ).classes("text-warning text-sm")

                    def _sync_vault_now():
                        try:
                            result = wiki_vault.sync_all_from_vault()
                            ui.notify(
                                f"Synced {result['synced']} file(s) from vault",
                                type="positive",
                            )
                            _reopen("Knowledge")
                        except Exception as exc:
                            ui.notify(f"Sync failed: {exc}", type="negative")

                    ui.button("Sync from Vault", icon="sync", on_click=_sync_vault_now).props(
                        "flat dense color=warning no-caps"
                    )

            with sync_container:
                ui.button(
                    "Check vault sync",
                    icon="sync",
                    on_click=_check_vault_sync,
                ).props("flat dense no-caps")
            if edited:
                with ui.card().classes("w-full bg-amber-1 border-l-4").style("border-color: #ff9800"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("sync_problem", color="amber-8").classes("text-lg")
                        ui.label(
                            f"{len(edited)} file{'s' if len(edited) != 1 else ''} edited in vault"
                        ).classes("font-bold text-amber-10")
                    ui.label(
                        f"These files were modified outside {APP_DISPLAY_NAME}. "
                        "Sync to import changes into the knowledge graph."
                    ).classes("text-xs text-grey-7")

                    def _sync_vault():
                        try:
                            result = wiki_vault.sync_all_from_vault()
                            ui.notify(
                                f"✅ Synced {result['synced']} file(s) from vault",
                                type="positive",
                            )
                            _reopen("Knowledge")
                        except Exception as exc:
                            ui.notify(f"Sync failed: {exc}", type="negative")

                    ui.button("🔄 Sync from Vault", on_click=_sync_vault).props(
                        "flat color=amber-8"
                    )

            with ui.row().classes("gap-2"):
                def _rebuild():
                    try:
                        result = wiki_vault.rebuild_vault()
                        ui.notify(
                            f"✅ Rebuilt: {result['exported']} articles, "
                            f"{result['sparse']} sparse, "
                            f"{result.get('orphans_removed', 0)} orphans removed",
                            type="positive",
                        )
                        _reopen("Knowledge")
                    except Exception as exc:
                        ui.notify(f"Failed: {exc}", type="negative")

                ui.button("🔄 Rebuild Vault", on_click=_rebuild).props("flat")

                def _open_vault():
                    import platform
                    import subprocess as sp
                    vp = wiki_vault.get_vault_path()
                    if not vp.exists():
                        ui.notify("Vault folder not found.", type="warning")
                        return
                    system = platform.system()
                    try:
                        if system == "Windows":
                            os.startfile(str(vp))
                        elif system == "Darwin":
                            sp.Popen(["open", str(vp)])
                        else:
                            sp.Popen(["xdg-open", str(vp)])
                    except Exception as exc:
                        ui.notify(f"Failed to open: {exc}", type="negative")

                ui.button("📂 Open Vault Folder", on_click=_open_vault).props("flat")

        # ── Browse knowledge ─────────────────────────────────────────
        ui.separator()

        if total > 0:
            from row_bot.ui.bulk_select import BulkSelect, render_bulk_action_bar
            from row_bot.ui.confirm import confirm_destructive
            from row_bot.ui import knowledge_audit as audit
            import row_bot.memory_evolution as memory_evolution

            _bulk_mem = BulkSelect()
            _browse_generation = LoadGeneration()
            _browse_page = {"limit": 25}

            def _load_memory_rows() -> list[dict]:
                if hasattr(memory_db, "list_memory_summaries"):
                    return memory_db.list_memory_summaries(limit=max(total, 1), description_chars=500)
                return memory_db.list_memories(limit=max(total, 1))

            def _open_knowledge_editor(mid: str) -> None:
                from row_bot.ui.entity_editor import open_entity_editor

                p.settings_child_modal_open = True

                def _saved() -> None:
                    token = _browse_generation.next()

                    async def _refresh_after_save() -> None:
                        started_refresh = time.perf_counter()
                        try:
                            rows = await run.io_bound(_load_memory_rows)
                        except Exception as exc:
                            logger.exception("Knowledge refresh after save failed")
                            ui.notify(f"Saved, but refresh failed: {exc}", type="warning")
                            return
                        if not _browse_generation.is_current(token):
                            return
                        log_ui_perf(
                            "settings.knowledge.after_save.data",
                            (time.perf_counter() - started_refresh) * 1000.0,
                            rows=len(rows),
                            threshold_ms=UI_DATA_WARN_MS,
                        )

                        defer_ui(lambda rows=rows, token=token: _refresh_status_summary(rows) if _browse_generation.is_current(token) else None, delay=0.01)
                        defer_ui(lambda rows=rows, token=token: _refresh_review_queue(rows) if _browse_generation.is_current(token) else None, delay=0.06)
                        defer_ui(lambda rows=rows, token=token: _refresh_memories(rows) if _browse_generation.is_current(token) else None, delay=0.12)

                    safe_ui_task(_refresh_after_save, context="settings knowledge refresh after entity save")

                def _closed() -> None:
                    p.settings_child_modal_open = False

                open_entity_editor(mid, on_saved=_saved, on_closed=_closed)

            status_summary_container = ui.row().classes("gap-2 q-mb-sm")

            def _refresh_status_summary(rows: list[dict] | None = None) -> None:
                status_summary_container.clear()
                counts = audit.status_counts(rows if rows is not None else _load_memory_rows())
                with status_summary_container:
                    _metric_chip("active", counts.get("active", 0), icon="check_circle", color="positive")
                    _metric_chip("needs review", counts.get("needs_review", 0), icon="rate_review", color="warning")
                    _metric_chip("superseded", counts.get("superseded", 0), icon="change_circle", color="grey")
                    _metric_chip("archived", counts.get("archived", 0), icon="archive", color="grey")

            _refresh_status_summary()

            cat_options = ["All"] + sorted(memory_db.VALID_CATEGORIES)
            cat_sel = ui.select(label="Filter by category", options=cat_options, value="All").classes("w-full")
            with ui.row().classes("w-full gap-2"):
                status_sel = ui.select(label="Status", options=audit.STATUS_OPTIONS, value="All").classes("col")
                source_sel = ui.select(label="Source", options=audit.SOURCE_OPTIONS, value="All").classes("col")
                tier_sel = ui.select(label="Tier", options=audit.TIER_OPTIONS, value="All").classes("col")
            search_input = ui.input("Search knowledge", placeholder="Type a keyword…").classes("w-full")

            review_container = ui.column().classes("w-full")

            with ui.row().classes("w-full items-center justify-end q-mt-xs"):
                _mem_select_btn = ui.button("Select").props(
                    "flat dense no-caps size=sm"
                )

                def _toggle_mem_select():
                    _bulk_mem.toggle_mode()
                    _mem_select_btn.text = (
                        "Done" if _bulk_mem.active else "Select"
                    )
                    _refresh_memories()

                _mem_select_btn.on("click", _toggle_mem_select)

            mem_container = ui.column().classes("w-full")

            def _render_audit_badges(summary: dict) -> None:
                ui.badge(summary["status_label"]).props(
                    f"color={summary.get('status_color', 'blue-grey')} outline"
                )
                ui.badge(summary["tier_label"]).props("color=blue-grey outline")
                ui.badge(summary["source_bucket"]).props("color=blue-grey outline")
                if summary.get("confidence_label"):
                    ui.badge(summary["confidence_label"]).props("color=blue-grey outline")

            def _render_audit_details(mem: dict, summary: dict) -> None:
                meta = [
                    f"ID: {mem['id']}",
                    f"Created: {mem.get('created_at', '')[:16]}",
                    f"Updated: {mem.get('updated_at', '')[:16]}",
                ]
                if summary.get("last_user_modified_at"):
                    meta.append(f"User modified: {summary['last_user_modified_at'][:16]}")
                if summary.get("last_evolved_at"):
                    meta.append(f"Evolved: {summary['last_evolved_at'][:16]}")
                if summary.get("recalled_at"):
                    meta.append(f"Recalled: {summary['recalled_at'][:16]}")
                ui.label(" | ".join(meta)).classes("text-xs text-grey-6")

                if summary.get("review_reason"):
                    ui.label(f"Review: {summary['review_reason']}").classes("text-xs text-orange-4")
                if summary.get("superseded_by"):
                    ui.label(f"Superseded by: {summary['superseded_by']}").classes("text-xs text-grey-6")
                if summary.get("supersedes"):
                    ui.label(f"Supersedes: {', '.join(summary['supersedes'][:4])}").classes("text-xs text-grey-6")

                provenance = [summary["source_label"], *summary.get("source_context_lines", [])]
                if provenance:
                    with ui.expansion("Provenance", icon="manage_search", value=False).classes("w-full"):
                        for line in provenance:
                            ui.label(line).classes("text-xs text-grey-6")
                        evidence = summary.get("evidence") or []
                        if evidence:
                            ui.label(f"Evidence: {summary.get('evidence_count', len(evidence))} item(s)").classes("text-xs text-grey-6 q-mt-xs")
                            for item in evidence:
                                ui.label(item).classes("text-xs text-grey-6")

            def _status_action(mid: str, action: str) -> None:
                if action == "archive":
                    memory_evolution.set_status(
                        mid,
                        "archived",
                        actor="manual",
                        reason="Archived from Knowledge UI",
                    )
                    ui.notify("Memory archived.", type="info")
                elif action == "restore":
                    memory_evolution.mark_user_modified(
                        mid,
                        actor="manual",
                        source_context={"actor": "manual", "surface": "knowledge_ui"},
                        status="active",
                    )
                    ui.notify("Memory restored.", type="positive")
                elif action == "resolve":
                    memory_evolution.mark_user_modified(
                        mid,
                        actor="manual",
                        source_context={"actor": "manual", "surface": "knowledge_ui", "action": "resolve_review"},
                        status="active",
                    )
                    ui.notify("Review resolved.", type="positive")
                _refresh_status_summary()
                _refresh_review_queue()
                _refresh_memories()

            def _filtered_memories(rows: list[dict] | None = None) -> list[dict]:
                rows = list(rows if rows is not None else _load_memory_rows())
                cat = None if cat_sel.value == "All" else cat_sel.value
                if cat:
                    rows = [m for m in rows if m.get("category", m.get("entity_type")) == cat]
                return audit.filter_memories(
                    rows,
                    status=status_sel.value,
                    source=source_sel.value,
                    tier=tier_sel.value,
                    query=search_input.value,
                )

            def _refresh_review_queue(rows: list[dict] | None = None) -> None:
                review_container.clear()
                rows = audit.filter_memories(
                    rows if rows is not None else _load_memory_rows(),
                    status="Needs review",
                    source="All",
                    tier="All",
                    query="",
                )
                if not rows:
                    return
                with review_container:
                    with _settings_section(
                        "Needs Review",
                        f"{len(rows)} memor{'y' if len(rows) == 1 else 'ies'} waiting for correction.",
                        icon="rate_review",
                        tone="warning",
                    ):
                        for mem in rows[:5]:
                            summary = audit.audit_summary(mem)
                            with ui.column().classes("w-full gap-1 q-pb-sm").style(
                                "border-bottom: 1px solid rgba(148, 163, 184, 0.18);"
                            ):
                                with ui.row().classes("w-full items-center gap-2"):
                                    ui.label(mem.get("subject", "(untitled)")).classes("text-sm text-weight-medium")
                                    _render_audit_badges(summary)
                                if summary.get("review_reason"):
                                    ui.label(summary["review_reason"]).classes("text-xs text-orange-4")
                                with ui.row().classes("gap-2"):
                                    def _edit_review(mid=mem["id"]):
                                        _open_knowledge_editor(mid)

                                    ui.button("Edit", icon="edit", on_click=_edit_review).props("flat dense no-caps")
                                    ui.button(
                                        "Resolve",
                                        icon="check",
                                        on_click=lambda mid=mem["id"]: _status_action(mid, "resolve"),
                                    ).props("flat dense color=positive no-caps")
                                    ui.button(
                                        "Archive",
                                        icon="archive",
                                        on_click=lambda mid=mem["id"]: _status_action(mid, "archive"),
                                    ).props("flat dense color=grey no-caps")
                        if len(rows) > 5:
                            ui.label(f"+{len(rows) - 5} more in Browse knowledge").classes("text-xs text-grey-6")

            def _refresh_memories(rows: list[dict] | None = None):
                mem_container.clear()
                memories = _filtered_memories(rows)
                visible = memories[: _browse_page["limit"]]
                with mem_container:
                    if not memories:
                        ui.label("No matching entries.").classes("text-grey-6")
                    else:
                        ui.label(
                            f"Showing {len(visible)} of {len(memories)} matching entries."
                        ).classes("text-xs text-grey-6 q-mb-xs")
                        for mem in visible:
                            _mem_id = mem["id"]
                            summary = audit.audit_summary(mem)
                            _header_label = (
                                f"**{mem['subject']}** — "
                                f"_{mem.get('category', mem.get('entity_type', ''))}_"
                            )
                            if _bulk_mem.active:
                                with ui.row().classes("w-full items-center no-wrap").style(
                                    "gap: 4px;"
                                ):
                                    _cb = ui.checkbox(
                                        value=_bulk_mem.is_selected(_mem_id),
                                    )
                                    _cb.on(
                                        "update:model-value",
                                        lambda e, i=_mem_id: _bulk_mem.toggle_item(
                                            i, bool(e.args),
                                        ),
                                    )
                                    _entry_container = ui.expansion(
                                        _header_label,
                                    ).classes("col-grow")
                            else:
                                _entry_container = ui.expansion(_header_label).classes("w-full")
                            with _entry_container:
                                with ui.row().classes("gap-2 q-mb-xs"):
                                    _render_audit_badges(summary)
                                content = mem.get("content", mem.get("description", ""))
                                ui.markdown(content, extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                                aliases = mem.get("aliases", "")
                                if aliases:
                                    ui.label(f"Aliases: {aliases}").classes("text-xs text-grey-6")
                                tags = mem.get("tags", "")
                                if tags:
                                    ui.label(f"Tags: {tags}").classes("text-xs text-grey-6")
                                try:
                                    rels = kg.get_relations(mem["id"])
                                    if rels:
                                        rel_lines = []
                                        for r in rels[:5]:
                                            arrow = "→" if r["direction"] == "outgoing" else "←"
                                            rel_lines.append(f"{arrow} {r['relation_type']}: {r['peer_subject']}")
                                        rel_text = " · ".join(rel_lines)
                                        if len(rels) > 5:
                                            rel_text += f" … +{len(rels) - 5} more"
                                        ui.label(f"🔗 {rel_text}").classes("text-xs text-blue-4")
                                except Exception:
                                    pass
                                ui.label(
                                    f"ID: {mem['id']} · Created: {mem['created_at'][:16]} · Updated: {mem['updated_at'][:16]}"
                                ).classes("text-xs text-grey-6")

                                _render_audit_details(mem, summary)

                                def _del_mem(mid=mem["id"]):
                                    memory_db.delete_memory(mid)
                                    ui.notify("Entry deleted.", type="info")
                                    _refresh_status_summary()
                                    _refresh_review_queue()
                                    _refresh_memories()

                                ui.button("🗑️ Delete", on_click=_del_mem).props("flat dense color=negative")

                                def _edit_mem(mid=mem["id"]):
                                    _open_knowledge_editor(mid)

                                ui.button("✏️ Edit", on_click=_edit_mem).props("flat dense")

                                if summary["status"] == "archived":
                                    ui.button(
                                        "Restore",
                                        icon="unarchive",
                                        on_click=lambda mid=mem["id"]: _status_action(mid, "restore"),
                                    ).props("flat dense color=positive no-caps")
                                else:
                                    ui.button(
                                        "Archive",
                                        icon="archive",
                                        on_click=lambda mid=mem["id"]: _status_action(mid, "archive"),
                                    ).props("flat dense color=grey no-caps")
                                if summary["status"] == "needs_review":
                                    ui.button(
                                        "Resolve",
                                        icon="check",
                                        on_click=lambda mid=mem["id"]: _status_action(mid, "resolve"),
                                    ).props("flat dense color=positive no-caps")

                        if len(visible) < len(memories):
                            def _load_more() -> None:
                                _browse_page["limit"] += 25
                                _refresh_memories()

                            ui.button(
                                "Load more",
                                icon="expand_more",
                                on_click=_load_more,
                            ).props("flat dense no-caps color=primary")

            def _do_mem_bulk_delete(ids: list[str]) -> None:
                def _commit():
                    deleted, failures = memory_db.delete_memories(ids)
                    msg = f"🗑️ Deleted {deleted} entr{'ies' if deleted != 1 else 'y'}."
                    if failures:
                        msg += f" {len(failures)} failed."
                    ui.notify(msg, type="negative" if failures else "info")
                    _refresh_status_summary()
                    _refresh_review_queue()
                    _refresh_memories()

                noun = "entry" if len(ids) == 1 else "entries"
                confirm_destructive(
                    f"Delete {len(ids)} {noun}?",
                    body="This cannot be undone.",
                    on_confirm=_commit,
                )

            render_bulk_action_bar(
                _bulk_mem,
                on_delete=_do_mem_bulk_delete,
                label_singular="entry",
                label_plural="entries",
                on_clear=_refresh_memories,
            )

            def _schedule_mem_refresh(reset: bool = True) -> None:
                token = _browse_generation.next()
                if reset:
                    _browse_page["limit"] = 25

                def _run() -> None:
                    if _browse_generation.is_current(token):
                        _refresh_memories()

                defer_ui(_run, delay=0.3)

            cat_sel.on("update:model-value", lambda _: _schedule_mem_refresh())
            status_sel.on("update:model-value", lambda _: _schedule_mem_refresh())
            source_sel.on("update:model-value", lambda _: _schedule_mem_refresh())
            tier_sel.on("update:model-value", lambda _: _schedule_mem_refresh())
            search_input.on("update:model-value", lambda _: _schedule_mem_refresh())
            _refresh_review_queue()
            _refresh_memories()

            trace_loaded = {"value": False}
            with ui.expansion("Recent recall decisions", icon="history", value=False).classes("w-full q-mt-md") as trace_exp:
                trace_container = ui.column().classes("w-full gap-1")
                with trace_container:
                    ui.label("Open to load recent recall decisions.").classes("text-grey-6 text-sm")

            def _trace_item_ids(traces: list[dict]) -> list[str]:
                ids: list[str] = []
                for row in traces:
                    for mid in row.get("selected_ids") or []:
                        if mid:
                            ids.append(str(mid))
                    for key in ("top_scores", "rejected"):
                        for item in row.get(key) or []:
                            if isinstance(item, dict) and item.get("id") and not item.get("subject"):
                                ids.append(str(item["id"]))
                return list(dict.fromkeys(ids))[:100]

            def _trace_subjects(traces: list[dict]) -> dict[str, str]:
                ids = _trace_item_ids(traces)
                if not ids:
                    return {}
                try:
                    if hasattr(memory_db, "list_memory_subjects"):
                        return memory_db.list_memory_subjects(ids)
                    subjects: dict[str, str] = {}
                    for mid in ids[:50]:
                        mem = memory_db.get_memory(mid)
                        if mem and mem.get("subject"):
                            subjects[mid] = str(mem["subject"])
                    return subjects
                except Exception:
                    logger.debug("Could not resolve recall trace subjects", exc_info=True)
                    return {}

            def _trace_memory_label(item: dict, subjects: dict[str, str]) -> str:
                mid = str(item.get("id") or "").strip()
                subject = str(item.get("subject") or subjects.get(mid) or "").strip()
                if subject:
                    return subject if len(subject) <= 80 else subject[:77].rstrip() + "..."
                return mid

            def _load_trace_rows(e=None) -> None:
                if trace_loaded["value"] or not getattr(trace_exp, "value", False):
                    return
                trace_loaded["value"] = True
                trace_container.clear()
                traces = audit.load_recent_recall_traces(limit=10)
                trace_subjects = _trace_subjects(traces)
                with trace_container:
                    if not traces:
                        ui.label("No recall trace entries yet.").classes("text-grey-6 text-sm")
                    else:
                        for row in reversed(traces):
                            state_label = "used" if row.get("allowed") else "skipped"
                            selected = row.get("selected_count", len(row.get("selected_ids", []) or []))
                            block_chars = row.get("block_chars", 0)
                            with ui.column().classes("w-full gap-1 q-pb-sm").style(
                                "border-bottom: 1px solid rgba(148, 163, 184, 0.18);"
                            ):
                                ui.label(
                                    f"{(row.get('ts') or '')[:19]} | {state_label} | {row.get('reason', '')}"
                                ).classes("text-xs text-weight-medium")
                                ui.label(
                                    f"candidates: {row.get('candidates_seen', 0)} | selected: {selected} | context chars: {block_chars}"
                                ).classes("text-xs text-grey-6")
                                labels = []
                                for item in (row.get("top_scores") or [])[:3]:
                                    if isinstance(item, dict):
                                        label = _trace_memory_label(item, trace_subjects)
                                        labels.append(f"{label}: {item.get('final', item.get('score', ''))}")
                                if labels:
                                    ui.label("Top: " + " | ".join(labels)).classes("text-xs text-grey-6")
                                reasons = []
                                for item in (row.get("rejected") or [])[:3]:
                                    if isinstance(item, dict) and item.get("reason"):
                                        reasons.append(str(item.get("reason")))
                                if reasons:
                                    ui.label("Rejected: " + ", ".join(reasons)).classes("text-xs text-grey-6")

            trace_exp.on("update:model-value", _load_trace_rows)

            journal_loaded = {"value": False}
            with ui.expansion("Memory change log", icon="receipt_long", value=False).classes("w-full") as journal_exp:
                journal_container = ui.column().classes("w-full gap-1")
                with journal_container:
                    ui.label("Open to load memory changes.").classes("text-grey-6 text-sm")

            def _journal_item_ids(rows: list[dict]) -> list[str]:
                ids: list[str] = []
                for row in rows:
                    for mid in row.get("entity_ids") or ([row.get("entity_id")] if row.get("entity_id") else []):
                        if mid:
                            ids.append(str(mid))
                return list(dict.fromkeys(ids))[:100]

            def _journal_subjects(rows: list[dict]) -> dict[str, str]:
                ids = _journal_item_ids(rows)
                if not ids:
                    return {}
                try:
                    if hasattr(memory_db, "list_memory_subjects"):
                        return memory_db.list_memory_subjects(ids)
                    subjects: dict[str, str] = {}
                    for mid in ids[:50]:
                        mem = memory_db.get_memory(mid)
                        if mem and mem.get("subject"):
                            subjects[mid] = str(mem["subject"])
                    return subjects
                except Exception:
                    logger.debug("Could not resolve memory journal subjects", exc_info=True)
                    return {}

            def _journal_action_label(action: str) -> str:
                return {
                    "user_modified": "User edit",
                    "set_status": "Status change",
                    "supersede": "Superseded",
                }.get(str(action or "").strip(), str(action or "Change").replace("_", " ").title())

            def _journal_reason_label(reason: str) -> str:
                return {
                    "high_authority_update": "Manual edit saved as authoritative",
                    "manual_memory_tool_update": "Updated through the memory tool",
                }.get(str(reason or "").strip(), str(reason or "").replace("_", " "))

            def _journal_status_label(row: dict) -> str:
                old_status = str(row.get("old_status") or "").strip()
                new_status = str(row.get("new_status") or "").strip()
                if old_status and new_status and old_status != new_status:
                    return f"{old_status} -> {new_status}"
                if new_status:
                    return f"status: {new_status}"
                return ""

            def _journal_subject_label(ids: list[str], subjects: dict[str, str]) -> str:
                labels = [subjects.get(mid) or mid for mid in ids[:3]]
                if len(ids) > 3:
                    labels.append(f"+{len(ids) - 3} more")
                return " | ".join(labels)

            def _load_journal_rows(e=None) -> None:
                if journal_loaded["value"] or not getattr(journal_exp, "value", False):
                    return
                journal_loaded["value"] = True
                journal_container.clear()
                journal = audit.load_recent_evolution_journal(limit=20)
                journal_subjects = _journal_subjects(journal)
                with journal_container:
                    if not journal:
                        ui.label("No memory change entries yet.").classes("text-grey-6 text-sm")
                    else:
                        for row in reversed(journal):
                            ids = row.get("entity_ids") or ([row.get("entity_id")] if row.get("entity_id") else [])
                            ids = [str(mid) for mid in ids if mid]
                            status_text = _journal_status_label(row)
                            header_parts = [
                                (row.get("timestamp") or "")[:19],
                                _journal_action_label(str(row.get("action") or "")),
                                str(row.get("actor") or "").title(),
                            ]
                            if status_text:
                                header_parts.append(status_text)
                            ui.label(
                                " | ".join([part for part in header_parts if part])
                            ).classes("text-xs text-weight-medium")
                            subject_text = _journal_subject_label(ids, journal_subjects)
                            reason = _journal_reason_label(str(row.get("reason") or ""))
                            detail_parts = [part for part in (subject_text, reason) if part]
                            if detail_parts:
                                ui.label(" | ".join(detail_parts)).classes("text-xs text-grey-6")

            journal_exp.on("update:model-value", _load_journal_rows)

        # ── Danger zone ──────────────────────────────────────────────
        ui.separator()

        _deleting_knowledge = False

        async def _delete_all_knowledge():
            nonlocal _deleting_knowledge
            if _deleting_knowledge:
                return
            _deleting_knowledge = True
            try:
                confirm = await ui.run_javascript(
                    "confirm('Delete ALL knowledge? This will erase all entities, relations, wiki files, and document indexes. This cannot be undone.')",
                    timeout=30,
                )
                if confirm:
                    memory_db.delete_all_memories()
                    reset_vector_store()
                    wiki_vault.clear_wiki_folder()
                    ui.notify("All knowledge deleted.", type="info")
                    _reopen("Knowledge")
            finally:
                _deleting_knowledge = False

        with ui.row().classes("w-full"):
            ui.button("🗑️ Delete all knowledge", on_click=_delete_all_knowledge).props("flat color=negative")

    # ── Voice Tab ────────────────────────────────────────────────────

    def _load_voice_model_rows():
        from row_bot.providers.model_catalog import rows_for_surface
        from row_bot.providers.model_catalog_cache import build_cached_model_catalog_rows
        from row_bot.providers.selection import list_quick_choices

        rows = build_cached_model_catalog_rows(
            defaults={},
            quick_choices=list_quick_choices("voice", include_inactive=True),
        )
        return rows_for_surface(rows, "voice")

    def _voice_model_summary_row(
        title: str,
        subtitle: str,
        reason: str,
        *,
        ready: bool,
        provider_label: str,
    ) -> None:
        with ui.row().classes("items-center gap-2 no-wrap w-full q-py-xs").style(
            "border-bottom: 1px solid rgba(148, 163, 184, 0.12);"
        ):
            ui.icon("mic", size="sm").classes("text-primary")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                ui.label(title).classes("text-sm text-weight-medium")
                ui.label(subtitle).classes("text-grey-6 text-xs")
            ui.badge(provider_label, color="blue-grey").props("outline dense")
            ui.badge("ready" if ready else "setup", color="positive" if ready else "orange").props("outline dense").tooltip(reason)

    def _provider_voice_model_row(row) -> None:
        from row_bot.providers.selection import add_quick_choice_for_model

        def _pin_voice_model() -> None:
            add_quick_choice_for_model(
                row.model_id,
                provider_id=row.provider_id,
                display_name=row.display_name,
                source="voice_settings",
                capabilities_snapshot=row.capabilities_snapshot,
                surface="voice",
            )
            ui.notify("Pinned to Voice Models", type="positive")

        can_pin = row.configured and row.installed
        with ui.row().classes("items-center gap-2 no-wrap w-full q-py-xs").style(
            "border-bottom: 1px solid rgba(148, 163, 184, 0.12);"
        ):
            ui.label(row.provider_icon or "AI").classes("text-sm").style("width: 22px; text-align: center;")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                ui.label(row.display_name).classes("text-sm text-weight-medium").style("line-height: 1.15;")
                if row.display_name != row.model_id:
                    ui.label(row.model_id).classes("text-grey-6 text-xs").style(
                        "line-height: 1.15; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                    )
            ui.badge(row.provider_display_name or row.provider_id, color="blue-grey").props("outline dense")
            for task in row.capabilities_snapshot.get("tasks", []):
                ui.badge(str(task).replace("_", " "), color="grey").props("outline dense")
            if not row.configured:
                ui.badge("connect", color="orange").props("outline dense").tooltip("Connect this provider in Providers.")
            elif not row.installed:
                ui.badge("unavailable", color="orange").props("outline dense")
            pin_button = ui.button("Pin", icon="push_pin", on_click=_pin_voice_model).props("flat dense no-caps color=primary")
            if not can_pin:
                pin_button.disable()

    def _build_voice_tab() -> None:
        from row_bot.voice import get_available_whisper_sizes
        from row_bot.voice.runtime import update_voice_runtime_settings
        from row_bot.voice.provider_catalog import (
            build_voice_provider_catalog,
            model_options_for_capability,
            provider_options_for_capability,
            selected_or_default_model,
        )
        from row_bot.voice.openai_realtime import (
            REALTIME_VOICE_OPTIONS,
        )
        from row_bot.tts import VOICE_CATALOG

        _settings_header(
            "Voice",
            "Configure Talk, Dictation, Realtime Talk Voice, normal read-aloud, voice models, and diagnostics.",
            "mic",
        )

        voice_svc = state.voice_service
        runtime_settings = state.voice_runtime_settings
        voice_catalog = build_voice_provider_catalog(voice_service=voice_svc, tts_service=state.tts_service)

        def _set_voice_runtime(**updates) -> None:
            state.voice_runtime_settings = update_voice_runtime_settings(**updates)

        def _set_voice_runtime_and_reopen(**updates) -> None:
            _set_voice_runtime(**updates)
            _reopen("Voice")

        def _provider_value(capability: str, selected_provider: str) -> str:
            options = provider_options_for_capability(voice_catalog, capability)
            if selected_provider in options:
                return selected_provider
            return next(iter(options), "local")

        talk_provider_value = _provider_value("talk", runtime_settings.talk_provider)
        dictation_provider_value = _provider_value("dictation", runtime_settings.dictation_provider)
        speech_output_provider_value = _provider_value("speech_output", runtime_settings.speech_output_provider)
        talk_model_value = selected_or_default_model(
            voice_catalog,
            "talk",
            talk_provider_value,
            runtime_settings.talk_model,
        )
        dictation_model_value = selected_or_default_model(
            voice_catalog,
            "dictation",
            dictation_provider_value,
            runtime_settings.dictation_model,
        )
        speech_output_model_value = selected_or_default_model(
            voice_catalog,
            "speech_output",
            speech_output_provider_value,
            runtime_settings.speech_output_model,
        )

        def _set_talk_provider(provider_id: str) -> None:
            _set_voice_runtime_and_reopen(
                talk_provider=provider_id,
                talk_model=selected_or_default_model(voice_catalog, "talk", provider_id, ""),
            )

        def _set_dictation_provider(provider_id: str) -> None:
            _set_voice_runtime_and_reopen(
                dictation_provider=provider_id,
                dictation_model=selected_or_default_model(voice_catalog, "dictation", provider_id, ""),
            )

        def _set_speech_output_provider(provider_id: str) -> None:
            _set_voice_runtime_and_reopen(
                speech_output_provider=provider_id,
                speech_output_model=selected_or_default_model(voice_catalog, "speech_output", provider_id, ""),
            )

        with _settings_section(
            "Talk",
            f"Continuous voice conversation with normal {APP_DISPLAY_NAME}. Talk may call the LLM, tools, browser automation, and approvals through the existing agent path.",
            icon="record_voice_over",
        ):
            ui.label("Local Talk keeps microphone transcription on this machine. OpenAI Realtime sends live microphone audio to OpenAI while Talk is active.").classes("text-grey-6 text-xs")
            talk_provider_options = provider_options_for_capability(voice_catalog, "talk")
            ui.select(
                label="Talk provider",
                options=talk_provider_options,
                value=talk_provider_value,
                on_change=lambda e: _set_talk_provider(str(e.value)),
            ).classes("w-full").props("dense outlined")
            talk_model_options = model_options_for_capability(voice_catalog, "talk", talk_provider_value)
            if talk_model_options:
                ui.select(
                    label="Talk model",
                    options=talk_model_options,
                    value=talk_model_value,
                    on_change=lambda e: _set_voice_runtime(talk_model=e.value),
                ).classes("w-full").props("dense outlined")
            if talk_provider_value == "local":
                ui.label(f"Local Talk uses local speech-to-text, then sends the finished text through the normal {APP_DISPLAY_NAME} chat path.").classes("text-grey-6 text-xs")
                caption_label = "Local captions"
                caption_copy = "Shows local transcript text while Talk is listening."
            else:
                ui.label(f"Realtime Talk is a live voice-agent session. Substantive work still routes through {APP_DISPLAY_NAME}'s consult/control bridge.").classes("text-grey-6 text-xs")
                caption_label = "Realtime captions"
                caption_copy = "Shows live transcript text while the Realtime session is active."
            ui.switch(
                caption_label,
                value=runtime_settings.captions_enabled,
                on_change=lambda e: _set_voice_runtime(captions_enabled=bool(e.value)),
            )
            ui.label(caption_copy).classes("text-grey-6 text-xs")
            if talk_provider_value == "openai_realtime":
                ui.switch(
                    "Fallback to local Talk if Realtime is unavailable",
                    value=runtime_settings.realtime_fallback_to_local,
                    on_change=lambda e: _set_voice_runtime(realtime_fallback_to_local=bool(e.value)),
                )
                ui.label(f"If Realtime cannot connect, Talk falls back to local microphone transcription plus the normal {APP_DISPLAY_NAME} response flow.").classes("text-grey-6 text-xs")

        if talk_provider_value == "openai_realtime":
            with _settings_section(
                "Realtime Talk Voice",
                "Controls the voice used by the active OpenAI Realtime Talk session.",
                icon="spatial_audio",
            ):
                ui.label("Voice applies to Realtime Talk only. It does not change local Kokoro read-aloud.").classes("text-grey-6 text-xs")
                ui.select(
                    label="Realtime voice",
                    options=REALTIME_VOICE_OPTIONS,
                    value=runtime_settings.realtime_voice,
                    on_change=lambda e: _set_voice_runtime(realtime_voice=e.value),
                ).classes("w-full").props("dense outlined")

        with _settings_section(
            "Dictation",
            "Speech-to-text only. Dictation fills the composer and does not call the LLM until Send.",
            icon="keyboard_voice",
        ):
            ui.label("Dictation is STT-only. It does not call the LLM, tools, or browser until you press Send.").classes("text-grey-6 text-xs")
            ui.select(
                label="Dictation provider",
                options=provider_options_for_capability(voice_catalog, "dictation"),
                value=dictation_provider_value,
                on_change=lambda e: _set_dictation_provider(str(e.value)),
            ).classes("w-full").props("dense outlined")
            dictation_model_options = model_options_for_capability(voice_catalog, "dictation", dictation_provider_value)
            if dictation_model_options:
                ui.select(
                    label="Dictation model",
                    options=dictation_model_options,
                    value=dictation_model_value,
                    on_change=lambda e: _set_voice_runtime(dictation_model=e.value),
                ).classes("w-full").props("dense outlined")
            whisper_sizes = get_available_whisper_sizes()
            whisper_labels = {
                "tiny": "Tiny (~39 MB, fastest)", "base": "Base (~74 MB, balanced)",
                "small": "Small (~244 MB, accurate)", "medium": "Medium (~769 MB, best accuracy)",
            }
            whisper_opts = {s: whisper_labels.get(s, s) for s in whisper_sizes}
            ui.select(
                label="Whisper model size", options=whisper_opts,
                value=voice_svc.whisper_size,
                on_change=lambda e: (setattr(voice_svc, "whisper_size", e.value), _set_voice_runtime(dictation_model=f"local-whisper-{e.value}")),
            ).classes("w-full").props("dense outlined")

        tts = state.tts_service

        with _settings_section(
            "Normal Read-Aloud",
            f"Hear non-Realtime {APP_DISPLAY_NAME} responses aloud using local Kokoro voices.",
            icon="volume_up",
        ):
            ui.label("Normal Read-Aloud is local text chat playback. Realtime Talk uses the separate Realtime Talk Voice settings above.").classes("text-grey-6 text-xs")
            ui.select(
                label="Read-aloud provider",
                options=provider_options_for_capability(voice_catalog, "speech_output"),
                value=speech_output_provider_value,
                on_change=lambda e: _set_speech_output_provider(str(e.value)),
            ).classes("w-full").props("dense outlined")
            speech_model_options = model_options_for_capability(voice_catalog, "speech_output", speech_output_provider_value)
            if speech_model_options:
                ui.select(
                    label="Read-aloud model",
                    options=speech_model_options,
                    value=speech_output_model_value,
                    on_change=lambda e: _set_voice_runtime(speech_output_model=e.value),
                ).classes("w-full").props("dense outlined")
            _status_dot(
                "Kokoro installed" if tts.is_installed() else "Kokoro not installed",
                "ok" if tts.is_installed() else "inactive",
            )
            if not tts.is_installed():
                async def _install_kokoro():
                    note = ui.notification(
                        "Downloading Kokoro TTS model & voices...",
                        type="ongoing",
                        spinner=True,
                        timeout=None,
                    )
                    try:
                        await run.io_bound(tts.download_model)
                    except Exception as exc:
                        logger.error("Kokoro TTS install failed", exc_info=True)
                        ui.notify(f"Kokoro TTS install failed: {exc}", type="negative", close_button=True)
                    else:
                        ui.notify("Kokoro TTS installed", type="positive")
                        _reopen("Voice")
                    finally:
                        note.dismiss()

                ui.button("Install Kokoro TTS", icon="download", on_click=_install_kokoro).classes("w-full").props("no-caps")
            else:
                ui.switch("Enable text-to-speech", value=tts.enabled,
                          on_change=lambda e: setattr(tts, "enabled", e.value))

                voice_opts = {v: VOICE_CATALOG.get(v, v) for v in tts.get_installed_voices()}
                if voice_opts:
                    ui.select(label="Voice", options=voice_opts, value=tts.voice,
                              on_change=lambda e: setattr(tts, "voice", e.value)).classes("w-full").props("dense outlined")

                ui.label("Speech speed").classes("text-sm")
                ui.slider(
                    min=0.5, max=2.0, step=0.1, value=tts.speed,
                    on_change=lambda e: setattr(tts, "speed", e.value),
                ).classes("w-full")

                ui.switch("Auto-speak voice responses", value=tts.auto_speak,
                          on_change=lambda e: setattr(tts, "auto_speak", e.value))

                def _test():
                    tts.speak_now(f"Hello! I'm {APP_DISPLAY_NAME}, your knowledgeable personal agent.")

                ui.button("Test voice", icon="volume_up", on_click=_test).props("flat no-caps")

        with _settings_section(
            "Voice Models",
            "Choose and review models for Talk, Dictation, realtime voice, transcription, and speech output. Provider credentials stay in Providers.",
            icon="graphic_eq",
        ):
            from row_bot.voice.local_provider import local_voice_provider_statuses

            ui.label("Runtime Voice Models").classes("text-subtitle2")
            with ui.column().classes("w-full gap-1"):
                local_statuses = local_voice_provider_statuses(voice_svc, tts)
                _voice_model_summary_row(
                    local_statuses[0].display_name,
                    f"Dictation and local Talk transcription - current size: {voice_svc.whisper_size}",
                    local_statuses[0].reason,
                    ready=local_statuses[0].ready,
                    provider_label="Local",
                )
                _voice_model_summary_row(
                    local_statuses[1].display_name,
                    f"Speech output - current voice: {VOICE_CATALOG.get(tts.voice, tts.voice)}",
                    local_statuses[1].reason,
                    ready=local_statuses[1].ready,
                    provider_label="Local",
                )
                for provider in voice_catalog:
                    if provider.provider_id == "local":
                        continue
                    talk_models = provider.models_for("talk")
                    if not talk_models:
                        continue
                    default_model = provider.default_talk_model or talk_models[0].model_id
                    _voice_model_summary_row(
                        f"{provider.label} Talk",
                        f"Realtime voice-agent default: {default_model}",
                        provider.reason,
                        ready=provider.ready,
                        provider_label=provider.label,
                    )

            ui.separator().classes("q-my-sm")
            with ui.row().classes("items-center justify-between w-full"):
                with ui.column().classes("gap-0"):
                    ui.label("Provider Voice Models").classes("text-subtitle2")
                    ui.label("Voice-only models are kept out of normal chat pickers. Pin them here for the Voice surface.").classes("text-grey-6 text-xs")
                ui.button("Open Providers", icon="vpn_key", on_click=lambda: _reopen("Providers")).props("flat dense no-caps")

            voice_models_container = ui.column().classes("w-full gap-1")

            async def _load_voice_models() -> None:
                voice_models_container.clear()
                with voice_models_container:
                    with ui.row().classes("items-center gap-2 text-grey-6 text-sm"):
                        ui.spinner(size="sm")
                        ui.label("Loading cached voice models...")
                try:
                    rows = await run.io_bound(_load_voice_model_rows)
                except Exception as exc:
                    logger.warning("Could not load voice model rows", exc_info=True)
                    voice_models_container.clear()
                    with voice_models_container:
                        ui.label(f"Could not load voice models: {exc}").classes("text-warning text-sm")
                    return
                voice_models_container.clear()
                with voice_models_container:
                    if not rows:
                        ui.label("No extra provider voice models in the cached model catalog yet. Runtime defaults above can still be ready. Refresh Models after connecting a provider to discover more options.").classes("text-grey-6 text-sm")
                        return
                    for row in rows[:40]:
                        _provider_voice_model_row(row)
                    if len(rows) > 40:
                        ui.label(f"Showing 40 of {len(rows)} cached voice models. Use Models catalog search for the full list.").classes("text-grey-6 text-xs")

            safe_ui_task(_load_voice_models, context="voice model rows load")

        with _settings_section(
            "Diagnostics",
            "Check the local audio stack and provider readiness without moving credentials out of Providers.",
            icon="troubleshoot",
        ):
            _status_dot(
                f"Voice service: {voice_svc.state}",
                "ok" if voice_svc.is_running else "inactive",
            )
            _status_dot(
                "Kokoro speech output installed" if tts.is_installed() else "Kokoro speech output not installed",
                "ok" if tts.is_installed() else "inactive",
            )
            from row_bot.voice.openai_realtime import OpenAIRealtimeProvider

            realtime_status = OpenAIRealtimeProvider().status()
            openai_ready = realtime_status.ready
            active_provider = next((provider for provider in voice_catalog if provider.provider_id == runtime_settings.talk_provider), None)
            _status_dot(
                f"Talk runtime: {(active_provider.label if active_provider else runtime_settings.talk_provider)}",
                "ok" if active_provider and active_provider.ready else "inactive",
            )
            if active_provider:
                ui.label(active_provider.reason).classes("text-grey-6 text-xs")
            _status_dot(
                f"Active voice session: {state.voice_coordinator.transport} / {state.voice_coordinator.state}",
                "ok" if state.voice_coordinator.is_running else "inactive",
            )
            _status_dot(
                f"OpenAI Realtime ready - using {realtime_status.display_name} default" if openai_ready else "OpenAI Realtime not configured",
                "ok" if openai_ready else "inactive",
            )
            snapshot = state.voice_coordinator.diagnostic_snapshot()
            latency = snapshot.get("realtime_latency_ms")
            if isinstance(latency, dict) and latency:
                latency_label = ", ".join(f"{key}: {value}ms" for key, value in latency.items())
                ui.label(f"Realtime latency: {latency_label}").classes("text-grey-6 text-xs")
            latency_summary = snapshot.get("realtime_latency_summary_ms")
            if isinstance(latency_summary, dict) and latency_summary:
                summary_label = ", ".join(f"{key.replace('_', ' ')}: {value}ms" for key, value in latency_summary.items())
                ui.label(f"Turn timing: {summary_label}").classes("text-grey-6 text-xs")
            ui.label(
                f"Active ids: response={snapshot.get('active_realtime_response_id') or 'none'}, generation={snapshot.get('active_row_bot_generation_id') or 'none'}"
            ).classes("text-grey-6 text-xs")
            ui.label("Talk may call LLMs and tools. Realtime sessions can have ongoing provider cost while active.").classes("text-grey-6 text-xs")
            ui.button("Open Providers", icon="vpn_key", on_click=lambda: _reopen("Providers")).props("flat dense no-caps")

    # ── Channels Tab ─────────────────────────────────────────────────

    def _build_channels_tab() -> None:
        from row_bot.channels import registry as _ch_registry
        from row_bot.channels import config as _ch_config

        # ── Messaging Channels ───────────────────────────────────
        _settings_header(
            "Channels",
            f"Connect {APP_DISPLAY_NAME} to external messaging platforms. Tunnel credentials live in System.",
            "forum",
        )

        from row_bot.docs_capture import is_docs_capture, load_docs_capture_demo_state

        if is_docs_capture():
            demo_channels = load_docs_capture_demo_state().get("channels") or []
            configured = sum(
                1 for item in demo_channels if "configured" in str(item.get("status") or "").lower()
            )
            with ui.row().classes("items-center gap-2 q-mb-sm"):
                _metric_chip("configured", configured, icon="settings")
                _metric_chip("running", 0, icon="play_circle")
            ui.label(
                "Display-only demonstration state; no channel credentials are stored and no adapters are started."
            ).classes("text-grey-6 text-xs q-mb-sm")
            for item in demo_channels:
                name = str(item.get("name") or "Channel")
                status = str(item.get("status") or "Not configured")
                with ui.expansion(f"{name} — {status}", icon="chat").classes("w-full"):
                    ui.label(
                        "Add credentials and an approved recipient before starting this channel."
                    ).classes("text-grey-6 text-sm")
            return

        channels = _ch_registry.all_channels()
        if not channels:
            ui.label("No channels registered.").classes("text-grey-6 text-sm")
            return

        configured = sum(1 for ch in channels if ch.is_configured())
        running = sum(1 for ch in channels if ch.is_running())
        with ui.row().classes("items-center gap-2 q-mb-sm"):
            _metric_chip("configured", configured, icon="settings")
            _metric_chip("running", running, icon="play_circle")
            if any(getattr(ch, "needs_tunnel", False) for ch in channels):
                ui.button(
                    "Tunnel credentials are in System",
                    icon="lan",
                    on_click=lambda: _reopen("System"),
                ).props("flat dense no-caps")

        for ch in channels:
            _build_channel_panel(ch, _ch_config, _ch_registry)

    def _build_channel_panel(ch, _ch_config, _ch_registry) -> None:
        """Render a single channel's settings panel, auto-generated from its
        config_fields, capabilities, and setup_guide properties."""

        def _ch_status_text():
            if ch.is_running():
                return "✅ Running"
            if ch.is_configured():
                return "⏸️ Stopped"
            return "⚠️ Not configured"

        icon = ch.icon or "chat"
        with ui.expansion(
            f"{ch.display_name} — {_ch_status_text()}",
            icon=icon,
        ).classes("w-full") as panel:

            # ── Config field inputs ──────────────────────────────────
            field_inputs: dict[str, Any] = {}
            for cf in ch.config_fields:
                refresh = None
                if cf.storage == "env" and cf.env_key:
                    inp, refresh = _channel_secret_input(
                        cf.label,
                        ch.name,
                        cf.env_key,
                        password=cf.field_type == "password",
                    )
                    with ui.row().classes("gap-2"):
                        ui.button(
                            "Save Current",
                            icon="key",
                            on_click=lambda ch_name=ch.name, ev=cf.env_key, display=cf.label, refresh_status=refresh: _import_channel_secret(ch_name, ev, display, refresh_status),
                        ).props("flat dense")
                        ui.button(
                            "Clear",
                            icon="delete",
                            on_click=lambda ch_name=ch.name, ev=cf.env_key, display=cf.label, refresh_status=refresh: _clear_channel_secret(ch_name, ev, display, refresh_status),
                        ).props("flat dense color=negative")
                else:
                    val = _ch_config.get(ch.name, cf.key, cf.default)
                    if cf.field_type == "password":
                        inp = ui.input(
                            label=cf.label, value=val or "",
                            password=True, password_toggle_button=True,
                        ).classes("w-full")
                    elif cf.field_type == "number":
                        inp = ui.number(
                            label=cf.label, value=val or cf.default,
                        ).classes("w-full")
                    elif cf.field_type == "slider":
                        inp = ui.slider(
                            min=cf.slider_min, max=cf.slider_max,
                            step=cf.slider_step, value=val or cf.default,
                        ).classes("w-full")
                    else:
                        inp = ui.input(
                            label=cf.label, value=val or "",
                        ).classes("w-full")

                if cf.help_text:
                    inp.tooltip(cf.help_text)
                field_inputs[cf.key] = (cf, inp, refresh)

            # ── Status indicator ─────────────────────────────────────
            status_container = ui.row().classes("items-center gap-2 mt-2")
            _update_channel_status(status_container, ch)

            def _refresh_header():
                panel._props["label"] = f"{ch.display_name} — {_ch_status_text()}"
                panel.update()

            # ── Save credentials ─────────────────────────────────────
            def _save_creds(ch=ch, inputs=field_inputs):
                for key, (cf, inp, refresh) in inputs.items():
                    raw = inp.value
                    if isinstance(raw, str):
                        raw = raw.strip()
                    if cf.storage == "env" and cf.env_key:
                        if raw:
                            from row_bot.channels.auth_store import set_channel_secret
                            set_channel_secret(ch.name, cf.env_key, str(raw))
                            inp.value = ""
                            inp.update()
                            if refresh:
                                refresh()
                    else:
                        _ch_config.set(ch.name, cf.key, raw)
                _update_channel_status(status_container, ch)
                _refresh_header()
                ui.notify(f"{ch.display_name} credentials saved", type="positive")

            # ── Start / stop ─────────────────────────────────────────
            async def _start_ch(ch=ch, _panel=panel):
                if not ch.is_configured():
                    ui.notify("Please save your credentials first", type="warning")
                    return
                try:
                    ok = await ch.start()
                    if ok:
                        _ch_config.set(ch.name, "auto_start", True)
                        clear_agent_cache()
                        try:
                            from row_bot.channels.thread_notifications import reconcile_pending_channel_notifications

                            delivered = await run.io_bound(
                                lambda: reconcile_pending_channel_notifications(25)
                            )
                            if delivered:
                                ui.notify(
                                    f"Delivered {delivered} pending channel update(s)",
                                    type="positive",
                                )
                        except Exception:
                            logger.debug("Channel notification reconciliation failed", exc_info=True)
                        ui.notify(f"✅ {ch.display_name} started!", type="positive")
                    else:
                        ui.notify(f"⚠️ Could not start {ch.display_name}", type="warning")
                except Exception as exc:
                    _report_manual_channel_start_failure(ch, exc)
                _update_channel_status(status_container, ch)
                _refresh_header()
                # Keep expansion open so QR code / status is visible
                _panel.open()

            async def _stop_ch(ch=ch):
                try:
                    await ch.stop()
                    _ch_config.set(ch.name, "auto_start", False)
                    clear_agent_cache()
                    ui.notify(f"{ch.display_name} stopped", type="info")
                except Exception as exc:
                    ui.notify(f"Error stopping {ch.display_name}: {exc}", type="negative")
                _update_channel_status(status_container, ch)
                _refresh_header()

            with ui.row().classes("gap-2 items-center"):
                ui.button("💾 Save", on_click=_save_creds)
                ui.button("▶️ Start", on_click=_start_ch).props("color=positive")
                ui.button("⏹️ Stop", on_click=_stop_ch).props("color=negative flat")

            # ── Tunnel toggle (webhook channels only) ────────────────
            if ch.needs_tunnel:
                tunnel_val = _ch_config.get(ch.name, "tunnel_enabled", True)
                tunnel_switch = ui.switch(
                    "🔗 Expose via tunnel",
                    value=tunnel_val,
                )
                tunnel_switch.tooltip(
                    "Automatically open a public tunnel for this channel's "
                    "webhook port when it starts."
                )

                def _on_tunnel_toggle(e, ch=ch):
                    _ch_config.set(ch.name, "tunnel_enabled", e.value)
                    ui.notify(
                        f"Tunnel {'enabled' if e.value else 'disabled'} "
                        f"for {ch.display_name}",
                        type="info",
                    )

                tunnel_switch.on("update:model-value", _on_tunnel_toggle)

                # Show live tunnel URL if active
                from row_bot.tunnel import tunnel_manager
                t_url = tunnel_manager.get_url(ch.webhook_port or 0)
                if t_url:
                    with ui.row().classes("items-center gap-2"):
                        ui.label("🌐").classes("text-sm")
                        ui.label(t_url).classes("text-sm text-primary")
                        ui.button(
                            icon="content_copy",
                            on_click=lambda u=t_url: (
                                ui.run_javascript(
                                    f"navigator.clipboard.writeText('{u}')"
                                ),
                                ui.notify("Copied!", type="info"),
                            ),
                        ).props("flat dense size=xs")

            # ── Custom UI hook ───────────────────────────────────────
            if _ch_registry.allows_custom_ui(ch.name):
                ch.build_custom_ui(panel)

            # ── DM Pairing Code ──────────────────────────────────────
            with ui.expansion("🔑 DM Pairing Code").classes("w-full mt-2"):
                ui.label(
                    "Generate a one-time code, then DM it to the bot on "
                    f"{ch.display_name} to authorise your account."
                ).classes("text-sm text-grey-6")
                _pair_code_label = ui.label("").classes(
                    "text-h5 text-weight-bold text-center q-my-sm"
                ).style("letter-spacing: 0.3em; user-select: all;")
                _pair_code_label.visible = False

                def _gen_pair_code(ch=ch, lbl=_pair_code_label):
                    from row_bot.channels.auth import generate_pairing_code
                    code = generate_pairing_code(ch.name)
                    lbl.text = code
                    lbl.visible = True
                    ui.notify(
                        f"Pairing code: {code} — DM this to the bot on {ch.display_name}",
                        type="info",
                        timeout=10000,
                    )

                ui.button(
                    "Generate Code", icon="vpn_key", on_click=_gen_pair_code,
                ).props("flat dense")

            # ── Paired Users ─────────────────────────────────────────
            from row_bot.channels.auth import get_approved_users, revoke_user, get_user_names
            approved = get_approved_users(ch.name)
            user_names = get_user_names(ch.name) if approved else {}
            if approved:
                with ui.expansion(f"👥 Paired Users ({len(approved)})").classes("w-full mt-2") as paired_exp:
                    _paired_list = ui.column().classes("w-full gap-1")

                    def _render_user_row(uid, container, exp, names):
                        name = names.get(uid, "")
                        with ui.row().classes("items-center w-full justify-between"):
                            if name:
                                ui.label(f"{name}").classes("text-sm")
                                ui.label(f"({uid})").classes("text-xs text-grey-5 font-mono")
                            else:
                                ui.label(uid).classes("text-sm font-mono")

                            def _revoke(
                                ch_name=ch.name,
                                user_id=uid,
                                container=container,
                                exp=exp,
                            ):
                                revoke_user(ch_name, user_id)
                                remaining = get_approved_users(ch_name)
                                updated_names = get_user_names(ch_name)
                                container.clear()
                                if remaining:
                                    exp._props["label"] = f"👥 Paired Users ({len(remaining)})"
                                    exp.update()
                                    with container:
                                        for u in remaining:
                                            _render_user_row(u, container, exp, updated_names)
                                else:
                                    exp.set_visibility(False)
                                ui.notify(f"Revoked {user_id}", type="warning")

                            ui.button(
                                icon="person_remove", on_click=_revoke,
                            ).props("flat dense color=negative size=xs")

                    with _paired_list:
                        for uid in approved:
                            _render_user_row(uid, _paired_list, paired_exp, user_names)

            # ── Setup guide ──────────────────────────────────────────
            guide = ch.setup_guide
            if guide:
                with ui.expansion("ⓘ Setup Guide").classes("w-full mt-2"):
                    ui.markdown(
                        guide,
                        extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                    ).classes("text-sm")

    # ══════════════════════════════════════════════════════════════════
    # STATUS HELPERS (used by Channels tab)
    # ══════════════════════════════════════════════════════════════════

    def _update_channel_status(container, ch):
        container.clear()
        with container:
            if ch.is_running():
                ui.icon("check_circle", color="green").classes("text-lg")
                ui.label(f"{ch.display_name} running").classes("text-green text-sm")
            elif ch.is_configured():
                ui.icon("pause_circle", color="blue").classes("text-lg")
                ui.label("Configured — click Start to begin").classes("text-blue text-sm")
            else:
                ui.icon("warning", color="orange").classes("text-lg")
                ui.label("Not configured").classes("text-orange text-sm")

    # ══════════════════════════════════════════════════════════════════
    # PLUGINS TAB
    # ══════════════════════════════════════════════════════════════════

    def _build_plugins_tab() -> None:
        from row_bot.plugins.ui_settings import build_plugins_tab as _build_tab

        def _open_marketplace():
            try:
                from row_bot.plugins.ui_marketplace import open_marketplace_dialog
                open_marketplace_dialog(on_install=lambda: _reopen("Plugins"))
            except Exception as exc:
                logger.warning("Marketplace not available: %s", exc)
                ui.notify("Marketplace not available yet", type="info")

        _build_tab(on_browse_marketplace=_open_marketplace)

    # ══════════════════════════════════════════════════════════════════
    # PREFERENCES TAB
    # ══════════════════════════════════════════════════════════════════

    def _open_migration_wizard_dialog() -> None:
        with ui.dialog().props("maximized") as migration_dlg:
            with ui.card().classes("w-full h-full no-shadow").style(
                "max-width: 64rem; margin: 0 auto;"
            ):
                with ui.row().classes("w-full items-center justify-between px-4 pt-3 pb-1"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("move_up", size="sm")
                        ui.label("Migration").classes("text-h5")
                    ui.button(icon="close", on_click=migration_dlg.close).props("flat round size=sm")

                ui.separator()

                with ui.scroll_area().classes("w-full").style("height: calc(100vh - 76px);"):
                    with ui.column().classes("w-full px-6 py-4"):
                        __import__(
                            "row_bot.ui.migration_wizard",
                            fromlist=["build_migration_wizard_tab"],
                        ).build_migration_wizard_tab()

        migration_dlg.open()

    def _build_preferences_tab() -> None:
        from row_bot.identity import (
            get_identity_config, save_identity_config,
            sanitize_personality, _DEFAULT_NAME, _PERSONALITY_MAX_LEN,
            is_self_improvement_enabled, set_self_improvement_enabled,
        )

        cfg = get_identity_config()

        _settings_header(
            "Preferences",
            "Customize identity, launch behavior, background intelligence, updates, and migration.",
            "tune",
        )

        # ── Name ─────────────────────────────────────────────────
        ui.label("Assistant name").classes("text-subtitle2 q-mt-sm")

        name_input = ui.input(
            label="Name",
            value=cfg["name"],
            validation={
                "Name cannot be empty": lambda v: bool(v and v.strip()),
            },
        ).classes("w-64")

        def _on_name_change(e):
            val = (e.value or "").strip()
            if not val:
                return
            c = get_identity_config()
            c["name"] = val
            save_identity_config(c)
            clear_agent_cache()

        name_input.on("blur", lambda e: _on_name_change(type("E", (), {"value": name_input.value})))

        ui.separator()

        # ── Personality ──────────────────────────────────────────
        ui.label("Personality").classes("text-subtitle2")
        ui.label(
            "Optional short description of how the assistant should behave. "
            f"Max {_PERSONALITY_MAX_LEN} characters."
        ).classes("text-grey-6 text-xs")

        personality_input = ui.textarea(
            label="Personality",
            value=cfg["personality"],
        ).props(f"maxlength={_PERSONALITY_MAX_LEN} counter").classes("w-full")

        def _on_personality_change(e):
            val = sanitize_personality(e.value or "")
            c = get_identity_config()
            c["personality"] = val
            save_identity_config(c)
            clear_agent_cache()
            if val != (e.value or ""):
                personality_input.set_value(val)
                ui.notify("Some text was removed (disallowed patterns)", type="warning")

        personality_input.on(
            "blur",
            lambda e: _on_personality_change(type("E", (), {"value": personality_input.value})),
        )

        ui.separator()

        # ── Preview ──────────────────────────────────────────────
        ui.label("Preview").classes("text-subtitle2")
        preview = ui.label().classes("text-grey-6 text-sm italic")

        def _update_preview():
            n = (name_input.value or _DEFAULT_NAME).strip() or _DEFAULT_NAME
            p_text = sanitize_personality(personality_input.value or "")
            line = f"You are {n}, a knowledgeable personal assistant with access to tools."
            if p_text:
                line += f" {p_text}"
            preview.set_text(line)

        _update_preview()
        name_input.on("update:model-value", lambda _: _update_preview())
        personality_input.on("update:model-value", lambda _: _update_preview())

        ui.separator()

        # ── Self-Improvement Toggle ──────────────────────────────
        ui.label("Self-Improvement").classes("text-subtitle2")
        ui.label(
            "When enabled, the assistant can create and improve skills, "
            "and receives guidance on how to get better at tasks over time."
        ).classes("text-grey-6 text-xs")

        def _on_self_improve_change(e):
            set_self_improvement_enabled(e.value)
            clear_agent_cache()
            ui.notify(
                "Self-improvement enabled" if e.value else "Self-improvement disabled",
                type="info",
            )

        ui.switch(
            "Enable self-improvement",
            value=is_self_improvement_enabled(),
            on_change=_on_self_improve_change,
        )

        # ── Auto-update section ─────────────────────────────────
        _build_window_mode_section()
        _build_dream_cycle_section()

        try:
            from row_bot.ui.update_dialog import build_update_section
            build_update_section()
        except Exception:  # pragma: no cover - defensive
            logger.debug("update section failed to build", exc_info=True)

        ui.separator()

        # ── Migration utility ───────────────────────────────────
        ui.label("Migration").classes("text-subtitle2")
        ui.label(
            f"Import selected data from Hermes Agent or OpenClaw when setting up {APP_DISPLAY_NAME}."
        ).classes("text-grey-6 text-xs")
        ui.button(
            "Open Migration Wizard",
            icon="move_up",
            on_click=_open_migration_wizard_dialog,
        ).props("unelevated no-caps color=primary")

    # ══════════════════════════════════════════════════════════════════
    # DIALOG SHELL
    # ══════════════════════════════════════════════════════════════════

    if mobile:
        ui.html(_MOBILE_SETTINGS_CSS, sanitize=False)
        from row_bot.ui.mobile_settings import (
            build_mobile_plugins_settings,
            build_mobile_providers_settings,
            build_mobile_skills_settings,
        )

        _tab_aliases = {
            "Cloud": "Providers",
            "Google": "Accounts",
            "Gmail": "Accounts",
            "Calendar": "Accounts",
            "Migration": "Preferences",
            "Search": "Tools",
        }
        _mobile_tab_defs: list[tuple[str, str, Callable]] = [
            ("Providers", "cloud", lambda: build_mobile_providers_settings(on_change=lambda: _reopen("Providers"))),
            ("Models", "smart_toy", _build_models_tab),
            ("Knowledge", "psychology", _build_knowledge_tab),
            ("Buddy", "pets", lambda: __import__("row_bot.ui.buddy", fromlist=["build_buddy_settings_tab"]).build_buddy_settings_tab(_reopen)),
            ("Voice", "mic", _build_voice_tab),
            ("System", "terminal", _build_system_access_tab),
            ("Tracker", "checklist", _build_tracker_tab),
            ("Documents", "description", _build_documents_tab),
            ("Tools", "search", _build_tools_tab),
            ("Skills", "auto_fix_high", build_mobile_skills_settings),
            ("Accounts", "group", _build_accounts_tab),
            ("Channels", "forum", _build_channels_tab),
            ("Utilities", "build", _build_utilities_tab),
            ("MCP", "hub", lambda: __import__("row_bot.ui.mcp_settings", fromlist=["build_mcp_settings_tab"]).build_mcp_settings_tab(_reopen)),
            ("Plugins", "extension", build_mobile_plugins_settings),
            ("Preferences", "tune", _build_preferences_tab),
        ]
        _builder_map = {name: builder for name, _icon, builder in _mobile_tab_defs}
        _section_options = {name: name for name, _icon, _builder in _mobile_tab_defs}
        _initial_name = _tab_aliases.get(initial_tab, initial_tab)
        if _initial_name not in _builder_map:
            _initial_name = "Providers"

        def _settings_tab_placeholder(text: str = "Loading settings...") -> None:
            with ui.column().classes("items-center justify-center gap-3 w-full").style("min-height: 260px;"):
                ui.spinner(size="lg").classes("text-blue-400")
                ui.label(text).classes("text-grey-5 text-sm")

        def _settings_tab_error(name: str, exc: BaseException) -> None:
            with ui.column().classes("w-full gap-2"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("error_outline", color="warning")
                    ui.label(f"Could not load {name} settings").classes("text-warning text-subtitle2")
                ui.label(str(exc)).classes("text-grey-6 text-sm")
                ui.button(
                    "Retry",
                    icon="refresh",
                    on_click=lambda name=name: _schedule_settings_tab(name),
                ).props("flat dense no-caps color=primary")

        def _render_settings_tab(name: str, generation: int) -> None:
            if not settings_generation.is_current(generation):
                return
            content_panel.clear()
            with content_panel:
                if not settings_generation.is_current(generation):
                    return
                start = time.perf_counter()
                try:
                    with timed_ui_section(
                        f"settings.mobile.tab.render.{name.lower()}",
                        threshold_ms=UI_SHELL_WARN_MS,
                        tab=name,
                    ):
                        _builder_map[name]()
                except Exception as exc:
                    logger.exception("Mobile settings tab '%s' failed to render", name)
                    _settings_tab_error(name, exc)
                finally:
                    log_ui_perf(
                        f"settings.mobile.tab.load.{name.lower()}",
                        (time.perf_counter() - start) * 1000.0,
                        threshold_ms=UI_SHELL_WARN_MS,
                        tab=name,
                    )

        def _schedule_settings_tab(name: str | None) -> None:
            if not name or name not in _builder_map:
                return
            generation = settings_generation.next()
            content_panel.clear()
            with content_panel:
                _settings_tab_placeholder(f"Loading {name} settings...")
            defer_ui(lambda name=name, generation=generation: _render_settings_tab(name, generation), delay=0.01)

        p.settings_dlg.clear()
        with p.settings_dlg:
            with ui.card().classes("row-bot-settings-mobile-shell no-shadow").props(
                "data-docs-id=settings-dialog data-mobile-settings=true"
            ):
                with ui.row().classes("row-bot-settings-mobile-header w-full items-center gap-2 no-wrap"):
                    ui.icon("settings", size="sm")
                    with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                        ui.label("Settings").classes("text-subtitle1 ellipsis")
                        ui.label("Full settings").classes("text-grey-6 text-xs")
                    ui.button(icon="close", on_click=_close_settings).props("flat dense round")
                with ui.column().classes("row-bot-settings-mobile-picker w-full gap-1"):
                    section_select = ui.select(
                        options=_section_options,
                        value=_initial_name,
                        label="Section",
                    ).classes("w-full").props("outlined dense data-docs-id=settings-mobile-section-select")
                content_panel = ui.column().classes("row-bot-settings-mobile-content w-full").props(
                    "data-docs-id=settings-mobile-tab-panel"
                )
                with content_panel:
                    _settings_tab_placeholder()

                section_select.on_value_change(lambda e: _schedule_settings_tab(str(e.value or "")))

        p.settings_dlg.open()
        log_ui_perf(
            "settings.mobile.open.shell",
            (time.perf_counter() - shell_started) * 1000.0,
            threshold_ms=UI_SHELL_WARN_MS,
            initial_tab=_initial_name,
        )
        defer_ui(lambda: _schedule_settings_tab(_initial_name), delay=0.05)
        return

    p.settings_dlg.clear()
    with p.settings_dlg:
        with ui.card().classes("w-full h-full no-shadow").props("data-docs-id=settings-dialog").style(
            "max-width: 64rem; margin: 0 auto;"
        ):
            with ui.row().classes("w-full items-center justify-between px-4 pt-3 pb-1"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("settings", size="sm")
                    ui.label("Settings").classes("text-h5")
                ui.button(icon="close", on_click=_close_settings).props("flat round size=sm")

            ui.separator()

            _tab_aliases = {
                "Cloud": "Providers",
                "Google": "Accounts",
                "Gmail": "Accounts",
                "Calendar": "Accounts",
                "Migration": "Preferences",
                "Search": "Tools",
            }
            _known_tab_names = {
                "Providers", "Models", "Knowledge", "Buddy", "Voice",
                "System", "Tracker", "Documents", "Tools", "Skills",
                "Accounts", "Channels", "Utilities", "MCP", "Plugins",
                "Preferences",
            }
            _initial_name = _tab_aliases.get(initial_tab, initial_tab)
            if _initial_name not in _known_tab_names:
                _initial_name = "Providers"
            _tab_map = {}
            with ui.splitter(value=18).classes("w-full flex-grow").props(
                "disable"
            ).style("height: calc(100vh - 100px);") as splitter:
                with splitter.before:
                    with ui.tabs(value=_initial_name).props("vertical data-docs-id=settings-tabs").classes("w-full h-full") as tabs:
                        tab_cloud = ui.tab("Providers", icon="cloud").props("data-docs-id=settings-tab-providers")
                        tab_models = ui.tab("Models", icon="smart_toy").props("data-docs-id=settings-tab-models")
                        tab_knowledge = ui.tab("Knowledge", icon="psychology").props("data-docs-id=settings-tab-knowledge")
                        tab_buddy = ui.tab("Buddy", icon="pets").props("data-docs-id=settings-tab-buddy")
                        tab_voice = ui.tab("Voice", icon="mic").props("data-docs-id=settings-tab-voice")
                        tab_fs = ui.tab("System", icon="terminal").props("data-docs-id=settings-tab-system")
                        tab_tracker = ui.tab("Tracker", icon="checklist").props("data-docs-id=settings-tab-tracker")
                        tab_docs = ui.tab("Documents", icon="description").props("data-docs-id=settings-tab-documents")
                        tab_tools = ui.tab("Tools", icon="search").props("data-docs-id=settings-tab-search")
                        tab_skills = ui.tab("Skills", icon="auto_fix_high").props("data-docs-id=settings-tab-skills")
                        tab_accounts = ui.tab("Accounts", icon="group").props("data-docs-id=settings-tab-accounts")
                        tab_channels = ui.tab("Channels", icon="forum").props("data-docs-id=settings-tab-channels")
                        tab_utils = ui.tab("Utilities", icon="build").props("data-docs-id=settings-tab-utilities")
                        tab_mcp = ui.tab("MCP", icon="hub").props("data-docs-id=settings-tab-mcp")
                        tab_plugins = ui.tab("Plugins", icon="extension").props("data-docs-id=settings-tab-plugins")
                        tab_prefs = ui.tab("Preferences", icon="tune").props("data-docs-id=settings-tab-preferences")
                        _tab_map = {
                            "Models": tab_models, "Cloud": tab_cloud, "Providers": tab_cloud,
                            "Knowledge": tab_knowledge,
                            "Buddy": tab_buddy,
                            "Voice": tab_voice,
                            "System": tab_fs, "Tracker": tab_tracker,
                            "Documents": tab_docs, "Search": tab_tools, "Tools": tab_tools,
                            "Skills": tab_skills,
                            "Google": tab_accounts,
                            "Gmail": tab_accounts, "Calendar": tab_accounts,
                            "Accounts": tab_accounts,
                            "Channels": tab_channels, "Utilities": tab_utils,
                            "MCP": tab_mcp,
                            "Migration": tab_prefs,
                            "Plugins": tab_plugins,
                            "Preferences": tab_prefs,
                        }

                # ── Lazy tab loading (build only visible tab) ──
                _tab_defs = [
                    (tab_cloud, "Providers", _build_cloud_tab),
                    (tab_models, "Models", _build_models_tab),
                    (tab_docs, "Documents", _build_documents_tab),
                    (tab_tools, "Tools", _build_tools_tab),
                    (tab_skills, "Skills", _build_skills_tab),
                    (tab_fs, "System", _build_system_access_tab),
                    (tab_accounts, "Accounts", _build_accounts_tab),
                    (tab_utils, "Utilities", _build_utilities_tab),
                    (tab_tracker, "Tracker", _build_tracker_tab),
                    (tab_knowledge, "Knowledge", _build_knowledge_tab),
                    (tab_buddy, "Buddy", lambda: __import__("row_bot.ui.buddy", fromlist=["build_buddy_settings_tab"]).build_buddy_settings_tab(_reopen)),
                    (tab_voice, "Voice", _build_voice_tab),
                    (tab_channels, "Channels", _build_channels_tab),
                    (tab_mcp, "MCP", lambda: __import__("row_bot.ui.mcp_settings", fromlist=["build_mcp_settings_tab"]).build_mcp_settings_tab(_reopen)),
                    (tab_plugins, "Plugins", _build_plugins_tab),
                    (tab_prefs, "Preferences", _build_preferences_tab),
                ]
                _builder_map: dict[str, Callable] = {}

                def _tab_name_for_value(value) -> str | None:
                    if isinstance(value, str):
                        return _tab_aliases.get(value, value)
                    for _tab_obj, _tab_name, _ in _tab_defs:
                        if value is _tab_obj:
                            return _tab_name
                    return None

                def _settings_tab_placeholder(text: str = "Loading settings...") -> None:
                    with ui.column().classes("items-center justify-center gap-3 w-full").style("min-height: 320px;"):
                        ui.spinner(size="lg").classes("text-blue-400")
                        ui.label(text).classes("text-grey-5 text-sm")

                def _settings_tab_error(name: str, exc: BaseException) -> None:
                    with ui.column().classes("w-full gap-2"):
                        with ui.row().classes("items-center gap-2"):
                            ui.icon("error_outline", color="warning")
                            ui.label(f"Could not load {name} settings").classes("text-warning text-subtitle2")
                        ui.label(str(exc)).classes("text-grey-6 text-sm")
                        ui.button(
                            "Retry",
                            icon="refresh",
                            on_click=lambda name=name: _schedule_settings_tab(name),
                        ).props("flat dense no-caps color=primary")

                def _render_settings_tab(name: str, generation: int) -> None:
                    if not settings_generation.is_current(generation):
                        return
                    content_panel.clear()
                    with content_panel:
                        if not settings_generation.is_current(generation):
                            return
                        start = time.perf_counter()
                        try:
                            with timed_ui_section(
                                f"settings.tab.render.{name.lower()}",
                                threshold_ms=UI_SHELL_WARN_MS,
                                tab=name,
                            ):
                                _builder_map[name]()
                        except Exception as exc:
                            logger.exception("Settings tab '%s' failed to render", name)
                            try:
                                from row_bot.stability import record_ui_callback_error

                                record_ui_callback_error(f"settings.tab.{name}", exc)
                            except Exception:
                                logger.debug("Could not record settings tab failure", exc_info=True)
                            _settings_tab_error(name, exc)
                        finally:
                            log_ui_perf(
                                f"settings.tab.load.{name.lower()}",
                                (time.perf_counter() - start) * 1000.0,
                                threshold_ms=UI_SHELL_WARN_MS,
                                tab=name,
                            )

                def _schedule_settings_tab(name: str | None) -> None:
                    if not name or name not in _builder_map:
                        return
                    generation = settings_generation.next()
                    content_panel.clear()
                    with content_panel:
                        _settings_tab_placeholder(f"Loading {name} settings...")
                    defer_ui(lambda name=name, generation=generation: _render_settings_tab(name, generation), delay=0.01)

                with splitter.after:
                    content_panel = ui.column().classes("w-full h-full px-6 py-4 overflow-auto").props("data-docs-id=settings-tab-panel")
                    for _t_obj, _t_name, _t_builder in _tab_defs:
                        _builder_map[_t_name] = _t_builder
                    with content_panel:
                        _settings_tab_placeholder()

                    def _on_tab_switch(e):
                        name = _tab_name_for_value(e.value)
                        if name and name in _builder_map:
                            _schedule_settings_tab(name)

                    tabs.on_value_change(_on_tab_switch)

    p.settings_dlg.open()
    log_ui_perf(
        "settings.open.shell",
        (time.perf_counter() - shell_started) * 1000.0,
        threshold_ms=UI_SHELL_WARN_MS,
        initial_tab=_initial_name,
    )
    defer_ui(lambda: _schedule_settings_tab(_initial_name), delay=0.05)


def _report_manual_channel_start_failure(channel: Any, exc: Exception) -> None:
    """Log a credential-safe diagnostic and preserve the existing notification."""
    logger.warning(
        "Manual channel start failed: channel=%s error_type=%s",
        channel.name,
        type(exc).__name__,
    )
    ui.notify(f"Error starting {channel.display_name}: {exc}", type="negative")
