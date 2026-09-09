"""Contested audit barriers run against the production runtime/domain owners."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from row_bot.cancellation import CancellationScope, use_cancellation_scope
from row_bot.runtime.executions import GenerationRuntimeRegistry
from tests.fixtures.tasks import fresh_tasks_module


def test_f_r01_preset_cancel_drains_callbacks_once_and_unblocks_transport():
    event = threading.Event()
    scope = CancellationScope(event)
    released = threading.Event()
    calls = []
    scope.register(lambda: (calls.append("first"), released.set()))
    event.set()
    assert scope.cancel()
    assert released.is_set()
    assert not scope.cancel()
    scope.register(lambda: calls.append("late"))
    assert calls == ["first", "late"]


def test_f_r01_failing_callback_does_not_suppress_others():
    scope = CancellationScope()
    scope.register(lambda: (_ for _ in ()).throw(RuntimeError("private fixture detail")))
    released = threading.Event()
    scope.register(released.set)
    scope.cancel()
    assert released.is_set()
    assert scope.cleanup_failures == ["cleanup_failed"]


def test_f_r03_completion_during_readiness_check_cannot_lose_wake(monkeypatch):
    from row_bot import agent_orchestrator as owner
    calls = []
    def ready(run_id):
        calls.append(run_id)
        if len(calls) == 1:
            owner._DEPENDENCY_EVENTS[run_id].set()
            return False
        return True
    monkeypatch.setattr(owner, "dependencies_ready", ready)
    assert owner.wait_for_dependencies("fixture-run", threading.Event())
    assert len(calls) == 2
    assert "fixture-run" not in owner._DEPENDENCY_EVENTS


def test_f_r03_cancel_waiter_unregisters_without_capacity(monkeypatch):
    from row_bot import agent_orchestrator as owner
    entered = threading.Event()
    scope = CancellationScope()
    monkeypatch.setattr(owner, "dependencies_ready", lambda _run: (entered.set(), False)[1])
    results = []
    def wait():
        with use_cancellation_scope(scope):
            results.append(owner.wait_for_dependencies("fixture-wait", scope.stop_event))
    worker = threading.Thread(target=wait)
    worker.start()
    assert entered.wait(2)
    scope.cancel()
    worker.join(2)
    assert results == [False]
    assert "fixture-wait" not in owner._DEPENDENCY_EVENTS


@pytest.mark.parametrize("action,expected", [("pause_goal", "paused"), ("clear_goal", "cleared")])
def test_f_r04_user_transition_wins_delayed_verifier(tmp_path, monkeypatch, action, expected):
    fresh_tasks_module(tmp_path, monkeypatch)
    from row_bot import goals
    goal = goals.start_goal("fixture-goal", "Keep synthetic state", max_turns=10)
    def verifier(_goal, _context):
        getattr(goals, action)("fixture-goal")
        return {"verdict": "complete", "reason": "stale completion"}
    result = goals.after_turn(thread_id="fixture-goal", turn_id="turn-a", verifier=verifier)
    assert not result.should_continue
    assert goals.get_goal(goal["id"])["status"] == expected


def test_f_r04_logical_turn_a_b_a_claims_once(tmp_path, monkeypatch):
    fresh_tasks_module(tmp_path, monkeypatch)
    from row_bot import goals
    goal = goals.start_goal("fixture-turns", "Process synthetic turns", max_turns=10)
    assert goals._claim_turn("fixture-turns", "A")
    assert goals._claim_turn("fixture-turns", "B")
    assert goals._claim_turn("fixture-turns", "A") is None
    assert goals.get_goal(goal["id"])["turns_used"] == 2


def test_f_r06_seven_equal_text_ids_only_consumed_batch_settles(tmp_path, monkeypatch):
    fresh_tasks_module(tmp_path, monkeypatch)
    from row_bot import agent_runs
    agent_runs.create_agent_run(run_id="fixture-steering", thread_id="fixture-child")
    for i in range(7):
        agent_runs.append_agent_parent_message("fixture-steering", "identical guidance", message_id=f"steer-{i}")
    first = agent_runs.pending_parent_message_records("fixture-steering")
    assert [item["id"] for item in first] == [f"steer-{i}" for i in range(5)]
    # A process loss before acknowledgement keeps the same exact batch visible.
    assert agent_runs.pending_parent_message_records("fixture-steering") == first
    agent_runs.acknowledge_parent_messages("fixture-steering", [item["id"] for item in first])
    assert [item["id"] for item in agent_runs.pending_parent_message_records("fixture-steering")] == ["steer-5", "steer-6"]


@pytest.mark.parametrize("mode", ["focused", "recent", "full"])
def test_f_r07_final_child_packet_is_bounded_and_mission_preserved(monkeypatch, mode):
    from row_bot import agent_context
    monkeypatch.setattr(agent_context, "load_parent_context", lambda *a, **k: {
        "summary": "large" * 10000, "recent": "history" * 10000,
        "full": "history" * 10000, "message_count": 3})
    packet = agent_context.build_child_agent_prompt(objective="Required mission", context="extra" * 10000,
        context_mode=mode, profile_snapshot={"instructions": "Mandatory policy", "context_policy_json": {"max_context_tokens": 400}})
    assert int(packet["estimated_tokens"]) <= 400
    assert "Required mission" in packet["prompt"] and "Mandatory policy" in packet["prompt"]


def test_f_r07_impossible_mandatory_envelope_fails_closed():
    from row_bot.agent_context import AgentContextError, build_child_agent_prompt
    with pytest.raises(AgentContextError, match="mandatory child packet"):
        build_child_agent_prompt(objective="required", context_mode="empty",
            profile_snapshot={"context_policy_json": {"max_context_tokens": 1}})


def test_f_r08_writer_a_waiters_leave_capacity_for_resource_b(monkeypatch):
    from row_bot import agent_runner, agent_runs, agent_settings
    monkeypatch.setattr(agent_runner, "_DISPATCH_QUEUE", [])
    monkeypatch.setattr(agent_runner, "_DISPATCH_ACTIVE", {})
    monkeypatch.setattr(agent_runner, "_DISPATCH_WRITER_KEYS", {})
    monkeypatch.setattr(agent_settings, "load_agent_runtime_settings", lambda: SimpleNamespace(max_concurrent_children=8, max_active_children_global=2))
    locks = {}
    queued = threading.Event()
    monkeypatch.setattr(agent_runs, "get_agent_write_lock", lambda key: locks.get(key))
    def acquire(key, run_id, **kwargs):
        if key in locks:
            return False
        locks[key] = run_id
        return True
    monkeypatch.setattr(agent_runs, "acquire_agent_write_lock", acquire)
    monkeypatch.setattr(agent_runs, "update_agent_status", lambda *args: queued.set())
    assert agent_runner._acquire_child_capacity("A1", "parent", threading.Event(), write_lock_key="A")
    stop = threading.Event()
    results = []
    waiter = threading.Thread(target=lambda: results.append(agent_runner._acquire_child_capacity("A2", "parent", stop, write_lock_key="A")))
    waiter.start()
    assert queued.wait(2)
    assert agent_runner._acquire_child_capacity("B1", "parent", threading.Event(), write_lock_key="B")
    assert set(agent_runner._DISPATCH_ACTIVE) == {"A1", "B1"}
    stop.set()
    with agent_runner._DISPATCH_CONDITION:
        agent_runner._DISPATCH_CONDITION.notify_all()
    waiter.join(2)
    assert results == [False]


def test_f_r09_deadline_and_stop_retain_owner_until_producer_acknowledges():
    now = [0.0]
    registry = GenerationRuntimeRegistry(clock=lambda: now[0])
    handle = registry.register("fixture-execution", deadline=10.0)
    now[0] = 11
    with pytest.raises(InterruptedError):
        registry.check_dispatch(handle)
    assert registry.active("fixture-execution") == (handle,)
    assert handle.status == "stopping" and not handle.producer_done.is_set()
    registry.finish(handle)
    assert not registry.active("fixture-execution")
    assert handle.status == "stopped"
