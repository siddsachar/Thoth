from __future__ import annotations

import sys

import pytest

from row_bot.process_cancellation import ProcessRunResult
from tests.fixtures.developer import fake_workspace


pytestmark = pytest.mark.subsystem


def _py_command(code: str) -> str:
    escaped = code.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{sys.executable}" -c "{escaped}"'


def test_run_workspace_command_captures_stdout_stderr_nonzero_and_timeout(tmp_path) -> None:
    from row_bot.developer import runtime

    workspace = fake_workspace(tmp_path)

    ok = runtime.run_workspace_command(workspace.path, _py_command("print('ok')"), "allow_all")
    assert ok.ran is True
    assert ok.ok is True
    assert ok.stdout.strip() == "ok"

    failed = runtime.run_workspace_command(
        workspace.path,
        _py_command("import sys; sys.stderr.write('bad'); sys.exit(3)"),
        "allow_all",
    )
    assert failed.returncode == 3
    assert failed.stderr == "bad"

    timed_out = runtime.run_workspace_command(
        workspace.path,
        _py_command("import time; time.sleep(2)"),
        "allow_all",
        timeout=1,
    )
    assert timed_out.returncode == 124
    assert "timed out" in timed_out.stderr


def test_run_workspace_command_blocks_unapproved_and_invalid_workspaces(tmp_path) -> None:
    from row_bot.developer import runtime

    workspace = fake_workspace(tmp_path)

    blocked = runtime.run_workspace_command(workspace.path, "git push origin HEAD", "approve")

    assert blocked.ran is False
    assert blocked.decision is not None
    assert blocked.decision.requires_approval is True

    with pytest.raises(ValueError):
        runtime.run_workspace_command(str(tmp_path / "missing"), "python -V", "allow_all")


def test_docker_network_policy_blocks_install_and_allows_safe_actions(tmp_path) -> None:
    from row_bot.developer import runtime
    from row_bot.developer.sandbox import ApprovalDecision

    workspace = fake_workspace(tmp_path, execution_mode="docker", sandbox_network="off")
    allowed = ApprovalDecision("allow", "ok")

    blocked = runtime._apply_docker_network_policy(workspace, "run_install", allowed)
    safe = runtime._apply_docker_network_policy(workspace, "run_safe_command", allowed)

    assert blocked.allowed is False
    assert "Docker Sandbox network is Off" in blocked.reason
    assert safe is allowed


def test_shell_command_uses_platform_shell_args_and_records_changed_files(monkeypatch, tmp_path) -> None:
    from row_bot.developer import change_ledger, runtime

    workspace = fake_workspace(tmp_path)
    snapshots = iter([{}, {"created.txt": "new"}])
    recorded = []

    monkeypatch.setattr(
        "row_bot.developer.storage.get_workspace",
        lambda _workspace_id: workspace,
    )
    monkeypatch.setattr(runtime, "_snapshot_changed_files", lambda _root: next(snapshots))
    monkeypatch.setattr(runtime, "_head_text", lambda _root, _path: None)
    monkeypatch.setattr(change_ledger, "record_change_set", lambda **kwargs: recorded.append(kwargs))

    def fake_run(argv, **kwargs):
        assert argv == runtime._platform_shell_args("echo hi")
        assert kwargs["cwd"] == workspace.path
        return ProcessRunResult(argv, 0, stdout="hi\n", stderr="")

    monkeypatch.setattr(runtime, "run_cancellable_subprocess", fake_run)

    result = runtime.run_workspace_shell_command(
        workspace.path,
        "echo hi",
        "allow_all",
        workspace_id=workspace.id,
        thread_id="thread-1",
    )

    assert result.returncode == 0
    assert result.changed_files == ["created.txt"]
    assert recorded[0]["workspace_id"] == workspace.id
    assert recorded[0]["files"][0].action == "create"


def test_shell_command_timeout_returns_structured_result(monkeypatch, tmp_path) -> None:
    from row_bot.developer import runtime

    workspace = fake_workspace(tmp_path)
    monkeypatch.setattr(
        "row_bot.developer.storage.get_workspace",
        lambda _workspace_id: workspace,
    )
    monkeypatch.setattr(runtime, "_snapshot_changed_files", lambda _root: {})

    def fake_run(argv, **_kwargs):
        return ProcessRunResult(argv, 124, stdout="partial", timed_out=True)

    monkeypatch.setattr(runtime, "run_cancellable_subprocess", fake_run)

    result = runtime.run_workspace_shell_command(
        workspace.path,
        "sleep 10",
        "allow_all",
        workspace_id=workspace.id,
        thread_id="thread-1",
        timeout=1,
    )

    assert result.returncode == 124
    assert result.stdout == "partial"
    assert "timed out" in result.stderr


def test_managed_background_process_starts_and_stops_cleanly(tmp_path) -> None:
    from row_bot.developer import runtime

    workspace = fake_workspace(tmp_path)
    command = _py_command("import time; time.sleep(30)")

    result = runtime.start_workspace_process(workspace.path, command, "allow_all")
    try:
        assert result.returncode == 0
        assert "Started PID" in result.stdout
        assert runtime.stop_workspace_processes(workspace.path) == 1
        assert runtime.stop_workspace_processes(workspace.path) == 0
    finally:
        runtime.stop_workspace_processes(workspace.path)
