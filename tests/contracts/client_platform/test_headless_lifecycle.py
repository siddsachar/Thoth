"""Independent acceptance barriers against real services and durable stores."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite import SqliteSaver
import pytest

from tests.fixtures.tasks import fresh_tasks_module
from tests.helpers.client_platform_fakes import CheckpointCommit, ScriptedAgentStream, StreamBarrier, fixture_id

pytestmark = pytest.mark.contract


@pytest.fixture
def platform(tmp_path, monkeypatch):
    from row_bot import threads
    from row_bot.application import client_platform
    from row_bot.projection.conversation import ConversationProjection
    from row_bot.runtime.executions import GenerationRuntimeRegistry
    from row_bot.projection import conversation as projection_owner
    from row_bot.runtime import executions as runtime_owner
    from row_bot.tools import registry as tool_registry

    fresh_tasks_module(tmp_path, monkeypatch)
    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "threads.db"))
    monkeypatch.setattr(threads, "DATA_DIR", tmp_path)
    monkeypatch.setattr(threads, "_MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(threads, "_THREAD_UI_DIR", tmp_path / "thread_ui")
    original_config = tool_registry.get_tool_config
    monkeypatch.setattr(tool_registry, "get_tool_config", lambda name, key, default=None:
                        str(tmp_path / "workspace") if (name, key) == ("filesystem", "workspace_root")
                        else original_config(name, key, default))
    threads._ensure_thread_db()
    for name in ("conversation-a", "conversation-b"):
        with sqlite3.connect(threads.DB_PATH) as connection:
            connection.execute("INSERT INTO thread_meta(thread_id,name) VALUES(?,?)", (name, name))
    registry = GenerationRuntimeRegistry()
    projection = ConversationProjection(registry.server_epoch)
    monkeypatch.setattr(client_platform, "generation_registry", registry)
    monkeypatch.setattr(client_platform, "conversation_projection", projection)
    monkeypatch.setattr(runtime_owner, "generation_registry", registry)
    monkeypatch.setattr(projection_owner, "conversation_projection", projection)
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.db")) as checkpointer:
        monkeypatch.setattr(threads, "checkpointer", checkpointer)
        service = client_platform.ClientPlatformService()
        yield service
        registry.shutdown()
        for handle in registry.active():
            assert handle.producer_done.wait(10), "Fixture left a producer running"


def command(kind: str, label: str, payload: dict | None = None, revision: str = "0") -> dict:
    return {"command_id": fixture_id(label + ":command"), "client_session_id": fixture_id("client-a"),
            "type": kind, "expected_revision": revision, "payload": payload or {}}


def submit(service, fake: ScriptedAgentStream, label: str = "submission") -> dict:
    service.stream_factory = fake.stream
    service.resume_factory = fake.resume
    return service.execute(owner_id="fixture-owner", idempotency_key=fixture_id(label + ":key"),
                           target="conversation-a", command=command("conversation.submit", label, {
                               "submission_id": fixture_id(label), "text": "Identical synthetic input",
                               "attachment_refs": [],
                               "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}}))


def test_f_p01_exact_submission_and_native_assistant_identity(platform):
    from row_bot.threads import get_latest_checkpoint_messages

    native_id = fixture_id("native-assistant")
    fake = ScriptedAgentStream((("token", "Synthetic output"),
                               CheckpointCommit((AIMessage(content="Synthetic output", id=native_id),), native_id),
                               ("done", "Synthetic output")))
    receipt = submit(platform, fake)
    handle = platform.registry.get(receipt["execution_id"])
    assert handle.producer_done.wait(10)
    rows = platform.snapshot("conversation-a")["rows"]
    assert [row["id"] for row in rows] == ["user:submission:" + fixture_id("submission"),
                                          "assistant:checkpoint:" + native_id]
    assert [message.id for message in get_latest_checkpoint_messages("conversation-a")] == [fixture_id("submission"), native_id]
    assert len(fake.calls) == 1
    assert fake.external_call_count == 0


def test_f_p03_snapshot_during_stream_keeps_public_delta_and_no_reexecution(platform):
    barrier = StreamBarrier()
    fake = ScriptedAgentStream((("token", "Visible before checkpoint"), barrier))
    receipt = submit(platform, fake, "live-snapshot")
    handle = platform.registry.get(receipt["execution_id"])
    try:
        assert barrier.entered.wait(10)
        first = platform.snapshot("conversation-a")
        second = platform.snapshot("conversation-a")
        texts = [block.get("text", "") for row in second["rows"] for block in row.get("blocks", [])]
        assert "Visible before checkpoint" in texts
        assert first["projection_revision"] == second["projection_revision"]
        assert len(fake.calls) == 1
    finally:
        platform.registry.stop("conversation-a")
        barrier.release.set()
        assert handle.producer_done.wait(10)


def test_f_r09_stop_acknowledgement_cannot_release_uninterruptible_producer(platform):
    barrier = StreamBarrier()
    fake = ScriptedAgentStream((("token", "Before blocking effect"), barrier, ("done", "Must not publish")))
    receipt = submit(platform, fake, "uninterruptible")
    handle = platform.registry.get(receipt["execution_id"])
    try:
        assert barrier.entered.wait(10)
        platform.registry.stop("conversation-a")
        assert handle.view()["status"] == "stopping"
        assert handle.view()["cancel_requested"] is True
        assert handle.view()["quiesced"] is False
        assert handle in platform.registry.active("conversation-a")
        assert not fake.quiesced.is_set()
    finally:
        barrier.release.set()
        assert handle.producer_done.wait(10)
    assert handle.view()["status"] == "stopped"
    assert not platform.registry.active("conversation-a")
    assert ("done", "Must not publish") not in fake.events


def test_interrupted_producer_never_rewritten_as_completed(platform):
    fake = ScriptedAgentStream((("error", "Synthetic failure"),))
    receipt = submit(platform, fake, "provider-error")
    handle = platform.registry.get(receipt["execution_id"])
    assert handle.producer_done.wait(10)
    assert handle.view()["status"] == "interrupted"


def test_f_p04_same_key_concurrent_different_target_is_one_immutable_binding(platform):
    barrier = threading.Barrier(2)
    request = command("conversation.rename", "same-key", {"title": "Renamed fixture"})

    def execute(target):
        from row_bot.application.client_platform import ClientPlatformError

        barrier.wait(timeout=10)
        try:
            return platform.execute(owner_id="fixture-owner", idempotency_key=fixture_id("same-key"),
                                    target=target, command=request)
        except ClientPlatformError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, ["conversation-a", "conversation-b"]))
    assert results.count("idempotency_mismatch") == 1
    assert sum(platform.get_conversation(name)["title"] == "Renamed fixture"
               for name in ("conversation-a", "conversation-b")) == 1


def test_f_p04_response_loss_replays_receipt_before_revision_check(platform):
    request = command("conversation.rename", "response-loss", {"title": "Stable rename"})
    kwargs = {"owner_id": "fixture-owner", "idempotency_key": fixture_id("response-loss:key"),
              "target": "conversation-a", "command": request}
    first = platform.execute(**kwargs)
    assert platform.get_conversation("conversation-a")["revision"] == "1"
    assert platform.execute(**kwargs) == first
    assert platform.get_conversation("conversation-a")["revision"] == "1"


def test_f_p04_owner_receipt_replay_survives_transport_session_change(platform):
    request = command("conversation.rename", "owner-response-loss", {"title": "One owner rename"})
    kwargs = {"owner_id": "fixture-owner", "idempotency_key": fixture_id("owner-response-loss:key"),
              "target": "conversation-a", "command": request}
    first = platform.execute(**kwargs)
    second_request = {**request, "client_session_id": fixture_id("client-b")}
    assert platform.execute(**{**kwargs, "command": second_request}) == first
    assert platform.get_conversation("conversation-a")["revision"] == "1"


def test_f_p05_complete_transcript_pagination_preserves_every_native_id(platform):
    from row_bot.threads import append_checkpoint_messages

    ids = [fixture_id(f"history:{index}") for index in range(1005)]
    append_checkpoint_messages("conversation-a", [HumanMessage(content=f"Synthetic history {index}", id=identity)
                                                  for index, identity in enumerate(ids)])
    seen = []
    cursor = None
    while True:
        page = platform.transcript("conversation-a", limit=73, cursor=cursor)
        assert len(page["rows"]) <= 73
        seen.extend(row["message_id"] for row in page["rows"])
        if not page["has_more"]:
            break
        assert page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    assert seen == ids


def test_f_p03_subscribers_have_independent_replay_and_expired_history_resets(platform):
    projection = platform.projection
    projection.MAX_EVENTS = 3
    before = platform.snapshot("conversation-a")
    cursor = before["cursor"]
    def delta(text):
        return {"pass_id": fixture_id("replay-pass"), "segment_id": fixture_id("replay-segment"),
                "row_id": "assistant:live:" + fixture_id("replay-pass") + ":" + fixture_id("replay-segment"),
                "render_revision": "1", "public_text_delta": text}
    for index in range(3):
        projection.publish("conversation-a", "transcript.delta", delta(str(index)))
    first = platform.events_since("conversation-a", cursor)
    second = platform.events_since("conversation-a", cursor)
    assert first == second
    assert len(first["events"]) == 3
    projection.publish("conversation-a", "transcript.delta", delta("fourth"))
    stale = platform.events_since("conversation-a", cursor)
    assert stale["snapshot_required"] is True
    assert stale["events"] == []


def test_f_u07_captured_workspace_beats_unrelated_legacy_execution_context(tmp_path, monkeypatch):
    from row_bot import conversation_resources, threads
    from row_bot.developer import storage, tool_context
    from row_bot.developer.state import DeveloperWorkspace

    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "threads.db"))
    monkeypatch.setattr(storage, "DEVELOPER_DIR", tmp_path / "developer")
    monkeypatch.setattr(storage, "WORKSPACES_PATH", tmp_path / "developer" / "workspaces.json")
    threads._ensure_thread_db()
    with sqlite3.connect(threads.DB_PATH) as connection:
        connection.execute("INSERT INTO thread_meta(thread_id,name) VALUES('bound-thread','Fixture')")
    for workspace in ("bound-workspace", "unrelated-workspace"):
        storage.save_workspace(DeveloperWorkspace(id=workspace, name=workspace, path=str(tmp_path / workspace)))
    conversation_resources.bind("bound-thread", "workspace", "bound-workspace", expected_revision=0)
    tokens = tool_context.set_context(workspace_id="unrelated-workspace", thread_id="different-thread")
    try:
        with conversation_resources.execution_context("bound-thread"):
            assert tool_context.get_workspace_id() == "bound-workspace"
        assert tool_context.get_workspace_id() == "unrelated-workspace"
    finally:
        tool_context.reset_context(tokens)
