"""Shared parent-thread Agent run drawer for chat-like surfaces."""

from __future__ import annotations

import logging
from typing import Callable

from nicegui import ui

from row_bot.ui.render import (
    _open_agent_worktree,
    _show_agent_worktree_compare,
    open_agent_peek_dialog,
)
from row_bot.ui.state import AppState, P, _active_generations

logger = logging.getLogger(__name__)


_TERMINAL_STATUSES = {
    "completed",
    "completed_delivery_failed",
    "failed",
    "stopped",
    "blocked",
    "cancelled",
    "timed_out",
}


def _agent_status_color(status: str) -> str:
    return {
        "active": "primary",
        "queued": "grey-6",
        "running": "primary",
        "waiting_approval": "warning",
        "waiting_user": "warning",
        "paused": "amber",
        "completed": "positive",
        "failed": "negative",
        "blocked": "negative",
        "stopped": "orange",
        "cleared": "grey-7",
    }.get(str(status or ""), "grey-6")


def _open_agent_thread(
    agent_run: dict,
    *,
    state: AppState,
    p: P,
    rebuild_main: Callable[..., None],
    rebuild_thread_list: Callable[[], None] | None = None,
) -> None:
    thread_id = str(agent_run.get("thread_id") or "").strip()
    if not thread_id:
        ui.notify("This Agent run has no child thread.", type="warning", close_button=True)
        return
    try:
        from row_bot.memory_extraction import set_active_thread
        from row_bot.threads import (
            _get_thread_approval_mode,
            _get_thread_developer_workspace,
            _get_thread_model_override,
            _get_thread_type,
            get_thread_name,
        )
        from row_bot.ui.helpers import load_thread_messages
        from row_bot.ui.voice_lifecycle import stop_voice_for_thread_change

        prev = state.thread_id
        prev_gen = _active_generations.get(prev) if prev else None
        if prev_gen and str(getattr(prev_gen, "status", "")) == "streaming":
            from row_bot.ui.streaming import _detach_generation

            _detach_generation(prev_gen, state, "open_agent_child_thread")
        stop_voice_for_thread_change(state, p, reason="open_agent_child_thread")
        target_thread_type = _get_thread_type(thread_id)
        state.active_designer_project = None
        state.thread_id = thread_id
        state.active_developer_workspace_id = (
            None
            if target_thread_type == "agent_child"
            else _get_thread_developer_workspace(thread_id) or None
        )
        state.thread_name = get_thread_name(thread_id) or str(
            agent_run.get("display_name") or "Agent"
        )
        state.thread_model_override = _get_thread_model_override(thread_id)
        state.thread_approval_mode = _get_thread_approval_mode(thread_id)
        state.messages = load_thread_messages(thread_id)
        try:
            p.pending_files.clear()
        except Exception:
            pass
        set_active_thread(thread_id, previous_id=prev)
        rebuild_main()
        if rebuild_thread_list is not None:
            rebuild_thread_list()
    except Exception as exc:
        logger.debug("Could not open Agent child thread", exc_info=True)
        ui.notify(f"Could not open Agent thread: {exc}", type="negative", close_button=True)


def open_agent_thread(
    agent_run: dict,
    *,
    state: AppState,
    p: P,
    rebuild_main: Callable[..., None],
    rebuild_thread_list: Callable[[], None] | None = None,
) -> None:
    """Open a child Agent's full thread in the current app shell."""
    _open_agent_thread(
        agent_run,
        state=state,
        p=p,
        rebuild_main=rebuild_main,
        rebuild_thread_list=rebuild_thread_list,
    )


def _message_agent(orchestration_id: str, run_id: str, *, p: P) -> None:
    p.parent_agent_dialog_open = True
    with ui.dialog() as dialog, ui.card().classes("w-full").style("max-width: 520px"):
        ui.label("Message Agent").classes("text-subtitle2 font-bold")
        message = ui.textarea(
            "Guidance",
            placeholder="This is queued for the next safe execution boundary.",
        ).props("autogrow outlined").classes("w-full")

        def submit() -> None:
            text = str(message.value or "").strip()
            if not text:
                ui.notify("Enter guidance first.", type="warning")
                return
            try:
                from row_bot.agent_orchestrator import message_orchestration

                queued = message_orchestration(
                    orchestration_id,
                    text,
                    run_id=run_id,
                )
                ui.notify(
                    "Guidance queued." if queued else "Agent is no longer active.",
                    type="positive" if queued else "warning",
                )
                dialog.close()
            except Exception as exc:
                ui.notify(str(exc), type="negative", close_button=True)

        def cancel() -> None:
            dialog.close()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=cancel).props("flat")
            ui.button("Queue guidance", on_click=submit, icon="send").props("color=primary")
    dialog.on("hide", lambda _event: setattr(p, "parent_agent_dialog_open", False))
    dialog.open()


