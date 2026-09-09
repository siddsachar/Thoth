"""Legacy event transport, consuming the same headless projection as the API."""

from __future__ import annotations

import json
import uuid
import threading
import queue
from collections.abc import Callable
from typing import Any

from row_bot.application.client_platform import client_platform_service
from row_bot.runtime.executions import ExecutionHandle
from row_bot.ui.legacy_adapter.event_queue import LegacyEventQueue as BoundedEventQueue, event_size

_DISK_LOCK = threading.Lock()
_DISK_BYTES = 0


def launch_legacy_generation(handle: ExecutionHandle, event_queue: Any,
                             producer: Callable[[], None]) -> threading.Thread | None:
    """Own normal/resumed resource entry and retained renderer finalization."""
    def owned() -> None:
        status = "interrupted"
        try:
            client_platform_service.registry.check_dispatch(handle)
            from row_bot.conversation_resources import execution_context
            with execution_context(handle.conversation_id):
                producer()
            status = "stopped" if handle.cancel_scope.is_cancelled() else "waiting_approval" if handle.approval_id else "completed"
        except InterruptedError:
            if handle.cancel_scope.is_cancelled():
                status = "stopped"
            try:
                event_queue.put(None)
            except InterruptedError:
                pass
        except Exception:
            try:
                if not handle.cancel_scope.is_cancelled():
                    event_queue.put(("error", "The generation could not run."))
                event_queue.put(None)
            except InterruptedError:
                pass
        finally:
            client_platform_service.finish_execution(handle, status)

    def start_failed(_exc: BaseException) -> None:
        try:
            event_queue.put(("error", "The generation could not start."))
            event_queue.put(None)
        finally:
            client_platform_service.finish_execution(handle, "interrupted")
    try:
        return client_platform_service.registry.launch(handle, owned, on_entry_failure=start_failed)
    except (RuntimeError, OSError):
        if not handle.producer_done.is_set():
            raise
        return None


class LegacyEventQueue(BoundedEventQueue):
    """One renderer's bounded transport; it never consumes a shared replay cursor."""

    MAX_SPOOL_BYTES = 256 * 1024 * 1024
    MAX_GLOBAL_SPOOL_BYTES = 1024 * 1024 * 1024
    MAX_PENDING_SECONDS = 30.0

    def __init__(self, handle: ExecutionHandle) -> None:
        super().__init__(maxsize=256)
        self.handle = handle
        self._spools: dict[str, Any] = {}
        self._consumer_closed = False
        self._spool_lock = threading.Lock()
        handle.cancel_scope.register(self.cancel_pending_puts)

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        if self._consumer_closed:
            raise InterruptedError("legacy_consumer_closed")
        if item is not None and isinstance(item, tuple) and len(item) == 2:
            client_platform_service.observe_event(self.handle.conversation_id, item, self.handle)
            if item[0] == "interrupt" and self.handle.approval_id:
                payload = item[1]
                if isinstance(payload, dict):
                    payload = {**payload, "_platform_approval_id": self.handle.approval_id}
                elif isinstance(payload, list):
                    payload = [{**entry, "_platform_approval_id": self.handle.approval_id}
                               if isinstance(entry, dict) else entry for entry in payload]
                item = (item[0], payload)
        if isinstance(item, tuple) and item[0] in {"token", "thinking_token"} and isinstance(item[1], str):
            for offset in range(0, len(item[1]), 8192):
                self._put_bounded((item[0], item[1][offset:offset + 8192]), block=block, timeout=timeout)
            return
        spooled_key = None
        try:
            event_size(item, maximum=self.max_bytes)
        except ValueError as exc:
            if str(exc) != "legacy_event_too_large":
                raise
            from row_bot import threads
            from row_bot.thread_cleanup import resolve_managed_path
            from row_bot.projection.canonical import canonical_assistant_v1
            directory = resolve_managed_path(threads._MEDIA_DIR, self.handle.conversation_id)
            directory.mkdir(parents=True, exist_ok=True)
            key = uuid.uuid4().hex
            path = directory / f"runtime-event-{key}.json"
            reserved = 0
            global _DISK_BYTES
            try:
                with path.open("xb") as output:
                    for chunk in canonical_assistant_v1(item):
                        with _DISK_LOCK:
                            if (reserved + len(chunk) > self.MAX_SPOOL_BYTES
                                    or _DISK_BYTES + len(chunk) > self.MAX_GLOBAL_SPOOL_BYTES):
                                raise ValueError("legacy_spool_storage_limit")
                            reserved += len(chunk)
                            _DISK_BYTES += len(chunk)
                        output.write(chunk)
            except BaseException:
                path.unlink(missing_ok=True)
                with _DISK_LOCK:
                    _DISK_BYTES -= reserved
                raise
            with self._spool_lock:
                if self._consumer_closed:
                    self._discard_spool(path, reserved)
                    raise InterruptedError("legacy_consumer_closed")
                self._spools[key] = (path, reserved)
                spooled_key = key
            item = ("_platform_spool", {"reference": key})
        try:
            self._put_bounded(item, block=block, timeout=timeout)
        except BaseException:
            if spooled_key:
                with self._spool_lock:
                    retained = self._spools.pop(spooled_key, None)
                if retained:
                    self._discard_spool(*retained)
            raise

    def _put_bounded(self, item: Any, *, block: bool, timeout: float | None) -> None:
        if block:
            timeout = self.MAX_PENDING_SECONDS if timeout is None else min(timeout, self.MAX_PENDING_SECONDS)
            if self.handle.deadline is not None:
                timeout = min(timeout, max(0.0, self.handle.deadline - client_platform_service.registry.clock()))
        try:
            super().put(item, block=block, timeout=timeout)
        except queue.Full:
            if not block:
                raise
            client_platform_service.registry.cancel(self.handle, reason="backpressure")
            raise InterruptedError("legacy_queue_deadline") from None

    @staticmethod
    def _discard_spool(path: Any, size: int) -> None:
        global _DISK_BYTES
        try:
            path.unlink(missing_ok=True)
        finally:
            with _DISK_LOCK:
                _DISK_BYTES -= size

    def materialize(self, item: Any) -> Any:
        if isinstance(item, tuple) and item[0] == "_platform_spool":
            with self._spool_lock:
                path, size = self._spools.pop(item[1]["reference"])
            try:
                with path.open(encoding="utf-8") as source:
                    return tuple(json.load(source))
            finally:
                self._discard_spool(path, size)
        return item

    def close_consumer(self) -> None:
        with self._spool_lock:
            self._consumer_closed = True
            paths = list(self._spools.values())
            self._spools.clear()
        self.cancel_pending_puts()
        for path, size in paths:
            self._discard_spool(path, size)
