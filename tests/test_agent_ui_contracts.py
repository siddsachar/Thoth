from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


ROOT = Path("src/row_bot")


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_shared_chat_surface_binds_checkpoint_reconciliation_state():
    from row_bot.ui.chat_components import _bind_shared_transcript_state
    from row_bot.ui.transcript import message_keys

    surface = SimpleNamespace(
        transcript_thread_id="stale-thread",
        transcript_generation=4,
        transcript_rendered_keys=["stale"],
        transcript_window_start=10,
        transcript_window_size=1,
        transcript_total=99,
    )
    state = SimpleNamespace(thread_id="developer-thread")
    messages = [
        {"role": "user", "content": "Build the calculator"},
        {"role": "assistant", "content": "Inspection is still active"},
    ]

    _bind_shared_transcript_state(surface, state, messages)

    assert surface.transcript_thread_id == "developer-thread"
    assert surface.transcript_generation == 5
    assert surface.transcript_rendered_keys == message_keys(messages)
    assert surface.transcript_window_start == 0
    assert surface.transcript_window_size == 2
    assert surface.transcript_total == 2


def test_terminal_orchestration_never_offers_stop_for_stale_active_counts():
    from row_bot.ui.agent_drawer import (
        agent_retry_available,
        orchestration_group_control,
    )

    assert orchestration_group_control("stopped", {"active": 1}) == ""
    assert orchestration_group_control("completed_partial", {"active": 2}) == ""
    assert orchestration_group_control("waiting_children", {"active": 1}) == "stop_all"
    assert agent_retry_available("stopped", "waiting_children") is True
    assert agent_retry_available("failed", "completed_partial") is False
    assert agent_retry_available("stopped", "stopped") is False


def test_activity_center_uses_current_agent_goal_runs_and_channel_monitor():
    src = _read("ui/command_center.py")

    assert "Activity Center" in src
    assert "Current goals and agents" in src
    assert "Current work, approvals, schedules" not in src
    assert "prepare_channel_goal_start" not in src
    assert "list_goals" in src
    assert "def _activity_attention_statuses" in src
    assert '_ACTIVITY_AGENT_ATTENTION_STATUSES = ("waiting_approval", "waiting_user", "blocked")' in src
    goal_section = src.split("def _rebuild_goal_activity", 1)[1].split("def _render_goal_activity_row", 1)[0]
    assert "waiting_user" not in goal_section
    assert "failed" not in goal_section
    assert 'status in {"active", "paused"}' in goal_section
    assert "Needs Attention" in src
    assert "Running Now" in src
    assert "row-bot-approvals-card" in src
    assert "row-bot-workflows-card" in src
    assert 'ui.label("Workflows")' in src
    assert src.index("Activity Center") < src.index("row-bot-approvals-card")
    assert src.index("row-bot-approvals-card") < src.index("row-bot-workflows-card")
    assert src.index("row-bot-workflows-card") < src.index("build_channel_monitor")
    assert src.index("row-bot-workflows-card") < src.index('ui.expansion("Insights"')
    activity_block = src.split("Activity Center", 1)[1].split("row-bot-approvals-card", 1)[0]
    approvals_block = src.split("row-bot-approvals-card", 1)[1].split("row-bot-workflows-card", 1)[0]
    workflows_block = src.split("row-bot-workflows-card", 1)[1].split("build_channel_monitor", 1)[0]
    assert "_approvals_container" not in activity_block
    assert "_upcoming_container" not in activity_block
    assert "_task_select" not in activity_block
    assert "_approvals_container" in approvals_block
    assert "get_pending_approvals" in approvals_block
    assert "payload_from_row" in approvals_block
    assert "compact_message" in approvals_block
    assert "_upcoming_container" not in approvals_block
    assert "_task_select" not in approvals_block
    assert "Active Workflows" in workflows_block
    assert "Upcoming" in workflows_block
    assert "Launch" in workflows_block
    assert workflows_block.index("Active Workflows") < workflows_block.index("Upcoming")
    assert workflows_block.index("Upcoming") < workflows_block.index("Launch")
    assert "get_running_tasks" in workflows_block
    assert "get_next_fire_times" in workflows_block
    assert "list_tasks" in workflows_block
    assert "run_task_background" in workflows_block
    assert "_approvals_container" not in workflows_block
    assert "list_agent_runs" in src
    assert 'kind="subagent"' in src
    agent_section = src.split("def _rebuild_agent_runs", 1)[1].split("ui.label(label)", 1)[0]
    assert '"failed", "timed_out"' not in agent_section
    assert "stop_agent_run" in src
    assert "Current Chat Agents" in src
    assert "No active Agents" in src
    assert "open_agent_peek_dialog" in src
    assert "Worktree" in src
    assert "_agent_workspace_tooltip" in src
    assert "_agent_preview_state" in src
    assert "_agent_preview_container" in src
    assert "_show_agent_preview" in src
    assert "list_agent_runs(limit=12)" not in src
    assert "Recent Workflows" not in src
    assert "get_recent_runs" not in src
    assert "completed in this chat" not in src
    assert "hidden_completed" not in src
    assert "build_channel_monitor" in src
    assert "Open Settings > Channels" in src
    assert src.index("build_channel_monitor") < src.index('ui.expansion("Insights"')
    assert "list_agent_profiles" not in src
    assert "_library_container" not in src
    assert "Allowed tools" not in src
    assert "Denied tools" not in src


