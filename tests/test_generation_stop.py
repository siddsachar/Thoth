from __future__ import annotations

import asyncio
import queue
import threading
from types import SimpleNamespace

from row_bot.cancellation import CancellationScope
from row_bot.ui.state import GenerationState, _active_generations
from row_bot.ui import streaming


class _FakeButton:
    client = None

    def __init__(self) -> None:
        self.props_calls: list[str] = []
        self.disabled = False

    def props(self, value: str) -> "_FakeButton":
        self.props_calls.append(value)
        return self

    def disable(self) -> "_FakeButton":
        self.disabled = True
        return self


class _FakeDialog:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _generation(thread_id: str = "thread-stop") -> GenerationState:
    stop_event = threading.Event()
    scope = CancellationScope(stop_event)
    return GenerationState(
        thread_id=thread_id,
        q=queue.Queue(),
        stop_event=stop_event,
        config={},
        enabled_tools=[],
        cancel_scope=scope,
        generation_id="gen-1",
    )


def teardown_function() -> None:
    _active_generations.clear()


def test_suspended_orchestration_does_not_block_durable_transcript_refresh() -> None:
    gen = _generation("thread-orchestration-wait")
    gen.status = "streaming"
    gen.live_row = object()
    _active_generations[gen.thread_id] = gen

    assert streaming._thread_has_attached_live_generation(gen.thread_id) is True

    gen.orchestration_suspended = True

    assert streaming._thread_has_attached_live_generation(gen.thread_id) is False


def test_request_generation_stop_detaches_wakes_queue_and_cancels_scope(monkeypatch) -> None:
    gen = _generation()
    callback_calls: list[str] = []
    gen.cancel_scope.register(lambda: callback_calls.append("closed"), "fake.close")
    _active_generations[gen.thread_id] = gen
    monkeypatch.setattr(streaming, "_stop_generation_child_agent_runs", lambda _gen: None)
    computer_closed: list[str] = []
    browser_closed: list[str] = []
    monkeypatch.setattr(
        "row_bot.computer_use.service.get_computer_use_service",
        lambda: SimpleNamespace(close_for_thread=computer_closed.append),
    )
    monkeypatch.setattr(
        "row_bot.tools.browser_tool.get_session_manager",
        lambda: SimpleNamespace(end_activity=browser_closed.append),
    )
    button = _FakeButton()
    state = SimpleNamespace(
        thread_id=gen.thread_id,
        voice_coordinator=None,
        tts_service=None,
        pending_interrupt={"approval": True},
        pending_interrupt_generation_id=gen.generation_id,
        pending_interrupt_tool_groups={"Computer activity": {}},
        pending_interrupt_runtime_surface="normal_chat",
    )
    p = SimpleNamespace(stop_btn=button)

    result = streaming.request_generation_stop(gen.thread_id, state=state, p=p, reason="test")

    assert result.status == "stopped"
    assert result.stopped is True
    assert gen.status == "stopped"
    assert gen.stop_event.is_set() is True
    assert gen.cancel_scope.is_cancelled() is True
    assert callback_calls == ["closed"]
    assert _active_generations.get(gen.thread_id) is None
    assert button.disabled is True
    assert "icon=stop" in button.props_calls
    assert gen.q.get_nowait() == ("stopped", {"reason": "test"})
    assert computer_closed == [gen.thread_id]
    assert browser_closed == [gen.thread_id]
    assert state.pending_interrupt is None
    assert state.pending_interrupt_generation_id == ""
    assert state.pending_interrupt_tool_groups == {}
    assert state.pending_interrupt_runtime_surface == ""


def test_generation_stop_emits_one_terminal_buddy_event_and_clears_tool_lane(
    monkeypatch,
) -> None:
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    gen = _generation("thread-buddy-stop")
    _active_generations[gen.thread_id] = gen
    events: list[BuddyEvent] = []

    def _emit(event_type, *, source, payload):
        event = BuddyEvent(
            type=event_type,
            source=source,
            payload=payload,
            id=len(events) + 1,
        )
        events.append(event)
        return event

    monkeypatch.setattr("row_bot.buddy.events.emit_buddy_event", _emit)
    monkeypatch.setattr(streaming, "_cleanup_live_control_sessions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(streaming, "_stop_generation_child_agent_runs", lambda _gen: None)
    monkeypatch.setattr(streaming, "_stop_generation_voice_outputs", lambda *_args: None)

    streaming.request_generation_stop(gen.thread_id, reason="manual")
    streaming.request_generation_stop(gen.thread_id, reason="manual")

    assert [event.type for event in events] == [BuddyEventType.GENERATION_STOPPED]
    brain = BuddyBrain()
    brain.resolve(
        BuddyEvent(
            type=BuddyEventType.TOOL_STARTED,
            source="test",
            payload={"thread_id": gen.thread_id, "tool": "computer_use"},
            id=100,
        )
    )
    stopped = brain.resolve(
        BuddyEvent(
            type=BuddyEventType.GENERATION_STOPPED,
            source="test",
            payload={"thread_id": gen.thread_id},
            id=101,
        )
    )
    assert stopped.message == "Stopped"
    assert brain._active == {}


def test_request_generation_stop_handles_pending_approval_without_active_entry(
    monkeypatch,
) -> None:
    fallback_event = threading.Event()
    button = _FakeButton()
    dialog = _FakeDialog()
    cleanup_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        streaming,
        "_cleanup_live_control_sessions",
        lambda thread_id, **kwargs: cleanup_calls.append(
            (thread_id, str(kwargs.get("context") or ""))
        ),
    )
    state = SimpleNamespace(
        thread_id="missing",
        stop_event=fallback_event,
        pending_interrupt={"tool": "computer_use"},
        pending_interrupt_generation_id="gen-pending",
        pending_interrupt_tool_groups={"Computer activity": {}},
        pending_interrupt_runtime_surface="normal_chat",
    )
    p = SimpleNamespace(stop_btn=button, interrupt_dlg=dialog)

    result = streaming.request_generation_stop("missing", state=state, p=p)

    assert result.status == "not_found"
    assert fallback_event.is_set() is True
    assert button.disabled is True
    assert dialog.closed is True
    assert cleanup_calls == [("missing", "generation stop without active producer")]
    assert state.pending_interrupt is None
    assert state.pending_interrupt_generation_id == ""
    assert state.pending_interrupt_tool_groups == {}
    assert state.pending_interrupt_runtime_surface == ""


