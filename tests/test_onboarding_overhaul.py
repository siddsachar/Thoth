from __future__ import annotations

import importlib
import json
import uuid
from pathlib import Path


def _case_dir() -> Path:
    path = Path(".tmp") / "pytest-onboarding-overhaul" / f"case-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reload_onboarding(monkeypatch, data_dir: Path):
    monkeypatch.setenv("THOTH_DATA_DIR", str(data_dir))
    import ui.helpers as helpers
    import ui.onboarding_state as onboarding_state

    importlib.reload(helpers)
    return importlib.reload(onboarding_state)


def test_onboarding_state_tracks_profile_steps_and_dismissal(monkeypatch):
    data_dir = _case_dir()
    onboarding = _reload_onboarding(monkeypatch, data_dir)

    onboarding.save_onboarding_profile(["workflows", "designer", "missing", "workflows"])
    onboarding.mark_onboarding_step("models")
    onboarding.mark_onboarding_step("channels", skipped=True)
    onboarding.request_setup_center_on_next_load()
    onboarding.dismiss_onboarding_home_card()

    state = onboarding.get_onboarding_state()
    progress = onboarding.onboarding_progress()

    assert state["version"] == onboarding.ONBOARDING_VERSION
    assert state["profile"] == ["workflows", "designer"]
    assert state["completed_steps"] == ["models"]
    assert state["skipped_steps"] == ["channels"]
    assert state["dismissed_home_card"] is True
    assert onboarding.consume_setup_center_on_next_load() is True
    assert onboarding.consume_setup_center_on_next_load() is False
    assert progress["done"] == 2
    assert progress["total"] == len(onboarding.SETUP_STEPS)
    assert "models" not in progress["remaining_steps"]
    assert "channels" not in progress["remaining_steps"]


def test_onboarding_step_completion_replaces_skip(monkeypatch):
    data_dir = _case_dir()
    onboarding = _reload_onboarding(monkeypatch, data_dir)

    onboarding.mark_onboarding_step("voice", skipped=True)
    onboarding.mark_onboarding_step("voice")

    state = onboarding.get_onboarding_state()

    assert state["completed_steps"] == ["voice"]
    assert state["skipped_steps"] == []


def test_onboarding_state_recovers_from_unknown_saved_values(monkeypatch):
    data_dir = _case_dir()
    cfg = data_dir / "app_config.json"
    cfg.write_text(
        json.dumps(
            {
                "setup_complete": True,
                "onboarding_profile": ["chat", "bogus"],
                "onboarding_completed_steps": ["models", "bogus"],
                "onboarding_skipped_steps": "bad",
            }
        ),
        encoding="utf-8",
    )
    onboarding = _reload_onboarding(monkeypatch, data_dir)

    state = onboarding.get_onboarding_state()

    assert state["setup_complete"] is True
    assert state["profile"] == ["chat"]
    assert state["completed_steps"] == ["models"]
    assert state["skipped_steps"] == []


def test_default_workflow_templates_are_disabled_manual_and_mixed_complexity(monkeypatch):
    data_dir = _case_dir()
    monkeypatch.setenv("THOTH_DATA_DIR", str(data_dir))
    import tasks

    tasks = importlib.reload(tasks)

    assert len(tasks._DEFAULT_TASKS) == 5
    assert sum(1 for t in tasks._DEFAULT_TASKS if t["complexity"] == "simple") == 3
    assert sum(1 for t in tasks._DEFAULT_TASKS if t["complexity"] == "advanced") == 2
    assert all(t.get("schedule") is None for t in tasks._DEFAULT_TASKS)
    assert all(t.get("enabled") is False for t in tasks._DEFAULT_TASKS)
    assert all(t.get("steps") for t in tasks._DEFAULT_TASKS)
    assert all(len(t.get("steps") or []) >= 2 for t in tasks._DEFAULT_TASKS)
    assert all(
        len(t.get("steps") or []) == 2
        for t in tasks._DEFAULT_TASKS
        if t["complexity"] == "simple"
    )
    assert any(
        step.get("type") == "approval"
        for t in tasks._DEFAULT_TASKS
        for step in t.get("steps", [])
    )
    assert any(
        step.get("type") == "condition"
        for t in tasks._DEFAULT_TASKS
        for step in t.get("steps", [])
    )
    assert not any(t.get("notify_only") for t in tasks._DEFAULT_TASKS)
    joined_templates = "\n".join(
        step.get("prompt", "") + step.get("message", "")
        for t in tasks._DEFAULT_TASKS
        for step in t.get("steps", [])
    )
    assert "<topic>" in joined_templates
    assert "<topic-or-decision>" in joined_templates
    assert "<product-or-project>" in joined_templates
    assert "<market-or-customer-segment>" in joined_templates
    assert "topic I provide" not in joined_templates

    tasks.seed_default_tasks()
    seeded = tasks.list_tasks()

    assert len(seeded) == 5
    assert all(t["enabled"] is False for t in seeded)
    assert all(t.get("schedule") in ("", None) for t in seeded)
    assert any(t.get("steps") for t in seeded)


