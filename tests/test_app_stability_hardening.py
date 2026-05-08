import importlib
import json
from types import SimpleNamespace
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def data_dir(monkeypatch):
    root = Path(".tmp") / "pytest-app-stability-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"case-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOTH_DATA_DIR", str(path))
    yield path


def test_workflow_drafts_round_trip_and_delete(data_dir, monkeypatch):
    import tasks

    monkeypatch.setattr(tasks, "_DB_PATH", str(data_dir / "tasks.db"))
    monkeypatch.setattr(tasks, "_scheduler", None)
    tasks._init_db()

    tasks.save_workflow_draft(
        None,
        {"name": "New draft", "prompts": ["draft prompt"], "channels": []},
    )
    new_draft = tasks.get_workflow_draft(None)

    assert new_draft is not None
    assert new_draft["mode"] == "new"
    assert new_draft["payload"]["name"] == "New draft"
    assert new_draft["payload"]["channels"] == []

    task_id = tasks.create_task(name="Existing", prompts=["original"])
    tasks.save_workflow_draft(
        task_id,
        {"name": "Edited draft", "prompts": ["changed"], "channels": ["telegram"]},
    )
    edit_draft = tasks.get_workflow_draft(task_id)

    assert edit_draft is not None
    assert edit_draft["mode"] == "edit"
    assert edit_draft["task_id"] == task_id
    assert edit_draft["payload"]["prompts"] == ["changed"]
    assert tasks.get_task(task_id)["name"] == "Existing"

    tasks.delete_workflow_draft(None)
    tasks.delete_workflow_draft(task_id)

    assert tasks.get_workflow_draft(None) is None
    assert tasks.get_workflow_draft(task_id) is None


def test_stability_client_error_writes_local_report(data_dir, monkeypatch):
    import stability

    stability = importlib.reload(stability)
    report_path = stability.record_client_error(
        {
            "message": "synthetic client failure",
            "source": "pytest",
            "details": object(),
        }
    )

    assert report_path is not None
    assert report_path.exists()
    assert report_path.parent == data_dir / "crashes"

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "client_error"
    assert payload["message"] == "synthetic client failure"
    assert payload["extra"]["client"]["source"] == "pytest"
    assert "object object" in payload["extra"]["client"]["details"]


def test_stability_source_contracts_are_wired():
    app_src = Path("app.py").read_text(encoding="utf-8")
    head_src = Path("ui/head_html.py").read_text(encoding="utf-8")
    timer_src = Path("ui/timer_utils.py").read_text(encoding="utf-8")
    settings_src = Path("ui/settings.py").read_text(encoding="utf-8")
    catalog_src = Path("ui/model_catalog.py").read_text(encoding="utf-8")
    dialog_src = Path("ui/task_dialog.py").read_text(encoding="utf-8")
    installer_src = Path("installer/thoth_setup.iss").read_text(encoding="utf-8")

    assert "setup_stability_monitoring()" in app_src
    assert 'app.add_route("/api/client-error"' in app_src
    assert "window.__thothClientErrorReporterInstalled" in head_src
    assert "def safe_ui_task(" in timer_src
    assert "safe_ui_task(_load, context=\"models settings load\")" in settings_src
    assert "_render_provider_summaries" in catalog_src
    assert "Open one provider or search" in catalog_src
    assert "save_workflow_draft" in dialog_src
    assert "Recovered unsaved draft" in dialog_src
    assert "safe_timer(2.0, _autosave_draft)" in dialog_src
    assert "stability.py" in installer_src


def test_provider_qualified_cloud_defaults_validate_after_refresh(monkeypatch):
    import models
    import providers.codex as codex

    with models._cloud_cache_lock:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["gpt-4.1"] = {"provider": "openai"}

    assert models._cloud_model_available_after_refresh("model:openai:gpt-4.1")

    monkeypatch.setattr(
        codex,
        "list_codex_model_infos",
        lambda: [SimpleNamespace(model_id="gpt-5.5")],
    )

    assert models._cloud_model_available_after_refresh("model:codex:gpt-5.5")
    assert not models._cloud_model_available_after_refresh("model:codex:not-present")
