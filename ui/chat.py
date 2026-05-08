"""Thoth UI — Chat screen (thread conversation view).

Extracted from the monolith's ``_build_chat`` inner function.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys
from datetime import datetime
from typing import Callable

from nicegui import events, run, ui

from ui.state import AppState, P, _active_generations
from ui.constants import ALLOWED_UPLOAD_SUFFIXES, welcome_message, EXAMPLE_PROMPTS
from ui.render import render_image_with_save
from ui.timer_utils import defer_ui

logger = logging.getLogger(__name__)


def build_chat(
    state: AppState,
    p: P,
    *,
    rebuild_main: Callable,
    rebuild_thread_list: Callable,
    send_message: Callable,
    open_settings: Callable,
    open_export: Callable,
    show_interrupt: Callable,
    add_chat_message: Callable,
    browse_file: Callable,
) -> None:
    """Render the full chat view for the current thread."""
    from agent import clear_agent_cache, get_token_usage
    from models import (
        get_current_model, is_cloud_model, get_cloud_provider,
        get_model_max_context, get_user_context_size, CONTEXT_SIZE_LABELS,
    )
    from providers.selection import (
        list_model_choice_options,
        model_choice_value,
        model_id_from_choice_value,
        provider_display_label,
    )
    from threads import (
        _save_thread_meta, _set_thread_model_override,
        get_thread_skills_override, set_thread_skills_override,
    )
    from tasks import get_running_tasks, stop_task
    from tools import registry as tool_registry
    from ui.helpers import persist_thread_media_state

    # ── Header ───────────────────────────────────────────────────────
    running_wfs = get_running_tasks()
    bg = running_wfs.get(state.thread_id)

    with ui.row().classes("w-full items-center shrink-0"):
        if bg:
            ui.html(
                f"<h3>⚡ {bg['name']} "
                f"<span style='font-size:0.8rem; opacity:0.7;'>"
                f"Running — Step {bg['step']+1}/{bg['total']}</span></h3>",
                sanitize=False,
            )
            def _stop_task_from_header(tid=state.thread_id):
                stop_task(tid)
                ui.notify("⏹️ Stop signal sent — task will stop after current step.", type="warning")
                rebuild_main()
            ui.button(icon="stop", on_click=_stop_task_from_header).props(
                "round color=red size=sm"
            ).tooltip("Stop task")
        else:
            p.chat_header_label = ui.label(f"💬 {state.thread_name}").classes("text-h5 flex-grow")

            # ── Model picker ─────────────────────────────────────────
            _cur_mo = state.thread_model_override or ""

            _cur_default = get_current_model()
            _cur_default_value = model_choice_value(_cur_default)
            _picker_opts = {"__default__": f"Default — {_cur_default}"}
            for option in list_model_choice_options("chat", include_values=[_cur_mo] if _cur_mo else []):
                value = str(option.get("value") or "")
                if value and value != _cur_default_value:
                    _picker_opts[value] = str(option.get("label") or value)

            _MORE_MODELS_SENTINEL = "⚙️ More models…"
            _picker_opts[_MORE_MODELS_SENTINEL] = _MORE_MODELS_SENTINEL

            _cur_mo_value = model_choice_value(_cur_mo)
            _picker_val = _cur_mo_value if _cur_mo_value and _cur_mo_value in _picker_opts else "__default__"

            async def _on_model_pick(e):
                val = e.value
                if val == _MORE_MODELS_SENTINEL:
                    e.sender.set_value(_picker_val)
                    open_settings("Models")
                    return
                if val == "__default__":
                    state.thread_model_override = ""
                    _set_thread_model_override(state.thread_id, "")
                    _switched_label = "default"
                elif val in _picker_opts:
                    state.thread_model_override = val
                    _set_thread_model_override(state.thread_id, val)
                    _switched_label = _picker_opts.get(val, val)
                else:
                    state.thread_model_override = ""
                    _set_thread_model_override(state.thread_id, "")
                    _switched_label = "default"
                clear_agent_cache()
                _eff = state.thread_model_override or get_current_model()
                if _switched_label == "default":
                    ui.notify(f"Switched to default model ({_eff})", type="info")
                else:
                    ui.notify(f"Switched to {_switched_label}", type="info")
                _mmax = await run.io_bound(lambda: get_model_max_context(_eff))
                _uval = get_user_context_size()
                if _mmax is not None and _uval > _mmax:
                    _ml = CONTEXT_SIZE_LABELS.get(_mmax, f"{_mmax:,}")
                    _ul = CONTEXT_SIZE_LABELS.get(_uval, f"{_uval:,}")
                    ui.notify(
                        f"Context capped: {_eff} max is {_ml} (you selected {_ul}). "
                        f"Trimming will use {_ml}.",
                        type="warning", close_button=True, timeout=8000,
                    )
                rebuild_main()

            ui.select(
                options=_picker_opts, value=_picker_val,
                on_change=_on_model_pick,
            ).props('dense borderless use-input input-debounce=300').classes("text-sm").style(
                "min-width: 200px; max-width: 320px;"
            ).tooltip("Select model for this thread")

            # ── Per-thread skills override ───────────────────────────
            import skills as _skills_mod
            _skills_mod.load_skills()
            _enabled_sk = [s for s in _skills_mod.get_enabled_skills()
                           if not _skills_mod.is_tool_guide(s)]
            if _enabled_sk:
                _thread_sk_override = get_thread_skills_override(state.thread_id)
                _enabled_names = set(sk.name for sk in _enabled_sk)
                _active_sk_names = (
                    set(_thread_sk_override) & _enabled_names
                    if _thread_sk_override is not None
                    else set(_enabled_names)
                )
                _sk_count = len(_active_sk_names)
                _sk_label = (
                    f"✨ {_sk_count} skill{'s' if _sk_count != 1 else ''}"
                    if _sk_count > 0
                    else "✨ No skills"
                )
                _sk_btn = ui.button(_sk_label).props("flat dense no-caps size=sm").classes("text-sm").style("min-width: 100px;")
                with _sk_btn:
                    with ui.menu().classes("q-pa-sm"):
                        ui.label("Skills for this thread").classes("text-xs text-grey-5 q-mb-xs")

                        def _update_sk_label():
                            _cur = get_thread_skills_override(state.thread_id)
                            _en = set(sk.name for sk in _skills_mod.get_enabled_skills()
                                      if not _skills_mod.is_tool_guide(sk))
                            _act = set(_cur) & _en if _cur is not None else _en
                            n = len(_act)
                            _sk_btn.text = f"✨ {n} skill{'s' if n != 1 else ''}" if n > 0 else "✨ No skills"

                        def _on_sk_toggle(name, val):
                            _cur = get_thread_skills_override(state.thread_id)
                            if _cur is None:
                                _cur = list(_enabled_names)
                            names_set = set(_cur)
                            if val:
                                names_set.add(name)
                            else:
                                names_set.discard(name)
                            set_thread_skills_override(state.thread_id, list(names_set))
                            clear_agent_cache()
                            _update_sk_label()

                        async def _reset_skills():
                            set_thread_skills_override(state.thread_id, None)
                            clear_agent_cache()
                            _update_sk_label()
                            for _cbn, _cbw in _sk_cbs.items():
                                _cbw.value = True

                        _sk_cbs = {}
                        with ui.column().classes("gap-0"):
                            for _sk in _enabled_sk:
                                _cb = ui.checkbox(
                                    f"{_sk.icon} {_sk.display_name}",
                                    value=_sk.name in _active_sk_names,
                                    on_change=lambda e, n=_sk.name: _on_sk_toggle(n, e.value),
                                ).classes("text-sm")
                                _sk_cbs[_sk.name] = _cb
                        ui.separator()
                        ui.button("Reset to global", icon="restart_alt",
                                  on_click=lambda: asyncio.create_task(_reset_skills())).props("flat dense size=sm")

        if state.messages:
            ui.button(icon="download", on_click=open_export).props("flat round").tooltip("Export")

    # ── Cloud/local model banner ─────────────────────────────────────
    _active_model = state.thread_model_override or get_current_model()
    _is_active_cloud = is_cloud_model(_active_model)
    if _is_active_cloud:
        _prov = get_cloud_provider(_active_model) or "cloud"
        _prov_label = provider_display_label(_prov)
        _model_label = model_id_from_choice_value(_active_model)
        with ui.row().classes("w-full items-center gap-2 q-px-sm q-py-xs").style(
            "background: rgba(255, 152, 0, 0.08); border-radius: 8px; border: 1px solid rgba(255, 152, 0, 0.25);"
        ):
            ui.icon("cloud", color="orange").style("font-size: 1.1rem;")
            ui.label(f"Using {_model_label} via {_prov_label} — data is sent to the cloud").classes("text-orange text-sm")
    else:
        with ui.row().classes("w-full items-center gap-2 q-px-sm q-py-xs").style(
            "background: rgba(76, 175, 80, 0.08); border-radius: 8px; border: 1px solid rgba(76, 175, 80, 0.25);"
        ):
            ui.icon("lock", color="green").style("font-size: 1.1rem;")
            ui.label(f"Using {_active_model} via Ollama — complete privacy").classes("text-green text-sm")

    # ── Scrollable message area ──────────────────────────────────────
    _scroll_bg = (
        "background: rgba(255, 152, 0, 0.03);"
        if _is_active_cloud
        else "background: rgba(76, 175, 80, 0.03);"
    )
    p.chat_scroll = ui.scroll_area().classes("w-full flex-grow").style(_scroll_bg)

    with p.chat_scroll:
        p.chat_container = ui.column().classes("w-full gap-2")

    # Render existing messages
    _reattach_gen = _active_generations.get(state.thread_id)
    _has_active_gen = (_reattach_gen and _reattach_gen.detached and _reattach_gen.status == "streaming")
    _has_running_task = state.thread_id in get_running_tasks()
    _msgs_to_render = state.messages
    if ((_has_active_gen or _has_running_task)
            and _msgs_to_render
            and _msgs_to_render[-1].get("content", "").startswith(
                "\u26a0\ufe0f The assistant was interrupted")):
        _msgs_to_render = _msgs_to_render[:-1]

    # ── Progressive render ───────────────────────────────────────────
    # For responsiveness on large threads, render the first batch
    # synchronously so the user sees content immediately, then stream
    # the remainder via chained timers (each yields to the event loop
    # so clicks / input remain responsive). Reattach / onboarding /
    # scroll hooks run AFTER all messages are in the DOM so order is
    # preserved.
    _INITIAL_RENDER = 15
    _CHUNK_SIZE = 10
    for msg in _msgs_to_render[:_INITIAL_RENDER]:
        add_chat_message(msg)
    _remaining_msgs = _msgs_to_render[_INITIAL_RENDER:]

    # ── Reattach to running generation ───────────────────────────────
    def _finalize_after_messages() -> None:
        if _reattach_gen and _reattach_gen.detached and _reattach_gen.status == "streaming":
            from identity import get_assistant_name as _gan_ra
            from ui.status_bar import get_bot_avatar_html as _gba_ra
            _ra_avatar = _gba_ra()
            _ra_name = _gan_ra()
            with p.chat_container:
                with ui.element("div").classes("thoth-msg-row"):
                    ui.html(f'<div class="thoth-avatar thoth-avatar-bot">{_ra_avatar}</div>', sanitize=False)
                    with ui.column().classes("thoth-msg-body gap-1") as _ra_wrapper:
                        ui.html(
                            '<div class="thoth-msg-header">'
                            f'<span class="thoth-msg-name">{_ra_name}</span>'
                            f'<span class="thoth-msg-stamp">{datetime.now().strftime("%H:%M")}</span>'
                            '</div>',
                            sanitize=False,
                        )
                        _reattach_gen.tool_col = ui.column().classes("w-full gap-1")
                        for _tr in _reattach_gen.tool_results:
                            with _reattach_gen.tool_col:
                                with ui.expansion(f"\u2705 {_tr['name']}", icon="check_circle").classes("w-full"):
                                    if _tr.get('content'):
                                        _disp = _tr['content'][:5_000]
                                        if len(_tr['content']) > 5_000:
                                            _disp += "\n\n\u2026 (truncated)"
                                        ui.code(_disp).classes("w-full text-xs")
                        for _cj in _reattach_gen.chart_data:
                            try:
                                import plotly.io as _pio
                                _fig = _pio.from_json(_cj)
                                with _reattach_gen.tool_col:
                                    ui.plotly(_fig).classes("w-full")
                            except Exception:
                                logger.debug("Chart rendering failed during reattach", exc_info=True)
                        for _img in _reattach_gen.captured_images:
                            try:
                                with _reattach_gen.tool_col:
                                    render_image_with_save(_img)
                            except Exception:
                                logger.debug("Image rendering failed during reattach", exc_info=True)
                        for _vid in _reattach_gen.captured_videos:
                            try:
                                with _reattach_gen.tool_col:
                                    from ui.render import render_video_with_save
                                    render_video_with_save(_vid.get("path", "") if isinstance(_vid, dict) else _vid)
                            except Exception:
                                logger.debug("Video rendering failed during reattach", exc_info=True)
                        _reattach_gen.assistant_md = ui.markdown(
                            _reattach_gen.accumulated,
                            extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                        ).classes("thoth-msg w-full")
                        _reattach_gen.wrapper = _ra_wrapper
                        _reattach_gen.thinking_label = None
                        _reattach_gen.thinking_md = None
            _reattach_gen.detached = False
            if p.stop_btn:
                p.stop_btn.enable()
        elif _reattach_gen and _reattach_gen.status in ("done", "error", "stopped", "interrupted"):
            if _reattach_gen.accumulated:
                a_msg: dict = {"role": "assistant", "content": _reattach_gen.accumulated}
                if _reattach_gen.tool_results:
                    a_msg["tool_results"] = _reattach_gen.tool_results
                if _reattach_gen.chart_data:
                    a_msg["charts"] = _reattach_gen.chart_data
                if _reattach_gen.captured_images:
                    a_msg["images"] = _reattach_gen.captured_images
                    if _reattach_gen.captured_images_persist:
                        a_msg["_media_persist_flags"] = list(_reattach_gen.captured_images_persist)
                        if any(_reattach_gen.captured_images_persist):
                            a_msg["_media_persist"] = True
                if _reattach_gen.captured_videos:
                    a_msg["videos"] = _reattach_gen.captured_videos
                state.messages.append(a_msg)
                persist_thread_media_state(state.thread_id, state.messages)
                add_chat_message(a_msg)
            _active_generations.pop(state.thread_id, None)

        # Onboarding
        if state.show_onboarding:
            from identity import get_assistant_name as _gan_ob
            from ui.status_bar import get_bot_avatar_html as _gba_ob
            _ob_avatar = _gba_ob()
            _ob_name = _gan_ob()
            with p.chat_container:
                with ui.element("div").classes("thoth-msg-row"):
                    ui.html(f'<div class="thoth-avatar thoth-avatar-bot">{_ob_avatar}</div>', sanitize=False)
                    with ui.column().classes("thoth-msg-body gap-1"):
                        ui.html(
                            '<div class="thoth-msg-header">'
                            f'<span class="thoth-msg-name">{_ob_name}</span>'
                            '</div>',
                            sanitize=False,
                        )
                        _cloud_ob2 = is_cloud_model(_active_model)
                        ui.markdown(welcome_message(cloud=_cloud_ob2), extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                        with ui.row().classes("flex-wrap gap-2"):
                            for prompt in EXAMPLE_PROMPTS:
                                def _try_inline(pr=prompt):
                                    state.show_onboarding = False
                                    asyncio.create_task(send_message(pr))
                                ui.button(prompt, on_click=_try_inline).props("flat dense outline").style("text-transform:none;")

                        def _dismiss():
                            state.show_onboarding = False
                            rebuild_main()
                        ui.button("✕ Dismiss", on_click=_dismiss).props("flat dense")

        # Interrupt UI
        if state.pending_interrupt:
            show_interrupt(state.pending_interrupt)

        if p.chat_scroll:
            p.chat_scroll.scroll_to(percent=1.0)
            # Client-side auto-scroll: a MutationObserver scrolls to the
            # bottom whenever content changes, unless the user has scrolled up.
            # Uses wheel/touchstart timestamps to distinguish user-initiated
            # scrolls from programmatic ones (MutationObserver).  On Mac
            # WKWebView the old approach of checking scroll position in every
            # scroll event caused a feedback loop because the programmatic
            # scroll fired a scroll event that immediately re-enabled _tSS.
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

    # If there are leftover messages, stream them into the container
    # in small deferred chunks. Each deferred task yields to the event
    # loop so the UI stays responsive without leaving NiceGUI timer
    # elements attached to containers that can be deleted on navigation.
    # Finalize
    # runs after the last chunk so reattach / onboarding land at the
    # bottom of the chat.
    if _remaining_msgs:
        _chunk_state = {"idx": 0}
        def _render_next_chunk():
            start = _chunk_state["idx"]
            end = min(start + _CHUNK_SIZE, len(_remaining_msgs))
            try:
                with p.chat_container:
                    for msg in _remaining_msgs[start:end]:
                        add_chat_message(msg)
            except Exception:
                logger.debug("chunked chat render failed", exc_info=True)
                _finalize_after_messages()
                return
            _chunk_state["idx"] = end
            if end < len(_remaining_msgs):
                defer_ui(_render_next_chunk)
            else:
                _finalize_after_messages()
        defer_ui(_render_next_chunk)
    else:
        _finalize_after_messages()

    # ── File chips (created early so _on_upload can reference) ──────
    # We'll parent these inside the input card below

    async def _on_upload(e: events.UploadEventArguments):
        data = await e.file.read()
        name = e.file.name
        p.pending_files.append({"name": name, "data": data})
        if hasattr(e, 'sender') and hasattr(e.sender, 'reset'):
            e.sender.reset()
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

    _hidden_upload = ui.upload(on_upload=_on_upload, auto_upload=True, multiple=True).classes("hidden")

    # Drag-and-drop (singleton listener — reads dynamic upload ID)
    ui.run_javascript(f'''
        (() => {{
            window._thothUploadId = {_hidden_upload.id};
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
                // Safety: auto-hide after 300ms of no dragover events
                if (dragTimer) clearTimeout(dragTimer);
                dragTimer = setTimeout(hideOverlay, 300);
            }});
            body.addEventListener("dragleave", (e) => {{
                if (e.relatedTarget === null || !body.contains(e.relatedTarget)) hideOverlay();
            }});
            // Use capture phase so we hide overlay even if Quasar QUploader stops propagation
            document.addEventListener("drop", (e) => {{
                hideOverlay();
                // Only intercept drops outside Quasar uploaders (let them handle their own)
                const inUploader = e.target.closest && e.target.closest('.q-uploader');
                if (inUploader) return;
                e.preventDefault();
                const files = e.dataTransfer?.files;
                if (!files || files.length === 0) return;
                const vue = getElement(window._thothUploadId);
                if (vue && vue.$refs.qRef) vue.$refs.qRef.addFiles(files);
            }}, true);
        }})();
    ''')

    # Clipboard image paste (singleton listener — reads dynamic upload ID)
    ui.run_javascript(f'''
        (() => {{
            window._thothUploadId = {_hidden_upload.id};
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
    ''')

    # ── Chat input card (modern SOTA layout) ────────────────────────
    async def _on_attach():
        if sys.platform == "darwin" and os.environ.get("THOTH_NATIVE") == "1":
            path = await browse_file(
                title="Attach file",
                filetypes=[("Supported files", " ".join(f"*.{e}" for e in ALLOWED_UPLOAD_SUFFIXES))],
            )
            if path and os.path.isfile(path):
                name = os.path.basename(path)
                data = await run.io_bound(pathlib.Path(path).read_bytes)
                p.pending_files.append({"name": name, "data": data})
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
                f"document.getElementById('c{_hidden_upload.id}').querySelector('input[type=file]').click()"
            )

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
                p.token_bar = ui.linear_progress(value=0, show_value=False).style("height: 3px; width: 100px;")

        # Textarea
        p.chat_input = (
            ui.textarea(placeholder="Ask anything…")
            .classes("w-full")
            .props('borderless autogrow input-style="padding: 12px 16px 4px 16px; max-height: 200px; overflow-y: auto;"')
            .style("font-size: 0.95rem;")
        )

        async def _on_send():
            text = p.chat_input.value
            if text and text.strip():
                p.chat_input.value = ""
                # Re-engage auto-scroll on new message
                if p.chat_scroll:
                    _re = p.chat_scroll.id
                    ui.run_javascript(
                        f"(function(){{ var e=getElement({_re}); if(e) e._tSS=true; }})()"
                    )
                await send_message(text)
            elif p.pending_files:
                p.chat_input.value = ""
                await send_message("")

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

        # Bottom bar inside card: attach, voice, spacer, send, stop
        with ui.row().classes("w-full items-center q-px-sm q-pb-sm q-pt-none gap-1"):
            ui.button(icon="attach_file", on_click=_on_attach).props(
                "flat round dense size=sm"
            ).tooltip("Attach files")

            def _toggle_voice(e):
                state.voice_enabled = e.value
                if e.value:
                    state.voice_service.start()
                else:
                    state.voice_service.stop()
            p.voice_switch = ui.switch("🎤 Voice", value=state.voice_enabled, on_change=_toggle_voice).classes("text-xs")
            p.voice_status_label = ui.label("").classes("text-xs text-grey-6")

            ui.space()  # push right-side items to the right

            ui.button(icon="send", on_click=_on_send).props("color=primary round dense size=sm").tooltip("Send")

            p.stop_btn = ui.button(icon="stop", on_click=_on_stop).props("round dense size=sm").tooltip("Stop generation")
            _has_active = state.thread_id in _active_generations
            if not _has_active:
                p.stop_btn.disable()
