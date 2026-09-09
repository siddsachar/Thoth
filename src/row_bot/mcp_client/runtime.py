"""Isolated MCP server runtime and dynamic LangChain tool wrappers."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import datetime as _dt
import logging
import os
import threading
import time
import traceback
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, create_model

from row_bot.cancellation import current_cancellation_scope
from row_bot.mcp_client import config as mcp_config
from row_bot.mcp_client.logging import log_event, mask_mapping
from row_bot.mcp_client.requirements import apply_managed_runtime_env, missing_command_message, resolve_command
from row_bot.mcp_client.results import normalize_call_result
from row_bot.mcp_client.safety import is_destructive_tool, prefixed_tool_name, sanitize_name_component, tool_enabled_by_default

logger = logging.getLogger(__name__)

try:  # optional until requirements are installed
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except Exception:  # pragma: no cover - exercised in environments without mcp
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

try:
    from mcp.client.streamable_http import streamablehttp_client
except Exception:  # pragma: no cover
    streamablehttp_client = None

try:
    from mcp.client.sse import sse_client
except Exception:  # pragma: no cover
    sse_client = None


@dataclass
class McpToolInfo:
    server_name: str
    name: str
    prefixed_name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    enabled: bool = False
    destructive: bool = False
    requires_approval: bool = False
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpServerStatus:
    name: str
    enabled: bool = False
    status: str = "disabled"
    transport: str = "stdio"
    tool_count: int = 0
    enabled_tool_count: int = 0
    destructive_tool_count: int = 0
    last_error: str = ""
    last_connected_at: str = ""
    last_discovered_at: str = ""
    source: dict[str, Any] = field(default_factory=dict)


_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_runtime_lock = threading.RLock()
_servers: dict[str, "McpServerRuntime"] = {}
_catalog: dict[str, dict[str, McpToolInfo]] = {}
_statuses: dict[str, McpServerStatus] = {}


def _get_effective_config() -> dict[str, Any]:
    cfg = mcp_config.get_config()
    try:
        from row_bot.plugins.mcp import with_plugin_mcp_servers

        return with_plugin_mcp_servers(cfg)
    except Exception as exc:
        logger.debug("Plugin MCP overlay skipped: %s", exc, exc_info=True)
        return cfg


def _get_effective_server_config(name: str) -> dict[str, Any]:
    servers = _get_effective_config().get("servers", {})
    server = servers.get(name, {}) if isinstance(servers, dict) else {}
    return dict(server) if isinstance(server, dict) else {}


class McpStdioCommandNotFound(RuntimeError):
    pass


class PrivateMcpSession:
    """Dedicated stdio MCP connection that never enters the MCP catalog.

    This is for reviewed internal adapters such as Computer Use.  It reuses
    Row-Bot's MCP loop and SDK transport while deliberately skipping config,
    discovery, status, dynamic tool wrapping, and generic model exposure.
    """

    def __init__(
        self,
        *,
        command: str,
        args: list[str] | tuple[str, ...] = (),
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.command = str(command)
        self.args = tuple(str(arg) for arg in args)
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = float(timeout)
        self._session: Any = None
        self._exit_stack: AsyncExitStack | None = None
        self._session_lock: asyncio.Lock | None = None

    async def _open_async(self) -> None:
        if not sdk_available() or StdioServerParameters is None or stdio_client is None:
            raise RuntimeError("Python package 'mcp' with stdio support is not installed")
        if self._session is not None:
            return
        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=self.command,
                args=list(self.args),
                env=dict(self.env),
                cwd=self.cwd,
            )
            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await asyncio.wait_for(session.initialize(), timeout=min(self.timeout, 30.0))
        except BaseException:
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise
        self._exit_stack = stack
        self._session = session
        self._session_lock = asyncio.Lock()

    def open(self) -> None:
        _schedule(self._open_async()).result(timeout=min(self.timeout, 35.0))

    async def _call_raw_async(self, tool_name: str, arguments: dict[str, Any], *,
                              deadline: float | None = None) -> Any:
        if self._session is None or self._session_lock is None:
            raise RuntimeError("Private MCP session is not connected")
        deadline = deadline if deadline is not None else time.monotonic() + self.timeout
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP deadline expired")
        async with asyncio.timeout(remaining):
            async with self._session_lock:
                if time.monotonic() >= deadline:
                    raise TimeoutError("MCP deadline expired")
                if self._session is None:
                    raise RuntimeError("Private MCP session is not connected")
                return await self._session.call_tool(str(tool_name), dict(arguments or {}))

    def call_raw(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Return the SDK CallToolResult without generic text normalization."""

        scope = current_cancellation_scope()
        if scope is not None and scope.is_cancelled():
            raise concurrent.futures.CancelledError()
        deadline = time.monotonic() + self.timeout
        future = _schedule(self._call_raw_async(tool_name, dict(arguments or {}), deadline=deadline))
        unregister = scope.register(self.close, "private_mcp.close") if scope is not None else None
        try:
            while True:
                if scope is not None and scope.is_cancelled():
                    future.cancel()
                    raise concurrent.futures.CancelledError()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    future.cancel()
                    raise concurrent.futures.TimeoutError()
                try:
                    return future.result(timeout=min(0.1, remaining))
                except concurrent.futures.TimeoutError:
                    continue
        finally:
            if unregister is not None:
                unregister()

    async def _close_async(self) -> None:
        self._session = None
        self._session_lock = None
        stack = self._exit_stack
        self._exit_stack = None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    def close(self) -> None:
        if self._session is None and self._exit_stack is None:
            return
        future = _schedule(self._close_async())
        with contextlib.suppress(Exception):
            future.result(timeout=5.0)


