from __future__ import annotations

import concurrent.futures

import pytest

from row_bot.computer_use.service import (
    ComputerUseError,
    ComputerUseService,
    LeaseBusyError,
    LeaseOwner,
    SessionState,
)


OWNER_A = LeaseOwner("thread-a", "generation-a", "task-a")
OWNER_B = LeaseOwner("thread-b", "generation-b", "task-b")


def test_global_lease_covers_discovery_and_contention(service) -> None:
    service.acquire(OWNER_A, validate_context=False)
    assert [app["name"] for app in service.list_apps(OWNER_A)] == ["Calculator", "Notepad"]
    with pytest.raises(LeaseBusyError):
        service.acquire(OWNER_B, validate_context=False)
    service.stop()
    service.acquire(OWNER_B, validate_context=False)


def test_takeover_retains_paused_lease_until_stop(service) -> None:
    service.acquire(OWNER_A, validate_context=False)
    service.take_over()
    assert service.status_snapshot()["state"] == SessionState.WAITING_USER.value
    with pytest.raises(LeaseBusyError):
        service.acquire(OWNER_B, validate_context=False)
    service.stop()
    service.acquire(OWNER_B, validate_context=False)


def test_resume_after_takeover_starts_new_driver_and_captures_first(service) -> None:
    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Calculator")[0]["target_id"]
    first = service.capture(target_id, OWNER_A)
    takeover_token = service.take_over()
    resumed = service.resume(OWNER_A, takeover_token=takeover_token)
    assert resumed.generation > first.generation
    assert service.status_snapshot()["state"] == SessionState.OBSERVING.value


def test_takeover_token_is_exact_one_time_and_never_switches_target(
    service,
    fake_transport,
) -> None:
    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Notepad")[0]["target_id"]
    first = service.capture(target_id, OWNER_A)
    token = service.take_over(
        thread_id=OWNER_A.thread_id,
        generation_id=OWNER_A.generation_id,
    )
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError, match="token is stale"):
        service.resume(OWNER_A, takeover_token="wrong-token")
    resumed = service.resume(OWNER_A, takeover_token=token)

    assert resumed.target.pid == first.target.pid
    assert resumed.target.window_id == first.target.window_id
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "start_session",
        "list_windows",
        "get_window_state",
        "list_windows",
    ]
    with pytest.raises(ComputerUseError, match="No paused Computer session"):
        service.resume(OWNER_A, takeover_token=token)


def test_resumed_tool_call_consumes_fresh_capture_without_replaying_mutation(
    service,
    fake_transport,
) -> None:
    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Notepad")[0]["target_id"]
    service.capture(target_id, OWNER_A)
    signature = ("drag", target_id, 0, 0, 0, 0)
    service.begin_tool_call(signature)
    token = service.take_over()
    service.end_tool_call(signature)
    calls_before = len(fake_transport.calls)

    service.resume(OWNER_A, takeover_token=token)

    assert service.resumed_call_matches(signature)
    resumed = service.consume_resumed_call(signature)
    assert resumed.target.target_id == target_id
    assert not service.resumed_call_matches(signature)
    assert "drag" not in [name for name, _args in fake_transport.calls[calls_before:]]


def test_resume_target_identity_drift_is_terminal_and_releases_lease(
    service,
    fake_transport,
) -> None:
    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Notepad")[0]["target_id"]
    service.capture(target_id, OWNER_A)
    token = service.take_over()
    fake_transport.scenario.capture_pid = 9999

    with pytest.raises(ComputerUseError, match="identity changed"):
        service.resume(OWNER_A, takeover_token=token)

    assert service.status_snapshot()["active"] is False
    service.acquire(OWNER_B, validate_context=False)


def test_resume_rejects_similar_replacement_when_capture_echoes_requested_identity(
    service,
    fake_transport,
) -> None:
    """Model the real Cua response that omits independent capture identity."""

    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Notepad")[0]["target_id"]
    service.capture(target_id, OWNER_A)
    token = service.take_over()
    fake_transport.scenario.windows = (
        {
            "window_id": 909,
            "pid": 9909,
            "app_name": "Notepad",
            "title": "Untitled - Notepad",
            "bounds": {"x": 20, "y": 20, "width": 900, "height": 700},
            "is_on_screen": True,
        },
    )
    calls_before = len(fake_transport.calls)

    with pytest.raises(ComputerUseError, match="identity changed"):
        service.resume(OWNER_A, takeover_token=token)

    resume_calls = fake_transport.calls[calls_before:]
    assert [name for name, _args in resume_calls].count("list_windows") == 1
    assert "get_window_state" not in [name for name, _args in resume_calls]
    assert "type_text" not in [name for name, _args in resume_calls]
    assert service.status_snapshot()["active"] is False
    service.acquire(OWNER_B, validate_context=False)


