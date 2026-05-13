from __future__ import annotations

import json
import os
import pathlib
import shlex
import subprocess
from dataclasses import dataclass, field

from developer import change_ledger
from developer.change_ledger import FileChange
from developer.sandbox import ApprovalDecision, decide_action
from developer.state import ApprovalMode


@dataclass(frozen=True)
class CommandSpec:
    label: str
    command: str
    kind: str = "test"


@dataclass(frozen=True)
class CommandResult:
    command: str
    cwd: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    decision: ApprovalDecision | None = None
    changed_files: list[str] = field(default_factory=list)
    execution_mode: str = "local"
    sandbox_backend: str = ""
    sandbox_pending_change_id: str = ""

    @property
    def ran(self) -> bool:
        return self.returncode is not None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class ManagedProcess:
    workspace_path: str
    command: str
    pid: int


_SHELL_CONTROL_OPERATORS = ("&&", "||", "|", ">", "<")
_ACTIVE_PROCESSES: dict[str, list[subprocess.Popen]] = {}


def split_command(command: str) -> list[str]:
    """Split a simple command without invoking the platform shell."""
    return shlex.split(command, posix=True)


def _unquoted_text(command: str) -> str:
    chars: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            if not in_single and not in_double:
                chars.append(char)
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single and not in_double:
            chars.append(char)
    return "".join(chars)


def has_shell_control_operator(command: str) -> bool:
    text = _unquoted_text(command)
    return any(operator in text for operator in _SHELL_CONTROL_OPERATORS)


def detect_project_commands(workspace_path: str) -> list[CommandSpec]:
    root = pathlib.Path(workspace_path).expanduser()
    commands: list[CommandSpec] = []
    if (root / "package.json").exists():
        try:
            data = json.loads((root / "package.json").read_text(encoding="utf-8"))
            scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
            if isinstance(scripts, dict):
                package_runner = _detect_js_runner(root)
                if "test" in scripts:
                    commands.append(CommandSpec(f"{package_runner} test", f"{package_runner} test", "test"))
                if "lint" in scripts:
                    commands.append(CommandSpec(f"{package_runner} run lint", f"{package_runner} run lint", "lint"))
                if "typecheck" in scripts:
                    commands.append(CommandSpec(f"{package_runner} run typecheck", f"{package_runner} run typecheck", "typecheck"))
                for dev_name in ("dev", "start"):
                    if dev_name in scripts:
                        commands.append(CommandSpec(f"{package_runner} run {dev_name}", f"{package_runner} run {dev_name}", "server"))
        except Exception:
            pass
    if (root / "pytest.ini").exists() or (root / "pyproject.toml").exists() or (root / "tests").exists():
        commands.append(CommandSpec("pytest", "python -m pytest"))
    if (root / "manage.py").exists():
        commands.append(CommandSpec("Django tests", "python manage.py test"))
    if (root / "Cargo.toml").exists():
        commands.append(CommandSpec("cargo test", "cargo test"))
    if (root / "go.mod").exists():
        commands.append(CommandSpec("go test", "go test ./..."))

    seen: set[str] = set()
    deduped: list[CommandSpec] = []
    for spec in commands:
        if spec.command in seen:
            continue
        seen.add(spec.command)
        deduped.append(spec)
    return deduped


