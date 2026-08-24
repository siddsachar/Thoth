from __future__ import annotations

import subprocess

import pytest

import scripts.run_test_matrix as matrix


pytestmark = [pytest.mark.subsystem, pytest.mark.installer]


def test_pr_tier_contains_required_deterministic_lanes() -> None:
    names = [spec.name for spec in matrix.commands_for_tier("pr")]

    assert "lock-check" in names
    assert "runtime-deps" in names
    assert "contracts" in names
    assert "subsystem" in names
    assert "coverage-migrated" in names
    assert "deterministic" in names
    assert "installer-contracts" in names
    assert "app-smoke" in names
    assert "legacy-inventory" in names
    assert "legacy-test-suite" not in names


def test_coverage_tier_enforces_migrated_subsystem_baseline() -> None:
    coverage = matrix.COMMANDS["coverage-migrated"]
    threshold_arg = next(arg for arg in coverage.argv if arg.startswith("--cov-fail-under="))
    selected_modules = {arg.removeprefix("--cov=") for arg in coverage.argv if arg.startswith("--cov=")}

    assert int(threshold_arg.split("=", 1)[1]) >= 45
    assert "--cov-fail-under=55" in coverage.argv
    assert "--cov-report=xml:.tmp/coverage/migrated-subsystems.xml" in coverage.argv
    assert "--cov=row_bot.knowledge_graph" in coverage.argv
    assert {
        "row_bot.providers.runtime",
        "row_bot.providers.selection",
        "row_bot.providers.catalog",
        "row_bot.tools.memory_tool",
        "row_bot.updater",
    } <= selected_modules
    assert {
        "row_bot.plugins.api",
        "row_bot.plugins.loader",
        "row_bot.plugins.registry",
        "row_bot.plugins.installer",
        "row_bot.plugins.marketplace",
    } <= selected_modules
    assert not any(module.startswith("row_bot.skills_hub") for module in selected_modules)
    assert coverage.env["COVERAGE_FILE"].endswith(".coverage.migrated-subsystems")


def test_release_tier_matches_pr_preflight_lanes() -> None:
    assert [spec.name for spec in matrix.commands_for_tier("release")] == [
        spec.name for spec in matrix.commands_for_tier("pr")
    ]


def test_changed_tier_expands_source_test_map() -> None:
    specs = matrix.commands_for_tier("changed", changed_files=["src/row_bot/providers/runtime.py"])

    changed = next(spec for spec in specs if spec.name == "changed-tests")
    assert "tests/contracts/test_provider_contract.py" in changed.argv
    assert "tests/subsystem/providers" in changed.argv
    assert changed.env["ROW_BOT_TEST_MODE"] == "1"


def test_changed_files_include_committed_worktree_and_untracked_changes(monkeypatch) -> None:
    outputs = {
        ("git", "diff", "--name-only", "origin/main...HEAD"): "committed.py\nshared.py\n",
        ("git", "diff", "--name-only", "HEAD"): "working.py\nshared.py\n",
        ("git", "ls-files", "--others", "--exclude-standard"): "untracked.py\n",
    }

    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=outputs[tuple(argv)], stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert matrix.changed_files_from_git("origin/main") == [
        "committed.py",
        "shared.py",
        "working.py",
        "untracked.py",
    ]


def test_dry_run_main_does_not_execute(monkeypatch, capsys) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("dry-run should not execute commands"))

    assert matrix.main(["contracts", "--dry-run"]) == 0
    assert "tests/contracts" in capsys.readouterr().out


def test_run_commands_stops_on_first_failure(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(subprocess, "run", fake_run)

    code = matrix.run_commands([matrix.COMMANDS["contracts"], matrix.COMMANDS["subsystem"]], continue_on_failure=False)

    assert code == 7
    assert len(calls) == 1
