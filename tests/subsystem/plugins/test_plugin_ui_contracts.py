from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from row_bot.plugins.manifest import PluginAuthor, PluginManifest, PluginProvides


pytestmark = pytest.mark.subsystem

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_plugin_settings_missing_required_keys_respects_secret_state(
    plugin_modules: dict[str, Any],
) -> None:
    state = plugin_modules["state"]
    from row_bot.plugins.ui_settings import _get_missing_keys

    manifest = PluginManifest(
        id="ui-plugin",
        name="UI Plugin",
        version="1.0.0",
        min_row_bot_version="0.0.0",
        author=PluginAuthor(name="Tester"),
        description="UI settings contract",
        settings={
            "api_keys": {
                "REQUIRED_KEY": {"label": "Required Key", "required": True},
                "OPTIONAL_KEY": {"label": "Optional Key", "required": False},
            }
        },
    )

    assert _get_missing_keys(manifest) == ["Required Key"]

    state.set_plugin_secret("ui-plugin", "REQUIRED_KEY", "configured")

    assert _get_missing_keys(manifest) == []


def test_plugin_center_v2_settings_secrets_and_sections(
    plugin_modules: dict[str, Any],
) -> None:
    state = plugin_modules["state"]
    from row_bot.plugins.ui_plugin_dialog import PLUGIN_DETAIL_SECTIONS
    from row_bot.plugins.ui_settings import (
        _can_enable_plugin,
        _get_missing_secrets,
        _get_missing_settings,
        _iter_secret_specs,
        _iter_setting_specs,
        _plugin_status,
        _record_manifest_health,
        _run_manifest_health,
    )

    manifest = PluginManifest(
        id="v2-ui-plugin",
        name="V2 UI Plugin",
        version="1.0.0",
        min_row_bot_version="0.0.0",
        author=PluginAuthor(name="Tester"),
        description="UI settings contract",
        settings={
            "workspace": {"type": "local_path", "label": "Workspace", "required": True},
            "mode": {"type": "select", "label": "Mode", "options": ["safe"], "default": "safe"},
        },
        secrets={
            "TOKEN": {"type": "secret", "label": "Token", "required": True},
        },
    )

    assert PLUGIN_DETAIL_SECTIONS == (
        "Overview",
        "Permissions",
        "Setup",
        "Auth",
        "Tools",
        "Channels",
        "Skills",
        "Health",
        "Logs",
        "Updates",
    )
    assert [name for name, _spec in _iter_setting_specs(manifest)] == ["workspace", "mode"]
    assert [name for name, _spec in _iter_secret_specs(manifest)] == ["TOKEN"]
    assert _get_missing_settings(manifest) == ["Workspace"]
    assert _get_missing_secrets(manifest) == ["Token"]
    assert _plugin_status(manifest, enabled=False)[0] == "Setup needed"
    assert _plugin_status(
        manifest,
        enabled=True,
        update_entry=SimpleNamespace(version="2.0.0"),
    ) == ("Update available v2.0.0", "warning")
    ok, reason = _can_enable_plugin(manifest)
    assert ok is False
    assert "needs setup" in reason

    state.set_plugin_config("v2-ui-plugin", "workspace", "D:/example")
    state.set_plugin_secret("v2-ui-plugin", "TOKEN", "secret-value")

    assert _get_missing_settings(manifest) == []
    assert _get_missing_secrets(manifest) == []
    assert _plugin_status(manifest, enabled=False)[0] == "Test required"
    ok, reason = _can_enable_plugin(manifest)
    assert ok is False
    assert "Run Test" in reason

    checks = _record_manifest_health(manifest)

    assert checks == [{"label": "Required local setup", "status": "ok"}]
    assert _plugin_status(manifest, enabled=False)[0] == "Test passed"
    assert _can_enable_plugin(manifest) == (True, "")

    live_check_manifest = PluginManifest(
        id="live-check-ui-plugin",
        name="Live Check UI Plugin",
        version="1.0.0",
        min_row_bot_version="0.0.0",
        author=PluginAuthor(name="Tester"),
        description="UI health contract",
        health_checks=[{"id": "api_probe", "type": "api_probe"}],
    )

    assert _run_manifest_health(live_check_manifest) == [
        {"label": "Api Probe", "status": "manual_required"}
    ]
    assert _record_manifest_health(live_check_manifest) == [
        {"label": "Api Probe", "status": "manual_required"}
    ]
    assert state.get_plugin_health_result("live-check-ui-plugin")["ok"] is True
    assert _can_enable_plugin(live_check_manifest) == (True, "")


