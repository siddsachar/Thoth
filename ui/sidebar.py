"""Thoth UI — sidebar (left drawer) with thread list.

Builds the sidebar drawer, home/new buttons, thread listing, and
settings/help buttons.  All navigation is handled via callbacks so
the module stays decoupled from the main page layout.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable

from nicegui import run, ui
from ui.timer_utils import safe_timer

from ui.state import AppState, P, _active_generations
from ui.constants import SIDEBAR_MAX_THREADS

logger = logging.getLogger(__name__)

# Module-level so filter choice survives rebuild_main() re-renders.
_SIDEBAR_FILTER: str = "all"  # one of: "all", "chat", "designer", "workflow"
_MODAL_FILTER: str = "all"
_SIDEBAR_AVATAR_CSS = """
.sb-avatar { position: relative; }
.sb-idle { color: #64748b; }
.sb-streaming { color: #2563eb; }
.sb-error { color: #dc2626; }
.sb-voice { color: #0f766e; }
.sb-task { color: #7c3aed; }
.sb-approval { color: #d97706; }
.sb-done { color: #16a34a; }
.sb-tts { color: #0891b2; }
.sb-state-label { font-size: 11px; }
.sb-ring-spin { animation: sb-ring-spin 1.1s linear infinite; }
@keyframes sb-ring-spin { to { transform: rotate(360deg); } }
"""


def build_sidebar(
    state: AppState,
    p: P,
    *,
    rebuild_main: Callable[[], None],
    open_settings: Callable[..., None],
    load_thread_messages: Callable[[str], list[dict]],
) -> Callable[[], None]:
    """Build the left drawer and return ``rebuild_thread_list`` so the
    caller can invoke it when needed.

    Parameters
    ----------
    rebuild_main:
        Called to refresh the main content area after a navigation event.
    open_settings:
        Called when the user clicks the Settings button.
    load_thread_messages:
        ``load_thread_messages(thread_id) -> list[dict]`` used to hydrate
        a thread when the user clicks it.
    """
    from threads import _list_threads, _save_thread_meta, _delete_thread, _get_thread_project_id
    from tasks import get_running_tasks, stop_task
    from models import is_cloud_model, get_current_model
    from memory_extraction import set_active_thread
    from agent import clear_summary_cache

    # Keep a reference the caller can use
    _rebuild_thread_list_ref: list[Callable[[], None]] = [lambda: None]

    with ui.left_drawer(value=True, fixed=True).style(
        "width: 280px;"
    ).classes("thoth-panel-card"):
        # Logo — always Thoth branding, independent of identity settings
        ui.html(
            '<div style="display:flex; align-items:center; gap:8px;">'
            '<span style="font-size:1.6rem; color:gold;">𓁟</span>'
            '<span style="font-size:1.25rem; font-weight:600; color:gold;'
            ' letter-spacing:0.5px;">Thoth</span></div>',
            sanitize=False,
        )
        ui.label("Personal AI Sovereignty").classes("text-xs text-grey-6").style(
            "margin-top: -2px;"
        )
        ui.separator()

        # Home + New buttons
        with ui.row().classes("w-full gap-2"):
            def _go_home():
                prev = state.thread_id
                prev_gen = _active_generations.get(prev) if prev else None
                if prev_gen and prev_gen.status == "streaming":
                    prev_gen.detached = True
                    if prev_gen.tts_active:
                        state.tts_service.stop()
                        prev_gen.tts_active = False
                state.active_designer_project = None
                state.thread_id = None
                state.thread_name = None
                state.messages = []
                p.pending_files.clear()
                set_active_thread(None, previous_id=prev)
                rebuild_main()
                _rebuild_thread_list_ref[0]()

            _home_btn = ui.button("🏠 Home", on_click=_go_home).classes("flex-grow").props("flat")

            async def _new_thread():
                tid = uuid.uuid4().hex[:12]
                name = f"💻 Thread {datetime.now().strftime('%b %d, %H:%M')}"
                await run.io_bound(_save_thread_meta, tid, name)
                prev = state.thread_id
                prev_gen = _active_generations.get(prev) if prev else None
                if prev_gen and prev_gen.status == "streaming":
                    prev_gen.detached = True
                    if prev_gen.tts_active:
                        state.tts_service.stop()
                        prev_gen.tts_active = False
                state.active_designer_project = None
                state.thread_id = tid
                state.thread_name = name
                state.messages = []
                state.thread_model_override = ""
                p.pending_files.clear()
                set_active_thread(tid, previous_id=prev)
                rebuild_main()
                _rebuild_thread_list_ref[0]()

            ui.button("＋ New", on_click=_new_thread).classes("flex-grow").props("color=primary")

        with ui.column().classes("w-full gap-1 q-mt-sm thoth-inner-panel"):
            ui.label("Conversations").classes("text-subtitle2")
            # Filter pill row — rebuilt by _rebuild_thread_list so counts stay fresh.
            p.thread_filter_container = ui.row().classes(
                "w-full gap-1 items-center no-wrap q-mb-xs"
            ).style("flex-wrap: wrap;")
            p.thread_container = ui.column().classes("w-full gap-0")

        # ── Channel monitor panel ────────────────────────────────────
        _ch_icon_map = {
            "telegram": "send",
            "discord": "sports_esports",
            "slack": "tag",
            "sms": "textsms",
            "whatsapp": "forum",
        }

        def _fmt_ago(epoch: float | None) -> str:
            """Format seconds-since-epoch as a relative string."""
            if epoch is None:
                return ""
            import time as _t
            delta = int(_t.time() - epoch)
            if delta < 60:
                return "just now"
            if delta < 3600:
                return f"{delta // 60}m ago"
            if delta < 86400:
                return f"{delta // 3600}h ago"
            return f"{delta // 86400}d ago"

        with ui.column().classes("w-full gap-0 q-mt-sm thoth-inner-panel"):
            _ch_monitor_container = ui.column().classes("w-full gap-0")

        def _build_channel_monitor() -> None:
            from channels.registry import all_channels
            from channels.base import get_last_activity

            _ch_monitor_container.clear()
            channels = all_channels()
            if not channels:
                return

            with _ch_monitor_container:
                ui.label("Channels").classes("text-subtitle2")

                for ch in channels:
                    is_on = ch.is_running()
                    is_cfg = ch.is_configured()

                    if is_on:
                        dot_color = "#4caf50"
                        status_text = _fmt_ago(get_last_activity(ch.name)) or "Running"
                    elif is_cfg:
                        dot_color = "#ff9800"
                        status_text = "Stopped"
                    else:
                        dot_color = "#666"
                        status_text = "Off"

                    icon_name = _ch_icon_map.get(ch.name, "chat")

                    def _ch_click(e, _ch=ch):
                        open_settings("Channels")

                    with ui.row().classes(
                        "w-full items-center no-wrap cursor-pointer q-py-xs q-px-sm rounded"
                    ).style(
                        "min-height: 28px; gap: 6px;"
                        "transition: background 0.15s;"
                    ).on("click", _ch_click).on(
                        "mouseenter",
                        js_handler="(e) => e.currentTarget.style.background='rgba(255,255,255,0.06)'",
                    ).on(
                        "mouseleave",
                        js_handler="(e) => e.currentTarget.style.background='transparent'",
                    ):
                        # Status dot
                        ui.html(
                            f'<span style="display:inline-block;width:8px;height:8px;'
                            f'border-radius:50%;background:{dot_color};flex-shrink:0;"></span>',
                            sanitize=False,
                        )
                        # Channel icon
                        ui.icon(icon_name, size="xs").classes(
                            "text-grey-5" if not is_on else "text-primary"
                        ).style("font-size: 0.85rem;")
                        # Name
                        ui.label(ch.display_name).classes("ellipsis").style(
                            "font-size: 0.8rem; flex-grow: 1;"
                            + ("opacity: 0.45;" if not is_cfg else "")
                        )
                        # Activity / status
                        ui.label(status_text).classes("text-grey-6").style(
                            "font-size: 0.7rem; flex-shrink: 0;"
                        )

        _build_channel_monitor()
        safe_timer(5.0, _build_channel_monitor)

        # Spacer pushes bottom section down
        ui.space()

        # ── Buddy companion ─────────────────────────────────────────
        from ui.buddy import build_sidebar_buddy
        build_sidebar_buddy(state, p, open_settings=open_settings)

        # Help & Settings buttons
        with ui.row().classes("w-full justify-center items-center gap-1"):
            def _show_help():
                from ui.onboarding_center import show_setup_center

                show_setup_center(
                    open_settings=open_settings,
                    rebuild_main=rebuild_main,
                    state=state,
                )

            ui.button("👋", on_click=_show_help).props("flat dense").style("font-size: 1.1rem;")
            ui.button(icon="settings", on_click=lambda: open_settings()).props(
                "flat dense round size=sm"
            ).classes("text-grey-5").style("font-size: 1.25rem;")
        from version import __version__ as _v
        ui.label(f"v{_v}").classes("text-xs text-grey-7 w-full text-center").style(
            "margin-top: -4px; letter-spacing: 0.3px; opacity: 0.5;"
        )

    # ── Thread list builder ──────────────────────────────────────────

    def _rebuild_thread_list() -> None:
        if p.thread_container is None:
            return
        p.thread_container.clear()
        if p.thread_filter_container is not None:
            p.thread_filter_container.clear()
        threads = _list_threads()
        running_tids = get_running_tasks()

        # Classify every thread once so pills + list share the same data.
        from threads import get_workflow_thread_ids
        workflow_tids = get_workflow_thread_ids()

        def _cat_of(pid: str, tid: str) -> str:
            if pid:
                return "designer"
            if tid in workflow_tids:
                return "workflow"
            return "chat"

        classified: list[tuple] = []
        counts = {"all": len(threads), "chat": 0, "designer": 0, "workflow": 0}
        for row in threads:
            tid = row[0]
            _pid = row[5] if len(row) > 5 else ""
            cat = _cat_of(_pid, tid)
            counts[cat] += 1
            classified.append((row, cat))

        # ── Filter pill row ─────────────────────────────────────────
        global _SIDEBAR_FILTER
        if p.thread_filter_container is not None and counts["all"] > 0:
            pills = [
                ("all", "All", counts["all"]),
                ("chat", "Chats", counts["chat"]),
                ("designer", "Designs", counts["designer"]),
                ("workflow", "Workflows", counts["workflow"]),
            ]
            with p.thread_filter_container:
                for key, label, n in pills:
                    # Hide empty buckets other than "All".
                    if key != "all" and n == 0:
                        continue
                    is_on = _SIDEBAR_FILTER == key

                    def _set_filter(k=key):
                        global _SIDEBAR_FILTER
                        _SIDEBAR_FILTER = k
                        _rebuild_thread_list()

                    btn = ui.button(
                        f"{label} {n}" if n else label,
                        on_click=_set_filter,
                    ).props(
                        "dense no-caps size=sm rounded "
                        + ("color=amber" if is_on else "flat color=grey-5")
                    ).style("font-size: 0.72rem; padding: 2px 8px;")
                    if is_on:
                        btn.classes("thoth-pill-active")

        # Apply filter
        if _SIDEBAR_FILTER != "all":
            classified = [c for c in classified if c[1] == _SIDEBAR_FILTER]

        def _fmt_ts(iso_str: str) -> str:
            try:
                dt = datetime.fromisoformat(iso_str)
                try:
                    return dt.strftime("%b %d, %#I:%M %p")
                except ValueError:
                    return dt.strftime("%b %d, %-I:%M %p")
            except Exception:
                return iso_str[:16] if iso_str else ""

        with p.thread_container:
            if not classified:
                ui.label("No conversations yet." if _SIDEBAR_FILTER == "all"
                         else "Nothing in this filter.").classes(
                    "text-grey-6 text-sm q-px-sm"
                )
                return

            visible = classified[:SIDEBAR_MAX_THREADS]
            for row, _cat in visible:
                tid, name, created, updated, *_rest = row
                _thread_model_ov = _rest[0] if _rest else ""
                _thread_project_id = _rest[1] if len(_rest) > 1 else ""
                name = name or ""
                is_active = tid == state.thread_id
                is_running = tid in running_tids
                is_generating_tid = tid in _active_generations
                is_cloud_thread = is_cloud_model(_thread_model_ov or get_current_model())
                is_designer_thread = bool(_thread_project_id)

                async def _select(t=tid, n=name, mo=_thread_model_ov, pid=_thread_project_id):
                    prev = state.thread_id
                    prev_gen = _active_generations.get(prev) if prev else None
                    if prev_gen and prev_gen.status == "streaming":
                        prev_gen.detached = True
                        if prev_gen.tts_active:
                            state.tts_service.stop()
                            prev_gen.tts_active = False

                    async def _load_messages_cached(tid_: str) -> list[dict]:
                        cached = state.message_cache.get(tid_)
                        if cached is not None and tid_ not in state.message_cache_dirty:
                            return list(cached)
                        msgs = await run.io_bound(load_thread_messages, tid_)
                        state.message_cache[tid_] = list(msgs)
                        state.message_cache_dirty.discard(tid_)
                        return msgs

                    if pid:
                        # Designer thread — open the associated project
                        from designer.storage import load_project
                        proj = load_project(pid)
                        if proj:
                            state.thread_id = t
                            state.thread_name = n
                            state.thread_model_override = mo or ""
                            state.messages = await _load_messages_cached(t)
                            p.pending_files.clear()
                            set_active_thread(t, previous_id=prev)
                            state.active_designer_project = proj
                            rebuild_main()
                            _rebuild_thread_list_ref[0]()
                            return
                        # Project missing — fall through to normal thread behaviour

                    # Non-designer thread (or missing project) — close designer if open
                    state.active_designer_project = None
                    state.thread_id = t
                    state.thread_name = n
                    state.thread_model_override = mo or ""
                    state.messages = await _load_messages_cached(t)
                    p.pending_files.clear()
                    set_active_thread(t, previous_id=prev)
                    rebuild_main()
                    _rebuild_thread_list_ref[0]()

                def _delete(t=tid):
                    _del_gen = _active_generations.get(t)
                    if _del_gen:
                        _del_gen.stop_event.set()
                    stop_task(t)
                    _delete_thread(t)
                    clear_summary_cache(t)
                    from tools.shell_tool import get_session_manager, clear_shell_history
                    get_session_manager().kill_session(t)
                    clear_shell_history(t)
                    from tools.browser_tool import (
                        get_session_manager as get_browser_session_manager,
                        clear_browser_history,
                    )
                    get_browser_session_manager().kill_session(t)
                    clear_browser_history(t)
                    set_active_thread(None, previous_id=t)
                    state.invalidate_thread_cache(t)
                    if state.thread_id == t:
                        state.thread_id = None
                        state.thread_name = None
                        state.messages = []
                        rebuild_main()
                    _rebuild_thread_list_ref[0]()

                with ui.item(on_click=_select).classes("w-full rounded").props(
                    "clickable" + (" active" if is_active else "")
                ).style("min-height: 40px; padding: 4px 8px;"):
                    with ui.item_section().props("avatar").style("min-width: 28px;"):
                        if is_generating_tid:
                            _thr_icon = "autorenew"
                        elif is_running:
                            _thr_icon = "hourglass_top"
                        elif is_designer_thread:
                            _thr_icon = "brush"
                        elif is_cloud_thread:
                            _thr_icon = "cloud"
                        elif name.startswith("✈️"):
                            _thr_icon = "send"
                        elif name.startswith("📧"):
                            _thr_icon = "email"
                        elif name.startswith("⚡"):
                            _thr_icon = "electric_bolt"
                        elif name.startswith("�") or "WhatsApp" in name:
                            _thr_icon = "forum"
                        elif name.startswith("🎮") or "Discord" in name:
                            _thr_icon = "sports_esports"
                        elif name.startswith("�📱"):
                            _thr_icon = "textsms"
                        elif name.startswith("💬"):
                            _thr_icon = "chat"
                        else:
                            _thr_icon = "computer"
                        _icon_el = ui.icon(_thr_icon, size="xs").classes(
                            "text-primary" if is_active else "text-grey-6"
                        )
                        if is_generating_tid:
                            _icon_el.classes(add="thoth-spin")
                    with ui.item_section():
                        ui.item_label(name).classes("ellipsis").style(
                            "font-size: 0.85rem;" + ("font-weight: 600;" if is_active else "")
                        )
                        if updated:
                            ui.item_label(_fmt_ts(updated)).props("caption").classes("text-grey-7").style(
                                "font-size: 0.7rem;"
                            )
                    with ui.item_section().props("side"):
                        ui.button(
                            icon="delete_outline", on_click=lambda e, t=tid: _delete(t)
                        ).props("flat dense round size=xs color=grey-6").on(
                            "click", js_handler="(e) => e.stopPropagation()"
                        )

            if len(threads) > SIDEBAR_MAX_THREADS:
                def _show_all():
                    from ui.bulk_select import BulkSelect, render_bulk_action_bar
                    from ui.confirm import confirm_destructive
                    from threads import delete_threads as _bulk_delete_threads

                    bulk = BulkSelect()

                    def _purge_external(t: str) -> None:
                        """Cleanup outside threads.py: session kills, history,
                        active generation stop, task stop. Safe on missing ids.
                        """
                        try:
                            gen = _active_generations.get(t)
                            if gen:
                                gen.stop_event.set()
                        except Exception:
                            pass
                        try:
                            stop_task(t)
                        except Exception:
                            pass
                        try:
                            clear_summary_cache(t)
                        except Exception:
                            pass
                        try:
                            from tools.shell_tool import (
                                get_session_manager, clear_shell_history,
                            )
                            get_session_manager().kill_session(t)
                            clear_shell_history(t)
                        except Exception:
                            pass
                        try:
                            from tools.browser_tool import (
                                get_session_manager as get_browser_session_manager,
                                clear_browser_history,
                            )
                            get_browser_session_manager().kill_session(t)
                            clear_browser_history(t)
                        except Exception:
                            pass

                    with ui.dialog() as dlg, ui.card().classes("w-96"):
                        with ui.row().classes("w-full items-center justify-between"):
                            ui.label("All Conversations").classes("text-h6")
                            select_btn = ui.button("Select").props(
                                "flat dense no-caps size=sm"
                            )

                        def _toggle_mode():
                            bulk.toggle_mode()
                            select_btn.text = "Done" if bulk.active else "Select"
                            _rebuild_dialog_list()

                        select_btn.on("click", _toggle_mode)

                        # Classify once for filter + pills
                        from threads import get_workflow_thread_ids as _gwf
                        _wf_tids = _gwf()

                        def _cat_modal(pid: str, tid: str) -> str:
                            if pid:
                                return "designer"
                            if tid in _wf_tids:
                                return "workflow"
                            return "chat"

                        _modal_counts = {"all": len(threads), "chat": 0,
                                         "designer": 0, "workflow": 0}
                        for _r in threads:
                            _pid = _r[5] if len(_r) > 5 else ""
                            _modal_counts[_cat_modal(_pid, _r[0])] += 1

                        filter_row = ui.row().classes(
                            "w-full gap-1 items-center q-mb-xs"
                        ).style("flex-wrap: wrap;")

                        def _render_modal_pills():
                            filter_row.clear()
                            global _MODAL_FILTER
                            pills = [
                                ("all", "All", _modal_counts["all"]),
                                ("chat", "Chats", _modal_counts["chat"]),
                                ("designer", "Designs",
                                 _modal_counts["designer"]),
                                ("workflow", "Workflows",
                                 _modal_counts["workflow"]),
                            ]
                            with filter_row:
                                for key, label, n in pills:
                                    if key != "all" and n == 0:
                                        continue
                                    is_on = _MODAL_FILTER == key

                                    def _set_mf(k=key):
                                        global _MODAL_FILTER
                                        _MODAL_FILTER = k
                                        _render_modal_pills()
                                        _rebuild_dialog_list()

                                    ui.button(
                                        f"{label} {n}" if n else label,
                                        on_click=_set_mf,
                                    ).props(
                                        "dense no-caps size=sm rounded "
                                        + ("color=amber" if is_on else "flat color=grey-5")
                                    ).style(
                                        "font-size: 0.72rem; padding: 2px 8px;"
                                    )

                        _render_modal_pills()

                        list_container = ui.column().classes("w-full gap-0")

                        def _rebuild_dialog_list() -> None:
                            list_container.clear()
                            # Filtered view of threads
                            _filtered = [
                                r for r in threads
                                if (_MODAL_FILTER == "all"
                                    or _cat_modal(r[5] if len(r) > 5 else "",
                                                  r[0]) == _MODAL_FILTER)
                            ]
                            with list_container:
                                if not _filtered:
                                    ui.label("Nothing in this filter.").classes(
                                        "text-grey-6 q-pa-md"
                                    )
                                    return
                                with ui.list().props("bordered separator").classes("w-full"):
                                    for row in _filtered:
                                        tid, name, created, updated, *_rest2 = row
                                        _mo2 = _rest2[0] if _rest2 else ""

                                        def _sel(t=tid, n=name, mo=_mo2):
                                            # In selection mode, clicking a row toggles selection
                                            if bulk.active:
                                                bulk.toggle_item(t)
                                                return
                                            prev = state.thread_id
                                            prev_gen = _active_generations.get(prev) if prev else None
                                            if prev_gen and prev_gen.status == "streaming":
                                                prev_gen.detached = True
                                                if prev_gen.tts_active:
                                                    state.tts_service.stop()
                                                    prev_gen.tts_active = False
                                            state.thread_id = t
                                            state.thread_name = n
                                            state.thread_model_override = mo or ""
                                            state.messages = load_thread_messages(t)
                                            dlg.close()
                                            rebuild_main()
                                            _rebuild_thread_list_ref[0]()

                                        def _del(t=tid):
                                            _purge_external(t)
                                            _delete_thread(t)
                                            if state.thread_id == t:
                                                state.thread_id = None
                                                state.messages = []
                                            dlg.close()
                                            rebuild_main()
                                            _rebuild_thread_list_ref[0]()

                                        with ui.item(on_click=_sel).props("clickable"):
                                            if bulk.active:
                                                with ui.item_section().props("avatar").style("min-width: 28px;"):
                                                    cb = ui.checkbox(value=bulk.is_selected(tid))
                                                    cb.on(
                                                        "update:model-value",
                                                        lambda e, t=tid: bulk.toggle_item(
                                                            t, bool(e.args),
                                                        ),
                                                    )
                                                    cb.on(
                                                        "click",
                                                        js_handler="(e) => e.stopPropagation()",
                                                    )
                                            else:
                                                with ui.item_section().props("avatar").style("min-width: 28px;"):
                                                    ui.icon("chat_bubble_outline", size="xs")
                                            with ui.item_section():
                                                ui.item_label(name)
                                                if updated:
                                                    ui.item_label(_fmt_ts(updated)).props("caption")
                                            if not bulk.active:
                                                with ui.item_section().props("side"):
                                                    ui.button(
                                                        icon="delete_outline",
                                                        on_click=lambda e, t=tid: _del(t),
                                                    ).props(
                                                        "flat dense round size=xs color=grey-6"
                                                    ).on(
                                                        "click",
                                                        js_handler="(e) => e.stopPropagation()",
                                                    )

                        action_slot = ui.column().classes("w-full")

                        def _do_bulk_delete(ids: list[str]) -> None:
                            def _commit():
                                for t in ids:
                                    _purge_external(t)
                                deleted, failures = _bulk_delete_threads(ids)
                                if state.thread_id in ids:
                                    state.thread_id = None
                                    state.thread_name = None
                                    state.messages = []
                                msg = f"🗑️ Deleted {deleted} conversation{'s' if deleted != 1 else ''}."
                                if failures:
                                    msg += f" {len(failures)} failed."
                                ui.notify(msg, type="negative" if failures else "info")
                                dlg.close()
                                rebuild_main()
                                _rebuild_thread_list_ref[0]()

                            noun = "conversation" if len(ids) == 1 else "conversations"
                            confirm_destructive(
                                f"Delete {len(ids)} {noun}?",
                                body=(
                                    "This cannot be undone. Sessions, media, "
                                    "and history will be cleared."
                                ),
                                on_confirm=_commit,
                            )

                        with action_slot:
                            render_bulk_action_bar(
                                bulk,
                                on_delete=_do_bulk_delete,
                                label_singular="conversation",
                                label_plural="conversations",
                                on_clear=_rebuild_dialog_list,
                            )

                        ui.separator()
                        with ui.row().classes("w-full gap-2"):
                            def _delete_all():
                                # Respect current filter: only nuke what's visible.
                                all_ids = [
                                    r[0] for r in threads
                                    if (_MODAL_FILTER == "all"
                                        or _cat_modal(r[5] if len(r) > 5 else "",
                                                      r[0]) == _MODAL_FILTER)
                                ]
                                if not all_ids:
                                    ui.notify("Nothing to delete in this filter.",
                                              type="warning")
                                    return

                                def _commit():
                                    for t in all_ids:
                                        _purge_external(t)
                                    _bulk_delete_threads(all_ids)
                                    state.thread_id = None
                                    state.thread_name = None
                                    state.messages = []
                                    dlg.close()
                                    rebuild_main()
                                    _rebuild_thread_list_ref[0]()

                                scope = (
                                    "conversations"
                                    if _MODAL_FILTER == "all"
                                    else {
                                        "chat": "chats",
                                        "designer": "design conversations",
                                        "workflow": "workflow conversations",
                                    }[_MODAL_FILTER]
                                )
                                confirm_destructive(
                                    f"Delete all {len(all_ids)} {scope}?",
                                    body="This cannot be undone.",
                                    on_confirm=_commit,
                                )

                            ui.button("Delete all", icon="delete_sweep", on_click=_delete_all).props(
                                "flat color=negative"
                            ).classes("flex-grow")
                            ui.button("Close", on_click=dlg.close).props("flat").classes("flex-grow")

                        _rebuild_dialog_list()
                    dlg.open()

                ui.button(
                    f"Show all ({len(threads)})", on_click=_show_all
                ).classes("w-full q-mt-xs").props("flat dense size=sm")

    _rebuild_thread_list_ref[0] = _rebuild_thread_list
    _rebuild_thread_list()

    return _rebuild_thread_list
