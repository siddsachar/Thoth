"""Reusable Thoth app launch smoke test.

Starts the app, waits for /api/launcher-ping, optionally checks /, and then
terminates the process. The script is intentionally stdlib-only so CI and
packaged release smoke can run it before any extra test dependencies are added.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path


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
        "THOTH_PORT": str(port),
        "PYTHONIOENCODING": "utf-8",
    }
    if data_dir is None:
        temp_data = tempfile.TemporaryDirectory(prefix="thoth_smoke_", ignore_cleanup_errors=True)
        env["THOTH_DATA_DIR"] = temp_data.name
    else:
        temp_data = None
        env["THOTH_DATA_DIR"] = str(data_dir)

    try:
        cmd = command or [sys.executable, "app.py"]
        with ExitStack() as stack:
            stdout_fd, stdout_name = tempfile.mkstemp(prefix="thoth_smoke_stdout_", suffix=".log")
            stderr_fd, stderr_name = tempfile.mkstemp(prefix="thoth_smoke_stderr_", suffix=".log")
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

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    result.add("FAIL", f"app exited during startup with code {proc.returncode}")
                    stdout_file.flush()
                    stderr_file.flush()
                    _add_tail(result, "stdout", stdout_path)
                    _add_tail(result, "stderr", stderr_path)
                    _add_tail(result, "launcher app log", Path.home() / ".thoth" / "thoth_app.log")
                    return result
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/launcher-ping", timeout=2) as response:
                        body = response.read(512).decode("utf-8", errors="replace")
                    if response.status == 200 and '"app":"thoth"' in body.replace(" ", "").lower():
                        result.add("PASS", f"/api/launcher-ping responded on port {port}")
                        break
                except Exception:
                    time.sleep(1)
            else:
                result.add("FAIL", f"/api/launcher-ping did not respond within {timeout:.0f}s")
                stdout_file.flush()
                stderr_file.flush()
                _add_tail(result, "stdout", stdout_path)
                _add_tail(result, "stderr", stderr_path)
                _add_tail(result, "launcher app log", Path.home() / ".thoth" / "thoth_app.log")
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
    parser = argparse.ArgumentParser(description="Launch Thoth and verify /api/launcher-ping")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--no-root-check", action="store_true")
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
        data_dir=args.data_dir,
    )
    for status, message in result.messages:
        print(f"[{status}] {message}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
