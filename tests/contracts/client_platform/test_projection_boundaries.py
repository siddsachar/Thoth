"""Contested ordering, identity and memory checks independent of the authors."""
from __future__ import annotations

import asyncio
import json
import threading

from langchain_core.messages import AIMessage
import pytest

from row_bot.projection.conversation import ConversationProjection
from row_bot.ui.legacy_adapter.view_subscription import LegacyViewSubscription
from tests.contracts.client_platform.test_headless_lifecycle import command, platform as platform, submit
from tests.helpers.client_platform_fakes import CheckpointCommit, ScriptedAgentStream, StreamBarrier, fixture_id

pytestmark = pytest.mark.contract


def test_f_u04_old_checkpoint_load_cannot_apply_after_a_b_a_selection():
    async def scenario():
        projection = ConversationProjection(fixture_id("view-epoch"))
        entered, release = threading.Event(), threading.Event()
        applied, finished = [], asyncio.Event()
        calls = []

        def load(target):
            calls.append(target)
            if len(calls) == 1:
                entered.set()
                assert release.wait(10)
                return [{"content": "old A before selection changed"}]
            return [{"content": "current A after selection changed"}]

        def apply(target, messages):
            applied.append((target, messages))
            if messages[0]["content"].startswith("current"):
                finished.set()

        viewer = LegacyViewSubscription(projection, load, apply)
        try:
            projection.install_checkpoint("a", "checkpoint-1", [])
            viewer.observe("a")
            assert await asyncio.to_thread(entered.wait, 10)
            viewer.observe("b")
            viewer.observe("a")
            projection.install_checkpoint("a", "checkpoint-2", [])
            release.set()
            await asyncio.wait_for(finished.wait(), 10)
            assert applied == [("a", [{"content": "current A after selection changed"}])]
        finally:
            release.set()
            viewer.close()

    asyncio.run(scenario())


def test_f_p01_two_native_ai_messages_commit_distinct_segments_in_one_pass(platform):
    tool_id, first_id, final_id = map(fixture_id, ("two-ai-tool", "two-ai-first", "two-ai-final"))
    fake = ScriptedAgentStream((
        ("token", "Checking synthetic input"),
        CheckpointCommit((AIMessage(content="Checking synthetic input", id=first_id,
                                   tool_calls=[{"name": "fixture_action", "args": {}, "id": tool_id}]),), first_id),
        ("tool_call", {"id": tool_id, "message_id": first_id}),
        ("tool_done", {"id": tool_id, "message_id": first_id}),
        ("token", "Synthetic result"),
        CheckpointCommit((AIMessage(content="Synthetic result", id=final_id),), final_id),
        ("done", "Synthetic result"),
    ))
    receipt = submit(platform, fake, "two-ai-pass")
    handle = platform.registry.get(receipt["execution_id"])
    assert handle.producer_done.wait(10)
    assert handle.status == "completed"
    from row_bot.runtime.admissions import transaction
    with transaction() as connection:
        segments = connection.execute(
            "SELECT segment_id,native_message_id FROM generation_segments WHERE pass_id=?",
            (handle.pass_id,),
        ).fetchall()
    assert len(segments) == 2
    assert {row[1] for row in segments} == {first_id, final_id}
    assert len({row[0] for row in segments}) == 2
    rows = platform.snapshot("conversation-a")["rows"]
    assert [row["message_id"] for row in rows if row["role"] == "assistant"] == [first_id, final_id]


def test_f_p05_projection_budget_includes_checkpoint_and_live_rows(monkeypatch):
    projection = ConversationProjection(fixture_id("budget-epoch"))
    monkeypatch.setattr(projection, "MAX_CONTENT_BYTES", 4096)
    projection.install_checkpoint("a", "checkpoint-1", [
        AIMessage(content="x" * 1400, id=fixture_id("budget-native-1")),
        AIMessage(content="y" * 1400, id=fixture_id("budget-native-2")),
    ])
    try:
        projection.publish("a", "transcript.delta", {
            "pass_id": fixture_id("budget-pass"), "segment_id": fixture_id("budget-segment"),
            "row_id": "assistant:live:" + fixture_id("budget-row"),
            "render_revision": "2", "public_text_delta": "z" * 2800,
        })
    except ValueError as error:
        assert str(error) == "projection_content_limit"
    snapshot = projection.snapshot("a")
    assert len(json.dumps(snapshot["rows"], ensure_ascii=False).encode()) <= projection.MAX_CONTENT_BYTES


def test_f_p05_oversized_durable_row_retains_authorized_content_reference(platform):
    from row_bot.threads import append_checkpoint_messages
    native_id = fixture_id("oversized-native")
    assert append_checkpoint_messages("conversation-a", [AIMessage(content="synthetic " * 40000, id=native_id)])
    row = platform.transcript("conversation-a")["rows"][0]
    assert len(json.dumps(row).encode()) <= 256 * 1024
    assert row.get("content_ref"), "Truncating durable content without a retrievable reference loses parity"