def test_profile_library_lives_in_left_sidebar():
    sidebar = _read("ui/sidebar.py")
    library = _read("ui/profile_library.py")
    task_dialog = _read("ui/task_dialog.py")
    profile_panel = library.split("def build_profile_library", 1)[1]

    assert "build_profile_library" in sidebar
    assert "Channels" not in sidebar
    assert "build_profile_library" in library
    assert "Agent profiles" in library
    assert "Toggle Agent profiles panel" in library
    assert "open_profile_view_dialog" in library
    assert "open_profile_editor_dialog" in library
    assert "list_agent_tool_catalog" in library
    assert "Core" in library
    assert "MCP" in library
    assert "Plugins" in library
    assert "Custom Tools" in library
    assert "Purpose" in library
    assert "Profile command" in library
    assert "width: min(920px, 96vw)" in library
    assert "rows=8" in library
    assert "Routing hint" in library
    assert "ui.checkbox" in library
    assert "_TOOL_ACCESS_OPTIONS" in library
    assert "allow_tools" in library
    assert "Selected tools only" in library
    assert "hard runtime boundary" in library
    assert "inherits enabled tools" in library
    assert "_create_profile_chat_thread" in library
    assert "skills_override" in library
    assert "Pinned skills for this profile" in library
    assert "Smart skill suggestions still work normally" in library
    assert "File editing workspace" in library
    assert "Use Worktree" in library
    assert "Child Agent" in task_dialog
    assert "Delegate to Agent" not in task_dialog
    assert "What should the agent do?" in task_dialog
    assert "Editing safety" in task_dialog
    assert "Wait for Agents" in task_dialog
    assert "Wait for Child Agents" not in task_dialog
    assert "Agent run IDs (optional)" in task_dialog
    assert "Select a Developer workspace for worktree mode." in task_dialog
    assert "Profile shortcut" not in library
    assert "Suggested skills" not in library
    assert "inherit_skills" not in library
    assert "list_agent_profiles" in library
    assert "duplicate_agent_profile" in library
    assert "save_agent_profile" in library
    assert "Start chat" in library
    assert "View Profile" in library
    assert "_toggle_profile_panel" in library
    assert "_set_profile_panel_open" in library
    assert "_refresh_profile_surfaces" in library
    assert "_refresh_sidebar_summary" in library
    assert "drawer.show()" in library
    assert "_duplicate_profile_from_panel" in library
    assert "duplicate_agent_profile(_profile_ref(profile))" in library
    assert "_refresh_profile_panel_after_action()" in library
    assert "on_saved=_refresh_profile_surfaces" in library
    assert "summary_label_ref" in library
    assert "ui.left_drawer" in profile_panel
    assert "_PROFILE_LIBRARY_DRAWER_CSS" in library
    assert "ui.add_head_html(_PROFILE_LIBRARY_DRAWER_CSS)" in library
    assert 'drawer._props["data-profile-library-drawer"] = "1"' in library
    assert '[data-profile-library-drawer="1"]' in library
    assert "left: 300px !important" in library
    assert 'panel_state: dict[str, Any] = {"drawer": None' in library
    assert 'panel_state["dialog"]' not in library
    assert "_discard_profile_panel" not in library
    assert 'dialog.on("hide"' not in profile_panel
    assert "position=left transition-show=slide-right transition-hide=slide-left" not in profile_panel
    assert "row-bot-profile-library-drawer" in library
    assert "no-swipe-open no-swipe-close" in library
    assert "chevron_right" in library
    assert "chevron_left" in library
    assert "grid-template-columns: 22px minmax(0, 1fr) auto" in library
    assert "deny_memory_write" not in library
    assert "allow_tool_groups" not in library
    assert "include_memory" not in library


