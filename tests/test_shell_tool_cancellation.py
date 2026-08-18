from __future__ import annotations

from row_bot.process_cancellation import ProcessRunResult
from row_bot.tools import shell_tool


def test_shell_session_reports_cancelled_command(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        return ProcessRunResult(args, 130, stdout="partial out", stderr="partial err", cancelled=True)

    monkeypatch.setattr(shell_tool, "run_cancellable_subprocess", fake_run)
    session = shell_tool.ShellSession(str(tmp_path))

    result = session.run_command("echo hi")

    assert result["exit_code"] == 130
    assert "partial out" in result["output"]
    assert "partial err" in result["output"]
    assert "Command stopped by user." in result["output"]


def test_shell_session_releases_lock_after_background_launch_returns(
    monkeypatch,
    tmp_path,
) -> None:
    commands: list[str] = []

    def fake_run(args, **kwargs):
        commands.append(str(args[-1]))
        return ProcessRunResult(args, 0, stdout=f"result-{len(commands)}")

    monkeypatch.setattr(shell_tool, "run_cancellable_subprocess", fake_run)
    session = shell_tool.ShellSession(str(tmp_path))

    launch = session.run_command("Start-Process background-server")
    follow_up = session.run_command("echo ready")

    assert launch["exit_code"] == 0
    assert launch["output"] == "result-1"
    assert follow_up["exit_code"] == 0
    assert follow_up["output"] == "result-2"
    assert len(commands) == 2
