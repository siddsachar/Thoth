"""Background-service helpers for Thoth.

Provides POSIX daemonization, PID-file management, status/stop helpers, and a
generator for a user-level systemd unit. Used by ``launcher.py`` to implement
``--run-as-service`` and the related management flags.

Windows is intentionally not supported here; on Windows ``--run-as-service``
prints guidance to use Task Scheduler or NSSM instead of attempting a fragile
detach.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _data_dir() -> Path:
    return Path(os.environ.get("THOTH_DATA_DIR", Path.home() / ".thoth"))


def default_pid_path() -> Path:
    return _data_dir() / "service.pid"


def default_log_path() -> Path:
    return _data_dir() / "service.log"


def read_pid(pid_path: Path) -> int | None:
    try:
        text = pid_path.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else — treat as alive.
        return True
    return True


def write_pid(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{pid}\n", encoding="utf-8")


def remove_pid(pid_path: Path) -> None:
    try:
        pid_path.unlink()
    except FileNotFoundError:
        pass


def daemonize(pid_path: Path, log_path: Path) -> None:
    """Detach from the controlling terminal using the standard double-fork.

    On return, the calling process is the daemon child: it has no controlling
    terminal, ``stdin`` is ``/dev/null``, and ``stdout``/``stderr`` are
    appended to ``log_path``. The PID of the daemon is written to ``pid_path``.

    Refuses to start a second daemon if ``pid_path`` already references a live
    process; raises :class:`SystemExit` with a friendly message in that case.
    """
    if os.name != "posix":
        raise SystemExit(
            "--run-as-service is only supported on Linux/macOS. "
            "On Windows, use Task Scheduler or NSSM to run thoth in the background."
        )

    existing = read_pid(pid_path)
    if existing is not None and is_alive(existing):
        raise SystemExit(f"Thoth service is already running (PID {existing}).")

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # First fork — let the parent return so the shell prompt comes back.
    if os.fork() > 0:
        os._exit(0)

    # New session, no controlling TTY.
    os.setsid()

    # Second fork — guarantees we cannot re-acquire a TTY.
    if os.fork() > 0:
        os._exit(0)

    os.chdir("/")
    os.umask(0o022)

    # Redirect standard fds.
    sys.stdout.flush()
    sys.stderr.flush()

    with open(os.devnull, "rb") as devnull_in:
        os.dup2(devnull_in.fileno(), sys.stdin.fileno())

    log_fd = os.open(
        str(log_path),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)

    write_pid(pid_path, os.getpid())


def install_signal_handlers(on_term) -> None:
    """Install SIGTERM/SIGINT handlers that delegate to ``on_term``.

    ``on_term`` is invoked with no arguments and should perform a graceful
    shutdown. The handler raises :class:`KeyboardInterrupt` afterwards so that
    existing ``except KeyboardInterrupt`` blocks still trigger.
    """
    def _handler(signum, _frame):  # pragma: no cover - exercised via signals
        logger.info("Received signal %s; shutting down", signum)
        try:
            on_term()
        finally:
            raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)


def stop_service(pid_path: Path, timeout: float = 10.0) -> str:
    pid = read_pid(pid_path)
    if pid is None:
        return "Thoth service is not running (no PID file)."
    if not is_alive(pid):
        remove_pid(pid_path)
        return f"Thoth service is not running (stale PID file removed; was PID {pid})."

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pid(pid_path)
        return f"Thoth service was not running (PID {pid})."
    except PermissionError as exc:
        return f"Cannot signal PID {pid}: {exc}."

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            remove_pid(pid_path)
            return f"Stopped Thoth service (PID {pid})."
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        if exc.errno != errno.ESRCH:
            return f"Failed to force-kill PID {pid}: {exc}."

    remove_pid(pid_path)
    return f"Force-killed Thoth service (PID {pid}) after {timeout:.0f}s timeout."


def status_message(pid_path: Path) -> str:
    pid = read_pid(pid_path)
    if pid is None:
        return "Thoth service: stopped."
    if is_alive(pid):
        return f"Thoth service: running (PID {pid}, pidfile {pid_path})."
    return f"Thoth service: stopped (stale PID file at {pid_path}, was PID {pid})."


_SYSTEMD_UNIT_TEMPLATE = """[Unit]
Description=Thoth — local-first AI assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={launch_cmd} --server --no-tray --no-open --no-splash
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
"""


def install_systemd_unit(
    launch_cmd: str | None = None,
    unit_path: Path | None = None,
) -> Path:
    """Write a user-level systemd unit for Thoth and return its path.

    Caller-controlled ``launch_cmd`` is interpolated verbatim into ``ExecStart``;
    if omitted, this resolves the absolute path to the ``thoth`` launcher
    (``~/.local/bin/thoth`` if available, otherwise the value of ``sys.argv[0]``).
    """
    if sys.platform.startswith("win"):
        raise SystemExit("Systemd is Linux-only. Use --run-as-service or Task Scheduler on other platforms.")

    if launch_cmd is None:
        candidate = Path.home() / ".local" / "bin" / "thoth"
        launch_cmd = str(candidate) if candidate.exists() else os.path.abspath(sys.argv[0])

    target = unit_path or (Path.home() / ".config" / "systemd" / "user" / "thoth.service")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_SYSTEMD_UNIT_TEMPLATE.format(launch_cmd=launch_cmd), encoding="utf-8")
    return target
