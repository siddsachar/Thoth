from __future__ import annotations

import ast
from pathlib import Path


SETTINGS = Path("ui/settings.py")


def _function_source(name: str) -> str:
    source = SETTINGS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found")


def test_settings_sections_moved_to_requested_tabs():
    system_src = _function_source("_build_system_access_tab")
    prefs_src = _function_source("_build_preferences_tab")
    knowledge_src = _function_source("_build_knowledge_tab")
    channels_src = _function_source("_build_channels_tab")

    assert "_build_window_mode_section()" in prefs_src
    assert "_build_window_mode_section()" not in system_src
    assert "_build_dream_cycle_section()" in prefs_src
    assert "Dream Cycle" not in knowledge_src
    assert "_build_tunnel_settings_section()" in system_src
    assert "_build_tunnel_settings_section()" not in channels_src
    assert "Tunnel credentials are in System" in channels_src


def test_settings_polish_helpers_are_local_and_used():
    settings_src = SETTINGS.read_text(encoding="utf-8")

    for helper in (
        "_settings_header",
        "_settings_section",
        "_metric_chip",
        "_status_dot",
    ):
        assert f"def {helper}" in settings_src

    for tab_name in (
        "_build_documents_tab",
        "_build_tools_tab",
        "_build_system_access_tab",
        "_build_utilities_tab",
        "_build_tracker_tab",
        "_build_knowledge_tab",
        "_build_voice_tab",
        "_build_channels_tab",
        "_build_preferences_tab",
    ):
        assert "_settings_header(" in _function_source(tab_name)


def test_status_and_home_links_follow_new_information_architecture():
    status_src = Path("ui/status_checks.py").read_text(encoding="utf-8")
    home_src = Path("ui/home.py").read_text(encoding="utf-8")
    tunnel_src = status_src.split("def check_tunnel", 1)[1].split("def check_gmail_oauth", 1)[0]
    dream_src = status_src.split("def check_dream_cycle", 1)[1].split("def check_tts", 1)[0]

    assert 'CheckResult("Tunnel"' in status_src
    assert 'settings_tab="Channels"' in tunnel_src
    assert 'CheckResult("Dream Cycle"' in status_src
    assert 'settings_tab="Knowledge"' in dream_src
    assert "Settings \u2192 Preferences" in home_src
    assert "Settings \u2192 Knowledge" not in home_src
