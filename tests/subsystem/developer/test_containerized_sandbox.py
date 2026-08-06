from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from row_bot.process_cancellation import ProcessRunResult
from tests.fixtures.developer import fake_workspace


pytestmark = pytest.mark.subsystem


def test_official_container_marker_is_strict() -> None:
    from row_bot.runtime_paths import is_containerized_runtime

    assert is_containerized_runtime({}) is False
    assert is_containerized_runtime({"ROW_BOT_CONTAINERIZED": "0"}) is False
    assert is_containerized_runtime({"ROW_BOT_CONTAINERIZED": "true"}) is False
    assert is_containerized_runtime({"ROW_BOT_CONTAINERIZED": " 1 "}) is False
    assert is_containerized_runtime({"ROW_BOT_CONTAINERIZED": "1"}) is True


def test_marker_fails_closed_before_resolver_or_runtime_probe(monkeypatch) -> None:
    from row_bot.developer import sandbox_runtime

    monkeypatch.setenv("ROW_BOT_CONTAINERIZED", "1")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("official container must not probe Docker or Podman")

    monkeypatch.setattr(sandbox_runtime, "resolve_docker", unexpected)
    monkeypatch.setattr(sandbox_runtime, "resolve_podman", unexpected)
    monkeypatch.setattr(sandbox_runtime.subprocess, "run", unexpected)

    probe = sandbox_runtime.detect_container_runtime()

    assert probe.available is False
    assert probe.binary == ""
    assert probe.message == sandbox_runtime.OFFICIAL_CONTAINER_SANDBOX_UNAVAILABLE


def test_unmarked_host_keeps_existing_docker_probe(monkeypatch) -> None:
    from row_bot.developer import sandbox_runtime

    monkeypatch.delenv("ROW_BOT_CONTAINERIZED", raising=False)
    monkeypatch.setattr(sandbox_runtime, "resolve_docker", lambda: "docker")
    monkeypatch.setattr(sandbox_runtime, "resolve_podman", lambda: None)
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        output = "Docker version 29.0" if argv == ["docker", "--version"] else "29.0"
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    monkeypatch.setattr(sandbox_runtime.subprocess, "run", fake_run)

    probe = sandbox_runtime.detect_container_runtime()

    assert probe.available is True
    assert calls == [
        ["docker", "--version"],
        ["docker", "info", "--format", "{{.ServerVersion}}"],
    ]


