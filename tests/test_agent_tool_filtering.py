from __future__ import annotations

import json
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from row_bot.providers.models import TransportMode
from row_bot.providers.tool_schema import ToolSchemaCompatibilityError


@pytest.fixture(autouse=True)
def _restore_agent_context_after_filtering_test():
    import row_bot.agent as agent

    # Graph preparation and the forced-resume case mutate execution context as
    # well as approval mode. Restore every original binding, including unset
    # bindings, so a child-resume fixture cannot affect a later workflow test.
    variables = {value for value in vars(agent).values() if isinstance(value, ContextVar)}
    tokens = [(variable, variable.set(variable.get(None))) for variable in variables]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def _bound_tools(graph: SimpleNamespace) -> dict[str, BaseTool]:
    from langgraph.prebuilt import ToolNode

    assert isinstance(graph.tools, ToolNode)
    return graph.tools.tools_by_name


def _lc_tool(name: str) -> StructuredTool:
    def _run(query: str = "") -> str:
        return f"{name}:{query}"

    return StructuredTool.from_function(
        func=_run,
        name=name,
        description=f"{name} test tool",
    )


def _malformed_array_tool(name: str) -> StructuredTool:
    class _Args(BaseModel):
        values: list[Any] = Field(default_factory=list)

    def _run(values: list[Any] | None = None) -> str:
        return f"{name}:{values or []}"

    return StructuredTool.from_function(
        func=_run,
        name=name,
        description=f"{name} malformed schema test tool",
        args_schema=_Args,
    )


def _prepare_graph(monkeypatch):
    import row_bot.agent as agent

    agent.clear_agent_cache()
    agent._approval_mode_var.set("allow_all")
    monkeypatch.setattr(agent, "_build_runtime_skill_snapshot", lambda: ((), (), False, "test-skills"))
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:test")
    monkeypatch.setattr(agent, "get_llm", lambda: object())
    monkeypatch.setattr(agent, "get_context_size", lambda model_name=None: 32_768)
    monkeypatch.setattr(agent, "get_agent_system_prompt", lambda: "system")
    monkeypatch.setattr(
        agent,
        "_ensure_agent_mode_ready",
        lambda model_name: SimpleNamespace(
            provider_id="test",
            runtime_model="test",
            capability_source="test",
            confidence="high",
        ),
    )
    monkeypatch.setattr(agent, "create_react_agent", lambda **kwargs: SimpleNamespace(**kwargs))
    return agent


def test_get_agent_graph_without_allowlist_progressively_exposes_plugin_and_channel_tools(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    core_tool = _lc_tool("filesystem")
    plugin_tool = _lc_tool("plugin_lookup")
    channel_tool = _lc_tool("channel_send")
    plugin_allow_args = []

    monkeypatch.setattr(
        agent.tool_registry,
        "get_tool",
        lambda name: SimpleNamespace(
            as_langchain_tools=lambda: [core_tool],
            destructive_tool_names=set(),
        ) if name == "filesystem" else None,
    )

    from row_bot.plugins import registry as plugin_registry
    from row_bot.channels import registry as channel_registry
    from row_bot.channels import tool_factory

    def fake_plugin_tools(allow_names=None):
        plugin_allow_args.append(allow_names)
        return [plugin_tool]

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", fake_plugin_tools)
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())
    monkeypatch.setattr(channel_registry, "running_channels", lambda: [SimpleNamespace(name="sms")])
    monkeypatch.setattr(tool_factory, "create_channel_tools", lambda channel: [channel_tool])

    graph = agent.get_agent_graph(["filesystem"])

    assert list(_bound_tools(graph)) == ["filesystem", "tool_search", "tool_invoke"]
    search = _bound_tools(graph)["tool_search"]
    results = json.loads(search.invoke({"query": "lookup", "limit": 5}))["results"]
    assert [result["name"] for result in results] == ["plugin_lookup"]
    channel_results = json.loads(search.invoke({"query": "channel", "limit": 5}))["results"]
    assert [result["name"] for result in channel_results] == ["channel_send"]
    assert plugin_allow_args == [None]


