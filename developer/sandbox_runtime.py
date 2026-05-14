from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import pathlib
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime

from developer.executables import resolve_docker, resolve_podman
from developer.state import DeveloperWorkspace
from developer.storage import DEVELOPER_DIR

logger = logging.getLogger(__name__)


SANDBOX_ROOT = DEVELOPER_DIR / "sandboxes"
PENDING_CHANGES_PATH = SANDBOX_ROOT / "pending_changes.json"
SESSIONS_PATH = SANDBOX_ROOT / "sessions.json"
_TEXT_LIMIT = 1_000_000
_COPY_SKIP_DIRS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "node_modules",
    "venv",
}
_SNAPSHOT_SKIP_DIRS = _COPY_SKIP_DIRS | {".git", ".hg", ".svn"}


@dataclass(frozen=True)
class SandboxProbe:
    available: bool
    binary: str = ""
    version: str = ""
    message: str = ""


@dataclass(frozen=True)
class SandboxProcessInfo:
    pid: int
    command: str
    log_path: str = ""
    started_at: str = ""

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "command": self.command,
            "log_path": self.log_path,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SandboxProcessInfo":
        return cls(
            pid=int(raw.get("pid") or 0),
            command=str(raw.get("command", "")),
            log_path=str(raw.get("log_path", "")),
            started_at=str(raw.get("started_at", "")),
        )


@dataclass(frozen=True)
class SandboxStatus:
    available: bool
    backend: str = "docker"
    container_name: str = ""
    running: bool = False
    exists: bool = False
    image: str = ""
    network: str = "off"
    shadow_workspace: str = ""
    runtime_version: str = ""
    message: str = ""
    processes: list[SandboxProcessInfo] = field(default_factory=list)


@dataclass(frozen=True)
class SandboxCommandOutcome:
    command: str
    cwd: str
    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    changed_files: list[str] = field(default_factory=list)
    pending_change_id: str = ""
    execution_mode: str = "docker"
    sandbox_backend: str = "docker"
    sandbox_workspace: str = ""
    container_name: str = ""

    @property
    def ran(self) -> bool:
        return self.returncode is not None


@dataclass(frozen=True)
class SandboxPendingChange:
    id: str
    workspace_id: str
    thread_id: str
    command: str
    patch: str
    files: list[str]
    created_at: str
    imported: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "thread_id": self.thread_id,
            "command": self.command,
            "patch": self.patch,
            "files": list(self.files),
            "created_at": self.created_at,
            "imported": self.imported,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SandboxPendingChange":
        return cls(
            id=str(raw.get("id", "")),
            workspace_id=str(raw.get("workspace_id", "")),
            thread_id=str(raw.get("thread_id", "")),
            command=str(raw.get("command", "")),
            patch=str(raw.get("patch", "")),
            files=[str(item) for item in raw.get("files", []) if str(item or "").strip()],
            created_at=str(raw.get("created_at", "")),
            imported=bool(raw.get("imported", False)),
        )