def test_denied_approval_resume_stops_before_the_model_can_retry() -> None:
    pulled: list[str] = []

    def _events():
        try:
            pulled.append("denied_result")
            yield (
                "tool_done",
                {
                    "name": "Computer Use (Beta)",
                    "content": '{"ok":false,"error":true,"error_code":"denied"}',
                },
            )
            pulled.append("model_retry")
            yield ("tool_call", {"name": "computer_use"})
        finally:
            pulled.append("closed")

    events = list(
        streaming._approval_resume_events(
            _events(),
            approved=False,
            pending={"tool": "computer_use"},
        )
    )

    assert [event[0] for event in events] == ["tool_done", "done"]
    assert events[-1][1] == "Computer Use access was denied. No action was taken."
    assert pulled == ["denied_result", "closed"]


def test_stale_approval_callback_cannot_resume_after_stop(monkeypatch) -> None:
    cleanup_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        streaming,
        "_cleanup_live_control_sessions",
        lambda thread_id, **kwargs: cleanup_calls.append(
            (thread_id, str(kwargs.get("context") or ""))
        ),
    )
    state = SimpleNamespace(
        thread_id="thread-stale-approval",
        pending_interrupt=None,
        pending_interrupt_generation_id="",
        pending_interrupt_tool_groups={},
        pending_interrupt_runtime_surface="",
    )
    p = SimpleNamespace(developer_approval_container=None)

    asyncio.run(
        streaming.resume_after_interrupt(
            False,
            state=state,
            p=p,
            cb=SimpleNamespace(),
        )
    )

    assert cleanup_calls == [
        ("thread-stale-approval", "stale approval response after cancellation")
    ]


def test_modal_stop_closes_approval_and_settles_before_service_cleanup(
    monkeypatch,
) -> None:
    dialog = _FakeDialog()
    state = SimpleNamespace(thread_id="thread-modal-stop")
    p = SimpleNamespace(interrupt_dlg=dialog)
    cb = SimpleNamespace()
    cleanup_calls: list[tuple[str, str]] = []
    resume_calls: list[tuple[bool, object, object, object, bool]] = []
    monkeypatch.setattr(
        streaming,
        "_cleanup_live_control_sessions",
        lambda thread_id, **kwargs: cleanup_calls.append(
            (thread_id, str(kwargs.get("context") or ""))
        ),
    )

    async def _resume(approved, *, state, p, cb, stop_requested=False):
        resume_calls.append((approved, state, p, cb, stop_requested))

    monkeypatch.setattr(streaming, "resume_after_interrupt", _resume)

    asyncio.run(streaming._stop_pending_approval(state, p, cb))

    assert dialog.closed is True
    assert cleanup_calls == []
    assert resume_calls == [(False, state, p, cb, True)]


def test_modal_stop_result_is_terminal_and_truthful() -> None:
    events = list(
        streaming._approval_resume_events(
            iter(
                [
                    (
                        "tool_done",
                        {
                            "name": "Computer Use (Beta)",
                            "content": '{"ok":false,"error":true,"error_code":"denied"}',
                        },
                    ),
                    ("tool_call", {"name": "computer_use"}),
                ]
            ),
            approved=False,
            pending={"tool": "computer_use"},
            stop_requested=True,
        )
    )

    assert [event[0] for event in events] == ["tool_done", "done"]
    assert events[-1][1] == "Computer Use was stopped. No action was taken."