def _detect_js_runner(root: pathlib.Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


def classify_command_action(command: str) -> str:
    raw_text = str(command or "")
    text = raw_text.lower()
    unquoted = _unquoted_text(raw_text).lower()
    if has_shell_control_operator(raw_text):
        return "run_network"
    if any(token in unquoted for token in (" pip install", "npm install", "pnpm install", "yarn install", "cargo install")):
        return "run_install"
    if any(token in unquoted for token in ("curl ", "wget ", "http://", "https://")):
        return "run_network"
    if any(token in text for token in ("urlopen(", "requests.get", "requests.post", "httpx.", "aiohttp.")):
        return "run_network"
    if any(token in unquoted for token in (" run dev", " start", "serve", "uvicorn", "flask run")):
        return "start_server"
    return "run_safe_command"


def run_workspace_command(
    workspace_path: str,
    command: str,
    approval_mode: ApprovalMode,
    *,
    timeout: int = 120,
    workspace_id: str = "",
    thread_id: str = "",
) -> CommandResult:
    root = pathlib.Path(workspace_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Workspace folder does not exist: {workspace_path}")
    action = classify_command_action(command)
    workspace = None
    if workspace_id:
        try:
            from developer.storage import get_workspace
            workspace = get_workspace(workspace_id)
        except Exception:
            workspace = None
    decision = decide_action(approval_mode, action)  # type: ignore[arg-type]
    decision = _apply_docker_network_policy(workspace, action, decision)
    if decision.decision != "allow":
        return CommandResult(
            command=command,
            cwd=str(root),
            returncode=None,
            stderr=decision.reason,
            decision=decision,
        )
    if workspace is not None and workspace.execution_mode == "docker":
        from developer.sandbox_runtime import run_docker_sandbox_command

        outcome = run_docker_sandbox_command(
            workspace,
            command,
            thread_id=thread_id,
            timeout=timeout,
        )
        return CommandResult(
            command=command,
            cwd=outcome.cwd,
            returncode=outcome.returncode,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            decision=decision,
            changed_files=outcome.changed_files,
            execution_mode="docker",
            sandbox_backend=outcome.sandbox_backend,
            sandbox_pending_change_id=outcome.pending_change_id,
        )
    args = split_command(command)
    completed = subprocess.run(
        args,
        cwd=str(root),
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(
        command=command,
        cwd=str(root),
        returncode=completed.returncode,
        stdout=completed.stdout[-20_000:],
        stderr=completed.stderr[-20_000:],
        decision=decision,
    )


def run_workspace_shell_command(
    workspace_path: str,
    command: str,
    approval_mode: ApprovalMode,
    *,
    workspace_id: str,
    thread_id: str,
    timeout: int = 120,
    confirmed: bool = False,
) -> CommandResult:
    """Run a shell command in the workspace and ledger file side effects.

    This is the Developer-native escape hatch for repo-specific commands. It
    intentionally uses the platform shell, so Auto Edit still asks before
    running it unless the caller has an explicit confirmation.
    """
    root = pathlib.Path(workspace_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Workspace folder does not exist: {workspace_path}")

    action = classify_command_action(command)
    try:
        from developer.storage import get_workspace
        workspace = get_workspace(workspace_id)
    except Exception:
        workspace = None
    decision = decide_action(approval_mode, action)  # type: ignore[arg-type]
    decision = _apply_docker_network_policy(workspace, action, decision)
    if decision.allowed and approval_mode == "auto_edit":
        decision = ApprovalDecision("ask", "Auto Edit requires approval before running a shell command.")
    decision = _apply_docker_network_policy(workspace, action, decision)
    if decision.requires_approval and confirmed:
        decision = ApprovalDecision("allow", "User explicitly approved this shell command.")
    decision = _apply_docker_network_policy(workspace, action, decision)
    if not decision.allowed:
        return CommandResult(
            command=command,
            cwd=str(root),
            returncode=None,
            stderr=decision.reason,
            decision=decision,
        )

    if workspace is not None and workspace.execution_mode == "docker":
        from developer.sandbox_runtime import run_docker_sandbox_command

        outcome = run_docker_sandbox_command(
            workspace,
            command,
            thread_id=thread_id,
            timeout=timeout,
        )
        return CommandResult(
            command=command,
            cwd=outcome.cwd,
            returncode=outcome.returncode,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            decision=decision,
            changed_files=outcome.changed_files,
            execution_mode="docker",
            sandbox_backend=outcome.sandbox_backend,
            sandbox_pending_change_id=outcome.pending_change_id,
        )

    before = _snapshot_changed_files(root)
    completed = subprocess.run(
        _platform_shell_args(command),
        cwd=str(root),
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    after = _snapshot_changed_files(root)
    changed_paths = sorted(set(before) | set(after))
    file_changes: list[FileChange] = []
    for rel_path in changed_paths:
        before_text = before.get(rel_path)
        if rel_path not in before:
            before_text = _head_text(root, rel_path)
        after_text = after.get(rel_path)
        if change_ledger.text_hash(before_text) == change_ledger.text_hash(after_text):
            continue
        if before_text is None and after_text is not None:
            action_name = "create"
        elif before_text is not None and after_text is None:
            action_name = "delete"
        else:
            action_name = "update"
        file_changes.append(
            FileChange(
                path=rel_path,
                action=action_name,
                before_hash=change_ledger.text_hash(before_text),
                after_hash=change_ledger.text_hash(after_text),
                before_text=before_text,
            )
        )
    if file_changes:
        change_ledger.record_change_set(
            workspace_id=workspace_id,
            thread_id=thread_id,
            summary=f"Shell command: {command[:120]}",
            files=file_changes,
        )
    return CommandResult(
        command=command,
        cwd=str(root),
        returncode=completed.returncode,
        stdout=completed.stdout[-20_000:],
        stderr=completed.stderr[-20_000:],
        decision=decision,
        changed_files=[item.path for item in file_changes],
    )


def _apply_docker_network_policy(workspace, action: str, decision: ApprovalDecision) -> ApprovalDecision:
    if (
        workspace is not None
        and getattr(workspace, "execution_mode", "") == "docker"
        and action in {"run_network", "run_install"}
        and getattr(workspace, "sandbox_network", "off") == "off"
    ):
        label = "package install" if action == "run_install" else "network command"
        return ApprovalDecision(
            "block",
            f"Docker Sandbox network is Off. Switch the sandbox network policy to Ask or On before running this {label}.",
        )
    return decision


def _platform_shell_args(command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command]
    return ["/bin/sh", "-lc", command]


def _snapshot_changed_files(root: pathlib.Path) -> dict[str, str | None]:
    paths = _git_status_paths(root)
    snapshot: dict[str, str | None] = {}
    for rel_path in paths:
        target = (root / rel_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if not target.exists() or not target.is_file():
            snapshot[rel_path] = None
            continue
        if not _looks_text(target):
            snapshot[rel_path] = None
            continue
        try:
            snapshot[rel_path] = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            snapshot[rel_path] = None
    return snapshot


def _git_status_paths(root: pathlib.Path) -> set[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return set()
    paths: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        rel = line[3:].strip()
        if " -> " in rel:
            old_path, new_path = rel.split(" -> ", 1)
            paths.add(old_path.strip())
            rel = new_path.strip()
        paths.add(rel)
    return paths


def _head_text(root: pathlib.Path, rel_path: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _looks_text(path: pathlib.Path, *, sniff_bytes: int = 4096) -> bool:
    try:
        data = path.read_bytes()[:sniff_bytes]
    except Exception:
        return False
    return b"\0" not in data


def start_workspace_process(
    workspace_path: str,
    command: str,
    approval_mode: ApprovalMode,
    *,
    workspace_id: str = "",
    thread_id: str = "",
) -> CommandResult:
    """Start a tracked long-running process for later cleanup.

    This is intentionally small for the first Developer phase. The UI does not
    expose arbitrary server starts yet, but this gives future preview/server
    flows one cleanup path instead of ad hoc subprocess management.
    """
    root = pathlib.Path(workspace_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Workspace folder does not exist: {workspace_path}")
    action = classify_command_action(command)
    if action == "run_safe_command":
        action = "start_server"
    decision = decide_action(approval_mode, action)  # type: ignore[arg-type]
    if decision.decision != "allow":
        return CommandResult(command=command, cwd=str(root), returncode=None, stderr=decision.reason, decision=decision)

    if workspace_id:
        try:
            from developer.storage import get_workspace
            workspace = get_workspace(workspace_id)
        except Exception:
            workspace = None
        if workspace is not None and workspace.execution_mode == "docker":
            from developer.sandbox_runtime import start_docker_sandbox_process

            outcome = start_docker_sandbox_process(workspace, command, thread_id=thread_id)
            return CommandResult(
                command=command,
                cwd=outcome.cwd,
                returncode=outcome.returncode,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                decision=decision,
                execution_mode="docker",
                sandbox_backend=outcome.sandbox_backend,
            )

    proc = subprocess.Popen(
        split_command(command),
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
    )
    _ACTIVE_PROCESSES.setdefault(str(root), []).append(proc)
    return CommandResult(command=command, cwd=str(root), returncode=0, stdout=f"Started PID {proc.pid}", decision=decision)


def stop_workspace_processes(workspace_path: str, *, workspace_id: str = "") -> int:
    if workspace_id:
        try:
            from developer.storage import get_workspace
            workspace = get_workspace(workspace_id)
        except Exception:
            workspace = None
        if workspace is not None and workspace.execution_mode == "docker":
            from developer.sandbox_runtime import stop_docker_sandbox_processes

            return stop_docker_sandbox_processes(workspace)
    root = str(pathlib.Path(workspace_path).expanduser().resolve())
    processes = _ACTIVE_PROCESSES.pop(root, [])
    stopped = 0
    for proc in processes:
        if proc.poll() is not None:
            continue
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stopped += 1
    return stopped
