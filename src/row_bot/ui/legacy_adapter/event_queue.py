"""Bounded FIFO transport for the retained NiceGUI renderer.

Execution and durable outcome ownership stay with the shared runtime. This
buffer never drops an accepted event or moves terminal events ahead of data.
"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from typing import Any


_DATA_EVENTS = {"token", "thinking", "thinking_token", "reasoning", "text_delta"}


def event_size(value: Any, *, maximum: int = 1024 * 1024) -> int:
    """Bound serialized-byte accounting without allocating a full event copy."""
    total = 0
    stack = [(value, 0)]
    seen: set[int] = set()
    visited = 0
    while stack:
        visited += 1
        if visited > 16384:
            raise ValueError("legacy_event_too_large")
        current, depth = stack.pop()
        if depth > 32:
            raise ValueError("legacy_event_invalid")
        if current is None or isinstance(current, (bool, int, float)):
            total += len(str(current)) + 2
        elif isinstance(current, str):
            total += 2
            for offset in range(0, len(current), 8192):
                # JSON's worst-case escaping keeps the bound conservative even
                # for control characters; ordinary unicode counts UTF-8 bytes.
                part = current[offset:offset + 8192]
                total += len(part.encode("utf-8", errors="replace"))
                total += sum(5 if ord(char) < 32 else 1 if char in '\\"' else 0 for char in part)
                if total > maximum:
                    raise ValueError("legacy_event_too_large")
        elif isinstance(current, (bytes, bytearray)):
            total += 2 + ((len(current) + 2) // 3) * 4
        elif isinstance(current, (dict, list, tuple)):
            if len(current) > 8192:
                raise ValueError("legacy_event_too_large")
            if id(current) in seen:
                raise ValueError("legacy_event_invalid")
            seen.add(id(current))
            total += 2 + len(current) * 2
            if total > maximum:
                raise ValueError("legacy_event_too_large")
            if isinstance(current, dict):
                for key, item in current.items():
                    stack.extend(((key, depth + 1), (item, depth + 1)))
            else:
                stack.extend((item, depth + 1) for item in current)
        else:
            raise ValueError("legacy_event_invalid")
        if total > maximum:
            raise ValueError("legacy_event_too_large")
    return total


class LegacyEventQueue(queue.Queue):
    """Reserve item and byte capacity for ordered semantic/control boundaries."""

    def __init__(self, maxsize: int = 256, *, max_bytes: int = 1024 * 1024,
                 reserved_items: int = 32, reserved_bytes: int = 128 * 1024,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 0 <= reserved_items < maxsize or not 0 <= reserved_bytes < max_bytes:
            raise ValueError("invalid_legacy_queue_limits")
        super().__init__(maxsize)
        self.max_bytes = max_bytes
        self.reserved_items = reserved_items
        self.reserved_bytes = reserved_bytes
        self._bytes = 0
        self._cancelled = False
        self._clock = clock

    @property
    def byte_size(self) -> int:
        with self.mutex:
            return self._bytes

    def _get(self) -> Any:
        item, size = self.queue.popleft()
        self._bytes -= size
        return item

    def full(self) -> bool:
        with self.mutex:
            return (self._qsize() >= self.maxsize - self.reserved_items
                    or self._bytes >= self.max_bytes - self.reserved_bytes)

    def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("'timeout' must be a non-negative number")
        data = (isinstance(item, tuple) and bool(item) and isinstance(item[0], str)
                and item[0] in _DATA_EVENTS)
        byte_limit = self.max_bytes - self.reserved_bytes if data else self.max_bytes
        size = event_size(item, maximum=byte_limit)
        item_limit = self.maxsize - self.reserved_items if data else self.maxsize
        deadline = self._clock() + timeout if timeout is not None else None
        with self.not_full:
            while True:
                if self._cancelled and data:
                    raise InterruptedError("legacy_queue_cancelled")
                if self._qsize() < item_limit and self._bytes + size <= byte_limit:
                    break
                if self._cancelled:
                    raise InterruptedError("legacy_queue_cancelled")
                if not block:
                    raise queue.Full
                remaining = None if deadline is None else deadline - self._clock()
                if remaining is not None and remaining <= 0:
                    raise queue.Full
                self.not_full.wait(remaining)
            self.queue.append((item, size))
            self._bytes += size
            self.unfinished_tasks += 1
            self.not_empty.notify()

    def cancel_pending_puts(self) -> None:
        """Unblock producers; retain queued events and remaining control reserve."""
        with self.not_full:
            self._cancelled = True
            self.not_full.notify_all()
