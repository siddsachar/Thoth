"""Row-Bot UI — Activity Center (right drawer).

Fixed right-side panel with current work, approvals, schedules, channels,
operational insights, and compact launch controls.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from row_bot.brand import APP_BRAND_ACCENT
from nicegui import ui
from row_bot.ui.timer_utils import defer_ui, safe_timer

from row_bot.ui.state import AppState, P, _active_generations
from row_bot.ui.render import open_agent_peek_dialog
from row_bot.ui.iconography import material_icon_for

logger = logging.getLogger(__name__)

_COMMAND_CENTER_EXPANDED_WIDTH = 440
_COMMAND_CENTER_COLLAPSED_WIDTH = 64
_COMMAND_CENTER_CONFIG_KEY = "workflow_console_collapsed"
_ACTIVITY_AGENT_ATTENTION_STATUSES = ("waiting_approval", "waiting_user", "blocked")


def _activity_attention_statuses() -> list[str]:
    """Statuses that represent actionable child Agent attention."""
    return list(_ACTIVITY_AGENT_ATTENTION_STATUSES)

_COMMAND_CENTER_CSS = """
<style>
.row-bot-command-center-drawer {
    transition: width 180ms ease, max-width 180ms ease;
    overflow: hidden;
    max-width: 100vw;
    box-sizing: border-box;
}
.row-bot-command-center-drawer *,
.row-bot-command-center-drawer *::before,
.row-bot-command-center-drawer *::after {
    box-sizing: border-box;
}
.workflow-console-rail {
    position: absolute;
    inset: 0 auto 0 0;
    width: 58px;
    z-index: 2;
    display: none;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    padding: 8px 6px;
    border-right: 1px solid rgba(255, 255, 255, 0.08);
    background: rgba(20, 20, 20, 0.88);
}
.workflow-console-toggle.q-btn {
    width: 42px;
    height: 42px;
    border: 1px solid rgba(255, 215, 0, 0.28);
    color: __ROW_BOT_BRAND_ACCENT__;
    background: rgba(255, 215, 0, 0.08);
}
.workflow-console-rail-label {
    writing-mode: vertical-rl;
    transform: rotate(180deg);
    color: #bdbdbd;
    font-size: 0.72rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-top: 4px;
}
.workflow-console-rail-badges {
    display: flex;
    flex-direction: column;
    gap: 7px;
    align-items: center;
}
.workflow-console-rail-badge {
    min-width: 30px;
    border-radius: 999px;
    padding: 2px 6px;
    font-size: 0.70rem;
    font-weight: 700;
    text-align: center;
    border: 1px solid rgba(255, 255, 255, 0.16);
}
.workflow-console-rail-badge.running {
    color: #90caf9;
    background: rgba(33, 150, 243, 0.16);
}
.workflow-console-rail-badge.approval {
    color: #ffcc80;
    background: rgba(255, 167, 38, 0.16);
}
.workflow-console-rail-badge.insights {
    color: #ce93d8;
    background: rgba(156, 39, 176, 0.16);
}
.workflow-console-rail.workflow-console-approval-alert {
    border-right-color: rgba(255, 193, 7, 0.85);
    box-shadow: inset 0 0 0 1px rgba(255, 193, 7, 0.32), 0 0 18px rgba(255, 193, 7, 0.24);
}
.workflow-console-rail.workflow-console-alert-flash {
    animation: workflow-console-approval-flash 1.2s ease-out 0s 3;
}
@keyframes workflow-console-approval-flash {
    0% { box-shadow: inset 0 0 0 1px rgba(255, 193, 7, 0.2), 0 0 0 rgba(255, 193, 7, 0.0); }
    45% { box-shadow: inset 0 0 0 2px rgba(255, 193, 7, 0.95), 0 0 24px rgba(255, 193, 7, 0.55); }
    100% { box-shadow: inset 0 0 0 1px rgba(255, 193, 7, 0.32), 0 0 18px rgba(255, 193, 7, 0.24); }
}
.workflow-console-scroll {
    width: 100%;
    max-width: 100%;
    overflow-x: hidden;
}
.workflow-console-scroll .q-scrollarea__container,
.workflow-console-scroll .q-scrollarea__content {
    width: 100%;
    min-width: 100%;
    max-width: 100%;
}
.workflow-console-content,
.workflow-console-section {
    width: 100%;
    min-width: 100%;
    max-width: 100%;
    align-self: stretch;
    overflow-x: hidden;
}
.row-bot-command-center-insights-expansion,
.row-bot-command-center-insights-expansion .q-expansion-item,
.row-bot-command-center-insights-expansion .q-expansion-item__content,
.row-bot-insight-card,
.row-bot-insight-proposals {
    width: 100%;
    max-width: 100%;
    min-width: 0;
    overflow-x: hidden;
}
.row-bot-insight-proposal-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 26px;
    column-gap: 4px;
    align-items: center;
    width: 100%;
    max-width: 100%;
    min-width: 0;
    overflow: hidden;
    border-top: 1px solid rgba(255,255,255,0.08);
    padding-top: 4px;
}
.row-bot-insight-proposal-main {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    column-gap: 4px;
    row-gap: 3px;
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
}
.row-bot-insight-proposal-title {
    flex: 1 1 100%;
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.row-bot-insight-preview-btn.q-btn {
    width: 24px;
    min-width: 24px;
    height: 24px;
}
.row-bot-command-center-drawer.workflow-console-collapsed .workflow-console-rail {
    display: flex;
}
.row-bot-command-center-drawer.workflow-console-collapsed .workflow-console-scroll {
    display: none;
}
.row-bot-activity-mobile-toggle.q-btn {
    display: none;
}
@media (max-width: 900px) {
    .row-bot-activity-mobile-toggle.q-btn {
        display: inline-flex;
        position: fixed;
        top: 12px;
        right: 12px;
        z-index: 6100;
        width: 42px;
        height: 42px;
        border: 1px solid rgba(255, 215, 0, 0.34);
        background: rgba(20, 20, 20, 0.94);
        color: __ROW_BOT_BRAND_ACCENT__;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.4);
    }
}
</style>
"""
_COMMAND_CENTER_CSS = _COMMAND_CENTER_CSS.replace("__ROW_BOT_BRAND_ACCENT__", APP_BRAND_ACCENT)


def _load_command_center_collapsed() -> bool:
    try:
        from row_bot.ui.helpers import load_app_config
        return bool(load_app_config().get(_COMMAND_CENTER_CONFIG_KEY, False))
    except Exception:
        logger.debug("Failed to load workflow console collapse preference", exc_info=True)
        return False


def _save_command_center_collapsed(collapsed: bool) -> None:
    try:
        from row_bot.ui.helpers import load_app_config, save_app_config
        cfg = load_app_config()
        cfg[_COMMAND_CENTER_CONFIG_KEY] = bool(collapsed)
        save_app_config(cfg)
    except Exception:
        logger.debug("Failed to save workflow console collapse preference", exc_info=True)

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
    open_settings: Callable[..., None] | None = None,
) -> None:
    """Build the always-open right drawer for current activity."""
    from row_bot.tasks import (
        get_running_tasks, get_task_logs, get_task, stop_task,
        get_pending_approvals, respond_to_approval,
        get_next_fire_times,
        list_tasks, run_task_background, get_running_task_thread,
        _prepare_task_thread,
    )
    from row_bot.memory_extraction import set_active_thread

    def _open_agent_thread_from_center(agent_run: dict) -> None:
        from row_bot.ui.agent_drawer import open_agent_thread

        open_agent_thread(
            agent_run,
            state=state,
            p=p,
            rebuild_main=rebuild_main,
            rebuild_thread_list=rebuild_thread_list,
        )

    def _safe_workflow_read(label: str, fn: Callable[[], object], fallback):
        try:
            return fn()
        except Exception as exc:
            logger.warning("Activity Center %s unavailable: %s", label, exc)
            return fallback

    def _render_workflow_unavailable() -> None:
        with ui.card().classes("w-full q-my-xs").style(
            "padding: 0.5rem; border-left: 3px solid #f0c040;"
        ):
            ui.label("Workflow data unavailable").classes("text-xs font-bold")
            ui.label(
                "Task database repair is needed. Restart once, or run "
                "launcher.py --reset-tasks-db."
            ).classes("text-xs text-grey-6")

    ui.html(_COMMAND_CENTER_CSS, sanitize=False)
    collapsed_state = {"value": _load_command_center_collapsed()}
    last_pending_count = {"value": 0}

    def _drawer_width() -> int:
        return _COMMAND_CENTER_COLLAPSED_WIDTH if collapsed_state["value"] else _COMMAND_CENTER_EXPANDED_WIDTH

    def _sync_shell_width_var(width: int) -> None:
        try:
            ui.run_javascript(
                "document.documentElement.style.setProperty("
                f"'--row-bot-command-center-width', '{int(width)}px');"
                "if (window.__rowBotApplyDrawerOverlapGuard) "
                "window.__rowBotApplyDrawerOverlapGuard();"
            )
        except Exception:
            logger.debug("Could not sync Activity Center width CSS variable", exc_info=True)

    with ui.right_drawer(value=False, fixed=True).style(
        f"width: {_drawer_width()}px; padding: 0; position: relative;"
    ).classes("row-bot-panel-card row-bot-command-center-drawer").props(
        f"show-if-above breakpoint=900 no-swipe-open no-swipe-close width={_drawer_width()}"
    ) as drawer:
        drawer._props["data-workflow-console-drawer"] = "1"

        with ui.element("div").classes("workflow-console-rail") as rail_shell:
            rail_shell._props["data-workflow-console-rail"] = "1"
            toggle_btn = ui.button(icon="chevron_right").classes(
                "workflow-console-toggle"
            ).props("round flat dense").tooltip("Toggle Activity Center")
            ui.html('<div class="workflow-console-rail-label">Activity</div>', sanitize=False)
            with ui.element("div").classes("workflow-console-rail-badges"):
                running_badge = ui.html(
                    '<div class="workflow-console-rail-badge running" title="Running goals, agents, and workflows">0</div>',
                    sanitize=False,
                )
                approval_badge = ui.html(
                    '<div class="workflow-console-rail-badge approval" title="Pending approvals">0</div>',
                    sanitize=False,
                )
                insights_badge = ui.html(
                    '<div class="workflow-console-rail-badge insights" title="Active insights">0</div>',
                    sanitize=False,
                )

        def _apply_drawer_state(*, persist: bool = False) -> None:
            width = _drawer_width()
            _sync_shell_width_var(width)
            defer_ui(lambda width=width: _sync_shell_width_var(width), delay=0.05)
            drawer._props["width"] = width
            drawer.style(replace=f"width: {width}px; padding: 0; position: relative;")
            if collapsed_state["value"]:
                drawer.classes(add="workflow-console-collapsed")
                toggle_btn._props["icon"] = "chevron_left"
            else:
                drawer.classes(remove="workflow-console-collapsed")
                toggle_btn._props["icon"] = "chevron_right"
            drawer.update()
            toggle_btn.update()
            if persist:
                _save_command_center_collapsed(collapsed_state["value"])

        def _toggle_drawer() -> None:
            collapsed_state["value"] = not collapsed_state["value"]
            _apply_drawer_state(persist=True)

        toggle_btn.on("click", _toggle_drawer)
        toggle_btn.on("click", js_handler="(e) => e.stopPropagation()")
        rail_shell.on("click", lambda: collapsed_state["value"] and _toggle_drawer())
        _apply_drawer_state()

        def _refresh_rail_counts() -> None:
            try:
                running_count = len(get_running_tasks())
            except Exception:
                running_count = 0
            try:
                from row_bot.agent_runs import list_agent_runs

                running_count += len(list_agent_runs(
                    statuses=["queued", "running", "waiting_approval", "waiting_user", "paused"],
                    kind="subagent",
                    limit=100,
                ))
            except Exception:
                pass
            try:
                from row_bot.goals import list_goals

                running_count += len(list_goals(
                    statuses=["active", "waiting_approval", "paused", "blocked"],
                    limit=100,
                ))
            except Exception:
                pass
            try:
                pending_count = len(get_pending_approvals())
            except Exception:
                pending_count = 0
            try:
                from row_bot.insights import get_active_insights
                insights_count = len(get_active_insights())
            except Exception:
                insights_count = 0

            running_badge.set_content(
                f'<div class="workflow-console-rail-badge running" title="Running goals, agents, and workflows">{running_count}</div>'
            )
            approval_badge.set_content(
                f'<div class="workflow-console-rail-badge approval" title="Pending approvals">{pending_count}</div>'
            )
            insights_badge.set_content(
                f'<div class="workflow-console-rail-badge insights" title="Active insights">{insights_count}</div>'
            )
            if collapsed_state["value"] and pending_count > 0:
                rail_shell.classes(add="workflow-console-approval-alert")
                if pending_count > last_pending_count["value"]:
                    rail_shell.classes(add="workflow-console-alert-flash")
                    safe_timer(
                        4.0,
                        lambda: rail_shell.classes(remove="workflow-console-alert-flash"),
                        once=True,
                    )
            else:
                rail_shell.classes(remove="workflow-console-approval-alert")
                rail_shell.classes(remove="workflow-console-alert-flash")
            last_pending_count["value"] = pending_count

        _refresh_rail_counts()
        safe_timer(3.0, _refresh_rail_counts)

        with ui.scroll_area().classes("w-full h-full workflow-console-scroll"):
          with ui.column().classes("w-full gap-2 workflow-console-content").style(
              "overflow-x: hidden; padding: 6px 8px;"
          ):
            with ui.column().classes("w-full gap-2 row-bot-inner-panel workflow-console-section").style(
                "width: 100%; min-width: 100%; max-width: 100%; overflow-x: hidden;"
            ):
                with ui.row().classes("w-full items-start justify-between no-wrap"):
                    with ui.column().classes("gap-0"):
                        ui.label("Activity Center").classes(
                            "text-subtitle1 font-bold"
                        ).style(f"color: {APP_BRAND_ACCENT}; letter-spacing: 0.5px;")
                        ui.label(
                            "Current goals and agents"
                        ).classes("text-xs text-grey-6").style(
                            "margin-top: -2px; letter-spacing: 0.3px;"
                        )
                    ui.button(icon="chevron_right", on_click=_toggle_drawer).props(
                        "round flat dense"
                    ).tooltip("Collapse Activity Center").style(
                        f"color: {APP_BRAND_ACCENT}; margin-top: -2px;"
                    )

                ui.separator().classes("q-my-none")
                _goal_activity_container = ui.column().classes("w-full gap-0")
                _agent_runs_container = ui.column().classes("w-full gap-0")
                _agent_preview_state = {"run_id": ""}
                _agent_preview_container = ui.column().classes("w-full gap-0")

                def _agent_status_color(status: str) -> str:
                    return {
                        "queued": "grey-6",
                        "running": "primary",
                        "waiting_approval": "warning",
                        "waiting_user": "warning",
                        "paused": "amber",
                        "completed": "positive",
                        "failed": "negative",
                        "blocked": "negative",
                        "stopped": "orange",
                        "active": "primary",
                        "completed": "positive",
                    }.get(str(status or ""), "grey-6")

                def _agent_workspace_tooltip(agent_run: dict) -> str:
                    run_id = str(agent_run.get("id") or "").strip()
                    if str(agent_run.get("workspace_mode") or "") != "worktree" or not run_id:
                        return ""
                    try:
                        from row_bot.developer.worktrees import get_worktree_for_run

                        worktree = get_worktree_for_run(run_id)
                    except Exception:
                        logger.debug("Could not load Agent worktree details", exc_info=True)
                        worktree = None
                    if not worktree:
                        return "Runs in its own local git Worktree."
                    metadata = worktree.get("metadata_json") or {}
                    seeded = ""
                    if metadata.get("seeded_from_current_changes"):
                        parts = []
                        if metadata.get("seeded_staged_diff"):
                            parts.append("staged")
                        if metadata.get("seeded_unstaged_diff"):
                            parts.append("unstaged")
                        copied = metadata.get("seeded_untracked_files") or []
                        if copied:
                            parts.append(f"{len(copied)} untracked")
                        seeded = "Inherited current changes: " + (", ".join(parts) or "yes")
                    return "\n".join(
                        item
                        for item in (
                            str(worktree.get("branch_name") or ""),
                            str(worktree.get("worktree_path") or ""),
                            seeded,
                        )
                        if item
                    ) or "Runs in its own local git Worktree."

                def _rebuild_goal_activity() -> None:
                    _goal_activity_container.clear()
                    try:
                        from row_bot import goals

                        attention_goals = goals.list_goals(
                            statuses=["waiting_approval", "blocked"],
                            limit=6,
                        )
                        running_goals = goals.list_goals(
                            statuses=["active", "paused"],
                            limit=6,
                        )
                        current_goal = (
                            goals.get_current_goal(str(state.thread_id or ""))
                            if state.thread_id
                            else None
                        )
                    except Exception as exc:
                        logger.warning("Activity Center goals unavailable: %s", exc)
                        attention_goals = []
                        running_goals = []
                        current_goal = None
                    with _goal_activity_container:
                        ui.label("Needs Attention").classes(
                            "text-xs font-bold text-grey-5"
                        ).style("letter-spacing: 0.8px; text-transform: uppercase;")
                        if not attention_goals:
                            ui.label("No goals need attention").classes(
                                "text-xs text-grey-7 q-ml-sm"
                            ).style("opacity: 0.5;")
                        for goal_row in attention_goals[:6]:
                            _render_goal_activity_row(goal_row)

                        ui.label("Running Now").classes(
                            "text-xs font-bold text-grey-5 q-mt-xs"
                        ).style("letter-spacing: 0.8px; text-transform: uppercase;")
                        seen: set[str] = set()
                        ordered: list[dict] = []
                        for goal_row in running_goals:
                            goal_id = str(goal_row.get("id") or "")
                            if goal_id and goal_id not in seen:
                                seen.add(goal_id)
                                ordered.append(goal_row)
                        if current_goal:
                            goal_id = str(current_goal.get("id") or "")
                            status = str(current_goal.get("status") or "")
                            if goal_id and goal_id not in seen and status in {"active", "paused"}:
                                seen.add(goal_id)
                                ordered.insert(0, current_goal)
                        if not ordered:
                            ui.label("No goals running").classes(
                                "text-xs text-grey-7 q-ml-sm"
                            ).style("opacity: 0.5;")
                        for goal_row in ordered[:6]:
                            _render_goal_activity_row(goal_row)

                def _render_goal_activity_row(goal_row: dict) -> None:
                    status = str(goal_row.get("status") or "unknown")
                    objective = str(goal_row.get("objective") or "Goal")
                    detail = str(
                        goal_row.get("last_progress")
                        or goal_row.get("last_reason")
                        or ""
                    )
                    with ui.row().classes("w-full items-center no-wrap gap-1 q-py-xs").style(
                        "overflow: hidden;"
                    ):
                        ui.badge(status, color=_agent_status_color(status)).props("outline dense")
                        ui.label(objective).classes("text-xs ellipsis").style("flex: 1; min-width: 0;")
                        if detail:
                            ui.label(detail).classes("text-xs text-grey-7 ellipsis").style("max-width: 150px;")

                def _rebuild_agent_preview() -> None:
                    _agent_preview_container.clear()
                    run_id = str(_agent_preview_state.get("run_id") or "").strip()
                    if not run_id:
                        return
                    try:
                        from row_bot.agent_runs import get_agent_events, get_agent_run

                        run_row = get_agent_run(run_id)
                        events = get_agent_events(run_id, limit=8)
                    except Exception:
                        logger.debug("Could not load Agent preview", exc_info=True)
                        run_row = None
                        events = []
                    with _agent_preview_container:
                        if not run_row:
                            _agent_preview_state["run_id"] = ""
                            return
                        status = str(run_row.get("status") or "unknown")
                        title = str(run_row.get("display_name") or run_row.get("id") or "Agent")
                        workspace_tooltip = _agent_workspace_tooltip(run_row)
                        summary = str(
                            run_row.get("summary")
                            or run_row.get("status_message")
                            or run_row.get("error")
                            or ""
                        )
                        with ui.card().classes("w-full q-my-xs").style(
                            "padding: 0.45rem 0.55rem; "
                            "border-left: 3px solid rgba(96,165,250,0.8); "
                            "overflow: hidden; box-sizing: border-box;"
                        ):
                            with ui.row().classes("w-full items-center no-wrap gap-1").style("overflow: hidden;"):
                                ui.badge(status, color=_agent_status_color(status)).props("outline dense")
                                ui.label(title).classes("text-xs font-bold ellipsis").style("flex: 1; min-width: 0;")
                                if workspace_tooltip:
                                    ui.badge("Worktree", color="blue-grey").props("outline dense").tooltip(
                                        workspace_tooltip
                                    )
                                ui.button(
                                    icon="open_in_new",
                                    on_click=lambda rid=run_id: open_agent_peek_dialog(
                                        rid,
                                        on_open_agent_thread=_open_agent_thread_from_center,
                                    ),
                                ).props("round flat dense size=xs").tooltip("Open preview dialog")
                                ui.button(
                                    icon="close",
                                    on_click=lambda: (
                                        _agent_preview_state.update({"run_id": ""}),
                                        _rebuild_agent_preview(),
                                    ),
                                ).props("round flat dense size=xs").tooltip("Close preview")
                            if summary:
                                ui.label(summary).classes("text-xs text-grey-5").style(
                                    "white-space: normal; display: -webkit-box; "
                                    "-webkit-line-clamp: 4; -webkit-box-orient: vertical; "
                                    "overflow: hidden;"
                                )
                            if events:
                                with ui.column().classes("w-full gap-1 q-mt-xs"):
                                    for event in events[-4:]:
                                        event_type = str(event.get("event_type") or "event").replace("_", " ")
                                        message = str(event.get("message") or event.get("payload_json") or "")
                                        if len(message) > 96:
                                            message = message[:93].rstrip() + "..."
                                        ui.label(f"{event_type}: {message}").classes("text-xs text-grey-7 ellipsis")

                def _show_agent_preview(run_id: str) -> None:
                    _agent_preview_state["run_id"] = str(run_id or "")
                    _rebuild_agent_preview()

                def _rebuild_agent_runs() -> None:
                    _agent_runs_container.clear()
                    try:
                        from row_bot.agent_runs import list_agent_runs, stop_agent_run

                        attention = list_agent_runs(
                            statuses=_activity_attention_statuses(),
                            kind="subagent",
                            limit=6,
                        )
                        active = list_agent_runs(
                            statuses=["queued", "running", "paused"],
                            kind="subagent",
                            limit=6,
                        )
                        current_recent = (
                            list_agent_runs(
                                parent_thread_id=str(state.thread_id or ""),
                                kind="subagent",
                                limit=6,
                            )
                            if state.thread_id
                            else []
                        )
                    except Exception as exc:
                        logger.warning("Activity Center agent runs unavailable: %s", exc)
                        attention = []
                        active = []
                        current_recent = []
                    with _agent_runs_container:
                        seen: set[str] = set()
                        ordered: list[dict] = []
                        for bucket in (attention, active):
                            for run_row in bucket:
                                run_id = str(run_row.get("id") or "")
                                if run_id and run_id not in seen:
                                    seen.add(run_id)
                                    ordered.append(run_row)
                        for run_row in current_recent:
                            run_id = str(run_row.get("id") or "")
                            if not run_id or run_id in seen:
                                continue
                            status = str(run_row.get("status") or "")
                            if status in {"completed", "completed_delivery_failed", "stopped", "cancelled"}:
                                continue
                            seen.add(run_id)
                            ordered.append(run_row)
                        label = "Current Chat Agents"
                        if attention:
                            label += f" ({len(attention)} need attention)"
                        ui.label(label).classes(
                            "text-xs font-bold text-grey-5"
                        ).style("letter-spacing: 0.8px; text-transform: uppercase;")
                        if not ordered:
                            ui.label("No active Agents").classes(
                                "text-xs text-grey-7 q-ml-sm"
                            ).style("opacity: 0.5;")
                            return
                        for agent_run in ordered[:8]:
                            run_id = str(agent_run.get("id") or "")
                            status = str(agent_run.get("status") or "unknown")
                            title = str(agent_run.get("display_name") or agent_run.get("id") or "Agent")
                            profile = str(
                                agent_run.get("profile_display_name")
                                or agent_run.get("profile_slug")
                                or agent_run.get("kind")
                                or "Agent"
                            )
                            message = str(
                                agent_run.get("status_message")
                                or agent_run.get("summary")
                                or agent_run.get("error")
                                or ""
                            )
                            workspace_tooltip = _agent_workspace_tooltip(agent_run)
                            with ui.row().classes(
                                "w-full items-center no-wrap gap-1 q-py-xs"
                            ).style("overflow: hidden;"):
                                ui.badge(status, color=_agent_status_color(status)).props("outline dense")
                                ui.label(title).classes("text-xs ellipsis").style("flex: 1; min-width: 0;")
                                ui.label(profile).classes("text-xs text-grey-6 ellipsis").style("max-width: 110px;")
                                if workspace_tooltip:
                                    ui.badge("Worktree", color="blue-grey").props("outline dense").tooltip(
                                        workspace_tooltip
                                    )
                                if message:
                                    ui.label(message).classes("text-xs text-grey-7 ellipsis").style("max-width: 120px;")
                                if run_id:
                                    ui.button(
                                        icon="visibility",
                                        on_click=lambda rid=run_id: _show_agent_preview(rid),
                                    ).props("round flat dense size=xs").tooltip("Peek Agent activity")
                                if status not in {"completed", "failed", "stopped", "blocked", "cancelled", "timed_out"}:
                                    ui.button(
                                        icon="stop",
                                        on_click=lambda rid=run_id: (
                                            stop_agent_run(rid),
                                            _rebuild_agent_runs(),
                                            _refresh_rail_counts(),
                                        ),
                                    ).props("round flat dense size=xs color=orange").tooltip("Stop Agent")
                _rebuild_goal_activity()
                safe_timer(5.0, _rebuild_goal_activity)
                _rebuild_agent_runs()
                safe_timer(5.0, _rebuild_agent_runs)

            with ui.column().classes("w-full gap-2 row-bot-inner-panel workflow-console-section row-bot-approvals-card").style(
                "width: 100%; min-width: 100%; max-width: 100%; overflow-x: hidden;"
            ):
                _approvals_container = ui.column().classes("w-full gap-0")

                def _rebuild_approvals() -> None:
                    _approvals_container.clear()
                    try:
                        pending = get_pending_approvals()
                    except Exception:
                        pending = []
                    with _approvals_container:
                        count_label = f" ({len(pending)})" if pending else ""
                        with ui.row().classes("w-full items-center no-wrap gap-1"):
                            ui.icon("hourglass_empty", size="xs").classes("text-grey-5")
                            ui.label(f"Approvals{count_label}").classes(
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
                    try:
                        from row_bot.approval_messages import compact_message, payload_from_row

                        approval_payload = payload_from_row(appr)
                        approval_message = compact_message(approval_payload, max_chars=180)
                        approval_label = (
                            approval_payload.get("source_label")
                            or appr.get("source_label")
                            or appr.get("task_name")
                            or "Approval"
                        )
                    except Exception:
                        approval_message = str(appr.get("message", ""))
                        approval_label = appr.get("source_label") or appr.get("task_name") or "Approval"
                    with ui.card().classes("w-full q-my-xs").style(
                        "padding: 0.4rem 0.5rem;"
                        "border-left: 3px solid #f0c040;"
                        "overflow: hidden; box-sizing: border-box;"
                    ):
                        with ui.row().classes("w-full items-center no-wrap gap-1").style(
                            "overflow: hidden;"
                        ):
                            ui.icon("notifications", size="xs").classes("text-grey-5")
                            ui.label(
                                str(approval_label)
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
                        msg = approval_message or appr.get("message", "")
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
                                    "Approved" if ok else "Already handled",
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
                                    "Denied" if ok else "Already handled",
                                    type="warning" if ok else "info",
                                )
                                _rebuild_approvals()
                                _rebuild_live()

                            ui.button("Approve", icon="check", on_click=_approve).props(
                                "unelevated dense no-caps size=xs"
                            ).style("background: #2d8a4e; color: white;")
                            ui.button("Deny", icon="close", on_click=_deny).props(
                                "flat dense no-caps size=xs"
                            ).style("color: #ff6b6b;")

                _rebuild_approvals()
                safe_timer(5.0, _rebuild_approvals)

            with ui.column().classes("w-full gap-2 row-bot-inner-panel workflow-console-section row-bot-workflows-card").style(
                "width: 100%; min-width: 100%; max-width: 100%; overflow-x: hidden;"
            ):
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.icon("account_tree", size="xs").classes("text-primary")
                    ui.label("Workflows").classes(
                        "text-xs font-bold text-grey-5"
                    ).style("letter-spacing: 0.8px; text-transform: uppercase;")
                _live_container = ui.column().classes("w-full gap-0")

                def _rebuild_live() -> None:
                    _live_container.clear()
                    running = _safe_workflow_read(
                        "running tasks", get_running_tasks, {}
                    )
                    with _live_container:
                        ui.label("Active Workflows").classes(
                            "text-xs font-bold text-grey-5"
                        ).style("letter-spacing: 0.8px; text-transform: uppercase;")
                        if not running:
                            with ui.row().classes(
                                "w-full justify-center q-py-sm"
                            ).style("opacity: 0.4;"):
                                ui.icon("play_circle", size="sm").classes("text-grey-7")
                                ui.label("No workflows running").classes("text-xs text-grey-7")
                            return

                        for tid, info in running.items():
                            _render_live_task(tid, info, is_paused=False)

                def _render_live_task(tid: str, info: dict, *, is_paused: bool) -> None:
                    icon = material_icon_for(info.get("icon"))
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
                        with ui.row().classes("w-full items-center no-wrap gap-1").style(
                            "overflow: hidden;"
                        ):
                            ui.icon(icon, size="xs").classes("text-grey-5")
                            ui.label(name).classes(
                                "font-bold text-xs ellipsis"
                            ).style("flex: 1; min-width: 0;")
                            ui.badge("running", color="green").props(
                                "dense"
                            ).classes("text-xs")

                            def _stop(t=tid, n=name):
                                stop_task(t)
                                ui.notify(f"Stopping {n}...", type="warning")

                            ui.button(icon="stop", on_click=_stop).props(
                                "round flat dense size=xs"
                            ).style("color: #ff6b6b;").tooltip("Stop")

                        if total > 0:
                            with ui.row().classes("w-full items-center no-wrap gap-1"):
                                ui.label(
                                    f"Step {step + 1}/{total}"
                                ).classes("text-xs text-grey-6")
                                ui.linear_progress(
                                    value=(step + 1) / total,
                                    show_value=False,
                                ).classes("flex-grow").props(
                                    "color=primary"
                                ).style("height: 4px;")
                                if started:
                                    ui.label(_elapsed(started)).classes(
                                        "text-xs text-grey-7"
                                    )

                        if step_label:
                            ui.label(step_label).classes(
                                "text-xs text-grey-5 ellipsis"
                            ).style("max-width: 100%;")

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

                _rebuild_live()
                safe_timer(3.0, _rebuild_live)

                ui.separator().classes("q-my-none")
                _upcoming_container = ui.column().classes("w-full gap-0")

                def _rebuild_upcoming() -> None:
                    _upcoming_container.clear()
                    with _upcoming_container:
                        with ui.row().classes("w-full items-center no-wrap gap-1"):
                            ui.icon("event", size="xs").classes("text-primary")
                            ui.label("Upcoming").classes(
                                "text-xs font-bold text-grey-5"
                            ).style(
                                "letter-spacing: 0.8px; text-transform: uppercase;"
                            )
                        upcoming = _safe_workflow_read(
                            "upcoming tasks", lambda: get_next_fire_times(5), None
                        )
                        if upcoming is None:
                            _render_workflow_unavailable()
                            return
                        if not upcoming:
                            ui.label("No scheduled tasks").classes(
                                "text-xs text-grey-7 q-ml-sm"
                            ).style("opacity: 0.5;")
                            return
                        for item in upcoming:
                            with ui.row().classes(
                                "w-full items-center no-wrap gap-1 q-py-xs"
                            ).style("overflow: hidden;"):
                                ui.icon(
                                    material_icon_for(item.get("task_icon")),
                                    size="xs",
                                ).classes("text-grey-5")
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

                ui.separator().classes("q-my-none")
                with ui.row().classes("w-full items-center no-wrap gap-1"):
                    ui.icon("rocket_launch", size="xs").classes("text-primary")
                    ui.label("Launch").classes(
                        "text-xs font-bold text-grey-5"
                    ).style(
                        "letter-spacing: 0.8px; text-transform: uppercase;"
                    )

                _task_select = ui.select(
                    options=[], label="Workflow",
                ).classes("w-full").props("dense outlined")

                def _refresh_task_options() -> None:
                    tasks = _safe_workflow_read(
                        "quick launch tasks", list_tasks, None
                    )
                    if tasks is None:
                        _task_select.options = {}
                        _task_select.update()
                        return
                    opts = {
                        t["id"]: t["name"]
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
                        from row_bot.tools import registry as tool_registry
                        bg_tools = [
                            tl.name for tl in tool_registry.get_enabled_tools()
                        ]
                        run_task_background(
                            task_id, tid, bg_tools,
                            start_step=0, notification=True,
                        )
                        ui.notify(
                            f"{task['name']} started",
                            type="positive",
                        )
                        rebuild_thread_list()
                        _refresh_task_options()
                        defer_ui(_rebuild_live, delay=0.5)

                    ui.button(
                        "Run", icon="play_arrow", on_click=_run_selected
                    ).props(
                        "unelevated dense no-caps color=green"
                    ).classes("flex-grow")

                    def _new_workflow():
                        show_task_dialog(None, lambda: (
                            _refresh_task_options(),
                            rebuild_main(),
                        ))

                    ui.button(
                        "New", icon="add", on_click=_new_workflow
                    ).props("outline dense no-caps").classes("flex-grow")

            with ui.column().classes("w-full gap-2 row-bot-inner-panel workflow-console-section").style(
                "width: 100%; min-width: 100%; max-width: 100%; overflow-x: hidden;"
            ):
                from row_bot.ui.channel_monitor import build_channel_monitor

                build_channel_monitor(
                    open_settings
                    or (lambda *_args, **_kwargs: ui.notify("Open Settings > Channels", type="info"))
                )

            # ════════════════════════════════════════════════════
            # §6  INSIGHTS  (separate inner panel)
            # ════════════════════════════════════════════════════
            with ui.column().classes("w-full gap-2 row-bot-inner-panel workflow-console-section").style(
                "width: 100%; min-width: 100%; max-width: 100%; overflow-x: hidden;"
            ):
                with ui.expansion("Insights", icon="lightbulb", value=False).classes(
                    "w-full row-bot-command-center-insights-expansion"
                ):
                    _insights_container = ui.column().classes("w-full gap-0 q-pt-xs").style(
                        "min-width: 0; max-width: 100%; overflow-x: hidden;"
                    )
                proposal_dialog_state = {"open": False}

                def _rebuild_insights() -> None:
                    if proposal_dialog_state.get("open"):
                        return
                    _insights_container.clear()
                    try:
                        from row_bot.insights import (
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
                            ui.icon("lightbulb", size="xs").classes("text-grey-5")
                            ui.label("Insights").classes(
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
                            else:
                                ui.element("div").style("margin-left: auto;")

                            def _review_skill_library():
                                try:
                                    from row_bot.evolution import review_skill_library_dry_run

                                    report = review_skill_library_dry_run(create_proposals=True)
                                    summary = report.get("summary", {})
                                    ui.notify(
                                        (
                                            "Skill review dry-run complete: "
                                            f"{summary.get('finding_count', 0)} finding(s), "
                                            f"{summary.get('proposal_count', 0)} proposal(s)."
                                        ),
                                        type="positive",
                                    )
                                    refresh = _rebuild_insights
                                    refresh()
                                except Exception as exc:
                                    ui.notify(f"Skill review failed: {exc}", type="negative")

                            ui.button(
                                icon="manage_search",
                                on_click=_review_skill_library,
                            ).props("flat round dense size=xs").tooltip("Review skill library")

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
                                proposal_dialog_state,
                            )

                def _render_insight_card(
                    ins: dict,
                    refresh_fn,
                    state: AppState,
                    p: P,
                    rebuild_main,
                    rebuild_thread_list,
                    load_thread_messages,
                    proposal_dialog_state: dict,
                ) -> None:
                    from row_bot.insights import (
                        dismiss_insight, pin_insight,
                        update_insight_status,
                        CATEGORY_ICONS, SEVERITY_SORT,
                    )

                    cat = ins.get("category", "system_health")
                    sev = ins.get("severity", "info")
                    icon = material_icon_for(
                        CATEGORY_ICONS.get(cat, "lightbulb"),
                        fallback="lightbulb",
                    )
                    title = ins.get("title", "Untitled")
                    body = ins.get("body", "")
                    suggestion = ins.get("suggestion", "")
                    iid = ins["id"]
                    is_pinned = ins.get("status") == "pinned"
                    proposals: list[dict] = []
                    try:
                        from row_bot.evolution import list_display_proposals_for_insight

                        proposals = list_display_proposals_for_insight(ins, include_terminal=True)
                    except Exception:
                        logger.debug("Could not load proposals for insight %s", iid, exc_info=True)

                    sev_colors = {
                        "critical": "#ff5252",
                        "warning": "#f0c040",
                        "info": "rgba(255,255,255,0.15)",
                    }
                    border_color = sev_colors.get(sev, sev_colors["info"])

                    def _navigate_to_thread(thread_id: str, thread_name: str = "") -> None:
                        from row_bot.memory_extraction import set_active_thread
                        from row_bot.ui.voice_lifecycle import stop_voice_for_thread_change

                        tid = str(thread_id or "").strip()
                        if not tid:
                            return
                        prev = state.thread_id
                        stop_voice_for_thread_change(state, p, reason="command_center_insight")
                        state.thread_id = tid
                        state.thread_name = thread_name or f"Investigate: {title}"
                        state.messages = load_thread_messages(tid)
                        p.pending_files.clear()
                        set_active_thread(tid, previous_id=prev)
                        rebuild_main()
                        rebuild_thread_list()

                    with ui.card().classes("w-full q-my-xs row-bot-insight-card").style(
                        f"padding: 0.4rem 0.5rem;"
                        f" border-left: 3px solid {border_color};"
                        f" min-width: 0; max-width: 100%;"
                        f" overflow: hidden; overflow-x: hidden; box-sizing: border-box;"
                    ):
                        # Title row
                        with ui.row().classes(
                            "w-full items-center no-wrap gap-1"
                        ).style("overflow: hidden;"):
                            ui.icon(icon, size="xs").classes("text-grey-5")
                            ui.label(title).classes(
                                "text-xs font-bold ellipsis"
                            ).style("flex: 1; min-width: 0;")
                            if is_pinned:
                                ui.icon(
                                    "push_pin", size="xs"
                                ).classes("text-primary")

                        # Body
                        if body:
                            ui.label(body).classes(
                                "text-xs text-grey-6"
                            ).style(
                                "white-space: normal; overflow: hidden;"
                                " overflow-wrap: anywhere; word-break: break-word;"
                                " display: -webkit-box; -webkit-line-clamp: 3;"
                                " -webkit-box-orient: vertical;"
                            )

                        # Suggestion
                        if suggestion:
                            with ui.row().classes("w-full items-start no-wrap gap-1"):
                                ui.icon("chat_bubble", size="xs").classes("text-grey-5")
                                ui.label(suggestion).classes(
                                    "text-xs text-grey-5"
                                ).style(
                                    "white-space: normal; overflow: hidden;"
                                    " overflow-wrap: anywhere; word-break: break-word;"
                                    " display: -webkit-box; -webkit-line-clamp: 3;"
                                    " -webkit-box-orient: vertical; font-style: italic;"
                                )

                        if proposals:
                            display_proposals = sorted(
                                proposals[:4],
                                key=lambda item: (
                                    _proposal_status_order(str(item.get("status") or "")),
                                    _proposal_type_order(str(item.get("proposal_type") or "")),
                                ),
                            )
                            with ui.column().classes("w-full gap-1 q-mt-xs row-bot-insight-proposals").style(
                                "min-width: 0; max-width: 100%; overflow-x: hidden;"
                            ):
                                for proposal in display_proposals:
                                    ptype = str(proposal.get("proposal_type", "proposal"))
                                    pstatus = str(proposal.get("status", "ready"))
                                    with ui.element("div").classes("row-bot-insight-proposal-row"):
                                        with ui.element("div").classes("row-bot-insight-proposal-main"):
                                            ui.badge(
                                                _proposal_type_label(ptype), color="grey-8"
                                            ).props("dense").classes("text-xs")
                                            ui.badge(
                                                _proposal_status_label(pstatus),
                                                color=_proposal_status_color(pstatus),
                                            ).props("dense").classes("text-xs")
                                            ui.label(
                                                _compact_proposal_title(proposal)
                                            ).classes("text-xs row-bot-insight-proposal-title").tooltip(
                                                str(proposal.get("title", "Proposal"))
                                            )
                                        ui.button(
                                            icon="visibility",
                                            on_click=lambda pid=proposal["id"]: _show_proposal_preview(
                                                pid,
                                                refresh_fn,
                                                navigate_thread=_navigate_to_thread,
                                                dialog_state=proposal_dialog_state,
                                            ),
                                        ).props("flat round dense size=xs").classes(
                                            "row-bot-insight-preview-btn"
                                        ).tooltip(
                                            f"Preview {_proposal_type_label(ptype)}"
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
                                ).style(f"color: {APP_BRAND_ACCENT};")
                            else:
                                def _pin(i=iid):
                                    pin_insight(i)
                                    ui.notify("Insight pinned", type="positive")
                                    refresh_fn()

                                ui.button(
                                    "Pin", on_click=_pin
                                ).props(
                                    "flat dense no-caps size=xs"
                                ).style(f"color: {APP_BRAND_ACCENT};")

                            # Investigate — opens chat with context
                            if not proposals:
                                def _generate(insight=ins):
                                    try:
                                        from row_bot.evolution import ensure_proposals_for_insight

                                        created = ensure_proposals_for_insight(insight)
                                        ui.notify(
                                            f"Prepared {len(created)} proposal(s)",
                                            type="positive" if created else "info",
                                        )
                                        refresh_fn()
                                    except Exception as exc:
                                        ui.notify(f"Could not prepare proposals: {exc}", type="negative")

                                ui.button(
                                    "Generate proposals", on_click=_generate
                                ).props(
                                    "flat dense no-caps size=xs"
                                ).style("color: #64b5f6;")

                _rebuild_insights()
                safe_timer(30.0, _rebuild_insights)

    ui.button(icon="pending_actions", on_click=drawer.toggle).classes(
        "row-bot-activity-mobile-toggle"
    ).props(
        "round dense aria-label='Toggle Activity Center'"
    ).tooltip("Toggle Activity Center")


def _proposal_type_order(proposal_type: str) -> int:
    order = {
        "send_feedback": 0,
        "investigate": 1,
        "create_skill": 2,
        "patch_skill": 3,
        "consolidate_skills": 4,
    }
    return order.get(str(proposal_type or ""), 99)


def _proposal_status_order(status: str) -> int:
    order = {
        "ready": 0,
        "draft": 1,
        "approved": 2,
        "applied": 3,
        "verified": 4,
        "rejected": 5,
        "failed": 6,
    }
    return order.get(str(status or ""), 99)


def _proposal_type_label(proposal_type: str) -> str:
    labels = {
        "send_feedback": "feedback",
        "investigate": "investigate",
        "create_skill": "skill",
        "patch_skill": "patch",
        "consolidate_skills": "review",
    }
    return labels.get(str(proposal_type or ""), str(proposal_type or "proposal").replace("_", " "))


def _proposal_status_color(status: str) -> str:
    colors = {
        "ready": "blue-grey",
        "draft": "grey",
        "approved": "primary",
        "applied": "positive",
        "verified": "green",
        "rejected": "warning",
        "failed": "negative",
    }
    return colors.get(str(status or ""), "blue-grey")


def _proposal_status_label(status: str) -> str:
    labels = {
        "ready": "review",
        "draft": "draft",
        "approved": "approved",
        "applied": "done",
        "verified": "verified",
        "rejected": "rejected",
        "failed": "failed",
    }
    return labels.get(str(status or ""), str(status or "review"))


def _compact_proposal_title(proposal: dict) -> str:
    title = str(proposal.get("title") or "Proposal").strip()
    for prefix in (
        "Send feedback:",
        "Investigate:",
        "Create skill:",
        "Patch skill:",
        "Review overlap:",
    ):
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix) :].strip()
            break
    return title or "Proposal"


def _proposal_preview_text(proposal: dict) -> str:
    """Return a compact, readable proposal preview."""

    import json

    preview = proposal.get("preview") if isinstance(proposal.get("preview"), dict) else {}
    if proposal.get("proposal_type") == "patch_skill" and preview.get("diff"):
        body = preview.get("diff", "")
    elif proposal.get("proposal_type") == "create_skill":
        overlaps = preview.get("overlaps") if isinstance(preview.get("overlaps"), list) else []
        lines = [
            f"Skill: {preview.get('display_name') or preview.get('skill_name') or 'Untitled skill'}",
            f"Identifier: {preview.get('skill_name') or 'unknown'}",
        ]
        description = str(preview.get("description") or "").strip()
        if description:
            lines.extend(["", "Description:", description])
        instructions = str(preview.get("instructions_preview") or "").strip()
        if instructions:
            lines.extend(["", "Instructions preview:", instructions])
        tags = preview.get("tags")
        if isinstance(tags, list) and tags:
            lines.extend(["", "Tags: " + ", ".join(str(tag) for tag in tags)])
        lines.extend(
            [
                "",
                f"Estimated tokens: {preview.get('estimated_tokens', 0)}",
                f"Suggested enabled: {bool(preview.get('suggested_enabled'))}",
            ]
        )
        if overlaps:
            lines.extend(["", "Potential overlaps:"])
            for item in overlaps[:5]:
                lines.append(
                    f"- {item.get('display_name') or item.get('name')} "
                    f"({item.get('source', 'unknown')}, score {item.get('score', 0)})"
                )
        body = "\n".join(lines)
    elif proposal.get("proposal_type") == "send_feedback":
        draft = preview.get("feedback_draft") if isinstance(preview.get("feedback_draft"), dict) else {}
        body = f"{draft.get('title', '')}\n\n{draft.get('body', '')}".strip()
    elif proposal.get("proposal_type") == "investigate":
        body = preview.get("draft_prompt") or preview.get("text") or ""
    else:
        body = json.dumps(preview, indent=2, ensure_ascii=False)
    validation = preview.get("validation") if isinstance(preview.get("validation"), dict) else {}
    if validation:
        body += "\n\nValidation:\n" + json.dumps(validation, indent=2, ensure_ascii=False)
    return str(body or "No preview available.")


def _show_proposal_preview(
    proposal_id: str,
    refresh_fn,
    *,
    navigate_thread=None,
    dialog_state: dict | None = None,
) -> None:
    """Open a preview/approval dialog for a controlled-evolution proposal."""

    import json

    from nicegui import ui as _ui

    try:
        from row_bot.evolution import apply_proposal, get_proposal, reject_proposal

        proposal = get_proposal(proposal_id)
    except Exception as exc:
        _ui.notify(f"Could not load proposal: {exc}", type="negative")
        return

    if not proposal:
        _ui.notify("Proposal not found", type="warning")
        return

    if dialog_state is not None:
        dialog_state["open"] = True

    with _ui.dialog() as dialog, _ui.card().classes("w-full").style(
        "max-width: 760px; max-height: 82vh; overflow: auto;"
    ):
        dialog.props("persistent")

        def _close_dialog(*, refresh: bool = False) -> None:
            if dialog_state is not None:
                dialog_state["open"] = False
            dialog.close()
            if refresh:
                refresh_fn()

        _ui.label(str(proposal.get("title", "Proposal"))).classes("text-subtitle1 font-bold")
        with _ui.row().classes("items-center gap-1"):
            _ui.badge(str(proposal.get("proposal_type", "proposal")), color="blue-grey")
            _ui.badge(f"risk: {proposal.get('risk', 'low')}", color="orange")
            _ui.badge(f"state: {_proposal_status_label(str(proposal.get('status', 'ready')))}", color="grey")
            _ui.badge(f"confidence: {proposal.get('confidence', 0)}", color="grey-8")
        rationale = str(proposal.get("rationale") or "")
        if rationale:
            _ui.label(rationale).classes("text-sm text-grey-5").style(
                "white-space: normal; word-break: break-word;"
            )
        proposal_type = str(proposal.get("proposal_type") or "")
        preview_text = _proposal_preview_text(proposal)
        _ui.label(preview_text).classes("text-xs").style(
            "white-space: pre-wrap; word-break: break-word; "
            "border: 1px solid rgba(255,255,255,0.12); padding: 8px; border-radius: 6px; "
            "max-height: 380px; overflow: auto; width: 100%;"
        )
        _ui.label(
            f"Verification: {proposal.get('verification_plan') or 'Review after applying.'}"
        ).classes("text-xs text-grey-6").style("white-space: normal;")
        status = str(proposal.get("status") or "ready")
        is_terminal = status in {"applied", "verified", "rejected", "failed"}
        if is_terminal:
            _ui.label(
                f"This proposal is {_proposal_status_label(status)} and is shown for history."
            ).classes("text-xs text-grey-5")
            reason_input = None
        else:
            reason_input = _ui.input("Rejection reason").classes("w-full").props("dense")

        with _ui.row().classes("w-full justify-end gap-2"):
            _ui.button("Close", on_click=lambda: _close_dialog()).props("flat no-caps")

            def _reject() -> None:
                try:
                    reject_proposal(proposal_id, str(reason_input.value if reason_input else ""))
                    _ui.notify("Proposal rejected", type="info")
                    _close_dialog(refresh=True)
                except Exception as exc:
                    _ui.notify(f"Reject failed: {exc}", type="negative")

            def _apply_current() -> dict:
                try:
                    return apply_proposal(
                        proposal_id,
                        require_approval=False,
                        approved_by_user=True,
                    )
                except Exception as exc:
                    return {"ok": False, "message": f"Apply failed: {exc}"}

            def _approve() -> None:
                result = _apply_current()
                try:
                    _ui.notify(
                        result.get("message", "Proposal applied"),
                        type="positive" if result.get("ok") else "warning",
                    )
                    if result.get("ok") and proposal_type == "investigate" and navigate_thread:
                        refs = (result.get("action_run") or {}).get("result_refs") or []
                        if refs:
                            navigate_thread(str(refs[0]), str(proposal.get("title") or "Investigate"))
                    _close_dialog(refresh=True)
                except Exception as exc:
                    _ui.notify(f"Apply failed: {exc}", type="negative")

            if is_terminal:
                if proposal_type == "investigate" and navigate_thread:
                    try:
                        from row_bot.evolution import list_action_runs

                        runs = list_action_runs(proposal_id=proposal_id, limit=1)
                    except Exception:
                        runs = []
                    refs = (runs[0].get("result_refs") if runs else []) or []
                    if refs:
                        _ui.button(
                            "Open thread",
                            on_click=lambda tid=str(refs[0]): (
                                navigate_thread(tid, str(proposal.get("title") or "Investigate")),
                                _close_dialog(refresh=True),
                            ),
                        ).props("outline no-caps color=primary")
                elif proposal_type == "send_feedback":
                    def _open_contact() -> None:
                        from row_bot.brand import APP_SUPPORT_URL

                        _ui.run_javascript(
                            f"window.open({json.dumps(APP_SUPPORT_URL)}, '_blank')"
                        )
                        _close_dialog(refresh=True)

                    _ui.button("Open contact page", on_click=_open_contact).props(
                        "outline no-caps color=primary"
                    )
            elif proposal_type == "send_feedback":
                _ui.button("Reject", on_click=_reject).props("outline no-caps color=warning")

                def _copy_feedback() -> None:
                    _ui.run_javascript(
                        f"navigator.clipboard.writeText({json.dumps(preview_text)})"
                    )
                    _ui.notify("Feedback report copied", type="positive")

                def _save_feedback() -> None:
                    result = _apply_current()
                    _ui.notify(
                        result.get("message", "Feedback report saved"),
                        type="positive" if result.get("ok") else "warning",
                    )
                    if result.get("ok"):
                        _close_dialog(refresh=True)

                def _submit_feedback() -> None:
                    from row_bot.brand import APP_SUPPORT_URL

                    result = _apply_current()
                    if result.get("ok") or "already applied" in str(result.get("message", "")).lower():
                        _ui.run_javascript(
                            f"window.open({json.dumps(APP_SUPPORT_URL)}, '_blank')"
                        )
                        _ui.notify("Opening Row-Bot contact page", type="positive")
                        _close_dialog(refresh=True)
                    else:
                        _ui.notify(
                            result.get("message", "Could not prepare feedback report"),
                            type="warning",
                        )

                _ui.button("Copy report", on_click=_copy_feedback).props("outline no-caps")
                _ui.button("Save report", on_click=_save_feedback).props("outline no-caps color=primary")
                _ui.button("Submit", on_click=_submit_feedback).props("unelevated no-caps color=positive")
            else:
                _ui.button("Reject", on_click=_reject).props("outline no-caps color=warning")
                _ui.button("Approve", on_click=_approve).props("unelevated no-caps color=positive")
    dialog.open()


def _apply_skill_draft(
    insight_id: str, refresh_fn
) -> None:
    """Create controlled skill proposals from a skill-proposal insight."""
    from nicegui import ui as _ui

    try:
        from row_bot.insights import apply_insight

        result = apply_insight(insight_id)
        _ui.notify(
            result.get("message", "Failed to create proposal"),
            type="positive" if result.get("ok") else "warning",
        )
        if result.get("ok"):
            refresh_fn()
    except Exception as exc:
        _ui.notify(f"Failed to create proposal: {exc}", type="negative")


def _escape_html(text: str) -> str:
    """Minimal HTML escape for log display."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