def test_get_agent_graph_with_allowlist_filters_plugin_and_channel_tools(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    core_tool = _lc_tool("filesystem")
    plugin_tool = _lc_tool("plugin_lookup")
    plugin_allow_args = []

    monkeypatch.setattr(
        agent.tool_registry,
        "get_tool",
        lambda name: SimpleNamespace(
            as_langchain_tools=lambda: [core_tool],
            destructive_tool_names=set(),
        ) if name == "filesystem" else None,
    )

    from row_bot.plugins import registry as plugin_registry
    from row_bot.channels import registry as channel_registry
    from row_bot.channels import tool_factory

    def fake_plugin_tools(allow_names=None):
        allow = set(allow_names or [])
        plugin_allow_args.append(allow)
        return [plugin_tool] if "plugin_lookup" in allow else []

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", fake_plugin_tools)
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())
    monkeypatch.setattr(channel_registry, "running_channels", lambda: (_ for _ in ()).throw(AssertionError("channels should not bind")))
    monkeypatch.setattr(tool_factory, "create_channel_tools", lambda channel: (_ for _ in ()).throw(AssertionError("channels should not bind")))

    graph = agent.get_agent_graph(
        ["filesystem"],
        tool_allowlist=["filesystem", "plugin_lookup"],
    )

    assert list(_bound_tools(graph)) == ["filesystem", "tool_search", "tool_invoke"]
    search = _bound_tools(graph)["tool_search"]
    assert [
        result["name"]
        for result in json.loads(search.invoke({"query": "plugin", "limit": 5}))["results"]
    ] == ["plugin_lookup"]
    assert plugin_allow_args == [{"filesystem", "plugin_lookup"}]
    assert agent._current_effective_tool_parent_names_var.get() == ("filesystem",)


def test_get_agent_graph_with_allowlist_filters_individual_mcp_tools(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    mcp_tool = _lc_tool("mcp_local_echo")
    allow_args = []

    monkeypatch.setattr(
        agent.tool_registry,
        "get_tool",
        lambda name: SimpleNamespace(
            as_langchain_tools=lambda: [_lc_tool("mcp_fallback")],
            destructive_tool_names={"mcp_fallback"},
        ) if name == "mcp" else None,
    )

    from row_bot.mcp_client import runtime as mcp_runtime
    from row_bot.plugins import registry as plugin_registry

    def fake_mcp_tools(allow_names=None):
        allow = set(allow_names or [])
        allow_args.append(allow)
        return [mcp_tool] if "mcp_local_echo" in allow else []

    monkeypatch.setattr(mcp_runtime, "get_langchain_tools", fake_mcp_tools)
    monkeypatch.setattr(mcp_runtime, "get_destructive_tool_names", lambda allow_names=None: set())
    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda allow_names=None: [])
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())

    graph = agent.get_agent_graph(["mcp"], tool_allowlist=["mcp_local_echo"])

    assert list(_bound_tools(graph)) == ["tool_search", "tool_invoke"]
    search = _bound_tools(graph)["tool_search"]
    assert json.loads(search.invoke({"query": "echo"}))["results"][0]["name"] == "mcp_local_echo"
    assert allow_args == [{"mcp_local_echo"}]
    assert agent._current_effective_tool_parent_names_var.get() == ("mcp",)


def test_get_agent_graph_memory_allowlist_exposes_normal_memory_tools(monkeypatch):
    agent = _prepare_graph(monkeypatch)

    from row_bot.plugins import registry as plugin_registry

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda allow_names=None: [])
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())

    graph = agent.get_agent_graph(["memory"], tool_allowlist=["memory"])
    names = set(_bound_tools(graph))

    assert {
        "save_memory",
        "search_memory",
        "list_memories",
        "update_memory",
        "delete_memory",
        "link_memories",
        "explore_connections",
    } <= names


