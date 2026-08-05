from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


def _fresh_goal_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in (
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_profiles",
        "row_bot.agent_runs",
        "row_bot.agent_orchestrator",
        "row_bot.goals",
        "row_bot.slash_commands",
        "row_bot.channels.commands",
        "row_bot.tools.goal_tool",
    ):
        sys.modules.pop(name, None)

    import row_bot.tasks as tasks
    import row_bot.threads as threads
    import row_bot.agent_runs as agent_runs
    import row_bot.goals as goals
    import row_bot.slash_commands as slash_commands
    import row_bot.tools.goal_tool as goal_tool

    commands = importlib.import_module("row_bot.channels.commands")
    tasks = importlib.reload(tasks)
    threads = importlib.reload(threads)
    agent_runs = importlib.reload(agent_runs)
    goals = importlib.reload(goals)
    slash_commands = importlib.reload(slash_commands)
    commands = importlib.reload(commands)
    goal_tool = importlib.reload(goal_tool)
    return threads, agent_runs, goals, slash_commands, commands, goal_tool


def test_goal_slash_lifecycle_and_channel_scope(tmp_path, monkeypatch):
    threads, agent_runs, goals, slash_commands, commands, _goal_tool = _fresh_goal_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Goal slash")

    specs = {spec.id: spec for spec in slash_commands.get_command_specs(include_skills=False)}
    assert specs["goal"].handler_key == "goal"
    assert slash_commands.resolve_command_text("/goal ship it")[0].id == "goal"

    response = slash_commands.dispatch_text_command(thread_id, "/goal ship a small feature")
    assert response and "Goal started" in response
    goal = goals.get_current_goal(thread_id)
    assert goal is not None
    assert goal["status"] == "active"
    assert goal["objective"] == "ship a small feature"
    assert goal["turns_used"] == 0
    assert goal["active_run_id"].startswith("goal-")
    run = agent_runs.get_agent_run(goal["active_run_id"])
    assert run["kind"] == "goal"
    assert run["thread_id"] == thread_id
    assert run["goal_id"] == goal["id"]

    assert commands.is_thread_scoped_command("/goal status")
    assert "could not identify" in commands.dispatch("sms", "/goal status").lower()
    channel_status = commands.dispatch("sms", "/goal status", thread_id=thread_id)
    assert channel_status and "ship a small feature" in channel_status

    paused = slash_commands.dispatch_text_command(thread_id, "/goal pause")
    assert paused and "Goal paused" in paused
    assert goals.get_current_goal(thread_id)["status"] == "paused"

    resumed = slash_commands.dispatch_text_command(thread_id, "/goal resume")
    assert resumed and "Goal resumed" in resumed
    assert goals.get_current_goal(thread_id)["status"] == "active"

    goals.set_goal_status(
        goals.get_current_goal(thread_id)["id"],
        "waiting_approval",
        reason="Waiting on approval",
        verdict="paused",
    )
    resumed_from_approval = slash_commands.dispatch_text_command(thread_id, "/goal resume")
    assert resumed_from_approval and "Goal resumed" in resumed_from_approval
    assert goals.get_current_goal(thread_id)["status"] == "active"

    completed = slash_commands.dispatch_text_command(thread_id, "/goal done tested")
    assert completed and "marked complete" in completed.lower()
    assert goals.get_current_goal(thread_id, include_terminal=True)["status"] == "completed"
    assert slash_commands.dispatch_text_command(thread_id, "/goal resume") == "No paused goal to resume."

    cleared = slash_commands.dispatch_text_command(thread_id, "/goal clear")
    assert cleared and "Goal cleared" in cleared
    assert goals.get_current_goal(thread_id, include_terminal=True) is None


def test_goal_tool_registers_and_updates_current_goal(tmp_path, monkeypatch):
    threads, _agent_runs, goals, _slash, _commands, goal_tool = _fresh_goal_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Goal tool")
    goals.start_goal(thread_id, "finish the tool test")

    from row_bot.tools import registry

    registered = registry.get_tool("goal")
    assert registered is not None
    assert {tool.name for tool in registered.as_langchain_tools()} == {"goal_update", "goal_status"}

    payload = json.loads(goal_tool._goal_update(
        thread_id=thread_id,
        progress="Added tests",
        evidence=["tests/test_goal_mode.py"],
        next_step="Run pytest",
    ))
    assert payload["ok"] is True
    assert payload["goal"]["last_progress"] == "Added tests"
    assert payload["goal"]["evidence"] == ["tests/test_goal_mode.py"]

    status_payload = json.loads(goal_tool._goal_status(thread_id=thread_id))
    assert status_payload["ok"] is True
    assert status_payload["goal"]["objective"] == "finish the tool test"


