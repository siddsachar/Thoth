"""Runtime stability and crash diagnostics for Thoth.

This module is intentionally small and dependency-light.  It captures failures
that often bypass normal request logging: uncaught thread exceptions, asyncio
task exceptions, unraisable finalizer errors, fatal interpreter faults, and
browser-side JavaScript errors reported by the UI.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import pathlib
import platform
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from types import TracebackType
from typing import Any


logger = logging.getLogger(__name__)

_DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
_CRASH_DIR = _DATA_DIR / "crashes"
_SESSION_ID = uuid.uuid4().hex[:12]
_INSTALLED = False
_FATAL_LOG_HANDLE = None
_PERF_MONITOR_TASK: asyncio.Task | None = None
_PERF_STOP = threading.Event()


def session_id() -> str:
    return _SESSION_ID


def crash_dir() -> pathlib.Path:
    return _CRASH_DIR


def setup_stability_monitoring() -> None:
    """Install process-wide exception hooks and fatal-fault logging."""
    global _INSTALLED, _FATAL_LOG_HANDLE
    if _INSTALLED:
        return
    _INSTALLED = True
    try:
        _CRASH_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("Could not create crash diagnostics directory", exc_info=True)

    try:
        fatal_path = _CRASH_DIR / f"fatal-{_SESSION_ID}.log"
        _FATAL_LOG_HANDLE = open(fatal_path, "a", encoding="utf-8")
        faulthandler.enable(file=_FATAL_LOG_HANDLE, all_threads=True)
    except Exception:
        logger.debug("Could not enable faulthandler", exc_info=True)

    _write_report(
        "startup",
        "Thoth stability monitoring started",
        extra={"pid": os.getpid(), "python": sys.version, "platform": platform.platform()},
    )

    sys.excepthook = _sys_excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _threading_excepthook
    if hasattr(sys, "unraisablehook"):
        sys.unraisablehook = _unraisable_hook


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Attach an event-loop exception handler for orphaned task failures."""
    try:
        loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.set_exception_handler(_asyncio_exception_handler)


def mark_shutdown(reason: str = "normal") -> None:
    _write_report("shutdown", "Thoth shutdown marker", extra={"reason": reason})


def record_client_error(payload: dict[str, Any]) -> pathlib.Path | None:
    """Persist a browser-side JS error report."""
    safe_payload = _json_safe(payload)
    msg = str(safe_payload.get("message") or safe_payload.get("reason") or "client error")
    kind = str(safe_payload.get("kind") or "client_error")
    if kind in {"activity", "visibility", "online", "offline"}:
        logger.debug("Client-side event reported: %s %s", kind, msg)
        return None
    if kind == "connection_state":
        logger.info("Client-side connection state reported: %s", msg)
        return None
    logger.warning("Client-side error reported: %s", msg)
    return _write_report("client_error", msg, extra={"client": safe_payload})


def start_performance_monitor(
    *,
    lag_warn_s: float = 1.0,
    interval_s: float = 2.0,
    memory_interval_s: float = 60.0,
) -> None:
    """Start lightweight local event-loop and memory diagnostics."""
    global _PERF_MONITOR_TASK
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _PERF_MONITOR_TASK is not None and not _PERF_MONITOR_TASK.done():
        return
    _PERF_STOP.clear()
    _PERF_MONITOR_TASK = loop.create_task(
        _performance_monitor_loop(lag_warn_s, interval_s, memory_interval_s),
        name="thoth-performance-monitor",
    )


def stop_performance_monitor() -> None:
    _PERF_STOP.set()


def log_performance_snapshot(label: str) -> None:
    """Log a one-off memory/thread snapshot for an important UI event."""
    _log_memory_snapshot(label=label)


def record_ui_callback_error(context: str, exc: BaseException) -> pathlib.Path | None:
    """Persist a UI callback/task exception with a short context label."""
    logger.exception("UI callback failed: %s", context)
    return _write_report(
        "ui_callback",
        f"UI callback failed: {context}",
        exc_type=type(exc).__name__,
        exc_message=str(exc),
        stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        extra={"context": context},
    )


