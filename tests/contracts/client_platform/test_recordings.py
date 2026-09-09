"""Portable v1 recordings are produced by real services and validated schemas.

Regeneration is opt-in through ROW_BOT_RECORD_PROTOCOL_FIXTURES=1. Normal test
runs consume the existing records; they never update an expected result.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage
import pytest

from tests.contracts.client_platform.test_headless_lifecycle import command, platform as platform
from tests.contracts.client_platform.test_protocol_boundaries import client as client, protocol_clock as protocol_clock
from tests.helpers.client_platform_fakes import CheckpointCommit, RecordedProtocolTrace, ScriptedAgentStream, StreamBarrier, ToolMediaResult, fixture_id

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.contract


def finish(trace, *, snapshot=None, delivery=None):
    document = trace.document(ROOT, final_snapshot=snapshot, delivery_order=delivery)
    trace.validate(ROOT, document)
    if os.environ.get("ROW_BOT_RECORD_PROTOCOL_FIXTURES") == "1":
        trace.write(ROOT, document)
    path = ROOT / "contracts/client-platform/v1/fixtures" / (trace.fixture + ".json")
    assert path.is_file(), "The versioned fixture has not yet been recorded"
    saved = json.loads(path.read_text(encoding="utf-8"))
    trace.validate(ROOT, saved)
    # A recording pins implementation fingerprints at its recording cut. Later
    # source hashes differ legitimately while semantic compatibility must hold.
    def comparable(doc):
        return {key: value for key, value in doc.items() if key != "source"}
    def mismatch(left, right, pointer=""):
        if isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                return pointer + ": different object fields"
            for key in left:
                result = mismatch(left[key], right[key], pointer + "/" + key)
                if result:
                    return result
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return f"{pointer}: lengths {len(left)} != {len(right)}"
            for index, (old, new) in enumerate(zip(left, right, strict=True)):
                result = mismatch(old, new, pointer + "/" + str(index))
                if result:
                    return result
        elif left != right:
            return f"{pointer}: {left!r} != {right!r}"
        return ""
    difference = mismatch(comparable(saved), comparable(document))
    assert not difference, difference


def post_command(client, trace, kind, label, payload=None, *, revision="0", target="conversation-a"):
    body = command(kind, label, payload, revision)
    body["client_session_id"] = client.headers["X-Client-Session"]
    trace.record("Command", body)
    response = client.post(f"/api/v1/conversations/{target}/commands", json=body,
                           headers={"Idempotency-Key": fixture_id(label + ":key")})
    trace.record("CommandReceipt" if response.is_success else "Problem", response.json())
    return response


def subscribe(client, trace, conversation="conversation-a"):
    response = client.post(f"/api/v1/conversations/{conversation}/subscriptions")
    assert response.status_code == 200, response.text
    value = response.json()
    trace.record("SubscriptionView", value)
    return value


def poll(client, trace, subscription):
    response = client.get("/api/v1/events/poll", params={"subscription_id": subscription["subscription_id"],
                                                       "cursor": subscription["cursor"]})
    assert response.status_code == 200, response.text
    value = response.json()
    trace.record("EventPage", value)
    for item in value["events"]:
        trace.record("Event", item["event"])
    return value


def generate(platform, client, trace, label, *, steps=None):
    native_id = fixture_id(label + ":native")
    fake = ScriptedAgentStream(steps or (("thinking", None), ("token", "Synthetic " + label),
        CheckpointCommit((AIMessage(content="Synthetic " + label, id=native_id),), native_id),
        ("done", "Synthetic " + label)))
    platform.stream_factory = fake.stream
    response = post_command(client, trace, "conversation.submit", label, {
        "submission_id": fixture_id(label), "text": "Synthetic prompt " + label,
        "attachment_refs": [], "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}})
    assert response.status_code == 202, response.text
    handle = platform.registry.get(response.json()["execution_id"])
    assert handle.producer_done.wait(10)
    assert handle.status == "completed"
    assert len(fake.calls) == 1
    return fake


def test_record_f_p01_normal_native_identity(platform, client):
    trace = RecordedProtocolTrace("F-P01")
    subscription = subscribe(client, trace)
    generate(platform, client, trace, "normal-output")
    poll(client, trace, subscription)
    tool_id, tool_ai_id, result_id, final_id = map(fixture_id, (
        "approval-tool", "approval-tool-ai", "approval-tool-result", "approval-final-ai"))
    fake = ScriptedAgentStream((
        CheckpointCommit((AIMessage(content="", id=tool_ai_id,
            tool_calls=[{"id": tool_id, "name": "fixture_action", "args": {}}]),), tool_ai_id),
        ("tool_call", {"id": tool_id, "message_id": tool_ai_id}),
        ("interrupt", [{"__interrupt_id": fixture_id("approval-interrupt"), "tool": "fixture_action",
                        "description": "Synthetic fixture action requires approval", "args": {}}]),
    ), (
        CheckpointCommit((ToolMessage(content="Synthetic tool result", id=result_id, tool_call_id=tool_id),)),
        ("tool_done", {"id": tool_id, "message_id": tool_ai_id}),
        ("token", "Synthetic approved completion"),
        CheckpointCommit((AIMessage(content="Synthetic approved completion", id=final_id),), final_id),
        ("done", "Synthetic approved completion"),
    ))
    platform.stream_factory, platform.resume_factory = fake.stream, fake.resume
    pending = post_command(client, trace, "conversation.submit", "approval-output", {
        "submission_id": fixture_id("approval-output"), "text": "Synthetic approval request",
        "attachment_refs": [], "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}})
    assert pending.status_code == 202, pending.text
    paused = platform.registry.get(pending.json()["execution_id"])
    assert paused.producer_done.wait(10) and paused.status == "waiting_approval"
    approval = client.get("/api/v1/approvals/" + paused.approval_id)
    assert approval.status_code == 200, approval.text
    trace.record("ApprovalView", approval.json())
    poll(client, trace, subscription)
    resolution = command("approval.resolve", "approve-action", {"decision": "approve", "nonce": approval.json()["nonce"]})
    resolution["client_session_id"] = client.headers["X-Client-Session"]
    trace.record("Command", resolution)
    resumed = client.post(f"/api/v1/approvals/{paused.approval_id}/commands", json=resolution,
                          headers={"Idempotency-Key": fixture_id("approve-action:key")})
    assert resumed.status_code == 202, resumed.text
    trace.record("CommandReceipt", resumed.json())
    settled = platform.registry.get(resumed.json()["execution_id"])
    assert settled.producer_done.wait(10) and settled.status == "completed"
    assert [call["kind"] for call in fake.calls] == ["submit", "resume"]
    assert fake.calls[1]["approved"] is True
    assert fake.calls[1]["interrupt_ids"] == (fixture_id("approval-interrupt"),)
    poll(client, trace, subscription)
    snapshot = platform.snapshot("conversation-a")
    assert len([row for row in snapshot["rows"] if row["role"] == "assistant"]) == 3
    assert len([row for row in snapshot["rows"] if row["role"] == "user"]) == 2
    assert [row for row in snapshot["rows"] if row["message_id"] == tool_ai_id][0]["tool_call_ids"] == [tool_id]
    trace.record("Snapshot", snapshot)
    trace.assertions = ["Every exact native AI has one root including tool-only AI", "Durable approval with nonce resumes the exact interrupt once", "No provider replay on snapshot"]
    trace.barriers = ["native checkpoint commit before explicit output binding", "durable approval pauses before action", "approved continuation resumes the same interrupt identity"]
    finish(trace, snapshot=snapshot)


def test_record_f_p02_duplicate_reordered_event_delivery(platform, client, monkeypatch):
    from row_bot import agent_runs
    trace = RecordedProtocolTrace("F-P02")
    subscription = subscribe(client, trace)
    generate(platform, client, trace, "reordered-output")
    agent_runs.create_agent_run(run_id=fixture_id("recorded-child"), thread_id="synthetic-child",
                               parent_thread_id="conversation-a", status="queued")
    agent_runs.update_agent_status(fixture_id("recorded-child"), "running")
    agent_runs.update_agent_status(fixture_id("recorded-child"), "completed")
    value = poll(client, trace, subscription)
    assert {item["event"]["type"] for item in value["events"]} >= {"agent.activity", "queue.updated"}
    sequences = [int(item["event"]["source_sequence_start"]) for item in value["events"]]
    assert sequences == sorted(set(sequences))
    # Pressure comes from two real admitted fake producers. Public reset is
    # emitted by the production budget owner; the recorder never invents it.
    second_subscription = subscribe(client, trace, "conversation-b")
    monkeypatch.setattr(platform.projection, "MAX_GLOBAL_CONTENT_BYTES", 8192)
    monkeypatch.setattr(platform.projection, "MAX_CONTENT_BYTES", 8192)
    barriers = [StreamBarrier(), StreamBarrier()]
    text = "Synthetic " * 775
    fake = ScriptedAgentStream(*[
        (("token", text), barrier,
         CheckpointCommit((AIMessage(content=text, id=fixture_id(f"pressure-native-{index}")),),
                          fixture_id(f"pressure-native-{index}")), ("done", text))
        for index, barrier in enumerate(barriers)])
    platform.stream_factory = fake.stream
    handles = []
    try:
        for index, target in enumerate(("conversation-b", "conversation-a")):
            response = post_command(client, trace, "conversation.submit", f"pressure-{index}", {
                "submission_id": fixture_id(f"pressure-input-{index}"), "text": "Synthetic pressure",
                "attachment_refs": [], "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}},
                revision=platform.get_conversation(target)["revision"], target=target)
            assert response.status_code == 202, response.text
            handles.append(platform.registry.get(response.json()["execution_id"]))
            assert barriers[index].entered.wait(10)
        pressure = poll(client, trace, second_subscription)
        first_pressure = poll(client, trace, subscription)
        assert any(item["event"]["type"] == "projection.reset" for item in [*pressure["events"], *first_pressure["events"]]), {
            target: {"bytes": state.content_bytes, "events": [item[0]["type"] for item in state.events]}
            for target, state in platform.projection._states.items()}
    finally:
        for index, handle in enumerate(handles):
            barriers[index].release.set()
            assert handle.producer_done.wait(10)
    assert all(handle.status == "completed" for handle in handles)
    unique_events = {}
    for index, record in enumerate(trace.records):
        if record["schema"] == "Event":
            unique_events.setdefault(record["value"]["event_id"], index)
    indexes = list(unique_events.values())
    delivery = [*reversed(indexes), *indexes]
    assert len({trace.records[index]["value"]["event_id"] for index in delivery}) == len(indexes)
    trace.assertions = ["Duplicate event IDs are the same semantic event", "Reordered delivery requires sequence reconciliation before application"]
    trace.barriers = ["duplicated reversed delivery after producer quiescence"]
    finish(trace, snapshot=platform.snapshot("conversation-a"), delivery=delivery)


def test_record_f_p03_epoch_reset_is_same_cut_snapshot(platform, client):
    trace = RecordedProtocolTrace("F-P03")
    subscription = subscribe(client, trace)
    generate(platform, client, trace, "reconnect-output")
    first = poll(client, trace, subscription)
    repeat = poll(client, trace, subscription)
    assert repeat == first
    platform.projection.server_epoch = fixture_id("F-P03:next-epoch")
    reset = poll(client, trace, subscription)
    assert reset["snapshot_required"] and reset["snapshot"]["cursor"] == reset["cursor"]
    trace.assertions = ["Same cursor replays the same events without provider dispatch", "Epoch reset snapshot and cursor share a cut"]
    trace.barriers = ["epoch changed after durable final checkpoint"]
    finish(trace, snapshot=reset["snapshot"])


def test_record_f_p04_response_loss_and_stale_revision(platform, client):
    trace = RecordedProtocolTrace("F-P04")
    first = post_command(client, trace, "conversation.rename", "response-loss", {"title": "Synthetic renamed"})
    repeat = post_command(client, trace, "conversation.rename", "response-loss", {"title": "Synthetic renamed"})
    stale = post_command(client, trace, "conversation.rename", "stale-write", {"title": "Must stay absent"})
    assert first.status_code == repeat.status_code == 200
    assert repeat.json() == first.json()
    assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"
    view = platform.get_conversation("conversation-a")
    assert view["title"] == "Synthetic renamed" and view["revision"] == "1"
    trace.record("ConversationView", view)
    trace.assertions = ["Same key returns the immutable durable receipt", "Another stale mutation does not change the title"]
    trace.barriers = ["response loss after rename commit before caller acknowledgement"]
    finish(trace)


def test_record_f_p05_complete_long_history(platform):
    from row_bot.threads import append_checkpoint_messages
    trace = RecordedProtocolTrace("F-P05")
    expected = [fixture_id(f"long-native:{index}") for index in range(1005)]
    assert append_checkpoint_messages("conversation-a", [AIMessage(content=f"Synthetic row {index}", id=identity)
                                                         for index, identity in enumerate(expected)])
    cursor, seen = None, []
    while True:
        page = platform.transcript("conversation-a", limit=100, cursor=cursor)
        trace.record("TranscriptPage", page)
        seen.extend(row["message_id"] for row in page["rows"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert seen == expected
    trace.assertions = ["All 1005 native message IDs are traversed exactly once", "Each cursor pins one checkpoint revision"]
    trace.barriers = ["durable history exceeds both materialized and page row bounds"]
    finish(trace)


def test_record_f_p06_supported_and_unsupported_minor(client):
    trace = RecordedProtocolTrace("F-P06")
    supported = client.post("/api/v1/handshake", json={"protocol_major": 1, "minimum_minor": 0, "maximum_minor": 0})
    assert supported.status_code == 200, supported.text
    trace.record("HandshakeView", supported.json())
    unsupported = client.post("/api/v1/handshake", json={"protocol_major": 1, "minimum_minor": 1, "maximum_minor": 1})
    assert unsupported.status_code == 426, unsupported.text
    trace.record("Problem", unsupported.json())
    trace.assertions = ["1.0 is the first released minor; no fabricated older release", "Unsupported required minor returns update-required failure"]
    finish(trace)


def test_record_f_p07_upload_chunks_and_authenticated_reference(platform, client, protocol_clock):
    trace = RecordedProtocolTrace("F-P07")
    data = b"z" * 1048576 + b"safe!"
    body = {"conversation_id": "conversation-a", "name": "synthetic.bin", "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "batch_id": fixture_id("upload-batch")}
    trace.record("UploadRequest", body)
    created = client.post("/api/v1/uploads/sessions", json=body)
    assert created.status_code == 200, created.text
    upload = created.json()
    assert upload["expires_in_seconds"] == 1800
    trace.record("UploadView", upload)
    for offset in (0, 1048576):
        # The recording must not depend on the host's monotonic clock resolution.
        # Idle time decreases the remaining TTL; each chunk renews it exactly.
        protocol_clock.advance(2.5)
        status = client.get(f"/api/v1/uploads/{upload['upload_id']}")
        assert status.status_code == 200, status.text
        assert status.json()["expires_in_seconds"] == 1797
        chunk = client.put(f"/api/v1/uploads/{upload['upload_id']}/chunks", params={"offset": offset},
                           content=data[offset:offset + 1048576], headers={"Content-Type": "application/octet-stream"})
        assert chunk.status_code == 200, chunk.text
        assert chunk.json()["expires_in_seconds"] == 1800
        trace.record("UploadView", chunk.json())
    completed = client.post(f"/api/v1/uploads/{upload['upload_id']}/complete",
                            json={"command_id": fixture_id("upload-complete")},
                            headers={"Idempotency-Key": fixture_id("upload-complete:key")})
    assert completed.status_code == 200, completed.text
    reference = completed.json()
    trace.record("AttachmentView", reference)
    downloaded = client.get("/api/v1/attachments/" + reference["attachment_ref"])
    assert downloaded.status_code == 200 and downloaded.content == data
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    subscription = subscribe(client, trace)
    native_id, tool_id, issuing_id, bad_tool_id = map(fixture_id,
        ("media-native", "media-tool", "media-issuing-ai", "media-invalid-tool"))
    image = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j/a8AAAAASUVORK5CYII="
    generate(platform, client, trace, "generated-media", steps=(
        CheckpointCommit((AIMessage(content="", id=issuing_id,
            tool_calls=[{"id": tool_id, "name": "fixture_generate_image", "args": {}},
                        {"id": bad_tool_id, "name": "fixture_invalid_image", "args": {}}]),), issuing_id),
        ("tool_call", {"id": tool_id, "message_id": issuing_id}),
        ToolMediaResult(issuing_id, tool_id, image),
        ("tool_call", {"id": bad_tool_id, "message_id": issuing_id}),
        ToolMediaResult(issuing_id, bad_tool_id, "synthetic-invalid-base64!"),
        CheckpointCommit((ToolMessage(content="Synthetic image generated", id=fixture_id("media-tool-result"),
                                      tool_call_id=tool_id),
                          ToolMessage(content="Synthetic invalid image", id=fixture_id("media-invalid-result"),
                                      tool_call_id=bad_tool_id))),
        ("token", "Synthetic media ready"),
        CheckpointCommit((AIMessage(content="Synthetic media ready", id=native_id),), native_id),
        ("done", "Synthetic media ready"),
    ))
    value = poll(client, trace, subscription)
    media = [item["event"]["payload"] for item in value["events"] if item["event"]["type"] == "media.available"]
    assert len(media) == 1 and media[0]["tool_call_id"] == tool_id and media[0]["message_id"] == issuing_id
    failures = [item["event"]["payload"] for item in value["events"] if item["event"]["type"] == "media.error"]
    assert failures == [{"code": "media_unavailable", "tool_call_id": bad_tool_id, "message_id": issuing_id}]
    image_download = client.get("/api/v1/attachments/" + media[0]["media_ref"])
    assert image_download.status_code == 200 and image_download.headers["content-type"].startswith("image/png")
    trace.assertions = ["Two bounded chunks commit only after exact size and hash", "Authenticated opaque reference returns exact staged bytes"]
    trace.barriers = ["first complete chunk before final partial chunk"]
    finish(trace)


def test_record_f_p08_server_issued_local_group_and_spoof_rejection(platform, client, tmp_path, monkeypatch):
    trace = RecordedProtocolTrace("F-P08")
    first = client.post("/api/v1/handshake", json={}).json()
    second = client.post("/api/v1/handshake", json={}).json()
    trace.record("HandshakeView", first)
    trace.record("HandshakeView", second)
    assert first["client_session_id"] != second["client_session_id"]
    assert first["client_group_id"] == second["client_group_id"]
    spoof = client.post("/api/v1/handshake", json={"client_group_id": fixture_id("unissued-group")})
    assert spoof.status_code == 403
    trace.record("Problem", spoof.json())
    from row_bot.developer import storage
    from row_bot.developer.state import DeveloperWorkspace
    monkeypatch.setattr(storage, "DEVELOPER_DIR", tmp_path / "developer")
    monkeypatch.setattr(storage, "WORKSPACES_PATH", tmp_path / "developer" / "workspaces.json")
    storage.save_workspace(DeveloperWorkspace(id="recorded-workspace", name="Synthetic workspace", path=str(tmp_path / "workspace")))
    subscription = subscribe(client, trace)
    bound = post_command(client, trace, "conversation.bind", "resource-binding", {
        "kind": "workspace", "resource_id": "recorded-workspace", "role": "context"})
    assert bound.status_code == 200, bound.text
    value = poll(client, trace, subscription)
    assert any(item["event"]["type"] == "resource.changed" for item in value["events"])
    trace.record("ConversationView", platform.get_conversation("conversation-a"))
    trace.assertions = ["Trusted local windows share the server-issued intent group", "A caller cannot choose an enrollment group"]
    finish(trace)


def test_record_f_p09_stop_is_not_quiescence(platform, client):
    trace = RecordedProtocolTrace("F-P09")
    barrier = StreamBarrier()
    fake = ScriptedAgentStream((("token", "Synthetic blocked output"), barrier))
    platform.stream_factory = fake.stream
    response = post_command(client, trace, "conversation.submit", "stop-output", {
        "submission_id": fixture_id("stop-output"), "text": "Synthetic blocking request", "attachment_refs": [],
        "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}})
    assert response.status_code == 202, response.text
    handle = platform.registry.get(response.json()["execution_id"])
    try:
        assert barrier.entered.wait(10)
        stopped = post_command(client, trace, "conversation.stop", "stop-now")
        assert stopped.status_code == 200, stopped.text
        assert handle.status == "stopping" and not handle.producer_done.is_set()
        trace.record("Snapshot", platform.snapshot("conversation-a"))
    finally:
        barrier.release.set()
        assert handle.producer_done.wait(10)
    assert handle.status == "stopped"
    snapshot = platform.snapshot("conversation-a")
    trace.record("Snapshot", snapshot)
    deleted = post_command(client, trace, "conversation.delete", "delete-after-stop")
    replay = post_command(client, trace, "conversation.delete", "delete-after-stop")
    assert deleted.status_code == replay.status_code == 200
    assert deleted.json()["status"] == "DeleteCompleted" and deleted.json() == replay.json()
    missing = client.get("/api/v1/conversations/conversation-a")
    assert missing.status_code == 404
    trace.record("Problem", missing.json())
    subscription = subscribe(client, trace, "conversation-b")
    platform.stream_factory = ScriptedAgentStream((("error", "Synthetic provider failed"),)).stream
    failed = post_command(client, trace, "conversation.submit", "failed-output", {
        "submission_id": fixture_id("failed-output"), "text": "Synthetic failing request", "attachment_refs": [],
        "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}}, target="conversation-b")
    assert failed.status_code == 202, failed.text
    failed_handle = platform.registry.get(failed.json()["execution_id"])
    assert failed_handle.producer_done.wait(10) and failed_handle.status == "interrupted"
    events = poll(client, trace, subscription)
    assert any(item["event"]["type"] == "generation.error" for item in events["events"])
    trace.assertions = ["Cancellation receipt precedes producer acknowledgement", "Only actual producer exit permits stopped/quiesced state", "Delete receipt survives response-loss replay; deleted content is unavailable", "Failed generation stays interrupted and has a safe public error"]
    trace.barriers = ["uninterruptible provider blocked after cancellation until explicit release", "delete after producer quiescence before receipt replay"]
    finish(trace, snapshot=platform.snapshot("conversation-b"))


def test_record_f_p10_durable_index_and_delivery_truth_contract():
    trace = RecordedProtocolTrace("F-P10", kind="contract_only")
    trace.record("Outcome", {"mutation_status": "committed", "projection_status": "pending", "external_outcome": "not_applicable"})
    trace.record("Outcome", {"mutation_status": "committed", "projection_status": "degraded", "external_outcome": "uncertain"})
    trace.assertions = ["Committed data does not imply ready search index", "Uncertain external delivery does not claim sent or trigger replay", "Phase 4 storage/channel failure injection remains separately gated"]
    finish(trace)
