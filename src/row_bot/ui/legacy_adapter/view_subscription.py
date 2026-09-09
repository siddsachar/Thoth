"""Per-page checkpoint invalidation over the shared execution projection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any


class LegacyViewSubscription:
    """Keep each viewer fresh even when another page populated the shared cache."""

    def __init__(self, projection: Any, load: Callable[[str], list[dict]],
                 apply: Callable[[str, list[dict]], None], *,
                 ready: Callable[[str], bool] = lambda _: True,
                 on_error: Callable[[str, str], None] | None = None) -> None:
        self._projection = projection
        self._load = load
        self._apply = apply
        self._ready = ready
        self._on_error = on_error
        self._loop = asyncio.get_running_loop()
        self._conversation_id = ""
        self._selection_epoch = 0
        self._checkpoint_revision = ""
        self._unsubscribe: Callable[[], None] | None = None
        self._task: asyncio.Task | None = None
        self._dirty = False
        self._closed = False
        self._deferred = False
        self._failure_key: tuple[int, str] | None = None
        self._failures = 0

    def observe(self, conversation_id: str | None) -> None:
        """Change subscriptions without querying the durable store on a timer."""
        target = str(conversation_id or "")
        if self._closed:
            return
        if target == self._conversation_id:
            if self._deferred:
                self._invalidate(target, "")
            return
        if self._unsubscribe:
            self._unsubscribe()
        self._conversation_id = target
        self._selection_epoch += 1
        self._checkpoint_revision = ""
        self._deferred = False
        self._unsubscribe = self._projection.subscribe(target, self._invalidate) if target else None
        if target:
            self._invalidate(target, "")

    def _invalidate(self, conversation_id: str, revision: str) -> None:
        if not self._closed and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._schedule, conversation_id)

    def _schedule(self, conversation_id: str) -> None:
        if self._closed or conversation_id != self._conversation_id:
            return
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = self._loop.create_task(self._drain())

    async def _drain(self) -> None:
        while self._dirty and not self._closed:
            self._dirty = False
            target = self._conversation_id
            selection_epoch = self._selection_epoch
            snapshot = self._projection.snapshot(target)
            checkpoint_revision = str(snapshot.get("checkpoint_revision") or "")
            generation = snapshot.get("generation") or {}
            if generation.get("status") in {"admitted", "running", "stopping"}:
                continue
            if not checkpoint_revision or checkpoint_revision == self._checkpoint_revision:
                continue
            failure_key = (selection_epoch, checkpoint_revision)
            if failure_key != self._failure_key:
                self._failure_key, self._failures = failure_key, 0
            if self._failures >= 3:
                continue
            if not self._ready(target):
                self._deferred = True
                continue
            self._deferred = False
            try:
                messages = await asyncio.to_thread(self._load, target)
                if not messages and snapshot.get("rows"):
                    raise ValueError("checkpoint_unavailable")
            except Exception:
                if self._closed or selection_epoch != self._selection_epoch:
                    continue
                self._failures += 1
                self._deferred = self._failures < 3
                if self._failures == 3:
                    logging.getLogger(__name__).warning("Conversation view refresh unavailable after bounded retries")
                    if self._on_error:
                        self._on_error(target, "checkpoint_unavailable")
                continue
            if self._closed or selection_epoch != self._selection_epoch:
                continue
            if not self._ready(target):
                self._deferred = True
                continue
            self._checkpoint_revision = checkpoint_revision
            self._apply(target, messages)

    def close(self) -> None:
        """Detach page observation without cancelling the shared producer."""
        self._closed = True
        self._selection_epoch += 1
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task and not self._task.done():
            self._task.cancel()

    def reconnect(self, conversation_id: str | None) -> None:
        """Reattach the same page after transport recovery with a fresh view cut."""
        if not self._closed:
            self.observe(conversation_id)
            return
        self._closed = False
        self._conversation_id = ""
        self._task = None
        self.observe(conversation_id)