class _GenericArgs(BaseModel):
    """Fallback for complex schemas: accept a JSON object as kwargs."""

    model_config = ConfigDict(extra="allow")


class _ResourceReadArgs(BaseModel):
    uri: str = Field(description="Resource URI to read from the MCP server.")


class _PromptGetArgs(BaseModel):
    name: str = Field(description="Prompt name to retrieve from the MCP server.")
    arguments: dict[str, Any] | None = Field(default=None, description="Optional prompt arguments.")


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def sdk_available() -> bool:
    return ClientSession is not None


def _missing_stdio_command_message(command: str) -> str:
    return missing_command_message(command)


def _resolve_stdio_command(command: str, env: dict[str, str]) -> str:
    expanded = os.path.expandvars(os.path.expanduser(command.strip()))
    if not expanded:
        raise RuntimeError("stdio MCP server requires a command")
    resolved, resolved_env, missing = resolve_command(expanded, env)
    if resolved:
        env.clear()
        env.update(resolved_env)
        return resolved
    raise McpStdioCommandNotFound(missing_command_message(command, missing))


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _thread
    with _runtime_lock:
        if _loop and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _thread = threading.Thread(target=_run, name="Row-Bot-MCP-Runtime", daemon=True)
        _thread.start()
        return _loop


def _schedule(coro: Any) -> concurrent.futures.Future:
    return asyncio.run_coroutine_threadsafe(coro, _ensure_loop())


def _future_result_with_generation_cancellation(
    future: concurrent.futures.Future,
    *,
    timeout: float,
    stopped_message: str,
    label: str,
) -> str:
    scope = current_cancellation_scope()
    if scope is None:
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise
    if scope.is_cancelled():
        future.cancel()
        return stopped_message
    unregister = scope.register(future.cancel, label)
    try:
        deadline = time.monotonic() + timeout
        while True:
            if scope.is_cancelled():
                future.cancel()
                return stopped_message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                future.cancel()
                raise concurrent.futures.TimeoutError()
            try:
                return future.result(timeout=min(0.1, remaining))
            except concurrent.futures.TimeoutError:
                continue
            except concurrent.futures.CancelledError:
                if scope.is_cancelled():
                    return stopped_message
                raise
    finally:
        unregister()


def _update_status(name: str, **updates: Any) -> None:
    with _runtime_lock:
        status = _statuses.get(name)
        if not status:
            server = _get_effective_server_config(name)
            status = McpServerStatus(
                name=name,
                enabled=bool(server.get("enabled")),
                transport=str(server.get("transport", "stdio")),
                source=dict(server.get("source") or {}),
            )
            _statuses[name] = status
        for key, value in updates.items():
            if hasattr(status, key):
                setattr(status, key, value)


