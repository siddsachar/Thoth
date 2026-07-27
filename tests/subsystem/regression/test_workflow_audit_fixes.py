from __future__ import annotations

from pathlib import Path

import pytest

from tests.fixtures.tasks import fresh_tasks_module


pytestmark = pytest.mark.subsystem

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_workflow_audit_source_contracts_are_wired() -> None:
    tasks_source = (REPO_ROOT / "src" / "row_bot" / "tasks.py").read_text(encoding="utf-8")
    shell_source = (REPO_ROOT / "src" / "row_bot" / "tools" / "shell_tool.py").read_text(encoding="utf-8")
    telegram_source = (REPO_ROOT / "src" / "row_bot" / "channels" / "telegram.py").read_text(encoding="utf-8")
    sidebar_source = (REPO_ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    command_center_source = (REPO_ROOT / "src" / "row_bot" / "ui" / "command_center.py").read_text(encoding="utf-8")

    run_task_background = tasks_source[tasks_source.index("def run_task_background"):][:30_000]
    resume_start = tasks_source.index("def _resume_graph_interrupted")
    resume_end = tasks_source.index("\ndef ", resume_start + 4)
    resume_graph = tasks_source[resume_start:resume_end]
    subtask_sync = tasks_source[tasks_source.index("def _run_subtask_sync"):][:5_000]
    deliver_channels = tasks_source[tasks_source.index("def _deliver_to_channels"):][:2_000]

    assert 'approval_mode == "block"' in run_task_background
    assert 'approval_mode == "allow_all"' in run_task_background
    assert "resume_invoke_agent" in run_task_background
    assert "not interrupts" in run_task_background
    assert "_stop_event.is_set()" in run_task_background
    assert "isinstance(result, dict)" in subtask_sync
    assert "cannot surface approval" in subtask_sync.lower()
    assert "_clear_graph_interrupted(" in resume_graph
    assert 'approval_mode == "block"' in resume_graph
    assert 'approval_mode == "allow_all"' in resume_graph
    assert "checkpoint" in resume_graph.lower()
    assert "no target configured" in deliver_channels
    assert "def _strip_quoted" in shell_source
    assert "classify_command(line" in shell_source or "classify_command(line," in shell_source
    assert "_PENDING_TTL_SECONDS" in telegram_source
    assert "def _cleanup_stale_pending" in telegram_source
    assert telegram_source.count("with _pending_lock:") >= 6
    assert "Already handled" in sidebar_source or "Already handled" in command_center_source


def test_shell_command_classification_regressions() -> None:
    from row_bot.tools.shell_tool import _strip_quoted, classify_command

    assert classify_command('echo "hello > world"') == "safe"
    assert classify_command("echo 'hello | world'") == "safe"
    assert classify_command("echo hello > /tmp/out") == "needs_approval"
    assert classify_command("ls | grep foo") == "needs_approval"
    assert classify_command("echo safe\nrm -rf /") == "blocked"
    assert classify_command("ls\npwd\nwhoami") == "safe"
    assert classify_command("ls\npip install foo") == "needs_approval"
    assert ">" not in _strip_quoted('echo "hello \\" > world"')
    assert isinstance(_strip_quoted('echo "unterminated'), str)
    assert _strip_quoted("") == ""
    assert _strip_quoted("ls -la") == "ls -la"


def test_delete_task_and_finish_run_clean_up_pipeline_state(tmp_path, monkeypatch) -> None:
    tasks = fresh_tasks_module(tmp_path, monkeypatch)

    task_id = tasks.create_task(name="cleanup test", prompts=["test"], apply_default_skills=False)
    conn = tasks._get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO pipeline_state "
        "(run_id, task_id, thread_id, current_step_index, step_outputs, status, config, created_at, updated_at) "
        "VALUES (?, ?, 'thread', 0, '{}', 'paused', '{}', datetime('now'), datetime('now'))",
        ("run-cleanup", task_id),
    )
    conn.execute(
        "INSERT OR REPLACE INTO approval_requests "
        "(id, run_id, task_id, step_id, resume_token, message, status, requested_at) "
        "VALUES ('request-cleanup', 'run-cleanup', ?, 'step_1', 'token-cleanup', 'test', 'pending', datetime('now'))",
        (task_id,),
    )
    conn.commit()
    conn.close()

    tasks.delete_task(task_id)

    conn = tasks._get_conn()
    pipeline_state = conn.execute("SELECT * FROM pipeline_state WHERE task_id = ?", (task_id,)).fetchone()
    approval = conn.execute("SELECT * FROM approval_requests WHERE id = 'request-cleanup'").fetchone()
    conn.close()
    assert pipeline_state is None
    assert approval is not None
    assert dict(approval)["status"] == "cancelled"

    task_id_2 = tasks.create_task(name="finish cleanup test", prompts=["test"], apply_default_skills=False)
    run_id = "run-finish-cleanup"
    conn = tasks._get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO task_runs (id, task_id, thread_id, started_at, status) "
        "VALUES (?, ?, 'thread-2', datetime('now'), 'running')",
        (run_id, task_id_2),
    )
    conn.execute(
        "INSERT OR REPLACE INTO pipeline_state "
        "(run_id, task_id, thread_id, current_step_index, step_outputs, status, config, created_at, updated_at) "
        "VALUES (?, ?, 'thread-2', 0, '{}', 'running', '{}', datetime('now'), datetime('now'))",
        (run_id, task_id_2),
    )
    conn.commit()
    conn.close()

    tasks._finish_run(run_id, "completed", "done")

    conn = tasks._get_conn()
    finished_state = conn.execute("SELECT * FROM pipeline_state WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    assert finished_state is None


def test_get_task_channels_distinguishes_none_from_empty_list(tmp_path, monkeypatch) -> None:
    tasks = fresh_tasks_module(tmp_path, monkeypatch)
    from row_bot.channels import registry

    class RunningChannel:
        name = "slack"
        display_name = "Slack"

        def is_running(self) -> bool:
            return True

    registry._reset()
    registry.register(RunningChannel())
    tasks.set_workflow_default_channels(["slack", "not-running"])

    default_task = tasks.create_task(name="default channels", prompts=["test"], apply_default_skills=False)
    no_delivery_task = tasks.create_task(
        name="no channels",
        prompts=["test"],
        channels=[],
        apply_default_skills=False,
    )

    inherited_channels = tasks.get_task_channels(tasks.get_task(default_task))
    assert [channel.name for channel in inherited_channels] == ["slack"]
    assert tasks.get_effective_task_channel_names(tasks.get_task(default_task)) == ["slack", "not-running"]
    assert tasks.get_task_channels(tasks.get_task(no_delivery_task)) == []
    registry._reset()