def test_plugin_center_readme_setup_guide_is_local_only(tmp_path: Path) -> None:
    from row_bot.plugins.ui_plugin_dialog import _plugin_readme_path, _read_plugin_readme

    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "README.md").write_text("# Setup\n\nUse local docs only.\n", encoding="utf-8")

    manifest = PluginManifest(
        id="readme-plugin",
        name="README Plugin",
        version="1.0.0",
        min_row_bot_version="0.0.0",
        author=PluginAuthor(name="Tester"),
        description="UI docs contract",
        path=plugin_dir,
    )

    assert _plugin_readme_path(manifest) == plugin_dir / "README.md"
    assert _read_plugin_readme(manifest).startswith("# Setup")

    manifest.path = None

    assert _plugin_readme_path(manifest) is None


def test_plugin_center_readme_setup_guide_rejects_large_files(tmp_path: Path) -> None:
    from row_bot.plugins.ui_plugin_dialog import README_MAX_BYTES, _read_plugin_readme

    plugin_dir = tmp_path / "large-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "README.md").write_text("x" * (README_MAX_BYTES + 1), encoding="utf-8")

    manifest = PluginManifest(
        id="large-readme-plugin",
        name="Large README Plugin",
        version="1.0.0",
        min_row_bot_version="0.0.0",
        author=PluginAuthor(name="Tester"),
        description="UI docs contract",
        path=plugin_dir,
    )

    with pytest.raises(ValueError, match="too large"):
        _read_plugin_readme(manifest)


def test_declared_plugin_health_checks_are_evaluated_locally(
    plugin_modules: dict[str, Any],
) -> None:
    state = plugin_modules["state"]
    from row_bot.plugins.ui_settings import _record_manifest_health, _run_manifest_health

    channel_manifest = PluginManifest(
        id="channel-health-plugin",
        name="Channel Health Plugin",
        version="1.0.0",
        min_row_bot_version="0.0.0",
        author=PluginAuthor(name="Tester"),
        description="Health check contract",
        provides=PluginProvides(channels=[{"id": "fake_channel"}]),
        settings={"target": {"type": "text", "label": "Target", "required": True}},
        health_checks=[{"id": "channel_configured", "type": "channel_configured"}],
    )

    blocked = _run_manifest_health(channel_manifest)
    assert {"label": "Target", "status": "missing_setting"} in blocked
    assert {"label": "Channel Configured", "status": "blocked_missing_setup"} in blocked

    state.set_plugin_config("channel-health-plugin", "target", "me")

    assert _record_manifest_health(channel_manifest) == [
        {"label": "Channel Configured", "status": "ok"}
    ]

    mcp_manifest = PluginManifest(
        id="mcp-health-plugin",
        name="MCP Health Plugin",
        version="1.0.0",
        min_row_bot_version="0.0.0",
        author=PluginAuthor(name="Tester"),
        description="MCP health check contract",
        provides=PluginProvides(
            mcp_servers=[{"id": "example_mcp", "transport": "stdio", "command": "python"}],
        ),
        health_checks=[{"id": "mcp_starts", "type": "mcp_server_starts"}],
    )

    assert _run_manifest_health(mcp_manifest) == [{"label": "Mcp Starts", "status": "ok"}]


