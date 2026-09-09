from __future__ import annotations

import argparse
import configparser
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class CommandSpec:
    name: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "argv": list(self.argv), "env": dict(self.env)}

    def display(self) -> str:
        return " ".join(self.argv)


def _cmd(name: str, *argv: str, env: dict[str, str] | None = None) -> CommandSpec:
    return CommandSpec(name=name, argv=tuple(argv), env=env or {})


TEST_ENV = {
    "ROW_BOT_DATA_DIR": str(
        Path(
            os.environ.get(
                "ROW_BOT_MATRIX_DATA_DIR",
                str(REPO_ROOT / ".tmp" / "matrix_row_bot"),
            )
        )
    ),
    "ROW_BOT_TEST_MODE": "1",
}

COVERAGE_ENV = {
    **TEST_ENV,
    "COVERAGE_FILE": str(REPO_ROOT / ".tmp" / "coverage" / ".coverage.migrated-subsystems"),
}

MIGRATED_COVERAGE_MODULES = (
    "row_bot.channels.base",
    "row_bot.channels.registry",
    "row_bot.mcp_client.runtime",
    "row_bot.mcp_client.safety",
    "row_bot.knowledge_graph",
    "row_bot.memory",
    "row_bot.memory_extraction",
    "row_bot.dream_cycle",
    "row_bot.developer.runtime",
    "row_bot.developer.sandbox",
    "row_bot.designer.export",
    "row_bot.providers.runtime",
    "row_bot.providers.selection",
    "row_bot.providers.catalog",
    "row_bot.tools.memory_tool",
    "row_bot.updater",
    "row_bot.plugins.api",
    "row_bot.plugins.loader",
    "row_bot.plugins.registry",
    "row_bot.plugins.installer",
    "row_bot.plugins.marketplace",
)

COVERAGE_SOURCE_DIR = Path("src/row_bot")
COVERAGE_CONFIG_PATH = Path(".tmp/coverage/migrated-subsystems.coveragerc")


def _write_migrated_coverage_config() -> Path:
    # Package-name discovery can import then evict dependency modules while
    # retaining their parent attributes. Directory discovery avoids that split;
    # the report still includes exactly the migrated inventory, including files
    # with no executed lines.
    config = configparser.ConfigParser()
    config["run"] = {"source": COVERAGE_SOURCE_DIR.as_posix()}
    config["report"] = {
        "include": "\n" + "\n".join(
            (Path("src") / Path(*module.split("."))).with_suffix(".py").as_posix()
            for module in MIGRATED_COVERAGE_MODULES
        ),
    }
    path = REPO_ROOT / COVERAGE_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        config.write(handle)
    return path