def test_goal_after_turn_uses_same_model_verifier_and_claims_once(tmp_path, monkeypatch):
    threads, agent_runs, goals, _slash, _commands, _goal_tool = _fresh_goal_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Goal verifier")
    started_goal = goals.start_goal(thread_id, "keep going")

    import row_bot.models as models

    called = {}

    class FakeLLM:
        def invoke(self, messages):
            called["messages"] = messages
            return type("Response", (), {"content": '{"verdict":"continue","reason":"not done yet"}'})()

    def fake_get_llm_for(ref):
        called["model_ref"] = ref
        return FakeLLM()

    def fake_get_llm():
        called["default_model"] = True
        return FakeLLM()

    monkeypatch.setattr(models, "get_current_model", lambda: "local:base")
    monkeypatch.setattr(models, "is_model_local", lambda ref: ref == "local:target")
    monkeypatch.setattr(models, "is_cloud_model", lambda ref: False)
    monkeypatch.setattr(models, "get_llm_for", fake_get_llm_for)
    monkeypatch.setattr(models, "get_llm", fake_get_llm)

    decision = goals.after_turn(
        thread_id=thread_id,
        turn_id="turn-1",
        assistant_text="I made partial progress.",
        model_override="local:target",
    )

    assert decision.should_continue is True
    assert decision.status == "active"
    assert called["model_ref"] == "local:target"
    assert "not done yet" in goals.get_current_goal(thread_id)["last_reason"]
    events = agent_runs.get_agent_events(started_goal["active_run_id"])
    assert "goal.continuation_requested" in {event["type"] for event in events}

    duplicate = goals.after_turn(
        thread_id=thread_id,
        turn_id="turn-1",
        assistant_text="duplicate",
        verifier=lambda _goal, _context: {"verdict": "continue", "reason": "duplicate"},
    )
    assert duplicate.should_continue is False
    assert goals.get_current_goal(thread_id)["turns_used"] == 1


def test_goal_verifier_failure_falls_back_to_continue(tmp_path, monkeypatch):
    threads, _agent_runs, goals, _slash, _commands, _goal_tool = _fresh_goal_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Goal fallback")
    goals.start_goal(thread_id, "survive verifier failure")

    def failing_verifier(_goal, _context):
        raise RuntimeError("network down")

    decision = goals.after_turn(
        thread_id=thread_id,
        turn_id="turn-1",
        assistant_text="Still working.",
        verifier=failing_verifier,
    )

    current = goals.get_current_goal(thread_id)
    assert decision.should_continue is True
    assert current["verifier_failures"] == 1
    assert "Verifier unavailable" in current["last_reason"]


def test_goal_verifier_receives_child_agent_dependency_evidence(tmp_path, monkeypatch):
    threads, agent_runs, goals, _slash, _commands, _goal_tool = _fresh_goal_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Goal child evidence")
    goals.start_goal(thread_id, "finish delegated work")
    agent_runs.create_agent_run(
        run_id="child-complete",
        kind="subagent",
        status="completed",
        parent_thread_id=thread_id,
        thread_id="child-thread",
        display_name="Child verifier",
        summary="Child completed the delegated verification.",
    )

    captured = {}

    def verifier(_goal, context):
        captured["dependencies"] = context.get("child_agent_dependencies")
        return {"verdict": "complete", "reason": "child evidence proves completion"}

    decision = goals.after_turn(
        thread_id=thread_id,
        turn_id="turn-child-complete",
        assistant_text="I delegated verification.",
        verifier=verifier,
    )

    dependencies = captured["dependencies"]
    assert dependencies[0]["id"] == "child-complete"
    assert dependencies[0]["status"] == "completed"
    assert "delegated verification" in dependencies[0]["summary"]
    assert decision.status == "completed"
    assert goals.get_current_goal(thread_id, include_terminal=True)["status"] == "completed"


def test_goal_verifier_sees_unfinished_or_failed_child_agents(tmp_path, monkeypatch):
    threads, agent_runs, goals, _slash, _commands, _goal_tool = _fresh_goal_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Goal child pending")
    goals.start_goal(thread_id, "wait for delegated work")
    agent_runs.create_agent_run(
        run_id="child-running",
        kind="subagent",
        status="running",
        parent_thread_id=thread_id,
        thread_id="child-running-thread",
        display_name="Running child",
        status_message="Still working",
    )
    agent_runs.create_agent_run(
        run_id="child-failed",
        kind="subagent",
        status="failed",
        parent_thread_id=thread_id,
        thread_id="child-failed-thread",
        display_name="Failed child",
        error="Could not finish",
    )

    def verifier(_goal, context):
        statuses = {item["id"]: item["status"] for item in context.get("child_agent_dependencies") or []}
        assert statuses["child-running"] == "running"
        assert statuses["child-failed"] == "failed"
        return {"verdict": "continue", "reason": "child agents are not complete"}

    decision = goals.after_turn(
        thread_id=thread_id,
        turn_id="turn-child-running",
        assistant_text="Children are still unresolved.",
        verifier=verifier,
    )

    assert decision.should_continue is True
    assert goals.get_current_goal(thread_id)["status"] == "active"


