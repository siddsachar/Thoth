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
    agent_src = Path("agent.py").read_text(encoding="utf-8")
    head_src = Path("ui/head_html.py").read_text(encoding="utf-8")
    timer_src = Path("ui/timer_utils.py").read_text(encoding="utf-8")
    defer_src = timer_src.split("def defer_ui", 1)[1].split("def safe_ui_task", 1)[0]
    settings_src = Path("ui/settings.py").read_text(encoding="utf-8")
    catalog_src = Path("ui/model_catalog.py").read_text(encoding="utf-8")
    dialog_src = Path("ui/task_dialog.py").read_text(encoding="utf-8")
    graph_src = Path("ui/graph_panel.py").read_text(encoding="utf-8")
    discord_src = Path("channels/discord_channel.py").read_text(encoding="utf-8")
    installer_src = Path("installer/thoth_setup.iss").read_text(encoding="utf-8")

    assert "setup_stability_monitoring()" in app_src
    assert 'app.add_route("/api/client-error"' in app_src
    assert 'app.add_route("/api/launcher-shutdown"' in app_src
    assert 'app.add_route("/api/startup-state"' in app_src
    assert "window.__thothStartupPollInstalled" in app_src
    assert "window.location.reload()" in app_src
    assert "async def _cleanup_runtime" in app_src
    assert "_ch_registry.all_channels()" in app_src
    assert "await asyncio.wait_for(_ch.stop(), timeout=10)" in app_src
    assert "os._exit(0)" in app_src
    assert "ui.navigate.reload()" in app_src
    assert 'ui.navigate.to("/")' not in app_src.split("# ── Startup warnings", 1)[0]
    assert "window.__thothClientErrorReporterInstalled" in head_src
    assert "thothReportClientEvent" in head_src
    assert "connection_state" in head_src
    assert "if (!document.body)" in head_src
    assert "DOMContentLoaded" in head_src
    assert "document.getElementById('thoth-ctx-menu')" in head_src
    assert "def safe_ui_task(" in timer_src
    safe_task_src = timer_src.split("def safe_ui_task", 1)[1].split("def deactivate_on_disconnect", 1)[0]
    assert "client = ui.context.client" in safe_task_src
    assert "with client:" in safe_task_src
    assert "client = ui.context.client" in defer_src
    assert "with client:" in defer_src
    assert "def _load_settings_tab" in settings_src
    assert "p.settings_dlg.open()" in settings_src
    assert "defer_ui(lambda: _load_settings_tab(_initial_name)" in settings_src
    assert "start_model_catalog_refresh_background" in settings_src
    assert "build_cached_model_catalog_rows" in settings_src
    assert "_render_provider_summaries" in catalog_src
    assert "Open one provider or search" in catalog_src
    assert "save_workflow_draft" in dialog_src
    assert "Recovered unsaved draft" in dialog_src
    assert "safe_timer(2.0, _autosave_draft)" in dialog_src
    assert "physicsTimer" in graph_src
    assert "window._thothGraph !== G" in graph_src
    assert "G.network.setOptions({ physics: false })" in graph_src
    assert "_is_transient_stream_disconnect" in agent_src
    assert "provider stream disconnected" in agent_src
    assert "start_performance_monitor()" in app_src
    assert "schedule_idle_extraction" in app_src
    assert "cleanup_old_checkpoints" in app_src
    assert '_ch_config.set("tunnel", "tunnel_main_app", _main_app_tunnel)' in app_src
    assert '_ch_config.set("tunnel", "tunnel_main_app", enabled)' in settings_src
    assert '_ch_config.set("tunnel", "tunnel_main_app", e.args)' not in settings_src
    assert "install_asyncio_exception_handler(loop)" in discord_src
    assert "loop.run_until_complete(client.close())" in discord_src
    assert "loop.shutdown_asyncgens()" in discord_src
    assert "stability.py" in installer_src
    assert "self._quitting = False" in Path("launcher.py").read_text(encoding="utf-8")
    assert 'name="quit-worker"' in Path("launcher.py").read_text(encoding="utf-8")
    assert 'name="quit-watchdog"' in Path("launcher.py").read_text(encoding="utf-8")
    assert "Quit watchdog forcing launcher exit after timeout" in Path("launcher.py").read_text(encoding="utf-8")


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


def test_stability_suppresses_benign_windows_proactor_reset():
    import stability

    context = {
        "handle": "<Handle _ProactorBasePipeTransport._call_connection_lost()>",
    }
    benign = ConnectionResetError(
        10054,
        "An existing connection was forcibly closed by the remote host",
    )

    assert stability._is_benign_asyncio_connection_reset(
        "Exception in callback _ProactorBasePipeTransport._call_connection_lost()",
        benign,
        context,
    )
    assert not stability._is_benign_asyncio_connection_reset(
        "Exception in callback something_else()",
        ValueError("boom"),
        {},
    )


def test_checkpoint_cleanup_keeps_latest_and_prunes_old(data_dir, monkeypatch):
    import sqlite3
    import threads

    threads = importlib.reload(threads)
    monkeypatch.setattr(threads, "DB_PATH", str(data_dir / "threads.db"))
    threads._init_thread_db(raise_on_error=True)

    with sqlite3.connect(threads.DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, value TEXT)"
        )
        conn.execute(
            "INSERT INTO thread_meta (thread_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("old-thread", "Old", "2000-01-01T00:00:00", "2000-01-01T00:00:00"),
        )
        for idx in range(5):
            cid = f"cp-{idx}"
            conn.execute("INSERT INTO checkpoints VALUES (?, ?, ?)", ("old-thread", "", cid))
            conn.execute("INSERT INTO writes VALUES (?, ?, ?, ?)", ("old-thread", "", cid, "x"))
        conn.commit()

    stats = threads.cleanup_old_checkpoints(keep_per_thread=2, min_age_minutes=0)

    with sqlite3.connect(threads.DB_PATH) as conn:
        checkpoint_ids = [
            row[0]
            for row in conn.execute(
                "SELECT checkpoint_id FROM checkpoints ORDER BY rowid"
            ).fetchall()
        ]
        write_ids = [
            row[0]
            for row in conn.execute(
                "SELECT checkpoint_id FROM writes ORDER BY rowid"
            ).fetchall()
        ]

    assert stats["checkpoints"] == 3
    assert stats["writes"] == 3
    assert checkpoint_ids == ["cp-3", "cp-4"]
    assert write_ids == ["cp-3", "cp-4"]
