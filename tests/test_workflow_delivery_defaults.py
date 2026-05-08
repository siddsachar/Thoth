import json
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def data_dir(monkeypatch):
    root = Path(".tmp") / "pytest-workflow-delivery-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"case-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOTH_DATA_DIR", str(path))
    yield path


class _FakeChannel:
    def __init__(self, name: str, display_name: str, running: bool = True):
        self.name = name
        self.display_name = display_name
        self._running = running

    def is_running(self) -> bool:
        return self._running


def test_workflow_default_channels_default_to_web_app_only(data_dir, monkeypatch):
    import tasks

    cfg_path = data_dir / "task_config.json"
    monkeypatch.setattr(tasks, "_TASK_CONFIG_PATH", str(cfg_path))

    assert tasks.get_workflow_default_channels() == []

    tasks.set_workflow_default_channels(["telegram", "slack", "telegram", "", 3])

    assert tasks.get_workflow_default_channels() == ["telegram", "slack"]
    assert json.loads(cfg_path.read_text())["workflow_default_channels"] == [
        "telegram",
        "slack",
    ]


def test_get_task_channels_inherits_defaults_and_respects_overrides(
    data_dir,
    monkeypatch,
):
    import tasks
    from channels import registry as channel_registry

    cfg_path = data_dir / "task_config.json"
    monkeypatch.setattr(tasks, "_TASK_CONFIG_PATH", str(cfg_path))
    tasks.set_workflow_default_channels(["telegram", "slack", "discord"])

    running = [
        _FakeChannel("telegram", "Telegram"),
        _FakeChannel("slack", "Slack"),
        _FakeChannel("discord", "Discord", running=False),
    ]
    monkeypatch.setattr(channel_registry, "running_channels", lambda: running[:2])

    inherited = tasks.get_task_channels({"channels": None})
    assert [ch.name for ch in inherited] == ["telegram", "slack"]

    overridden = tasks.get_task_channels({"channels": ["slack"]})
    assert [ch.name for ch in overridden] == ["slack"]

    web_app_only = tasks.get_task_channels({"channels": []})
    assert web_app_only == []


def test_legacy_delivery_channel_is_treated_as_specific_override(
    data_dir,
    monkeypatch,
):
    import tasks
    from channels import registry as channel_registry

    cfg_path = data_dir / "task_config.json"
    monkeypatch.setattr(tasks, "_TASK_CONFIG_PATH", str(cfg_path))
    tasks.set_workflow_default_channels(["slack"])

    running = [
        _FakeChannel("telegram", "Telegram"),
        _FakeChannel("slack", "Slack"),
    ]
    monkeypatch.setattr(channel_registry, "running_channels", lambda: running)

    channels = tasks.get_task_channels({
        "channels": None,
        "delivery_channel": "telegram",
    })

    assert [ch.name for ch in channels] == ["telegram"]


def test_create_task_preserves_empty_channel_override(data_dir, monkeypatch):
    import tasks

    monkeypatch.setattr(tasks, "_DB_PATH", str(data_dir / "tasks.db"))
    monkeypatch.setattr(tasks, "_scheduler", None)
    tasks._init_db()

    task_id = tasks.create_task(name="Web only", prompts=["Say hi"], channels=[])

    assert tasks.get_task(task_id)["channels"] == []


def test_workflow_delivery_ui_source_contracts():
    home_src = open("ui/home.py", encoding="utf-8").read()
    dialog_src = open("ui/task_dialog.py", encoding="utf-8").read()

    assert "Delivery defaults" in home_src
    assert "ui.menu()" in home_src
    assert "ui.checkbox" in home_src
    assert "Web app always on" in home_src
    assert "Web app always receives run status" in home_src
    assert "multiple=True" not in home_src

    assert "Use workflow default" in dialog_src
    assert "Custom channels" in dialog_src
    assert "Web app only" in dialog_src
    assert "All channels" not in dialog_src