def test_live_control_events_cover_approval_takeover_resume_and_cleanup(service) -> None:
    states: list[str] = []
    service.add_listener(lambda snapshot: states.append(str(snapshot["state"])))
    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Calculator")[0]["target_id"]
    service.capture(target_id, OWNER_A)
    takeover_token = service.take_over()
    service.resume(OWNER_A, takeover_token=takeover_token)
    service.stop()

    assert states[0] == SessionState.OBSERVING.value
    assert SessionState.WAITING_APPROVAL.value in states
    assert SessionState.WAITING_USER.value in states
    assert states[-1] == SessionState.READY.value
    assert service.status_snapshot()["active"] is False


def test_status_and_preview_reads_add_zero_driver_or_capture_calls(service) -> None:
    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Calculator")[0]["target_id"]
    service.capture(target_id, OWNER_A)
    before = service.performance_snapshot()

    for _ in range(10):
        service.status_snapshot()
        service.current_observation(target_id)
        service.ephemeral_screenshot()

    after = service.performance_snapshot()
    assert after["driver_calls"] == before["driver_calls"]
    assert after["captures"] == before["captures"]


def test_new_lease_never_shows_previous_session_action(service) -> None:
    service.acquire(OWNER_A, validate_context=False)
    service.launch_app("Calculator", OWNER_A)
    assert service.status_snapshot()["last_action"] == "launch app"

    service.stop()
    assert service.status_snapshot()["last_action"] == ""
    service.acquire(OWNER_B, validate_context=False)
    assert service.status_snapshot()["last_action"] == ""
    assert service.status_snapshot()["action_count"] == 0


def test_new_generation_never_inherits_prior_unresolved_completion(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {
            "role": "Edit",
            "label": "Document field",
            "value": "old",
            "enabled": True,
        },
    )
    fake_transport.scenario.set_value_updates_document = False
    fake_transport.scenario.delivery_profile = "catalyst_value_unavailable"
    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Calculator")[0]["target_id"]
    observation = service.capture(target_id, OWNER_A)
    service.act(
        "replace_text",
        target_id,
        OWNER_A,
        element_token=observation.elements[0].token,
        text="private replacement",
    )

    service.stop()
    service.acquire(OWNER_B, validate_context=False)

    assert service.computer_use_completion_blocked(OWNER_B) is False
    assert service.status_snapshot()["computer_use_completion_blocked"] is False
    assert service.computer_use_completion_blocked(
        thread_id=OWNER_A.thread_id,
        generation_id=OWNER_A.generation_id,
    ) is True


def test_takeover_clears_private_pending_attempt_but_not_completion_evidence(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {
            "role": "Edit",
            "label": "Native Editor",
            "value": None,
            "enabled": True,
        },
    )
    fake_transport.scenario.delivery_profile = "catalyst_value_unavailable"
    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Calculator")[0]["target_id"]
    observation = service.capture(target_id, OWNER_A)
    service.act(
        "replace_text",
        target_id,
        OWNER_A,
        element_token=observation.elements[0].token,
        text="private unresolved value",
    )

    assert service._pending_mutation is not None
    before = service.completion_evidence_statuses(OWNER_A)
    service.take_over()

    assert service._pending_mutation is None
    assert service.completion_evidence_statuses(OWNER_A) == before
    assert before[-1]["status"] == "unresolved"


def test_different_explicit_replacement_supersedes_unresolved_attempt(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.semantic_elements = (
        {
            "role": "Edit",
            "label": "Native Editor",
            "value": None,
            "enabled": True,
        },
    )
    fake_transport.scenario.delivery_profile = "catalyst_value_unavailable"
    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Calculator")[0]["target_id"]
    observation = service.capture(target_id, OWNER_A)
    first = service.act(
        "replace_text",
        target_id,
        OWNER_A,
        element_token=observation.elements[0].token,
        text="first private value",
    )

    fake_transport.scenario.delivery_profile = "native_targeted_insertion"
    second = service.act(
        "replace_text",
        target_id,
        OWNER_A,
        element_token=first.elements[0].token,
        text="different private value",
    )

    statuses = service.completion_evidence_statuses(OWNER_A)
    assert [entry["status"] for entry in statuses[-2:]] == [
        "superseded",
        "verified",
    ]
    assert second.outcome == "verified"
    assert service.computer_use_completion_blocked(OWNER_A) is False
    assert "first private value" not in repr(statuses)
    assert "different private value" not in repr(statuses)


def test_app_approval_denial_is_terminal_and_clears_live_control(fake_client) -> None:
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: False,
    )
    states: list[dict] = []
    service.add_listener(lambda snapshot: states.append(dict(snapshot)))
    service.acquire(OWNER_A, validate_context=False)

    with pytest.raises(ComputerUseError, match="not approved"):
        service.launch_app("Calculator", OWNER_A)

    snapshot = service.status_snapshot()
    assert snapshot["active"] is False
    assert snapshot["state"] == SessionState.READY.value
    assert snapshot["has_thumbnail"] is False
    assert states[-1]["active"] is False
    assert states[-1]["state"] == SessionState.READY.value