def _tool_attr(tool: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(tool, dict) and name in tool:
            return tool.get(name)
        if hasattr(tool, name):
            return getattr(tool, name)
    return default


def _normalize_tools(server_name: str, server_cfg: dict[str, Any], tools: list[Any]) -> dict[str, McpToolInfo]:
    tool_cfg = server_cfg.get("tools", {}) if isinstance(server_cfg, dict) else {}
    saved_enabled = dict(tool_cfg.get("enabled") or {})
    include = set(tool_cfg.get("include") or [])
    exclude = set(tool_cfg.get("exclude") or [])
    approval_overrides = set(tool_cfg.get("require_approval") or [])
    normalized: dict[str, McpToolInfo] = {}
    for tool in tools:
        tool_name = str(_tool_attr(tool, "name", default="") or "").strip()
        if not tool_name:
            continue
        if include and tool_name not in include:
            continue
        if tool_name in exclude:
            continue
        description = str(_tool_attr(tool, "description", default="") or "")
        schema = _tool_attr(tool, "inputSchema", "input_schema", default={}) or {}
        destructive = is_destructive_tool(tool_name, description, tool)
        enabled = bool(saved_enabled.get(tool_name, tool_enabled_by_default(destructive)))
        requires = tool_name in approval_overrides or destructive
        normalized[tool_name] = McpToolInfo(
            server_name=server_name,
            name=tool_name,
            prefixed_name=prefixed_tool_name(server_name, tool_name),
            description=description or f"MCP tool {tool_name} from {server_name}",
            input_schema=schema if isinstance(schema, dict) else {},
            enabled=enabled,
            destructive=destructive,
            requires_approval=requires,
            source=dict(server_cfg.get("source") or {}),
        )
    return normalized


def _sync_catalog_from_config(config: dict[str, Any] | None = None) -> None:
    cfg = config or _get_effective_config()
    servers_cfg = cfg.get("servers", {}) if isinstance(cfg.get("servers"), dict) else {}
    with _runtime_lock:
        for server_name, tools in _catalog.items():
            server_cfg = servers_cfg.get(server_name, {}) if isinstance(servers_cfg.get(server_name, {}), dict) else {}
            tools_cfg = server_cfg.get("tools", {}) if isinstance(server_cfg.get("tools"), dict) else {}
            enabled_map = dict(tools_cfg.get("enabled") or {})
            approval_overrides = set(tools_cfg.get("require_approval") or [])
            for info in tools.values():
                info.enabled = bool(enabled_map.get(info.name, tool_enabled_by_default(info.destructive)))
                info.requires_approval = info.destructive or info.name in approval_overrides
            status = _statuses.get(server_name)
            if status:
                status.tool_count = len(tools)
                status.enabled_tool_count = sum(1 for info in tools.values() if info.enabled)
                status.destructive_tool_count = sum(1 for info in tools.values() if info.destructive)


def _json_schema_type(schema: dict[str, Any]) -> Any:
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), None)
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        items = schema.get("items")
        item_type = _json_schema_type(items) if isinstance(items, dict) else Any
        return list[item_type]
    if schema_type == "object":
        return dict[str, Any]
    return str if schema_type == "string" else Any


