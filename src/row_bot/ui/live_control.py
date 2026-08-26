"""Persistent, low-overhead live controls for separate Browser and Computer engines."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable

from nicegui import run, ui

from row_bot.automation.contracts import ActivitySnapshot

@dataclass(frozen=True)
class LiveControlView:
    engine: str = ""
    active: bool = False
    state: str = "idle"
    state_label: str = ""
    target: str = ""
    scope: str = ""
    last_action: str = ""
    can_take_over: bool = False
    can_resume: bool = False
    can_preview: bool = False
    has_preview: bool = False
    preview_shielded: bool = False
    frame_width: int = 0
    frame_height: int = 0
    revision: int = 0
    generation_id: str = ""


def computer_live_control_view(snapshot: dict[str, Any], thread_id: str) -> LiveControlView:
    activity = ActivitySnapshot.from_mapping(snapshot, surface="computer")
    active = activity.active and activity.thread_id == str(thread_id or "")
    if not active:
        return LiveControlView(revision=activity.revision)
    state = activity.state
    app = str(snapshot.get("app") or "Computer Use")
    window = str(snapshot.get("window") or "")
    return LiveControlView(
        engine="computer",
        active=True,
        state=state,
        state_label=activity.state_label,
        target=f"{app} · {window}" if window and window.casefold() != app.casefold() else app,
        scope="This app only",
        last_action=activity.last_action,
        can_take_over=state not in {"waiting_user", "resuming", "stopping", "failed"},
        can_resume=state == "waiting_user",
        can_preview=True,
        has_preview=bool(snapshot.get("has_thumbnail")) and state != "waiting_user",
        preview_shielded=state in {"waiting_user", "waiting_approval"},
        frame_width=int(snapshot.get("frame_width") or 0),
        frame_height=int(snapshot.get("frame_height") or 0),
        revision=activity.revision,
        generation_id=activity.generation_id,
    )


def browser_live_control_view(snapshot: dict[str, Any], thread_id: str) -> LiveControlView:
    activity = ActivitySnapshot.from_mapping(snapshot, surface="browser")
    active = activity.active and activity.thread_id == str(thread_id or "")
    if not active:
        return LiveControlView(revision=activity.revision)
    state = activity.state
    return LiveControlView(
        engine="browser",
        active=True,
        state=state,
        state_label=activity.state_label,
        target=activity.target or "Browser",
        scope="This task tab only",
        last_action=activity.last_action,
        can_take_over=state not in {"waiting_user", "stopping", "needs_attention"},
        can_resume=False,
        can_preview=True,
        has_preview=bool(snapshot.get("has_thumbnail")) and state != "waiting_user",
        preview_shielded=activity.preview_shielded or state == "waiting_user",
        revision=activity.revision,
    )


def select_live_control_view(
    computer_snapshot: dict[str, Any],
    browser_snapshot: dict[str, Any],
    thread_id: str,
) -> LiveControlView:
    """Select presentation only; engine services remain entirely separate."""

    computer = computer_live_control_view(computer_snapshot, thread_id)
    if computer.active:
        return computer
    return browser_live_control_view(browser_snapshot, thread_id)


def build_live_control_dock(
    state: Any,
    p: Any,
    *,
    stop_generation: Callable[..., Any],
) -> Any:
    """Mount the persistent chat control dock without action-path polling."""

    from row_bot.computer_use.service import get_computer_use_service
    from row_bot.tools.browser_tool import get_session_manager

    previous_cleanup = getattr(p, "live_control_cleanup", None)
    if callable(previous_cleanup):
        previous_cleanup()

    computer_service = get_computer_use_service()
    browser_manager = get_session_manager()
    client = ui.context.client
    current: dict[str, Any] = {
        "view": LiveControlView(),
        "preview_hidden": False,
        "preview_key": None,
    }

    card = ui.card().classes("w-full q-pa-sm row-bot-live-control").style(
        "border: 1px solid rgba(56,189,248,.45); "
        "background: linear-gradient(135deg, rgba(8,47,73,.45), rgba(17,24,39,.92)); "
        "box-shadow: 0 8px 24px rgba(0,0,0,.2);"
    )
    with card:
        with ui.row().classes("w-full items-center no-wrap gap-2"):
            engine_icon = ui.icon("computer", size="sm").classes("text-light-blue-4")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                title_label = ui.label("Live control").classes("text-sm text-weight-bold ellipsis")
                detail_label = ui.label("").classes("text-xs text-grey-5 ellipsis")
            state_badge = ui.badge("Observing", color="blue-grey")

        preview_container = ui.column().classes(
            "w-full gap-1 row-bot-live-preview"
        ).style("width: min(100%, 420px); align-self: flex-start;")
        preview_container.set_visibility(False)

        with ui.row().classes("w-full items-center gap-2"):
            stop_button = ui.button("Stop", icon="stop").props(
                "unelevated dense no-caps color=negative"
            )
            takeover_button = ui.button("Take over", icon="pan_tool").props(
                "outline dense no-caps"
            )
            resume_button = ui.button("Resume", icon="play_arrow").props(
                "outline dense no-caps color=positive"
            )
            preview_button = ui.button("Hide picture", icon="picture_in_picture_alt").props(
                "flat dense no-caps"
            )
            ui.space()
            scope_label = ui.label("").classes("text-xs text-grey-6")

    card.set_visibility(False)

    def _sync_visibility(
        element: Any,
        visible: bool,
        *,
        visible_display: str = "block",
    ) -> None:
        """Keep NiceGUI's property and serialized `hidden` class in sync."""

        element.set_visibility(visible)
        if visible:
            element.classes(remove="hidden")
        else:
            element.classes(add="hidden")
        element.style(
            add=(
                f"display: {visible_display} !important"
                if visible
                else "display: none !important"
            )
        )
        element.update()

    def _clear_preview(*, reset_preference: bool = False) -> None:
        if reset_preference:
            current["preview_hidden"] = False
        current["preview_key"] = None
        preview_container.clear()
        _sync_visibility(preview_container, False, visible_display="flex")
        preview_button.set_text(
            "Show picture" if current["preview_hidden"] else "Hide picture"
        )

    def _render_preview(
        image: bytes | None,
        *,
        browser: bool = False,
        shielded: bool = False,
    ) -> None:
        preview_container.clear()
        if shielded:
            with preview_container:
                ui.label(
                    "Picture hidden while you control the target or while a protected surface is visible."
                ).classes("text-xs text-grey-5 q-pa-sm")
        elif not image:
            with preview_container:
                ui.label("Waiting for the first safe target picture…").classes(
                    "text-xs text-grey-5 q-pa-sm"
                )
        else:
            source = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
            with preview_container:
                ui.image(source).props("fit=contain no-spinner").classes(
                    "w-full row-bot-live-preview-frame"
                ).style(
                    "height: min(220px, 28vh); background: rgba(0,0,0,.32); "
                    "border-radius: 8px; overflow: hidden;"
                )
                ui.label("Live picture · ephemeral and removed when this session ends").classes(
                    "text-xs text-grey-6"
                )
        _sync_visibility(preview_container, True, visible_display="flex")
        preview_button.set_text("Hide picture")

    def _snapshots() -> tuple[dict[str, Any], dict[str, Any], str]:
        thread_id = str(getattr(state, "thread_id", "") or "")
        return (
            computer_service.status_snapshot(),
            browser_manager.status_snapshot(thread_id),
            thread_id,
        )

    def _refresh() -> None:
        computer_snapshot, browser_snapshot, thread_id = _snapshots()
        view = select_live_control_view(computer_snapshot, browser_snapshot, thread_id)
        previous = current["view"]
        current["view"] = view
        # A listener can race the initial NiceGUI element payload. Always send
        # the authoritative class state so a lost first transition cannot leave
        # a client-side `hidden` class after the server view becomes active.
        _sync_visibility(card, view.active)
        if not view.active:
            _clear_preview(reset_preference=True)
            return
        engine_icon.props(
            "name=computer" if view.engine == "computer" else "name=language"
        )
        title_label.set_text(
            f"Computer · {view.target}" if view.engine == "computer" else f"Browser · {view.target}"
        )
        detail_label.set_text(view.last_action or view.state_label)
        state_badge.set_text(view.state_label)
        scope_label.set_text(view.scope)
        takeover_button.set_visibility(view.can_take_over)
        resume_button.set_visibility(view.can_resume or (view.engine == "browser" and view.state == "waiting_user"))
        resume_button.set_text("Resume")
        preview_button.set_visibility(view.can_preview)
        if previous.engine != view.engine or previous.active != view.active:
            _clear_preview(reset_preference=True)

        preview_key = (
            view.engine,
            view.revision,
            view.preview_shielded,
            current["preview_hidden"],
        )
        if current["preview_hidden"]:
            if current["preview_key"] != preview_key:
                preview_container.clear()
                _sync_visibility(preview_container, False, visible_display="flex")
                preview_button.set_text("Show picture")
                current["preview_key"] = preview_key
        elif current["preview_key"] != preview_key:
            image = (
                computer_service.ephemeral_screenshot()
                if view.engine == "computer"
                else browser_manager.ephemeral_screenshot(thread_id)
            )
            _render_preview(
                image,
                browser=view.engine == "browser",
                shielded=view.preview_shielded,
            )
            current["preview_key"] = preview_key

    def _schedule_refresh(_snapshot: dict[str, Any] | None = None) -> None:
        try:
            client.safe_invoke(_refresh)
        except RuntimeError:
            pass

    def _stop() -> None:
        view = current["view"]
        reason = "live_control"
        if view.engine == "computer":
            computer_service.stop()
            reason = "computer_task_stop"
        elif view.engine == "browser":
            browser_manager.end_activity(str(getattr(state, "thread_id", "") or ""))
            reason = "browser_task_stop"
        stop_generation(
            str(getattr(state, "thread_id", "") or ""),
            reason=reason,
        )
        _clear_preview(reset_preference=True)
        _refresh()

    def _take_over() -> None:
        view = current["view"]
        if view.engine == "computer":
            computer_service.take_over(
                thread_id=str(getattr(state, "thread_id", "") or ""),
                generation_id=view.generation_id,
            )
        elif view.engine == "browser":
            thread_id = str(getattr(state, "thread_id", "") or "")
            stop_generation(thread_id, reason="browser_takeover")
            browser_manager.take_over(thread_id)
        _clear_preview()
        _refresh()

    async def _resume_or_done() -> None:
        view = current["view"]
        if view.engine == "computer":
            pending = getattr(state, "pending_interrupt", None)
            items = pending if isinstance(pending, list) else [pending]
            takeover_pending = any(
                isinstance(item, dict)
                and str(item.get("kind") or "") == "computer_takeover"
                for item in items
            )
            callbacks = getattr(p, "streaming_callbacks", None)
            if not takeover_pending or callbacks is None:
                ui.notify(
                    "Computer is still pausing safely. Resume will be available in a moment.",
                    type="info",
                )
                return
            try:
                await run.io_bound(computer_service.resume_from_local_ui)
                from row_bot.ui.streaming import resume_after_interrupt

                await resume_after_interrupt(
                    True,
                    state=state,
                    p=p,
                    cb=callbacks,
                )
                ui.notify("Computer control resumed from a fresh capture.", type="positive")
            except Exception as exc:
                ui.notify(str(exc), type="negative")
        else:
            browser_manager.end_activity(str(getattr(state, "thread_id", "") or ""))
        _refresh()

    def _toggle_preview() -> None:
        current["preview_hidden"] = not bool(current["preview_hidden"])
        current["preview_key"] = None
        _refresh()

    stop_button.on_click(_stop)
    takeover_button.on_click(_take_over)
    resume_button.on_click(_resume_or_done)
    preview_button.on_click(_toggle_preview)

    remove_computer_listener = computer_service.add_listener(_schedule_refresh)
    remove_browser_listener = browser_manager.add_activity_listener(_schedule_refresh)

    def _disconnect() -> None:
        remove_computer_listener()
        remove_browser_listener()

    client.on_disconnect(_disconnect)
    p.live_control_container = card
    p.live_control_refresh = _refresh
    p.live_control_cleanup = _disconnect
    _refresh()
    return card