def test_channel_monitor_component_preserves_channel_contract():
    src = _read("ui/channel_monitor.py")

    assert "all_channels" in src
    assert "get_last_activity" in src
    assert "SESSION_DIR" in src
    assert "_get_user_phone" in src
    assert "open_settings(\"Channels\")" in src
    assert "safe_timer(5.0" in src
    assert "telegram" in src
    assert "whatsapp" in src


def test_transcript_uses_one_standalone_actionable_child_approval_card():
    src = _read("ui/render.py")
    agent_card = src.split("def _render_agent_run_card", 1)[1].split(
        "def _agent_card_runs_from_tool_results",
        1,
    )[0]

    assert "_render_approval_request_card" not in agent_card
    assert 'if role == "assistant" and msg.get("approval_request_id")' in src


def test_global_profile_picker_contract():
    picker = _read("ui/profile_picker.py")
    controls = _read("ui/chat_components.py")
    chat = _read("ui/chat.py")
    designer = Path("src/row_bot/designer/editor.py").read_text(encoding="utf-8")
    developer = Path("src/row_bot/developer/ui.py").read_text(encoding="utf-8")

    assert "list_agent_profiles" in picker
    assert "_set_thread_agent_profile" in picker
    assert "_clear_thread_agent_profile" in picker
    assert "set_thread_skills_override" in picker
    assert "skills_override" in picker
    assert "clear_agent_cache" in picker
    assert "_apply_profile_picker_selection" in picker
    assert "open_profile_view_dialog" in picker
    assert "Developer workspace permissions still apply" in picker
    assert "ensure_composer_control_css" in picker
    assert "row-bot-composer-control-group" in picker
    assert "row-bot-composer-select" in picker
    assert "dense borderless options-dense hide-bottom-space" in picker
    assert "label=label" not in picker
    assert ".row-bot-composer-select .q-field__label" in controls
    assert "display: none !important;" in controls
    assert "build_profile_picker" in chat
    assert "build_profile_picker" in designer
    assert "build_profile_picker" in developer


def test_developer_studio_live_testing_polish_contracts():
    developer = Path("src/row_bot/developer/ui.py").read_text(encoding="utf-8")
    sidebar = _read("ui/sidebar.py")
    chat = _read("ui/chat.py")

    assert "Review this repo" not in developer
    assert "Fix failing tests" not in developer
    assert "Add a feature" not in developer
    assert "Explain architecture" not in developer
    assert "Prepare a PR" not in developer
    assert "Branch" in developer
    assert "Feature branch" not in developer
    assert "New Thread in Worktree (Recommended)" in developer
    assert "New Thread in Current Folder" in developer
    assert "Start in Worktree" in developer
    assert "Use current folder" in developer
    assert "def _worktree_action_state" in developer
    assert 'repo_label = "In Git"' in developer
    assert "Repo root:" in developer
    assert "Original folder has changes not in this Worktree" in developer
    assert "row-bot-composer-select" in developer
    assert 'label="Run in"' not in developer
    assert "Choose where Developer commands run" in developer
    assert "width: min(840px, 94vw)" in sidebar
    assert 'classes("w-96")' not in sidebar
    assert "Child Agent of" in chat
    assert "Back to Parent" in chat
    assert "get_agent_run_for_thread" in chat


