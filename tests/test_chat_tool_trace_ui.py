from __future__ import annotations

import json
from pathlib import Path

from row_bot.ui.tool_trace import (
    agent_runs_from_payload,
    canonical_tool_name,
    display_tool_content,
    group_tool_results,
    is_agent_tool_result,
    is_browser_tool_name,
    parse_agent_tool_payload,
    tool_result_failed,
    tool_group_completion_summary,
)
from row_bot.computer_use.service import ComputerUseError
from row_bot.tools.computer_use_tool import _computer_error_payload


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


def test_tool_content_uses_safe_summary_instead_of_private_json_payload():
    private_title = "secret@example.test - Private inbox"
    raw = json.dumps(
        {
            "windows": [{"app": "Edge", "window": private_title}],
            "display_summary": "Found one matching Notepad window.",
        }
    )

    displayed = display_tool_content(raw)

    assert displayed == "Found one matching Notepad window."
    assert private_title not in displayed


def test_structured_computer_errors_are_failed_without_exposing_private_payload() -> None:
    private_value = "do not display this value"
    content = json.dumps(
        {
            "ok": False,
            "error": True,
            "error_code": "invalid_input",
            "display_summary": "Computer action needs valid input.",
            "private": private_value,
        }
    )

    assert tool_result_failed(content) is True
    assert display_tool_content(content) == "Computer action needs valid input."
    assert private_value not in display_tool_content(content)


def test_protected_computer_surface_is_terminal_and_never_a_driver_failure() -> None:
    content = _computer_error_payload(
        "list_windows",
        ComputerUseError(
            "Row-Bot and its Computer control surfaces cannot be targeted.",
            code="hard_blocked",
        ),
    )
    payload = json.loads(content)

    assert payload["error_code"] == "hard_blocked"
    assert payload["retryable"] is False
    assert payload["terminal"] is True
    assert "protected" in payload["display_summary"].casefold()
    assert "driver" not in payload["display_summary"].casefold()
    assert tool_result_failed(content) is True


def test_computer_group_with_recovered_error_is_not_a_clean_success() -> None:
    group = group_tool_results(
        [
            {"name": "computer_use", "content": "Captured the selected target."},
            {
                "name": "computer_use",
                "content": json.dumps(
                    {
                        "ok": False,
                        "error": True,
                        "error_code": "driver_failed",
                        "display_summary": "Computer action failed safely.",
                    }
                ),
            },
            {"name": "computer_use", "content": "Captured fresh verification."},
        ]
    )[0]

    assert any(tool_result_failed(item) for item in group.results)
    assert "error" not in group.label.lower()
    assert tool_group_completion_summary(group.results) == "2 succeeded · 1 failed"


def test_agent_tool_payload_is_detected_from_agent_tool_json():
    result = {
        "name": "Agents",
        "content": json.dumps(
            {
                "ok": True,
                "message": "Agent completed.",
                "run": {
                    "id": "run-1",
                    "display_name": "PDF essay writer",
                    "status": "completed",
                    "summary": "Created the PDF.",
                },
            }
        ),
    }

    payload = parse_agent_tool_payload(result)

    assert payload is not None
    assert is_agent_tool_result(result)
    assert [run["id"] for run in agent_runs_from_payload(payload)] == ["run-1"]


def test_agent_tool_payload_detection_is_conservative_for_other_tools():
    assert not is_agent_tool_result({"name": "workspace_read_file", "content": "{}"})
    assert parse_agent_tool_payload({"name": "Agents", "content": "not json"}) is None


def test_agent_tool_cards_dedupe_runs_by_id_without_dropping_raw_payloads():
    from row_bot.ui import render

    results = [
        {
            "name": "delegate_work",
            "content": json.dumps(
                {
                    "message": "Child Agent started.",
                    "run": {
                        "id": "run-1",
                        "display_name": "Alpha",
                        "status": "queued",
                    },
                }
            ),
        },
        {
            "name": "agent_wait",
            "content": json.dumps(
                {
                    "message": "Agent completed.",
                    "run": {
                        "id": "run-1",
                        "display_name": "Alpha",
                        "status": "completed",
                        "summary": "alpha ok",
                    },
                }
            ),
        },
    ]

    card_runs, raw_results = render._agent_card_runs_from_tool_results(results)

    assert len(card_runs) == 1
    assert card_runs[0][0]["id"] == "run-1"
    assert card_runs[0][0]["status"] == "completed"
    assert card_runs[0][1] == "Agent completed."
    assert raw_results == results


