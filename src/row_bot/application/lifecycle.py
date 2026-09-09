"""Explicit process lifecycle for the shared client-platform runtime.

Existing application startup still owns unrelated optional subsystems. This
composition owns admission recovery, execution shutdown and Inspector scheduling
for both the headless host and the NiceGUI compatibility host.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from row_bot.runtime import admissions
from row_bot.runtime.executions import generation_registry

logger = logging.getLogger(__name__)


async def _shutdown_inspector() -> None:
    from row_bot.developer.inspector_snapshot import shutdown_snapshot_refreshes

    await shutdown_snapshot_refreshes()


def _close_live_content() -> None:
    from row_bot.application.live_content import close
    close()


class ApplicationLifecycle:
    """Recover durable facts before commands, then stop producers before views."""

    def __init__(self, *, registry: Any = generation_registry,
                 recover: Callable[[str], Any] = admissions.recover,
                 shutdown_inspector: Callable[[], Awaitable[None]] = _shutdown_inspector,
                 close_live_content: Callable[[], None] = _close_live_content) -> None:
        self.registry = registry
        self._recover = recover
        self._shutdown_inspector = shutdown_inspector
        self._close_live_content = close_live_content
        self._lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._inspector_closed = False
        self._content_closed = False

    async def startup(self) -> dict[str, str]:
        """Reconcile prior process facts without restarting any external call."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("runtime_closed")
            if not self._started:
                await asyncio.to_thread(self._recover, self.registry.server_epoch)
                self._started = True
            return {"status": "ready", "server_epoch": self.registry.server_epoch}

    async def shutdown(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Request cancellation and report actual quiescence within a bounded wait."""
        async with self._lock:
            self._closed = True
            self.registry.shutdown()
            handles = self.registry.active()
            if handles:
                def wait_for_producers() -> None:
                    deadline = time.monotonic() + max(0.0, timeout)
                    for handle in handles:
                        handle.producer_done.wait(max(0.0, deadline - time.monotonic()))
                await asyncio.to_thread(wait_for_producers)
            if not self._inspector_closed:
                await self._shutdown_inspector()
                self._inspector_closed = True
            pending = self.registry.active()
            if pending:
                logger.warning("Headless shutdown has %d producers still stopping", len(pending))
            elif not self._content_closed:
                self._close_live_content()
                self._content_closed = True
            return {"status": "stopping" if pending else "quiesced",
                    "pending_executions": [handle.execution_id for handle in pending]}


application_lifecycle = ApplicationLifecycle()
