from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import row_bot.threads as threads


pytestmark = pytest.mark.subsystem


@pytest.fixture
def isolated_thread_db(tmp_path, monkeypatch):
    db_path = tmp_path / "threads.db"
    monkeypatch.setattr(threads, "DB_PATH", str(db_path))
    threads._init_thread_db(raise_on_error=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        conn.execute(
            "INSERT INTO thread_meta (thread_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("thread-1", "Test", "now", "now"),
        )
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id) VALUES (?, '', ?)",
            ("thread-1", "rev-1"),
        )
        conn.commit()
    return db_path


def _summary_state(messages, summary="structured summary"):
    return {
        "schema_version": 1,
        "mode": "agent",
        "model_ref": "model:openai:test",
        "source_revision": "rev-1",
        "boundary_message_count": 2,
        "boundary_digest": threads.context_boundary_digest(messages, 2, "agent"),
        "summary": summary,
        "prompt_fingerprint": "prompt",
        "tool_fingerprint": "tools",
        "policy_fingerprint": "policy",
    }


def test_summary_state_cas_is_first_writer_wins(isolated_thread_db):
    messages = [HumanMessage(content="one"), AIMessage(content="answer"), HumanMessage(content="two")]
    first = _summary_state(messages, "first")
    second = _summary_state(messages, "second")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda state: threads.save_summary_state_cas(
                "thread-1",
                state,
                expected_revision="rev-1",
            ),
            (first, second),
        ))

    assert sorted(results) == [False, True]
    loaded = threads.load_validated_summary_state(
        "thread-1",
        "agent",
        messages=messages,
    )
    assert loaded is not None
    assert loaded["summary"] in {"first", "second"}


def test_summary_validation_rejects_mode_and_boundary_mismatch(isolated_thread_db):
    messages = [HumanMessage(content="one"), AIMessage(content="answer"), HumanMessage(content="two")]
    assert threads.save_summary_state_cas(
        "thread-1",
        _summary_state(messages),
        expected_revision="rev-1",
    )

    assert threads.load_validated_summary_state(
        "thread-1", "chat_only", messages=messages
    ) is None
    changed = [HumanMessage(content="tampered"), *messages[1:]]
    assert threads.load_validated_summary_state(
        "thread-1", "agent", messages=changed
    ) is None


def test_summary_cas_rejects_stale_checkpoint_revision(isolated_thread_db):
    messages = [HumanMessage(content="one"), AIMessage(content="answer"), HumanMessage(content="two")]
    with sqlite3.connect(isolated_thread_db) as conn:
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id) VALUES (?, '', ?)",
            ("thread-1", "rev-2"),
        )
        conn.commit()

    assert not threads.save_summary_state_cas(
        "thread-1",
        _summary_state(messages),
        expected_revision="rev-1",
    )


def test_timeline_event_is_idempotent_bounded_and_excluded_from_messages(isolated_thread_db):
    event = threads.append_thread_event(
        "thread-1",
        "context_compacted",
        "same-key",
        after_message_count=1,
        source_revision="rev-1",
        boundary_digest_prefix="a" * 64,
    )
    duplicate = threads.append_thread_event(
        "thread-1",
        "context_compacted",
        "same-key",
        after_message_count=2,
        source_revision="rev-1",
        boundary_digest_prefix="b" * 64,
    )

    assert duplicate["id"] == event["id"]
    assert len(threads.list_thread_events("thread-1")) == 1
    encoded = json.dumps(event["payload"])
    assert "transcript" not in encoded
    assert "tool_calls" not in encoded
    assert "summary" not in encoded
    merged = threads.merge_thread_events(
        [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "world"}],
        [event],
    )
    assert [message["role"] for message in merged] == ["user", "context_event", "assistant"]


def test_terminal_failure_event_is_persistent_actionable_and_distinct(isolated_thread_db):
    event = threads.append_thread_event(
        "thread-1",
        "context_compaction_failed",
        "failure-key",
        source_revision="rev-1",
    )

    assert event["event_type"] == "context_compaction_failed"
    assert event["payload"]["severity"] == "warning"
    assert "Retry" in event["payload"]["display_copy"]
    assert "larger-context model" in event["payload"]["display_copy"]
    assert event["payload"]["display_copy"] != threads.CONTEXT_COMPACTED_COPY


def test_channel_delivery_claim_is_atomic_and_durable(isolated_thread_db):
    event = threads.append_thread_event(
        "thread-1",
        "context_compacted",
        "delivery-key",
        source_revision="rev-1",
    )

    first = threads.claim_thread_event_delivery(event["id"], "telegram")
    second = threads.claim_thread_event_delivery(event["id"], "telegram")
    threads.complete_thread_event_delivery(event["id"], platform_refs=["platform-1"])
    delivered = threads.list_thread_events("thread-1")[0]["payload"]["channel_delivery"]

    assert first is not None
    assert second is None
    assert delivered == {
        "state": "delivered",
        "channel": "telegram",
        "platform_refs": ["platform-1"],
    }


