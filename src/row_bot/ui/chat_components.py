"""Reusable chat UI components shared between the main chat and the Designer.

Extracted from ``ui.chat`` so both the normal chat view and the Designer
editor can use the same input bar, file upload, and message area.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import sys
import time
from typing import Any, Callable

from row_bot.brand import APP_NATIVE_ENV
from nicegui import events, run, ui

from row_bot.ui.state import AppState, P, _active_generations
from row_bot.ui.constants import ALLOWED_UPLOAD_SUFFIXES
from row_bot.ui.performance import log_ui_perf
from row_bot.ui.streaming import request_generation_stop
from row_bot.ui.timer_utils import defer_ui

logger = logging.getLogger(__name__)


_MODEL_PICKER_CACHE_TTL_SECONDS = 60.0
_model_picker_options_cache: dict[str, Any] = {
    "signature": None,
    "loaded_at": 0.0,
    "options": [],
    "diagnostics": {},
}
_model_picker_options_refresh_task: asyncio.Task | None = None
_model_picker_options_last_diagnostics: dict[str, Any] = {}
_composer_css_added = False


def _bind_shared_transcript_state(
    p: P,
    state: AppState,
    messages: list[dict] | None,
) -> None:
    """Bind a shared chat surface to the transcript reconciliation state."""

    from row_bot.ui.transcript import message_keys

    rows = list(messages or [])
    p.transcript_thread_id = str(state.thread_id or "")
    p.transcript_generation += 1
    p.transcript_rendered_keys = message_keys(rows)
    p.transcript_window_start = 0
    p.transcript_window_size = len(rows)
    p.transcript_total = len(rows)


def ensure_composer_control_css() -> None:
    """Install shared composer toolbar CSS once per process."""

    global _composer_css_added
    if _composer_css_added:
        return
    ui.add_css(
        """
        .row-bot-composer-toolbar {
          min-height: 40px;
          align-items: center;
          flex-wrap: nowrap;
        }
        .row-bot-composer-control-group {
          height: 34px;
          min-height: 34px;
          padding: 2px 6px;
          border-radius: 999px;
          border: 1px solid rgba(255,255,255,0.10);
          background: rgba(255,255,255,0.045);
          display: flex;
          align-items: center;
          gap: 4px;
        }
        .row-bot-composer-voice-group {
          height: 34px;
          min-height: 34px;
          padding: 2px 4px;
          border-radius: 999px;
          border: 1px solid rgba(255,255,255,0.08);
          background: rgba(255,255,255,0.035);
          display: flex;
          align-items: center;
          gap: 2px;
        }
        .row-bot-composer-action-group {
          height: 38px;
          min-height: 38px;
          padding: 2px 4px;
          border-radius: 999px;
          border: 1px solid rgba(255,255,255,0.10);
          background: rgba(255,255,255,0.04);
          display: flex;
          align-items: center;
          gap: 4px;
        }
        .row-bot-composer-icon-button {
          width: 30px;
          height: 30px;
          min-width: 30px;
          min-height: 30px;
          align-self: center;
        }
        .row-bot-composer-icon-button .q-btn__content {
          min-height: 30px;
          line-height: 30px;
        }
        .row-bot-composer-select {
          height: 30px;
          min-height: 30px;
          align-self: center;
        }
        .row-bot-composer-select .q-field__control {
          height: 30px !important;
          min-height: 30px !important;
          padding: 0 2px !important;
          align-items: center !important;
        }
        .row-bot-composer-select .q-field__label {
          display: none !important;
        }
        .row-bot-composer-select .q-field__control-container,
        .row-bot-composer-select .q-field__native,
        .row-bot-composer-select .q-field__input {
          height: 30px !important;
          min-height: 30px !important;
          line-height: 30px !important;
          padding: 0 !important;
          align-items: center !important;
        }
        .row-bot-composer-select .q-field__native span {
          line-height: 30px !important;
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .row-bot-composer-select .q-field__append,
        .row-bot-composer-select .q-field__marginal {
          height: 30px !important;
          min-height: 30px !important;
          padding: 0 !important;
          align-items: center !important;
        }
        .row-bot-composer-select .q-icon {
          font-size: 18px;
          line-height: 30px;
        }
        .row-bot-composer-separator {
          height: 20px;
          opacity: 0.35;
          align-self: center;
        }
        .row-bot-composer-left-gap {
          width: 8px;
          min-width: 8px;
          height: 1px;
        }
        .row-bot-composer-action-divider {
          width: 1px;
          height: 22px;
          margin: 0 3px;
          background: rgba(255,255,255,0.16);
        }
        .row-bot-composer-send-button,
        .row-bot-composer-stop-button {
          width: 34px;
          height: 34px;
          min-width: 34px;
          min-height: 34px;
          align-self: center;
          box-shadow: 0 6px 18px rgba(0,0,0,0.25);
        }
        .row-bot-composer-send-button .q-btn__content,
        .row-bot-composer-stop-button .q-btn__content {
          min-height: 34px;
          line-height: 34px;
        }
        """
    )
    _composer_css_added = True


async def _submit_voice_transcript(send_fn: Callable, text: str) -> None:
    from row_bot.voice.actions import submit_voice_text

    await submit_voice_text(send_fn, text, surface="shared_composer")


def _voice_surface_for_state(state: AppState) -> str:
    if getattr(state, "active_developer_workspace_id", None):
        return "developer"
    if getattr(state, "active_designer_project", None):
        return "designer"
    return "normal_chat"


def _provider_config_signature() -> tuple[str, str]:
    try:
        from row_bot.providers import config as provider_config

        cfg = provider_config.load_provider_config()
        payload = {
            "quick_choices": [
                {
                    key: choice.get(key)
                    for key in (
                        "id",
                        "kind",
                        "provider_id",
                        "model_id",
                        "display_name",
                        "visibility",
                        "pinned",
                        "order",
                        "active",
                        "inactive_reason",
                        "inactive_surfaces",
                        "capabilities_snapshot",
                        "risk_label",
                    )
                    if key in choice
                }
                for choice in cfg.get("quick_choices", [])
                if isinstance(choice, dict)
            ],
            "routes": [
                {
                    key: route.get(key)
                    for key in ("id", "display_name", "enabled", "primary", "fallbacks", "data_policy")
                    if key in route
                }
                for route in cfg.get("routes", [])
                if isinstance(route, dict)
            ],
            "providers": {
                str(provider_id): {
                    key: entry.get(key)
                    for key in (
                        "configured",
                        "source",
                        "auth_method",
                        "fingerprint",
                        "external_reference_exists",
                        "external_reference_path_hash",
                        "runtime_enabled",
                        "base_url",
                        "enabled",
                    )
                    if key in entry
                }
                for provider_id, entry in (cfg.get("providers") or {}).items()
                if isinstance(entry, dict)
            },
            "custom_endpoints": [
                {
                    "id": endpoint.get("id"),
                    "provider_id": endpoint.get("provider_id"),
                    "enabled": endpoint.get("enabled", True),
                    "display_name": endpoint.get("display_name") or endpoint.get("name"),
                    "base_url": endpoint.get("base_url"),
                    "auth_required": endpoint.get("auth_required"),
                    "models": [
                        {
                            key: model.get(key)
                            for key in (
                                "id",
                                "model_id",
                                "display_name",
                                "label",
                                "context_window",
                                "ctx",
                                "capabilities_snapshot",
                                "vision",
                            )
                            if key in model
                        }
                        for model in (endpoint.get("models") or [])
                        if isinstance(model, dict)
                    ],
                }
                for endpoint in cfg.get("custom_endpoints", [])
                if isinstance(endpoint, dict)
            ],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return (str(pathlib.Path(provider_config.CONFIG_PATH)), digest)
    except FileNotFoundError:
        try:
            from row_bot.providers import config as provider_config

            return (str(pathlib.Path(provider_config.CONFIG_PATH)), "")
        except Exception:
            return ("", "")
    except Exception:
        logger.debug("Could not stat provider config for model picker cache", exc_info=True)
        return ("", "")


def _copy_model_picker_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(option) for option in options if isinstance(option, dict)]


def _get_cached_model_picker_options() -> tuple[list[dict[str, Any]], bool, dict[str, Any]] | None:
    options = _model_picker_options_cache.get("options")
    signature = _model_picker_options_cache.get("signature")
    current_signature = _provider_config_signature()
    if not options or signature != current_signature:
        return None
    loaded_at = float(_model_picker_options_cache.get("loaded_at") or 0.0)
    age_ms = max(0.0, (time.monotonic() - loaded_at) * 1000.0)
    stale = age_ms > (_MODEL_PICKER_CACHE_TTL_SECONDS * 1000.0)
    metadata = {
        "cache_hit": True,
        "cache_stale": stale,
        "cache_age_ms": round(age_ms, 1),
        "cache_signature_match": True,
    }
    diagnostics = _model_picker_options_cache.get("diagnostics")
    if isinstance(diagnostics, dict):
        metadata.update({f"cached_{key}": value for key, value in diagnostics.items()})
    return _copy_model_picker_options(options), stale, metadata


def _store_model_picker_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    copied = _copy_model_picker_options(options)
    _model_picker_options_cache.update({
        "signature": _provider_config_signature(),
        "loaded_at": time.monotonic(),
        "options": copied,
        "diagnostics": dict(_model_picker_options_last_diagnostics),
    })
    return _copy_model_picker_options(copied)


def _load_model_picker_options_sync() -> list[dict[str, Any]]:
    global _model_picker_options_last_diagnostics
    from row_bot.providers.selection import list_model_choice_options

    result = list_model_choice_options("chat", return_diagnostics=True)
    if isinstance(result, tuple):
        options, diagnostics = result
        _model_picker_options_last_diagnostics = dict(diagnostics)
        return _copy_model_picker_options(options)
    _model_picker_options_last_diagnostics = {}
    return _copy_model_picker_options(result)


async def _refresh_model_picker_options() -> list[dict[str, Any]]:
    global _model_picker_options_refresh_task

    task = _model_picker_options_refresh_task
    if task is not None and not task.done():
        return await task

    async def _runner() -> list[dict[str, Any]]:
        options = await run.io_bound(_load_model_picker_options_sync)
        return _store_model_picker_options(options)

    task = asyncio.create_task(_runner())
    _model_picker_options_refresh_task = task
    try:
        return await task
    finally:
        if _model_picker_options_refresh_task is task:
            _model_picker_options_refresh_task = None


# Model picker
# MESSAGE AREA (scroll + container + auto-scroll JS)
# Model picker

def build_chat_messages(
    p: P,
    state: AppState,
    *,
    messages: list[dict] | None = None,
    add_chat_message: Callable | None = None,
    placeholder_text: str = "Ask anything...",
    cloud_tint: bool | None = None,
) -> None:
    """Build the scrollable chat message area and wire ``p.chat_scroll`` / ``p.chat_container``.

    Parameters
    ----------
    messages
        Pre-existing messages to render.  Pass ``state.messages`` for the
        normal chat or the current designer thread.
    add_chat_message
        Callback to render a single message dict.  For normal chat this is
        ``lambda msg: add_chat_message(msg, p, thread_id)``.  For the
        Designer it can be ``None`` (messages not rendered here).
    placeholder_text
        Shown when ``messages`` is empty.
    cloud_tint
        ``True`` = orange tint, ``False`` = green tint, ``None`` = neutral.
    """
    if cloud_tint is True:
        _bg = "background: rgba(255, 152, 0, 0.03);"
    elif cloud_tint is False:
        _bg = "background: rgba(76, 175, 80, 0.03);"
    else:
        _bg = ""

    p.chat_scroll = ui.scroll_area().classes("w-full flex-grow").style(_bg)

    with p.chat_scroll:
        p.chat_container = ui.column().classes("w-full gap-2")

    # Render existing messages
    if messages and add_chat_message:
        for msg in messages:
            add_chat_message(msg)
    elif not messages:
        with p.chat_container:
            ui.label(placeholder_text).classes("text-grey-5 text-sm q-pa-md")

    _bind_shared_transcript_state(p, state, messages)

    # Auto-scroll MutationObserver
    if p.chat_scroll:
        p.chat_scroll.scroll_to(percent=1.0)
        _sid = p.chat_scroll.id
        ui.run_javascript(f"""(function(){{
            var el = getElement({_sid});
            if (!el || !el.$el) return;
            var c = el.$el.querySelector('.q-scrollarea__container');
            if (!c || !(c instanceof Node)) return;
            el._tSS = true;
            var uTs = 0;
            c.addEventListener('wheel', function() {{ uTs = Date.now(); }}, {{passive:true}});
            c.addEventListener('touchstart', function() {{ uTs = Date.now(); }}, {{passive:true}});
            c.addEventListener('scroll', function() {{
                if (Date.now() - uTs > 1000) return;
                el._tSS = (c.scrollHeight - c.scrollTop - c.clientHeight) < 50;
            }});
            new MutationObserver(function() {{
                if (el._tSS) c.scrollTop = c.scrollHeight;
            }}).observe(c, {{childList: true, subtree: true, characterData: true}});
        }})()""")


# Upload helpers
# FILE UPLOAD (hidden widget + drag-drop + clipboard paste)
# Upload helpers

def build_file_upload(
    p: P,
    state: AppState,
) -> ui.upload:
    """Build the hidden upload widget and install drag-drop / paste listeners.

    Returns the hidden ``ui.upload`` element so callers can trigger it
    programmatically (e.g. attach button click).
    """

    async def _on_upload(e: events.UploadEventArguments):
        data = await e.file.read()
        name = e.file.name
        p.pending_files.append({"name": name, "data": data})
        if hasattr(e, "sender") and hasattr(e.sender, "reset"):
            e.sender.reset()
        if p.file_chips_row:
            with p.file_chips_row:
                idx = len(p.pending_files) - 1

                def _remove(i=idx, badge=None):
                    if i < len(p.pending_files):
                        p.pending_files.pop(i)
                    if badge:
                        badge.delete()

                b = ui.badge(f"Attached: {name} x", color="grey-8").props("outline")
                b.on("click", lambda b=b, i=idx: _remove(i, b))
                b.style("cursor: pointer;")

    hidden_upload = ui.upload(on_upload=_on_upload, auto_upload=True, multiple=True).classes("hidden")

    # Drag-and-drop (singleton listener - reads dynamic upload ID)
    ui.run_javascript(f"""
        (() => {{
            window._rowBotUploadId = {hidden_upload.id};
            if (window._rowBotDragInstalled) return;
            window._rowBotDragInstalled = true;
            const body = document.body;
            let overlay = null;
            let dragTimer = null;
            function showOverlay() {{
                if (overlay) return;
                overlay = document.createElement("div");
                overlay.style.cssText = "position:fixed;inset:0;z-index:9999;" +
                    "background:rgba(30,136,229,0.15);border:3px dashed #1e88e5;" +
                    "display:flex;align-items:center;justify-content:center;pointer-events:none;";
                overlay.innerHTML = '<div style="color:#1e88e5;font-size:1.5rem;font-weight:600;">Drop files here</div>';
                document.body.appendChild(overlay);
            }}
            function hideOverlay() {{
                if (overlay) {{ overlay.remove(); overlay = null; }}
                if (dragTimer) {{ clearTimeout(dragTimer); dragTimer = null; }}
            }}
            body.addEventListener("dragover", (e) => {{
                e.preventDefault(); showOverlay();
                if (dragTimer) clearTimeout(dragTimer);
                dragTimer = setTimeout(hideOverlay, 300);
            }});
            body.addEventListener("dragleave", (e) => {{
                if (e.relatedTarget === null || !body.contains(e.relatedTarget)) hideOverlay();
            }});
            document.addEventListener("drop", (e) => {{
                hideOverlay();
                const inUploader = e.target.closest && e.target.closest('.q-uploader');
                if (inUploader) return;
                e.preventDefault();
                const files = e.dataTransfer?.files;
                if (!files || files.length === 0) return;
                const vue = getElement(window._rowBotUploadId);
                if (vue && vue.$refs.qRef) vue.$refs.qRef.addFiles(files);
            }}, true);
        }})();
    """)

    # Clipboard image paste (singleton listener - reads dynamic upload ID)
    ui.run_javascript(f"""
        (() => {{
            window._rowBotUploadId = {hidden_upload.id};
            if (window._rowBotPasteInstalled) return;
            window._rowBotPasteInstalled = true;
            document.addEventListener("paste", (e) => {{
                const items = e.clipboardData?.items;
                if (!items) return;
                const imageFiles = [];
                for (const item of items) {{
                    if (item.type.startsWith("image/")) {{
                        const file = item.getAsFile();
                        if (file) {{
                            const ext = file.type.split("/")[1] || "png";
                            const ts = Date.now();
                            const named = new File([file], "pasted_image_" + ts + "." + ext, {{type: file.type}});
                            imageFiles.push(named);
                        }}
                    }}
                }}
                if (imageFiles.length === 0) return;
                e.preventDefault();
                const vue = getElement(window._rowBotUploadId);
                if (vue && vue.$refs.qRef) vue.$refs.qRef.addFiles(imageFiles);
            }});
        }})();
    """)

    return hidden_upload


# Composer
# CHAT INPUT BAR (textarea + buttons + model picker + voice + stop)
# Composer

def build_chat_input_bar(
    p: P,
    state: AppState,
    *,
    send_fn: Callable,
    hidden_upload: ui.upload,
    browse_file: Callable | None = None,
    open_settings: Callable | None = None,
    show_model_picker: bool = True,
    on_model_switch: Callable | None = None,
    composer_extras: Any | None = None,
) -> None:
    """Build the chat input card with textarea, buttons, and optional model picker.

    Parameters
    ----------
    send_fn
        ``async def send_fn(text)`` - called when the user sends a message.
    hidden_upload
        The ``ui.upload`` element from ``build_file_upload`` so the attach
        button can trigger it.
    browse_file
        Native file browser callable (macOS).  ``None`` to skip.
    open_settings
        Called when "More models..." is selected.  ``None`` to skip model picker.
    show_model_picker
        Whether to render the model override dropdown.
    on_model_switch
        Called after the thread model override changes.
    """
    if composer_extras is not None:
        p.refresh_skill_chips = composer_extras.refresh_from_store
    ensure_composer_control_css()

    from row_bot.ui.live_control import build_live_control_dock

    build_live_control_dock(
        state,
        p,
        stop_generation=lambda thread_id: request_generation_stop(
            thread_id,
            state=state,
            p=p,
            reason="live_control",
        ),
    )

    # Attach handler
    async def _on_attach():
        if (sys.platform == "darwin" and os.environ.get(APP_NATIVE_ENV) == "1"
                and browse_file is not None):
            path = await browse_file(
                title="Attach file",
                filetypes=[("Supported files", " ".join(f"*.{e}" for e in ALLOWED_UPLOAD_SUFFIXES))],
            )
            if path and os.path.isfile(path):
                name = os.path.basename(path)
                data = await run.io_bound(pathlib.Path(path).read_bytes)
                p.pending_files.append({"name": name, "data": data})
                if p.file_chips_row:
                    with p.file_chips_row:
                        idx = len(p.pending_files) - 1

                        def _remove(i=idx, badge=None):
                            if i < len(p.pending_files):
                                p.pending_files.pop(i)
                            if badge:
                                badge.delete()

                        b = ui.badge(f"Attached: {name} x", color="grey-8").props("outline")
                        b.on("click", lambda b=b, i=idx: _remove(i, b))
                        b.style("cursor: pointer;")
        else:
            await ui.run_javascript(
                f"document.getElementById('c{hidden_upload.id}').querySelector('input[type=file]').click()"
            )

    # Input card
    with ui.column().classes("w-full shrink-0 gap-0").style(
        "border: 1px solid rgba(255,255,255,0.15); border-radius: 18px; "
        "background: rgba(255,255,255,0.04); padding: 0; overflow: hidden; "
        "position: relative;"
    ):
        # File chips inside the card (top)
        p.file_chips_row = ui.row().classes("w-full flex-wrap gap-1 q-px-md q-pt-sm")

        if composer_extras is not None:
            try:
                composer_extras.render_before_input()
            except Exception:
                logger.debug("Shared composer extras failed to render", exc_info=True)

        # Context counter - absolute overlay, top-right
        with ui.row().classes("items-center gap-1").style(
            "position: absolute; top: 8px; right: 12px; z-index: 1; "
            "pointer-events: none; opacity: 0.7;"
        ):
            with ui.column().classes("gap-0 items-end").style("min-width: 100px;"):
                p.token_label = ui.label("Context: 0K / 32K (0%)").classes("text-xs text-grey-6")
                p.token_bar = ui.linear_progress(value=0, show_value=False).style(
                    "height: 3px; width: 100px;"
                )

        # Textarea
        p.chat_input = (
            ui.textarea(placeholder="Ask anything...")
            .classes("w-full")
            .props(
                'borderless autogrow input-style="padding: 12px 16px 4px 16px; '
                'max-height: 200px; overflow-y: auto;"'
            )
            .style("font-size: 0.95rem;")
        )

        def _register_active_voice_binding() -> None:
            from row_bot.voice.actions import ActiveVoiceSurfaceBinding

            surface = _voice_surface_for_state(state)
            thread_id = str(state.thread_id or "")

            def _get_text() -> str:
                return str(p.chat_input.value or "") if p.chat_input is not None else ""

            def _set_text(value: str) -> None:
                if p.chat_input is None:
                    return
                p.chat_input.value = value
                p.chat_input.update()
                if composer_extras is not None:
                    try:
                        composer_extras.queue_skill_chip_refresh(value)
                    except Exception:
                        logger.debug("Could not sync composer extras text", exc_info=True)

            p.active_voice_binding = ActiveVoiceSurfaceBinding(
                surface=surface,
                thread_id=thread_id,
                get_composer_text=_get_text,
                set_composer_text=_set_text,
                send_talk_text=send_fn,
            )
            logger.info(
                "voice.realtime.pipeline %s",
                {
                    "stage": "active_voice_surface_bound",
                    "surface": surface,
                    "thread_id": thread_id,
                },
            )

        _register_active_voice_binding()

        if composer_extras is not None:
            try:
                composer_extras.attach_input(p.chat_input)
            except Exception:
                logger.debug("Shared composer extras failed to attach input handlers", exc_info=True)

        def _clear_persisted_thread_draft() -> None:
            try:
                from row_bot.threads import delete_thread_draft

                delete_thread_draft(str(state.thread_id or ""))
            except Exception:
                logger.debug("Could not clear persisted thread draft", exc_info=True)

        try:
            from row_bot.threads import load_thread_draft

            draft = load_thread_draft(str(state.thread_id or ""))
            draft_text = str((draft or {}).get("text") or "")
            if draft_text and not str(p.chat_input.value or "").strip():
                p.chat_input.value = draft_text
                p.chat_input.update()
                if composer_extras is not None:
                    composer_extras.queue_skill_chip_refresh(draft_text)
                try:
                    p.chat_input.run_method("focus")
                except Exception:
                    pass
        except Exception:
            logger.debug("Could not restore persisted thread draft", exc_info=True)

        async def _on_send():
            text = p.chat_input.value
            if text and text.strip():
                p.chat_input.value = ""
                _clear_persisted_thread_draft()
                if composer_extras is not None:
                    try:
                        composer_extras.clear_draft_on_send()
                    except Exception:
                        logger.debug("Shared composer extras failed to clear draft on send", exc_info=True)
                if p.chat_scroll:
                    _re = p.chat_scroll.id
                    ui.run_javascript(
                        f"(function(){{ var e=getElement({_re}); if(e) e._tSS=true; }})()"
                    )
                await send_fn(text)
            elif p.pending_files:
                p.chat_input.value = ""
                _clear_persisted_thread_draft()
                if composer_extras is not None:
                    try:
                        composer_extras.clear_draft_on_send()
                    except Exception:
                        logger.debug("Shared composer extras failed to clear attachment draft", exc_info=True)
                await send_fn("")

        # Enter to send; modified Enter keeps native textarea behavior.
        p.chat_input.on(
            "keydown.enter",
            _on_send,
            js_handler="""(e) => {
                if (window._rowBotSlashPaletteOpen) return;
                if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
                e.preventDefault();
                emit();
            }""",
        )

        def _on_stop():
            request_generation_stop(state.thread_id, state=state, p=p, reason="shared_chat")

        # Bottom bar: attach, model/approval, voice, spacer, send, stop
        with ui.row().classes("w-full row-bot-composer-toolbar q-px-sm q-pb-sm q-pt-none gap-1"):
            ui.button(icon="attach_file", on_click=_on_attach).props(
                "flat round dense size=sm"
            ).classes("row-bot-composer-icon-button").tooltip("Attach files")

            ui.element("div").classes("row-bot-composer-left-gap")

            build_composer_policy_cluster(
                state,
                open_settings=open_settings,
                show_model_picker=show_model_picker,
                on_model_switch=on_model_switch,
            )
            from row_bot.ui.voice_realtime_events import make_realtime_event_handler

            _on_realtime_event = make_realtime_event_handler(
                state=state,
                p=p,
                send_message=send_fn,
            )


            p.realtime_event_sink = ui.element("div").style("display:none")
            try:
                p.realtime_client = ui.context.client
            except Exception:
                p.realtime_client = None
            p.realtime_event_sink.on(
                "row-bot-realtime-event",
                _on_realtime_event,
                js_handler="(e) => emit(e.detail)",
            )

            def _start_local_talk() -> None:
                _register_active_voice_binding()
                state.voice_input_mode = "talk"
                state.voice_enabled = True
                state.voice_coordinator.start_talk()
                if p.dictate_btn:
                    _set_dictate_button_active(p, False)

            def _start_realtime_talk() -> None:
                from row_bot.voice.openai_realtime import OpenAIRealtimeProvider
                from row_bot.voice.realtime_client import start_realtime_client_js
                from row_bot.ui.streaming import run_realtime_client_js

                status = OpenAIRealtimeProvider().status()
                if not status.ready:
                    if state.voice_runtime_settings.realtime_fallback_to_local:
                        ui.notify("OpenAI Realtime is not configured. Falling back to local Talk.", type="warning")
                        _start_local_talk()
                    else:
                        ui.notify(status.reason, type="negative", close_button=True)
                        state.voice_enabled = False
                        if p.voice_switch:
                            _set_talk_button_active(p, False)
                    return
                state.voice_input_mode = "talk"
                state.voice_enabled = True
                _register_active_voice_binding()
                session_id = state.voice_coordinator.start_realtime_talk()
                if p.dictate_btn:
                    _set_dictate_button_active(p, False)
                delivered = run_realtime_client_js(
                    p,
                    start_realtime_client_js(
                        sink_id=p.realtime_event_sink.id,
                        session_id=session_id,
                    ),
                    context="shared_start_realtime_talk",
                )
                if not delivered:
                    state.voice_enabled = False
                    state.voice_coordinator.stop()
                    if p.voice_switch:
                        _set_talk_button_active(p, False)

            def _stop_talk() -> None:
                from row_bot.voice.realtime_client import stop_realtime_client_js
                from row_bot.ui.streaming import run_realtime_client_js

                if state.voice_coordinator.transport == "realtime":
                    run_realtime_client_js(p, stop_realtime_client_js(), context="shared_stop_realtime_talk")
                state.voice_enabled = False
                state.voice_coordinator.stop()
                binding = getattr(p, "active_voice_binding", None)
                if binding is not None:
                    binding.clear()
                p.active_voice_binding = None

            def _toggle_voice():
                if not (state.voice_enabled and state.voice_input_mode == "talk"):
                    if state.voice_runtime_settings.talk_provider == "openai_realtime":
                        _start_realtime_talk()
                    else:
                        _start_local_talk()
                    _set_talk_button_active(p, state.voice_enabled and state.voice_input_mode == "talk")
                elif state.voice_input_mode == "talk":
                    _stop_talk()
                    _set_talk_button_active(p, False)

            def _toggle_dictate():
                if state.voice_enabled and state.voice_input_mode == "dictate":
                    state.voice_enabled = False
                    state.voice_coordinator.stop()
                    binding = getattr(p, "active_voice_binding", None)
                    if binding is not None:
                        binding.clear()
                    p.active_voice_binding = None
                    if p.dictate_btn:
                        _set_dictate_button_active(p, False)
                    return
                state.voice_input_mode = "dictate"
                state.voice_enabled = True
                _register_active_voice_binding()
                state.voice_coordinator.start_dictation()
                if p.voice_switch:
                    _set_talk_button_active(p, False)
                if p.dictate_btn:
                    _set_dictate_button_active(p, True)

            p.voice_status_label = ui.label("").classes("text-xs text-grey-6")

            ui.space()

            with ui.row().classes("items-center row-bot-composer-action-group"):
                with ui.row().classes("items-center row-bot-composer-voice-group"):
                    p.voice_switch = ui.button(icon="record_voice_over", on_click=_toggle_voice).props(
                        "flat round dense size=sm"
                    ).classes("row-bot-composer-icon-button").tooltip("Talk")
                    p.voice_switch.value = False
                    _set_talk_button_active(p, state.voice_enabled and state.voice_input_mode == "talk")
                    p.dictate_btn = ui.button(icon="keyboard_voice", on_click=_toggle_dictate).props(
                        "flat round dense size=sm"
                    ).classes("row-bot-composer-icon-button").tooltip("Dictate into the composer")
                    p.dictate_btn.value = False
                    _set_dictate_button_active(p, state.voice_enabled and state.voice_input_mode == "dictate")

                ui.element("div").classes("row-bot-composer-action-divider")

                ui.button(icon="send", on_click=_on_send).props(
                    "color=primary round dense size=sm"
                ).classes("row-bot-composer-send-button").tooltip("Send")

                p.stop_btn = ui.button(icon="stop", on_click=_on_stop).props(
                    "round dense size=sm"
                ).classes("row-bot-composer-stop-button").tooltip("Stop generation")
            _has_active = state.thread_id in _active_generations
            if not _has_active:
                p.stop_btn.disable()


def _compact_select_style(*, min_width: int, max_width: int) -> str:
    return (
        f"min-width: {min_width}px; max-width: {max_width}px; "
        "height: 30px; --q-field-padding: 0;"
    )


def _set_talk_button_active(p: P, active: bool) -> None:
    button = getattr(p, "voice_switch", None)
    if not button:
        return
    button.value = bool(active)
    button.props(
        "color=primary icon=graphic_eq unelevated"
        if active
        else "color=blue-grey-3 icon=record_voice_over"
    )
    try:
        button.update()
    except Exception:
        logger.debug("Could not update Talk button state", exc_info=True)


def _set_dictate_button_active(p: P, active: bool) -> None:
    button = getattr(p, "dictate_btn", None)
    if not button:
        return
    button.value = bool(active)
    button.props(
        "color=primary icon=keyboard_voice unelevated"
        if active
        else "color=blue-grey-3 icon=keyboard_voice"
    )
    try:
        button.update()
    except Exception:
        logger.debug("Could not update Dictate button state", exc_info=True)


def build_composer_policy_cluster(
    state: AppState,
    *,
    open_settings: Callable | None = None,
    show_model_picker: bool = True,
    on_model_switch: Callable | None = None,
    generation_getter: Callable[[], int] | None = None,
    shell_generation: int | None = None,
) -> None:
    """Render the compact model and approval controls as one composer cluster."""

    ensure_composer_control_css()

    with ui.row().classes("items-center row-bot-composer-control-group"):
        if show_model_picker:
            ui.icon("hub", size="18px").classes("text-grey-5")
            _build_inline_model_picker(
                state,
                open_settings=open_settings,
                on_model_switch=on_model_switch,
                generation_getter=generation_getter,
                shell_generation=shell_generation,
            )
            ui.separator().props("vertical").classes("row-bot-composer-separator")
        ui.icon("shield", size="18px").classes("text-grey-5")
        _build_inline_approval_picker(state)


def _build_inline_model_picker(
    state: AppState,
    *,
    open_settings: Callable | None = None,
    on_model_switch: Callable | None = None,
    generation_getter: Callable[[], int] | None = None,
    shell_generation: int | None = None,
) -> None:
    """Compact model picker rendered inside the input bar."""
    from row_bot.agent import clear_agent_cache
    from row_bot.models import (
        get_current_model,
        get_context_policy,
        get_model_max_context,
        CONTEXT_SIZE_LABELS,
    )
    from row_bot.providers.selection import (
        model_choice_value,
        model_id_from_choice_value,
    )

    _cur_default = get_current_model()
    _cur_default_value = model_choice_value(_cur_default)
    _default_opt = "__default__"
    _picker_opts = {_default_opt: f"Default - {model_id_from_choice_value(_cur_default_value) or _cur_default}"}

    _cur_mo = state.thread_model_override or ""
    _cur_mo_value = model_choice_value(_cur_mo)
    if _cur_mo_value and _cur_mo_value != _cur_default_value:
        _picker_opts[_cur_mo_value] = model_id_from_choice_value(_cur_mo_value)

    _LOADING_MODELS_SENTINEL = "__loading_models__"
    _MODELS_UNAVAILABLE_SENTINEL = "__models_unavailable__"
    _picker_opts[_LOADING_MODELS_SENTINEL] = "Loading pinned models..."

    _MORE_MODELS_SENTINEL = "__more_models__"
    if open_settings:
        _picker_opts[_MORE_MODELS_SENTINEL] = "More models..."

    _picker_val = _cur_mo_value if _cur_mo_value and _cur_mo_value in _picker_opts else _default_opt
    _current_picker_value = [_picker_val]
    _loaded_picker_values: set[str] = set()

    def _merge_picker_options(options: list[dict[str, Any]]) -> None:
        for value in list(_loaded_picker_values):
            if value != _cur_mo_value:
                _picker_opts.pop(value, None)
        _loaded_picker_values.clear()
        _picker_opts.pop(_LOADING_MODELS_SENTINEL, None)
        _picker_opts.pop(_MODELS_UNAVAILABLE_SENTINEL, None)
        _picker_opts.pop(_MORE_MODELS_SENTINEL, None)
        for option in options:
            value = str(option.get("value") or "")
            if not value or value == _cur_default_value:
                continue
            _picker_opts[value] = str(option.get("label") or value)
            if value != _cur_mo_value:
                _loaded_picker_values.add(value)
        if open_settings:
            _picker_opts[_MORE_MODELS_SENTINEL] = "More models..."

    from row_bot.docs_capture import docs_capture_model_choices

    docs_options = docs_capture_model_choices()
    cached_options = (docs_options, False, {"source": "docs_capture"}) if docs_options else _get_cached_model_picker_options()
    _cached_picker_stale = True
    if cached_options is not None:
        _cached_options, _cached_picker_stale, _cached_metadata = cached_options
        _merge_picker_options(_cached_options)
        log_ui_perf(
            "chat.model_picker.options.cache",
            0.0,
            threshold_ms=500.0,
            options=len(_cached_options),
            stale=_cached_picker_stale,
            **_cached_metadata,
        )

    async def _on_model_pick(e):
        val = e.value
        if val == _current_picker_value[0]:
            return
        _picker_val = _current_picker_value[0]
        if val == _picker_val:
            return
        if val in (_LOADING_MODELS_SENTINEL, _MODELS_UNAVAILABLE_SENTINEL):
            e.sender.set_value(_current_picker_value[0])
            return
        if val == _MORE_MODELS_SENTINEL:
            e.sender.set_value(_current_picker_value[0])
            if open_settings:
                open_settings("Models")
            return
        from row_bot.threads import _set_thread_model_override
        if val == _default_opt:
            state.thread_model_override = ""
            _set_thread_model_override(state.thread_id, "")
        elif val in _picker_opts:
            runtime_model = model_id_from_choice_value(val)
            if getattr(state, "active_developer_workspace_id", None) or getattr(state, "active_designer_project", None):
                from row_bot.providers.readiness import evaluate_agent_readiness

                readiness = await run.io_bound(lambda: evaluate_agent_readiness(val))
                if not readiness.ready:
                    e.sender.set_value(_current_picker_value[0])
                    ui.notify(
                        f"{runtime_model} is Chat Only or unavailable. This surface requires an Agent-ready model.",
                        type="negative",
                        close_button=True,
                        timeout=10000,
                    )
                    return
            state.thread_model_override = val
            _set_thread_model_override(state.thread_id, val)
        else:
            state.thread_model_override = ""
            _set_thread_model_override(state.thread_id, "")
            val = _default_opt
        _current_picker_value[0] = val
        e.sender.set_value(val)
        clear_agent_cache()
        _eff = state.thread_model_override or get_current_model()
        if on_model_switch:
            on_model_switch()
        _policy = await run.io_bound(lambda: get_context_policy(_eff))
        _native_max = _policy.native_max
        if _native_max is None:
            _native_max = await run.io_bound(lambda: get_model_max_context(_eff))
        if _native_max is not None and _policy.user_cap > _native_max:
            _ml = CONTEXT_SIZE_LABELS.get(_native_max, f"{_native_max:,}")
            _ul = CONTEXT_SIZE_LABELS.get(_policy.user_cap, f"{_policy.user_cap:,}")
            ui.notify(
                f"Context capped: {_eff} max is {_ml} (you selected {_ul}). "
                f"Trimming will use {_ml}.",
                type="warning",
                close_button=True,
                timeout=8000,
            )
        ui.notify(f"Switched to {_picker_opts.get(val, _eff)}", type="info")

    _select = ui.select(
        options=_picker_opts,
        value=_picker_val,
        on_change=_on_model_pick,
    ).props("dense borderless options-dense hide-bottom-space data-docs-id=chat-model-picker").classes("text-xs row-bot-composer-select").style(
        _compact_select_style(min_width=170, max_width=260)
    ).tooltip("Select model for this thread")

    async def _load_picker_options() -> None:
        started = time.perf_counter()
        try:
            options_started = time.perf_counter()
            options = await _refresh_model_picker_options()
            options_elapsed_ms = (time.perf_counter() - options_started) * 1000.0
            load_diagnostics = dict(_model_picker_options_last_diagnostics)
            load_diagnostics.pop("options", None)
            log_ui_perf(
                "chat.model_picker.options.load",
                options_elapsed_ms,
                threshold_ms=500.0,
                options=len(options),
                cache_hit=False,
                **load_diagnostics,
            )
            if (
                generation_getter is not None
                and shell_generation is not None
                and generation_getter() != shell_generation
            ):
                return
            apply_started = time.perf_counter()
            _merge_picker_options(options)
            _select.options = dict(_picker_opts)
            _select.update()
            log_ui_perf(
                "chat.model_picker.options.apply",
                (time.perf_counter() - apply_started) * 1000.0,
                threshold_ms=200.0,
                options=len(_picker_opts),
            )
            log_ui_perf(
                "chat.model_picker.options",
                (time.perf_counter() - started) * 1000.0,
                threshold_ms=500.0,
                options=len(_picker_opts),
            )
        except Exception:
            logger.debug("Could not load chat model picker options", exc_info=True)
            if (
                generation_getter is not None
                and shell_generation is not None
                and generation_getter() != shell_generation
            ):
                return
            _picker_opts.pop(_LOADING_MODELS_SENTINEL, None)
            _picker_opts[_MODELS_UNAVAILABLE_SENTINEL] = "Pinned models unavailable"
            if open_settings:
                _picker_opts[_MORE_MODELS_SENTINEL] = "More models..."
            _select.options = dict(_picker_opts)
            _select.update()

    if not docs_options and (cached_options is None or _cached_picker_stale):
        defer_ui(_load_picker_options, delay=0.05)


def _build_inline_approval_picker(state: AppState) -> None:
    """Compact approval-mode picker rendered inside the input bar."""
    from row_bot.agent import clear_agent_cache
    from row_bot.approval_policy import DEFAULT_APPROVAL_MODE, approval_label, normalize_approval_mode
    from row_bot.threads import _set_thread_approval_mode

    options = {
        "block": "Block",
        "approve": "Ask",
        "allow_all": "Auto",
    }
    current = normalize_approval_mode(
        getattr(state, "thread_approval_mode", "") or DEFAULT_APPROVAL_MODE,
        DEFAULT_APPROVAL_MODE,
    )
    state.thread_approval_mode = current

    async def _on_pick(e) -> None:
        val = normalize_approval_mode(e.value, current)
        if val == getattr(state, "thread_approval_mode", DEFAULT_APPROVAL_MODE):
            return
        state.thread_approval_mode = val
        if state.thread_id:
            await run.io_bound(_set_thread_approval_mode, state.thread_id, val)
        clear_agent_cache()
        e.sender.set_value(val)
        ui.notify(f"Approval mode: {approval_label(val)}", type="info")

    ui.select(
        options=options,
        value=current,
        on_change=_on_pick,
    ).props("dense borderless options-dense hide-bottom-space").classes("text-xs row-bot-composer-select").style(
        _compact_select_style(min_width=78, max_width=104)
    ).tooltip("Approval mode for this thread")
