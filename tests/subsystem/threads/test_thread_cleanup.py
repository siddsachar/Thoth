from __future__ import annotations

from datetime import datetime, timedelta
import importlib
import os
import sqlite3
import sys
import threading
import queue

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


def _insert_thread_checkpoint(threads, thread_id: str, checkpoint_id: str) -> None:
    threads.checkpointer.setup()
    with sqlite3.connect(threads.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id) VALUES (?, '', ?)",
            (thread_id, checkpoint_id),
        )
        conn.execute(
            "INSERT INTO writes "
            "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel) "
            "VALUES (?, '', ?, ?, 0, ?)",
            (thread_id, checkpoint_id, "task", "messages"),
        )
        conn.commit()


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


def test_parent_delete_recursively_removes_direct_and_nested_agent_child_state(
    tmp_path,
    monkeypatch,
) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    tasks = stack["tasks"]
    cleanup = stack["cleanup"]
    agent_runs = importlib.import_module("row_bot.agent_runs")

    parent_id = threads.create_thread("Parent", thread_id="agent-parent")
    child_id = threads.create_thread(
        "Direct child",
        thread_id="agent-child-direct",
        thread_type="agent_child",
    )
    nested_id = threads.create_thread(
        "Nested child",
        thread_id="agent-child-nested",
        thread_type="agent_child",
    )
    direct_run = agent_runs.create_agent_run(
        run_id="agent-run-direct",
        kind="subagent",
        status="completed",
        parent_thread_id=parent_id,
        thread_id=child_id,
        display_name="Direct child",
    )
    nested_run = agent_runs.create_agent_run(
        run_id="agent-run-nested",
        kind="subagent",
        status="completed",
        parent_run_id=direct_run["id"],
        parent_thread_id=child_id,
        thread_id=nested_id,
        display_name="Nested child",
    )
    workflow_run = agent_runs.create_agent_run(
        run_id="workflow-audit-run",
        kind="workflow",
        status="completed",
        thread_id=parent_id,
        task_id="workflow-audit-task",
        display_name="Workflow audit",
    )
    for run in (direct_run, nested_run, workflow_run):
        agent_runs.append_agent_event(
            run["id"],
            "summary.updated",
            {"summary": run["display_name"]},
        )
    agent_runs.create_agent_run_edge(direct_run["id"], nested_run["id"])
    assert agent_runs.acquire_agent_write_lock(
        "thread:direct",
        direct_run["id"],
        thread_id=child_id,
    )
    assert agent_runs.acquire_agent_write_lock(
        "thread:nested",
        nested_run["id"],
        parent_run_id=direct_run["id"],
        thread_id=nested_id,
    )
    approval_ids = []
    for run, source_thread_id in (
        (direct_run, child_id),
        (nested_run, nested_id),
    ):
        _token, approval_id = tasks.create_approval_request(
            run_id=f"approval-{run['id']}",
            task_id="",
            step_id="agent_interrupt",
            message="Approve child",
            agent_run_id=run["id"],
            resume_kind="agent_run",
            source_thread_id=source_thread_id,
            parent_thread_id=parent_id,
        )
        approval_ids.append(approval_id)

    now = datetime.now().isoformat()
    conn = tasks._get_conn()
    try:
        for goal_id, thread_id, run_id in (
            ("goal-direct", child_id, direct_run["id"]),
            ("goal-nested", nested_id, nested_run["id"]),
        ):
            conn.execute(
                "INSERT INTO thread_goals "
                "(id, thread_id, objective, status, created_at, updated_at, active_run_id) "
                "VALUES (?, ?, ?, 'active', ?, ?, ?)",
                (goal_id, thread_id, goal_id, now, now, run_id),
            )
        conn.commit()
    finally:
        conn.close()

    killed_shell: list[str] = []
    killed_browser: list[str] = []

    class _SessionManager:
        def __init__(self, calls: list[str]):
            self.calls = calls

        def kill_session(self, thread_id: str) -> None:
            self.calls.append(thread_id)

    shell_tool = importlib.import_module("row_bot.tools.shell_tool")
    browser_tool = importlib.import_module("row_bot.tools.browser_tool")
    monkeypatch.setattr(shell_tool, "get_session_manager", lambda: _SessionManager(killed_shell))
    monkeypatch.setattr(shell_tool, "clear_shell_history", lambda _thread_id: None)
    monkeypatch.setattr(browser_tool, "get_session_manager", lambda: _SessionManager(killed_browser))
    monkeypatch.setattr(browser_tool, "clear_browser_history", lambda _thread_id: None)

    for index, thread_id in enumerate((child_id, nested_id), start=1):
        _insert_thread_checkpoint(threads, thread_id, f"checkpoint-{index}")
        threads.save_thread_draft(thread_id, f"draft-{index}")
        threads.save_media_file(thread_id, f"media-{index}.bin", b"owned")
        threads.save_thread_media(thread_id, {"entries": []})
        (threads._THREAD_UI_DIR / f"{thread_id}.images.json").write_text(
            "{}",
            encoding="utf-8",
        )

    result = cleanup.delete_thread(parent_id)

    assert result.deleted is True
    assert result.warnings == ()
    assert all(not threads._thread_exists(thread_id) for thread_id in (parent_id, child_id, nested_id))
    with sqlite3.connect(threads.DB_PATH) as conn:
        for thread_id in (child_id, nested_id):
            assert conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM writes WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0] == 0
    for thread_id in (child_id, nested_id):
        assert not (threads._MEDIA_DIR / thread_id).exists()
        assert not (threads._THREAD_UI_DIR / f"{thread_id}.media.json").exists()
        assert not (threads._THREAD_UI_DIR / f"{thread_id}.images.json").exists()
        assert not (threads._THREAD_UI_DIR / f"{thread_id}.draft.json").exists()
    assert agent_runs.get_agent_run(direct_run["id"]) is None
    assert agent_runs.get_agent_run(nested_run["id"]) is None
    assert agent_runs.get_agent_run(workflow_run["id"]) is not None
    assert agent_runs.get_agent_events(workflow_run["id"])
    assert agent_runs.list_agent_write_locks() == []
    conn = tasks._get_conn()
    try:
        remaining_event_run_ids = {
            str(row[0])
            for row in conn.execute("SELECT run_id FROM agent_run_events").fetchall()
        }
        assert remaining_event_run_ids == {workflow_run["id"]}
        assert conn.execute("SELECT COUNT(*) FROM agent_run_edges").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM thread_goals").fetchone()[0] == 0
        for approval_id in approval_ids:
            assert conn.execute(
                "SELECT COUNT(*) FROM approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()[0] == 0
    finally:
        conn.close()
    assert set((parent_id, child_id, nested_id)) <= set(killed_shell)
    assert set((parent_id, child_id, nested_id)) <= set(killed_browser)


def test_parent_delete_cancels_active_child_and_blocks_late_child_writes(
    tmp_path,
    monkeypatch,
) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    cleanup = stack["cleanup"]
    agent_runs = importlib.import_module("row_bot.agent_runs")
    state_module = importlib.import_module("row_bot.ui.state")

    parent_id = threads.create_thread("Parent", thread_id="active-agent-parent")
    child_id = threads.create_thread(
        "Active child",
        thread_id="active-agent-child",
        thread_type="agent_child",
    )
    child_run = agent_runs.create_agent_run(
        run_id="active-agent-run",
        kind="subagent",
        status="running",
        parent_thread_id=parent_id,
        thread_id=child_id,
        display_name="Active child",
    )
    stop_order: list[tuple[str, bool]] = []
    original_stop = agent_runs.stop_agent_run

    def _record_stop(run_id: str):
        stop_order.append((run_id, agent_runs.get_agent_run(run_id) is not None))
        return original_stop(run_id)

    monkeypatch.setattr(agent_runs, "stop_agent_run", _record_stop)
    generation = state_module.GenerationState(
        thread_id=child_id,
        q=queue.Queue(),
        stop_event=threading.Event(),
        config={"configurable": {"thread_id": child_id}},
        enabled_tools=[],
    )
    state_module._active_generations[child_id] = generation
    from row_bot.runtime import executions
    registry = executions.GenerationRuntimeRegistry()
    monkeypatch.setattr(executions, "generation_registry", registry)
    handle = registry.register(child_id, stop_event=generation.stop_event, domain="agent", domain_id=child_run["id"])
    entered, release = threading.Event(), threading.Event()
    def producer():
        entered.set()
        assert release.wait(timeout=5)
        agent_runs.finish_agent_run(child_run["id"], "stopped")
    worker = registry.launch(handle, producer)
    assert entered.wait(timeout=2)
    try:
        result = cleanup.delete_thread(parent_id)

        assert result.deleted is False
        assert stop_order == [(child_run["id"], True)]
        assert generation.stop_event.is_set()
        assert not handle.producer_done.is_set()
        assert agent_runs.get_agent_run(child_run["id"])["status"] == "stopping"
        assert cleanup.is_thread_deleting(parent_id) is True
        assert cleanup.is_thread_deleting(child_id) is True
        threads._save_thread_meta(child_id, "Late child resurrection")
        assert threads._thread_exists(child_id) is True
        assert threads.get_thread_name(child_id) == "Active child"
        with pytest.raises((ValueError, RuntimeError), match="delet"):
            threads._save_thread_meta(child_id, "Premature explicit recreation", allow_recreate=True)
    finally:
        release.set()
        worker.join(timeout=3)
        state_module._active_generations.pop(child_id, None)

    assert handle.producer_done.is_set()
    assert cleanup.delete_thread(parent_id).deleted is True
    assert not threads._thread_exists(parent_id)
    assert not threads._thread_exists(child_id)
    # Durable tombstones continue rejecting implicit late writes after cleanup.
    assert cleanup.is_thread_deleting(child_id) is True
    assert agent_runs.get_agent_run(child_run["id"]) is None


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


def test_deletion_resumes_after_metadata_purge_before_durable_completion(tmp_path, monkeypatch) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads, cleanup = stack["threads"], stack["cleanup"]
    from row_bot.runtime import admissions
    thread_id = threads.create_thread("Crash cut", thread_id="delete-after-row-purge")
    admissions.close_admission(thread_id)
    admissions.advance_deletion(thread_id, "producer_released")
    threads._purge_thread_rows(thread_id)
    late_sidecar = threads._THREAD_UI_DIR / f"{thread_id}.draft.json"
    late_sidecar.write_text("{}", encoding="utf-8")

    result = cleanup.delete_thread(thread_id)

    assert result.deleted is True
    assert not late_sidecar.exists()
    assert admissions.deletion_receipt(thread_id) == "DeleteCompleted"
    assert cleanup.delete_thread(thread_id).deleted is True
    threads._save_thread_meta(thread_id, "Late implicit write")
    assert not threads._thread_exists(thread_id)


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


def test_idle_repair_deletes_only_provable_historical_agent_child_orphans(
    tmp_path,
    monkeypatch,
) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    tasks = stack["tasks"]
    cleanup = stack["cleanup"]
    agent_runs = importlib.import_module("row_bot.agent_runs")

    parent_id = threads.create_thread("Parent", thread_id="repair-parent")
    owned_id = threads.create_thread(
        "Owned child",
        thread_id="repair-owned",
        thread_type="agent_child",
    )
    orphan_id = threads.create_thread(
        "Orphan child",
        thread_id="repair-orphan",
        thread_type="agent_child",
    )
    recent_id = threads.create_thread(
        "Recent child",
        thread_id="repair-recent",
        thread_type="agent_child",
    )
    malformed_owner_id = threads.create_thread(
        "Malformed owner child",
        thread_id="repair-malformed-owner",
        thread_type="agent_child",
    )
    locked_id = threads.create_thread(
        "Locked child",
        thread_id="repair-locked",
        thread_type="agent_child",
    )
    agent_runs.create_agent_run(
        run_id="repair-owned-run",
        kind="subagent",
        status="completed",
        parent_thread_id=parent_id,
        thread_id=owned_id,
        display_name="Owned child",
    )

    old = (datetime.now() - timedelta(days=3)).isoformat()
    with sqlite3.connect(threads.DB_PATH) as conn:
        conn.execute(
            "UPDATE thread_meta SET created_at = ?, updated_at = ? "
            "WHERE thread_id != ?",
            (old, old, recent_id),
        )
        conn.commit()
    conn = tasks._get_conn()
    try:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO agent_runs "
            "(id, kind, status, thread_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "malformed-owner-run",
                "not-a-valid-kind",
                "not-a-valid-status",
                malformed_owner_id,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO agent_write_locks "
            "(lock_key, run_id, thread_id, acquired_at) VALUES (?, ?, ?, ?)",
            ("repair-unknown-lock", "missing-run", locked_id, now),
        )
        conn.commit()
    finally:
        conn.close()
    threads.save_media_file(orphan_id, "orphan.bin", b"orphan")

    first = cleanup.sweep_orphaned_thread_artifacts(now=datetime.now())

    assert first["agent_child_candidates"] == 5
    assert first["agent_child_deleted"] == 1
    assert first["agent_child_owned"] == 2
    assert first["agent_child_unknown"] == 1
    assert first["agent_child_recent"] == 1
    assert first["agent_child_failures"] == 0
    assert threads._thread_exists(orphan_id) is False
    assert not (threads._MEDIA_DIR / orphan_id).exists()
    for retained_id in (owned_id, recent_id, malformed_owner_id, locked_id):
        assert threads._thread_exists(retained_id) is True

    second = cleanup.sweep_orphaned_thread_artifacts(now=datetime.now())

    assert second["agent_child_deleted"] == 0
    assert threads._thread_exists(orphan_id) is False


def test_idle_agent_child_repair_retains_everything_when_ownership_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    stack = _fresh_stack(tmp_path, monkeypatch)
    threads = stack["threads"]
    cleanup = stack["cleanup"]
    child_id = threads.create_thread(
        "Unknown child",
        thread_id="repair-ownership-unavailable",
        thread_type="agent_child",
    )
    old = (datetime.now() - timedelta(days=3)).isoformat()
    with sqlite3.connect(threads.DB_PATH) as conn:
        conn.execute(
            "UPDATE thread_meta SET created_at = ?, updated_at = ? WHERE thread_id = ?",
            (old, old, child_id),
        )
        conn.commit()
    monkeypatch.setattr(cleanup, "_agent_child_ownership_states", lambda _ids: None)

    stats = cleanup.sweep_orphaned_thread_artifacts(now=datetime.now())

    assert stats["agent_child_deleted"] == 0
    assert stats["agent_child_unknown"] == 1
    assert threads._thread_exists(child_id) is True


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