COMMANDS: dict[str, CommandSpec] = {
    "client-platform-boundaries": _cmd(
        "client-platform-boundaries", "uv", "run", "python", "scripts/check_client_platform_boundaries.py",
        env=TEST_ENV,
    ),
    "client-platform-contracts": _cmd(
        "client-platform-contracts", "uv", "run", "python", "scripts/generate_client_platform_contracts.py", "--check",
        env=TEST_ENV,
    ),
    "lock-check": _cmd("lock-check", "uv", "lock", "--check"),
    "requirements-check": _cmd("requirements-check", "python", "scripts/export_locked_requirements.py", "--check"),
    "sync-test": _cmd("sync-test", "uv", "sync", "--locked", "--all-extras", "--group", "test"),
    "runtime-deps": _cmd(
        "runtime-deps",
        "uv",
        "run",
        "python",
        "scripts/verify_runtime_dependencies.py",
        "all",
        env=TEST_ENV,
    ),
    "compileall": _cmd(
        "compileall",
        "uv",
        "run",
        "python",
        "-m",
        "compileall",
        "-q",
        "-x",
        r"(\.git|\.venv|dist|build|__pycache__)",
        "src",
        "tests",
        "scripts",
        "app.py",
        "debug_tools.py",
        "launcher.py",
        env=TEST_ENV,
    ),
    "ruff-safety": _cmd(
        "ruff-safety",
        "uv",
        "run",
        "--group",
        "lint",
        "ruff",
        "check",
        ".",
        "--select",
        "E9,F63,F7,F82",
        "--output-format=github",
    ),
    "contracts": _cmd(
        "contracts",
        "uv",
        "run",
        "python",
        "-m",
        "pytest",
        "tests/contracts",
        "-m",
        "not live_provider and not e2e",
        "-q",
        env=TEST_ENV,
    ),
    "subsystem": _cmd(
        "subsystem",
        "uv",
        "run",
        "python",
        "-m",
        "pytest",
        "tests/subsystem",
        "-m",
        "not live_provider and not e2e",
        "-q",
        env=TEST_ENV,
    ),
    "coverage-migrated": _cmd(
        "coverage-migrated",
        "uv",
        "run",
        "python",
        "-m",
        "pytest",
        "tests/contracts",
        "tests/subsystem",
        "-m",
        "not live_provider and not e2e",
        f"--cov={COVERAGE_SOURCE_DIR.as_posix()}",
        f"--cov-config={COVERAGE_CONFIG_PATH.as_posix()}",
        "--cov-report=term-missing:skip-covered",
        "--cov-report=xml:.tmp/coverage/migrated-subsystems.xml",
        "--cov-fail-under=55",
        "-q",
        env=COVERAGE_ENV,
    ),
    "deterministic": _cmd(
        "deterministic",
        "uv",
        "run",
        "python",
        "-m",
        "pytest",
        "tests",
        "-m",
        "not live_provider and not e2e",
        "-q",
        env=TEST_ENV,
    ),
    "app-smoke": _cmd(
        "app-smoke",
        "uv",
        "run",
        "python",
        "scripts/smoke_app.py",
        "--port",
        "8090",
        "--timeout",
        "120",
        env={**TEST_ENV, "ROW_BOT_AUTO_START_OLLAMA": "0"},
    ),
    "installer-contracts": _cmd(
        "installer-contracts",
        "uv",
        "run",
        "python",
        "-m",
        "pytest",
        "tests/subsystem/installer",
        "tests/contracts/installers",
        "-m",
        "not live_provider and not e2e",
        "-q",
        env=TEST_ENV,
    ),
    "legacy-inventory": _cmd(
        "legacy-inventory",
        "uv",
        "run",
        "python",
        "-m",
        "pytest",
        "tests/subsystem/test_legacy_inventory.py",
        "tests/subsystem/test_source_test_map.py",
        "-q",
        env=TEST_ENV,
    ),
}


TIER_COMMANDS: dict[str, tuple[str, ...]] = {
    "dependency-integrity": ("lock-check", "requirements-check", "sync-test", "runtime-deps"),
    "contract-subsystem": ("contracts", "subsystem"),
    "contracts": ("contracts",),
    "subsystem": ("subsystem",),
    "coverage": ("coverage-migrated",),
    "deterministic": ("deterministic",),
    "app-smoke": ("app-smoke",),
    "installer-contracts": ("installer-contracts",),
    "legacy-inventory": ("legacy-inventory",),
    "fast": ("ruff-safety", "client-platform-boundaries", "client-platform-contracts", "contracts", "subsystem", "legacy-inventory"),
    "pr": (
        "lock-check",
        "requirements-check",
        "sync-test",
        "runtime-deps",
        "compileall",
        "ruff-safety",
        "client-platform-boundaries",
        "client-platform-contracts",
        "contracts",
        "subsystem",
        "coverage-migrated",
        "deterministic",
        "installer-contracts",
        "app-smoke",
        "legacy-inventory",
    ),
    "release": (
        "lock-check",
        "requirements-check",
        "sync-test",
        "runtime-deps",
        "compileall",
        "ruff-safety",
        "client-platform-boundaries",
        "client-platform-contracts",
        "contracts",
        "subsystem",
        "coverage-migrated",
        "deterministic",
        "installer-contracts",
        "app-smoke",
        "legacy-inventory",
    ),
    "all": (
        "lock-check",
        "requirements-check",
        "sync-test",
        "runtime-deps",
        "compileall",
        "ruff-safety",
        "client-platform-boundaries",
        "client-platform-contracts",
        "contracts",
        "subsystem",
        "coverage-migrated",
        "deterministic",
        "installer-contracts",
        "app-smoke",
        "legacy-inventory",
    ),
}


