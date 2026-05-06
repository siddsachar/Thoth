"""Thoth UI — Command Center (right drawer).

Fixed right-side panel with live workflow monitoring, approvals,
quick launch, and recent run history.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable

from nicegui import ui
from ui.timer_utils import defer_ui, safe_timer

from ui.state import AppState, P, _active_generations

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────

_STATUS_DOT: dict[str, tuple[str, str]] = {
    "completed":                ("check_circle",  "positive"),
    "completed_delivery_failed": ("warning",       "warning"),
    "failed":                   ("error",          "negative"),
    "stopped":                  ("stop_circle",    "orange"),
    "paused":                   ("pause_circle",   "amber"),
    "running":                  ("play_circle",    "primary"),
    "cancelled":                ("cancel",         "grey-6"),
}


def _relative_time(iso: str) -> str:
    """Return a human-friendly relative time like '2m ago' or 'in 1h 30m'."""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso or ""
    now = datetime.now(dt.tzinfo)          # match tz-awareness of parsed dt
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        # future
        secs = abs(secs)
        if secs < 60:
            return f"in {secs}s"
        if secs < 3600:
            return f"in {secs // 60}m"
        h, m = divmod(secs, 3600)
        return f"in {h}h {m // 60 :>02}m"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        h, m = divmod(secs, 3600)
        return f"{h}h {m // 60 :>02}m ago"
    return f"{secs // 86400}d ago"


def _elapsed(iso_start: str) -> str:
    """Return elapsed time as 'Xm Ys'."""
    try:
        dt = datetime.fromisoformat(iso_start)
    except (ValueError, TypeError):
        return ""
    secs = max(0, int((datetime.now() - dt).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# ═════════════════════════════════════════════════════════════════════════
# MAIN BUILDER
# ═════════════════════════════════════════════════════════════════════════

def build_command_center(
    state: AppState,
    p: P,
    *,
    rebuild_main: Callable[[], None],
    rebuild_thread_list: Callable[[], None],
    show_task_dialog: Callable,
    load_thread_messages: Callable[[str], list[dict]],
) -> None:
    """Build the always-open right drawer with 5 workflow sections."""
    from tasks import (
        get_running_tasks, get_task_logs, get_task, stop_task,
        get_pending_approvals, respond_to_approval,
        get_next_fire_times, get_recent_runs,
        list_tasks, run_task_background, get_running_task_thread,
        _prepare_task_thread,
    )
    from memory_extraction import set_active_thread

    with ui.right_drawer(value=True, fixed=True).style(
        "width: 440px; padding: 0;"
    ).classes("thoth-panel-card").props('no-swipe-open no-swipe-close width=440'):
        with ui.scroll_area().classes("w-full h-full"):
          with ui.column().classes("w-full gap-2").style(
              "overflow: hidden; padding: 6px 8px;"
          ):
            with ui.column().classes("w-full gap-2 thoth-inner-panel"):
                with ui.column().classes("w-full gap-0"):
                    ui.label("Workflow Console").classes(
                        "text-subtitle1 font-bold"
                    ).style("color: gold; letter-spacing: 0.5px;")
                    ui.label(
                        "Background Agents"
                    ).classes("text-xs text-grey-6").style(
                        "margin-top: -2px; letter-spacing: 0.3px;"
                    )

                # ════════════════════════════════════════════════════
                # §1  RUNNING
                # ════════════════════════════════════════════════════
                ui.separator().classes("q-my-none")
                _live_container = ui.column().classes("w-full gap-0")

                def _rebuild_live() -> None:
                    _live_container.clear()
                    running = get_running_tasks()
                    # Also include paused runs (not in _active_runs)
                    paused_runs = [
                        r for r in get_recent_runs(10)
                        if r.get("status") == "paused"
                    ]
                    with _live_container:
                        ui.label("▶ Running").classes(
                            "text-xs font-bold text-grey-5"
                        ).style("letter-spacing: 0.8px; text-transform: uppercase;")
                        if not running and not paused_runs:
                            with ui.row().classes(
                                "w-full justify-center q-py-sm"
                            ).style("opacity: 0.4;"):
                                ui.icon("play_circle", size="sm").classes("text-grey-7")
                                ui.label("No workflows running").classes("text-xs text-grey-7")
                            return

                        # Running tasks
                        for tid, info in running.items():
                            _render_live_task(tid, info, is_paused=False)

                        # Paused tasks (from DB, not in _active_runs)
                        for pr in paused_runs:
                            _tid = pr.get("thread_id", "")
                            if _tid not in running:
                                _render_paused_task(pr)

                def _render_live_task(tid: str, info: dict, *, is_paused: bool) -> None:
                    icon = info.get("icon", "⚡")
                    name = info.get("name", "Task")
                    step = info.get("step", 0)
                    total = info.get("total", 0)
                    step_label = info.get("step_label", "")
                    started = info.get("started_at", "")

                    with ui.card().classes("w-full q-my-xs").style(
                        "padding: 0.4rem 0.5rem;"
                        "border-left: 3px solid #4caf50;"
                        "overflow: hidden; box-sizing: border-box;"
                    ):
                        # Header: icon + name + badge + stop
                        with ui.row().classes("w-full items-center no-wrap gap-1").style(
                            "overflow: hidden;"
                        ):
                            ui.label(icon).style("font-size: 1rem;")
                            ui.label(name).classes(
                                "font-bold text-xs ellipsis"
                            ).style("flex: 1; min-width: 0;")
                            ui.badge("running", color="green").props(
                                "dense"
                            ).classes("text-xs")
                            def _stop(t=tid, n=name):
                                stop_task(t)
                                ui.notify(f"⏹ Stopping {n}…", type="warning")
                            ui.button(icon="stop", on_click=_stop).props(
                                "round flat dense size=xs"
                            ).style("color: #ff6b6b;").tooltip("Stop")

                        # Step info + progress
                        if total > 0:
                            with ui.row().classes("w-full items-center no-wrap gap-1"):
                                ui.label(
                                    f"Step {step + 1}/{total}"
                                ).classes("text-xs text-grey-6")
                                ui.linear_progress(
                                    value=(step + 1) / total,
                                    show_value=False
                                ).classes("flex-grow").props(
                                    "color=amber"
                                ).style("height: 4px;")
                                if started:
                                    ui.label(_elapsed(started)).classes(
                                        "text-xs text-grey-7"
                                    )

                        # Step label
                        if step_label:
                            ui.label(step_label).classes(
                                "text-xs text-grey-5 ellipsis"
                            ).style("max-width: 100%;")

                        # Expandable log
                        logs = get_task_logs(tid, 15)
                        if logs:
                            with ui.expansion("Log", icon="terminal").props(
                                "dense"
                            ).classes("w-full text-xs").style(
                                "min-width: 0; max-width: 100%; overflow: hidden;"
                            ):
                                ui.html(
                                    '<pre style="'
                                    "font-size: 0.65rem; line-height: 1.3;"
                                    "background: rgba(0,0,0,0.3); padding: 6px;"
                                    "border-radius: 4px; margin: 0;"
                                    "max-height: 180px; overflow-y: auto;"
                                    "overflow-x: hidden;"
                                    "white-space: pre-wrap; word-break: break-all;"
                                    '">'
                                    + _escape_html("\n".join(logs))
                                    + "</pre>",
                                    sanitize=False,
                                )

                def _render_paused_task(run: dict) -> None:
                    icon = run.get("task_icon", "⚡")
                    name = run.get("task_name", "Task")
                    step = run.get("steps_done", 0)
                    total = run.get("steps_total", 0)

                    with ui.card().classes("w-full q-my-xs").style(
                        "padding: 0.4rem 0.5rem;"
                        "border-left: 3px solid #f0c040;"
                        "overflow: hidden; box-sizing: border-box;"
                    ):
                        with ui.row().classes("w-full items-center no-wrap gap-1").style(
                            "overflow: hidden;"
                        ):
                            ui.label(icon).style("font-size: 1rem;")
                            ui.label(name).classes(
                                "font-bold text-xs ellipsis"
                            ).style("flex: 1; min-width: 0;")
                            ui.badge("paused", color="amber").props(
                                "dense"
                            ).classes("text-xs")
                        if total > 0:
                            ui.label(
                                f"Step {step}/{total} · Waiting for approval"
                            ).classes("text-xs text-grey-6")

                _rebuild_live()
                safe_timer(3.0, _rebuild_live)

                # ════════════════════════════════════════════════════
                # §2  PENDING APPROVALS
                # ════════════════════════════════════════════════════
                ui.separator().classes("q-my-none")
                _approvals_container = ui.column().classes("w-full gap-0")

                def _rebuild_approvals() -> None:
                    _approvals_container.clear()
                    try:
                        pending = get_pending_approvals()
                    except Exception:
                        pending = []
                    with _approvals_container:
                        count_label = f" ({len(pending)})" if pending else ""
                        ui.label(f"⏳ Approvals{count_label}").classes(
                            "text-xs font-bold text-grey-5"
                        ).style(
                            "letter-spacing: 0.8px; text-transform: uppercase;"
                        )
                        if not pending:
                            ui.label("No pending approvals").classes(
                                "text-xs text-grey-7 q-ml-sm"
                            ).style("opacity: 0.5;")
                            return
                        for appr in pending:
                            _render_approval(appr)

                def _render_approval(appr: dict) -> None:
                    with ui.card().classes("w-full q-my-xs").style(
                        "padding: 0.4rem 0.5rem;"
                        "border-left: 3px solid #f0c040;"
                        "overflow: hidden; box-sizing: border-box;"
                    ):
                        with ui.row().classes("w-full items-center no-wrap gap-1").style(
                            "overflow: hidden;"
                        ):
                            ui.label("🔔").style("font-size: 0.9rem;")
                            ui.label(
                                appr.get("task_name", "Task")
                            ).classes(
                                "font-bold text-xs ellipsis"
                            ).style("flex: 1; min-width: 0;")
                            # Timeout countdown
                            timeout_at = appr.get("timeout_at", "")
                            if timeout_at:
                                remaining = _relative_time(
                                    datetime.now().isoformat()
                                )
                                try:
                                    to_dt = datetime.fromisoformat(timeout_at)
                                    secs = max(0, int(
                                        (to_dt - datetime.now()).total_seconds()
                                    ))
                                    if secs > 0:
                                        m, s = divmod(secs, 60)
                                        ui.label(f"{m}:{s:02d}").classes(
                                            "text-xs text-grey-7"
                                        )
                                except (ValueError, TypeError):
                                    pass
                        msg = appr.get("message", "")
                        if msg:
                            ui.label(
                                (msg[:100] + "…") if len(msg) > 100 else msg
                            ).classes("text-xs text-grey-5")
                        with ui.row().classes("gap-1 q-mt-xs"):
                            async def _approve(tok=appr["resume_token"]):
                                from nicegui import run
                                ok = await run.io_bound(
                                    respond_to_approval, tok, True
                                )
                                ui.notify(
                                    "✅ Approved" if ok else "ℹ️ Already handled",
                                    type="positive" if ok else "info",
                                )
                                _rebuild_approvals()
                                _rebuild_live()

                            async def _deny(tok=appr["resume_token"]):
                                from nicegui import run
                                ok = await run.io_bound(
                                    respond_to_approval, tok, False
                                )
                                ui.notify(
                                    "❌ Denied" if ok else "ℹ️ Already handled",
                                    type="warning" if ok else "info",
                                )
                                _rebuild_approvals()
                                _rebuild_live()

                            ui.button("✅ Approve", on_click=_approve).props(
                                "unelevated dense no-caps size=xs"
                            ).style("background: #2d8a4e; color: white;")
                            ui.button("❌ Deny", on_click=_deny).props(
                                "flat dense no-caps size=xs"
                            ).style("color: #ff6b6b;")

                _rebuild_approvals()
                safe_timer(5.0, _rebuild_approvals)

                # ════════════════════════════════════════════════════
                # §3  UPCOMING SCHEDULE
                # ════════════════════════════════════════════════════
                ui.separator().classes("q-my-none")
                _upcoming_container = ui.column().classes("w-full gap-0")

                def _rebuild_upcoming() -> None:
                    _upcoming_container.clear()
                    with _upcoming_container:
                        ui.label("📅 Upcoming").classes(
                            "text-xs font-bold text-grey-5"
                        ).style(
                            "letter-spacing: 0.8px; text-transform: uppercase;"
                        )
                        upcoming = get_next_fire_times(5)
                        if not upcoming:
                            ui.label("No scheduled tasks").classes(
                                "text-xs text-grey-7 q-ml-sm"
                            ).style("opacity: 0.5;")
                            return
                        for item in upcoming:
                            with ui.row().classes(
                                "w-full items-center no-wrap gap-1 q-py-xs"
                            ).style("overflow: hidden;"):
                                ui.label(
                                    item.get("task_icon", "⚡")
                                ).style("font-size: 0.85rem;")
                                ui.label(
                                    item.get("task_name", "?")
                                ).classes(
                                    "text-xs ellipsis"
                                ).style("flex: 1; min-width: 0;")
                                nr = item.get("next_run", "")
                                ui.label(_relative_time(nr)).classes(
                                    "text-xs text-grey-6"
                                )

                _rebuild_upcoming()
                safe_timer(30.0, _rebuild_upcoming)

                # ════════════════════════════════════════════════════
                # §4  QUICK LAUNCH
                # ════════════════════════════════════════════════════
                ui.separator().classes("q-my-none")
                ui.label("🚀 Quick Launch").classes(
                    "text-xs font-bold text-grey-5"
                ).style(
                    "letter-spacing: 0.8px; text-transform: uppercase;"
                )

                _task_select = ui.select(
                    options=[], label="Workflow",
                ).classes("w-full").props("dense outlined")

                def _refresh_task_options() -> None:
                    tasks = list_tasks()
                    opts = {
                        t["id"]: f"{t.get('icon', '⚡')} {t['name']}"
                        for t in tasks
                        if t.get("enabled", True)
                    }
                    _task_select.options = opts
                    _task_select.update()

                _refresh_task_options()
                safe_timer(3.0, _refresh_task_options)

                with ui.row().classes("w-full gap-1"):
                    def _run_selected():
                        task_id = _task_select.value
                        if not task_id:
                            ui.notify("Select a workflow first", type="warning")
                            return
                        task = get_task(task_id)
                        if not task:
                            ui.notify("Workflow not found", type="negative")
                            return
                        tid = _prepare_task_thread(task)
                        from tools import registry as tool_registry
                        bg_tools = [
                            tl.name for tl in tool_registry.get_enabled_tools()
                        ]
                        run_task_background(
                            task_id, tid, bg_tools,
                            start_step=0, notification=True,
                        )
                        ui.notify(
                            f"⚡ {task['name']} started",
                            type="positive",
                        )
                        rebuild_thread_list()
                        _refresh_task_options()
                        defer_ui(_rebuild_live, delay=0.5)

                    ui.button(
                        "▶ Run", on_click=_run_selected
                    ).props(
                        "unelevated dense no-caps color=green"
                    ).classes("flex-grow")

                    def _new_workflow():
                        show_task_dialog(None, lambda: (
                            _refresh_task_options(),
                            rebuild_main(),
                        ))

                    ui.button(
                        "+ New", on_click=_new_workflow
                    ).props("outline dense no-caps").classes("flex-grow")

                # ════════════════════════════════════════════════════
                # §5  RECENT RUNS
                # ════════════════════════════════════════════════════
                ui.separator().classes("q-my-none")
                _recent_container = ui.column().classes("w-full gap-0")

                def _rebuild_recent() -> None:
                    _recent_container.clear()
                    recent = get_recent_runs(8)
                    with _recent_container:
                        ui.label("🕐 Recent Runs").classes(
                            "text-xs font-bold text-grey-5"
                        ).style(
                            "letter-spacing: 0.8px; text-transform: uppercase;"
                        )
                        if not recent:
                            ui.label("No runs yet").classes(
                                "text-xs text-grey-7 q-ml-sm"
                            ).style("opacity: 0.5;")
                            return
                        for r in recent:
                            _render_recent_run(r)

                def _render_recent_run(r: dict) -> None:
                    status = r.get("status", "unknown")
                    s_icon, s_color = _STATUS_DOT.get(
                        status, ("pending", "grey-6")
                    )
                    thread_id = r.get("thread_id", "")

                    def _navigate(tid=thread_id, tname=r.get("task_name", "")):
                        if not tid:
                            return
                        prev = state.thread_id
                        prev_gen = _active_generations.get(prev) if prev else None
                        if prev_gen and prev_gen.status == "streaming":
                            prev_gen.detached = True
                        state.thread_id = tid
                        state.thread_name = tname
                        state.messages = load_thread_messages(tid)
                        p.pending_files.clear()
                        set_active_thread(tid, previous_id=prev)
                        rebuild_main()
                        rebuild_thread_list()

                    with ui.row().classes(
                        "w-full items-center no-wrap gap-1 q-py-xs"
                    ).style(
                        ("cursor: pointer; " if thread_id else "")
                        + "overflow: hidden;"
                    ).on("click", _navigate if thread_id else lambda: None):
                        ui.label(
                            r.get("task_icon", "⚡")
                        ).style("font-size: 0.85rem;")
                        ui.label(
                            r.get("task_name", "?")
                        ).classes(
                            "text-xs ellipsis"
                        ).style("flex: 1; min-width: 0;")
                        ui.icon(s_icon, size="xs").classes(f"text-{s_color}")
                        started = r.get("started_at", "")
                        if started:
                            ui.label(_relative_time(started)).classes(
                                "text-xs text-grey-7"
                            )
                        # Retry button for failed runs
                        if status == "failed":
                            task_id = r.get("task_id", "")

                            def _retry(tid_r=task_id):
                                task = get_task(tid_r)
                                if not task:
                                    ui.notify("Workflow not found", type="negative")
                                    return
                                new_tid = _prepare_task_thread(task)
                                from tools import registry as tool_registry
                                bg_tools = [
                                    tl.name
                                    for tl in tool_registry.get_enabled_tools()
                                ]
                                run_task_background(
                                    tid_r, new_tid, bg_tools,
                                    start_step=0, notification=True,
                                )
                                ui.notify("🔄 Retrying…", type="positive")
                                rebuild_thread_list()
                                defer_ui(_rebuild_live, delay=0.5)
                                defer_ui(_rebuild_recent, delay=1.0)

                            ui.button(
                                icon="refresh", on_click=_retry
                            ).props(
                                "round flat dense size=xs"
                            ).style("color: #f0c040;").tooltip("Retry").on(
                                "click",
                                js_handler="(e) => e.stopPropagation()",
                            )

                _rebuild_recent()
                safe_timer(10.0, _rebuild_recent)

            # ════════════════════════════════════════════════════
            # §6  INSIGHTS  (separate inner panel)
            # ════════════════════════════════════════════════════
            with ui.column().classes("w-full gap-2 thoth-inner-panel"):
                _insights_container = ui.column().classes("w-full gap-0")

                def _rebuild_insights() -> None:
                    _insights_container.clear()
                    try:
                        from insights import (
                            get_active_insights, get_insights_meta,
                            dismiss_insight, pin_insight,
                            update_insight_status,
                            CATEGORY_ICONS, SEVERITY_SORT,
                        )
                    except ImportError:
                        return

                    active = get_active_insights()
                    meta = get_insights_meta()
                    last_analysis = meta.get("last_analysis", "")

                    with _insights_container:
                        # Header
                        with ui.row().classes(
                            "w-full items-center no-wrap gap-1"
                        ):
                            ui.label("💡 Insights").classes(
                                "text-xs font-bold text-grey-5"
                            ).style(
                                "letter-spacing: 0.8px;"
                                " text-transform: uppercase;"
                            )
                            if active:
                                ui.badge(
                                    str(len(active)), color="amber"
                                ).props("dense").classes("text-xs")
                            if last_analysis:
                                ui.label(
                                    _relative_time(last_analysis)
                                ).classes("text-xs text-grey-7").style(
                                    "margin-left: auto;"
                                )

                        if not active:
                            with ui.row().classes(
                                "w-full justify-center q-py-sm"
                            ).style("opacity: 0.4;"):
                                ui.icon("lightbulb", size="sm").classes(
                                    "text-grey-7"
                                )
                                ui.label("No insights yet").classes(
                                    "text-xs text-grey-7"
                                )
                            return

                        for ins in active[:10]:
                            _render_insight_card(
                                ins, _rebuild_insights,
                                state, p, rebuild_main,
                                rebuild_thread_list,
                                load_thread_messages,
                            )

                def _render_insight_card(
                    ins: dict,
                    refresh_fn,
                    state: AppState,
                    p: P,
                    rebuild_main,
                    rebuild_thread_list,
                    load_thread_messages,
                ) -> None:
                    from insights import (
                        dismiss_insight, pin_insight,
                        update_insight_status,
                        CATEGORY_ICONS, SEVERITY_SORT,
                    )

                    cat = ins.get("category", "system_health")
                    sev = ins.get("severity", "info")
                    icon = CATEGORY_ICONS.get(cat, "💡")
                    title = ins.get("title", "Untitled")
                    body = ins.get("body", "")
                    suggestion = ins.get("suggestion", "")
                    iid = ins["id"]
                    is_pinned = ins.get("status") == "pinned"
                    has_draft = bool(ins.get("skill_draft"))
                    can_apply = (
                        cat == "skill_proposal"
                        and bool(ins.get("auto_fixable"))
                        and has_draft
                    )

                    sev_colors = {
                        "critical": "#ff5252",
                        "warning": "#f0c040",
                        "info": "rgba(255,255,255,0.15)",
                    }
                    border_color = sev_colors.get(sev, sev_colors["info"])

                    with ui.card().classes("w-full q-my-xs").style(
                        f"padding: 0.4rem 0.5rem;"
                        f" border-left: 3px solid {border_color};"
                        f" overflow: hidden; box-sizing: border-box;"
                    ):
                        # Title row
                        with ui.row().classes(
                            "w-full items-center no-wrap gap-1"
                        ).style("overflow: hidden;"):
                            ui.label(icon).style("font-size: 0.85rem;")
                            ui.label(title).classes(
                                "text-xs font-bold ellipsis"
                            ).style("flex: 1; min-width: 0;")
                            if is_pinned:
                                ui.icon(
                                    "push_pin", size="xs"
                                ).classes("text-amber")

                        # Body
                        if body:
                            ui.label(body).classes(
                                "text-xs text-grey-6"
                            ).style(
                                "white-space: normal;"
                                " word-break: break-word;"
                            )

                        # Suggestion
                        if suggestion:
                            ui.label(
                                f"💬 {suggestion}"
                            ).classes("text-xs text-grey-5").style(
                                "white-space: normal;"
                                " word-break: break-word;"
                                " font-style: italic;"
                            )

                        # Action buttons
                        with ui.row().classes(
                            "w-full items-center gap-1 q-mt-xs"
                        ):
                            # Dismiss
                            def _dismiss(i=iid):
                                dismiss_insight(i)
                                ui.notify("Insight dismissed", type="info")
                                refresh_fn()

                            ui.button(
                                "Dismiss", on_click=_dismiss
                            ).props(
                                "flat dense no-caps size=xs"
                            ).style("color: #999;")

                            # Pin / Unpin
                            if is_pinned:
                                def _unpin(i=iid):
                                    update_insight_status(i, "new")
                                    refresh_fn()

                                ui.button(
                                    "Unpin", on_click=_unpin
                                ).props(
                                    "flat dense no-caps size=xs"
                                ).style("color: #f0c040;")
                            else:
                                def _pin(i=iid):
                                    pin_insight(i)
                                    ui.notify("Insight pinned", type="positive")
                                    refresh_fn()

                                ui.button(
                                    "Pin", on_click=_pin
                                ).props(
                                    "flat dense no-caps size=xs"
                                ).style("color: #f0c040;")

                            # Investigate — opens chat with context
                            def _investigate(
                                ins_title=title,
                                ins_body=body,
                                ins_sug=suggestion,
                            ):
                                from memory_extraction import set_active_thread
                                from threads import _save_thread_meta
                                new_tid = uuid.uuid4().hex[:12]
                                _save_thread_meta(
                                    new_tid,
                                    f"Investigate: {ins_title}",
                                )
                                state.thread_id = new_tid
                                state.thread_name = (
                                    f"Investigate: {ins_title}"
                                )
                                state.messages = []
                                p.pending_files.clear()
                                set_active_thread(new_tid)
                                rebuild_main()
                                rebuild_thread_list()
                                # Pre-fill input with context
                                msg = (
                                    f"An automated insight was generated:"
                                    f"\n\n**{ins_title}**\n{ins_body}"
                                )
                                if ins_sug:
                                    msg += f"\n\nSuggestion: {ins_sug}"
                                msg += (
                                    "\n\nPlease investigate this and"
                                    " suggest what I should do."
                                )
                                if p.chat_input:
                                    p.chat_input.value = msg

                            ui.button(
                                "Investigate", on_click=_investigate
                            ).props(
                                "flat dense no-caps size=xs"
                            ).style("color: #64b5f6;")

                            # Apply — only for skill_proposal with draft
                            if can_apply:
                                def _apply(i=iid):
                                    _apply_skill_draft(i, refresh_fn)

                                ui.button(
                                    "Apply", on_click=_apply
                                ).props(
                                    "flat dense no-caps size=xs"
                                ).style("color: #66bb6a;")

                _rebuild_insights()
                safe_timer(30.0, _rebuild_insights)


def _apply_skill_draft(
    insight_id: str, refresh_fn
) -> None:
    """Apply a skill-proposal insight through the backend helper."""
    from nicegui import ui as _ui

    try:
        from insights import apply_insight

        result = apply_insight(insight_id)
        _ui.notify(
            result.get("message", "Failed to apply insight"),
            type="positive" if result.get("ok") else "warning",
        )
        if result.get("ok"):
            refresh_fn()
    except Exception as exc:
        _ui.notify(f"Failed to apply insight: {exc}", type="negative")


def _escape_html(text: str) -> str:
    """Minimal HTML escape for log display."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
