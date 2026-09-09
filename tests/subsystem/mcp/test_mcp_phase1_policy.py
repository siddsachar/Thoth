from __future__ import annotations

import asyncio
import concurrent.futures
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.subsystem, pytest.mark.mcp_transport]


def test_browser_name_cannot_override_destructive_annotations():
    from row_bot.mcp_client.safety import is_destructive_tool
    for tool in (SimpleNamespace(annotations={"destructiveHint": True, "readOnlyHint": True}),
                 {"annotations": {"destructiveHint": True, "readOnlyHint": True}}):
        assert is_destructive_tool("browser_click", tool_obj=tool)
    assert is_destructive_tool("delete_file", tool_obj={"annotations": {"readOnlyHint": True}})


def test_bound_mcp_callable_rejects_disable_and_replacement(monkeypatch):
    from row_bot.mcp_client import runtime
    cfg = {"enabled": True, "servers": {"fixture": {"enabled": True, "tools": {"enabled": {"read": True}}}}}
    server = runtime.McpServerRuntime("fixture", {"tool_timeout": 1})
    monkeypatch.setattr(runtime, "_servers", {"fixture": server})
    monkeypatch.setattr(runtime, "_catalog", {"fixture": {"read": runtime.McpToolInfo("fixture", "read", "mcp_fixture_read", enabled=True)}})
    monkeypatch.setattr(runtime, "_get_effective_config", lambda: cfg)
    monkeypatch.setattr(runtime, "_schedule", lambda _: pytest.fail("revoked capability dispatched"))
    bound = runtime._make_tool_func("fixture", "read", enforce_policy=True)
    cfg["enabled"] = False
    with pytest.raises(RuntimeError, match="revoked"):
        bound()
    cfg["enabled"] = True
    runtime._servers["fixture"] = runtime.McpServerRuntime("fixture", {})
    with pytest.raises(RuntimeError, match="replaced"):
        bound()


def test_wrapper_bound_without_runtime_cannot_adopt_later_registration(monkeypatch):
    from row_bot.mcp_client import runtime
    monkeypatch.setattr(runtime, "_servers", {})
    monkeypatch.setattr(runtime, "_get_effective_config", lambda: {"enabled": True, "servers": {"fixture": {"enabled": True}}})
    bound = runtime._make_tool_func("fixture", "read", enforce_policy=True)
    runtime._servers["fixture"] = runtime.McpServerRuntime("fixture", {})
    with pytest.raises(RuntimeError, match="replaced"):
        bound()


def test_deadline_expired_while_waiting_lock_never_dispatches(monkeypatch):
    from row_bot.mcp_client import runtime
    calls = []
    now = [10.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    server = runtime.McpServerRuntime("fixture", {})
    async def call_tool(*args):
        calls.append(args)
    server.session = SimpleNamespace(call_tool=call_tool)
    class Lock:
        async def __aenter__(self):
            now[0] = 12
        async def __aexit__(self, *args):
            return False
    server._session_lock = Lock()
    with pytest.raises(TimeoutError):
        asyncio.run(server.call_tool("read", {}, deadline=11))
    assert not calls


def test_timeout_cancels_future_without_generation_scope():
    from row_bot.mcp_client import runtime
    future = concurrent.futures.Future()
    with pytest.raises(concurrent.futures.TimeoutError):
        runtime._future_result_with_generation_cancellation(future, timeout=0, stopped_message="stopped", label="fixture")
    assert future.cancelled()


def test_private_session_deadline_includes_lock_wait(monkeypatch):
    from row_bot.mcp_client import runtime
    now = [10.0]
    calls = []
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    private = runtime.PrivateMcpSession(command="fixture-never-executed", timeout=1)
    async def call_tool(*args):
        calls.append(args)
    private._session = SimpleNamespace(call_tool=call_tool)
    class Lock:
        async def __aenter__(self):
            now[0] = 12
        async def __aexit__(self, *args):
            return False
    private._session_lock = Lock()
    with pytest.raises(TimeoutError):
        asyncio.run(private._call_raw_async("read", {}, deadline=11))
    assert not calls


def test_late_discovery_cannot_replace_current_catalog(monkeypatch):
    from row_bot.mcp_client import runtime
    old = runtime.McpServerRuntime("fixture", {})
    newer = runtime.McpServerRuntime("fixture", {})
    async def list_tools():
        runtime._servers["fixture"] = newer
        return SimpleNamespace(tools=[])
    old.session = SimpleNamespace(list_tools=list_tools)
    current_catalog = {"current": object()}
    monkeypatch.setattr(runtime, "_servers", {"fixture": old})
    monkeypatch.setattr(runtime, "_catalog", {"fixture": current_catalog})
    asyncio.run(old._discover_tools())
    assert runtime._catalog["fixture"] is current_catalog