def test_get_agent_graph_parent_mcp_allowlist_includes_all_mcp_tools(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    mcp_tools = [_lc_tool("mcp_local_echo"), _lc_tool("mcp_other_list")]
    allow_args = []

    monkeypatch.setattr(
        agent.tool_registry,
        "get_tool",
        lambda name: SimpleNamespace(
            as_langchain_tools=lambda: [_lc_tool("mcp_fallback")],
            destructive_tool_names=set(),
        ) if name == "mcp" else None,
    )

    from row_bot.mcp_client import runtime as mcp_runtime
    from row_bot.plugins import registry as plugin_registry

    def fake_mcp_tools(allow_names=None):
        allow_args.append(set(allow_names or []))
        return mcp_tools

    monkeypatch.setattr(mcp_runtime, "get_langchain_tools", fake_mcp_tools)
    monkeypatch.setattr(mcp_runtime, "get_destructive_tool_names", lambda allow_names=None: set())
    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda allow_names=None: [])
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())

    graph = agent.get_agent_graph(["mcp"], tool_allowlist=["mcp"])

    assert list(_bound_tools(graph)) == ["tool_search", "tool_invoke"]
    search = _bound_tools(graph)["tool_search"]
    assert {
        result["name"]
        for result in json.loads(search.invoke({"query": "mcp", "limit": 5}))["results"]
    } == {"mcp_local_echo", "mcp_other_list"}
    assert allow_args == [{"mcp"}]


def test_eager_loading_mode_preserves_direct_external_tool_list(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    core_tool = _lc_tool("filesystem")
    plugin_tool = _lc_tool("plugin_lookup")
    monkeypatch.setattr(agent.tool_registry, "get_external_tool_loading_mode", lambda: "eager")
    monkeypatch.setattr(
        agent.tool_registry,
        "get_tool",
        lambda name: SimpleNamespace(
            as_langchain_tools=lambda: [core_tool],
            destructive_tool_names=set(),
        ) if name == "filesystem" else None,
    )
    from row_bot.plugins import registry as plugin_registry
    from row_bot.channels import registry as channel_registry

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda allow_names=None: [plugin_tool])
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())
    monkeypatch.setattr(channel_registry, "running_channels", lambda: [])

    graph = agent.get_agent_graph(["filesystem"])

    assert list(_bound_tools(graph)) == ["filesystem", "plugin_lookup"]


def test_get_agent_graph_cache_key_includes_allowlist(monkeypatch):
    agent = _prepare_graph(monkeypatch)

    monkeypatch.setattr(
        agent.tool_registry,
        "get_tool",
        lambda name: SimpleNamespace(
            as_langchain_tools=lambda: [_lc_tool(name)],
            destructive_tool_names=set(),
        ),
    )

    from row_bot.plugins import registry as plugin_registry

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda allow_names=None: [])
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())

    first = agent.get_agent_graph(["filesystem", "row_bot_status"], tool_allowlist=["filesystem"])
    second = agent.get_agent_graph(["filesystem", "row_bot_status"], tool_allowlist=["row_bot_status"])
    repeated = agent.get_agent_graph(["filesystem", "row_bot_status"], tool_allowlist=["filesystem"])

    assert first is repeated
    assert first is not second