def test_f_p05_global_history_and_inactive_projection_eviction_are_bounded(monkeypatch):
    projection = ConversationProjection(fixture_id("global-budget-epoch"))
    monkeypatch.setattr(projection, "MAX_GLOBAL_HISTORY_BYTES", 8192)
    monkeypatch.setattr(projection, "MAX_GLOBAL_CONTENT_BYTES", 8192)
    monkeypatch.setattr(projection, "MAX_CONTENT_BYTES", 4096)
    for index in range(8):
        conversation = f"conversation-{index}"
        projection.install_checkpoint(conversation, f"checkpoint-{index}", [
            AIMessage(content="synthetic" * 110, id=fixture_id(f"global-native-{index}-a")),
            AIMessage(content="synthetic" * 110, id=fixture_id(f"global-native-{index}-b")),
        ])
        for _ in range(5):
            projection.publish(conversation, "generation.activity", {"state": "thinking"})
    snapshots = [projection.snapshot(f"conversation-{index}") for index in range(8)]
    assert sum(len(json.dumps(snapshot["rows"], ensure_ascii=False).encode()) for snapshot in snapshots) <= 8192
    assert any(not snapshot["checkpoint_revision"] for snapshot in snapshots)
    assert projection.events_since("conversation-0", "0")["snapshot_required"]
    assert sum(state.event_bytes for state in projection._states.values()) <= 8192


def test_f_p05_many_active_conversations_do_not_exceed_global_content_bound(monkeypatch):
    projection = ConversationProjection(fixture_id("active-global-epoch"))
    monkeypatch.setattr(projection, "MAX_GLOBAL_CONTENT_BYTES", 8192)
    monkeypatch.setattr(projection, "MAX_CONTENT_BYTES", 4096)
    for index in range(40):
        pass_id, segment_id = fixture_id(f"active-pass-{index}"), fixture_id(f"active-segment-{index}")
        projection.publish(f"active-{index}", "transcript.delta", {
            "pass_id": pass_id, "segment_id": segment_id,
            "row_id": f"assistant:live:{pass_id}:{segment_id}",
            "render_revision": "1", "public_text_delta": "Synthetic" * 200,
        })
        actual = sum(state.content_bytes for state in projection._states.values())
        assert actual <= 8192, f"Active content retained {actual} bytes after conversation {index}"


