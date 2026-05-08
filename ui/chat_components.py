"""Reusable chat UI components shared between the main chat and the Designer.

Extracted from ``ui.chat`` so both the normal chat view and the Designer
editor can use the same input bar, file upload, and message area.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys
from typing import Callable, Optional

from nicegui import events, run, ui

from ui.state import AppState, P, _active_generations
from ui.constants import ALLOWED_UPLOAD_SUFFIXES

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# MESSAGE AREA (scroll + container + auto-scroll JS)
# ══════════════════════════════════════════════════════════════════════

def build_chat_messages(
    p: P,
    state: AppState,
    *,
    messages: list[dict] | None = None,
    add_chat_message: Callable | None = None,
    placeholder_text: str = "Ask anything…",
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

    # Auto-scroll MutationObserver
    if p.chat_scroll:
        p.chat_scroll.scroll_to(percent=1.0)
        _sid = p.chat_scroll.id
        ui.run_javascript(f"""(function(){{
            var el = getElement({_sid});
            if (!el || !el.$el) return;
            var c = el.$el.querySelector('.q-scrollarea__container');
            if (!c) return;
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


# ══════════════════════════════════════════════════════════════════════
# FILE UPLOAD (hidden widget + drag-drop + clipboard paste)
# ══════════════════════════════════════════════════════════════════════

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

                b = ui.badge(f"📎 {name} ✕", color="grey-8").props("outline")
                b.on("click", lambda b=b, i=idx: _remove(i, b))
                b.style("cursor: pointer;")

    hidden_upload = ui.upload(on_upload=_on_upload, auto_upload=True, multiple=True).classes("hidden")

    # Drag-and-drop (singleton listener — reads dynamic upload ID)
    ui.run_javascript(f"""
        (() => {{
            window._thothUploadId = {hidden_upload.id};
            if (window._thothDragInstalled) return;
            window._thothDragInstalled = true;
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
                const vue = getElement(window._thothUploadId);
                if (vue && vue.$refs.qRef) vue.$refs.qRef.addFiles(files);
            }}, true);
        }})();
    """)

    # Clipboard image paste (singleton listener — reads dynamic upload ID)
    ui.run_javascript(f"""
        (() => {{
            window._thothUploadId = {hidden_upload.id};
            if (window._thothPasteInstalled) return;
            window._thothPasteInstalled = true;
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
                const vue = getElement(window._thothUploadId);
                if (vue && vue.$refs.qRef) vue.$refs.qRef.addFiles(imageFiles);
            }});
        }})();
    """)

    return hidden_upload


# ══════════════════════════════════════════════════════════════════════
# CHAT INPUT BAR (textarea + buttons + model picker + voice + stop)
# ══════════════════════════════════════════════════════════════════════

def build_chat_input_bar(
    p: P,
    state: AppState,
    *,
    send_fn: Callable,
    hidden_upload: ui.upload,
    browse_file: Callable | None = None,
    open_settings: Callable | None = None,
    show_model_picker: bool = True,
) -> None:
    """Build the chat input card with textarea, buttons, and optional model picker.

    Parameters
    ----------
    send_fn
        ``async def send_fn(text)`` — called when the user sends a message.
    hidden_upload
        The ``ui.upload`` element from ``build_file_upload`` so the attach
        button can trigger it.
    browse_file
        Native file browser callable (macOS).  ``None`` to skip.
    open_settings
        Called when "More models…" is selected.  ``None`` to skip model picker.
    show_model_picker
        Whether to render the model override dropdown.  ``True`` in normal
        chat and Designer.  ``False`` only if explicitly suppressed.
    """
    # ── Attach handler ───────────────────────────────────────────────
    async def _on_attach():
        if (sys.platform == "darwin" and os.environ.get("THOTH_NATIVE") == "1"
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

                        b = ui.badge(f"📎 {name} ✕", color="grey-8").props("outline")
                        b.on("click", lambda b=b, i=idx: _remove(i, b))
                        b.style("cursor: pointer;")
        else:
            await ui.run_javascript(
                f"document.getElementById('c{hidden_upload.id}').querySelector('input[type=file]').click()"
            )

    # ── Input card ───────────────────────────────────────────────────
    with ui.column().classes("w-full shrink-0 gap-0").style(
        "border: 1px solid rgba(255,255,255,0.15); border-radius: 18px; "
        "background: rgba(255,255,255,0.04); padding: 0; overflow: hidden; "
        "position: relative;"
    ):
        # File chips inside the card (top)
        p.file_chips_row = ui.row().classes("w-full flex-wrap gap-1 q-px-md q-pt-sm")

        # Context counter — absolute overlay, top-right
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
            ui.textarea(placeholder="Ask anything…")
            .classes("w-full")
            .props(
                'borderless autogrow input-style="padding: 12px 16px 4px 16px; '
                'max-height: 200px; overflow-y: auto;"'
            )
            .style("font-size: 0.95rem;")
        )

        async def _on_send():
            text = p.chat_input.value
            if text and text.strip():
                p.chat_input.value = ""
                if p.chat_scroll:
                    _re = p.chat_scroll.id
                    ui.run_javascript(
                        f"(function(){{ var e=getElement({_re}); if(e) e._tSS=true; }})()"
                    )
                await send_fn(text)
            elif p.pending_files:
                p.chat_input.value = ""
                await send_fn("")

        # Enter to send; modified Enter keeps native textarea behavior.
        p.chat_input.on(
            "keydown.enter",
            _on_send,
            js_handler="""(e) => {
                if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
                e.preventDefault();
                emit();
            }""",
        )

        def _on_stop():
            gen = _active_generations.get(state.thread_id)
            if gen:
                gen.stop_event.set()
            tts = state.tts_service
            if tts and tts.enabled:
                tts.stop()
                if state.voice_service and state.voice_service.is_running:
                    state.voice_service.unmute()
            if p.stop_btn:
                p.stop_btn.props('icon=hourglass_top')

        # Bottom bar: attach, model picker, voice, spacer, send, stop
        with ui.row().classes("w-full items-center q-px-sm q-pb-sm q-pt-none gap-1"):
            ui.button(icon="attach_file", on_click=_on_attach).props(
                "flat round dense size=sm"
            ).tooltip("Attach files")

            # ── Inline model picker ──────────────────────────────────
            if show_model_picker:
                _build_inline_model_picker(state, open_settings=open_settings)

            def _toggle_voice(e):
                state.voice_enabled = e.value
                if e.value:
                    state.voice_service.start()
                else:
                    state.voice_service.stop()

            p.voice_switch = ui.switch("🎤 Voice", value=state.voice_enabled, on_change=_toggle_voice).classes("text-xs")
            p.voice_status_label = ui.label("").classes("text-xs text-grey-6")

            ui.space()

            ui.button(icon="send", on_click=_on_send).props(
                "color=primary round dense size=sm"
            ).tooltip("Send")

            p.stop_btn = ui.button(icon="stop", on_click=_on_stop).props(
                "round dense size=sm"
            ).tooltip("Stop generation")
            _has_active = state.thread_id in _active_generations
            if not _has_active:
                p.stop_btn.disable()


def _build_inline_model_picker(
    state: AppState,
    *,
    open_settings: Callable | None = None,
) -> None:
    """Compact model picker rendered inside the input bar."""
    from agent import clear_agent_cache
    from models import get_current_model
    from providers.selection import list_model_choice_options, model_choice_value

    _cur_default = get_current_model()
    _cur_default_value = model_choice_value(_cur_default)
    _default_opt = "__default__"
    _picker_opts = {_default_opt: f"Default — {_cur_default}"}

    _cur_mo = state.thread_model_override or ""
    for option in list_model_choice_options("chat", include_values=[_cur_mo] if _cur_mo else []):
        value = str(option.get("value") or "")
        if value and value != _cur_default_value:
            _picker_opts[value] = str(option.get("label") or value)

    _MORE = "⚙️ More models…"
    if open_settings:
        _picker_opts[_MORE] = _MORE

    _cur_mo_value = model_choice_value(_cur_mo)
    _picker_val = _cur_mo_value if _cur_mo_value and _cur_mo_value in _picker_opts else _default_opt

    async def _on_pick(e):
        val = e.value
        if val == _MORE:
            e.sender.set_value(_picker_val)
            if open_settings:
                open_settings("Models")
            return
        from threads import _set_thread_model_override
        if val == _default_opt:
            state.thread_model_override = ""
            _set_thread_model_override(state.thread_id, "")
        elif val in _picker_opts:
            state.thread_model_override = val
            _set_thread_model_override(state.thread_id, val)
        else:
            state.thread_model_override = ""
            _set_thread_model_override(state.thread_id, "")
        clear_agent_cache()

    ui.select(
        options=_picker_opts,
        value=_picker_val,
        on_change=_on_pick,
    ).props("dense borderless").classes("text-xs").style(
        "min-width: 140px; max-width: 220px;"
    ).tooltip("Select model")