def test_plugin_ui_entrypoints_are_importable_callables() -> None:
    from row_bot.plugins.ui_marketplace import open_marketplace_dialog
    from row_bot.plugins.ui_plugin_dialog import open_plugin_dialog
    from row_bot.plugins.ui_settings import build_plugins_tab

    assert callable(build_plugins_tab)
    assert callable(open_plugin_dialog)
    assert callable(open_marketplace_dialog)


def test_settings_dialog_contains_plugins_tab_wiring() -> None:
    settings_path = REPO_ROOT / "src" / "row_bot" / "ui" / "settings.py"
    source = settings_path.read_text(encoding="utf-8")

    ast.parse(source)
    assert 'tab_plugins = ui.tab("Plugins"' in source
    assert '"Plugins": tab_plugins' in source
    assert "_build_plugins_tab" in source
    assert "open_marketplace_dialog(on_install" in source
    assert "if _ch_registry.allows_custom_ui(ch.name):" in source
    assert "ch.build_custom_ui(panel)" in source


def test_plugin_rich_return_markers_are_rendered_in_streaming_and_reload_paths() -> None:
    streaming = (REPO_ROOT / "src" / "row_bot" / "ui" / "streaming.py").read_text(encoding="utf-8")
    render = (REPO_ROOT / "src" / "row_bot" / "ui" / "render.py").read_text(encoding="utf-8")

    for marker in ("__IMAGE__:", "__HTML__:", "__CHART__:"):
        assert marker in streaming
        assert marker in render
    assert "render_image_with_save" in streaming
    assert "ui.html(" in streaming


def test_agent_collects_plugin_destructive_tool_names() -> None:
    agent = (REPO_ROOT / "src" / "row_bot" / "agent.py").read_text(encoding="utf-8")

    assert "plugin_registry_mod.get_destructive_names(allow_names=allow_set)" in agent


def test_app_startup_loads_plugins_before_mcp_and_schedules_agent_prewarm_after_ready() -> None:
    app_source = (REPO_ROOT / "src" / "row_bot" / "app.py").read_text(encoding="utf-8")

    plugin_index = app_source.index("from row_bot.plugins.loader import refresh_plugin_runtime")
    mcp_index = app_source.index("from row_bot.mcp_client.runtime import discover_enabled_servers")
    ready_index = app_source.rindex("_st.startup_ready = True")
    graph_schedule_index = app_source.rindex("_schedule_agent_graph_prewarm()")

    assert plugin_index < mcp_index < ready_index < graph_schedule_index
    assert "refresh_plugin_runtime," in app_source
    assert "discover_mcp=False" in app_source


def test_plugin_center_and_marketplace_use_runtime_refresh_helper() -> None:
    settings_source = (REPO_ROOT / "src" / "row_bot" / "plugins" / "ui_settings.py").read_text(encoding="utf-8")
    marketplace_source = (REPO_ROOT / "src" / "row_bot" / "plugins" / "ui_marketplace.py").read_text(encoding="utf-8")

    assert "plugin_loader.refresh_plugin_runtime" in settings_source
    assert "loader.refresh_plugin_runtime" in marketplace_source
    assert "clear_agent_cache after plugin toggle" not in settings_source


def test_plugin_center_install_flow_copy_is_clear_about_kept_off_state() -> None:
    settings_source = (REPO_ROOT / "src" / "row_bot" / "plugins" / "ui_settings.py").read_text(encoding="utf-8")
    marketplace_source = (REPO_ROOT / "src" / "row_bot" / "plugins" / "ui_marketplace.py").read_text(encoding="utf-8")

    assert "Install Disabled" not in marketplace_source
    assert "installed disabled until" not in marketplace_source
    assert "Install and Keep Off" in marketplace_source
    assert "copy this plugin now and keep it off" in marketplace_source
    assert '"Run Test"' in settings_source
    assert '"Enable Plugin"' in settings_source
    assert '"Disable Plugin"' in settings_source
