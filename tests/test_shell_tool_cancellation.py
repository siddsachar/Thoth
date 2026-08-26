from __future__ import annotations

import os

import pytest

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


@pytest.mark.skipif(not shell_tool._IS_WINDOWS, reason="PowerShell contract is Windows-only")
def test_powershell_error_record_forces_nonzero_even_after_later_success(tmp_path) -> None:
    session = shell_tool.ShellSession(str(tmp_path))

    result = session.run_command(
        "Write-Error 'synthetic failure'; Write-Output 'later success'"
    )

    assert result["exit_code"] != 0
    assert "synthetic failure" in result["output"]
    assert "later success" in result["output"]


@pytest.mark.skipif(not shell_tool._IS_WINDOWS, reason="PowerShell contract is Windows-only")
def test_powershell_success_warning_and_persistent_cwd_remain_successful(tmp_path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    escaped = str(nested).replace("'", "''")
    session = shell_tool.ShellSession(str(tmp_path))

    changed = session.run_command(
        f"Set-Location -LiteralPath '{escaped}'; Write-Warning 'notice'"
    )
    follow_up = session.run_command("Write-Output 'ready'")

    assert changed["exit_code"] == 0
    assert "notice" in changed["output"]
    assert os.path.normcase(changed["cwd"]) == os.path.normcase(str(nested))
    assert follow_up["exit_code"] == 0
    assert follow_up["output"] == "ready"
    assert os.path.normcase(follow_up["cwd"]) == os.path.normcase(str(nested))


@pytest.mark.skipif(not shell_tool._IS_WINDOWS, reason="PowerShell contract is Windows-only")
def test_powershell_preserves_nonzero_native_exit_code(tmp_path) -> None:
    result = shell_tool.ShellSession(str(tmp_path)).run_command("cmd /c exit 7")

    assert result["exit_code"] == 7
