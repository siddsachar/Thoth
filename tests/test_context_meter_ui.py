from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_desktop_meter_is_single_event_driven_interactive_component() -> None:
    components = _source("src/row_bot/ui/chat_components.py")
    chat = _source("src/row_bot/ui/chat.py")
    app = _source("src/row_bot/app.py")

    assert components.count("def create_context_meter(") == 1
    assert "create_context_meter(p, state)" in components
    assert "create_context_meter(p, state)" in chat
    assert 'tabindex="0" role="status"' in components
    assert "pointer-events: none" not in components[components.index("def create_context_meter"):]
    assert "Approximately " in components
    assert " of " in components and " usable" in components
    assert "Compacts around " in components
    assert "Model window: " in components
    assert "safe_timer(5.0, _update_token_counter)" not in app
    assert "_schedule_token_counter_async" not in app
    assert "get_token_usage" not in app


def test_mobile_has_event_row_but_never_renders_context_meter() -> None:
    mobile = _source("src/row_bot/ui/mobile_chat.py")
    renderer = _source("src/row_bot/ui/render.py")

    assert "create_context_meter" not in mobile
    assert 'msg.get("role") == "context_event"' in renderer
    context_branch = renderer[renderer.index('msg.get("role") == "context_event"'):]
    assert "ui.expansion" not in context_branch.split("elif role", 1)[0]
    assert "row-bot-context-event" in context_branch


def test_streaming_updates_meter_and_inserts_durable_event_once() -> None:
    streaming = _source("src/row_bot/ui/streaming.py")

    for event_type in (
        "context_usage",
        "compaction_started",
        "compaction_succeeded",
        "compaction_failed",
    ):
        assert event_type in streaming
    assert "cache_and_project_context_usage(" in streaming
    assert "detached=gen.detached" in streaming
    assert "event_id not in gen.context_event_ids" in streaming
    assert '"role": "context_event"' in streaming


def test_meter_controller_clears_stale_snapshot_and_holds_compacting_value() -> None:
    from row_bot.ui.chat_components import ContextMeterController

    class FakeElement:
        def __init__(self) -> None:
            self.text = ""
            self.value = 0.0
            self.styles = []
            self.properties = []

        def style(self, value):
            self.styles.append(value)
            return self

        def props(self, value):
            self.properties.append(value)
            return self

        def update(self):
            return None

    root = FakeElement()
    label = FakeElement()
    progress = FakeElement()
    marker = FakeElement()
    tooltip = FakeElement()
    controller = ContextMeterController(root, label, progress, marker, tooltip)
    usage = {
        "status": "ready",
        "estimated_input_tokens": 42_000,
        "usable_input_tokens": 109_000,
        "compact_at_tokens": 96_000,
        "effective_limit_tokens": 128_000,
    }

    controller.update(usage)
    ready_value = progress.value
    controller.update({**usage, "status": "compacting"})
    assert label.text == "Compacting context..."
    assert progress.value == ready_value
    controller.update(None)
    assert label.text == "Context unavailable"
    assert progress.value == 0.0


def test_meter_distinguishes_no_snapshot_unknown_auto_and_unknown_override() -> None:
    from row_bot.ui.chat_components import ContextMeterController

    class FakeElement:
        def __init__(self) -> None:
            self.text = ""
            self.value = 0.0

        def style(self, _value):
            return self

        def props(self, _value):
            return self

        def update(self):
            return None

    elements = [FakeElement() for _ in range(5)]
    controller = ContextMeterController(*elements)
    _root, label, _progress, _marker, tooltip = elements

    controller.update(None)
    assert tooltip.text == "Context will appear after the next validated model preparation."

    controller.update(None, capacity_state="unknown_auto")
    assert label.text == "Context unavailable"
    assert "Refresh the provider catalog" in tooltip.text
    assert "Advanced override" in tooltip.text
    assert "max 0" not in tooltip.text.lower()

    controller.update(
        {
            "status": "ready",
            "estimated_input_tokens": 10_000,
            "usable_input_tokens": 222_822,
            "compact_at_tokens": 196_608,
            "native_window_tokens": None,
            "effective_limit_tokens": 262_144,
            "capacity_source": "advanced_override",
        }
    )
    assert label.text.startswith("Context ")
    assert "Override limit: 262K" in tooltip.text
    assert "Native model window: unknown" in tooltip.text
    assert "Model window: 262K" not in tooltip.text


