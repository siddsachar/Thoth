from __future__ import annotations

import importlib
import sys


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path / "data"))
    sys.modules.pop("developer.runtime", None)
    import developer.runtime as runtime

    return importlib.reload(runtime)


def test_developer_runtime_splits_commands_without_shell(tmp_path, monkeypatch):
    runtime = _fresh_modules(tmp_path, monkeypatch)

    args = runtime.split_command('python -c "print(123)"')

    assert args == ["python", "-c", "print(123)"]


def test_developer_runtime_requires_approval_for_shell_control(tmp_path, monkeypatch):
    runtime = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runtime.run_workspace_command(str(repo), "python --version && python --version", "agent_run")

    assert result.ran is False
    assert result.decision.decision == "ask"


def test_developer_runtime_tracks_and_stops_workspace_processes(tmp_path, monkeypatch):
    runtime = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runtime.start_workspace_process(str(repo), 'python -c "import time; time.sleep(30)"', "agent_run")
    stopped = runtime.stop_workspace_processes(str(repo))

    assert result.ran is True
    assert "Started PID" in result.stdout
    assert stopped == 1