def test_repeated_same_blocker_marks_goal_blocked(tmp_path, monkeypatch):
    threads, _agent_runs, goals, _slash, _commands, _goal_tool = _fresh_goal_modules(
        tmp_path,
        monkeypatch,
    )
    thread_id = threads.create_thread("Goal blocker")
    goals.start_goal(thread_id, "finish without credentials")

    verifier = lambda _goal, _context: {"verdict": "continue", "reason": "needs more work"}
    for index in range(3):
        goals.update_goal_progress(
            thread_id=thread_id,
            status="active",
            progress="Still trying",
            blockers=["waiting for API token"],
        )
        decision = goals.after_turn(
            thread_id=thread_id,
            turn_id=f"turn-{index}",
            assistant_text="Blocked on token.",
            verifier=verifier,
        )

    final_goal = goals.get_current_goal(thread_id, include_terminal=True)
    assert final_goal["status"] == "blocked"
    assert final_goal["blocker_count"] == 3
    assert decision.should_continue is False
    assert decision.status == "blocked"


def test_goal_continuation_is_posted_to_shared_parent_runner(tmp_path, monkeypatch):
    threads, _agent_runs, goals, _slash, _commands, _goal_tool = _fresh_goal_modules(
        tmp_path,
        monkeypatch,
    )
    import row_bot.agent_orchestrator as orchestrator

    thread_id = threads.create_thread("Goal shared runner")
    goal = goals.start_goal(thread_id, "finish after child evidence")
    captured: list[dict] = []
    monkeypatch.setattr(
        goals,
        "after_turn",
        lambda **_kwargs: goals.GoalContinuationDecision(
            goal=goal,
            should_continue=True,
            continuation_prompt="[Goal continuation] Verify the child evidence.",
            reason="one more check",
            status="active",
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "get_orchestration",
        lambda _orchestration_id: {
            "root_objective": goal["objective"],
            "model_ref": "fake:model",
            "approval_mode": "block",
            "continuation_state_json": {
                "config": {"configurable": {"thread_id": thread_id}},
                "enabled_tool_names": ["agents", "goal"],
            },
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "start_parent_event_turn",
        lambda **kwargs: captured.append(kwargs) or {"id": "goal-parent-turn"},
    )

    goals.after_orchestration_completion(
        thread_id,
        "previous-orchestration",
        "The first child result is ready.",
    )

    assert len(captured) == 1
    assert captured[0]["runtime_surface"] == "goal"
    assert captured[0]["event_kind"] == "goal_continuation"
    assert captured[0]["parent_thread_id"] == thread_id
    assert captured[0]["enabled_tool_names"] == ["agents", "goal"]
    assert captured[0]["event_payload"]["goal_id"] == goal["id"]


def test_goal_streaming_and_ui_contracts_are_wired():
    streaming = Path("src/row_bot/ui/streaming.py").read_text(encoding="utf-8")
    chat = Path("src/row_bot/ui/chat.py").read_text(encoding="utf-8")
    goal_ui = Path("src/row_bot/ui/goal_ui.py").read_text(encoding="utf-8")
    composer = Path("src/row_bot/ui/chat_composer_extras.py").read_text(encoding="utf-8")
    guide = Path("tool_guides/goal_guide/SKILL.md").read_text(encoding="utf-8")

    assert "goals.is_goal_start_argument" in streaming
    assert "goals.build_initial_goal_prompt" in streaming
    assert "goals.after_turn" in streaming
    assert "goal_continuation_prompt" in streaming
    assert "internal_goal_continuation" in streaming
    assert "internal_goal_continuation=True" in streaming
    assert "not queued_visible_user_msg and not internal_goal_continuation" in streaming
    assert "build_goal_progress_panel" in chat
    assert "def build_goal_progress_panel" in goal_ui
    assert "goals.get_current_goal" in goal_ui
    assert "internal_goal_continuation=True" in goal_ui
    assert 'spec.handler_key == "goal"' in composer
    assert "name: goal_guide" in guide
    assert "goal_update" in guide