def test_agent_drawer_contract_and_surface_usage():
    drawer = _read("ui/agent_drawer.py")
    chat = _read("ui/chat.py")
    designer = Path("src/row_bot/designer/editor.py").read_text(encoding="utf-8")
    developer = Path("src/row_bot/developer/ui.py").read_text(encoding="utf-8")
    app = Path("src/row_bot/app.py").read_text(encoding="utf-8")

    assert "build_parent_agent_drawer" in drawer
    assert "Ã‚" not in drawer
    assert "Â" not in drawer
    assert "orchestration_status_label" in drawer
    assert "orchestration_group_control" in drawer
    assert "Cancel final answer" in drawer
    assert '"min-height: 30px; flex-wrap: wrap;"' in drawer
    assert "-webkit-line-clamp: 2" in drawer
    assert "open_agent_peek_dialog" in drawer
    assert "open_in_new" in drawer
    assert "_open_agent_thread" in drawer
    assert "_active_generations" in drawer
    assert "from row_bot.ui.streaming import _detach_generation" in drawer
    assert '_detach_generation(prev_gen, state, "open_agent_child_thread")' in drawer
    assert "load_thread_messages" in drawer
    assert "set_active_thread" in drawer
    assert "list_agent_runs" in drawer
    assert 'kind="subagent"' in drawer
    assert "stop_agent_run" in drawer
    assert "get_agent_parent_messages" in drawer
    assert "Note queued:" in drawer
    assert "Worktree" in drawer
    assert "_open_agent_worktree" in drawer
    assert "_show_agent_worktree_compare" in drawer
    assert "folder_open" in drawer
    assert "difference" in drawer
    assert "Open worktree" in drawer
    assert "Compare" in drawer
    assert "model_iterations_used" not in drawer
    assert "delegation depth" not in drawer
    assert "open_agent_thread" in drawer
    assert "_get_thread_developer_workspace" in drawer
    assert "_get_thread_type" in drawer
    assert 'target_thread_type == "agent_child"' in drawer
    assert "build_profile_picker" in chat
    assert "_render_thread_profile_chip" not in chat
    assert "_render_parent_agent_strip" in chat
    assert "parent_agent_strip_container" in chat
    assert "refresh_parent_agent_strip" in chat
    assert "if p.parent_agent_dialog_open:" in chat
    assert "p.parent_agent_dialog_open = True" in drawer
    assert '"hide"' in drawer
    assert "_refresh_parent_agent_strip()" in chat
    assert "build_parent_agent_drawer" in chat
    assert "build_parent_agent_drawer" in designer
    assert "build_parent_agent_drawer" in developer
    assert "rebuild_main=lambda **kw: _rebuild_main(**kw)" in app
    assert "agent_child_open" not in chat

    streaming = _read("ui/streaming.py")
    render = _read("ui/render.py")
    assert "model_iterations_used" not in render
    assert "delegation depth" not in render
    assert 'f"{turns_used}/{max_turns} turns"' in render
    assert "def _detach_if_thread_changed" in streaming
    assert '_detach_if_thread_changed(gen, state, "active thread changed")' in streaming


def test_code_highlighting_only_processes_plain_unhighlighted_nodes():
    head = _read("ui/head_html.py")
    render = _read("ui/render.py").replace('\\"', '"')
    streaming = _read("ui/streaming.py").replace('\\"', '"')

    assert 'pre code:not([data-highlighted="yes"])' in head
    assert "el.children.length" in head
    assert 'pre code:not([data-highlighted="yes"])' in render
    assert 'pre code:not([data-highlighted="yes"])' in streaming