def build_parent_agent_drawer(
    state: AppState,
    p: P,
    *,
    rebuild_main: Callable[..., None],
    rebuild_thread_list: Callable[[], None] | None = None,
    limit: int = 6,
) -> None:
    """Render the current parent thread's Agent runs with peek, thread, and Worktree actions."""

    if not state.thread_id:
        return
    try:
        from row_bot.agent_runs import (
            get_agent_parent_messages,
            list_agent_runs,
            stop_agent_run,
        )
        from row_bot.agent_orchestrator import list_orchestrations

        runs = list_agent_runs(parent_thread_id=state.thread_id, kind="subagent", limit=limit)
        orchestrations = list_orchestrations(parent_thread_id=state.thread_id, limit=1)
    except Exception:
        logger.debug("Could not load parent Agent Runs", exc_info=True)
        return
    if not runs and not orchestrations:
        return

    with ui.column().classes("w-full gap-1 q-px-md q-pb-xs row-bot-parent-agent-drawer"):
        if orchestrations:
            orchestration = orchestrations[0]
            orchestration_id = str(orchestration.get("id") or "")
            orchestration_status = str(orchestration.get("status") or "")
            try:
                from row_bot.agent_orchestrator import orchestration_overview

                overview = orchestration_overview(orchestration_id)
                counts = overview.get("counts") or {}
            except Exception:
                counts = {}
            with ui.row().classes("w-full items-center gap-2").style(
                "border: 1px solid rgba(59, 130, 246, 0.28); "
                "border-radius: 8px; padding: 7px 9px; "
                "background: rgba(59, 130, 246, 0.055);"
            ):
                ui.icon("account_tree", size="xs").classes("text-primary")
                ui.label("Agent group").classes("text-xs font-bold")
                ui.badge(
                    orchestration_status,
                    color=_agent_status_color(orchestration_status),
                ).props("outline dense")
                ui.label(
                    f"{int(counts.get('running') or 0)} running Â· "
                    f"{int(counts.get('completed') or 0)} complete Â· "
                    f"{int(counts.get('failed') or 0)} failed"
                ).classes("text-xs text-grey-6")
                ui.space()
                if orchestration_status == "interrupted":
                    def _resume(oid=orchestration_id) -> None:
                        try:
                            from row_bot.agent_orchestrator import resume_orchestration

                            resume_orchestration(oid)
                            ui.notify("Agent group resumed.", type="positive")
                            rebuild_main()
                        except Exception as exc:
                            ui.notify(str(exc), type="negative", close_button=True)

                    ui.button("Resume", icon="play_arrow", on_click=_resume).props(
                        "flat dense no-caps color=primary"
                    )
                elif orchestration_status not in {
                    "completed",
                    "completed_partial",
                    "failed",
                    "stopped",
                }:
                    def _stop_all(oid=orchestration_id) -> None:
                        try:
                            from row_bot.agent_orchestrator import stop_orchestration

                            stop_orchestration(oid)
                            rebuild_main()
                        except Exception as exc:
                            ui.notify(str(exc), type="negative", close_button=True)

                    ui.button("Stop all", icon="stop", on_click=_stop_all).props(
                        "flat dense no-caps color=orange"
                    )
        with ui.row().classes("w-full items-center gap-2").style(
            "border: 1px solid rgba(148, 163, 184, 0.22); "
            "border-radius: 8px; padding: 6px 8px; "
            "background: rgba(148, 163, 184, 0.045);"
        ):
            ui.icon("hub", size="xs").classes("text-primary")
            ui.label("Agents").classes("text-xs font-bold text-grey-5")
            ui.space()
            ui.label(f"{len(runs)} recent").classes("text-xs text-grey-7")

        for agent_run in runs[:4]:
            run_id = str(agent_run.get("id") or "")
            child_thread_id = str(agent_run.get("thread_id") or "")
            name = str(agent_run.get("display_name") or run_id or "Agent")
            status = str(agent_run.get("status") or "unknown")
            profile = str(
                agent_run.get("profile_display_name")
                or agent_run.get("profile_slug")
                or "Agent"
            )
            workspace_mode = str(agent_run.get("workspace_mode") or "")
            workspace_detail = ""
            if workspace_mode == "worktree" and run_id:
                try:
                    from row_bot.developer.worktrees import get_worktree_for_run

                    worktree = get_worktree_for_run(run_id)
                    if worktree:
                        branch = str(worktree.get("branch_name") or "")
                        path = str(worktree.get("worktree_path") or "")
                        workspace_detail = "\n".join(
                            item for item in (branch, path) if item
                        )
                except Exception:
                    logger.debug("Could not load Agent worktree details", exc_info=True)
            message = str(
                agent_run.get("status_message")
                or agent_run.get("summary")
                or agent_run.get("error")
                or ""
            )
            try:
                parent_notes = get_agent_parent_messages(run_id, limit=3)
            except Exception:
                logger.debug("Could not load Agent parent messages", exc_info=True)
                parent_notes = []
            member = None
            try:
                from row_bot.agent_orchestrator import get_member_for_run

                member = get_member_for_run(run_id)
            except Exception:
                member = None
            if parent_notes:
                latest_note = str(parent_notes[-1])
                note_preview = latest_note if len(latest_note) <= 80 else latest_note[:79].rstrip() + "..."
                message = f"Note queued: {note_preview}"

            with ui.row().classes("w-full items-center no-wrap gap-2 q-px-md").style(
                "min-height: 30px;"
            ):
                ui.badge(status, color=_agent_status_color(status)).props("outline dense")
                ui.label(name).classes("text-xs font-medium ellipsis").style("flex: 1; min-width: 0;")
                ui.label(profile).classes("text-xs text-grey-6 ellipsis").style("max-width: 120px;")
                used = int(agent_run.get("model_iterations_used") or 0)
                maximum = int(agent_run.get("model_iterations_max") or 0)
                depth = int(agent_run.get("depth") or 0)
                if maximum:
                    ui.label(f"{used}/{maximum} · d{depth}").classes(
                        "text-xs text-grey-7 no-wrap"
                    ).tooltip("Model work rounds used · delegation depth")
                if workspace_mode == "worktree":
                    ui.badge("Worktree", color="blue-grey").props("outline dense").tooltip(
                        workspace_detail or "Runs in its own local git Worktree."
                    )
                if message:
                    ui.label(message).classes("text-xs text-grey-7 ellipsis").style("max-width: 180px;")
                if run_id:
                    ui.button(
                        icon="visibility",
                        on_click=lambda rid=run_id: open_agent_peek_dialog(
                            rid,
                            on_open_agent_thread=lambda row: open_agent_thread(
                                row,
                                state=state,
                                p=p,
                                rebuild_main=rebuild_main,
                                rebuild_thread_list=rebuild_thread_list,
                            ),
                        ),
                    ).props("flat dense round size=xs").tooltip("Peek Agent activity")
                if child_thread_id:
                    ui.button(
                        icon="open_in_new",
                        on_click=lambda row=agent_run: open_agent_thread(
                            row,
                            state=state,
                            p=p,
                            rebuild_main=rebuild_main,
                            rebuild_thread_list=rebuild_thread_list,
                        ),
                    ).props("flat dense round size=xs").tooltip("Open full Agent thread")
                if workspace_mode == "worktree":
                    ui.button(
                        icon="folder_open",
                        on_click=lambda row=agent_run: _open_agent_worktree(row),
                    ).props("flat dense round size=xs").tooltip("Open worktree")
                    ui.button(
                        icon="difference",
                        on_click=lambda row=agent_run: _show_agent_worktree_compare(row),
                    ).props("flat dense round size=xs").tooltip("Compare")
                if status not in _TERMINAL_STATUSES:
                    if member:
                        ui.button(
                            icon="message",
                            on_click=lambda oid=str(member.get("orchestration_id") or ""), rid=run_id: _message_agent(
                                oid,
                                rid,
                                p=p,
                            ),
                        ).props("flat dense round size=xs").tooltip(
                            "Queue guidance for the next safe boundary"
                        )
                    ui.button(
                        icon="stop",
                        on_click=lambda rid=run_id: (stop_agent_run(rid), rebuild_main()),
                    ).props("flat dense round size=xs color=orange").tooltip("Stop Agent")
                elif member and status in {"failed", "blocked", "timed_out", "stopped"}:
                    def _retry(rid=run_id) -> None:
                        try:
                            from row_bot.agent_orchestrator import retry_member

                            retry_member(rid, force=True)
                            ui.notify("Replacement Agent started.", type="positive")
                            rebuild_main()
                        except Exception as exc:
                            ui.notify(str(exc), type="negative", close_button=True)

                    ui.button(
                        icon="refresh",
                        on_click=_retry,
                    ).props("flat dense round size=xs color=primary").tooltip(
                        "Retry as a new Agent Run"
                    )