def test_f_p05_evicted_active_reference_recovers_without_restarting_producer(platform, monkeypatch):
    import base64
    from row_bot import threads

    targets = ["conversation-a", "conversation-b", "conversation-c", "conversation-d"]
    for target in targets[2:]:
        threads._save_thread_meta(target, "Synthetic pressure conversation")
    monkeypatch.setattr(platform.projection, "MAX_GLOBAL_CONTENT_BYTES", 900)
    monkeypatch.setattr(platform.projection, "MAX_CONTENT_BYTES", 900)
    barriers = [StreamBarrier() for _ in targets]
    text = "Synthetic " * 60
    fake = ScriptedAgentStream(*[(("token", text), barrier) for barrier in barriers])
    platform.stream_factory = fake.stream
    handles = []
    try:
        for index, target in enumerate(targets):
            receipt = platform.execute(owner_id="fixture-owner", idempotency_key=fixture_id(f"eviction-key-{index}"),
                target=target, command=command("conversation.submit", f"eviction-{index}", {
                    "submission_id": fixture_id(f"eviction-input-{index}"), "text": "Synthetic pressure",
                    "attachment_refs": [], "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}}))
            handles.append(platform.registry.get(receipt["execution_id"]))
            assert barriers[index].entered.wait(10)
        assert not platform.projection._states[targets[0]].live_rows
        assert sum(state.content_bytes for state in platform.projection._states.values()) <= 900
        recovered = platform.snapshot(targets[0])
        row = next(row for row in recovered["rows"] if row["role"] == "assistant")
        assert row["content_status"] == "lazy"
        page = platform.lazy_content(targets[0], row["content_ref"])
        assert json.loads(base64.b64decode(page["data"])) == [{"type": "text", "text": text}]
        assert sum(state.content_bytes for state in platform.projection._states.values()) <= 900
        assert len(fake.calls) == 4
        assert len(platform.registry.active()) == 4
        assert not any(handle.producer_done.is_set() for handle in handles)
    finally:
        for index, handle in enumerate(handles):
            platform.registry.stop(targets[index])
            barriers[index].release.set()
            assert handle.producer_done.wait(10)


def test_f_p05_lazy_content_pages_reconstruct_exact_unicode_and_pin_revision(platform):
    import base64
    from row_bot.application.client_platform import ClientPlatformError
    from row_bot.threads import append_checkpoint_messages

    native_id = fixture_id("lazy-unicode-native")
    text = "Synthetic\n\"\\\U0001f600" * 30000
    assert append_checkpoint_messages("conversation-a", [AIMessage(content=text, id=native_id)])
    cursor, pinned, payload = None, None, bytearray()
    first_cursor = None
    while True:
        page = platform.lazy_content("conversation-a", native_id, limit_bytes=60001, cursor=cursor)
        chunk = base64.b64decode(page["data"])
        assert len(chunk) <= 60001
        pinned = pinned or page["checkpoint_revision"]
        assert page["checkpoint_revision"] == pinned
        payload.extend(chunk)
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
        first_cursor = first_cursor or cursor
    assert json.loads(payload) == [{"type": "text", "text": text}]
    assert append_checkpoint_messages("conversation-a", [AIMessage(content="later", id=fixture_id("later-native"))])
    with pytest.raises(ClientPlatformError, match="cursor_expired"):
        platform.lazy_content("conversation-a", native_id, cursor=first_cursor)


def test_f_p01_canonical_large_block_comparison_keeps_64k_scratch_bound():
    import tracemalloc
    from row_bot.projection.canonical import exact_assistant_equal

    left = [{"type": "text", "text": "Synthetic\n\"\\\U0001f600" * 50000}]
    # Allocate both inputs before tracing so the measurement covers comparison
    # scratch, not the already-owned public content being compared.
    right = json.loads(json.dumps(left))
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        assert exact_assistant_equal(left, right)
        peak = tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()
    assert peak <= 65536, f"Canonical comparison allocated {peak} scratch bytes"


def test_f_p05_safe_legacy_json_checkpoint_preserves_native_identity_and_fields(platform):
    from row_bot import threads

    native_id = fixture_id("legacy-json-native")
    original = AIMessage(content="Synthetic legacy JSON", id=native_id,
                         additional_kwargs={"synthetic_preserved_field": {"value": 7}})
    assert threads.append_checkpoint_messages("conversation-a", [original])
    config = {"configurable": {"thread_id": "conversation-a", "checkpoint_ns": ""}}
    saved = threads.checkpointer.get_tuple(config)
    legacy = {**saved.checkpoint, "channel_values": {**saved.checkpoint["channel_values"],
              "synthetic_other_channel": {"preserve": [1, 2, 3]},
              "messages": [{"lc": 2, "type": "constructor",
                            "id": ["langchain_core", "messages", "ai", "AIMessage"],
                            "kwargs": original.model_dump()}]}}
    with threads.checkpointer.cursor() as cursor:
        cursor.execute("UPDATE checkpoints SET type='json',checkpoint=? WHERE thread_id=? AND checkpoint_id=?",
                       (json.dumps(legacy).encode(), "conversation-a", saved.checkpoint["id"]))
    # This exact legacy payload is readable by the pre-existing safe serializer.
    assert threads.checkpointer.get_tuple(config).checkpoint["channel_values"]["messages"][0] == original
    rows = platform.transcript("conversation-a")["rows"]
    assert [row["message_id"] for row in rows] == [native_id]
    migrated = threads.checkpointer.get_tuple(config)
    assert migrated.checkpoint["channel_values"]["messages"] == [original]
    assert migrated.checkpoint["channel_values"]["synthetic_other_channel"] == {"preserve": [1, 2, 3]}
    assert migrated.parent_config["configurable"]["checkpoint_id"] == saved.checkpoint["id"]


def test_f_p05_large_active_stream_remains_retrievable_and_completes(platform):
    import base64

    native_id = fixture_id("large-live-native")
    text = "Synthetic large live content \U0001f600\n" * 70000
    assert len(text.encode()) > 2 * 1024 * 1024
    barrier = StreamBarrier()
    fake = ScriptedAgentStream((("token", text), barrier,
                               CheckpointCommit((AIMessage(content=text, id=native_id),), native_id),
                               ("done", text)))
    receipt = submit(platform, fake, "large-live")
    handle = platform.registry.get(receipt["execution_id"])
    try:
        assert barrier.entered.wait(20), "Projection pressure interrupted the producer before its barrier"
        snapshot = platform.snapshot("conversation-a")
        assert len(json.dumps(snapshot["rows"], ensure_ascii=False).encode()) <= 2 * 1024 * 1024
        row = next(row for row in snapshot["rows"] if row["role"] == "assistant")
        assert row["content_status"] == "lazy"
        assert row["content_ref"].startswith("live:")
        cursor, content = None, bytearray()
        while True:
            page = platform.lazy_content("conversation-a", row["content_ref"], cursor=cursor)
            content.extend(base64.b64decode(page["data"]))
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        assert json.loads(content) == [{"type": "text", "text": text}]
        assert not handle.producer_done.is_set()
    finally:
        barrier.release.set()
        assert handle.producer_done.wait(20)
    assert handle.status == "completed"
    rows = platform.transcript("conversation-a")["rows"]
    assert [row["message_id"] for row in rows if row["role"] == "assistant"] == [native_id]
    assert next(row for row in rows if row["message_id"] == native_id)["content_ref"] == native_id
