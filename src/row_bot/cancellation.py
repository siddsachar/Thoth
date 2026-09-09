"""Cooperative cancellation scopes for Row-Bot runtime work."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CancelCallback = Callable[[], None]

_CURRENT_SCOPE: contextvars.ContextVar["CancellationScope | None"] = contextvars.ContextVar(
    "row_bot_current_cancellation_scope",
    default=None,
)


@dataclass(frozen=True)
class _CallbackEntry:
    token: int
    callback: CancelCallback
    label: str


class CancellationScope:
    """Thread-safe cancellation token with best-effort cleanup callbacks."""

    def __init__(self, stop_event: threading.Event | None = None) -> None:
        self.stop_event = stop_event or threading.Event()
        self._lock = threading.RLock()
        self._callbacks: list[_CallbackEntry] = []
        self._next_token = 0
        self.reason = ""
        self.cancelled_at = 0.0
        self._callbacks_drained = False
        self.cleanup_failures: list[str] = []

    def is_cancelled(self) -> bool:
        return self.stop_event.is_set()

    def register(self, callback: CancelCallback, label: str = "") -> CancelCallback:
        """Register *callback* to run once when the scope is cancelled.

        Returns an unregister function. If the scope is already cancelled, the
        callback is invoked immediately and the unregister function becomes a
        no-op.
        """

        with self._lock:
            if self.is_cancelled():
                should_call_now = True
                token = -1
            else:
                self._next_token += 1
                token = self._next_token
                self._callbacks.append(_CallbackEntry(token, callback, label))
                should_call_now = False

        if should_call_now:
            self._run_callback(callback, label)

        def unregister() -> None:
            if token < 0:
                return
            with self._lock:
                self._callbacks = [entry for entry in self._callbacks if entry.token != token]

        return unregister

    def cancel(self, reason: str = "user") -> bool:
        """Cancel this scope and run registered cleanup callbacks.

        Returns True only for the first cancellation request.
        """

        import time

        with self._lock:
            if self._callbacks_drained:
                return False
            self._callbacks_drained = True
            self.reason = str(reason or "user")
            self.cancelled_at = time.perf_counter()
            self.stop_event.set()
            callbacks = list(self._callbacks)
            self._callbacks.clear()

        for entry in callbacks:
            self._run_callback(entry.callback, entry.label)
        return True

    def _run_callback(self, callback: CancelCallback, label: str) -> None:
        try:
            callback()
        except Exception:
            with self._lock:
                self.cleanup_failures.append("cleanup_failed")
            logger.debug("Cancellation callback failed%s", f" for {label}" if label else "", exc_info=True)


def current_cancellation_scope() -> CancellationScope | None:
    return _CURRENT_SCOPE.get()


@contextlib.contextmanager
def use_cancellation_scope(scope: CancellationScope | None) -> Iterator[None]:
    token = _CURRENT_SCOPE.set(scope)
    try:
        yield
    finally:
        _CURRENT_SCOPE.reset(token)