def test_context_policy_presentation_and_notice_deduplication(monkeypatch):
    from types import SimpleNamespace

    from row_bot.ui import chat_components

    state = SimpleNamespace(thread_id="thread-1", context_policy_notice_keys={})
    unknown_auto = SimpleNamespace(
        model_ref="model:openai:opaque",
        provider_id="openai",
        runtime_model="opaque",
        policy_kind="provider",
        native_limit_tokens=None,
        requested_limit_tokens=None,
        effective_limit_tokens=None,
        capacity_source="unknown",
    )
    unknown_override = SimpleNamespace(
        **{
            **unknown_auto.__dict__,
            "requested_limit_tokens": 262_144,
            "effective_limit_tokens": 262_144,
            "capacity_source": "advanced_override",
        }
    )
    notices = []
    monkeypatch.setattr(chat_components.ui, "notify", lambda message, **kwargs: notices.append((message, kwargs)))

    auto_view = chat_components.context_policy_presentation(unknown_auto)
    override_view = chat_components.context_policy_presentation(unknown_override)

    assert auto_view["settings_note"] == "Native limit unknown · no override set"
    assert auto_view["mobile_note"] == "Context setup required · native limit unknown · no override set"
    assert "Refresh the provider catalog" in auto_view["notification"]
    assert "Advanced override" in auto_view["notification"]
    assert "choose another model" in auto_view["notification"]
    assert override_view["settings_note"] == "Native limit unknown · using 262K override"
    assert override_view["notification"] == (
        "Native context is unknown; Row-Bot will use your 262K Advanced override."
    )

    assert chat_components.notify_context_policy_once(state, unknown_auto) is True
    assert chat_components.notify_context_policy_once(state, unknown_auto) is False
    assert chat_components.notify_context_policy_once(state, unknown_override) is True
    assert len(notices) == 2


def test_context_snapshot_projection_requires_active_thread_and_provider_model_identity():
    from types import SimpleNamespace

    from row_bot.ui.state import (
        cache_and_project_context_usage,
        clear_context_usage_projection,
    )

    state = SimpleNamespace(
        thread_id="thread-active",
        thread_model_override="model:openai:shared",
        current_model="model:openai:default",
        context_usage={"model_ref": "model:openai:shared", "estimated_input_tokens": 10},
        context_usage_thread_id="thread-active",
        context_usage_model_ref="model:openai:shared",
        context_usage_cache={},
    )
    inactive = {"model_ref": "model:openai:shared", "estimated_input_tokens": 99}
    wrong_provider = {"model_ref": "model:anthropic:shared", "estimated_input_tokens": 88}
    active = {"model_ref": "model:openai:shared", "estimated_input_tokens": 42}

    assert cache_and_project_context_usage(state, "thread-other", inactive) is False
    assert state.context_usage["estimated_input_tokens"] == 10
    assert state.context_usage_cache["thread-other"] == inactive
    assert cache_and_project_context_usage(state, "thread-active", wrong_provider) is False
    assert state.context_usage["estimated_input_tokens"] == 10
    assert cache_and_project_context_usage(state, "thread-active", active, detached=True) is False
    assert state.context_usage["estimated_input_tokens"] == 10
    assert cache_and_project_context_usage(state, "thread-active", active) is True
    assert state.context_usage["estimated_input_tokens"] == 42

    clear_context_usage_projection(state)
    assert state.context_usage is None
    assert state.context_usage_thread_id == ""
    assert state.context_usage_model_ref == ""


def test_global_context_snapshot_clear_drops_cache_and_visible_identity():
    from types import SimpleNamespace

    from row_bot.ui.state import clear_all_context_usage_state

    state = SimpleNamespace(
        context_usage={"model_ref": "model:openai:test"},
        context_usage_thread_id="thread-1",
        context_usage_model_ref="model:openai:test",
        context_usage_cache={"thread-1": {"model_ref": "model:openai:test"}},
    )

    clear_all_context_usage_state(state)

    assert state.context_usage is None
    assert state.context_usage_thread_id == ""
    assert state.context_usage_model_ref == ""
    assert state.context_usage_cache == {}
