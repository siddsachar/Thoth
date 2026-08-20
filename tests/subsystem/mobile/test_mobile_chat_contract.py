from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_mobile_chat_has_list_detail_and_back_contract() -> None:
    src = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")

    assert "def build_mobile_thread_list(" in src
    assert "def build_mobile_thread_detail(" in src
    assert "data-docs-id=mobile-chat-detail" in src
    assert "arrow_back" in src
    assert "mobile_chat_back" in src
    assert "build_chat_input_bar" not in src


def test_mobile_thread_list_creates_threads_before_composing() -> None:
    src = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")
    list_section = src.split("def build_mobile_thread_list(", 1)[1].split("def build_mobile_thread_detail(", 1)[0]

    assert "New thread" in list_section
    assert "create_mobile_thread(" in list_section
    assert "_build_mobile_thread_composer(" not in list_section
    assert "send_message" not in list_section
    assert "Send a message above" not in src
    assert "create_thread(name, thread_id=tid" in src


def test_mobile_thread_list_excludes_child_agent_threads_before_limit() -> None:
    from row_bot.ui.mobile_chat import _mobile_conversation_threads

    rows = [
        ("parent-new", "Parent new", "", "2026-08-06T10:00:00", "", "", "chat"),
        ("child", "Child agent", "", "2026-08-06T09:59:00", "", "", "agent_child"),
        ("parent-old", "Parent old", "", "2026-08-06T09:58:00", "", "", ""),
    ]

    visible = _mobile_conversation_threads(rows, limit=2)

    assert [row[0] for row in visible] == ["parent-new", "parent-old"]


def test_mobile_active_thread_composer_uses_shared_chat_controls() -> None:
    src = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")

    assert "data-docs-id=mobile-chat-composer" in src
    assert "attach_file" in src
    assert "mobile-chat-controls" in src
    assert "data-docs-id=mobile-reasoning-control" in src
    assert "set_thread_chat_controls(" in src
    assert "send_message(text)" in src
    assert "create_chat_composer_extras(" in src
    assert "show_draft_suggestions=False" in src
    assert "composer_extras.attach_input(text_input)" in src
    assert "composer_extras.open_skill_picker()" in src
    assert "compact_skill_chips=True" in src
    assert "_set_thread_model_override" in src
    assert "_set_thread_approval_mode" in src
    assert "_set_thread_agent_profile" in src
    assert "_clear_thread_agent_profile" in src
    assert "owner_controls" not in src
    assert "has_capability" not in src
    assert "Companion sessions" not in src


def test_mobile_reasoning_command_can_open_and_focus_controls_sheet() -> None:
    shared_src = Path("src/row_bot/ui/chat_components.py").read_text(encoding="utf-8")

    assert 'data-docs-id="mobile-chat-composer"' in shared_src
    assert ".row-bot-mobile-model-pill" in shared_src
    assert 'data-docs-id="mobile-reasoning-control"' in shared_src
    assert "controlsTrigger.click()" in shared_src
    assert "for (let attempt = 0; attempt < 10; attempt += 1)" in shared_src
    assert "if (openPicker(mobile)) return true" in shared_src


def test_mobile_submit_preserves_nicegui_client_context() -> None:
    src = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")

    assert "async def _submit() -> None:" in src
    assert "await _submit_async()" in src
    assert "asyncio.create_task(_submit_async())" not in src


def test_mobile_active_thread_removes_space_heavy_policy_chrome() -> None:
    src = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")
    detail_section = src.split("def build_mobile_thread_detail(", 1)[1].split("def build_mobile_chat(", 1)[0]
    composer = src.split("def _build_mobile_thread_composer(", 1)[1].split("def build_mobile_thread_list(", 1)[0]

    assert "row-bot-mobile-policy-banner" not in detail_section
    assert "Cloud model: messages may be sent to the provider." not in detail_section
    assert "_current_model_label(state)} - {_current_agent_profile_label(state)}" not in detail_section
    assert "ui.badge(" not in composer
    assert "row-bot-mobile-policy-badge" not in composer
    assert "row-bot-mobile-policy-chip" in src
    assert "_mobile_generation_active(state)" in src


def test_mobile_chat_controls_expose_context_setup_without_a_meter() -> None:
    src = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")

    assert "create_context_meter" not in src
    assert "context_policy_presentation" in src
    assert "notify_context_policy_once" in src
    assert 'open_settings("Models")' in src
    assert "clear_context_usage_projection" in src