def _schema_to_model(tool_info: McpToolInfo) -> type[BaseModel]:
    schema = tool_info.input_schema or {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return _GenericArgs
    required = set(schema.get("required") or [])
    fields: dict[str, tuple[Any, Any]] = {}
    for field_name, spec in properties.items():
        if not isinstance(spec, dict) or not str(field_name).isidentifier():
            return _GenericArgs
        field_type = _json_schema_type(spec)
        default = ... if field_name in required else spec.get("default", None)
        fields[str(field_name)] = (field_type, Field(default, description=str(spec.get("description") or "")))
    try:
        return create_model(f"{tool_info.prefixed_name}_Args", **fields)
    except Exception:
        return _GenericArgs


class McpServerRuntime:
    def __init__(self, name: str, cfg: dict[str, Any]) -> None:
        self.name = name
        self.cfg = cfg
        self.session: Any = None
        self.exit_stack: AsyncExitStack | None = None
        self.stop_event: asyncio.Event | None = None
        self._session_lock = asyncio.Lock()

    async def start(self) -> None:
        if not sdk_available():
            _update_status(self.name, status="dependency_missing", last_error="Python package 'mcp' is not installed")
            return
        self.stop_event = asyncio.Event()
        _update_status(self.name, status="connecting", enabled=True, transport=self.cfg.get("transport", "stdio"), last_error="")
        try:
            await self._connect()
            await self._discover_tools()
            await self.stop_event.wait()
        except asyncio.CancelledError:
            raise
        except McpStdioCommandNotFound as exc:
            _update_status(self.name, status="dependency_missing", last_error=str(exc))
            log_event("mcp.server.dependency_missing", level=logging.WARNING, server=self.name, error=str(exc))
        except Exception as exc:
            _update_status(self.name, status="failed", last_error=str(exc))
            log_event("mcp.server.failed", level=logging.WARNING, server=self.name, error=str(exc), traceback=traceback.format_exc())
        finally:
            await self.close()
            with _runtime_lock:
                if _servers.get(self.name) is self:
                    _servers.pop(self.name, None)

    async def _connect(self) -> None:
        transport = str(self.cfg.get("transport") or "stdio")
        self.exit_stack = AsyncExitStack()
        if transport == "stdio":
            if StdioServerParameters is None or stdio_client is None:
                raise RuntimeError("MCP stdio transport is unavailable")
            command = str(self.cfg.get("command") or "").strip()
            if not command:
                raise RuntimeError("stdio MCP server requires a command")
            env = os.environ.copy()
            env.update({str(k): str(v) for k, v in dict(self.cfg.get("env") or {}).items()})
            env = apply_managed_runtime_env(self.cfg, env)
            command = _resolve_stdio_command(command, env)
            params = StdioServerParameters(
                command=command,
                args=[str(arg) for arg in self.cfg.get("args") or []],
                env=env,
                cwd=self.cfg.get("cwd") or None,
            )
            read_stream, write_stream = await self.exit_stack.enter_async_context(stdio_client(params))
        elif transport in {"streamable_http", "http", "streamable-http"}:
            if streamablehttp_client is None:
                raise RuntimeError("MCP Streamable HTTP transport is unavailable")
            url = str(self.cfg.get("url") or "").strip()
            if not url:
                raise RuntimeError("HTTP MCP server requires a URL")
            read_stream, write_stream, _ = await self.exit_stack.enter_async_context(
                streamablehttp_client(url, headers=dict(self.cfg.get("headers") or {}))
            )
        elif transport == "sse":
            if sse_client is None:
                raise RuntimeError("MCP SSE transport is unavailable")
            url = str(self.cfg.get("url") or "").strip()
            if not url:
                raise RuntimeError("SSE MCP server requires a URL")
            read_stream, write_stream = await self.exit_stack.enter_async_context(
                sse_client(url, headers=dict(self.cfg.get("headers") or {}))
            )
        else:
            raise RuntimeError(f"Unsupported MCP transport: {transport}")
        self.session = await self.exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
        await asyncio.wait_for(self.session.initialize(), timeout=float(self.cfg.get("connect_timeout", 30)))
        _update_status(self.name, status="connected", last_connected_at=_now(), last_error="")
        log_event("mcp.server.connected", server=self.name, transport=transport, cfg=mask_mapping(self.cfg))

    async def _discover_tools(self) -> None:
        if not self.session:
            return
        session = self.session
        result = await asyncio.wait_for(session.list_tools(), timeout=float(self.cfg.get("connect_timeout", 30)))
        tools = list(getattr(result, "tools", result if isinstance(result, list) else []))
        normalized = _normalize_tools(self.name, self.cfg, tools)
        with _runtime_lock:
            if (self.session is not session or (self.stop_event is not None and self.stop_event.is_set())
                    or _servers.get(self.name, self) is not self):
                return  # An old discovery callback cannot revive a replaced runtime.
            _catalog[self.name] = normalized
        _update_status(
            self.name,
            status="connected",
            tool_count=len(normalized),
            enabled_tool_count=sum(1 for info in normalized.values() if info.enabled),
            destructive_tool_count=sum(1 for info in normalized.values() if info.destructive),
            last_discovered_at=_now(),
            last_error="",
        )
        log_event("mcp.tools.discovered", server=self.name, tools=len(normalized))
        mcp_config.clear_agent_cache_if_loaded()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any], *,
                        deadline: float | None = None,
                        validate: Callable[[], None] | None = None) -> str:
        deadline = deadline if deadline is not None else time.monotonic() + float(self.cfg.get("tool_timeout", 120))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP deadline expired")
        async with asyncio.timeout(remaining):
            async with self._session_lock:
                if validate is not None:
                    validate()
                if deadline <= time.monotonic():
                    raise TimeoutError("MCP deadline expired")
                if not self.session:
                    raise RuntimeError(f"MCP server '{self.name}' is not connected")
                output_limit = int(self.cfg.get("output_limit", 24000))
                log_event("mcp.tool.call", server=self.name, tool=tool_name)
                result = await self.session.call_tool(tool_name, arguments or {})
                return normalize_call_result(result, output_limit=output_limit)

    async def list_resources(self) -> str:
        if not self.session:
            raise RuntimeError(f"MCP server '{self.name}' is not connected")
        if not hasattr(self.session, "list_resources"):
            return "This MCP server/session does not expose resource listing."
        result = await asyncio.wait_for(self.session.list_resources(), timeout=float(self.cfg.get("tool_timeout", 120)))
        resources = list(getattr(result, "resources", []) or [])
        if not resources:
            return "No MCP resources found."
        lines = ["MCP resources:"]
        for resource in resources:
            uri = getattr(resource, "uri", "")
            name = getattr(resource, "name", "") or uri
            description = getattr(resource, "description", "") or ""
            lines.append(f"- {name}: {uri}" + (f" — {description}" if description else ""))
        return "\n".join(lines)

    async def read_resource(self, uri: str) -> str:
        if not self.session:
            raise RuntimeError(f"MCP server '{self.name}' is not connected")
        result = await asyncio.wait_for(self.session.read_resource(uri), timeout=float(self.cfg.get("tool_timeout", 120)))
        contents = list(getattr(result, "contents", []) or [])
        if not contents:
            return "MCP resource returned no content."
        parts: list[str] = []
        for item in contents:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
            else:
                mime = getattr(item, "mimeType", "") or getattr(item, "mime_type", "") or "binary"
                parts.append(f"[MCP resource content omitted: {mime}]")
        output = "\n\n".join(parts)
        limit = int(self.cfg.get("output_limit", 24000))
        if len(output) > limit:
            output = output[:limit] + f"\n\n[Truncated MCP resource at {limit} characters]"
        return output

    async def list_prompts(self) -> str:
        if not self.session:
            raise RuntimeError(f"MCP server '{self.name}' is not connected")
        if not hasattr(self.session, "list_prompts"):
            return "This MCP server/session does not expose prompt listing."
        result = await asyncio.wait_for(self.session.list_prompts(), timeout=float(self.cfg.get("tool_timeout", 120)))
        prompts = list(getattr(result, "prompts", []) or [])
        if not prompts:
            return "No MCP prompts found."
        lines = ["MCP prompts:"]
        for prompt in prompts:
            name = getattr(prompt, "name", "")
            description = getattr(prompt, "description", "") or ""
            lines.append(f"- {name}" + (f": {description}" if description else ""))
        return "\n".join(lines)

    async def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if not self.session:
            raise RuntimeError(f"MCP server '{self.name}' is not connected")
        result = await asyncio.wait_for(self.session.get_prompt(name, arguments or {}), timeout=float(self.cfg.get("tool_timeout", 120)))
        messages = list(getattr(result, "messages", []) or [])
        description = getattr(result, "description", "") or ""
        parts = [description] if description else []
        for message in messages:
            role = getattr(message, "role", "message")
            content = getattr(message, "content", "")
            text = getattr(content, "text", None) if content is not None else None
            parts.append(f"[{role}] {text if text is not None else content}")
        return "\n\n".join(parts) or "MCP prompt returned no messages."

    async def close(self) -> None:
        self.session = None
        if self.exit_stack:
            with contextlib.suppress(Exception):
                await self.exit_stack.aclose()
        self.exit_stack = None
        with _runtime_lock:
            current_status = _statuses.get(self.name)
            preserve_status = current_status and current_status.status in {"failed", "dependency_missing"}
        if not preserve_status:
            _update_status(self.name, status="stopped")

    async def stop(self) -> None:
        if self.stop_event and not self.stop_event.is_set():
            self.stop_event.set()
        await self.close()


