"""Reusable Row-Bot app launch smoke test.

Starts the app, waits for an authenticated launcher ping (or a public health
probe for commands that spawn a child server), optionally checks /, and then
terminates the process. The script is intentionally stdlib-only so CI and
packaged release smoke can run it before any extra test dependencies are added.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path


LAUNCH_SECRET_ENV = "ROW_BOT_LAUNCH_SECRET"


@dataclass
class SmokeResult:
    ok: bool
    port: int
    messages: list[tuple[str, str]] = field(default_factory=list)

    def add(self, status: str, message: str) -> None:
        self.messages.append((status, message))


def _port_open(port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _tail_file(path: Path, max_lines: int = 80) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    return "\n".join(lines[-max(1, max_lines):])


def _add_tail(result: SmokeResult, label: str, path: Path, max_lines: int = 80) -> None:
    tail = _tail_file(path, max_lines=max_lines)
    if tail:
        result.add("INFO", f"{label} tail:\n{tail}")


def run_app_smoke(
    *,
    command: list[str] | None = None,
    cwd: Path | str | None = None,
    port: int = 8080,
    timeout: float = 90.0,
    check_root: bool = True,
    wait_startup_ready: bool = False,
    public_probes: bool = False,
    data_dir: Path | str | None = None,
) -> SmokeResult:
    """Run a live app smoke test and return structured status messages."""
    cwd_path = Path.cwd() if cwd is None else Path(cwd)
    result = SmokeResult(ok=False, port=port)

    if _port_open(port):
        result.add("WARN", f"port {port} already in use; skipping live launch")
        result.ok = True
        return result

    proc: subprocess.Popen | None = None
    env = {
        **os.environ,
        "ROW_BOT_PORT": str(port),
        "PYTHONIOENCODING": "utf-8",
    }
    launcher_secret = secrets.token_urlsafe(32)
    env[LAUNCH_SECRET_ENV] = launcher_secret
    if data_dir is None:
        temp_data = tempfile.TemporaryDirectory(prefix="row_bot_smoke_", ignore_cleanup_errors=True)
        env["ROW_BOT_DATA_DIR"] = temp_data.name
    else:
        temp_data = None
        env["ROW_BOT_DATA_DIR"] = str(data_dir)

    try:
        cmd = command or [sys.executable, "app.py"]
        with ExitStack() as stack:
            stdout_fd, stdout_name = tempfile.mkstemp(prefix="row_bot_smoke_stdout_", suffix=".log")
            stderr_fd, stderr_name = tempfile.mkstemp(prefix="row_bot_smoke_stderr_", suffix=".log")
            os.close(stdout_fd)
            os.close(stderr_fd)
            stdout_path = Path(stdout_name)
            stderr_path = Path(stderr_name)
            stdout_file = stack.enter_context(stdout_path.open("w", encoding="utf-8"))
            stderr_file = stack.enter_context(stderr_path.open("w", encoding="utf-8"))
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd_path),
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=stdout_file,
                stderr=stderr_file,
            )
            result.add("PASS", f"app process started (PID {proc.pid})")

            launched_at = time.monotonic()
            deadline = time.monotonic() + timeout
            probe_path = "/healthz" if public_probes else "/api/launcher-ping"
            probe_headers = (
                {} if public_probes else {"Authorization": f"Bearer {launcher_secret}"}
            )
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    result.add("FAIL", f"app exited during startup with code {proc.returncode}")
                    stdout_file.flush()
                    stderr_file.flush()
                    _add_tail(result, "stdout", stdout_path)
                    _add_tail(result, "stderr", stderr_path)
                    _add_tail(result, "launcher app log", Path(env["ROW_BOT_DATA_DIR"]) / "row_bot_app.log")
                    return result
                try:
                    probe_request = urllib.request.Request(
                        f"http://127.0.0.1:{port}{probe_path}",
                        headers=probe_headers,
                    )
                    with urllib.request.urlopen(probe_request, timeout=2) as response:
                        body = response.read(512).decode("utf-8", errors="replace")
                    payload = json.loads(body)
                    expected_payload = (
                        payload.get("ok") is True and payload.get("status") == "alive"
                        if public_probes
                        else payload.get("app") == "row-bot"
                    )
                    if response.status == 200 and expected_payload:
                        elapsed = time.monotonic() - launched_at
                        result.add(
                            "PASS",
                            f"{probe_path} responded on port {port} after {elapsed:.1f}s",
                        )
                        break
                except Exception:
                    time.sleep(1)
            else:
                result.add("FAIL", f"{probe_path} did not respond within {timeout:.0f}s")
                stdout_file.flush()
                stderr_file.flush()
                _add_tail(result, "stdout", stdout_path)
                _add_tail(result, "stderr", stderr_path)
                _add_tail(result, "launcher app log", Path(env["ROW_BOT_DATA_DIR"]) / "row_bot_app.log")
                return result

            if wait_startup_ready:
                readiness_path = "/readyz" if public_probes else "/api/startup-state"
                readiness_headers = (
                    {}
                    if public_probes
                    else {"Authorization": f"Bearer {launcher_secret}"}
                )
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        result.add("FAIL", f"app exited before startup_ready with code {proc.returncode}")
                        stdout_file.flush()
                        stderr_file.flush()
                        _add_tail(result, "stdout", stdout_path)
                        _add_tail(result, "stderr", stderr_path)
                        _add_tail(result, "launcher app log", Path(env["ROW_BOT_DATA_DIR"]) / "row_bot_app.log")
                        return result
                    try:
                        startup_request = urllib.request.Request(
                            f"http://127.0.0.1:{port}{readiness_path}",
                            headers=readiness_headers,
                        )
                        with urllib.request.urlopen(startup_request, timeout=2) as response:
                            body = response.read(2048).decode("utf-8", errors="replace")
                        state = json.loads(body)
                        ready = (
                            state.get("ok") is True and state.get("status") == "ready"
                            if public_probes
                            else state.get("ready") is True
                        )
                        if response.status == 200 and ready:
                            elapsed = time.monotonic() - launched_at
                            result.add(
                                "PASS",
                                f"{readiness_path} ready on port {port} after {elapsed:.1f}s",
                            )
                            break
                    except Exception:
                        time.sleep(1)
                else:
                    result.add(
                        "FAIL",
                        f"{readiness_path} did not become ready within {timeout:.0f}s",
                    )
                    stdout_file.flush()
                    stderr_file.flush()
                    _add_tail(result, "stdout", stdout_path)
                    _add_tail(result, "stderr", stderr_path)
                    _add_tail(result, "launcher app log", Path(env["ROW_BOT_DATA_DIR"]) / "row_bot_app.log")
                    return result

            if check_root:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
                        if response.status == 200:
                            result.add("PASS", "HTTP GET / returned 200")
                        else:
                            result.add("WARN", f"HTTP GET / returned {response.status}")
                except Exception as exc:
                    result.add("WARN", f"HTTP GET / failed: {exc}")

            result.ok = True
            return result
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            result.add("PASS", "app process terminated")
        if temp_data is not None:
            temp_data.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch Row-Bot and verify its health")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--no-root-check", action="store_true")
    parser.add_argument("--wait-startup-ready", action="store_true")
    parser.add_argument(
        "--public-probes",
        action="store_true",
        help="Use /healthz and /readyz when the command owns the app secret",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Optional command after --")
    args = parser.parse_args()

    command = args.command or None
    if command and command[0] == "--":
        command = command[1:]
    result = run_app_smoke(
        command=command,
        cwd=args.cwd,
        port=args.port,
        timeout=args.timeout,
        check_root=not args.no_root_check,
        wait_startup_ready=args.wait_startup_ready,
        public_probes=args.public_probes,
        data_dir=args.data_dir,
    )
    for status, message in result.messages:
        print(f"[{status}] {message}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
