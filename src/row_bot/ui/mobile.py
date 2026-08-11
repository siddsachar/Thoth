"""Browser-first compact owner shell for Row-Bot."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from nicegui import run as nicegui_run, ui

from row_bot.brand import APP_DISPLAY_NAME
from row_bot.ui.access_context import (
    access_context_from_client,
    uses_compact_presentation,
)
from row_bot.ui.mobile_chat import build_mobile_chat, mobile_chat_mode, open_thread_on_mobile, set_mobile_chat_mode
from row_bot.ui.mobile_workflows import build_mobile_workflows
from row_bot.ui.state import AppState, P, _active_generations

logger = logging.getLogger(__name__)

_MOBILE_TABS: tuple[tuple[str, str], ...] = (
    ("Chat", "chat"),
    ("Activity", "notifications"),
    ("Workflows", "bolt"),
    ("Knowledge", "psychology"),
    ("Settings", "settings"),
)

_MOBILE_CSS = """
<style>
body:has(.row-bot-mobile-root) {
    margin: 0 !important;
    overflow: hidden !important;
    background: #071113 !important;
}
body:has(.row-bot-mobile-root) .nicegui-content,
body:has(.row-bot-mobile-root) .q-layout,
body:has(.row-bot-mobile-root) .q-page,
body:has(.row-bot-mobile-root) main {
    margin: 0 !important;
    padding: 0 !important;
    width: 100vw !important;
    max-width: none !important;
    min-height: 100dvh !important;
    overflow: hidden !important;
}
.row-bot-mobile-root,
.row-bot-mobile-shell {
    box-sizing: border-box;
    min-width: 0;
    max-width: 100%;
}
.row-bot-main-shell.row-bot-mobile-root {
    position: fixed;
    inset: 0;
    width: 100vw !important;
    height: 100dvh !important;
    max-width: none !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    overflow: hidden !important;
}
.row-bot-mobile-outer {
    box-sizing: border-box;
    width: 100vw;
    max-width: none;
    height: 100dvh;
    min-height: 0;
    overflow: hidden;
    padding: 0;
    margin: 0;
    border: 0;
    border-radius: 0;
}
.row-bot-mobile-shell {
    width: 100vw;
    height: 100%;
    min-height: 0;
    overflow: hidden;
    border: 0;
    border-radius: 0;
    background: #071113;
    color: #ecf4f5;
}
.row-bot-mobile-header {
    flex: 0 0 auto;
    min-height: 42px;
    padding: calc(4px + env(safe-area-inset-top)) 12px 6px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    background: #0b1518;
}
.row-bot-mobile-content {
    flex: 1 1 auto;
    min-height: 0;
    width: 100%;
    overflow: hidden;
}
.row-bot-mobile-content .q-scrollarea__content {
    max-width: 100%;
}
.row-bot-mobile-pad {
    padding: 14px;
    padding-bottom: 84px;
}
.row-bot-mobile-nav {
    flex: 0 0 auto;
    min-height: 64px;
    border-top: 1px solid rgba(255,255,255,0.08);
    background: #10191c;
    padding: 6px 6px max(6px, env(safe-area-inset-bottom));
}
.row-bot-mobile-nav .q-btn {
    min-width: 0;
    width: 20%;
    height: 52px;
    border-radius: 8px;
}
.row-bot-mobile-nav .q-btn__content {
    gap: 2px;
    flex-direction: column;
    font-size: 0.68rem;
    line-height: 1.1;
    white-space: nowrap;
}
.row-bot-mobile-card {
    box-sizing: border-box;
    width: 100%;
    border: 1px solid rgba(255,255,255,0.09);
    border-radius: 8px;
    background: #162124;
    padding: 10px;
}
.row-bot-mobile-metric {
    border-radius: 8px;
    background: #18272b;
    border: 1px solid rgba(255,255,255,0.08);
    padding: 8px 10px;
    min-width: 86px;
    flex: 1 1 0;
}
.row-bot-mobile-thread-row,
.row-bot-mobile-list-row {
    border-bottom: 1px solid rgba(255,255,255,0.07);
    min-height: 58px;
    padding: 10px 0;
}
.row-bot-mobile-list-card {
    box-sizing: border-box;
    width: 100%;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    padding: 12px 0;
}
.row-bot-mobile-chat-start textarea {
    min-height: 118px;
}
.row-bot-mobile-chat-detail {
    flex: 1 1 auto;
    min-height: 0;
    height: 100%;
    overflow: hidden;
}
.row-bot-mobile-detail-header {
    flex: 0 0 auto;
    min-height: 46px;
    padding: 4px 8px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    background: #0d171a;
}
.row-bot-mobile-message-area {
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
}
.row-bot-mobile-composer {
    box-sizing: border-box;
    flex: 0 0 auto;
    min-width: 0;
    max-width: 100%;
    padding: 6px max(10px, env(safe-area-inset-right)) max(8px, env(safe-area-inset-bottom)) max(10px, env(safe-area-inset-left));
    border-top: 1px solid rgba(255,255,255,0.08);
    background: #10191c;
}
.row-bot-mobile-composer .q-textarea {
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 14px;
    background: #142124;
}
.row-bot-mobile-model-pill {
    min-width: 0;
    max-width: 44vw;
}
.row-bot-mobile-model-pill .q-btn__content {
    min-width: 0;
}
.row-bot-mobile-model-pill .q-btn__content span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.row-bot-mobile-action-row {
    box-sizing: border-box;
    width: 100%;
    max-width: 100%;
    min-width: 0;
    overflow: hidden;
}
.row-bot-mobile-action-row > .q-space {
    min-width: 4px;
}
.row-bot-mobile-skill-chip-slot {
    display: flex;
    justify-content: flex-end;
    flex: 0 0 84px;
    min-width: 0;
    max-width: 84px;
    overflow: hidden;
}
.row-bot-mobile-skill-chip-row {
    min-width: 0;
    max-width: 100%;
    overflow: hidden;
}
.row-bot-mobile-skill-chip-row .q-btn {
    min-width: 0;
    max-width: 100%;
    padding-left: 6px;
    padding-right: 6px;
}
.row-bot-mobile-skill-chip-row .q-btn__content {
    min-width: 0;
    flex-wrap: nowrap;
    gap: 3px;
}
.row-bot-mobile-skill-chip-row .q-btn__content span {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.row-bot-mobile-action-row > .q-btn {
    flex: 0 0 auto;
}
.row-bot-mobile-action-row > .row-bot-mobile-model-pill {
    flex: 1 1 0;
    width: 0;
    overflow: hidden;
}
.row-bot-mobile-send-button {
    flex: 0 0 auto;
}
.row-bot-mobile-policy-chip {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    width: 100%;
    box-sizing: border-box;
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 8px;
    background: rgba(148, 163, 184, 0.08);
    padding: 8px;
}
.row-bot-mobile-sheet {
    border-radius: 18px 18px 0 0;
    padding: 16px;
    background: #10191c;
    color: #ecf4f5;
}
.row-bot-mobile-empty {
    display: flex;
    flex-direction: column;
    gap: 6px;
    align-items: center;
    justify-content: center;
    min-height: 150px;
    padding: 18px;
    color: #c5d0d3;
    text-align: center;
}
.row-bot-mobile-workflow-editor {
    max-width: none;
    border-radius: 0;
    background: #071113;
    color: #ecf4f5;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}
.row-bot-mobile-editor-header {
    flex: 0 0 auto;
    padding: calc(8px + env(safe-area-inset-top)) 12px 10px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    background: #10191c;
}
.row-bot-mobile-editor-body {
    flex: 1 1 auto;
    min-height: 0;
    padding: 12px;
}
.row-bot-mobile-editor-body .q-scrollarea__content {
    padding: 12px;
}
.row-bot-mobile-section {
    width: 100%;
    padding: 0 0 14px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
}
.row-bot-mobile-section-title {
    font-size: 0.78rem;
    font-weight: 700;
    letter-spacing: 0;
    color: #a9b8bc;
    margin-bottom: 8px;
}
.row-bot-mobile-notice {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    width: 100%;
    border-radius: 8px;
    border: 1px solid rgba(88, 166, 255, 0.28);
    background: rgba(88, 166, 255, 0.08);
    padding: 10px;
}
.row-bot-mobile-shell .q-card {
    border-radius: 8px;
}
.row-bot-mobile-shell .q-field,
.row-bot-mobile-shell .q-select,
.row-bot-mobile-shell .q-textarea,
.row-bot-mobile-shell .q-input {
    min-width: 0;
    max-width: 100%;
}
@media (max-width: 520px) {
    .row-bot-mobile-pad {
        padding-left: 10px;
        padding-right: 10px;
    }
    .row-bot-mobile-header .text-h6 {
        font-size: 1rem;
    }
    .row-bot-mobile-model-pill {
        max-width: 42vw;
    }
    .row-bot-mobile-skill-chip-slot {
        flex-basis: 80px;
        max-width: 80px;
    }
}
</style>
"""


def is_mobile_client(client: Any) -> bool:
    """Return whether the centrally authorized request selected compact layout.

    The query-string fallback exists only for small isolated UI tests where the
    access middleware is not present. It never grants capabilities.
    """
    context = access_context_from_client(client)
    if context is not None:
        return uses_compact_presentation(context)
    request = getattr(client, "request", None)
    query_params = getattr(request, "query_params", {}) or {}
    value = str(query_params.get("mobile") or query_params.get("m") or "").strip().lower()
    return value in {"1", "true", "yes", "compact"}


def _mobile_view(state: AppState) -> str:
    view = str(getattr(state, "mobile_view", "") or "Chat")
    valid = {name for name, _icon in _MOBILE_TABS}
    if view not in valid:
        view = "Chat"
    if getattr(state, "mobile_view", None) != view:
        setattr(state, "mobile_view", view)
    return view


def _set_mobile_view(state: AppState, view: str) -> None:
    setattr(state, "mobile_view", view)


def _copy_button(value: str, tooltip: str = "Copy") -> None:
    encoded = json.dumps(value)
    ui.button(
        icon="content_copy",
        on_click=lambda: (
            ui.run_javascript(f"navigator.clipboard.writeText({encoded})"),
            ui.notify("Copied", type="info"),
        ),
    ).props("flat dense round size=sm").tooltip(tooltip)


def _safe_call(label: str, fn: Callable[[], Any], fallback: Any) -> Any:
    try:
        return fn()
    except Exception as exc:
        logger.warning("Mobile %s unavailable: %s", label, exc)
        return fallback


def _pending_tool_interrupt(state: AppState) -> Any | None:
    if state.pending_interrupt:
        return state.pending_interrupt
    if state.thread_id:
        gen = _active_generations.get(state.thread_id)
        if gen is not None and getattr(gen, "interrupt_data", None):
            return gen.interrupt_data
    for gen in _active_generations.values():
        if getattr(gen, "interrupt_data", None):
            return gen.interrupt_data
    return None


def respond_to_mobile_approval(
    resume_token: str,
    approved: bool,
    note: str = "",
) -> bool:
    """Respond to a durable approval from the mobile surface."""
    from row_bot import tasks

    return bool(tasks.respond_to_approval(resume_token, approved, note=note, source="mobile"))


def respond_to_mobile_workflow_approval(
    resume_token: str,
    approved: bool,
    note: str = "",
) -> bool:
    """Compatibility alias for older mobile workflow approval callers."""

    return respond_to_mobile_approval(resume_token, approved, note=note)


async def respond_to_mobile_workflow_approval_async(
    resume_token: str,
    approved: bool,
    note: str = "",
) -> bool:
    """Respond to a durable approval without blocking the NiceGUI event loop."""
    return bool(await nicegui_run.io_bound(respond_to_mobile_approval, resume_token, approved, note))


def _build_mobile_header(state: AppState, *, open_settings: Callable[..., None]) -> None:
    with ui.row().classes("row-bot-mobile-header w-full items-center gap-2 no-wrap"):
        ui.image("/static/row_bot_glyph_256.png").style("width: 24px; height: 24px; object-fit: contain;")
        ui.label(APP_DISPLAY_NAME).classes("text-subtitle2 ellipsis")


def _build_mobile_nav(
    state: AppState,
    *,
    rebuild_main: Callable[..., None],
    open_settings: Callable[..., None],
) -> None:
    active = _mobile_view(state)
    with ui.row().classes("row-bot-mobile-nav w-full items-center justify-around no-wrap"):
        for name, icon in _MOBILE_TABS:
            def _switch(tab: str = name) -> None:
                if tab == "Settings":
                    open_settings("Providers")
                    return
                if tab == "Chat":
                    set_mobile_chat_mode(state, "threads")
                _set_mobile_view(state, tab)
                rebuild_main(immediate=True, reason=f"mobile_tab_{tab.lower()}")

            button = ui.button(name, icon=icon, on_click=_switch).props("flat dense no-caps stack")
            if name == active:
                button.props("color=primary")
            else:
                button.props("color=grey-6")


def _metric(label: str, value: Any, icon: str) -> None:
    with ui.element("div").classes("row-bot-mobile-metric"):
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon(icon).classes("text-primary")
            ui.label(str(value)).classes("text-subtitle1")
        ui.label(label).classes("text-grey-6 text-xs")


def _build_activity(
    state: AppState,
    *,
    rebuild_main: Callable[..., None],
    show_interrupt: Callable[[Any], None],
) -> None:
    from row_bot.tasks import get_pending_approvals, get_recent_runs, get_running_tasks, stop_task

    pending_approvals = _safe_call("pending approvals", get_pending_approvals, [])
    running_tasks = _safe_call("running workflows", get_running_tasks, {})
    recent_runs = _safe_call("recent runs", lambda: get_recent_runs(limit=8), [])
    tool_interrupt = _pending_tool_interrupt(state)
    active_generations = [
        gen for gen in _active_generations.values()
        if str(getattr(gen, "status", "")) in {"streaming", "queued", "paused"}
        or getattr(gen, "interrupt_data", None)
    ]

    with ui.row().classes("w-full gap-2"):
        _metric("Approvals", len(pending_approvals) + (1 if tool_interrupt else 0), "verified_user")
        _metric("Running", len(running_tasks) + len(active_generations), "autorenew")
        _metric("Recent", len(recent_runs), "history")

    if tool_interrupt:
        with ui.element("div").classes("row-bot-mobile-card"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.icon("warning_amber").classes("text-warning")
                with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                    ui.label("Chat tool approval").classes("text-subtitle2")
                    ui.label("An agent action is waiting for your approval.").classes("text-grey-6 text-xs")
                ui.button(
                    "Review",
                    icon="task_alt",
                    on_click=lambda data=tool_interrupt: show_interrupt(data),
                ).props("flat dense no-caps color=primary")

    if pending_approvals:
        ui.label("Pending approvals").classes("text-subtitle2 q-mt-sm")
    for approval in pending_approvals:
        try:
            from row_bot.approval_messages import compact_message, payload_from_row

            approval_payload = payload_from_row(approval)
            message = compact_message(approval_payload, max_chars=360)
            task_name = str(
                approval_payload.get("source_label")
                or approval.get("source_label")
                or approval.get("task_name")
                or "Approval"
            )
        except Exception:
            message = str(approval.get("message") or "Approval required.")
            task_name = str(approval.get("source_label") or approval.get("task_name") or "Approval")
        resume_token = str(approval.get("resume_token") or "")
        busy_tokens = getattr(state, "mobile_workflow_approval_busy", None)
        if not isinstance(busy_tokens, set):
            busy_tokens = set()
            state.mobile_workflow_approval_busy = busy_tokens
        is_busy = bool(resume_token and resume_token in busy_tokens)

        async def _respond(approved: bool, token: str = resume_token) -> None:
            if not token:
                ui.notify("Approval is missing a resume token", type="warning")
                return
            submitting = getattr(state, "mobile_workflow_approval_busy", None)
            if not isinstance(submitting, set):
                submitting = set()
                state.mobile_workflow_approval_busy = submitting
            if token in submitting:
                return
            submitting.add(token)
            rebuild_main(immediate=True, reason="mobile_workflow_approval_sending")
            try:
                ok = await respond_to_mobile_workflow_approval_async(token, approved)
                ui.notify(
                    "Approval sent" if ok else "Approval is no longer pending",
                    type="positive" if ok else "warning",
                )
            finally:
                submitting.discard(token)
                rebuild_main(immediate=True, reason="mobile_workflow_approval")

        async def _deny(token: str = resume_token) -> None:
            await _respond(False, token)

        async def _approve(token: str = resume_token) -> None:
            await _respond(True, token)

        with ui.element("div").classes("row-bot-mobile-card"):
            ui.label(task_name).classes("text-subtitle2")
            ui.label(message).classes("text-grey-5 text-sm").style("white-space: pre-wrap;")
            if is_busy:
                with ui.row().classes("items-center gap-2 text-primary"):
                    ui.spinner(size="sm")
                    ui.label("Sending approval...").classes("text-xs")
            with ui.row().classes("w-full justify-end gap-2"):
                deny_btn = ui.button("Deny", icon="close", on_click=_deny).props(
                    "flat dense no-caps color=negative"
                )
                approve_btn = ui.button("Approve", icon="check", on_click=_approve).props(
                    "flat dense no-caps color=positive"
                )
                if is_busy:
                    deny_btn.disable()
                    approve_btn.disable()

    if running_tasks:
        ui.label("Running workflows").classes("text-subtitle2 q-mt-sm")
    for thread_id, info in running_tasks.items():
        with ui.element("div").classes("row-bot-mobile-card"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.icon("bolt").classes("text-primary")
                with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                    ui.label(str(info.get("name") or "Workflow")).classes("text-subtitle2 ellipsis")
                    ui.label(
                        f"Step {int(info.get('step', 0)) + 1}/{info.get('total', '?')}"
                    ).classes("text-grey-6 text-xs")
                ui.button(
                    icon="stop",
                    on_click=lambda tid=thread_id: (
                        stop_task(tid),
                        ui.notify("Stop signal sent", type="warning"),
                        rebuild_main(immediate=True, reason="mobile_stop_workflow"),
                    ),
                ).props("flat dense round color=negative").tooltip("Stop")

    if active_generations:
        ui.label("Active chat").classes("text-subtitle2 q-mt-sm")
    for gen in active_generations:
        with ui.element("div").classes("row-bot-mobile-card"):
            ui.label(str(getattr(gen, "thread_id", "") or "Chat")).classes("text-subtitle2")
            status = str(getattr(gen, "status", "") or "running")
            ui.label(status.replace("_", " ").title()).classes("text-grey-6 text-xs")

    ui.label("Recent workflow runs").classes("text-subtitle2 q-mt-sm")
    if not recent_runs:
        ui.label("No workflow runs yet.").classes("text-grey-6 text-sm")
    for run in recent_runs:
        with ui.row().classes("row-bot-mobile-list-row w-full items-center gap-2 no-wrap"):
            ui.icon("history").classes("text-grey-6")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                ui.label(str(run.get("task_name") or "Workflow")).classes("text-sm ellipsis")
                ui.label(str(run.get("status") or "unknown")).classes("text-grey-6 text-xs")


def _build_chat_start(
    state: AppState,
    p: P,
    *,
    send_message: Callable[..., Any],
    load_thread_messages: Callable[[str], list[dict]],
    rebuild_main: Callable[..., None],
) -> None:
    with ui.element("div").classes("row-bot-mobile-card row-bot-mobile-chat-start"):
        ui.label("New chat").classes("text-subtitle2")
        text_input = ui.textarea("Message Row-Bot").classes("w-full").props("outlined autogrow")

        def _submit() -> None:
            text = str(text_input.value or "").strip()
            if not text:
                return
            state.active_designer_project = None
            state.active_developer_workspace_id = None
            _set_mobile_view(state, "Chat")
            asyncio.create_task(send_message(text))

        ui.button("Send", icon="send", on_click=_submit).props("flat no-caps color=primary")

    from row_bot.threads import _list_threads

    threads = _safe_call("threads", lambda: _list_threads(include_details=True), [])[:8]
    try:
        from row_bot.agent_orchestrator import get_thread_orchestration_activity

        orchestration_activity = get_thread_orchestration_activity(
            [str(row[0]) for row in threads]
        )
    except Exception:
        logger.debug("Could not load mobile Agent activity", exc_info=True)
        orchestration_activity = {}
    ui.label("Recent chats").classes("text-subtitle2 q-mt-sm")
    if not threads:
        ui.label("No chats yet.").classes("text-grey-6 text-sm")
    for row in threads:
        thread_id = str(row[0])
        name = str(row[1] or "Untitled")
        updated = str(row[3] or "")
        agent_activity = orchestration_activity.get(thread_id) or {}
        agent_is_blocking = bool(
            agent_activity.get("blocking")
            and str(agent_activity.get("state") or "") == "active"
        )
        with ui.row().classes("row-bot-mobile-thread-row w-full items-center gap-2 no-wrap"):
            if agent_is_blocking:
                ui.spinner("oval", size="xs", color="primary").props(
                    'role="status" aria-label="Child Agents working"'
                ).tooltip("Child Agents working")
            else:
                ui.icon("chat_bubble_outline").classes("text-grey-6")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                ui.label(name).classes("text-sm ellipsis")
                ui.label(updated).classes("text-grey-6 text-xs")
            ui.button(
                icon="chevron_right",
                on_click=lambda tid=thread_id: open_thread_on_mobile(
                    tid,
                    state=state,
                    p=p,
                    load_thread_messages=load_thread_messages,
                    rebuild_main=rebuild_main,
                ),
            ).props("flat dense round").tooltip("Open")


def _build_workflows(
    state: AppState,
    p: P,
    *,
    rebuild_main: Callable[..., None],
    rebuild_thread_list: Callable[[], None],
    show_task_dialog: Callable[[dict | None, Callable[[], None]], None],
    load_thread_messages: Callable[[str], list[dict]],
) -> None:
    from row_bot.tasks import _prepare_task_thread, get_running_task_thread, list_tasks, run_task_background
    from row_bot.tools import registry as tool_registry

    tasks = _safe_call("workflow list", list_tasks, [])
    if not tasks:
        with ui.element("div").classes("row-bot-mobile-card"):
            ui.label("No workflows yet.").classes("text-subtitle2")
            ui.button(
                "Create workflow",
                icon="add",
                on_click=lambda: show_task_dialog(None, lambda: rebuild_main(immediate=True, reason="mobile_workflow_dialog")),
            ).props("flat dense no-caps color=primary")
        return

    def _start_task(task: dict) -> None:
        thread_id = get_running_task_thread(task["id"])
        if thread_id:
            open_thread_on_mobile(
                thread_id,
                state=state,
                p=p,
                load_thread_messages=load_thread_messages,
                rebuild_main=rebuild_main,
            )
            return
        enabled = [tool.name for tool in tool_registry.get_enabled_tools()]
        thread_id = _prepare_task_thread(task)
        run_task_background(task["id"], thread_id, enabled, notification=True)
        open_thread_on_mobile(
            thread_id,
            state=state,
            p=p,
            load_thread_messages=load_thread_messages,
            rebuild_main=rebuild_main,
        )
        rebuild_thread_list()

    with ui.row().classes("w-full justify-between items-center"):
        ui.label("Workflows").classes("text-subtitle2")
        ui.button(
            icon="add",
            on_click=lambda: show_task_dialog(None, lambda: rebuild_main(immediate=True, reason="mobile_workflow_dialog")),
        ).props("flat dense round color=primary").tooltip("New workflow")

    for task in tasks:
        name = str(task.get("name") or "Workflow")
        description = str(task.get("description") or "")
        enabled = bool(task.get("enabled", True))
        with ui.element("div").classes("row-bot-mobile-card"):
            with ui.row().classes("w-full items-start gap-2 no-wrap"):
                ui.icon("bolt").classes("text-primary" if enabled else "text-grey-6")
                with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                    ui.label(name).classes("text-subtitle2 ellipsis")
                    if description:
                        ui.label(description).classes("text-grey-6 text-xs").style("white-space: pre-wrap;")
                    status = "Enabled" if enabled else "Disabled"
                    ui.label(status).classes("text-grey-6 text-xs")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(
                    icon="edit",
                    on_click=lambda t=task: show_task_dialog(
                        t,
                        lambda: rebuild_main(immediate=True, reason="mobile_workflow_dialog"),
                    ),
                ).props("flat dense round").tooltip("Edit")
                run_button = ui.button(
                    "Run",
                    icon="play_arrow",
                    on_click=lambda t=task: _start_task(t),
                ).props("flat dense no-caps color=primary")
                if not enabled:
                    run_button.disable()


def _build_knowledge() -> None:
    from row_bot import knowledge_graph as kg

    stats = _safe_call("knowledge stats", kg.get_graph_stats, {})
    with ui.row().classes("w-full gap-2"):
        _metric("Entities", stats.get("total_entities", 0), "account_tree")
        _metric("Relations", stats.get("total_relations", 0), "hub")

    results_col = ui.column().classes("w-full gap-2")
    query_input = ui.input("Search knowledge").classes("w-full").props("outlined dense")

    def _render_results() -> None:
        query = str(query_input.value or "").strip()
        results_col.clear()
        with results_col:
            if not query:
                ui.label("Search local knowledge without provider calls.").classes("text-grey-6 text-sm")
                return
            results = _safe_call("knowledge search", lambda: kg.search_entities(query, limit=12), [])
            if not results:
                ui.label("No matching memories found.").classes("text-grey-6 text-sm")
                return
            for entity in results:
                with ui.element("div").classes("row-bot-mobile-card"):
                    ui.label(str(entity.get("subject") or "Untitled")).classes("text-subtitle2")
                    ui.label(str(entity.get("entity_type") or "entity")).classes("text-grey-6 text-xs")
                    desc = str(entity.get("description") or "")
                    if desc:
                        ui.label(desc[:360]).classes("text-sm").style("white-space: pre-wrap;")

    query_input.on("keydown.enter", lambda: _render_results())
    ui.button("Search", icon="search", on_click=_render_results).props("flat dense no-caps color=primary")
    _render_results()


def _build_desktop_only_notice(
    state: AppState,
    *,
    rebuild_main: Callable[..., None],
) -> None:
    workspace = "Developer Studio" if state.active_developer_workspace_id else "Designer Studio"
    with ui.element("div").classes("row-bot-mobile-card"):
        ui.label(f"{workspace} is desktop-only").classes("text-subtitle2")
        ui.label(
            "The rich workspace stays on the desktop surface. You can still use its tools through normal chat on mobile."
        ).classes("text-grey-6 text-sm")

        def _return_to_chat() -> None:
            state.active_developer_workspace_id = None
            state.active_designer_project = None
            _set_mobile_view(state, "Chat")
            rebuild_main(immediate=True, reason="mobile_desktop_only_exit")

        ui.button("Continue in chat", icon="chat", on_click=_return_to_chat).props("flat dense no-caps color=primary")


def build_mobile_shell(
    state: AppState,
    p: P,
    *,
    rebuild_main: Callable[..., None],
    rebuild_thread_list: Callable[[], None],
    send_message: Callable[..., Any],
    show_task_dialog: Callable[[dict | None, Callable[[], None]], None],
    load_thread_messages: Callable[[str], list[dict]],
    open_settings: Callable[..., None],
    show_interrupt: Callable[[Any], None],
    add_chat_message: Callable[[dict], Any],
) -> None:
    """Render the compact owner shell."""
    ui.html(_MOBILE_CSS, sanitize=False)
    p.chat_scroll = None
    p.chat_container = None
    view = _mobile_view(state)
    active_chat_detail = (
        view == "Chat"
        and state.active_designer_project is None
        and state.active_developer_workspace_id is None
        and bool(state.thread_id)
        and mobile_chat_mode(state) == "thread"
    )
    with ui.column().classes("row-bot-mobile-shell w-full no-wrap").props("data-docs-id=mobile-shell"):
        if not active_chat_detail:
            _build_mobile_header(state, open_settings=open_settings)
        if view == "Chat" and state.active_designer_project is None and state.active_developer_workspace_id is None:
            with ui.column().classes("row-bot-mobile-content w-full").style("overflow: hidden;"):
                build_mobile_chat(
                    state,
                    p,
                    send_message=send_message,
                    load_thread_messages=load_thread_messages,
                    add_chat_message=add_chat_message,
                    open_settings=open_settings,
                    rebuild_main=rebuild_main,
                )
        else:
            with ui.scroll_area().classes("row-bot-mobile-content"):
                with ui.column().classes("row-bot-mobile-pad w-full gap-3"):
                    if state.active_designer_project is not None or state.active_developer_workspace_id is not None:
                        _build_desktop_only_notice(state, rebuild_main=rebuild_main)
                    elif view == "Activity":
                        _build_activity(state, rebuild_main=rebuild_main, show_interrupt=show_interrupt)
                    elif view == "Chat":
                        _build_chat_start(
                            state,
                            p,
                            send_message=send_message,
                            load_thread_messages=load_thread_messages,
                            rebuild_main=rebuild_main,
                        )
                    elif view == "Workflows":
                        build_mobile_workflows(
                            state,
                            p,
                            rebuild_main=rebuild_main,
                            rebuild_thread_list=rebuild_thread_list,
                            load_thread_messages=load_thread_messages,
                        )
                    elif view == "Knowledge":
                        _build_knowledge()
        if not active_chat_detail:
            _build_mobile_nav(state, rebuild_main=rebuild_main, open_settings=open_settings)