def _dedupe_commands(names: tuple[str, ...]) -> list[CommandSpec]:
    specs: list[CommandSpec] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        specs.append(COMMANDS[name])
    return specs


def changed_files_from_git(base: str) -> list[str]:
    changed: list[str] = []

    def _append_names(argv: tuple[str, ...]) -> bool:
        result = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        for line in result.stdout.splitlines():
            name = line.strip()
            if name and name not in changed:
                changed.append(name)
        return True

    if not _append_names(("git", "diff", "--name-only", f"{base}...HEAD")):
        _append_names(("git", "diff", "--name-only", base))
    _append_names(("git", "diff", "--name-only", "HEAD"))
    _append_names(("git", "ls-files", "--others", "--exclude-standard"))
    return changed


def changed_commands(changed_files: list[str]) -> list[CommandSpec]:
    from tests.helpers.source_test_map import select_tests_for_changes

    selection = select_tests_for_changes(changed_files)
    commands: list[CommandSpec] = []
    if selection.test_paths:
        commands.append(
            _cmd(
                "changed-tests",
                "uv",
                "run",
                "python",
                "-m",
                "pytest",
                *selection.test_paths,
                "-m",
                "not live_provider and not e2e",
                "-q",
                env=TEST_ENV,
            )
        )
    commands.append(COMMANDS["legacy-inventory"])
    return commands


def commands_for_tier(tier: str, changed_files: list[str] | None = None) -> list[CommandSpec]:
    if tier == "changed":
        return changed_commands(changed_files or [])
    if tier not in TIER_COMMANDS:
        raise KeyError(f"unknown tier: {tier}")
    return _dedupe_commands(TIER_COMMANDS[tier])


def run_commands(commands: list[CommandSpec], *, continue_on_failure: bool) -> int:
    exit_code = 0
    for spec in commands:
        print(f":: {spec.name}: {spec.display()}", flush=True)
        if spec.name == "coverage-migrated":
            _write_migrated_coverage_config()
        env = {**os.environ, **spec.env}
        result = subprocess.run(spec.argv, cwd=REPO_ROOT, env=env, check=False)
        if result.returncode != 0:
            exit_code = result.returncode
            if not continue_on_failure:
                return exit_code
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Row-Bot's explicit local/CI test matrix tiers.")
    parser.add_argument(
        "tier",
        choices=sorted([*TIER_COMMANDS.keys(), "changed"]),
        help="Matrix tier to run.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument("--json-plan", action="store_true", help="Print the command plan as JSON.")
    parser.add_argument("--continue-on-failure", action="store_true", help="Run remaining commands after a failure.")
    parser.add_argument("--base", default="origin/main", help="Git base for the changed tier.")
    parser.add_argument("--changed-file", action="append", default=[], help="Changed file path for deterministic tests.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    changed_files = args.changed_file or (changed_files_from_git(args.base) if args.tier == "changed" else [])
    commands = commands_for_tier(args.tier, changed_files=changed_files)

    if args.json_plan:
        print(json.dumps({"tier": args.tier, "commands": [spec.as_dict() for spec in commands]}, indent=2))
    elif args.dry_run:
        for spec in commands:
            print(f":: {spec.name}: {spec.display()}")

    if args.dry_run:
        return 0
    return run_commands(commands, continue_on_failure=args.continue_on_failure)


if __name__ == "__main__":
    raise SystemExit(main())