def test_chat_tool_trace_source_contracts():
    agent_src = Path("src/row_bot/agent.py").read_text(encoding="utf-8")
    app_src = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    render_src = Path("src/row_bot/ui/render.py").read_text(encoding="utf-8")
    state_src = Path("src/row_bot/ui/state.py").read_text(encoding="utf-8")
    streaming_src = Path("src/row_bot/ui/streaming.py").read_text(encoding="utf-8")
    chat_src = Path("src/row_bot/ui/chat.py").read_text(encoding="utf-8")
    components_src = Path("src/row_bot/ui/chat_components.py").read_text(encoding="utf-8")
    installer_src = Path("installer/row_bot_setup.iss").read_text(encoding="utf-8")

    assert "group_tool_results" in render_src
    assert "render_agent_tool_result" in render_src
    assert "render_agent_tool_results" in render_src
    assert "safe_timer(1.0, _tick)" in render_src
    assert "_agent_run_is_terminal" in render_src
    assert "_agent_card_runs_from_tool_results" in render_src
    assert "agent_tool_results: list[dict]" in render_src
    assert "agent_tool_results," in render_src
    assert "agent_run_ids" in render_src
    assert "agent_result_use_prompt" in render_src
    assert "agent_result_use_available" in render_src
    assert "on_use_agent_result" in render_src
    assert "agent_result_use_prompt" in app_src
    assert "_ask_parent_to_use_agent_result" in app_src
    assert "_ask_parent_to_use_agent_result" in chat_src
    assert "asyncio.create_task(send_message(prompt))" in chat_src
    assert "on_use_agent_result=_ask_parent_to_use_agent_result" in chat_src
    assert "Raw Agent tool output" in render_src
    assert "group_tool_results" in streaming_src or "_finish_live_tool_result" in streaming_src
    assert "refresh_parent_agent_strip" in streaming_src
    assert "parse_agent_spawn_text" in streaming_src
    assert "if direct_agent_command_text:" in streaming_src
    assert "_handle_direct_agent_spawn" in streaming_src
    assert "turn_boundary" in streaming_src
    assert "agent_run_refresh_key" in streaming_src
    assert "is_agent_tool_result" in streaming_src
    assert "has_agent_tool_results = any(" in streaming_src
    assert "or has_agent_tool_results" in streaming_src
    assert "live_row" in state_src
    assert "def _delete_live_generation_row" in streaming_src
    assert "_delete_live_generation_row(gen)" in streaming_src
    assert "_add_live_tool_pending" in streaming_src
    assert "_finish_live_tool_result" in streaming_src
    assert "_agent_tool_results," in chat_src
    assert "is_agent_tool_result(_tr)" in chat_src
    assert "ToolCallPayload" in agent_src
    assert "_tool_call_payload(tc)" in agent_src
    assert "_tool_event_raw_name" in streaming_src
    assert "_schedule_delegated_agent_card_probe" in streaming_src
    assert "_append_delegated_agent_card_message" in streaming_src
    assert "_thread_has_attached_live_generation" in streaming_src
    assert "_render_live_agent_run_card" in streaming_src
    assert "_agent_tool_result_already_live" in streaming_src
    assert "live_agent_run_ids" in state_src
    assert "live_async_agent_run_ids" in state_src
    assert "baseline_child_agent_run_ids" in state_src
    assert "render_agent_run_cards" in render_src
    assert "_filter_visible_agent_tool_results" in streaming_src
    assert "_ordered_agent_run_ids_from_tool_results" in streaming_src
    assert "promoted_agent_run_ids" in streaming_src
    assert "_ordered_agent_run_ids_from_tool_results" in chat_src
    assert "_schedule_direct_agent_card_refresh" in chat_src
    assert "_schedule_agent_tool_result_card_refresh" in streaming_src
    assert "_schedule_agent_tool_result_card_refresh" in chat_src
    assert "_append_async_delegated_agent_completion_messages" in streaming_src
    assert "_append_async_delegated_agent_completion_messages" in app_src
    assert "_async_delegated_run_ids_from_tool_results" in streaming_src
    assert "_async_child_agent_run_ids_for_generation" in streaming_src
    assert "_current_child_agent_run_ids" in app_src
    assert "child_run_ids = _current_child_agent_run_ids(tid)" in app_src
    assert "candidate_run_ids=child_run_ids" in app_src
    assert "_update_direct_agent_refresh_keys(" in app_src
    assert "card_changed" in app_src
    assert "async_completion_run_ids" in streaming_src
    assert "refresh_model_controls_on_done" in state_src
    assert "model_controls_container" in state_src
    assert "refresh_model_controls" in state_src
    assert "cb.refresh_model_controls" in app_src
    assert "_poll_agent_card_refresh" in app_src
    assert "_last_agent_run_refresh" in app_src
    assert "list_agent_runs(parent_thread_id=tid, kind=\"subagent\"" in app_src
    assert "def _thread_has_live_generation(tid: str)" in app_src
    assert "live_generation = _thread_has_live_generation(tid)" in app_src
    assert "_thread_has_attached_live_generation(tid)" in app_src
    agent_poll = app_src.split("def _poll_agent_card_refresh", 1)[1].split(
        "def _poll_notifications",
        1,
    )[0]
    orchestration_reload = app_src.split(
        "def _reload_completed_orchestration_transcript",
        1,
    )[1].split("def _current_child_agent_run_ids", 1)[0]
    assert "p.transcript_rendered_keys = []" not in agent_poll
    assert "state.messages = load_thread_messages(tid)" not in orchestration_reload
    assert "upsert_durable_transcript_message" in orchestration_reload
    assert 'keys["sidebar"]' in agent_poll
    assert 'keys["strip"]' in agent_poll
    assert 'keys["transcript"]' in agent_poll
    assert agent_poll.index("if not transcript_inspection_needed:") < agent_poll.index(
        "child_run_ids = _current_child_agent_run_ids(tid)"
    )
    assert agent_poll.index(
        "_sync_thread_approval_messages("
    ) < agent_poll.index("if live_generation:")
    assert "_sync_child_agent_approval_messages(" not in agent_poll
    assert "p.refresh_model_controls = _refresh_model_controls" in chat_src
    assert "_interrupt_changes_model_setting(pending)" in streaming_src
    assert "_schedule_model_controls_refresh(cb)" in streaming_src
    assert "persistent transition-show=fade transition-hide=fade" in streaming_src
    assert "defer_ui(p.interrupt_dlg.open" in streaming_src
    assert "browser_step_count += 1" in streaming_src
    assert "_capture_balanced_browser_screenshot" in streaming_src
    assert "render_image_with_save(\n                                _b64_ss" not in streaming_src
    assert "Model selection now lives in the composer" in chat_src
    assert "build_composer_policy_cluster" in chat_src
    assert 'list_model_choice_options("chat"' not in chat_src
    assert "on_model_change" not in chat_src
    assert "model_banner_container" in chat_src
    assert "on_model_switch=_refresh_model_surface" in chat_src
    assert "p.chat_scroll.style(replace=" in chat_src
    assert "tooltip(\"Select model for this thread\")" not in chat_src
    assert "on_model_change" not in components_src
    assert "on_model_switch" in components_src
    assert "_MORE_MODELS_SENTINEL" in components_src
    assert "async def _on_model_pick" in components_src
    picker_section = components_src.split("def _build_inline_model_picker", 1)[1]
    assert "use-input" not in picker_section
    assert "options-dense" in picker_section
    assert "if val == _picker_val" in components_src
    assert "get_model_max_context" in components_src
    assert Path("src/row_bot/ui/tool_trace.py").exists()
    assert 'Source: "..\\src\\row_bot\\*"' in installer_src
