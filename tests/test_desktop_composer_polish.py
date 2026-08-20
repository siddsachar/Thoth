from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_desktop_composer_uses_component_width_responsiveness() -> None:
    controls = _source("src/row_bot/ui/chat_components.py")
    chat = _source("src/row_bot/ui/chat.py")

    assert "container-name: row-bot-composer" in controls
    assert "container-type: inline-size" in controls
    assert "@container row-bot-composer (max-width: 759px)" in controls
    assert "@container row-bot-composer (max-width: 519px)" in controls
    assert "row-bot-desktop-composer" in chat
    assert "row-bot-desktop-composer" in controls
    assert "row-bot-composer-policy-host" in chat
    assert "row-bot-composer-policy-host" in controls


def test_policy_controls_are_three_ordered_icon_and_picker_units() -> None:
    controls = _source("src/row_bot/ui/chat_components.py")
    cluster = controls.split("def build_composer_policy_cluster(", 1)[1].split(
        "def _build_inline_model_picker(", 1
    )[0]

    model = cluster.split("row-bot-composer-control-model", 1)[1].split(
        "row-bot-composer-control-reasoning", 1
    )[0]
    reasoning = cluster.split("row-bot-composer-control-reasoning", 1)[1].split(
        "row-bot-composer-control-approval", 1
    )[0]
    approval = cluster.split("row-bot-composer-control-approval", 1)[1]

    assert 'ui.icon("hub"' in model
    assert "_build_inline_model_picker(" in model
    assert 'ui.icon("psychology"' in reasoning
    assert "row-bot-composer-reasoning-host" in reasoning
    assert "reasoning_control.set_visibility(rendered)" in cluster
    assert 'ui.icon("shield"' in approval
    assert "_build_inline_approval_picker(state)" in approval
    assert cluster.index("row-bot-composer-control-model") < cluster.index(
        "row-bot-composer-control-reasoning"
    ) < cluster.index("row-bot-composer-control-approval")


def test_desktop_policy_controls_keep_state_accessible_when_compact() -> None:
    controls = _source("src/row_bot/ui/chat_components.py")

    assert 'row-bot-composer-model-select"' in controls
    assert 'row-bot-composer-reasoning-select"' in controls
    assert 'row-bot-composer-approval-select"' in controls
    assert '_props["aria-label"] = f"Model: {selected_label}"' in controls
    assert '_props["aria-label"] = f"Thinking: {selection.label}"' in controls
    assert '_props["aria-label"] = f"Approval: {selected_label}"' in controls
    assert ".row-bot-composer-model-select .q-field__control-container" in controls
    assert ".row-bot-composer-reasoning-select .q-field__control-container" in controls
    assert ".row-bot-composer-approval-select .q-field__control-container" in controls


def test_desktop_composer_compacts_secondary_text_and_uses_one_action_slot() -> None:
    controls = _source("src/row_bot/ui/chat_components.py")
    chat = _source("src/row_bot/ui/chat.py")
    extras = _source("src/row_bot/ui/chat_composer_extras.py")

    for source in (chat, controls):
        assert "row-bot-composer-primary-action-slot" in source
        assert "row-bot-composer-status" in source
        assert "row-bot-composer-skills-trigger-compact" in source
        assert "row-bot-composer-skill-count" in source
    assert ":has(" in controls
    assert "row-bot-context-label-full" in controls
    assert "row-bot-context-label-compact" in controls
    assert "row-bot-composer-skills-trigger-full" in extras
    assert "row-bot-composer-skills-trigger-compact" in extras


def test_do_anything_copy_is_desktop_only() -> None:
    app = _source("src/row_bot/app.py")
    chat = _source("src/row_bot/ui/chat.py")
    controls = _source("src/row_bot/ui/chat_components.py")
    mobile = _source("src/row_bot/ui/mobile_chat.py")

    assert 'ui.label("Do anything…")' in app
    assert 'ui.textarea(placeholder="Do anything…")' in chat
    assert 'ui.textarea(placeholder="Do anything…")' in controls
    assert "row-bot-desktop-composer" not in mobile
    assert 'ui.textarea(placeholder="Ask anything...")' in mobile
    assert 'placeholder_text="Ask anything..."' in mobile