def test_gemini_final_boundary_isolates_malformed_mcp_plugin_and_channel_tools(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    core_tool = _lc_tool("filesystem")
    malformed_mcp = _malformed_array_tool("mcp_bad_array")
    malformed_plugin = _malformed_array_tool("plugin_bad_array")
    malformed_channel = _malformed_array_tool("channel_bad_array")

    monkeypatch.setattr(
        agent,
        "_ensure_agent_mode_ready",
        lambda model_name: SimpleNamespace(
            provider_id="google",
            runtime_model="gemini-test",
            capability_source="test",
            confidence="high",
            transport=TransportMode.GOOGLE_GENAI,
        ),
    )

    def fake_core_tool(name):
        if name == "filesystem":
            return SimpleNamespace(as_langchain_tools=lambda: [core_tool], destructive_tool_names=set())
        if name == "mcp":
            return SimpleNamespace(as_langchain_tools=lambda: [malformed_mcp], destructive_tool_names=set())
        return None

    monkeypatch.setattr(agent.tool_registry, "get_tool", fake_core_tool)

    from row_bot.plugins import registry as plugin_registry
    from row_bot.channels import registry as channel_registry
    from row_bot.channels import tool_factory

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda allow_names=None: [malformed_plugin])
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())
    monkeypatch.setattr(channel_registry, "running_channels", lambda: [SimpleNamespace(name="sms")])
    monkeypatch.setattr(tool_factory, "create_channel_tools", lambda channel: [malformed_channel])
    monkeypatch.setattr(tool_factory, "destructive_channel_tool_names", lambda channel: set())

    graph = agent.get_agent_graph(["filesystem", "mcp"])

    assert list(_bound_tools(graph)) == ["filesystem"]


def test_gemini_explicit_allowlist_fails_for_malformed_tool(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    malformed = _malformed_array_tool("malformed")
    monkeypatch.setattr(
        agent,
        "_ensure_agent_mode_ready",
        lambda model_name: SimpleNamespace(
            provider_id="google",
            runtime_model="gemini-test",
            capability_source="test",
            confidence="high",
            transport=TransportMode.GOOGLE_GENAI,
        ),
    )
    monkeypatch.setattr(
        agent.tool_registry,
        "get_tool",
        lambda name: SimpleNamespace(
            as_langchain_tools=lambda: [malformed],
            destructive_tool_names=set(),
        ),
    )

    from row_bot.plugins import registry as plugin_registry

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda allow_names=None: [])
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())

    with pytest.raises(ToolSchemaCompatibilityError, match=r"malformed.*values\.items"):
        agent.get_agent_graph(["malformed"], tool_allowlist=["malformed"])


def test_forced_approval_resume_keeps_empty_discovery_bridge_bound(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    from row_bot.plugins import registry as plugin_registry
    from row_bot.channels import registry as channel_registry

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda allow_names=None: [])
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())
    monkeypatch.setattr(channel_registry, "running_channels", lambda: [])
    monkeypatch.setattr(agent.tool_registry, "get_external_tool_loading_mode", lambda: "eager")
    agent._set_active_runtime_context(
        thread_id="child-resume",
        runtime_surface="agent_child",
        enabled_tool_names=(),
        tool_allowlist=(),
        external_discovery_active=True,
    )

    graph = agent.get_agent_graph([], tool_allowlist=[])

    assert list(_bound_tools(graph)) == ["tool_search", "tool_invoke"]
    invoke = _bound_tools(graph)["tool_invoke"]
    payload = json.loads(invoke.invoke({"name": "removed_target", "arguments": {}}))
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_tool"
    assert "no longer available" in payload["error"]["message"]