def test_shared_goal_ui_is_used_by_all_chat_surfaces():
    goal_ui = _read("ui/goal_ui.py")
    chat = _read("ui/chat.py")
    state = _read("ui/state.py")
    streaming = _read("ui/streaming.py")
    app = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    designer = Path("src/row_bot/designer/editor.py").read_text(encoding="utf-8")
    developer = Path("src/row_bot/developer/ui.py").read_text(encoding="utf-8")

    assert "def build_goal_progress_panel" in goal_ui
    assert "-> str | None" in goal_ui
    assert "def _open_goal_detail_dialog" in goal_ui
    assert "from nicegui import context, ui" in goal_ui
    assert "with context.client.content:" in goal_ui
    assert "Mount outside the Goal strip subtree" in goal_ui
    assert "goals.get_current_goal" in goal_ui
    assert "goals.pause_goal" in goal_ui
    assert "goals.resume_goal" in goal_ui
    assert "goals.complete_goal" in goal_ui
    assert "goals.clear_goal" in goal_ui
    assert "build_continuation_prompt" in goal_ui
    assert "Child-Agent Dependencies" in goal_ui
    assert 'kind="subagent"' in goal_ui
    assert "Pending Approvals" in goal_ui
    assert "Current Goal Events" in goal_ui
    assert "_render_goal_progress_row" not in chat
    assert "build_goal_progress_panel" in chat
    assert "build_goal_progress_panel" in developer
    assert "build_goal_progress_panel" in designer
    assert 'surface="main"' in chat
    assert 'surface="developer"' in developer
    assert 'surface="designer"' in designer
    assert "goal_strip_container" in state
    assert "refresh_goal_strip" in state
    assert "goal_strip_refresh_timer" in state
    assert "row-bot-goal-strip-slot" in chat
    assert "row-bot-goal-strip-slot" in developer
    assert "row-bot-goal-strip-slot" in designer
    assert chat.index("p.chat_scroll =") < chat.index("p.goal_strip_container =")
    assert developer.index("build_chat_messages(") < developer.index("p.goal_strip_container =")
    assert designer.index("build_chat_messages(") < designer.index("p.goal_strip_container =")
    assert chat.index("p.goal_strip_container =") < chat.index("with ui.column().classes(\"w-full shrink-0 gap-0\").style(")
    assert developer.index("p.goal_strip_container =") < developer.index("build_chat_input_bar(")
    assert designer.index("p.goal_strip_container =") < designer.index("build_chat_input_bar(")
    assert "ui.timer(2.0, _refresh_goal_strip)" in chat
    assert "ui.timer(2.0, _refresh_goal_strip)" in developer
    assert "ui.timer(2.0, _refresh_goal_strip)" in designer
    assert "cb.refresh_goal_strip" in app
    assert "_schedule_goal_strip_refresh(cb)" in streaming
    assert "def _schedule_goal_surface_refresh" in streaming
    assert "rebuild(immediate=True, reason=reason)" in streaming
    assert 'reason=f"goal_visible_final_' in streaming
    assert "goals.get_current_goal(gen.thread_id, include_terminal=True)" in streaming
    assert 'raw_tool_name in ("goal_update", "goal_status")' in streaming
    assert '"goal" in str(tool_name).lower()' in streaming
    assert "if state.thread_id == gen.thread_id and not gen.detached:\n        _schedule_goal_strip_refresh(cb)" in streaming
    goal_start_block = streaming.split(
        "if await run.io_bound(lambda: goals.is_goal_start_argument(arg)):",
        1,
    )[1].split("else:", 1)[0]
    assert "goals.start_goal" in goal_start_block
    assert "_schedule_goal_strip_refresh(cb)" in goal_start_block


