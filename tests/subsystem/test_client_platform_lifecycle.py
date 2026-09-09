from __future__ import annotations

import asyncio

import pytest

from row_bot.application.lifecycle import ApplicationLifecycle
from row_bot.runtime.executions import GenerationRuntimeRegistry

pytestmark = pytest.mark.subsystem


def test_startup_recovers_once_without_dispatch_and_shutdown_reports_real_quiescence():
    registry = GenerationRuntimeRegistry()
    trace = []

    async def inspector():
        trace.append("inspector_closed")

    lifecycle = ApplicationLifecycle(registry=registry, recover=lambda epoch: trace.append("recovered"),
                                     shutdown_inspector=inspector,
                                     close_live_content=lambda: trace.append("content_closed"))

    async def scenario():
        ready = await asyncio.gather(lifecycle.startup(), lifecycle.startup())
        assert ready[0] == ready[1]
        assert trace == ["recovered"]
        handle = registry.register("fixture")
        handle.cancel_scope.register(lambda: trace.append("cancel_requested"))
        pending = await lifecycle.shutdown(timeout=0)
        assert pending == {"status": "stopping", "pending_executions": [handle.execution_id]}
        assert trace == ["recovered", "cancel_requested", "inspector_closed"]
        assert not handle.producer_done.is_set()
        with pytest.raises(ValueError, match="runtime_closed"):
            registry.register("too-late")
        registry.finish(handle)
        assert (await lifecycle.shutdown(timeout=0))["status"] == "quiesced"
        assert trace[-1] == "content_closed"
        await lifecycle.shutdown(timeout=0)
        assert trace.count("content_closed") == 1
        assert trace.count("inspector_closed") == trace.count("cancel_requested") == 1
        with pytest.raises(RuntimeError, match="runtime_closed"):
            await lifecycle.startup()

    asyncio.run(scenario())


def test_shutdown_wait_budget_is_shared_across_producers(monkeypatch):
    from types import SimpleNamespace
    from row_bot.application import lifecycle as module

    now = [0.0]
    waits = []
    class Signal:
        def wait(self, duration):
            waits.append(duration)
            now[0] += duration
    handles = [SimpleNamespace(execution_id=f"fixture-{index}", producer_done=Signal()) for index in range(50)]
    registry = SimpleNamespace(shutdown=lambda: None, active=lambda: handles)
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    async def inspector():
        pass
    lifecycle = ApplicationLifecycle(registry=registry, shutdown_inspector=inspector)
    result = asyncio.run(lifecycle.shutdown(timeout=5))
    assert result["status"] == "stopping"
    assert waits == [5.0, *([0.0] * 49)]


def test_failed_recovery_does_not_advertise_ready_and_can_retry():
    registry = GenerationRuntimeRegistry()
    attempts = []

    def recover(epoch):
        attempts.append(epoch)
        if len(attempts) == 1:
            raise RuntimeError("fixture recovery unavailable")

    async def inspector():
        pass

    lifecycle = ApplicationLifecycle(registry=registry, recover=recover, shutdown_inspector=inspector)

    async def scenario():
        with pytest.raises(RuntimeError, match="fixture recovery unavailable"):
            await lifecycle.startup()
        assert not registry.active()
        assert (await lifecycle.startup())["status"] == "ready"
        assert len(attempts) == 2
        assert (await lifecycle.shutdown())["status"] == "quiesced"

    asyncio.run(scenario())
