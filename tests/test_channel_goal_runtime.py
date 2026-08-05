from __future__ import annotations

import importlib
import sys
from pathlib import Path


def _fresh_channel_goal_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in (
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_runs",
        "row_bot.goals",
        "row_bot.channels.runtime",
    ):
        sys.modules.pop(name, None)

    import row_bot.tasks as tasks
    import row_bot.threads as threads
    import row_bot.agent_runs as agent_runs
    import row_bot.goals as goals
    import row_bot.channels.runtime as runtime

    tasks = importlib.reload(tasks)
    threads = importlib.reload(threads)
    agent_runs = importlib.reload(agent_runs)
    goals = importlib.reload(goals)
    runtime = importlib.reload(runtime)
    return threads, agent_runs, goals, runtime


def test_channel_goal_start_runs_initial_turn_and_continuation(tmp_path, monkeypatch):
    threads, _agent_runs, goals, runtime = _fresh_channel_goal_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Channel goal")

    start = runtime.prepare_channel_goal_start("/goal finish channel parity", thread_id)
    assert start is not None
    assert start.objective == "finish channel parity"
    assert start.prompt.startswith("[Goal mode started]")
    ack = runtime.format_goal_started_ack(start)
    assert "Goal started: finish channel parity" in ack
    assert "I'm working on it now" in ack
    assert "ask for approval" in ack
    assert runtime.prepare_channel_goal_start("/goal status", thread_id) is None
    assert runtime.prepare_channel_goal_start("/goal", thread_id) is None

    verdicts = [
        {"verdict": "continue", "reason": "needs another channel turn"},
        {"verdict": "complete", "reason": "channel evidence is enough"},
    ]
    monkeypatch.setattr(goals, "_verify_goal", lambda *_args, **_kw: verdicts.pop(0))

    prompts: list[str] = []
    sent: list[str] = []

    def _run_turn(prompt: str, _config: dict):
        prompts.append(prompt)
        return f"answer {len(prompts)}", None, [], []

    result = runtime.run_channel_goal_sync(
        channel_name="sms",
        thread_id=thread_id,
        config={"configurable": {"thread_id": thread_id}},
        first_prompt=start.prompt,
        run_turn=_run_turn,
        send_text=sent.append,
    )

    assert result.turns == 2
    assert result.status == "completed"
    assert sent == ["answer 1", "answer 2"]
    assert prompts[0].startswith("[Goal mode started]")
    assert prompts[1].startswith("[Goal continuation]")
    assert goals.get_current_goal(thread_id, include_terminal=True)["status"] == "completed"


def test_channel_goal_loop_stops_on_approval_interrupt(tmp_path, monkeypatch):
    threads, _agent_runs, goals, runtime = _fresh_channel_goal_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Channel approval goal")
    start = runtime.prepare_channel_goal_start("/goal request approval safely", thread_id)
    assert start is not None

    sent: list[str] = []
    interrupt = {"tool": "shell", "description": "Needs approval"}

    result = runtime.run_channel_goal_sync(
        channel_name="whatsapp",
        thread_id=thread_id,
        config={"configurable": {"thread_id": thread_id}},
        first_prompt=start.prompt,
        run_turn=lambda _prompt, _config: ("approval needed", interrupt, [], []),
        send_text=sent.append,
    )

    assert result.turns == 1
    assert result.status == "waiting_approval"
    assert result.interrupt_data == interrupt
    assert sent == ["approval needed"]
    assert goals.get_current_goal(thread_id)["status"] == "waiting_approval"


def test_channel_goal_delivers_real_progress_then_waits_for_unified_parent(
    tmp_path,
    monkeypatch,
):
    threads, _agent_runs, goals, runtime = _fresh_channel_goal_modules(tmp_path, monkeypatch)
    import row_bot.agent_orchestrator as orchestrator

    orchestrator = importlib.reload(orchestrator)
    thread_id = threads.create_thread("Channel child progress")
    generation_id = "channel-generation"
    config = {
        "configurable": {
            "thread_id": thread_id,
            "generation_id": generation_id,
        }
    }
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id=thread_id,
        parent_generation_id=generation_id,
        root_objective="Research the answer",
        model_ref="fake:model",
        approval_mode="block",
        runtime_surface="channel",
        orchestration_version=2,
    )
    orchestrator.arm_parent_wait(
        orchestration["id"],
        continuation_state={"config": config, "enabled_tool_names": ["agents"]},
    )
    monkeypatch.setattr(
        goals,
        "_verify_goal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("progress must not be evaluated as a finished Goal turn")
        ),
    )
    sent: list[str] = []

    result = runtime.run_channel_goal_sync(
        channel_name="sms",
        thread_id=thread_id,
        config=config,
        first_prompt="initial goal prompt",
        run_turn=lambda _prompt, _config: (
            "I delegated the research and will continue when it returns.",
            None,
        ),
        send_text=sent.append,
    )

    assert result.status == "waiting_children"
    assert sent == ["I delegated the research and will continue when it returns."]


