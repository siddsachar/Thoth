from __future__ import annotations

import importlib
import subprocess
import sys


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path / "data"))
    sys.modules.pop("developer.runtime", None)
    sys.modules.pop("developer.change_ledger", None)
    import developer.runtime as runtime

    return importlib.reload(runtime)


def test_developer_detects_project_commands(tmp_path, monkeypatch):
    runtime = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts": {"test": "vitest", "lint": "eslint .", "typecheck": "tsc"}}',
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    commands = runtime.detect_project_commands(str(repo))
    labels = {spec.label for spec in commands}

    assert "npm test" in labels
    assert "npm run lint" in labels
    assert "npm run typecheck" in labels
    assert "pytest" in labels


def test_developer_detects_package_manager_and_dev_server(tmp_path, monkeypatch):
    runtime = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"scripts": {"dev": "vite", "start": "vite --host", "test": "vitest"}}',
        encoding="utf-8",
    )

    commands = runtime.detect_project_commands(str(repo))
    by_command = {spec.command: spec for spec in commands}

    assert "pnpm test" in by_command
    assert by_command["pnpm run dev"].kind == "server"
    assert by_command["pnpm run start"].kind == "server"


def test_developer_runtime_policy_blocks_without_running(tmp_path, monkeypatch):
    runtime = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runtime.run_workspace_command(str(repo), "python --version", "read_only")

    assert result.ran is False
    assert result.decision.decision == "block"


def test_developer_runtime_runs_safe_command_in_workspace(tmp_path, monkeypatch):
    runtime = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runtime.run_workspace_command(
        str(repo),
        "python -c \"from pathlib import Path; print(Path.cwd().name)\"",
        "ask",
    )

    assert result.ran is True
    assert result.ok is True
    assert "repo" in result.stdout


def test_developer_runtime_requires_approval_for_install(tmp_path, monkeypatch):
    runtime = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runtime.run_workspace_command(str(repo), "python -m pip install sampleproject", "auto_edit")

    assert result.ran is False
    assert result.decision.decision == "ask"


def test_developer_shell_command_records_file_side_effects(tmp_path, monkeypatch):
    runtime = _fresh_modules(tmp_path, monkeypatch)
    from developer import change_ledger

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True, text=True)
    command = "python -c \"from pathlib import Path; Path('created.txt').write_text('hello\\n', encoding='utf-8')\""

    blocked = runtime.run_workspace_shell_command(
        str(repo),
        command,
        "auto_edit",
        workspace_id="ws-1",
        thread_id="thread-1",
    )
    assert blocked.ran is False
    assert blocked.decision.decision == "ask"

    result = runtime.run_workspace_shell_command(
        str(repo),
        command,
        "auto_edit",
        workspace_id="ws-1",
        thread_id="thread-1",
        confirmed=True,
    )

    assert result.ran is True
    assert result.ok is True
    assert "created.txt" in result.changed_files
    changes = change_ledger.list_change_sets(workspace_id="ws-1", thread_id="thread-1")
    assert changes
    assert changes[0].files[0].path == "created.txt"
