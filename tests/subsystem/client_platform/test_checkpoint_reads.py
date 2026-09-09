"""The retained checkpoint owner supports bounded reads and stable upgrades."""
from __future__ import annotations

import base64
import json
import io
from concurrent.futures import ThreadPoolExecutor

import pytest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tests.contracts.client_platform.test_headless_lifecycle import platform  # noqa: F401


def test_public_reads_skip_large_checkpoint_blob_without_deserializing(platform, monkeypatch):
    from row_bot import threads
    from row_bot.runtime.checkpoint_reader import BlobReader
    messages = [HumanMessage(content="Synthetic" * 20000, id=f"history-{index}") for index in range(45)]
    messages.append(AIMessage(content="Unicode 😀\n" * 45000, id="target-native"))
    assert threads.append_checkpoint_messages("conversation-a", messages)
    platform.snapshot("conversation-a")
    reads = []
    original = BlobReader._read
    def read(self, size):
        reads.append(size)
        return original(self, size)
    monkeypatch.setattr(BlobReader, "_read", read)
    monkeypatch.setattr(threads.checkpointer, "get_tuple", lambda *_: (_ for _ in ()).throw(AssertionError("full checkpoint load")))
    assert platform.transcript("conversation-a", limit=7)["has_more"]
    cursor, chunks = None, []
    while True:
        page = platform.lazy_content("conversation-a", "target-native", cursor=cursor)
        chunks.append(base64.b64decode(page["data"]))
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert json.loads(b"".join(chunks))[0]["text"] == messages[-1].content
    assert max(reads) <= 1024


def test_legacy_identity_migration_preserves_content_ids_and_order_once(platform):
    from row_bot import threads
    from langgraph.checkpoint.base import empty_checkpoint
    old = [HumanMessage(content="same"), AIMessage(content="same", id="preserved-ai"),
           ToolMessage(content="Tool result", tool_call_id="preserved-call")]
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": old}
    checkpoint["channel_versions"] = {"messages": "00000000000000000000000000000001.0000000000000000"}
    threads.checkpointer.put({"configurable": {"thread_id": "conversation-a", "checkpoint_ns": ""}}, checkpoint,
                            {"source": "input", "step": 0}, checkpoint["channel_versions"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = list(pool.map(threads.migrate_checkpoint_message_ids, ["conversation-a"] * 2))
    assert revisions[0] == revisions[1]
    migrated = threads.get_latest_checkpoint_messages("conversation-a")
    assert all(message.id for message in migrated)
    assert migrated[1].id == "preserved-ai"
    assert migrated[2].tool_call_id == "preserved-call"
    for before, after in zip(old, migrated):
        assert before.model_dump(exclude={"id"}) == after.model_dump(exclude={"id"})
    assert [row["message_id"] for row in platform.transcript("conversation-a")["rows"]] == [message.id for message in migrated]


def test_active_large_stream_uses_lazy_projection_and_keeps_native_completion(platform):
    from tests.contracts.client_platform.test_headless_lifecycle import submit
    from tests.helpers.client_platform_fakes import CheckpointCommit, ScriptedAgentStream, StreamBarrier
    text = "Synthetic 😀\n" * 180000
    barrier = StreamBarrier()
    fake = ScriptedAgentStream((("token", text), barrier,
        CheckpointCommit((AIMessage(content=text, id="large-final-native"),), "large-final-native"), ("done", text)))
    accepted = submit(platform, fake, "large-live")
    handle = platform.registry.get(accepted["execution_id"])
    try:
        assert barrier.entered.wait(20)
        snap = platform.snapshot("conversation-a")
        live = next(row for row in snap["rows"] if row["id"].startswith("assistant:live:"))
        assert live["content_status"] == "lazy"
        assert len(json.dumps(snap["rows"]).encode()) <= platform.projection.MAX_CONTENT_BYTES
        page = platform.lazy_content("conversation-a", live["content_ref"])
        assert len(base64.b64decode(page["data"])) <= 65536
    finally:
        barrier.release.set()
        assert handle.producer_done.wait(20)
    assert handle.status == "completed"
    assert any(row["message_id"] == "large-final-native" for row in platform.transcript("conversation-a")["rows"])


def test_large_block_and_tool_identity_sets_are_lazy_without_metadata_loss(platform):
    from row_bot import threads
    content = [{"type": "text", "text": f"Synthetic block {index}"} for index in range(5000)]
    calls = [{"id": f"native-call-{index}", "name": "synthetic", "args": {}} for index in range(500)]
    assert threads.append_checkpoint_messages("conversation-a", [AIMessage(content=content, tool_calls=calls, id="many-blocks")])
    row = platform.transcript("conversation-a")["rows"][0]
    assert row["content_status"] == "lazy" and row["tool_call_ids"] == []
    def read_all(reference):
        cursor, chunks = None, []
        while True:
            page = platform.lazy_content("conversation-a", reference, cursor=cursor)
            chunks.append(base64.b64decode(page["data"]))
            if not page["has_more"]:
                return json.loads(b"".join(chunks))
            cursor = page["next_cursor"]
    assert read_all(row["content_ref"]) == content
    assert read_all(row["tool_calls_ref"]) == [call["id"] for call in calls]


def test_checkpoint_container_work_and_depth_are_explicitly_bounded():
    import msgpack
    from row_bot.runtime.checkpoint_reader import BlobReader
    reader = BlobReader(io.BytesIO(msgpack.packb([None] * 1000)), "synthetic")
    reader.MAX_NODES = 32
    with pytest.raises(ValueError, match="checkpoint_read_limit"):
        reader.end(0)
    nested = None
    for _ in range(70):
        nested = [nested]
    reader = BlobReader(io.BytesIO(msgpack.packb(nested)), "synthetic")
    with pytest.raises(ValueError, match="checkpoint_format_invalid"):
        reader.end(0)


def test_multimodal_native_content_never_adopts_by_matching_only_text(platform):
    from tests.contracts.client_platform.test_headless_lifecycle import submit
    from tests.helpers.client_platform_fakes import CheckpointCommit, ScriptedAgentStream
    content = [{"type": "text", "text": "Same text"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,Zml4dHVyZQ=="}}]
    fake = ScriptedAgentStream((("token", "Same text"),
        CheckpointCommit((AIMessage(content=content, id="multimodal-native"),), "multimodal-native"), ("done", "Same text")))
    accepted = submit(platform, fake, "multimodal")
    assert platform.registry.get(accepted["execution_id"]).producer_done.wait(10)
    settled = [event for event in platform.events_since("conversation-a", "0")["events"]
               if event["type"] == "transcript.settled"]
    assert settled[-1]["payload"]["adoption"] == "no_adoption"
