from __future__ import annotations

from pathlib import Path

from ui.tool_trace import (
    canonical_tool_name,
    display_tool_content,
    group_tool_results,
    is_browser_tool_name,
)


def test_tool_results_group_by_name_without_losing_entries():
    results = [
        {"name": "web_search", "content": "first"},
        {"name": "browser_click", "content": "clicked"},
        {"name": "web_search", "content": "second"},
        {"name": "browser_click", "content": "clicked again"},
        {"name": "workspace_read_file", "content": "file"},
    ]

    groups = group_tool_results(results)

    assert [g.name for g in groups] == [
        "web_search",
        "Browser activity",
        "workspace_read_file",
    ]
    assert [g.count for g in groups] == [2, 2, 1]
    assert [r["content"] for r in groups[0].results] == ["first", "second"]
    assert [r["content"] for r in groups[1].results] == ["clicked", "clicked again"]


def test_browser_group_labels_are_activity_summaries():
    group = group_tool_results(
        [
            {"name": "browser_navigate", "content": "url"},
            {"name": "browser_click", "content": "clicked"},
            {"name": "browser_scroll", "content": "scrolled"},
        ]
    )[0]

    assert canonical_tool_name("browser_click") == "Browser Click"
    assert is_browser_tool_name("browser_click")
    assert is_browser_tool_name("Browser Click")
    assert group.name == "Browser activity"
    assert group.label == "Browser activity · 3 steps"


def test_tool_content_truncates_only_for_display():
    raw = "x" * 20

    assert display_tool_content(raw, limit=10) == "x" * 10 + "\n\n… (truncated)"
    assert raw == "x" * 20


def test_chat_tool_trace_source_contracts():
    render_src = Path("ui/render.py").read_text(encoding="utf-8")
    streaming_src = Path("ui/streaming.py").read_text(encoding="utf-8")
    chat_src = Path("ui/chat.py").read_text(encoding="utf-8")
    components_src = Path("ui/chat_components.py").read_text(encoding="utf-8")
    installer_src = Path("installer/thoth_setup.iss").read_text(encoding="utf-8")

    assert "group_tool_results" in render_src
    assert "group_tool_results" in streaming_src or "_finish_live_tool_result" in streaming_src
    assert "_add_live_tool_pending" in streaming_src
    assert "_finish_live_tool_result" in streaming_src
    assert "browser_step_count += 1" in streaming_src
    assert "_capture_balanced_browser_screenshot" in streaming_src
    assert "render_image_with_save(\n                                _b64_ss" not in streaming_src
    assert "Model selection now lives in the composer" in chat_src
    assert "_build_inline_model_picker" in chat_src
    assert 'list_model_choice_options("chat"' not in chat_src
    assert "on_model_change" not in chat_src
    assert "model_banner_container" in chat_src
    assert "on_model_switch=_refresh_model_surface" in chat_src
    assert "p.chat_scroll.style(replace=" in chat_src
    assert "tooltip(\"Select model for this thread\")" not in chat_src
    assert "on_model_change" not in components_src
    assert "on_model_switch" in components_src
    assert "if val == _picker_val" in components_src
    assert "get_model_max_context" in components_src
    assert "tool_trace.py" in installer_src