def test_mobile_policy_summary_distinguishes_app_fallback_and_override(monkeypatch) -> None:
    from types import SimpleNamespace

    import row_bot.models as models
    from row_bot.ui.mobile_chat import _model_policy_summary

    state = SimpleNamespace(
        thread_model_override="model:openai:opaque",
        current_model="model:openai:default",
    )
    policy = SimpleNamespace(
        model_ref="model:openai:opaque",
        provider_id="openai",
        runtime_model="opaque",
        policy_kind="provider",
        native_limit_tokens=None,
        requested_limit_tokens=None,
        effective_limit_tokens=131_072,
        capacity_source="app_fallback",
    )
    monkeypatch.setattr(models, "get_context_policy", lambda model_ref: policy)
    monkeypatch.setattr(models, "is_cloud_model", lambda model_ref: True)

    assert _model_policy_summary(state) == (
        "warning",
        "128K context fallback",
        "Native limit unknown · Row-Bot 128K fallback active",
        True,
    )

    policy.requested_limit_tokens = 262_144
    policy.effective_limit_tokens = 262_144
    policy.capacity_source = "advanced_override"

    assert _model_policy_summary(state) == (
        "info",
        "Context override",
        "Native limit unknown · using 262K override",
        False,
    )


def test_mobile_composer_keeps_stop_button_visible_and_disabled_when_idle() -> None:
    src = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")
    composer = src.split("def _build_mobile_thread_composer(", 1)[1].split("def build_mobile_thread_list(", 1)[0]

    assert 'p.stop_btn = ui.button(icon="stop"' in composer
    assert 'on_click=lambda: _stop_generation(state, p)' in composer
    assert "p.stop_btn.disable()" in composer
    assert 'if _mobile_generation_active(state):\n                ui.button(icon="stop"' not in composer


def test_mobile_composer_places_skills_in_bottom_action_row_before_stop() -> None:
    src = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")
    composer = src.split("def _build_mobile_thread_composer(", 1)[1].split("def build_mobile_thread_list(", 1)[0]

    assert "composer_extras.render_before_input(" in composer
    assert "render_skill_chips=False" in composer
    assert "composer_extras.render_before_input()\n" not in composer
    assert "row-bot-mobile-action-row" in composer
    assert "row-bot-mobile-skill-chip-slot" in composer
    assert "composer_extras.render_skill_chips(" in composer
    assert (
        composer.index("row-bot-mobile-skill-chip-slot")
        < composer.index('p.stop_btn = ui.button(icon="stop"')
        < composer.index('ui.button(icon="send"')
    )


def test_mobile_composer_reserves_send_button_inside_narrow_viewports() -> None:
    chat_src = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")
    mobile_src = Path("src/row_bot/ui/mobile.py").read_text(encoding="utf-8")

    assert "row-bot-mobile-send-button" in chat_src
    assert ".row-bot-mobile-action-row > .row-bot-mobile-model-pill" in mobile_src
    assert "flex: 1 1 0;" in mobile_src
    assert ".row-bot-mobile-send-button" in mobile_src
    assert "max(10px, env(safe-area-inset-right))" in mobile_src


def test_mobile_chat_mode_defaults_to_thread_list_without_active_thread() -> None:
    from row_bot.ui.mobile_chat import mobile_chat_mode

    state = SimpleNamespace(thread_id=None)

    assert mobile_chat_mode(state) == "threads"
    assert state.mobile_chat_mode == "threads"


def test_create_mobile_thread_sets_active_detail_state(monkeypatch) -> None:
    from row_bot.ui import mobile_chat

    calls: list[tuple] = []
    state = SimpleNamespace(
        thread_id="previous",
        thread_approval_mode="block",
        active_designer_project="project",
        active_developer_workspace_id="workspace",
        messages=[{"role": "user", "content": "old"}],
        tts_service=SimpleNamespace(stop=lambda: None),
    )
    p = SimpleNamespace(pending_files=[{"name": "x"}])

    monkeypatch.setattr(mobile_chat.uuid, "uuid4", lambda: SimpleNamespace(hex="abc123def4567890"))
    monkeypatch.setattr(mobile_chat, "create_thread", lambda name, **kwargs: calls.append(("create", name, kwargs)))
    monkeypatch.setattr(mobile_chat, "set_active_thread", lambda tid, previous_id=None: calls.append(("active", tid, previous_id)))
    monkeypatch.setattr(mobile_chat, "stop_voice_for_thread_change", lambda *args, **kwargs: calls.append(("voice", kwargs)))

    def load_messages(thread_id: str) -> list[dict]:
        calls.append(("load", thread_id))
        return []

    def rebuild_main(**kwargs) -> None:
        calls.append(("rebuild", kwargs))

    thread_id = mobile_chat.create_mobile_thread(
        state=state,
        p=p,
        load_thread_messages=load_messages,
        rebuild_main=rebuild_main,
    )

    assert thread_id == "abc123def456"
    assert state.thread_id == "abc123def456"
    assert state.mobile_chat_mode == "thread"
    assert state.mobile_view == "Chat"
    assert state.messages == []
    assert p.pending_files == []
    assert calls[0][0] == "voice"
    assert calls[1][0] == "create"
    assert calls[1][1].startswith("\U0001f4f1 Thread ")
    assert state.thread_name == calls[1][1]
    assert calls[-1] == ("rebuild", {"immediate": True, "reason": "mobile_new_thread"})
