from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import StructuredTool

from row_bot.plugins.api import PluginAPI, PluginTool
from row_bot.plugins.manifest import PluginAuthor, PluginManifest, PluginProvides

from .conftest import manifest_payload, write_plugin


pytestmark = pytest.mark.subsystem


class PluginFixtureTool(PluginTool):
    @property
    def name(self) -> str:
        return "test_tool"

    @property
    def display_name(self) -> str:
        return "Test Tool"

    @property
    def description(self) -> str:
        return "A plugin registry test tool"

    @property
    def destructive_tool_names(self) -> set[str]:
        return {"send_message", "delete_record"}

    @property
    def background_allowed_tool_names(self) -> set[str]:
        return {"send_message"}

    def execute(self, query: str) -> str:
        return f"result:{query}"


def _manifest(plugin_id: str = "test-plugin") -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        name="Test Plugin",
        version="1.0.0",
        min_row_bot_version="0.0.0",
        author=PluginAuthor(name="Tester"),
        description="A deterministic test plugin.",
        tags=["testing"],
    )


def test_registry_tracks_enabled_tools_skills_and_permission_sets(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    state = plugin_modules["state"]
    registry = plugin_modules["registry"]
    api = PluginAPI("test-plugin", tmp_path / "plugin", state)
    tool = PluginFixtureTool(api)

    state.set_plugin_enabled("test-plugin", True)
    warnings = registry.register_plugin(
        manifest=_manifest(),
        tools=[tool],
        skills=[{
            "name": "test_skill",
            "display_name": "Test Skill",
            "icon": "*",
            "description": "A test skill",
            "instructions": "Use the test skill.",
        }],
    )

    assert warnings == []
    assert registry.get_plugin_tool_names() == ["test_tool"]
    assert [t.name for t in registry.get_langchain_tools()] == ["test_tool"]
    assert [t.name for t in registry.get_langchain_tools(allow_names=["test-plugin"])] == ["test_tool"]
    assert registry.get_enabled_plugin_tool_names() == ["test_tool"]
    assert registry.get_destructive_names() == {"send_message", "delete_record"}
    assert registry.get_destructive_names(allow_names=["send_message"]) == {"send_message"}
    assert registry.get_background_allowed_names() == {"send_message"}

    prompt = registry.get_skills_prompt()
    assert "Plugin Skills" in prompt
    assert "Test Skill" in prompt
    assert "plugin: Test Plugin" in prompt
    assert "Use the test skill." in prompt
    assert "Test Skill" in registry.get_skills_prompt(allow_names=["test-plugin"])
    assert "Test Skill" in registry.get_skills_prompt(allow_names=["test_tool"])
    assert registry.get_skills_prompt(allow_names=["other_tool"]) == ""
    assert registry.get_plugin_skills("test-plugin")[0]["name"] == "test_skill"

    records = registry.get_enabled_plugin_tool_records()
    assert records[0]["runtime_name"] == "test_tool"
    assert records[0]["plugin_id"] == "test-plugin"
    assert records[0]["destructive"] is False

    state.set_plugin_enabled("test-plugin", False)
    assert registry.get_langchain_tools() == []
    assert registry.get_destructive_names() == set()
    assert registry.get_background_allowed_names() == set()
    assert registry.get_skills_prompt() == ""
    assert registry.get_plugin_skills("test-plugin") == []
    assert registry.get_plugin_skills("test-plugin", enabled_only=False)[0]["name"] == "test_skill"


def test_registry_includes_plugin_owned_mcp_tools_when_enabled(
    plugin_modules: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = plugin_modules["state"]
    registry = plugin_modules["registry"]
    manifest = _manifest("mcp-plugin")
    manifest.provides = PluginProvides(mcp_servers=[{"id": "fake-mcp"}])
    fake_tool = StructuredTool.from_function(
        func=lambda query="": f"mcp:{query}",
        name="mcp_plugin_fake_read",
        description="Fake plugin MCP tool",
    )

    calls: list[tuple[str, Any]] = []

    from row_bot.mcp_client import runtime as mcp_runtime

    def fake_get_plugin_langchain_tools(plugin_id: str, allow_names=None):
        calls.append((plugin_id, allow_names))
        return [fake_tool]

    def fake_get_plugin_tool_records(plugin_id: str):
        return [{
            "runtime_name": "mcp_plugin_fake_read",
            "plugin_id": plugin_id,
            "plugin_name": "MCP Plugin",
            "source": "mcp",
            "destructive": False,
        }]

    monkeypatch.setattr(mcp_runtime, "get_plugin_langchain_tools", fake_get_plugin_langchain_tools)
    monkeypatch.setattr(
        mcp_runtime,
        "get_plugin_destructive_tool_names",
        lambda plugin_id, allow_names=None: {"mcp_plugin_fake_delete"},
    )
    monkeypatch.setattr(mcp_runtime, "get_plugin_tool_records", fake_get_plugin_tool_records)

    state.set_plugin_enabled("mcp-plugin", True)
    registry.register_plugin(
        manifest=manifest,
        tools=[],
        skills=[{
            "name": "mcp_skill",
            "display_name": "MCP Skill",
            "icon": "*",
            "description": "A plugin MCP skill",
            "instructions": "Use the plugin MCP tool.",
        }],
    )

    assert [tool.name for tool in registry.get_langchain_tools()] == ["mcp_plugin_fake_read"]
    assert registry.get_langchain_tools(allow_names=["mcp-plugin"])[0].name == "mcp_plugin_fake_read"
    assert "MCP Skill" in registry.get_skills_prompt(allow_names=["mcp-plugin"])
    assert "MCP Skill" in registry.get_skills_prompt(allow_names=["mcp_plugin_fake_read"])
    assert registry.get_skills_prompt(allow_names=["other_tool"]) == ""
    assert registry.get_destructive_names() == {"mcp_plugin_fake_delete"}
    assert registry.get_enabled_plugin_tool_names() == ["mcp_plugin_fake_read"]
    assert registry.get_enabled_plugin_tool_records()[0]["source"] == "mcp"
    assert calls[1] == ("mcp-plugin", None)

    state.set_plugin_enabled("mcp-plugin", False)
    assert registry.get_langchain_tools() == []
    assert registry.get_destructive_names() == set()


def test_plugin_mcp_overlay_uses_enabled_manifests_and_resolves_config(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    state = plugin_modules["state"]
    registry = plugin_modules["registry"]
    manifest = _manifest("office-plugin")
    manifest.path = tmp_path / "office-plugin"
    manifest.provides = PluginProvides(
        mcp_servers=[{
            "id": "office",
            "command": "python",
            "args": ["-m", "office_mcp"],
            "env": {"TENANT": "setting:tenant", "TOKEN": "secret:TOKEN"},
            "tools": {"include": ["search_mail"]},
        }]
    )
    registry.register_plugin(manifest=manifest, tools=[], skills=[])
    state.set_plugin_config("office-plugin", "tenant", "contoso")
    state.set_plugin_secret("office-plugin", "TOKEN", "secret-value")
    state.set_plugin_enabled("office-plugin", True)

    from row_bot.plugins.mcp import plugin_mcp_server_name, with_plugin_mcp_servers

    cfg = with_plugin_mcp_servers({
        "enabled": False,
        "servers": {"user-server": {"enabled": True, "transport": "stdio"}},
    })
    server_name = plugin_mcp_server_name("office-plugin", "office")
    server = cfg["servers"][server_name]

    assert cfg["enabled"] is True
    assert cfg["servers"]["user-server"]["enabled"] is False
    assert server["enabled"] is True
    assert server["cwd"] == str(manifest.path)
    assert server["env"] == {"TENANT": "contoso", "TOKEN": "secret-value"}
    assert server["source"]["kind"] == "plugin"
    assert server["source"]["plugin_id"] == "office-plugin"

    state.set_plugin_enabled("office-plugin", False)
    disabled_cfg = with_plugin_mcp_servers({"enabled": False, "servers": {}})

    assert disabled_cfg["enabled"] is False
    assert server_name not in disabled_cfg["servers"]


def test_registry_detects_tool_and_skill_name_collisions(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    registry = plugin_modules["registry"]
    state = plugin_modules["state"]
    api = PluginAPI("first-plugin", tmp_path / "plugin", state)
    tool = PluginFixtureTool(api)

    registry.register_plugin(
        manifest=_manifest("first-plugin"),
        tools=[tool],
        skills=[{"name": "shared_skill", "instructions": "first"}],
    )
    warnings = registry.register_plugin(
        manifest=_manifest("second-plugin"),
        tools=[tool],
        skills=[{"name": "shared_skill", "instructions": "second"}],
    )

    assert len(warnings) == 1
    assert all("collides" in warning for warning in warnings)
    assert registry.get_plugin_tool_names() == ["test_tool"]
    assert registry.get_plugin_skills("second-plugin", enabled_only=False)[0]["instructions"] == "second"


def test_loader_loads_valid_plugin_discovers_skills_and_registers_tool(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    state = plugin_modules["state"]
    loader = plugin_modules["loader"]
    registry = plugin_modules["registry"]
    skill = (
        "---\n"
        "name: sample_skill\n"
        "display_name: Sample Skill\n"
        "description: Skill from plugin\n"
        "tags: [sample, workflow]\n"
        "activation:\n  keywords: [sample]\n"
        "version: '2.0'\n"
        "author: Plugin Author\n"
        "---\n"
        "Follow the plugin skill instructions.\n"
    )
    plugin_dir = write_plugin(tmp_path, "sample-plugin", skill=skill)

    state.set_plugin_enabled("sample-plugin", True)
    result = loader._load_single_plugin(plugin_dir)

    assert result.success is True
    assert result.error == ""
    assert result.manifest is not None
    assert result.manifest.id == "sample-plugin"
    assert registry.get_plugin_tool_names() == ["sample_tool"]
    skill_record = registry.get_plugin_skills("sample-plugin")[0]
    assert skill_record["name"] == "sample_skill"
    assert skill_record["tags"] == ["sample", "workflow"]
    assert skill_record["activation"] == {"keywords": ["sample"]}
    assert skill_record["version"] == "2.0"
    assert skill_record["author"] == "Plugin Author"
    assert skill_record["root"] == plugin_dir / "skills" / "sample_skill"


def test_loader_keeps_disabled_plugin_visible_without_registering_runtime_tools(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    state = plugin_modules["state"]
    loader = plugin_modules["loader"]
    registry = plugin_modules["registry"]
    plugin_dir = write_plugin(tmp_path, "disabled-plugin")

    state.set_plugin_enabled("disabled-plugin", False)
    result = loader._load_single_plugin(plugin_dir)

    assert result.success is True
    assert result.manifest is not None
    assert "disabled" in result.warnings[0].lower()
    assert registry.get_manifest("disabled-plugin") is not None
    assert registry.get_plugin_tool_names() == []


def test_loader_registers_plugin_channel_and_disable_unregisters(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    state = plugin_modules["state"]
    loader = plugin_modules["loader"]
    from row_bot.channels import registry as channel_registry

    plugin_dir = write_plugin(
        tmp_path,
        "channel-plugin",
        manifest=manifest_payload(
            "channel-plugin",
            provides={
                "native_tools": [],
                "mcp_servers": [],
                "channels": [{"id": "plugin-fake"}],
                "skills": [],
            },
        ),
        main=(
            "from plugins.api import Channel\n\n"
            "class PluginFakeChannel(Channel):\n"
            "    @property\n"
            "    def name(self): return 'plugin_fake'\n"
            "    @property\n"
            "    def display_name(self): return 'Plugin Fake'\n"
            "    async def start(self): self.running = True; return True\n"
            "    async def stop(self): self.running = False\n"
            "    def is_configured(self): return True\n"
            "    def is_running(self): return getattr(self, 'running', False)\n"
            "    def send_message(self, target, text): self.last = (target, text)\n\n"
            "def register(api):\n"
            "    api.register_channel(PluginFakeChannel())\n"
        ),
    )

    state.set_plugin_enabled("channel-plugin", True)
    result = loader._load_single_plugin(plugin_dir)

    assert result.success is True
    assert channel_registry.get("plugin_fake") is not None
    assert channel_registry.get_source("plugin_fake").kind == "plugin"
    assert channel_registry.get_source("plugin_fake").plugin_id == "channel-plugin"

    state.set_plugin_enabled("channel-plugin", False)

    assert channel_registry.get("plugin_fake") is None


def test_loader_rejects_plugin_channel_manifest_mismatch(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    state = plugin_modules["state"]
    loader = plugin_modules["loader"]
    from row_bot.channels import registry as channel_registry

    plugin_dir = write_plugin(
        tmp_path,
        "channel-plugin",
        manifest=manifest_payload(
            "channel-plugin",
            provides={
                "native_tools": [],
                "mcp_servers": [],
                "channels": [{"id": "teams"}],
                "skills": [],
            },
        ),
        main=(
            "from plugins.api import Channel\n\n"
            "class OtherChannel(Channel):\n"
            "    @property\n"
            "    def name(self): return 'other'\n"
            "    @property\n"
            "    def display_name(self): return 'Other'\n"
            "    async def start(self): return True\n"
            "    async def stop(self): pass\n"
            "    def is_configured(self): return True\n"
            "    def is_running(self): return True\n"
            "    def send_message(self, target, text): pass\n\n"
            "def register(api):\n"
            "    api.register_channel(OtherChannel())\n"
        ),
    )

    state.set_plugin_enabled("channel-plugin", True)
    result = loader._load_single_plugin(plugin_dir)

    assert result.success is False
    assert "undeclared channel" in result.error
    assert channel_registry.get("other") is None


def test_loader_rejects_declared_channel_without_registration(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    state = plugin_modules["state"]
    loader = plugin_modules["loader"]
    plugin_dir = write_plugin(
        tmp_path,
        "channel-plugin",
        manifest=manifest_payload(
            "channel-plugin",
            provides={
                "native_tools": [],
                "mcp_servers": [],
                "channels": [{"id": "teams"}],
                "skills": [],
            },
        ),
        main="def register(api):\n    return None\n",
    )

    state.set_plugin_enabled("channel-plugin", True)
    result = loader._load_single_plugin(plugin_dir)

    assert result.success is False
    assert "registered none" in result.error


def test_loader_failure_after_register_cleans_up_webhooks(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = plugin_modules["state"]
    loader = plugin_modules["loader"]
    webhooks = plugin_modules["webhooks"]
    monkeypatch.setattr(webhooks, "_ensure_route_mounted", lambda: None)
    plugin_dir = write_plugin(
        tmp_path,
        "channel-plugin",
        manifest=manifest_payload(
            "channel-plugin",
            provides={
                "native_tools": [],
                "mcp_servers": [],
                "channels": [{"id": "teams"}],
                "skills": [],
            },
        ),
        main=(
            "from plugins.api import PluginWebhookResponse\n\n"
            "def register(api):\n"
            "    api.register_webhook_route(\n"
            "        'events', lambda request: PluginWebhookResponse(body='ok')\n"
            "    )\n"
        ),
    )

    state.set_plugin_enabled("channel-plugin", True)
    result = loader._load_single_plugin(plugin_dir)

    assert result.success is False
    assert "registered none" in result.error
    assert webhooks._webhooks == {}


def test_unregister_removes_plugin_webhooks(
    plugin_modules: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = plugin_modules["registry"]
    webhooks = plugin_modules["webhooks"]
    monkeypatch.setattr(webhooks, "_ensure_route_mounted", lambda: None)
    webhooks.register_plugin_webhook(
        "sample-plugin",
        "events",
        lambda request: None,
    )

    registry.unregister_plugin("sample-plugin")

    assert webhooks._webhooks == {}


def test_loader_reports_broken_plugin_without_crashing(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    state = plugin_modules["state"]
    loader = plugin_modules["loader"]
    registry = plugin_modules["registry"]
    plugin_dir = write_plugin(
        tmp_path,
        "broken-plugin",
        manifest=manifest_payload(
            "broken-plugin",
            provides={
                "native_tools": [],
                "mcp_servers": [],
                "channels": [],
                "skills": [],
            },
        ),
        main="def register(api):\n    raise RuntimeError('boom')\n",
    )

    state.set_plugin_enabled("broken-plugin", True)
    result = loader._load_single_plugin(plugin_dir)

    assert result.success is False
    assert "crashed" in result.error
    assert registry.get_plugin_tool_names() == []
    logs = loader.read_plugin_logs("broken-plugin")
    assert logs[-1]["success"] is False
    assert "crashed" in logs[-1]["error"]


@pytest.mark.parametrize(
    ("filename", "source", "expected"),
    [
        ("plugin_main.py", "import os\nos.system('echo bad')\n", "os.system"),
        ("plugin_main.py", "from os import system\nsystem('echo bad')\n", "os.system"),
        ("plugin_main.py", "import subprocess\n", "subprocess"),
        ("plugin_main.py", "value = eval('1 + 1')\n", "eval"),
        ("plugin_main.py", "from agent import get_agent_graph\n", "agent"),
        ("plugin_main.py", "from row_bot.agent import get_agent_graph\n", "row_bot.agent"),
        ("plugin_main.py", "from row_bot.channels.base import Channel\n", "row_bot.channels.base"),
        ("plugin_main.py", "from nicegui import ui\n", "nicegui"),
        ("plugin_main.py", "import streamlit\n", "streamlit"),
    ],
)
def test_loader_security_scan_rejects_dangerous_code(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
    filename: str,
    source: str,
    expected: str,
) -> None:
    loader = plugin_modules["loader"]
    plugin_dir = tmp_path / "unsafe-plugin"
    plugin_dir.mkdir()
    (plugin_dir / filename).write_text(source, encoding="utf-8")

    error = loader._security_scan(plugin_dir)

    assert error is not None
    assert expected in error


def test_loader_security_scan_allows_plugin_api_import(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    loader = plugin_modules["loader"]
    plugin_dir = tmp_path / "safe-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin_main.py").write_text(
        "from plugins.api import BotFrameworkAuthResult, Channel, PluginAPI, PluginTool\n"
        "def register(api):\n"
        "    return None\n",
        encoding="utf-8",
    )

    assert loader._security_scan(plugin_dir) is None


def test_unregister_removes_plugin_manifest_tools_and_skills(
    plugin_modules: dict[str, Any],
    tmp_path: Path,
) -> None:
    state = plugin_modules["state"]
    loader = plugin_modules["loader"]
    registry = plugin_modules["registry"]
    plugin_dir = write_plugin(
        tmp_path,
        "sample-plugin",
        skill="---\nname: sample_skill\n---\nSkill instructions.\n",
    )
    state.set_plugin_enabled("sample-plugin", True)
    result = loader._load_single_plugin(plugin_dir)
    assert result.success is True

    registry.unregister_plugin("sample-plugin")

    assert registry.get_manifest("sample-plugin") is None
    assert registry.get_plugin_tool_names() == []
    assert registry.get_plugin_skills("sample-plugin") == []


def test_load_plugins_quarantines_legacy_thoth_manifest(
    plugin_modules: dict[str, Any],
) -> None:
    loader = plugin_modules["loader"]
    registry = plugin_modules["registry"]
    plugin_dir = loader.PLUGINS_DIR / "legacy-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "id": "legacy-plugin",
            "name": "Legacy Plugin",
            "version": "0.1.0",
            "min_thoth_version": "0.9.0",
            "provides": {"tools": [{"id": "old_tool"}]},
        }),
        encoding="utf-8",
    )
    (plugin_dir / "plugin_main.py").write_text("def register(api): pass\n", encoding="utf-8")

    results = loader.load_plugins()

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].stale is True
    assert "min_thoth_version" in results[0].warnings[0]
    assert not plugin_dir.exists()
    stale_dir = Path(results[0].stale_path)
    assert stale_dir.exists()
    assert stale_dir.parent == loader.STALE_PLUGINS_DIR
    assert registry.get_loaded_manifests() == []
    assert loader.get_load_summary()["failed"] == 0
    assert loader.get_load_summary()["stale"] == 1

    report = json.loads((loader.STALE_PLUGINS_DIR / loader.STALE_PLUGIN_REPORT).read_text(encoding="utf-8"))
    assert report["plugins"][0]["plugin_id"] == "legacy-plugin"
    assert "min_thoth_version" in report["plugins"][0]["reason"]


def test_stale_plugin_quarantine_uses_unique_destination(
    plugin_modules: dict[str, Any],
) -> None:
    loader = plugin_modules["loader"]
    existing = loader.STALE_PLUGINS_DIR / "legacy-plugin"
    existing.mkdir(parents=True)
    plugin_dir = loader.PLUGINS_DIR / "legacy-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "id": "legacy-plugin",
            "name": "Legacy Plugin",
            "version": "0.1.0",
            "provides": {"tools": []},
        }),
        encoding="utf-8",
    )

    result = loader.load_plugins()[0]

    assert result.stale is True
    assert Path(result.stale_path) != existing
    assert Path(result.stale_path).exists()
    assert existing.exists()


def test_invalid_v2_plugin_remains_load_failure(
    plugin_modules: dict[str, Any],
) -> None:
    loader = plugin_modules["loader"]
    plugin_dir = loader.PLUGINS_DIR / "broken-v2-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({"schema_version": 2, "id": "broken-v2-plugin"}),
        encoding="utf-8",
    )

    result = loader.load_plugins()[0]

    assert result.success is False
    assert result.stale is False
    assert plugin_dir.exists()
    assert not loader.STALE_PLUGINS_DIR.exists()


def test_refresh_plugin_runtime_registers_enabled_tool_for_agent_graph(
    plugin_modules: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = plugin_modules["state"]
    loader = plugin_modules["loader"]
    registry = plugin_modules["registry"]
    plugin_dir = write_plugin(loader.PLUGINS_DIR, "sample-plugin")
    state.set_plugin_enabled("sample-plugin", True)

    import row_bot.agent as agent

    agent.clear_agent_cache()
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:test")
    monkeypatch.setattr(agent, "get_llm", lambda: object())
    monkeypatch.setattr(agent, "get_context_size", lambda model_name=None: 32_768)
    monkeypatch.setattr(agent, "get_agent_system_prompt", lambda: "system")
    monkeypatch.setattr(
        agent,
        "_ensure_agent_mode_ready",
        lambda model_name: type(
            "Ready",
            (),
            {
                "provider_id": "test",
                "runtime_model": "test",
                "capability_source": "test",
                "confidence": "high",
            },
        )(),
    )
    monkeypatch.setattr(agent, "create_react_agent", lambda **kwargs: type("Graph", (), kwargs)())
    monkeypatch.setattr(agent.tool_registry, "get_enabled_tools", lambda: [])
    monkeypatch.setattr(agent.tool_registry, "get_tool", lambda name: None)
    from row_bot.mcp_client import runtime as mcp_runtime

    mcp_calls: list[str] = []
    monkeypatch.setattr(mcp_runtime, "discover_enabled_servers", lambda: mcp_calls.append("discover"))

    loader.refresh_plugin_runtime("test")
    loader.refresh_plugin_runtime("test again")
    graph = agent.get_agent_graph([])

    assert registry.get_plugin_tool_names() == ["sample_tool"]
    assert [tool.name for tool in graph.tools] == [
        "tool_search", "tool_invoke", "skill_search", "skill_load",
    ]
    assert mcp_calls == ["discover", "discover"]
    assert plugin_dir.exists()