def discover_enabled_servers() -> None:
    """Start or refresh enabled MCP servers without blocking startup."""
    cfg = _get_effective_config()
    if not cfg.get("enabled"):
        with _runtime_lock:
            running_names = list(_servers)
        for name in running_names:
            stop_server(name)
        with _runtime_lock:
            _catalog.clear()
            for name, server_cfg in cfg.get("servers", {}).items():
                _statuses[name] = McpServerStatus(name=name, enabled=bool(server_cfg.get("enabled")), status="global_disabled")
        return
    if not sdk_available():
        log_event("mcp.dependency_missing", level=logging.WARNING, package="mcp")
        for name, server_cfg in cfg.get("servers", {}).items():
            if server_cfg.get("enabled"):
                _update_status(name, enabled=True, status="dependency_missing", last_error="Python package 'mcp' is not installed")
        return
    desired = {name: server for name, server in cfg.get("servers", {}).items() if server.get("enabled")}
    with _runtime_lock:
        existing = set(_servers)
    for stale in existing - set(desired):
        stop_server(stale)
    for name, server_cfg in desired.items():
        with _runtime_lock:
            running = name in _servers
            status = _statuses.get(name)
            failed_until_refresh = status is not None and status.status == "failed"
        if failed_until_refresh:
            continue
        if running:
            continue
        runtime = McpServerRuntime(name, server_cfg)
        with _runtime_lock:
            _servers[name] = runtime
            _statuses[name] = McpServerStatus(
                name=name,
                enabled=True,
                status="connecting",
                transport=str(server_cfg.get("transport", "stdio")),
                source=dict(server_cfg.get("source") or {}),
            )
        _schedule(runtime.start())


def stop_server(name: str) -> None:
    with _runtime_lock:
        runtime = _servers.pop(name, None)
        _catalog.pop(name, None)
    if runtime:
        future = _schedule(runtime.stop())
        with contextlib.suppress(Exception):
            future.result(timeout=5)


def refresh_server(name: str) -> None:
    stop_server(name)
    with _runtime_lock:
        _statuses.pop(name, None)
    discover_enabled_servers()


def shutdown() -> None:
    """Stop MCP child sessions and runtime loop. Safe to call repeatedly."""
    global _loop, _thread
    with _runtime_lock:
        names = list(_servers)
    for name in names:
        stop_server(name)
    loop = _loop
    if loop and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    _loop = None
    _thread = None
    log_event("mcp.runtime.shutdown")


async def probe_server_async(name: str, server_cfg: dict[str, Any]) -> dict[str, Any]:
    """Connect to a server temporarily and return discovered tools/status."""
    runtime = McpServerRuntime(name, server_cfg)
    try:
        await runtime._connect()
        result = await asyncio.wait_for(runtime.session.list_tools(), timeout=float(server_cfg.get("connect_timeout", 30)))
        tools = list(getattr(result, "tools", result if isinstance(result, list) else []))
        normalized = _normalize_tools(name, server_cfg, tools)
        return {
            "ok": True,
            "tools": [info.__dict__ for info in normalized.values()],
            "tool_count": len(normalized),
            "destructive_tool_count": sum(1 for info in normalized.values() if info.destructive),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "tools": []}
    finally:
        await runtime.close()


