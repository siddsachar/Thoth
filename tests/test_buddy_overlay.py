from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from row_bot.buddy.overlay import (
    ApprovalProjection,
    BuddyPlacement,
    BuddyPlacementState,
    ForegroundAppTracker,
    ForegroundWindow,
    NativeBuddyLifecycle,
    OverlayTurnTarget,
    RuntimeSurface,
    ScreenArea,
    build_thread_snapshot,
    clamp_overlay_position,
    enable_windows_per_monitor_dpi,
    native_overlay_transparency,
    placement_state_from_config,
    placement_state_for_app_startup,
    position_for_drop,
    project_approval,
    screen_areas_from_native,
    should_defer_native_show,
    _WindowsForegroundBackend,
)


class FakeWindow:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def hide(self) -> None:
        self.calls.append(("hide",))

    def show(self) -> None:
        self.calls.append(("show",))

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", x, y))

    def destroy(self) -> None:
        self.calls.append(("destroy",))


def _state(**overrides):
    values = {
        "thread_id": "thread-1",
        "thread_name": "Overlay work",
        "thread_model_override": "openai:gpt-test",
        "thread_approval_mode": "always_ask",
        "active_developer_workspace_id": None,
        "active_designer_project": None,
        "pending_interrupt": None,
        "messages": [{"role": "assistant", "content": "Previous **answer**"}],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_legacy_surface_config_migrates_to_one_placement(monkeypatch, tmp_path):
    import row_bot.buddy.config as config_mod

    config_path = tmp_path / "buddy_config.json"
    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", config_path)
    config_path.write_text(
        '{"desktop_enabled": true, "floating_enabled": true, "overlay": {"width": 260, "height": 260, "x": -410, "y": 55}}',
        encoding="utf-8",
    )

    config = config_mod.get_buddy_config()

    assert config["placement"] == "desktop"
    assert config["visible"] is True
    assert config["overlay"] == {
        "width": 380,
        "height": 230,
        "always_on_top": True,
        "x": -410,
        "y": 55,
    }
    assert "floating_enabled" not in config
    assert "desktop_enabled" not in config


def test_legacy_non_desktop_users_migrate_to_docked_even_if_floating():
    state = placement_state_from_config({"floating_enabled": True, "mode": "floating"})
    assert state == BuddyPlacementState(BuddyPlacement.DOCKED, True, False)


def test_canonical_visibility_keeps_legacy_enabled_mirror_in_sync():
    from row_bot.buddy.config import _normalize_config

    hidden = _normalize_config({"placement": "desktop", "visible": False, "enabled": True})
    shown = _normalize_config({"placement": "desktop", "visible": True, "enabled": False})
    assert hidden["enabled"] is False
    assert shown["enabled"] is True


def test_manual_native_show_never_waits_forever_for_page_ready():
    assert should_defer_native_show(ready=False, manual=False) is True
    assert should_defer_native_show(ready=False, manual=True) is False
    assert should_defer_native_show(ready=True, manual=False) is False


def test_app_startup_returns_buddy_to_dock_without_reviving_hidden_visibility():
    shown = placement_state_for_app_startup(
        {"placement": "desktop", "visible": True, "collapsed": True}
    )
    hidden = placement_state_for_app_startup(
        {"placement": "desktop", "visible": False, "collapsed": True}
    )

    assert shown == BuddyPlacementState(BuddyPlacement.DOCKED, True, False)
    assert hidden == BuddyPlacementState(BuddyPlacement.DOCKED, False, False)


def test_windows_native_overlay_uses_hit_testable_per_monitor_mode():
    contexts: list[int] = []

    assert native_overlay_transparency("win32") is False
    assert native_overlay_transparency("darwin") is True
    assert enable_windows_per_monitor_dpi(
        "win32", set_context=lambda context: contexts.append(context) or True
    ) is True
    assert contexts == [-4]
    assert enable_windows_per_monitor_dpi(
        "darwin", set_context=lambda _context: contexts.append(99) or True
    ) is False
    assert contexts == [-4]


def test_placement_transitions_keep_hidden_and_collapsed_as_conditions():
    desktop = BuddyPlacementState().tear_off().collapse().hide()
    assert desktop == BuddyPlacementState(BuddyPlacement.DESKTOP, False, True)
    assert desktop.show().expand() == BuddyPlacementState(BuddyPlacement.DESKTOP, True, False)
    assert desktop.dock() == BuddyPlacementState(BuddyPlacement.DOCKED, True, False)
    assert BuddyPlacementState().collapse() == BuddyPlacementState()


def test_native_lifecycle_cancels_main_close_only_while_torn_off():
    stored = {"placement": "desktop", "visible": False, "overlay": {}}
    main = FakeWindow()
    buddy = FakeWindow()

    def load():
        return dict(stored)

    def save(value):
        stored.clear()
        stored.update(value)
        return dict(stored)

    lifecycle = NativeBuddyLifecycle(
        load_config=load,
        save_config=save,
        main_window=main,
        buddy_window=lambda: buddy,
    )

    assert lifecycle.main_closing() is False
    assert main.calls == [("hide",)]
    assert lifecycle.dock() is True
    assert buddy.calls == [("destroy",)]
    assert lifecycle.main_closing() is True


def test_native_lifecycle_tear_off_move_hide_show_and_quit():
    stored = {"placement": "docked", "visible": True, "overlay": {}}
    main = FakeWindow()
    buddy = FakeWindow()

    def save(value):
        stored.clear()
        stored.update(value)
        return dict(stored)

    lifecycle = NativeBuddyLifecycle(
        load_config=lambda: dict(stored),
        save_config=save,
        main_window=main,
        buddy_window=lambda: buddy,
    )

    assert lifecycle.tear_off(-320, 120) is True
    assert stored["placement"] == "desktop"
    assert stored["overlay"]["x"] == -320
    assert buddy.calls == [("move", -320, 120), ("show",)]
    assert lifecycle.hide() is True
    assert stored["visible"] is False
    assert lifecycle.show() is True
    assert stored["visible"] is True
    lifecycle.moved(-250, 160)
    assert stored["overlay"] == {"x": -250, "y": 160}
    lifecycle.quit()
    assert buddy.calls[-1] == ("destroy",)
    assert main.calls[-1] == ("destroy",)


def test_positioning_retains_negative_coordinates_and_recovers_missing_monitor():
    screens = [ScreenArea(-1920, 0, 1920, 1080), ScreenArea(0, 0, 1920, 1040)]
    assert clamp_overlay_position(-1800, 80, screens) == (-1800, 80)
    assert position_for_drop(-20, 20, screens) == (-388, 8)
    assert clamp_overlay_position(3000, 1500, [screens[1]]) == (1532, 802)


def test_native_screen_normalization_prefers_windows_work_area():
    frame = SimpleNamespace(X=-1920, Y=40, Width=1920, Height=1000)
    native = SimpleNamespace(x=-1920, y=0, width=1920, height=1080, frame=frame)
    assert screen_areas_from_native([native]) == [ScreenArea(-1920, 40, 1920, 1000)]


@pytest.mark.parametrize(
    ("state", "surface", "extra"),
    [
        (_state(), RuntimeSurface.CHAT, {}),
        (
            _state(active_developer_workspace_id="workspace-7"),
            RuntimeSurface.DEVELOPER,
            {"developer_workspace_id": "workspace-7"},
        ),
        (
            _state(active_designer_project=SimpleNamespace(id="project-4", mode="edit")),
            RuntimeSurface.DESIGNER,
            {"designer_project_id": "project-4", "designer_mode": "edit"},
        ),
    ],
)
def test_overlay_turn_capture_preserves_normal_developer_and_designer_surface(state, surface, extra):
    target = OverlayTurnTarget.capture(state)
    assert target.thread_id == "thread-1"
    assert target.runtime_surface is surface
    assert target.configurable_values() == {
        "runtime_surface": surface.value,
        "model_override": "openai:gpt-test",
        "approval_mode": "always_ask",
        **extra,
    }


def test_turn_capture_keeps_original_message_list_when_selected_thread_changes():
    original_messages = [{"role": "assistant", "content": "Thread one"}]
    state = _state(messages=original_messages)
    target = OverlayTurnTarget.capture(state)
    state.thread_id = "thread-2"
    state.messages = [{"role": "assistant", "content": "Thread two"}]
    assert target.thread_id == "thread-1"
    assert target.messages is original_messages


def test_thread_draft_continuity_between_full_composer_and_overlay(monkeypatch, tmp_path):
    import row_bot.threads as threads

    monkeypatch.setattr(threads, "_THREAD_UI_DIR", tmp_path)
    threads.save_thread_draft("thread-1", "from full composer", source="normal_chat")
    assert threads.load_thread_draft("thread-1")["text"] == "from full composer"
    threads.save_thread_draft("thread-1", "edited in overlay", source="buddy_overlay")
    loaded = threads.load_thread_draft("thread-1")
    assert loaded["text"] == "edited in overlay"
    assert loaded["source"] == "buddy_overlay"


def test_snapshot_projects_only_selected_generation_across_thread_switch():
    old_generation = SimpleNamespace(
        status="streaming",
        accumulated="Old thread text",
        interrupt_data=None,
        error="",
        pending_tools={},
    )
    selected_generation = SimpleNamespace(
        status="streaming",
        accumulated="New thread text",
        interrupt_data=None,
        error="",
        pending_tools={},
    )
    state = _state(thread_id="thread-2", thread_name="New target")

    snapshot = build_thread_snapshot(
        state,
        {"thread-1": old_generation, "thread-2": selected_generation},
    )

    assert snapshot.thread_id == "thread-2"
    assert snapshot.response_text == "New thread text"
    assert snapshot.can_stop is True
    assert "Old thread" not in snapshot.response_text


def test_snapshot_does_not_attribute_old_thread_approval_after_selection_change():
    state = _state(
        thread_id="thread-2",
        pending_interrupt={"description": "Delete old output", "target": "old.txt"},
        pending_interrupt_generation_id="thread-1:generation-1",
    )
    snapshot = build_thread_snapshot(state, {})
    assert snapshot.approval.required is False


def test_snapshot_uses_progress_before_tokens_and_plain_text_answer_afterward():
    generation = SimpleNamespace(
        status="streaming",
        accumulated="",
        interrupt_data=None,
        error="",
        pending_tools={"call": {"label": "Browser search"}},
    )
    state = _state(messages=[])
    progress = build_thread_snapshot(state, {"thread-1": generation}, buddy_status="Working")
    assert progress.progress_text == "Working with Browser search"
    generation.accumulated = "## Result\n**Safe** [link](https://example.test) <script>ignored</script>"
    answer = build_thread_snapshot(state, {"thread-1": generation})
    assert answer.progress_text == ""
    assert answer.response_text == "Result\nSafe link ignored"


@pytest.mark.parametrize("message", ["Provider connection failed", "Desktop client disconnected"])
def test_snapshot_projects_runtime_errors_without_starting_another_turn(message):
    generation = SimpleNamespace(
        status="error",
        accumulated="",
        interrupt_data=None,
        error=message,
        pending_tools={},
    )
    snapshot = build_thread_snapshot(_state(messages=[]), {"thread-1": generation})
    assert snapshot.error == message
    assert snapshot.generating is False
    assert snapshot.can_stop is False


def test_well_described_approvals_can_be_settled_in_overlay_but_vague_requires_full_ui():
    simple = project_approval(
        {"description": "Write the reviewed file", "target": "notes.md", "reversible": True}
    )
    assert simple == ApprovalProjection(True, True, "Write the reviewed file", "notes.md", 1)
    grouped = project_approval(
        [
            {"description": "Focus Edge", "action": "focus"},
            {"description": "Select the AI topic", "target": "AI"},
        ]
    )
    assert grouped.required is True
    assert grouped.simple is True
    assert grouped.count == 2
    assert "Focus Edge" in grouped.description
    assert "Select the AI topic" in grouped.description
    assert grouped.reason == "Approve all or open details"
    vague = project_approval({"tool": "shell"})
    assert vague.simple is False


def test_grouped_approval_with_an_undescribed_action_still_requires_full_ui():
    projection = project_approval(
        [
            {"description": "Focus Edge", "action": "focus"},
            {"tool": "browser_click"},
        ]
    )
    assert projection == ApprovalProjection(
        True,
        False,
        "Approval required",
        "Review in Row-Bot",
        2,
    )


@dataclass
class FakeForegroundBackend:
    window: ForegroundWindow | None
    activated: list[ForegroundWindow]

    def current(self):
        return self.window

    def activate(self, window):
        self.activated.append(window)
        return True


class FakeWindowsUser32:
    def __init__(self, *, valid: bool = True, minimized: bool = False, activated: bool = True) -> None:
        self.valid = valid
        self.minimized = minimized
        self.activated = activated
        self.calls: list[tuple] = []

    def IsWindow(self, handle):  # noqa: N802
        self.calls.append(("IsWindow", handle))
        return self.valid

    def IsIconic(self, handle):  # noqa: N802
        self.calls.append(("IsIconic", handle))
        return self.minimized

    def ShowWindow(self, handle, command):  # noqa: N802
        self.calls.append(("ShowWindow", handle, command))
        return True

    def SetForegroundWindow(self, handle):  # noqa: N802
        self.calls.append(("SetForegroundWindow", handle))
        return self.activated


def _windows_foreground_backend(user32: FakeWindowsUser32) -> _WindowsForegroundBackend:
    backend = _WindowsForegroundBackend.__new__(_WindowsForegroundBackend)
    backend.user32 = user32
    return backend


def test_windows_foreground_rejects_invalid_handle_without_restore_or_activation():
    user32 = FakeWindowsUser32(valid=False)
    backend = _windows_foreground_backend(user32)

    assert backend.activate(ForegroundWindow(44, 700)) is False
    assert user32.calls == [("IsWindow", 44)]


@pytest.mark.parametrize("placement", ["normal", "maximized"])
def test_windows_foreground_visible_window_is_activated_without_placement_change(placement):
    user32 = FakeWindowsUser32(minimized=False)
    backend = _windows_foreground_backend(user32)

    assert backend.activate(ForegroundWindow(44, 700, placement, "Edge")) is True
    assert user32.calls == [
        ("IsWindow", 44),
        ("IsIconic", 44),
        ("SetForegroundWindow", 44),
    ]


def test_windows_foreground_minimized_window_restores_once_before_activation():
    user32 = FakeWindowsUser32(minimized=True)
    backend = _windows_foreground_backend(user32)

    assert backend.activate(ForegroundWindow(44, 700, "Edge", "Edge")) is True
    assert user32.calls == [
        ("IsWindow", 44),
        ("IsIconic", 44),
        ("ShowWindow", 44, 9),
        ("SetForegroundWindow", 44),
    ]


def test_windows_foreground_failed_activation_is_not_retried():
    user32 = FakeWindowsUser32(activated=False)
    backend = _windows_foreground_backend(user32)

    assert backend.activate(ForegroundWindow(44, 700)) is False
    assert [name for name, *_args in user32.calls].count("SetForegroundWindow") == 1


def test_foreground_tracker_filters_row_bot_and_restores_external_app_once_per_send():
    backend = FakeForegroundBackend(ForegroundWindow(11, 700, "Editor", "Code"), [])
    tracker = ForegroundAppTracker(backend, own_process_id=900, ignored_handles=lambda: {22})
    assert tracker.observe().handle == 11
    assert tracker.app_name == "Code"
    backend.window = ForegroundWindow(22, 901, "Buddy", "Row-Bot")
    assert tracker.observe().handle == 11
    assert tracker.restore_once() is True
    assert backend.activated == [ForegroundWindow(11, 700, "Editor", "Code")]


def test_foreground_tracker_does_not_reject_external_apps_by_title_alone():
    external = ForegroundWindow(33, 700, "Buddy notes", "Notes")
    backend = FakeForegroundBackend(external, [])
    tracker = ForegroundAppTracker(backend, own_process_id=900)
    assert tracker.observe() == external


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (OverlayTurnTarget("thread-1", "Chat", RuntimeSurface.CHAT), {"runtime_surface": "normal_chat"}),
        (
            OverlayTurnTarget(
                "thread-1",
                "Developer",
                RuntimeSurface.DEVELOPER,
                developer_workspace_id="workspace-1",
            ),
            {"runtime_surface": "developer", "developer_workspace_id": "workspace-1"},
        ),
        (
            OverlayTurnTarget(
                "thread-1",
                "Designer",
                RuntimeSurface.DESIGNER,
                designer_project_id="project-1",
                designer_mode="edit",
            ),
            {
                "runtime_surface": "designer",
                "designer_project_id": "project-1",
                "designer_mode": "edit",
            },
        ),
    ],
)
def test_overlay_send_uses_captured_surface_and_never_adds_implicit_images(monkeypatch, target, expected):
    import row_bot.developer.agent_context as developer_context
    import row_bot.developer.profile as developer_profile
    import row_bot.agent as agent
    import row_bot.threads as threads
    import row_bot.tools.registry as tool_registry
    import row_bot.ui.helpers as helpers
    import row_bot.ui.streaming as streaming
    from row_bot.ui.state import _active_generations

    async def ready(*_args, **_kwargs):
        return True

    async def consume_noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent, "stream_agent", lambda *_args, **_kwargs: iter(()))
    monkeypatch.setattr(streaming, "_context_capacity_ready_for_send", ready)
    monkeypatch.setattr(streaming, "_agent_ready_forced_surface", ready)
    monkeypatch.setattr(streaming, "_subscription_auth_block_message", lambda *_args: None)
    monkeypatch.setattr(streaming, "_profile_runtime_config_for_thread", lambda *_args: {})
    monkeypatch.setattr(streaming, "_child_agent_run_ids_for_thread", lambda *_args: set())
    monkeypatch.setattr(streaming, "_build_assistant_placeholder", lambda *_args: None)
    monkeypatch.setattr(streaming, "consume_generation", consume_noop)
    monkeypatch.setattr(threads, "should_auto_rename_thread", lambda *_args: False)
    monkeypatch.setattr(threads, "touch_thread", lambda *_args: None)
    monkeypatch.setattr(threads, "_get_thread_approval_mode", lambda *_args: "always_ask")
    monkeypatch.setattr(threads, "_get_thread_project_workspace", lambda *_args: "")
    monkeypatch.setattr(helpers, "persist_thread_media_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_registry, "get_enabled_tools", lambda: [])
    monkeypatch.setattr(developer_context, "build_developer_agent_context", lambda *_args: "context")
    monkeypatch.setattr(developer_profile, "effective_tool_names", lambda names: names)

    state = _state(
        messages=[],
        active_developer_workspace_id=None,
        active_designer_project=None,
        attached_data_cache={},
        vision_service=None,
        voice_coordinator=SimpleNamespace(transport=""),
        tts_service=SimpleNamespace(enabled=False),
        voice_enabled=False,
        voice_input_mode="talk",
    )
    state.cache_active_messages = lambda: None
    p = SimpleNamespace(
        pending_files=[],
        file_chips_row=None,
        chat_header_label=None,
        stop_btn=None,
        chat_container=None,
        chat_scroll=None,
    )
    cb = SimpleNamespace(
        rebuild_main=lambda *args, **kwargs: None,
        rebuild_thread_list=lambda: None,
        add_chat_message=lambda *_args: None,
    )

    try:
        asyncio.run(streaming.send_message("raw input", state=state, p=p, cb=cb, turn_target=target))
        generation = _active_generations["thread-1"]
        configurable = generation.config["configurable"]
        for key, value in expected.items():
            assert configurable[key] == value
        assert generation.captured_images == []
        assert state.messages == [{"role": "user", "content": "raw input"}]
    finally:
        _active_generations.pop("thread-1", None)


def test_no_thread_raw_slash_command_creates_normal_thread_and_forwards_text(monkeypatch):
    import row_bot.slash_commands as slash_commands
    import row_bot.threads as threads
    import row_bot.tools.registry as tool_registry
    import row_bot.ui.helpers as helpers
    import row_bot.ui.streaming as streaming

    created: list[tuple[str, str, str]] = []
    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        threads,
        "create_thread",
        lambda name, *, thread_id, approval_mode, **_kwargs: created.append((name, thread_id, approval_mode)),
    )
    monkeypatch.setattr(threads, "touch_thread", lambda *_args: None)
    monkeypatch.setattr(helpers, "persist_thread_media_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_registry, "get_enabled_tools", lambda: [])
    monkeypatch.setattr(
        slash_commands,
        "resolve_command_text",
        lambda text, **_kwargs: (SimpleNamespace(id="status"), "") if text == "/status raw" else None,
    )
    monkeypatch.setattr(
        slash_commands,
        "dispatch_text_command",
        lambda thread_id, text, **_kwargs: dispatched.append((thread_id, text)) or "Status ready",
    )

    state = _state(
        thread_id=None,
        thread_name=None,
        messages=[],
        active_developer_workspace_id="stale-workspace",
        active_designer_project=SimpleNamespace(id="stale-project", mode="edit"),
    )
    state.show_onboarding = True
    state.cache_active_messages = lambda: None
    p = SimpleNamespace(pending_files=[])
    cb = SimpleNamespace(
        rebuild_main=lambda *args, **kwargs: None,
        rebuild_thread_list=lambda: None,
        add_chat_message=lambda *_args: None,
    )

    asyncio.run(streaming.send_message("/status raw", state=state, p=p, cb=cb))

    assert len(created) == 1
    assert created[0][1] == state.thread_id
    assert dispatched == [(state.thread_id, "/status raw")]
    assert state.messages == [
        {"role": "user", "content": "/status raw"},
        {"role": "assistant", "content": "Status ready"},
    ]
