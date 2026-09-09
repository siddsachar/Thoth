"""New client operations close their owned SQLite connections without GC."""
from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace
import sqlite3

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
import pytest

from tests.contracts.client_platform.test_headless_lifecycle import platform  # noqa: F401


@pytest.fixture
def connections(platform, monkeypatch):
    from row_bot import threads

    # Schema setup predates this change and already ran in the real fixture.
    # Its legacy connection lifetime is outside these seven new call sites.
    monkeypatch.setattr(threads, "_ensure_thread_db", lambda: None)
    monkeypatch.setattr(threads, "_thread_write_blocked", lambda _thread_id: False)
    original = sqlite3.connect
    opened = []
    failure = {"after": None}

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if failure["after"] and sql.startswith(failure["after"]):
                raise sqlite3.OperationalError("Synthetic failure after SQL execution")
            return result

        def close(self):
            self.closed = True
            super().close()

    def connect(*args, **kwargs):
        kwargs["factory"] = TrackedConnection
        connection = original(*args, **kwargs)
        opened.append(connection)  # Strong references make GC irrelevant.
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield SimpleNamespace(opened=opened, failure=failure, raw_connect=original, path=threads.DB_PATH)
    for connection in opened:
        if not connection.closed:
            connection.close()


def _assert_closed(connections):
    assert connections.opened
    assert all(connection.closed for connection in connections.opened)
    for connection in connections.opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def _row(connections):
    with closing(connections.raw_connect(connections.path)) as connection:
        return connection.execute(
            "SELECT name,pinned_at,project_id,client_revision FROM thread_meta WHERE thread_id='conversation-a'"
        ).fetchone()


def _checkpoint(message):
    from row_bot import threads
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [message]}
    checkpoint["channel_versions"] = {"messages": "00000000000000000000000000000001.0000000000000000"}
    threads.checkpointer.put(
        {"configurable": {"thread_id": "conversation-a", "checkpoint_ns": ""}}, checkpoint,
        {"source": "input", "step": 0}, checkpoint["channel_versions"],
    )


def test_metadata_and_list_close_on_success_and_missing_row(platform, connections):
    from row_bot.application.client_platform import ClientPlatformError
    for _ in range(3):
        assert platform._metadata("conversation-a")["name"] == "conversation-a"
        assert len(platform.list_conversations()["items"]) == 2
        with pytest.raises(ClientPlatformError, match="not_found"):
            platform._metadata("missing")
    _assert_closed(connections)


@pytest.mark.parametrize("operation", ["metadata", "list"])
def test_metadata_reads_close_on_sql_error(platform, connections, operation):
    connections.failure["after"] = "SELECT * FROM thread_meta" if operation == "metadata" else "SELECT thread_id FROM thread_meta"
    with pytest.raises(sqlite3.OperationalError, match="Synthetic failure"):
        if operation == "metadata":
            platform._metadata("conversation-a")
        else:
            platform.list_conversations()
    _assert_closed(connections)


@pytest.mark.parametrize("kind,payload", [("rename", {"title": "Renamed"}), ("pin", {"pinned": True})])
@pytest.mark.parametrize("fail", [False, True])
def test_metadata_write_closes_and_preserves_commit_or_rollback(platform, connections, kind, payload, fail):
    before = _row(connections)
    command = {"type": "conversation." + kind, "expected_revision": "0", "payload": payload}
    if fail:
        connections.failure["after"] = "UPDATE thread_meta SET"
        with pytest.raises(sqlite3.OperationalError, match="Synthetic failure"):
            platform._execute(command, "conversation-a")
        assert _row(connections) == before
    else:
        assert platform._execute(command, "conversation-a")["revision"] == "1"
        after = _row(connections)
        assert after[3] == 1
        assert after[0] == "Renamed" if kind == "rename" else bool(after[1])
    _assert_closed(connections)


@pytest.mark.parametrize("case", ["mutation", "unchanged", "missing", "sql_error"])
def test_legacy_resource_write_closes_for_every_exit(platform, connections, case):
    from row_bot import threads
    before = _row(connections)
    target = "missing" if case == "missing" else "conversation-a"
    resource = "" if case == "unchanged" else "synthetic-artifact"
    if case == "sql_error":
        connections.failure["after"] = "UPDATE thread_meta SET"
        with pytest.raises(sqlite3.OperationalError, match="Synthetic failure"):
            threads._set_thread_project_id(target, resource)
    else:
        threads._set_thread_project_id(target, resource)
    after = _row(connections)
    if case == "mutation":
        assert after[2:] == ("synthetic-artifact", 1)
    else:
        assert after == before
    _assert_closed(connections)


@pytest.mark.parametrize("case", ["native", "missing_id", "empty"])
def test_checkpoint_identity_connections_close_for_migration_and_early_returns(platform, connections, case):
    from row_bot import threads
    if case != "empty":
        _checkpoint(HumanMessage(content="Synthetic", id="native-input" if case == "native" else None))
    first = threads.migrate_checkpoint_message_ids("conversation-a")
    second = threads.migrate_checkpoint_message_ids("conversation-a")
    assert first == second
    assert bool(first) == (case != "empty")
    if case != "empty":
        messages = threads.get_latest_checkpoint_messages("conversation-a")
        assert messages[0].id
        assert messages[0].content == "Synthetic"
    _assert_closed(connections)


@pytest.mark.parametrize("native", [False, True])
def test_checkpoint_marker_error_closes_and_rolls_back_before_retry(platform, connections, native):
    from row_bot import threads
    _checkpoint(HumanMessage(content="Synthetic", id="native-input" if native else None))
    connections.failure["after"] = "INSERT INTO checkpoint_identity_migrations"
    with pytest.raises(sqlite3.OperationalError, match="Synthetic failure"):
        threads.migrate_checkpoint_message_ids("conversation-a")
    _assert_closed(connections)
    with closing(connections.raw_connect(connections.path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM checkpoint_identity_migrations").fetchone()[0] == 0
    connections.failure["after"] = None
    revision = threads.migrate_checkpoint_message_ids("conversation-a")
    assert revision == threads.migrate_checkpoint_message_ids("conversation-a")
    _assert_closed(connections)