def probe_server(name: str, server_cfg: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    if not sdk_available():
        return {"ok": False, "error": "Python package 'mcp' is not installed", "tools": []}
    future = _schedule(probe_server_async(name, server_cfg))
    wait_seconds = timeout or float(server_cfg.get("connect_timeout", 30)) + 5
    try:
        return future.result(timeout=wait_seconds)
    except concurrent.futures.CancelledError:
        return {
            "ok": False,
            "error": "MCP connection ended before the server completed its handshake.",
            "tools": [],
        }
    except concurrent.futures.TimeoutError:
        future.cancel()
        return {
            "ok": False,
            "error": f"MCP connection timed out after {wait_seconds:g} seconds.",
            "tools": [],
        }


def _validate_bound_runtime(server_name: str, expected: Any, *, tool_name: str = "",
                            feature: str = "") -> None:
    cfg = _get_effective_config()
    server_cfg = cfg.get("servers", {}).get(server_name, {})
    if not cfg.get("enabled") or not server_cfg.get("enabled"):
        raise RuntimeError("MCP capability was revoked")
    with _runtime_lock:
        if _servers.get(server_name) is not expected:
            raise RuntimeError("MCP registration was replaced")
        if tool_name:
            info = _catalog.get(server_name, {}).get(tool_name)
            options = server_cfg.get("tools", {})
            if (info is None or tool_name in options.get("exclude", [])
                    or (options.get("include") and tool_name not in options["include"])
                    or not options.get("enabled", {}).get(tool_name, tool_enabled_by_default(info.destructive))):
                raise RuntimeError("MCP capability was revoked")
        if feature and not server_cfg.get("tools", {}).get(feature):
            raise RuntimeError("MCP capability was revoked")


def _call_tool_sync(server_name: str, tool_name: str, kwargs: dict[str, Any], *, expected: Any = None) -> str:
    with _runtime_lock:
        runtime = _servers.get(server_name)
    if not runtime:
        raise RuntimeError(f"MCP server '{server_name}' is not running")
    timeout = float(runtime.cfg.get("tool_timeout", 120))
    deadline = time.monotonic() + timeout
    validate = None
    if expected is not None:
        validate = lambda: _validate_bound_runtime(server_name, expected, tool_name=tool_name)
        validate()
    future = _schedule(runtime.call_tool(tool_name, kwargs, deadline=deadline, validate=validate))
    return _future_result_with_generation_cancellation(
        future,
        timeout=max(0, deadline - time.monotonic()),
        stopped_message="MCP tool call stopped by user.",
        label=f"mcp_tool.{server_name}.{tool_name}.cancel",
    )


def _make_tool_func(server_name: str, tool_name: str, *, enforce_policy: bool = False) -> Callable[..., str]:
    with _runtime_lock:
        expected = _servers.get(server_name, object()) if enforce_policy else None
    def _run(**kwargs: Any) -> str:
        return _call_tool_sync(server_name, tool_name, kwargs, expected=expected)

    return _run


async def _authorized_operation(server_name: str, expected: Any, feature: str,
                                operation: Callable[[], Any], deadline: float) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("MCP deadline expired")
    async with asyncio.timeout(remaining):
        if expected is not None:
            _validate_bound_runtime(server_name, expected, feature=feature)
        return await operation()


def _make_resource_list_func(server_name: str, *, enforce_policy: bool = False) -> Callable[[], str]:
    with _runtime_lock:
        expected = _servers.get(server_name, object()) if enforce_policy else None
    def _run() -> str:
        with _runtime_lock:
            runtime = _servers.get(server_name)
        if not runtime:
            raise RuntimeError(f"MCP server '{server_name}' is not running")
        deadline = time.monotonic() + float(runtime.cfg.get("tool_timeout", 120))
        future = _schedule(_authorized_operation(server_name, expected, "resources_enabled", runtime.list_resources, deadline))
        return _future_result_with_generation_cancellation(
            future,
            timeout=max(0, deadline - time.monotonic()),
            stopped_message="MCP resource listing stopped by user.",
            label=f"mcp_resources.{server_name}.cancel",
        )
    return _run


def _make_resource_read_func(server_name: str, *, enforce_policy: bool = False) -> Callable[..., str]:
    with _runtime_lock:
        expected = _servers.get(server_name, object()) if enforce_policy else None
    def _run(uri: str) -> str:
        with _runtime_lock:
            runtime = _servers.get(server_name)
        if not runtime:
            raise RuntimeError(f"MCP server '{server_name}' is not running")
        deadline = time.monotonic() + float(runtime.cfg.get("tool_timeout", 120))
        future = _schedule(_authorized_operation(server_name, expected, "resources_enabled", lambda: runtime.read_resource(uri), deadline))
        return _future_result_with_generation_cancellation(
            future,
            timeout=max(0, deadline - time.monotonic()),
            stopped_message="MCP resource read stopped by user.",
            label=f"mcp_resource.{server_name}.cancel",
        )
    return _run


def _make_prompt_list_func(server_name: str, *, enforce_policy: bool = False) -> Callable[[], str]:
    with _runtime_lock:
        expected = _servers.get(server_name, object()) if enforce_policy else None
    def _run() -> str:
        with _runtime_lock:
            runtime = _servers.get(server_name)
        if not runtime:
            raise RuntimeError(f"MCP server '{server_name}' is not running")
        deadline = time.monotonic() + float(runtime.cfg.get("tool_timeout", 120))
        future = _schedule(_authorized_operation(server_name, expected, "prompts_enabled", runtime.list_prompts, deadline))
        return _future_result_with_generation_cancellation(
            future,
            timeout=max(0, deadline - time.monotonic()),
            stopped_message="MCP prompt listing stopped by user.",
            label=f"mcp_prompts.{server_name}.cancel",
        )
    return _run


def _make_prompt_get_func(server_name: str, *, enforce_policy: bool = False) -> Callable[..., str]:
    with _runtime_lock:
        expected = _servers.get(server_name, object()) if enforce_policy else None
    def _run(name: str, arguments: dict[str, Any] | None = None) -> str:
        with _runtime_lock:
            runtime = _servers.get(server_name)
        if not runtime:
            raise RuntimeError(f"MCP server '{server_name}' is not running")
        deadline = time.monotonic() + float(runtime.cfg.get("tool_timeout", 120))
        future = _schedule(_authorized_operation(server_name, expected, "prompts_enabled", lambda: runtime.get_prompt(name, arguments), deadline))
        return _future_result_with_generation_cancellation(
            future,
            timeout=max(0, deadline - time.monotonic()),
            stopped_message="MCP prompt read stopped by user.",
            label=f"mcp_prompt.{server_name}.cancel",
        )
    return _run


def _allow_names_set(allow_names: Iterable[str] | None) -> set[str] | None:
    if allow_names is None:
        return None
    return {str(name) for name in allow_names if str(name or "").strip()}


def _mcp_runtime_name_allowed(name: str, allow: set[str] | None) -> bool:
    return allow is None or "mcp" in allow or str(name or "") in allow


def _source_plugin_allowed(info: McpToolInfo, plugin_id: str | None) -> bool:
    if plugin_id is None:
        return True
    source = info.source or {}
    return source.get("kind") == "plugin" and source.get("plugin_id") == plugin_id


def _server_source_plugin_allowed(server_cfg: dict[str, Any], plugin_id: str | None) -> bool:
    if plugin_id is None:
        return True
    source = server_cfg.get("source", {}) if isinstance(server_cfg, dict) else {}
    return source.get("kind") == "plugin" and source.get("plugin_id") == plugin_id


def get_langchain_tools(
    allow_names: Iterable[str] | None = None,
    *,
    source_plugin_id: str | None = None,
    refresh: bool = True,
) -> list[StructuredTool]:
    cfg = _get_effective_config()
    if not cfg.get("enabled"):
        return []
    allow = _allow_names_set(allow_names)
    if refresh:
        discover_enabled_servers()
    _sync_catalog_from_config(cfg)
    wrappers: list[StructuredTool] = []
    with _runtime_lock:
        infos = [
            info for tools in _catalog.values() for info in tools.values()
            if info.enabled and _source_plugin_allowed(info, source_plugin_id)
        ]
    for info in infos:
        if not _mcp_runtime_name_allowed(info.prefixed_name, allow):
            continue
        try:
            wrappers.append(StructuredTool.from_function(
                func=_make_tool_func(info.server_name, info.name, enforce_policy=True),
                name=info.prefixed_name,
                description=f"External MCP tool from server '{info.server_name}'. {info.description}",
                args_schema=_schema_to_model(info),
            ))
        except Exception as exc:
            log_event("mcp.tool.wrap_failed", level=logging.WARNING, server=info.server_name, tool=info.name, error=str(exc))
    for server_name, server_cfg in cfg.get("servers", {}).items():
        if not _server_source_plugin_allowed(server_cfg, source_plugin_id):
            continue
        if server_name not in _servers:
            continue
        tools_cfg = server_cfg.get("tools", {})
        safe_server = sanitize_name_component(server_name)
        if tools_cfg.get("resources_enabled"):
            list_name = f"mcp_{safe_server}_list_resources"
            read_name = f"mcp_{safe_server}_read_resource"
            if _mcp_runtime_name_allowed(list_name, allow):
                wrappers.append(StructuredTool.from_function(
                    func=_make_resource_list_func(server_name, enforce_policy=True),
                    name=list_name,
                    description=f"List resources exposed by MCP server '{server_name}'.",
                ))
            if _mcp_runtime_name_allowed(read_name, allow):
                wrappers.append(StructuredTool.from_function(
                    func=_make_resource_read_func(server_name, enforce_policy=True),
                    name=read_name,
                    description=f"Read a resource URI from MCP server '{server_name}'.",
                    args_schema=_ResourceReadArgs,
                ))
        if tools_cfg.get("prompts_enabled"):
            list_name = f"mcp_{safe_server}_list_prompts"
            get_name = f"mcp_{safe_server}_get_prompt"
            if _mcp_runtime_name_allowed(list_name, allow):
                wrappers.append(StructuredTool.from_function(
                    func=_make_prompt_list_func(server_name, enforce_policy=True),
                    name=list_name,
                    description=f"List prompts exposed by MCP server '{server_name}'.",
                ))
            if _mcp_runtime_name_allowed(get_name, allow):
                wrappers.append(StructuredTool.from_function(
                    func=_make_prompt_get_func(server_name, enforce_policy=True),
                    name=get_name,
                    description=f"Get a prompt from MCP server '{server_name}'.",
                    args_schema=_PromptGetArgs,
                ))
    return wrappers


def get_plugin_langchain_tools(
    plugin_id: str,
    allow_names: Iterable[str] | None = None,
    *,
    refresh: bool = True,
) -> list[StructuredTool]:
    return get_langchain_tools(
        allow_names=allow_names,
        source_plugin_id=plugin_id,
        refresh=refresh,
    )


def get_destructive_tool_names(
    allow_names: Iterable[str] | None = None,
    *,
    source_plugin_id: str | None = None,
) -> set[str]:
    allow = _allow_names_set(allow_names)
    _sync_catalog_from_config()
    with _runtime_lock:
        return {
            info.prefixed_name
            for tools in _catalog.values()
            for info in tools.values()
            if (
                info.enabled
                and info.requires_approval
                and _source_plugin_allowed(info, source_plugin_id)
                and _mcp_runtime_name_allowed(info.prefixed_name, allow)
            )
        }


def get_plugin_destructive_tool_names(
    plugin_id: str,
    allow_names: Iterable[str] | None = None,
) -> set[str]:
    return get_destructive_tool_names(allow_names=allow_names, source_plugin_id=plugin_id)


def get_plugin_tool_records(plugin_id: str) -> list[dict[str, Any]]:
    _sync_catalog_from_config()
    with _runtime_lock:
        infos = [
            info for tools in _catalog.values() for info in tools.values()
            if info.enabled and _source_plugin_allowed(info, plugin_id)
        ]
    records: list[dict[str, Any]] = []
    for info in infos:
        source = dict(info.source or {})
        records.append({
            "runtime_name": info.prefixed_name,
            "parent_name": source.get("server_id") or info.server_name,
            "plugin_id": plugin_id,
            "plugin_name": source.get("plugin_name") or plugin_id,
            "tags": ["mcp"],
            "label": info.name,
            "description": info.description,
            "destructive": info.requires_approval,
            "source": "mcp",
            "server_name": info.server_name,
        })
    return records


def get_catalog_snapshot() -> dict[str, list[dict[str, Any]]]:
    _sync_catalog_from_config()
    with _runtime_lock:
        return {
            server: [info.__dict__.copy() for info in tools.values()]
            for server, tools in _catalog.items()
        }


def get_status_summary() -> dict[str, Any]:
    cfg = _get_effective_config()
    _sync_catalog_from_config(cfg)
    with _runtime_lock:
        statuses = {name: status.__dict__.copy() for name, status in _statuses.items()}
        catalog = {
            server: [info.__dict__.copy() for info in tools.values()]
            for server, tools in _catalog.items()
        }
    for name, server_cfg in cfg.get("servers", {}).items():
        statuses.setdefault(name, McpServerStatus(
            name=name,
            enabled=bool(server_cfg.get("enabled")),
            status="disabled" if not server_cfg.get("enabled") else "not_started",
            transport=str(server_cfg.get("transport", "stdio")),
            source=dict(server_cfg.get("source") or {}),
        ).__dict__.copy())
    return {
        "enabled": bool(cfg.get("enabled")),
        "sdk_available": sdk_available(),
        "server_count": len(cfg.get("servers", {})),
        "enabled_server_count": sum(1 for server in cfg.get("servers", {}).values() if server.get("enabled")),
        "connected_server_count": sum(1 for status in statuses.values() if status.get("status") == "connected"),
        "tool_count": sum(len(tools) for tools in catalog.values()),
        "enabled_tool_count": sum(1 for tools in catalog.values() for info in tools if info.get("enabled")),
        "destructive_tool_count": sum(1 for tools in catalog.values() for info in tools if info.get("requires_approval")),
        "servers": statuses,
        "tools": catalog,
    }