def test_resume_recaptures_only_the_previously_selected_same_app_target(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.windows = (
        {
            "window_id": 901,
            "pid": 9001,
            "app_name": "python.exe",
            "title": "Row-Bot",
            "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
            "is_on_screen": True,
        },
        {
            "window_id": 202,
            "pid": 4343,
            "app_name": "Notepad",
            "title": "TARGET B - Notepad",
            "bounds": {"x": 20, "y": 20, "width": 900, "height": 700},
            "is_on_screen": True,
        },
        {
            "window_id": 203,
            "pid": 4344,
            "app_name": "Notepad",
            "title": "TARGET A - Notepad",
            "bounds": {"x": 40, "y": 40, "width": 900, "height": 700},
            "is_on_screen": True,
        },
    )
    service.acquire(OWNER_A, validate_context=False)
    matches = service.list_windows(
        OWNER_A,
        app="Notepad",
        window_hint="TARGET A",
    )
    assert len(matches) == 1
    selected = matches[0]["target_id"]
    service.capture(selected, OWNER_A)
    takeover_token = service.take_over()

    service.resume(OWNER_A, takeover_token=takeover_token)

    capture_args = [args for name, args in fake_transport.calls if name == "get_window_state"]
    assert capture_args[-1]["pid"] == 4344
    assert capture_args[-1]["window_id"] == 203
    assert service.status_snapshot()["app"] == "Notepad"


def test_resume_without_a_previously_captured_target_releases_the_lease(service) -> None:
    service.acquire(OWNER_A, validate_context=False)
    assert service.list_windows(OWNER_A, app="Notepad")
    takeover_token = service.take_over()

    with pytest.raises(ComputerUseError, match="paused session was released"):
        service.resume(OWNER_A, takeover_token=takeover_token)

    assert service.status_snapshot()["active"] is False
    assert service.status_snapshot()["state"] == SessionState.READY.value


def test_wait_preserves_owned_notepad_target_without_reacquiring_or_polling_driver(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    import row_bot.computer_use.service as service_module

    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Notepad")[0]["target_id"]
    first = service.capture(target_id, OWNER_A)
    calls_before = len(fake_transport.calls)
    captures_before = service.performance_snapshot()["captures"]
    clock = [100.0]
    calls_seen_during_wait: list[int] = []

    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock[0])

    def _advance(seconds: float) -> None:
        calls_seen_during_wait.append(len(fake_transport.calls))
        clock[0] += seconds

    monkeypatch.setattr(service_module.time, "sleep", _advance)

    verified = service.wait_and_capture(target_id, 8_000, OWNER_A)

    assert verified.target.target_id == first.target.target_id
    assert verified.target.app_name == "Notepad"
    assert all(count == calls_before for count in calls_seen_during_wait)
    assert [name for name, _args in fake_transport.calls[calls_before:]] == [
        "get_window_state"
    ]
    assert service.performance_snapshot()["captures"] == captures_before + 1
    assert service.status_snapshot()["thread_id"] == OWNER_A.thread_id


def test_takeover_interrupts_generic_wait_and_retains_paused_lease(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    import row_bot.computer_use.service as service_module

    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Notepad")[0]["target_id"]
    service.capture(target_id, OWNER_A)
    calls_before = len(fake_transport.calls)
    clock = [200.0]
    took_over = [False]

    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock[0])

    def _take_over(seconds: float) -> None:
        clock[0] += seconds
        if not took_over[0]:
            took_over[0] = True
            service.take_over()

    monkeypatch.setattr(service_module.time, "sleep", _take_over)

    with pytest.raises(concurrent.futures.CancelledError):
        service.wait_and_capture(target_id, 10_000, OWNER_A)

    assert [name for name, _args in fake_transport.calls[calls_before:]] == []
    assert service.status_snapshot()["state"] == SessionState.WAITING_USER.value
    with pytest.raises(LeaseBusyError):
        service.acquire(OWNER_B, validate_context=False)


def test_stop_interrupts_generic_wait_and_releases_lease(
    service,
    fake_transport,
    monkeypatch,
) -> None:
    import row_bot.computer_use.service as service_module

    service.acquire(OWNER_A, validate_context=False)
    target_id = service.list_windows(OWNER_A, app="Notepad")[0]["target_id"]
    service.capture(target_id, OWNER_A)
    calls_before = len(fake_transport.calls)
    clock = [300.0]
    stopped = [False]

    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock[0])

    def _stop(seconds: float) -> None:
        clock[0] += seconds
        if not stopped[0]:
            stopped[0] = True
            service.stop()

    monkeypatch.setattr(service_module.time, "sleep", _stop)

    with pytest.raises(concurrent.futures.CancelledError):
        service.wait_and_capture(target_id, 10_000, OWNER_A)

    assert [name for name, _args in fake_transport.calls[calls_before:]] == []
    assert service.status_snapshot()["active"] is False
    service.acquire(OWNER_B, validate_context=False)
