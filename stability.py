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
    logger.warning("Client-side error reported: %s", msg)
    return _write_report("client_error", msg, extra={"client": safe_payload})


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
