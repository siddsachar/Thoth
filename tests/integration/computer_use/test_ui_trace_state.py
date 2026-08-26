from __future__ import annotations

from pathlib import Path
import json

import pytest

from row_bot.ui.tool_trace import (
    canonical_tool_name,
    group_tool_results,
    is_computer_tool_name,
    tool_result_failed,
)


def test_computer_trace_is_separate_from_browser_and_grouped() -> None:
    groups = group_tool_results([
        {"name": "browser_click", "content": "web"},
        {"name": "computer_use", "content": "native one"},
        {"name": "computer_use", "content": "native two"},
    ])
    assert [group.name for group in groups] == ["Browser activity", "Computer activity"]
    assert groups[1].label == "Computer activity · 2 steps"
    assert canonical_tool_name("computer_use") == "Computer activity"
    assert is_computer_tool_name("computer_use")


def test_active_session_and_settings_sources_expose_required_local_controls() -> None:
    source = Path("src/row_bot/ui/computer_use.py").read_text(encoding="utf-8")
    live_control = Path("src/row_bot/ui/live_control.py").read_text(encoding="utf-8")
    settings = Path("src/row_bot/ui/settings.py").read_text(encoding="utf-8")
    streaming = Path("src/row_bot/ui/streaming.py").read_text(encoding="utf-8")
    for label in (
        "Computer Use (Beta)",
        "Computer Use is ready",
        "Install Computer Use",
        "Check setup",
        "Test Computer Use",
        "Technical details",
        "Manage Computer Use",
        "Developer options",
        "Open Accessibility Settings",
        "Open Screen Recording Settings",
        "macOS is blocking Computer Use",
    ):
        assert label in source
    assert "Privacy_Accessibility" in source
    assert "Privacy_ScreenCapture" in source
    for label in (
        "Stop",
        "Take over",
        "Resume",
        "Hide picture",
        "Live picture",
        "This app only",
        "This task tab only",
    ):
        assert label in live_control
    assert 'resume_button.set_text("Resume")' in live_control
    assert 'else "Done"' not in live_control
    assert 'reason = "browser_task_stop"' in live_control
    assert "fit=contain" in live_control
    assert "width: min(100%, 420px)" in live_control
    assert "previous.engine != view.engine" in live_control
    assert "_clear_preview(reset_preference=True)" in live_control
    assert "_sync_visibility(card, view.active)" in live_control
    assert "def _sync_visibility(" in live_control
    assert 'element.classes(remove="hidden")' in live_control
    assert 'element.classes(add="hidden")' in live_control
    assert '"display: none !important"' in live_control
    assert 'visible_display="flex"' in live_control
    assert "element.update()" in live_control
    assert "Windows/macOS" not in source
    assert "build_active_session_card(compact=True)" not in streaming
    for path in (
        "src/row_bot/ui/chat.py",
        "src/row_bot/ui/chat_components.py",
        "src/row_bot/ui/mobile_chat.py",
    ):
        assert "build_live_control_dock" in Path(path).read_text(encoding="utf-8")
    state_source = Path("src/row_bot/ui/state.py").read_text(encoding="utf-8")
    assert 'self.pending_interrupt_generation_id: str = ""' in state_source
    assert "self.pending_interrupt_tool_groups: dict = {}" in state_source
    assert 'self.pending_interrupt_runtime_surface: str = ""' in state_source
    assert 'generation_id = source_generation_id or f"{gen_thread_id}:{uuid.uuid4().hex[:12]}"' in streaming
    assert "gen.pending_tools = pending_tool_groups" in streaming
    assert "_approval_resume_runtime_surface" in streaming
    assert '"Stop task"' in streaming
    assert "_stop_pending_approval(state, p, cb)" in streaming
    assert 'runtime_surface = "developer" if is_developer else "designer" if is_designer else "approval"' not in streaming
    assert "profile_runtime_config = await run.io_bound(_profile_runtime_config_for_thread, gen_thread_id)" in streaming
    assert "Browser & Computer Use" in settings
    assert "Move the existing one-per-run Browser frame into the live panel" in streaming


def test_approval_resume_preserves_only_the_expected_interactive_origin() -> None:
    from row_bot.ui.streaming import _approval_resume_runtime_surface

    assert _approval_resume_runtime_surface(
        "normal_chat",
        is_developer=False,
        is_designer=False,
    ) == "normal_chat"
    assert _approval_resume_runtime_surface(
        "developer",
        is_developer=True,
        is_designer=False,
    ) == "developer"
    assert _approval_resume_runtime_surface(
        "",
        is_developer=False,
        is_designer=False,
    ) == "normal_chat"
    with pytest.raises(ValueError):
        _approval_resume_runtime_surface(
            "channel",
            is_developer=False,
            is_designer=False,
        )
    with pytest.raises(ValueError):
        _approval_resume_runtime_surface(
            "approval",
            is_developer=False,
            is_designer=False,
        )


def test_dispatched_but_unverified_computer_receipt_is_not_rendered_as_failure() -> None:
    current = json.dumps({
        "ok": True,
        "error": False,
        "action_dispatched": True,
        "action_completed": True,
        "driver_effect": "unverifiable",
        "visual_change": "unchanged",
        "effect_verified": False,
        "display_summary": "Input delivered; visible result unchanged.",
    })
    older = json.dumps({
        "ok": True,
        "action_dispatched": True,
        "action_completed": False,
        "effect_verified": False,
        "display_summary": "Input sent; effect not verified.",
    })
    structured_error = json.dumps({
        "ok": False,
        "error": True,
        "error_code": "target_mismatch",
        "display_summary": "Target changed.",
    })

    assert tool_result_failed(current) is False
    assert tool_result_failed(older) is False
    assert tool_result_failed(structured_error) is True