def test_main_shell_reserves_fixed_drawers_and_activity_center_width():
    app = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    sidebar = _read("ui/sidebar.py")
    command_center = _read("ui/command_center.py")

    assert "--row-bot-left-drawer-width: 280px" in app
    assert "--row-bot-command-center-width: 440px" in app
    assert "row-bot-main-shell" in app
    assert "row-bot-main-card" in app
    assert "width: 100%;" in app
    assert "max-width: 100%;" in app
    assert "--row-bot-main-left-correction" in app
    assert "--row-bot-main-right-correction" in app
    assert "__rowBotDrawerOverlapGuardInstalled" in app
    assert "__rowBotApplyDrawerOverlapGuard" in app
    assert "leftOverlap" in app
    assert "rightOverlap" in app
    assert "margin-left: var(--row-bot-left-drawer-width)" not in app
    assert "margin-right: var(--row-bot-command-center-width)" not in app
    assert '_main_shell_classes = "row-bot-main-shell row-bot-mobile-root" if _mobile_client else "row-bot-main-shell"' in app
    assert '_main_shell = ui.element("div").classes(_main_shell_classes)' in app
    assert "with _main_shell:" in app
    assert "width: var(--row-bot-left-drawer-width, 280px);" in sidebar
    assert 'data-row-bot-left-drawer' in sidebar
    assert "def _sync_shell_width_var" in command_center
    assert "--row-bot-command-center-width" in command_center
    assert "_sync_shell_width_var(width)" in command_center
    assert "__rowBotApplyDrawerOverlapGuard" in command_center


def test_activity_center_uses_responsive_drawer_with_narrow_screen_toggle():
    command_center = _read("ui/command_center.py")

    assert "ui.right_drawer(value=False, fixed=True)" in command_center
    assert "show-if-above breakpoint=900" in command_center
    assert ".row-bot-activity-mobile-toggle.q-btn" in command_center
    assert "@media (max-width: 900px)" in command_center
    assert 'ui.button(icon="pending_actions", on_click=drawer.toggle)' in command_center
    assert "aria-label='Toggle Activity Center'" in command_center


def test_sidebar_uses_responsive_drawer_with_narrow_screen_toggle():
    sidebar = _read("ui/sidebar.py")

    assert "ui.left_drawer(value=False, fixed=True)" in sidebar
    assert "show-if-above breakpoint=900" in sidebar
    assert ".row-bot-navigation-mobile-toggle.q-btn" in sidebar
    assert "@media (max-width: 900px)" in sidebar
    assert 'ui.button(icon="menu", on_click=drawer.toggle)' in sidebar
    assert "aria-label='Toggle navigation'" in sidebar


def test_sidebar_hides_child_agent_threads_by_default():
    src = _read("ui/sidebar.py")

    assert '"agents"' in src
    assert 'thread_type == "agent_child"' in src
    assert "def _is_hidden_agent_child_run" in src
    assert 'agent_run.get("kind") or "") != "subagent"' in src
    assert '{"key": "agents"' not in src
    assert '"label": "Agents"' not in src
    assert "parent_child_counts" not in src
    assert "_SIDEBAR_AGENT_EXPANDED" not in src
    assert 'payload["agent_parent_expanded"]' not in src
    assert 'payload.pop("agent_parent_expanded", None)' in src
    assert "for agent_run in _agent_run_rows" in src
    assert "for run in _agent_run_rows" not in src
    assert "_is_hidden_agent_child_run(agent_run)" in src
    assert '_SIDEBAR_FILTER == "all"' in src
    assert "def _filter_classified_thread_rows" in src
    assert 'item[1] != "agents"' in src
    assert "_filter_classified_thread_rows(classified, _SIDEBAR_FILTER)" in src
    assert "Show child Agent threads" not in src
    assert "valid_modal_keys" in src


def test_agent_card_uses_compact_peek_contract():
    src = _read("ui/render.py")

    assert "open_agent_peek_dialog" in src
    assert "Peek Agent activity" in src
    assert "Open thread" in src
    assert "Open worktree" in src
    assert "Compare" in src
    assert "agent_lifecycle" in src
    assert "-webkit-line-clamp: 2" in src
    assert "Raw Agent tool output" in src
    assert "Open the full child thread from the Agents drawer" not in src