def test_generation_stop_stops_only_generation_linked_child_runs(monkeypatch) -> None:
    gen = _generation()
    gen.baseline_child_agent_run_ids = {"old"}
    gen.live_agent_run_ids = {"live"}
    gen.live_async_agent_run_ids = {"async"}
    stopped: list[str] = []

    monkeypatch.setattr(streaming, "_child_agent_run_ids_for_thread", lambda _thread_id: {"old", "new"})

    import row_bot.agent_runner as agent_runner

    monkeypatch.setattr(agent_runner, "stop_agent_run", lambda run_id: stopped.append(run_id))

    streaming._stop_generation_child_agent_runs(gen)

    assert stopped == ["async", "live", "new"]


def test_final_live_control_cleanup_closes_computer_and_preserves_browser_takeover(
    monkeypatch,
) -> None:
    computer_closed: list[str] = []
    browser_closed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "row_bot.computer_use.service.get_computer_use_service",
        lambda: SimpleNamespace(
            status_snapshot=lambda: {"active": True, "thread_id": "thread-final", "state": "observing"},
            close_for_thread=computer_closed.append,
        ),
    )
    monkeypatch.setattr(
        "row_bot.tools.browser_tool.get_session_manager",
        lambda: SimpleNamespace(
            end_activity=lambda thread_id, *, preserve_takeover=False: browser_closed.append(
                (thread_id, preserve_takeover)
            )
        ),
    )

    streaming._cleanup_live_control_sessions(
        "thread-final",
        preserve_browser_takeover=True,
        context="test",
    )

    assert computer_closed == ["thread-final"]
    assert browser_closed == [("thread-final", True)]


def test_final_live_control_cleanup_preserves_matching_computer_approval_pause(
    monkeypatch,
) -> None:
    computer_closed: list[str] = []
    browser_closed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "row_bot.computer_use.service.get_computer_use_service",
        lambda: SimpleNamespace(
            status_snapshot=lambda: {
                "active": True,
                "thread_id": "thread-approval",
                "state": "waiting_approval",
            },
            close_for_thread=computer_closed.append,
        ),
    )
    monkeypatch.setattr(
        "row_bot.tools.browser_tool.get_session_manager",
        lambda: SimpleNamespace(
            end_activity=lambda thread_id, *, preserve_takeover=False: browser_closed.append(
                (thread_id, preserve_takeover)
            )
        ),
    )

    streaming._cleanup_live_control_sessions(
        "thread-approval",
        preserve_computer_pause=True,
        preserve_browser_takeover=True,
        context="approval interrupt",
    )

    assert computer_closed == []
    assert browser_closed == [("thread-approval", True)]


def test_final_live_control_cleanup_preserves_matching_takeover_pause(
    monkeypatch,
) -> None:
    computer_closed: list[str] = []
    monkeypatch.setattr(
        "row_bot.computer_use.service.get_computer_use_service",
        lambda: SimpleNamespace(
            status_snapshot=lambda: {
                "active": True,
                "thread_id": "thread-takeover",
                "state": "waiting_user",
            },
            close_for_thread=computer_closed.append,
        ),
    )
    monkeypatch.setattr(
        "row_bot.tools.browser_tool.get_session_manager",
        lambda: SimpleNamespace(end_activity=lambda *_args, **_kwargs: None),
    )

    streaming._cleanup_live_control_sessions(
        "thread-takeover",
        preserve_computer_pause=True,
        context="takeover interrupt",
    )

    assert computer_closed == []


def test_final_live_control_cleanup_does_not_preserve_unrelated_computer_pause(
    monkeypatch,
) -> None:
    computer_closed: list[str] = []
    monkeypatch.setattr(
        "row_bot.computer_use.service.get_computer_use_service",
        lambda: SimpleNamespace(
            status_snapshot=lambda: {
                "active": True,
                "thread_id": "another-thread",
                "state": "waiting_approval",
            },
            close_for_thread=computer_closed.append,
        ),
    )
    monkeypatch.setattr(
        "row_bot.tools.browser_tool.get_session_manager",
        lambda: SimpleNamespace(end_activity=lambda *_args, **_kwargs: None),
    )

    streaming._cleanup_live_control_sessions(
        "thread-final",
        preserve_computer_pause=True,
        context="unrelated approval interrupt",
    )

    assert computer_closed == ["thread-final"]


def test_browser_live_picture_reuses_the_existing_one_per_generation_capture(
    monkeypatch,
) -> None:
    gen = _generation("thread-browser-preview")
    gen.browser_step_count = 1
    screenshots: list[str] = []
    manager = SimpleNamespace(
        has_active_session=lambda: True,
        take_screenshot=lambda thread_id: screenshots.append(thread_id) or b"png",
    )

    async def _io_bound(fn, *args):
        return fn(*args)

    monkeypatch.setattr(
        "row_bot.tools.browser_tool.get_session_manager",
        lambda: manager,
    )
    monkeypatch.setattr(streaming.run, "io_bound", _io_bound)

    asyncio.run(
        streaming._capture_balanced_browser_screenshot(gen, SimpleNamespace())
    )
    asyncio.run(
        streaming._capture_balanced_browser_screenshot(gen, SimpleNamespace())
    )

    assert screenshots == [gen.thread_id]
    assert gen.browser_preview_attempted is True
    assert gen.captured_images == []