def test_context_usage_snapshot_requires_matching_identity(isolated_thread_db, monkeypatch):
    usage = {
        "schema_version": 1,
        "model_ref": "model:openai:test",
        "mode": "agent",
        "checkpoint_revision": "rev-1",
        "preparation_fingerprint": "prepared",
    }
    threads.save_context_usage("thread-1", usage)
    monkeypatch.setattr(threads, "get_latest_checkpoint_revision", lambda thread_id: "rev-1")

    assert threads.load_context_usage(
        "thread-1",
        expected={"checkpoint_revision": "rev-1", "model_ref": "model:openai:test"},
    ) == usage
    assert threads.load_context_usage(
        "thread-1",
        expected={"checkpoint_revision": "rev-2"},
    ) is None
    assert threads.load_context_usage(
        "thread-1",
        expected={"preparation_fingerprint": "stale"},
    ) is None


def test_settled_snapshot_uses_message_digest_and_can_load_as_last_measured(
    isolated_thread_db,
    monkeypatch,
):
    settled_messages = [HumanMessage(content="one"), AIMessage(content="answer")]
    newer_messages = [*settled_messages, HumanMessage(content="two")]
    usage = {
        "schema_version": 2,
        "snapshot_kind": "settled",
        "model_ref": "model:openai:test",
        "mode": "agent",
        "checkpoint_revision": "rev-1",
        "checkpoint_message_digest": threads.context_boundary_digest(
            settled_messages,
            len(settled_messages),
            "agent",
        ),
        "preparation_fingerprint": "prepared",
    }
    monkeypatch.setattr(
        threads,
        "get_latest_checkpoint_messages",
        lambda thread_id: list(settled_messages),
    )

    assert threads.save_context_usage_cas("thread-1", usage)
    current = threads.load_context_usage(
        "thread-1",
        expected={"model_ref": "model:openai:test"},
        allow_stale=True,
    )
    assert current is not None
    assert current["snapshot_freshness"] == "current"

    monkeypatch.setattr(
        threads,
        "get_latest_checkpoint_messages",
        lambda thread_id: list(newer_messages),
    )
    assert threads.load_context_usage(
        "thread-1",
        expected={"model_ref": "model:openai:test"},
    ) is None
    stale = threads.load_context_usage(
        "thread-1",
        expected={"model_ref": "model:openai:test"},
        allow_stale=True,
    )
    assert stale is not None
    assert stale["snapshot_freshness"] == "stale"
    assert threads.load_context_usage(
        "thread-1",
        expected={"model_ref": "model:anthropic:test"},
        allow_stale=True,
    ) is None


def test_settled_snapshot_cas_rejects_detached_older_message_state(
    isolated_thread_db,
    monkeypatch,
):
    older_messages = [HumanMessage(content="one")]
    current_messages = [*older_messages, AIMessage(content="answer")]
    monkeypatch.setattr(
        threads,
        "get_latest_checkpoint_messages",
        lambda thread_id: list(current_messages),
    )
    usage = {
        "schema_version": 2,
        "snapshot_kind": "settled",
        "model_ref": "model:openai:test",
        "mode": "agent",
        "checkpoint_message_digest": threads.context_boundary_digest(
            older_messages,
            len(older_messages),
            "agent",
        ),
    }

    assert not threads.save_context_usage_cas("thread-1", usage)
    assert threads.load_context_usage("thread-1") is None


def test_global_policy_change_clears_all_display_snapshots(isolated_thread_db):
    with sqlite3.connect(isolated_thread_db) as conn:
        conn.execute(
            "INSERT INTO thread_meta (thread_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("thread-2", "Two", "now", "now"),
        )
        conn.commit()
    threads.save_context_usage("thread-1", {"schema_version": 1, "model_ref": "model:openai:one"})
    threads.save_context_usage("thread-2", {"schema_version": 1, "model_ref": "model:openai:two"})

    threads.clear_all_context_usage()

    assert threads.load_context_usage("thread-1") is None
    assert threads.load_context_usage("thread-2") is None


def test_designer_style_copy_keeps_validated_summary_but_invalidates_usage(
    isolated_thread_db,
    monkeypatch,
):
    messages = [HumanMessage(content="one"), AIMessage(content="answer"), HumanMessage(content="two")]
    with sqlite3.connect(isolated_thread_db) as conn:
        conn.execute(
            "INSERT INTO thread_meta (thread_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("thread-copy", "Copy", "now", "now"),
        )
        conn.commit()
    monkeypatch.setattr(threads, "get_latest_checkpoint_messages", lambda thread_id: list(messages))
    assert threads.save_summary_state_cas(
        "thread-1",
        _summary_state(messages),
        expected_revision="rev-1",
    )
    threads.save_context_usage("thread-copy", {"schema_version": 1, "model_ref": "stale"})

    assert threads.copy_validated_summary_state("thread-1", "thread-copy")
    copied = threads.load_validated_summary_state(
        "thread-copy",
        "agent",
        messages=messages,
    )
    assert copied is not None
    assert copied["summary"] == "structured summary"
    assert threads.load_context_usage("thread-copy") is None