def detect_container_runtime() -> SandboxProbe:
    for name, resolver in (("docker", resolve_docker), ("podman", resolve_podman)):
        binary = resolver()
        if not binary:
            continue
        try:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            return SandboxProbe(False, binary=binary, message=str(exc))
        if proc.returncode == 0:
            version = (proc.stdout or proc.stderr).strip()
            if name == "docker":
                try:
                    server_proc = subprocess.run(
                        [binary, "info", "--format", "{{.ServerVersion}}"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                except Exception as exc:
                    message = _friendly_docker_engine_error(str(exc))
                    return SandboxProbe(False, binary=binary, version=version, message=f"Docker CLI is installed but the engine is not accessible: {message}")
                if server_proc.returncode != 0:
                    message = _friendly_docker_engine_error(server_proc.stderr or server_proc.stdout or "Docker engine is not accessible.")
                    return SandboxProbe(False, binary=binary, version=version, message=f"Docker CLI is installed but the engine is not accessible: {message}")
                server_version = (server_proc.stdout or "").strip()
                if server_version:
                    version = f"{version} (engine {server_version})"
            return SandboxProbe(True, binary=binary, version=version)
        return SandboxProbe(False, binary=binary, message=(proc.stderr or proc.stdout or "Container runtime failed.").strip())
    return SandboxProbe(False, message="Docker or Podman was not found on PATH.")


def sandbox_shadow_path(workspace_id: str) -> pathlib.Path:
    digest = hashlib.sha1(workspace_id.encode("utf-8")).hexdigest()[:12]
    return SANDBOX_ROOT / workspace_id / digest / "workspace"


def sandbox_container_name(workspace_id: str) -> str:
    digest = hashlib.sha1(workspace_id.encode("utf-8")).hexdigest()[:16]
    return f"thoth-dev-{digest}"


def get_docker_sandbox_status(workspace: DeveloperWorkspace) -> SandboxStatus:
    probe = detect_container_runtime()
    name = sandbox_container_name(workspace.id)
    shadow = sandbox_shadow_path(workspace.id)
    if not probe.available:
        return SandboxStatus(
            available=False,
            container_name=name,
            image=workspace.sandbox_image,
            network=workspace.sandbox_network,
            shadow_workspace=str(shadow),
            message=probe.message,
            processes=list_sandbox_processes(workspace.id),
        )
    exists, running = _container_state(probe.binary, name)
    image_present = exists or _docker_image_exists(probe.binary, workspace.sandbox_image)
    message = "Running" if running else ("Stopped" if exists else "Not created")
    if not image_present:
        message = _missing_image_message(workspace.sandbox_image)
    return SandboxStatus(
        available=image_present,
        container_name=name,
        exists=exists,
        running=running,
        image=workspace.sandbox_image,
        network=workspace.sandbox_network,
        shadow_workspace=str(shadow),
        runtime_version=probe.version,
        message=message,
        processes=list_sandbox_processes(workspace.id),
    )


def list_pending_changes(*, workspace_id: str, thread_id: str | None = None, include_imported: bool = False) -> list[SandboxPendingChange]:
    payload = _load_pending_payload()
    rows: list[SandboxPendingChange] = []
    for raw in payload.get("changes", []):
        if not isinstance(raw, dict):
            continue
        change = SandboxPendingChange.from_dict(raw)
        if change.workspace_id != workspace_id:
            continue
        if thread_id is not None and change.thread_id != thread_id:
            continue
        if change.imported and not include_imported:
            continue
        rows.append(change)
    rows.sort(key=lambda item: item.created_at, reverse=True)
    return rows


def get_pending_change(change_id: str) -> SandboxPendingChange | None:
    payload = _load_pending_payload()
    for raw in payload.get("changes", []):
        if isinstance(raw, dict) and raw.get("id") == change_id:
            return SandboxPendingChange.from_dict(raw)
    return None


def mark_pending_change_imported(change_id: str) -> None:
    payload = _load_pending_payload()
    for raw in payload.get("changes", []):
        if isinstance(raw, dict) and raw.get("id") == change_id:
            raw["imported"] = True
            break
    _save_pending_payload(payload)


def run_docker_sandbox_command(
    workspace: DeveloperWorkspace,
    command: str,
    *,
    thread_id: str,
    timeout: int = 120,
) -> SandboxCommandOutcome:
    probe = detect_container_runtime()
    if not probe.available:
        return SandboxCommandOutcome(
            command=command,
            cwd=workspace.path,
            returncode=None,
            stderr=f"Docker Sandbox is not available: {probe.message}",
        )

    try:
        container_name, shadow = ensure_docker_sandbox(workspace, probe=probe)
    except Exception as exc:
        return SandboxCommandOutcome(
            command=command,
            cwd=workspace.path,
            returncode=None,
            stderr=str(exc),
        )
    before = _snapshot_text_files(shadow)
    docker_args = _docker_exec_args(probe.binary, container_name, command)
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            docker_args,
            cwd=str(shadow),
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        after_timeout = _snapshot_text_files(shadow)
        pending = _record_pending_change(workspace, thread_id, command, before, after_timeout)
        return SandboxCommandOutcome(
            command=command,
            cwd="/workspace",
            returncode=None,
            stdout=(exc.stdout or "")[-20_000:] if isinstance(exc.stdout, str) else "",
            stderr=f"Sandbox command timed out after {timeout}s.",
            changed_files=pending.files if pending else [],
            pending_change_id=pending.id if pending else "",
            sandbox_workspace=str(shadow),
            container_name=container_name,
        )
    elapsed = time.perf_counter() - started
    if elapsed > 5:
        logger.info("developer sandbox command completed in %.3fs workspace=%s", elapsed, workspace.id)
    after = _snapshot_text_files(shadow)
    pending = _record_pending_change(workspace, thread_id, command, before, after)
    return SandboxCommandOutcome(
        command=command,
        cwd="/workspace",
        returncode=completed.returncode,
        stdout=(completed.stdout or "")[-20_000:],
        stderr=(completed.stderr or "")[-20_000:],
        changed_files=pending.files if pending else [],
        pending_change_id=pending.id if pending else "",
        sandbox_workspace=str(shadow),
        container_name=container_name,
    )


def write_file_in_docker_sandbox(
    workspace: DeveloperWorkspace,
    path: str,
    content: str,
    *,
    thread_id: str,
) -> SandboxCommandOutcome:
    try:
        ensure_docker_sandbox(workspace)
        container_name, shadow = _ensure_shadow_workspace(workspace)
    except Exception as exc:
        return SandboxCommandOutcome(
            command=f"write {path}",
            cwd=workspace.path,
            returncode=None,
            stderr=str(exc),
        )
    before = _snapshot_text_files(shadow)
    try:
        target = _validate_shadow_relative_path(shadow, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except Exception as exc:
        return SandboxCommandOutcome(
            command=f"write {path}",
            cwd="/workspace",
            returncode=None,
            stderr=str(exc),
            sandbox_workspace=str(shadow),
            container_name=container_name,
        )
    after = _snapshot_text_files(shadow)
    pending = _record_pending_change(workspace, thread_id, f"write {path}", before, after)
    return SandboxCommandOutcome(
        command=f"write {path}",
        cwd="/workspace",
        returncode=0,
        stdout=f"Wrote {path} in Docker Sandbox shadow workspace. No container command was needed.",
        changed_files=pending.files if pending else [],
        pending_change_id=pending.id if pending else "",
        sandbox_workspace=str(shadow),
        container_name=container_name,
    )


def apply_patch_in_docker_sandbox(
    workspace: DeveloperWorkspace,
    patch: str,
    *,
    thread_id: str,
    summary: str = "",
) -> SandboxCommandOutcome:
    try:
        ensure_docker_sandbox(workspace)
        container_name, shadow = _ensure_shadow_workspace(workspace)
    except Exception as exc:
        return SandboxCommandOutcome(
            command=summary or "apply patch",
            cwd=workspace.path,
            returncode=None,
            stderr=str(exc),
        )
    before = _snapshot_text_files(shadow)
    try:
        paths = _paths_from_patch(shadow, patch)
        if not paths:
            raise ValueError("Patch did not include any workspace file paths.")
        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=str(shadow),
            input=patch,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if check.returncode != 0:
            raise ValueError((check.stderr or check.stdout or "Patch did not apply.").strip())
        applied = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=str(shadow),
            input=patch,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if applied.returncode != 0:
            raise ValueError((applied.stderr or applied.stdout or "Patch failed while applying.").strip())
    except Exception as exc:
        return SandboxCommandOutcome(
            command=summary or "apply patch",
            cwd="/workspace",
            returncode=None,
            stderr=str(exc),
            sandbox_workspace=str(shadow),
            container_name=container_name,
        )
    after = _snapshot_text_files(shadow)
    pending = _record_pending_change(workspace, thread_id, summary or "apply patch", before, after)
    return SandboxCommandOutcome(
        command=summary or "apply patch",
        cwd="/workspace",
        returncode=0,
        stdout="Applied patch in Docker Sandbox shadow workspace. No container command was needed.",
        changed_files=pending.files if pending else [],
        pending_change_id=pending.id if pending else "",
        sandbox_workspace=str(shadow),
        container_name=container_name,
    )


def ensure_docker_sandbox(
    workspace: DeveloperWorkspace,
    *,
    probe: SandboxProbe | None = None,
    rebuild: bool = False,
) -> tuple[str, pathlib.Path]:
    probe = probe or detect_container_runtime()
    if not probe.available:
        raise RuntimeError(f"Docker Sandbox is not available: {probe.message}")
    host_root = pathlib.Path(workspace.path).expanduser().resolve()
    if not host_root.is_dir():
        raise ValueError(f"Workspace folder does not exist: {workspace.path}")

    container_name = sandbox_container_name(workspace.id)
    shadow = sandbox_shadow_path(workspace.id)
    if rebuild:
        _remove_container(probe.binary, container_name)
        _remove_shadow_workspace(workspace.id)
    if not shadow.exists():
        _prepare_shadow_workspace(workspace.id, host_root)

    exists, running = _container_state(probe.binary, container_name)
    if exists and _container_network_mismatch(probe.binary, container_name, workspace):
        logger.info(
            "Developer sandbox network policy changed; recreating container %s for workspace %s",
            container_name,
            workspace.id,
        )
        _remove_container(probe.binary, container_name)
        exists = False
        running = False
    if exists and running:
        return container_name, shadow
    if exists:
        proc = subprocess.run(
            [probe.binary, "start", container_name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "Failed to start Docker Sandbox.").strip())
        return container_name, shadow

    if not _docker_image_exists(probe.binary, workspace.sandbox_image):
        raise RuntimeError(_missing_image_message(workspace.sandbox_image))

    args = _docker_create_args(probe.binary, workspace, shadow, container_name)
    proc = subprocess.run(
        args,
        cwd=str(shadow),
        shell=False,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(_friendly_docker_error(proc.stderr or proc.stdout or "Failed to create Docker Sandbox."))
    return container_name, shadow


def rebuild_docker_sandbox(workspace: DeveloperWorkspace) -> SandboxStatus:
    ensure_docker_sandbox(workspace, rebuild=True)
    return get_docker_sandbox_status(workspace)


def cleanup_workspace_sandbox(workspace_id: str) -> bool:
    probe = detect_container_runtime()
    container_name = sandbox_container_name(workspace_id)
    if probe.binary:
        _remove_container(probe.binary, container_name)
    _clear_sandbox_processes(workspace_id)
    target = SANDBOX_ROOT / workspace_id
    resolved = target.resolve()
    root = SANDBOX_ROOT.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Refusing to clean sandbox outside sandbox root: {target}") from exc
    if not resolved.exists():
        return False
    _safe_rmtree(resolved)
    return True


def start_docker_sandbox_process(
    workspace: DeveloperWorkspace,
    command: str,
    *,
    thread_id: str = "",
) -> SandboxCommandOutcome:
    del thread_id
    probe = detect_container_runtime()
    if not probe.available:
        return SandboxCommandOutcome(
            command=command,
            cwd=workspace.path,
            returncode=None,
            stderr=f"Docker Sandbox is not available: {probe.message}",
        )
    try:
        container_name, shadow = ensure_docker_sandbox(workspace, probe=probe)
    except Exception as exc:
        return SandboxCommandOutcome(
            command=command,
            cwd=workspace.path,
            returncode=None,
            stderr=str(exc),
        )
    log_name = f"thoth-dev-{hashlib.sha1((command + str(time.time())).encode('utf-8')).hexdigest()[:10]}.log"
    log_path = f"/tmp/{log_name}"
    script = f"nohup /bin/sh -lc {shlex.quote(command)} > {shlex.quote(log_path)} 2>&1 & echo $!"
    proc = subprocess.run(
        [probe.binary, "exec", container_name, "/bin/sh", "-lc", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return SandboxCommandOutcome(
            command=command,
            cwd="/workspace",
            returncode=None,
            stderr=(proc.stderr or proc.stdout or "Failed to start sandbox process.").strip(),
            sandbox_workspace=str(shadow),
            container_name=container_name,
        )
    try:
        pid = int((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        pid = 0
    if pid:
        _record_sandbox_process(
            workspace.id,
            SandboxProcessInfo(
                pid=pid,
                command=command,
                log_path=log_path,
                started_at=datetime.now().isoformat(),
            ),
        )
    return SandboxCommandOutcome(
        command=command,
        cwd="/workspace",
        returncode=0,
        stdout=f"Started sandbox PID {pid}" if pid else "Started sandbox process",
        sandbox_workspace=str(shadow),
        container_name=container_name,
    )


def stop_docker_sandbox_processes(workspace: DeveloperWorkspace) -> int:
    probe = detect_container_runtime()
    if not probe.available:
        return 0
    container_name = sandbox_container_name(workspace.id)
    stopped = 0
    for process in list_sandbox_processes(workspace.id):
        if process.pid <= 0:
            continue
        subprocess.run(
            [probe.binary, "exec", container_name, "/bin/sh", "-lc", f"kill {int(process.pid)} 2>/dev/null || true"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        stopped += 1
    _clear_sandbox_processes(workspace.id)
    return stopped


def list_sandbox_processes(workspace_id: str) -> list[SandboxProcessInfo]:
    payload = _load_sessions_payload()
    raw = payload.get("processes", {}).get(workspace_id, [])
    if not isinstance(raw, list):
        return []
    return [SandboxProcessInfo.from_dict(item) for item in raw if isinstance(item, dict)]


def _docker_create_args(binary: str, workspace: DeveloperWorkspace, shadow: pathlib.Path, container_name: str) -> list[str]:
    args = [
        binary,
        "run",
        "-d",
        "--name",
        container_name,
        "--label",
        f"thoth.developer.workspace_id={workspace.id}",
        "-v",
        f"{str(shadow)}:/workspace",
        "-w",
        "/workspace",
    ]
    if workspace.sandbox_network == "off":
        args.extend(["--network", "none"])
    for name in workspace.sandbox_env_allowlist:
        value = os.environ.get(name)
        if value is not None:
            args.extend(["-e", f"{name}={value}"])
    args.extend([workspace.sandbox_image, "/bin/sh", "-lc", "sleep infinity"])
    return args


def _docker_exec_args(binary: str, container_name: str, command: str) -> list[str]:
    return [binary, "exec", container_name, "/bin/sh", "-lc", command]


def _docker_image_exists(binary: str, image: str) -> bool:
    proc = subprocess.run(
        [binary, "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return proc.returncode == 0


def _container_state(binary: str, container_name: str) -> tuple[bool, bool]:
    proc = subprocess.run(
        [binary, "inspect", "-f", "{{.State.Running}}", container_name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        return False, False
    return True, (proc.stdout or "").strip().lower() == "true"


def _container_network_mismatch(binary: str, container_name: str, workspace: DeveloperWorkspace) -> bool:
    mode = _container_network_mode(binary, container_name)
    if not mode:
        return False
    if workspace.sandbox_network == "off":
        return mode != "none"
    return mode == "none"


def _container_network_mode(binary: str, container_name: str) -> str:
    proc = subprocess.run(
        [binary, "inspect", "-f", "{{.HostConfig.NetworkMode}}", container_name],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip().lower()


def _missing_image_message(image: str) -> str:
    return (
        f"Docker Sandbox image '{image}' is not available locally. "
        "Thoth will not auto-pull sandbox images during command execution because "
        "Docker credential-helper and network failures can otherwise interrupt the chat. "
        f"Pull the image manually with `docker pull {image}`, or choose a sandbox image "
        "that already exists locally."
    )


def _remove_container(binary: str, container_name: str) -> None:
    proc = subprocess.run(
        [binary, "rm", "-f", container_name],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        logger.warning(
            "Failed to remove Developer sandbox container %s: %s",
            container_name,
            (proc.stderr or proc.stdout or "").strip(),
        )


def _safe_rmtree(path: pathlib.Path) -> None:
    def _onerror(func, target, exc_info):
        try:
            os.chmod(target, 0o700)
            func(target)
        except Exception:
            raise exc_info[1]

    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            shutil.rmtree(path, onerror=_onerror)
            return
        except FileNotFoundError:
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise RuntimeError(f"Failed to remove sandbox folder {path}: {last_error}") from last_error


def _remove_shadow_workspace(workspace_id: str) -> bool:
    target = SANDBOX_ROOT / workspace_id
    resolved = target.resolve()
    root = SANDBOX_ROOT.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Refusing to remove sandbox outside sandbox root: {target}") from exc
    if not resolved.exists():
        return False
    _safe_rmtree(resolved)
    return True


def _record_sandbox_process(workspace_id: str, process: SandboxProcessInfo) -> None:
    payload = _load_sessions_payload()
    processes = payload.setdefault("processes", {})
    rows = [item for item in processes.get(workspace_id, []) if isinstance(item, dict)]
    rows = [item for item in rows if int(item.get("pid") or 0) != process.pid]
    rows.append(process.to_dict())
    processes[workspace_id] = rows
    _save_sessions_payload(payload)


def _clear_sandbox_processes(workspace_id: str) -> None:
    payload = _load_sessions_payload()
    processes = payload.setdefault("processes", {})
    processes.pop(workspace_id, None)
    _save_sessions_payload(payload)


def _prepare_shadow_workspace(workspace_id: str, host_root: pathlib.Path) -> pathlib.Path:
    shadow = sandbox_shadow_path(workspace_id)
    root = SANDBOX_ROOT.resolve()
    shadow_parent = shadow.parent.resolve()
    try:
        shadow_parent.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Refusing to prepare sandbox outside sandbox root: {shadow}") from exc
    if shadow.exists():
        _safe_rmtree(shadow)
    shadow.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(host_root, shadow, ignore=_copy_ignore)
    return shadow


def _ensure_shadow_workspace(workspace: DeveloperWorkspace) -> tuple[str, pathlib.Path]:
    host_root = pathlib.Path(workspace.path).expanduser().resolve()
    if not host_root.is_dir():
        raise ValueError(f"Workspace folder does not exist: {workspace.path}")
    shadow = sandbox_shadow_path(workspace.id)
    if not shadow.exists():
        _prepare_shadow_workspace(workspace.id, host_root)
    return sandbox_container_name(workspace.id), shadow


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _COPY_SKIP_DIRS}


def _friendly_docker_error(message: str) -> str:
    text = (message or "Docker Sandbox failed.").strip()
    if "docker-credential-desktop" in text and "not found" in text:
        return (
            text
            + "\n\nDocker is trying to use the Docker Desktop credential helper, but "
            "docker-credential-desktop is not on PATH. Start Docker Desktop once, repair "
            "the Docker Desktop install, or remove/adjust the credsStore entry in your "
            "Docker config. Docker Sandbox file edits can still use the shadow workspace, "
            "but running sandbox commands requires a working Docker engine and image pull."
        )
    if "Unable to find image" in text:
        return (
            text
            + "\n\nThe sandbox image is not available locally. Start Docker Desktop and "
            "pull the image, or choose an image that already exists locally."
        )
    return text


def _friendly_docker_engine_error(message: str) -> str:
    text = (message or "Docker engine is not accessible.").strip()
    lowered = text.lower()
    if "permission denied" in lowered:
        return text
    stopped_markers = (
        "dockerdesktoplinuxengine",
        "the system cannot find the file specified",
        "open //./pipe/docker",
        "open \\\\.\\pipe\\docker",
        "error during connect",
        "is the docker daemon running",
        "cannot connect to the docker daemon",
    )
    if any(marker in lowered for marker in stopped_markers):
        return (
            "Docker Desktop is installed but not running. Start Docker Desktop and wait "
            "until the engine is ready, then retry the Docker Sandbox command."
        )
    return text


def _validate_shadow_relative_path(root: pathlib.Path, rel_path: str) -> pathlib.Path:
    clean = (rel_path or "").strip().replace("\\", "/")
    if not clean or clean == "/dev/null":
        raise ValueError("Empty sandbox path.")
    if clean.startswith(("a/", "b/")):
        clean = clean[2:]
    if clean.startswith("/") or clean.startswith("../") or "/../" in clean:
        raise ValueError(f"Path escapes sandbox workspace: {rel_path}")
    target = (root / clean).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes sandbox workspace: {rel_path}") from exc
    return target


def _paths_from_patch(root: pathlib.Path, patch: str) -> list[str]:
    paths: list[str] = []
    for line in (patch or "").splitlines():
        text = line.strip()
        candidates: list[str] = []
        if text.startswith("diff --git "):
            parts = text.split()
            if len(parts) >= 4:
                candidates.extend([parts[2], parts[3]])
        elif text.startswith("--- ") or text.startswith("+++ "):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                candidates.append(parts[1].split("\t", 1)[0])
        for candidate in candidates:
            if candidate == "/dev/null":
                continue
            clean = candidate[2:] if candidate.startswith(("a/", "b/")) else candidate
            _validate_shadow_relative_path(root, clean)
            if clean not in paths:
                paths.append(clean)
    return paths


def _snapshot_text_files(root: pathlib.Path) -> dict[str, str | None]:
    snapshot: dict[str, str | None] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if _is_skipped_path(rel):
            continue
        if path.stat().st_size > _TEXT_LIMIT:
            continue
        if not _looks_text(path):
            continue
        try:
            snapshot[rel] = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
    return snapshot


def _record_pending_change(
    workspace: DeveloperWorkspace,
    thread_id: str,
    command: str,
    before: dict[str, str | None],
    after: dict[str, str | None],
) -> SandboxPendingChange | None:
    changed = sorted(path for path in (set(before) | set(after)) if before.get(path) != after.get(path))
    if not changed:
        return None
    patch_parts: list[str] = []
    for path in changed:
        patch = _unified_file_patch(path, before.get(path), after.get(path))
        if patch:
            patch_parts.append(patch)
    combined_patch = "\n".join(patch_parts).strip() + "\n" if patch_parts else ""
    if not combined_patch:
        return None
    change_id = f"sbox_{hashlib.sha1((workspace.id + command + str(time.time())).encode('utf-8')).hexdigest()[:12]}"
    change = SandboxPendingChange(
        id=change_id,
        workspace_id=workspace.id,
        thread_id=thread_id,
        command=command,
        patch=combined_patch,
        files=changed,
        created_at=datetime.now().isoformat(),
    )
    payload = _load_pending_payload()
    payload.setdefault("changes", []).append(change.to_dict())
    _save_pending_payload(payload)
    return change


def _unified_file_patch(path: str, before: str | None, after: str | None) -> str:
    before_lines = [] if before is None else before.splitlines()
    after_lines = [] if after is None else after.splitlines()
    fromfile = "/dev/null" if before is None else f"a/{path}"
    tofile = "/dev/null" if after is None else f"b/{path}"
    body = list(difflib.unified_diff(before_lines, after_lines, fromfile=fromfile, tofile=tofile, lineterm=""))
    if not body:
        return ""
    header = [
        f"diff --git a/{path} b/{path}",
    ]
    if before is None:
        header.append("new file mode 100644")
    elif after is None:
        header.append("deleted file mode 100644")
    return "\n".join(header + body)


def _is_skipped_path(rel_path: str) -> bool:
    return any(part in _SNAPSHOT_SKIP_DIRS for part in pathlib.PurePosixPath(rel_path).parts)


def _looks_text(path: pathlib.Path, *, sniff_bytes: int = 4096) -> bool:
    try:
        data = path.read_bytes()[:sniff_bytes]
    except Exception:
        return False
    return b"\0" not in data


def _load_pending_payload() -> dict:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    if not PENDING_CHANGES_PATH.exists():
        return {"changes": []}
    try:
        payload = json.loads(PENDING_CHANGES_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load Developer sandbox pending changes from %s", PENDING_CHANGES_PATH)
        return {"changes": []}
    if not isinstance(payload, dict):
        return {"changes": []}
    payload.setdefault("changes", [])
    return payload


def _save_pending_payload(payload: dict) -> None:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    tmp_path: pathlib.Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="pending_changes.", suffix=".tmp", dir=SANDBOX_ROOT)
        tmp_path = pathlib.Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp_path.replace(PENDING_CHANGES_PATH)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.debug("Failed to remove temp sandbox pending file %s", tmp_path, exc_info=True)


def _load_sessions_payload() -> dict:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    if not SESSIONS_PATH.exists():
        return {"processes": {}}
    try:
        payload = json.loads(SESSIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load Developer sandbox sessions from %s", SESSIONS_PATH)
        return {"processes": {}}
    if not isinstance(payload, dict):
        return {"processes": {}}
    payload.setdefault("processes", {})
    return payload


def _save_sessions_payload(payload: dict) -> None:
    SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    tmp_path: pathlib.Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix="sessions.", suffix=".tmp", dir=SANDBOX_ROOT)
        tmp_path = pathlib.Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp_path.replace(SESSIONS_PATH)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.debug("Failed to remove temp sandbox session file %s", tmp_path, exc_info=True)