def test_external_target_keeps_underlying_approval_name_and_one_repeat_guard(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    target = _lc_tool("plugin_lookup")
    interrupts: list[dict] = []
    guarded: list[tuple[str, dict]] = []
    agent._approval_mode_var.set("approve")
    monkeypatch.setattr(agent, "interrupt", lambda payload: interrupts.append(payload) or True)
    monkeypatch.setattr(
        agent,
        "register_exact_tool_request",
        lambda name, arguments: guarded.append((name, arguments)) or "allow",
    )
    from row_bot.plugins import registry as plugin_registry
    from row_bot.channels import registry as channel_registry

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", lambda allow_names=None: [target])
    monkeypatch.setattr(
        plugin_registry,
        "get_destructive_names",
        lambda allow_names=None: {"plugin_lookup"},
    )
    monkeypatch.setattr(channel_registry, "running_channels", lambda: [])

    graph = agent.get_agent_graph([])
    invoke = _bound_tools(graph)["tool_invoke"]
    payload = invoke.invoke({
        "name": "plugin_lookup",
        "arguments": {"query": "alpha"},
    })

    assert payload == "plugin_lookup:alpha"
    assert interrupts[0]["tool"] == "plugin_lookup"
    assert interrupts[0]["external_discovery_active"] is True
    assert [name for name, _arguments in guarded] == ["plugin_lookup"]


def test_blocked_destructive_external_target_is_not_searchable(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    agent._approval_mode_var.set("block")
    destructive = _lc_tool("plugin_delete")
    safe = _lc_tool("plugin_lookup")
    from row_bot.plugins import registry as plugin_registry
    from row_bot.channels import registry as channel_registry

    monkeypatch.setattr(
        plugin_registry,
        "get_langchain_tools",
        lambda allow_names=None: [destructive, safe],
    )
    monkeypatch.setattr(
        plugin_registry,
        "get_destructive_names",
        lambda allow_names=None: {"plugin_delete"},
    )
    monkeypatch.setattr(channel_registry, "running_channels", lambda: [])

    graph = agent.get_agent_graph([])
    search = _bound_tools(graph)["tool_search"]
    payload = json.loads(search.invoke({"query": "plugin", "limit": 5}))

    assert [result["name"] for result in payload["results"]] == ["plugin_lookup"]


def test_discovery_assembly_failure_falls_back_to_same_filtered_external_snapshot(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    allowed = _lc_tool("plugin_allowed")
    denied = _lc_tool("plugin_denied")
    from row_bot.plugins import registry as plugin_registry
    from row_bot.channels import registry as channel_registry
    import row_bot.tools.discovery as discovery

    monkeypatch.setattr(
        plugin_registry,
        "get_langchain_tools",
        lambda allow_names=None: [allowed] if "plugin_allowed" in set(allow_names or ()) else [denied],
    )
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())
    monkeypatch.setattr(channel_registry, "running_channels", lambda: [])
    monkeypatch.setattr(
        discovery,
        "build_tool_discovery_tools",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("assembly failed")),
    )

    graph = agent.get_agent_graph([], tool_allowlist=["plugin_allowed"])

    assert list(_bound_tools(graph)) == ["plugin_allowed"]


def test_cache_identity_tracks_external_metadata_and_skill_snapshot(monkeypatch):
    agent = _prepare_graph(monkeypatch)
    state = {"description": "first", "plugin_id": "one", "skill": "skills-one"}
    from row_bot.plugins import registry as plugin_registry
    from row_bot.channels import registry as channel_registry

    def plugin_tool(_allow_names=None, **_kwargs):
        return [StructuredTool.from_function(
            func=lambda query="": query,
            name="plugin_lookup",
            description=state["description"],
        )]

    monkeypatch.setattr(plugin_registry, "get_langchain_tools", plugin_tool)
    monkeypatch.setattr(plugin_registry, "get_destructive_names", lambda allow_names=None: set())
    monkeypatch.setattr(
        plugin_registry,
        "get_enabled_plugin_tool_records",
        lambda: [{
            "runtime_name": "plugin_lookup",
            "plugin_id": state["plugin_id"],
            "parent_name": "lookup",
        }],
    )
    monkeypatch.setattr(channel_registry, "running_channels", lambda: [])
    monkeypatch.setattr(
        agent,
        "_build_runtime_skill_snapshot",
        lambda: ((), (), False, state["skill"]),
    )

    first = agent.get_agent_graph([])
    state["description"] = "second"
    description_changed = agent.get_agent_graph([])
    state["plugin_id"] = "two"
    source_changed = agent.get_agent_graph([])
    state["skill"] = "skills-two"
    skill_changed = agent.get_agent_graph([])

    assert len({id(first), id(description_changed), id(source_changed), id(skill_changed)}) == 4