def test_docker_selection_is_rejected_without_rewriting_saved_mode(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot.developer import sandbox_runtime, storage

    workspace = fake_workspace(tmp_path, execution_mode="local")
    saved: list[object] = []
    monkeypatch.setenv("ROW_BOT_CONTAINERIZED", "1")
    monkeypatch.setattr(storage, "get_workspace", lambda _workspace_id: workspace)
    monkeypatch.setattr(storage, "save_workspace", lambda value: saved.append(value))

    with pytest.raises(
        ValueError,
        match="Developer Docker Sandbox is not available inside",
    ):
        storage.set_workspace_execution_settings(
            workspace.id,
            execution_mode="docker",
        )

    assert workspace.execution_mode == "local"
    assert saved == []
    assert sandbox_runtime.OFFICIAL_CONTAINER_SANDBOX_UNAVAILABLE


def test_retained_docker_workspace_reports_unavailable_without_probe(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot.developer import sandbox_runtime

    workspace = fake_workspace(tmp_path, execution_mode="docker")
    monkeypatch.setenv("ROW_BOT_CONTAINERIZED", "1")
    monkeypatch.setattr(sandbox_runtime, "list_sandbox_processes", lambda _id: [])

    def unexpected(*_args, **_kwargs):
        raise AssertionError("retained Docker mode must fail before probing")

    monkeypatch.setattr(sandbox_runtime, "resolve_docker", unexpected)
    monkeypatch.setattr(sandbox_runtime.subprocess, "run", unexpected)

    status = sandbox_runtime.get_docker_sandbox_status(workspace)

    assert status.available is False
    assert status.message == sandbox_runtime.OFFICIAL_CONTAINER_SANDBOX_UNAVAILABLE


def test_retained_docker_command_and_process_never_fall_back_locally(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot.developer import runtime

    workspace = fake_workspace(tmp_path, execution_mode="docker")
    monkeypatch.setenv("ROW_BOT_CONTAINERIZED", "1")
    monkeypatch.setattr(
        "row_bot.developer.storage.get_workspace",
        lambda _workspace_id: workspace,
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Docker-mode work must not execute locally")

    monkeypatch.setattr(runtime, "run_cancellable_subprocess", unexpected)
    monkeypatch.setattr(runtime.subprocess, "Popen", unexpected)

    command = runtime.run_workspace_command(
        workspace.path,
        "python -V",
        "allow_all",
        workspace_id=workspace.id,
        thread_id="thread-1",
    )
    process = runtime.start_workspace_process(
        workspace.path,
        "python -m http.server 8000",
        "allow_all",
        workspace_id=workspace.id,
        thread_id="thread-1",
    )

    assert command.ran is False
    assert process.ran is False
    assert command.execution_mode == "docker"
    assert process.execution_mode == "docker"
    assert "application container" in command.stderr
    assert "application container" in process.stderr


def test_missing_requested_workspace_fails_closed_for_every_run_path(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot.developer import runtime

    workspace = fake_workspace(tmp_path)
    monkeypatch.setattr(
        "row_bot.developer.storage.get_workspace",
        lambda _workspace_id: None,
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("missing workspace must not run locally")

    monkeypatch.setattr(runtime, "run_cancellable_subprocess", unexpected)
    monkeypatch.setattr(runtime.subprocess, "Popen", unexpected)

    direct = runtime.run_workspace_command(
        workspace.path,
        "python -V",
        "allow_all",
        workspace_id="missing",
    )
    shell = runtime.run_workspace_shell_command(
        workspace.path,
        "python -V",
        "allow_all",
        workspace_id="missing",
        thread_id="thread-1",
    )
    process = runtime.start_workspace_process(
        workspace.path,
        "python -m http.server 8000",
        "allow_all",
        workspace_id="missing",
    )

    for result in (direct, shell, process):
        assert result.ran is False
        assert result.decision is not None
        assert result.decision.decision == "block"
        assert "could not be resolved: missing" in result.stderr


def test_explicit_local_mode_still_runs_on_a_mounted_path(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot.developer import runtime

    workspace = fake_workspace(tmp_path, execution_mode="local")
    monkeypatch.setenv("ROW_BOT_CONTAINERIZED", "1")
    monkeypatch.setattr(
        "row_bot.developer.storage.get_workspace",
        lambda _workspace_id: workspace,
    )
    monkeypatch.setattr(
        runtime,
        "run_cancellable_subprocess",
        lambda argv, **_kwargs: ProcessRunResult(argv, 0, stdout="mounted\n"),
    )

    result = runtime.run_workspace_command(
        workspace.path,
        "python -V",
        "allow_all",
        workspace_id=workspace.id,
    )

    assert result.returncode == 0
    assert result.stdout == "mounted\n"
    assert result.execution_mode == "local"


def test_approved_custom_tool_uses_explicit_local_container_path_without_probe(
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot.developer import tool_capsules
    from row_bot.developer.runtime import CommandResult

    root = tmp_path / "tool"
    root.mkdir()
    tool = SimpleNamespace(
        id="tool-1",
        name="Fixture Tool",
        source_url="",
        installed_path=str(root),
        enabled=True,
    )
    monkeypatch.setenv("ROW_BOT_CONTAINERIZED", "1")
    monkeypatch.setattr(tool_capsules, "list_capsules", lambda: [tool])

    def unexpected_probe():
        raise AssertionError("container Custom Tool must not probe Docker")

    monkeypatch.setattr(tool_capsules, "detect_container_runtime", unexpected_probe)
    decisions: list[object] = []

    def fake_local(_tool, command, decision):
        decisions.append(decision)
        return CommandResult(
            command=command,
            cwd=str(root),
            returncode=0,
            execution_mode="local",
            decision=decision,
        )

    monkeypatch.setattr(tool_capsules, "_run_custom_tool_local_direct", fake_local)

    result = tool_capsules.run_custom_tool_test_command(
        tool.id,
        "curl https://example.invalid",
        approved_once=True,
        approval_mode="approve",
    )

    assert result.execution_mode == "local"
    assert result.returncode == 0
    assert decisions[0].allowed is True
    assert "inside the Row-Bot application container" in decisions[0].reason


def test_container_ui_copy_and_nested_runtime_actions_are_guarded() -> None:
    source = Path("src/row_bot/developer/ui.py").read_text(encoding="utf-8")

    assert "OFFICIAL_CONTAINER_SANDBOX_UNAVAILABLE" in source
    assert "deliberately runs this command" in source
    assert "rebuild_button.disable()" in source
    assert "cleanup_button.disable()" in source
    assert 'workspace_header["execution_mode_select"]' in source