def test_existing_users_are_not_reseeded_automatically(monkeypatch):
    data_dir = _case_dir()
    monkeypatch.setenv("THOTH_DATA_DIR", str(data_dir))
    import tasks

    tasks = importlib.reload(tasks)
    tasks.create_task(name="User workflow", prompts=["Do something"], enabled=False)
    tasks.seed_default_tasks()

    names = [t["name"] for t in tasks.list_tasks()]

    assert names == ["User workflow"]
    assert (data_dir / ".tasks_seeded").exists()


def test_setup_center_orders_steps_by_profile_intent():
    from ui.onboarding_center import _ordered_setup_steps, _priority_steps_for_profile

    ordered = [step for step, _meta in _ordered_setup_steps({"profile": ["designer", "workflows"]})]

    assert ordered[:4] == ["designer", "knowledge", "workflows", "channels"]
    assert _priority_steps_for_profile(["designer", "workflows"]) == [
        "designer",
        "knowledge",
        "workflows",
        "channels",
        "accounts",
    ]
    assert set(ordered) == {
        "models",
        "knowledge",
        "workflows",
        "designer",
        "channels",
        "accounts",
        "tools",
        "extensions",
        "voice",
        "final",
    }


def test_setup_center_only_offers_missing_workflow_starters(monkeypatch):
    data_dir = _case_dir()
    monkeypatch.setenv("THOTH_DATA_DIR", str(data_dir))
    import tasks
    import ui.onboarding_center as center

    tasks = importlib.reload(tasks)
    center = importlib.reload(center)

    assert center._missing_starter_workflow_count() == 5

    tasks.seed_default_tasks()

    assert center._missing_starter_workflow_count() == 0


def test_onboarding_source_contracts_are_wired():
    setup_src = Path("ui/setup_wizard.py").read_text(encoding="utf-8")
    center_src = Path("ui/onboarding_center.py").read_text(encoding="utf-8")
    sidebar_src = Path("ui/sidebar.py").read_text(encoding="utf-8")
    home_src = Path("ui/home.py").read_text(encoding="utf-8")
    installer_src = Path("installer/thoth_setup.iss").read_text(encoding="utf-8")

    for marker in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "XAI_API_KEY",
        "MINIMAX_API_KEY",
        "OPENROUTER_API_KEY",
        "Use ChatGPT / Codex",
        "Custom endpoint",
        "Local (Ollama)",
        "Designer Studio",
        "Starter workflows are added disabled",
        "Open Thoth",
        "Continue setup",
        "Import existing setup?",
    ):
        assert marker in setup_src

    assert "Recommended Setup" not in setup_src
    assert setup_src.index("Connect your first model") < setup_src.index("Import existing setup?")
    assert setup_src.index("Import existing setup?") < setup_src.index("You're ready")
    assert "INTENT_OPTIONS" in setup_src
    assert "save_onboarding_profile" in setup_src
    assert "request_setup_center_on_next_load" in setup_src
    assert "mark_onboarding_step(\"models\")" in setup_src
    assert "codex_runtime_available" in setup_src
    assert "start_codex_device_flow" in setup_src
    assert "poll_codex_device_authorization" in setup_src
    assert "exchange_codex_device_authorization" in setup_src
    assert "save_codex_oauth_tokens" in setup_src
    assert "list_codex_model_infos" in setup_src
    assert "Open OpenAI Login" in setup_src
    assert "Open Settings -> Providers" not in setup_src
    assert "save_external_reference" not in setup_src
    assert "open_setup_center_on_next_load" in Path("app.py").read_text(encoding="utf-8")
    assert "consume_setup_center_on_next_load" in Path("app.py").read_text(encoding="utf-8")
    assert "ui.navigate.reload()" in Path("app.py").read_text(encoding="utf-8")
    assert "open_setup_center_on_next_load" in Path("ui/state.py").read_text(encoding="utf-8")
    assert "show_setup_center" in sidebar_src
    assert "state=state" in sidebar_src
    assert "Recommended from your choices" in center_src
    assert "Recommended" in center_src
    assert 'preferred_home_tab = "Designer"' in center_src
    assert "Finish setting up Thoth" in home_src
    assert "add_default_workflow_templates" in center_src
    assert "_missing_starter_workflow_count" in center_src
    assert "Add starters" not in center_src
    assert "SETUP_STEPS" in center_src
    assert "onboarding_center.py" in installer_src
    assert "onboarding_state.py" in installer_src


def test_welcome_message_and_example_prompts_are_current():
    from ui.constants import EXAMPLE_PROMPTS, welcome_message

    local_msg = welcome_message(cloud=False)
    cloud_msg = welcome_message(cloud=True)

    for marker in (
        "private AI workspace",
        "Knowledge",
        "Workflows",
        "Designer Studio",
        "Browser & tools",
        "Telegram, WhatsApp, Discord, Slack, or SMS",
        "sidebar hello button",
    ):
        assert marker in local_msg
    assert "Everything runs locally" not in local_msg
    assert "23 tools" not in local_msg
    assert "Telegram or Email" not in local_msg
    assert "selected model runs in the cloud" in cloud_msg

    assert len(EXAMPLE_PROMPTS) == 6
    joined = "\n".join(EXAMPLE_PROMPTS)
    for marker in (
        "documents",
        "disabled workflow",
        "Designer Studio",
        "current projects",
        "AI agent trends",
        "calendar",
    ):
        assert marker in joined
