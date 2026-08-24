from __future__ import annotations

from datetime import datetime, timedelta
import importlib
import os
import sqlite3
import sys
import threading

import pytest


pytestmark = pytest.mark.subsystem


def _fresh_stack(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    previous_threads = sys.modules.get("row_bot.threads")
    if previous_threads is not None:
        try:
            previous_threads.conn.close()
        except Exception:
            pass
    tasks = importlib.reload(importlib.import_module("row_bot.tasks"))
    threads = importlib.reload(importlib.import_module("row_bot.threads"))
    cleanup = importlib.reload(importlib.import_module("row_bot.thread_cleanup"))
    importlib.reload(importlib.import_module("row_bot.agent_runs"))
    designer_storage = importlib.reload(importlib.import_module("row_bot.designer.storage"))
    designer_history = importlib.reload(importlib.import_module("row_bot.designer.history"))
    designer_publish = importlib.reload(importlib.import_module("row_bot.designer.publish"))
    designer_session = importlib.reload(importlib.import_module("row_bot.designer.session"))
    for name in (
        "row_bot.developer.storage",
        "row_bot.developer.todos",
        "row_bot.developer.change_ledger",
        "row_bot.developer.sandbox_runtime",
        "row_bot.developer.worktrees",
        "row_bot.developer.inspector_snapshot",
        "row_bot.tools.shell_tool",
        "row_bot.tools.browser_tool",
    ):
        importlib.reload(importlib.import_module(name))

    return {
        "data_dir": data_dir,
        "tasks": tasks,
        "threads": threads,
        "cleanup": cleanup,
        "designer_storage": designer_storage,
        "designer_history": designer_history,
        "designer_publish": designer_publish,
        "designer_session": designer_session,
    }


def _insert_thread_task_state(tasks, thread_id: str) -> None:
    conn = tasks._get_conn()
    try:
        conn.execute(
            "INSERT INTO pipeline_state (run_id, task_id, thread_id) VALUES (?, ?, ?)",
            ("run-owned", "", thread_id),
        )
        conn.execute(
            "INSERT INTO approval_requests "
            "(id, run_id, task_id, step_id, resume_token, status, source_thread_id, parent_thread_id) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            ("approval-owned", "run-owned", "", "step", "resume-owned", thread_id, thread_id),
        )
        conn.execute(
            "INSERT INTO approval_channel_refs (approval_id, channel, message_ref) VALUES (?, ?, ?)",
            ("approval-owned", "fake", "message"),
        )
        conn.execute(
            "INSERT INTO channel_thread_refs (thread_id, channel, target, updated_at) VALUES (?, ?, ?, ?)",
            (thread_id, "fake", "target", datetime.now().isoformat()),
        )
        conn.execute(
            "INSERT INTO channel_thread_notifications "
            "(key, thread_id, channel, target, kind, text, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                "notification-owned",
                thread_id,
                "fake",
                "target",
                "completion",
                "large queued payload",
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_normal_thread_deletion_removes_owned_state_and_persistent_media(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    tasks = stack["tasks"]
    cleanup = stack["cleanup"]
    thread_id = threads.create_thread("Delete me", thread_id="delete-me")
    threads.checkpointer.setup()
    with sqlite3.connect(threads.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO thread_events "
            "(thread_id, event_type, event_key, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, "context_compacted", "event-owned", datetime.now().isoformat()),
        )
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id) VALUES (?, '', ?)",
            (thread_id, "checkpoint-owned"),
        )
        conn.execute(
            "INSERT INTO writes "
            "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel) "
            "VALUES (?, '', ?, ?, 0, ?)",
            (thread_id, "checkpoint-owned", "task", "messages"),
        )
        conn.commit()

    threads.save_thread_draft(thread_id, "unfinished secret")
    persistent = threads.save_media_file(thread_id, "keep.png", b"persistent bytes")
    transient = threads.save_media_file(thread_id, "drop.png", b"transient bytes")
    threads.save_thread_media(
        thread_id,
        {"entries": [{"media": [{"path": persistent.name, "persist": True}]}]},
    )
    (threads._THREAD_UI_DIR / f"{thread_id}.images.json").write_text("{}", encoding="utf-8")
    _insert_thread_task_state(tasks, thread_id)

    durable_knowledge = stack["data_dir"] / "memory.db"
    durable_knowledge.write_bytes(b"durable")
    export = stack["data_dir"] / "vault" / "conversations" / "export.md"
    export.parent.mkdir(parents=True)
    export.write_text("explicit export", encoding="utf-8")

    result = cleanup.delete_thread(thread_id)

    assert result.deleted is True
    assert threads._thread_exists(thread_id) is False
    with sqlite3.connect(threads.DB_PATH) as conn:
        assert conn.execute("SELECT COUNT(*) FROM thread_events WHERE thread_id = ?", (thread_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM writes WHERE thread_id = ?", (thread_id,)).fetchone()[0] == 0
    assert not persistent.exists()
    assert not transient.exists()
    assert not (threads._MEDIA_DIR / thread_id).exists()
    assert not (threads._THREAD_UI_DIR / f"{thread_id}.media.json").exists()
    assert not (threads._THREAD_UI_DIR / f"{thread_id}.images.json").exists()
    assert not (threads._THREAD_UI_DIR / f"{thread_id}.draft.json").exists()
    conn = tasks._get_conn()
    try:
        for table in (
            "pipeline_state",
            "approval_requests",
            "approval_channel_refs",
            "channel_thread_refs",
            "channel_thread_notifications",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()
    assert durable_knowledge.read_bytes() == b"durable"
    assert export.read_text(encoding="utf-8") == "explicit export"


def test_workflow_audits_survive_with_deleted_thread_links_scrubbed(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    tasks = stack["tasks"]
    cleanup = stack["cleanup"]
    thread_id = threads.create_thread("Workflow audit", thread_id="workflow-audit")
    now = datetime.now().isoformat()
    conn = tasks._get_conn()
    try:
        conn.execute(
            "INSERT INTO tasks (id, name, prompts, created_at, persistent_thread_id) "
            "VALUES (?, ?, '[]', ?, ?)",
            ("task-audit", "Audit task", now, thread_id),
        )
        conn.execute(
            "INSERT INTO task_runs "
            "(id, task_id, thread_id, started_at, finished_at, status, pipeline_state_id) "
            "VALUES (?, ?, ?, ?, ?, 'completed', ?)",
            ("run-audit", "task-audit", thread_id, now, now, "pipeline-audit"),
        )
        conn.execute(
            "INSERT INTO pipeline_state (run_id, task_id, thread_id) VALUES (?, ?, ?)",
            ("pipeline-audit", "task-audit", thread_id),
        )
        conn.execute(
            "INSERT INTO approval_requests "
            "(id, run_id, task_id, step_id, resume_token, message, channel, status, "
            "source_thread_id, parent_thread_id, approval_payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                "approval-audit",
                "pipeline-audit",
                "task-audit",
                "step",
                "resume-audit",
                "sensitive prompt",
                "fake",
                thread_id,
                thread_id,
                '{"prompt": "sensitive"}',
            ),
        )
        conn.execute(
            "INSERT INTO approval_channel_refs (approval_id, channel, message_ref) "
            "VALUES (?, ?, ?)",
            ("approval-audit", "fake", "external-message"),
        )
        conn.commit()
    finally:
        conn.close()

    cleanup.delete_thread(thread_id)

    conn = tasks._get_conn()
    try:
        task = conn.execute(
            "SELECT persistent_thread_id FROM tasks WHERE id = 'task-audit'"
        ).fetchone()
        run = conn.execute(
            "SELECT thread_id, pipeline_state_id FROM task_runs WHERE id = 'run-audit'"
        ).fetchone()
        approval = conn.execute(
            "SELECT status, message, channel, source_thread_id, parent_thread_id, "
            "approval_payload_json FROM approval_requests WHERE id = 'approval-audit'"
        ).fetchone()
        assert task is not None and task[0] is None
        assert run is not None and tuple(run) == (thread_id, None)
        assert approval is not None
        assert tuple(approval) == ("cancelled", "", "", "", "", "{}")
        assert conn.execute(
            "SELECT COUNT(*) FROM pipeline_state WHERE run_id = 'pipeline-audit'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM approval_channel_refs WHERE approval_id = 'approval-audit'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_repeated_deletion_cleans_late_sidecars_and_write_guard_blocks_active_thread(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    cleanup = stack["cleanup"]
    thread_id = threads.create_thread("Active", thread_id="active-delete")
    token = cleanup._mark_thread_deleting(thread_id)

    assert threads.append_checkpoint_messages(thread_id, ["late"]) is False
    with pytest.raises(RuntimeError, match="being deleted"):
        threads.save_media_file(thread_id, "late.png", b"late")
    threads._save_thread_meta(thread_id, "Late resurrection")
    cleanup.finish_thread_deletion(thread_id, token)
    assert threads._thread_exists(thread_id) is False

    late_sidecar = threads._THREAD_UI_DIR / f"{thread_id}.draft.json"
    late_sidecar.write_text("{}", encoding="utf-8")
    assert cleanup.delete_thread(thread_id).deleted is True
    assert not late_sidecar.exists()
    assert cleanup.delete_thread(thread_id).deleted is False


def test_explicit_channel_recreation_can_reuse_a_deleted_thread_id(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    cleanup = stack["cleanup"]
    thread_id = threads.create_thread("Channel", thread_id="stable-channel-id")
    cleanup.delete_thread(thread_id)

    threads._save_thread_meta(
        thread_id,
        "New inbound channel conversation",
        allow_recreate=True,
    )

    assert cleanup.is_thread_deleting(thread_id) is False
    assert threads._thread_exists(thread_id) is True


def test_deletion_guard_stays_until_an_active_producer_finalizes(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    cleanup = stack["cleanup"]
    thread_id = threads.create_thread("Active", thread_id="producer-active")
    token = cleanup._mark_thread_deleting(thread_id)
    cleanup._purge_owned_state(thread_id)
    entered = threading.Event()
    release = threading.Event()
    checks = 0

    def _producer_active(_thread_id: str) -> bool:
        nonlocal checks
        checks += 1
        if checks == 1:
            entered.set()
            release.wait(timeout=2)
            return True
        return False

    monkeypatch.setattr(cleanup, "_thread_has_active_producer", _producer_active)
    worker = threading.Thread(target=cleanup._deferred_finish, args=(thread_id, token))
    worker.start()
    assert entered.wait(timeout=2)
    threads._save_thread_meta(thread_id, "Late resurrection")
    assert threads._thread_exists(thread_id) is False
    assert cleanup.is_thread_deleting(thread_id) is True
    release.set()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert cleanup.is_thread_deleting(thread_id) is False


def test_designer_conversation_detaches_but_project_artifacts_survive(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    cleanup = stack["cleanup"]
    storage = stack["designer_storage"]
    history = stack["designer_history"]
    publish = stack["designer_publish"]
    from row_bot.designer.state import DesignerProject

    project = DesignerProject(id="design-keep", name="Keep design", thread_id="designer-thread")
    storage.save_project(project)
    storage.save_asset_bytes(project.id, "asset", "asset.png", b"asset")
    history.snapshot(project, "before delete")
    publish.ensure_published_dir()
    published = publish.PUBLISHED_DIR / f"{project.id}.html"
    published.write_text("published", encoding="utf-8")
    threads.create_thread(
        "Designer",
        thread_id=project.thread_id,
        project_id=project.id,
    )

    cleanup.delete_thread("designer-thread")

    restored = storage.load_project(project.id)
    assert restored is not None
    assert restored.thread_id is None
    assert (storage.ASSETS_DIR / project.id).exists()
    assert (history.HISTORY_DIR / project.id).exists()
    assert published.exists()


def test_design_deletion_removes_history_publish_cache_and_all_linked_threads(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    storage = stack["designer_storage"]
    history = stack["designer_history"]
    publish = stack["designer_publish"]
    session = stack["designer_session"]
    from row_bot.designer.state import DesignerProject

    project = DesignerProject(id="design-delete", name="Delete design", thread_id="design-thread-json")
    storage.save_project(project)
    storage.save_reference_bytes(project.id, "ref", "brief.txt", b"brief")
    storage.save_asset_bytes(project.id, "asset", "asset.png", b"asset")
    history.snapshot(project, "snapshot")
    publish.ensure_published_dir()
    published = publish.PUBLISHED_DIR / f"{project.id}.html"
    published.write_text("published", encoding="utf-8")
    threads.create_thread("JSON link", thread_id="design-thread-json", project_id=project.id)
    threads.create_thread("Metadata link", thread_id="design-thread-meta", project_id=project.id)
    session.set_active_project(project)

    assert storage.delete_project(project.id) is True

    assert storage.load_project(project.id) is None
    assert not (storage.REFERENCES_DIR / project.id).exists()
    assert not (storage.ASSETS_DIR / project.id).exists()
    assert not (history.HISTORY_DIR / project.id).exists()
    assert not published.exists()
    assert not threads._thread_exists("design-thread-json")
    assert not threads._thread_exists("design-thread-meta")
    assert session.get_ui_active_project() is None


def test_managed_path_rejects_escape_and_root_deletion(tmp_path, monkeypatch) -> None:
    cleanup = _fresh_stack(tmp_path, monkeypatch)["cleanup"]
    root = tmp_path / "managed"
    root.mkdir()

    assert cleanup.resolve_managed_path(root, "child").parent == root.resolve()
    with pytest.raises(ValueError, match="outside"):
        cleanup.resolve_managed_path(root, root.parent / "outside")
    with pytest.raises(ValueError, match="root itself"):
        cleanup.resolve_managed_path(root, root)


def test_developer_current_folder_and_unimported_sandbox_are_preserved(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    cleanup = stack["cleanup"]
    storage = importlib.import_module("row_bot.developer.storage")
    todos = importlib.import_module("row_bot.developer.todos")
    ledger = importlib.import_module("row_bot.developer.change_ledger")
    sandbox = importlib.import_module("row_bot.developer.sandbox_runtime")
    from row_bot.developer.state import DeveloperTodo

    repo = tmp_path / "user-repository"
    repo.mkdir()
    user_file = repo / "uncommitted.txt"
    user_file.write_text("user work", encoding="utf-8")
    workspace = storage.add_or_update_local_workspace(str(repo))
    thread_id = threads.create_thread(
        "Current folder",
        thread_id="developer-current",
        thread_type="code",
        developer_workspace_id=workspace.id,
        project_workspace_id=workspace.id,
    )
    workspace.default_thread_id = thread_id
    workspace.hidden = True
    storage.save_workspace(workspace)
    todos.save_todos(thread_id, [DeveloperTodo(id="todo", label="Owned")])
    ledger.record_change_set(
        workspace_id=workspace.id,
        thread_id=thread_id,
        summary="Imported change",
        files=[],
    )
    imported = sandbox.SandboxPendingChange(
        id="imported",
        workspace_id=workspace.id,
        thread_id=thread_id,
        command="edit",
        patch="diff",
        files=["imported.txt"],
        created_at="2026-08-24T00:00:00",
        imported=True,
    )
    unimported = sandbox.SandboxPendingChange(
        id="unimported",
        workspace_id=workspace.id,
        thread_id=thread_id,
        command="edit",
        patch="diff",
        files=["recovery.txt"],
        created_at="2026-08-24T00:00:00",
    )
    sandbox._save_pending_payload({"changes": [imported.to_dict(), unimported.to_dict()]})

    result = cleanup.delete_thread(thread_id)

    assert result.retained_sandbox is True
    assert repo.exists()
    assert user_file.read_text(encoding="utf-8") == "user work"
    restored = storage.get_workspace(workspace.id)
    assert restored is not None
    assert restored.hidden is False
    assert restored.default_thread_id == ""
    assert todos.list_todos(thread_id) == []
    assert ledger.list_change_sets(thread_id=thread_id, include_reverted=True) == []
    pending = sandbox.list_pending_changes(
        workspace_id=workspace.id,
        thread_id=thread_id,
        include_imported=True,
    )
    assert [item.id for item in pending] == ["unimported"]


def test_idle_orphan_sweep_removes_only_unowned_managed_artifacts(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    cleanup = stack["cleanup"]
    storage = stack["designer_storage"]
    history = stack["designer_history"]
    publish = stack["designer_publish"]
    from row_bot.designer.state import DesignerProject

    threads.create_thread("Live", thread_id="live-thread")
    live_media = threads.save_media_file("live-thread", "live.bin", b"live")
    orphan_media = threads._MEDIA_DIR / "orphan-thread"
    orphan_media.mkdir(parents=True)
    (orphan_media / "orphan.bin").write_bytes(b"orphan")
    orphan_draft = threads._THREAD_UI_DIR / "orphan-thread.draft.json"
    orphan_draft.write_text("{}", encoding="utf-8")

    project = DesignerProject(id="live-project", name="Live")
    storage.save_project(project)
    live_history = history.HISTORY_DIR / project.id
    live_history.mkdir(parents=True)
    orphan_history = history.HISTORY_DIR / "orphan-project"
    orphan_history.mkdir(parents=True)
    publish.ensure_published_dir()
    live_publish = publish.PUBLISHED_DIR / f"{project.id}.html"
    live_publish.write_text("live", encoding="utf-8")
    orphan_publish = publish.PUBLISHED_DIR / "orphan-project.html"
    orphan_publish.write_text("orphan", encoding="utf-8")
    stale_temp = storage.PROJECTS_DIR / "stale.atomic.tmp"
    stale_temp.write_text("temp", encoding="utf-8")
    corrupt_project = storage.PROJECTS_DIR / "recoverable-project.json"
    corrupt_project.write_text("{not valid json", encoding="utf-8")
    recoverable_history = history.HISTORY_DIR / "recoverable-project"
    recoverable_history.mkdir(parents=True)
    recoverable_publish = publish.PUBLISHED_DIR / "recoverable-project.html"
    recoverable_publish.write_text("recoverable", encoding="utf-8")
    sandbox = importlib.import_module("row_bot.developer.sandbox_runtime")
    nested_user_temp = sandbox.SANDBOX_ROOT / "workspace" / "shadow" / "user.tmp"
    nested_user_temp.parent.mkdir(parents=True)
    nested_user_temp.write_text("unimported user file", encoding="utf-8")
    old = (datetime.now() - timedelta(days=2)).timestamp()
    os.utime(stale_temp, (old, old))
    os.utime(nested_user_temp, (old, old))

    stats = cleanup.sweep_orphaned_thread_artifacts(now=datetime.now())

    assert stats["media_dirs"] == 1
    assert stats["thread_ui_files"] == 1
    assert stats["designer_history"] == 1
    assert stats["designer_published"] == 1
    assert stats["temp_files"] == 1
    assert live_media.exists()
    assert live_history.exists()
    assert live_publish.exists()
    assert recoverable_history.exists()
    assert recoverable_publish.exists()
    assert nested_user_temp.exists()
    assert not orphan_media.exists()
    assert not orphan_draft.exists()
    assert not orphan_history.exists()
    assert not orphan_publish.exists()


def test_sqlite_compaction_is_thresholded_and_reclaims_file_space(tmp_path, monkeypatch) -> None:
    cleanup = _fresh_stack(tmp_path, monkeypatch)["cleanup"]
    db_path = tmp_path / "compact.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE payloads (value BLOB)")
        conn.executemany(
            "INSERT INTO payloads VALUES (?)",
            [(b"x" * 8192,) for _ in range(512)],
        )
        conn.commit()
        conn.execute("DELETE FROM payloads")
        conn.commit()
    before = db_path.stat().st_size

    skipped = cleanup.compact_sqlite_database(db_path)
    assert skipped["compacted"] is False
    assert skipped["reason"] == "below_free_bytes_threshold"

    compacted = cleanup.compact_sqlite_database(
        db_path,
        min_free_bytes=1,
        min_free_ratio=0.01,
    )
    assert compacted["compacted"] is True
    assert db_path.stat().st_size < before