def _sys_excepthook(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    _write_report(
        "uncaught_exception",
        str(exc),
        exc_type=exc_type.__name__,
        exc_message=str(exc),
        stack="".join(traceback.format_exception(exc_type, exc, tb)),
    )
    sys.__excepthook__(exc_type, exc, tb)


def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
    _write_report(
        "thread_exception",
        str(args.exc_value),
        exc_type=args.exc_type.__name__,
        exc_message=str(args.exc_value),
        stack="".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
        extra={"thread": getattr(args.thread, "name", "") if args.thread else ""},
    )
    if getattr(threading, "__excepthook__", None):
        threading.__excepthook__(args)


def _unraisable_hook(unraisable) -> None:
    exc = unraisable.exc_value
    _write_report(
        "unraisable_exception",
        str(exc),
        exc_type=getattr(unraisable.exc_type, "__name__", ""),
        exc_message=str(exc),
        stack="".join(traceback.format_exception(unraisable.exc_type, exc, unraisable.exc_traceback)),
        extra={"object": repr(unraisable.object), "err_msg": str(unraisable.err_msg or "")},
    )
    if getattr(sys, "__unraisablehook__", None):
        sys.__unraisablehook__(unraisable)


def _asyncio_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    exc = context.get("exception")
    message = str(context.get("message") or "asyncio exception")
    if _is_benign_asyncio_connection_reset(message, exc, context):
        logger.debug("Suppressed benign asyncio connection reset during pipe teardown: %s", exc)
        return
    if exc is not None:
        _write_report(
            "asyncio_exception",
            message,
            exc_type=type(exc).__name__,
            exc_message=str(exc),
            stack="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            extra={"context": _json_safe({k: v for k, v in context.items() if k != "exception"})},
        )
    else:
        _write_report("asyncio_exception", message, extra={"context": _json_safe(context)})
    loop.default_exception_handler(context)


def _is_benign_asyncio_connection_reset(
    message: str,
    exc: BaseException | None,
    context: dict[str, Any],
) -> bool:
    """Detect Windows Proactor pipe shutdown noise.

    On Windows, subprocess/pipe cleanup can raise ``ConnectionResetError`` from
    ``_ProactorBasePipeTransport._call_connection_lost`` after the remote side
    already closed.  It is teardown noise, not an app crash.
    """
    if not isinstance(exc, ConnectionResetError):
        return False
    handle_text = repr(context.get("handle", ""))
    text = f"{message} {handle_text} {exc}".lower()
    return (
        "_proactorbasepipetransport._call_connection_lost" in text
        or "forcibly closed by the remote host" in text
    )


async def _performance_monitor_loop(
    lag_warn_s: float,
    interval_s: float,
    memory_interval_s: float,
) -> None:
    last_memory = 0.0
    expected = time.monotonic() + interval_s
    while not _PERF_STOP.is_set():
        try:
            await asyncio.sleep(interval_s)
            now = time.monotonic()
            lag = max(0.0, now - expected)
            expected = now + interval_s
            if lag >= lag_warn_s:
                logger.warning("perf: event loop lag %.3fs", lag)
            if now - last_memory >= memory_interval_s:
                last_memory = now
                _log_memory_snapshot()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Performance monitor tick failed", exc_info=True)


def _log_memory_snapshot(label: str | None = None) -> None:
    try:
        import psutil

        proc = psutil.Process(os.getpid())
        mem = proc.memory_info()
        if label:
            logger.info(
                "perf: snapshot %s rss=%.1fMB vms=%.1fMB threads=%d",
                label,
                mem.rss / (1024 * 1024),
                mem.vms / (1024 * 1024),
                proc.num_threads(),
            )
        else:
            logger.info(
                "perf: memory rss=%.1fMB vms=%.1fMB threads=%d",
                mem.rss / (1024 * 1024),
                mem.vms / (1024 * 1024),
                proc.num_threads(),
            )
    except Exception:
        logger.debug("Memory snapshot unavailable", exc_info=True)


def _write_report(
    kind: str,
    message: str,
    *,
    exc_type: str = "",
    exc_message: str = "",
    stack: str = "",
    extra: dict[str, Any] | None = None,
) -> pathlib.Path | None:
    try:
        _CRASH_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        report = {
            "ts": now.isoformat(timespec="milliseconds"),
            "kind": kind,
            "session_id": _SESSION_ID,
            "pid": os.getpid(),
            "message": message,
            "exc_type": exc_type,
            "exc_message": exc_message,
            "stack": stack,
            "extra": _json_safe(extra or {}),
        }
        path = _CRASH_DIR / f"{now.strftime('%Y%m%d-%H%M%S')}-{kind}-{time.time_ns()}.json"
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return path
    except Exception:
        logger.debug("Could not write stability report", exc_info=True)
        return None


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except Exception:
        if isinstance(value, dict):
            return {str(k): _json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_json_safe(v) for v in value]
        return repr(value)