def test_channel_goal_approval_grant_resumes_and_continues(tmp_path, monkeypatch):
    threads, _agent_runs, goals, runtime = _fresh_channel_goal_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Channel approval resume")
    start = runtime.prepare_channel_goal_start("/goal continue after approval", thread_id)
    assert start is not None
    goals.set_goal_status(
        start.goal["id"],
        "waiting_approval",
        reason="Waiting on user approval",
        verdict="paused",
    )

    assert runtime.resolve_goal_approval_for_config(
        {"configurable": {"thread_id": thread_id}},
        True,
    ) is True
    assert goals.get_current_goal(thread_id)["status"] == "active"

    verdicts = [
        {"verdict": "continue", "reason": "approval was granted"},
        {"verdict": "complete", "reason": "finished after approval"},
    ]
    monkeypatch.setattr(goals, "_verify_goal", lambda *_args, **_kw: verdicts.pop(0))
    sent: list[str] = []
    prompts: list[str] = []

    def _run_turn(prompt: str, _config: dict):
        prompts.append(prompt)
        return f"continued {len(prompts)}", None, [], []

    result = runtime.continue_channel_goal_after_turn_sync(
        channel_name="sms",
        thread_id=thread_id,
        config={"configurable": {"thread_id": thread_id}},
        assistant_text="approval was granted",
        interrupt_data=None,
        run_turn=_run_turn,
        send_text=sent.append,
    )

    assert result.status == "completed"
    assert sent == ["continued 1"]
    assert prompts and prompts[0].startswith("[Goal continuation]")
    assert goals.get_current_goal(thread_id, include_terminal=True)["status"] == "completed"


def test_channel_goal_approval_denial_blocks_goal(tmp_path, monkeypatch):
    threads, _agent_runs, goals, runtime = _fresh_channel_goal_modules(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Channel approval denial")
    start = runtime.prepare_channel_goal_start("/goal stop after denial", thread_id)
    assert start is not None
    goals.set_goal_status(
        start.goal["id"],
        "waiting_approval",
        reason="Waiting on user approval",
        verdict="paused",
    )

    assert runtime.resolve_goal_approval_for_config(
        {"configurable": {"thread_id": thread_id}},
        False,
    ) is True
    blocked = goals.get_current_goal(thread_id, include_terminal=True)
    assert blocked["status"] == "blocked"
    assert "denied" in blocked["last_reason"].lower()


def test_channel_adapters_use_shared_goal_runtime():
    root = Path("src/row_bot/channels")
    runtime_src = (root / "runtime.py").read_text(encoding="utf-8")
    assert "def prepare_channel_goal_start" in runtime_src
    assert "def format_goal_started_ack" in runtime_src
    assert "def run_channel_goal_sync" in runtime_src
    assert "async def run_channel_goal_async" in runtime_src
    assert "def resolve_goal_approval_for_config" in runtime_src
    assert "def continue_channel_goal_after_turn_sync" in runtime_src
    assert "async def continue_channel_goal_after_turn_async" in runtime_src

    for filename in ("sms.py", "whatsapp.py"):
        src = (root / filename).read_text(encoding="utf-8")
        assert "finalize_channel_orchestration(" not in src
        assert "prepare_channel_goal_start" in src
        assert "format_goal_started_ack" in src
        assert "run_channel_goal_sync" in src
        assert src.index("format_goal_started_ack") < src.index("run_channel_goal_sync")
        assert "resolve_goal_approval_for_config" in src
        assert "continue_channel_goal_after_turn_sync" in src
        assert "ch_commands.dispatch" in src
        assert src.index("prepare_channel_goal_start") < src.index("ch_commands.dispatch")

    for filename in ("telegram.py", "slack.py", "discord_channel.py"):
        src = (root / filename).read_text(encoding="utf-8")
        assert "finalize_channel_orchestration(" not in src
        assert "prepare_channel_goal_start" in src
        assert "format_goal_started_ack" in src
        assert "run_channel_goal_async" in src
        assert src.index("format_goal_started_ack") < src.index("run_channel_goal_async")
        assert "resolve_goal_approval_for_config" in src
        assert "continue_channel_goal_after_turn_async" in src
        assert "ch_commands.dispatch" in src or "cmd_goal" in src
