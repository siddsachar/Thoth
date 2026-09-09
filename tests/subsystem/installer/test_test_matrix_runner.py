from __future__ import annotations

import configparser
from pathlib import Path
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

import pytest

import scripts.run_test_matrix as matrix


pytestmark = [pytest.mark.subsystem, pytest.mark.installer]


def test_pr_tier_contains_required_deterministic_lanes() -> None:
    names = [spec.name for spec in matrix.commands_for_tier("pr")]

    assert "lock-check" in names
    assert "runtime-deps" in names
    assert "contracts" in names
    assert "client-platform-boundaries" in names
    assert "client-platform-contracts" in names
    assert "client-foundation" in names
    assert "dependency-requirements" in names
    assert "subsystem" in names
    assert "coverage-migrated" in names
    assert "deterministic" in names
    assert "installer-contracts" in names
    assert "app-smoke" in names
    assert "legacy-inventory" in names
    assert "legacy-test-suite" not in names


def test_coverage_tier_enforces_migrated_subsystem_baseline(tmp_path, monkeypatch) -> None:
    coverage = matrix.COMMANDS["coverage-migrated"]
    threshold_arg = next(arg for arg in coverage.argv if arg.startswith("--cov-fail-under="))
    selected_modules = set(matrix.MIGRATED_COVERAGE_MODULES)

    assert int(threshold_arg.split("=", 1)[1]) >= 45
    assert "--cov-fail-under=55" in coverage.argv
    assert "--cov-report=xml:.tmp/coverage/migrated-subsystems.xml" in coverage.argv
    assert "--cov=src/row_bot" in coverage.argv
    assert "--cov-config=.tmp/coverage/migrated-subsystems.coveragerc" in coverage.argv
    assert "row_bot.knowledge_graph" in selected_modules
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
    monkeypatch.setattr(matrix, "REPO_ROOT", tmp_path)
    config = configparser.ConfigParser()
    config.read(matrix._write_migrated_coverage_config(), encoding="utf-8")
    assert config["run"]["source"] == "src/row_bot"
    assert set(config["report"]["include"].split()) == {
        "src/" + module.replace(".", "/") + ".py" for module in selected_modules
    }
    assert len(selected_modules) == 21


def test_coverage_discovery_preserves_imports_and_counts_unexecuted_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(matrix, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(matrix, "MIGRATED_COVERAGE_MODULES", ("row_bot.covered", "row_bot.uncovered"))
    package = tmp_path / "src" / "row_bot"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from pathlib import Path\nPath('discovery-imported').write_text('unexpected')\n", encoding="utf-8"
    )
    for name in ("covered", "uncovered", "excluded"):
        (package / f"{name}.py").write_text("value = 1\n", encoding="utf-8")
    config = matrix._write_migrated_coverage_config()
    script = textwrap.dedent("""
        import coverage
        import runpy
        import sys
        cov = coverage.Coverage(config_file=sys.argv[1], data_file=sys.argv[2])
        cov.start()
        try:
            runpy.run_path('src/row_bot/covered.py')
            runpy.run_path('src/row_bot/excluded.py')
        finally:
            cov.stop()
        cov.xml_report(outfile='coverage.xml')
    """)
    subprocess.run(
        [sys.executable, "-c", script, str(config), str(tmp_path / ".coverage")],
        cwd=tmp_path, check=True, capture_output=True, text=True, timeout=30,
    )
    assert not (tmp_path / "discovery-imported").exists()
    classes = {Path(node.attrib["filename"]).name: node for node in ET.parse(tmp_path / "coverage.xml").findall(".//class")}
    assert set(classes) == {"covered.py", "uncovered.py"}
    assert float(classes["covered.py"].attrib["line-rate"]) == 1
    assert float(classes["uncovered.py"].attrib["line-rate"]) == 0


def test_coverage_dry_run_does_not_write_generated_configuration(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(matrix, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("dry-run should not execute commands"))
    assert matrix.main(["coverage", "--dry-run"]) == 0
    assert "--cov=src/row_bot" in capsys.readouterr().out
    assert not (tmp_path / matrix.COVERAGE_CONFIG_PATH).exists()


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


def test_changed_frontend_selects_node_checks_and_backend_contracts() -> None:
    specs = matrix.commands_for_tier("changed", changed_files=["frontend/src/api/http.ts"])
    assert "client-foundation" in [spec.name for spec in specs]
    changed = next(spec for spec in specs if spec.name == "changed-tests")
    assert "tests/subsystem/client_host" in changed.argv
    assert "tests/subsystem/client_protocol" in changed.argv


def test_client_checks_never_install_and_fail_fast(tmp_path, monkeypatch) -> None:
    import scripts.run_client_checks as client

    frontend = tmp_path / "frontend"
    compiler = frontend / "node_modules/typescript/bin/tsc"
    compiler.parent.mkdir(parents=True)
    compiler.touch()
    monkeypatch.setattr(client, "FRONTEND", frontend)
    monkeypatch.setattr(client.shutil, "which", lambda _name: "fixture-node")
    monkeypatch.setenv("VITE_ENABLE_FIXTURES", "1")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://invalid.example")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(client.subprocess, "run", run)
    assert client.main([]) == 7
    assert len(calls) == 1
    assert "VITE_ENABLE_FIXTURES" not in calls[0][1]["env"]
    assert not any(key.startswith("OTEL_") for key in calls[0][1]["env"])
    assert not any("install" in argument or "npm" in argument for command in client.check_commands() for argument in command)
    assert len(client.check_commands(True)) < len(client.check_commands())


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
