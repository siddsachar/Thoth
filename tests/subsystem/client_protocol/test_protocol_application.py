from __future__ import annotations

import hashlib
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from row_bot.api.v1.routes import create_client_platform_app
from tests.helpers.client_platform_fakes import CheckpointCommit, ScriptedAgentStream, StreamBarrier
from tests.subsystem.client_protocol.test_protocol_security import bootstrap

pytestmark = pytest.mark.subsystem


@pytest.fixture
def service(tmp_path, monkeypatch):
    from row_bot import threads, tasks
    from row_bot.application.client_platform import ClientPlatformService
    from row_bot.projection.conversation import ConversationProjection
    from row_bot.runtime.executions import GenerationRuntimeRegistry
    from row_bot.tools import registry as tools

    monkeypatch.setattr(tasks, "_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(tasks, "_SCHEMA_READY_PATH", None)
    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "threads.db"))
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(threads, "_MEDIA_DIR", media)
    threads._ensure_thread_db()
    connection = sqlite3.connect(threads.DB_PATH, check_same_thread=False)
    monkeypatch.setattr(threads, "checkpointer", threads._DeletionAwareSqliteSaver(connection))
    monkeypatch.setattr(tools, "get_enabled_tools", lambda: [])
    result = ClientPlatformService()
    result.registry = GenerationRuntimeRegistry()
    result.projection = ConversationProjection(result.registry.server_epoch)
    yield result
    result.registry.shutdown()
    for handle in result.registry.active():
        assert handle.producer_done.wait(5)
    connection.close()


def _client(service):
    return TestClient(create_client_platform_app(service, choices=lambda: {"models": [], "capabilities": []}),
                      base_url="http://localhost", client=("127.0.0.1", 12345))


def _command(client, headers, kind, payload, *, target=None, revision="0", key=None, command_id=None):
    body = {"command_id": command_id or str(uuid4()), "client_session_id": headers["X-Client-Session"],
            "type": kind, "expected_revision": revision, "payload": payload}
    url = "/api/v1/conversations/commands" if target is None else f"/api/v1/conversations/{target}/commands"
    response = client.post(url, json=body, headers={**headers, "Idempotency-Key": key or str(uuid4())})
    return response


def test_real_service_duplicate_create_conflict_and_two_subscribers(service):
    from langchain_core.messages import AIMessage
    barrier = StreamBarrier()
    fake = ScriptedAgentStream((("token", "Fixture answer"), barrier,
                                CheckpointCommit((AIMessage(id="fixture-output", content="Fixture answer"),), "fixture-output"),
                                ("done", None)))
    service.stream_factory = fake.stream
    with _client(service) as client:
        _, a = bootstrap(client)
        _, b = bootstrap(client)
        key, command_id = str(uuid4()), str(uuid4())
        first = _command(client, a, "conversation.create", {"title": "Fixture"}, key=key, command_id=command_id)
        assert first.status_code == 200, first.text
        conversation = first.json()["conversation_id"]
        duplicate = _command(client, b, "conversation.create", {"title": "Fixture"}, key=key, command_id=command_id)
        assert duplicate.json() == first.json()
        subscriptions = [client.post(f"/api/v1/conversations/{conversation}/subscriptions", headers=h).json() for h in (a, b)]
        submitted = _command(client, a, "conversation.submit", {"submission_id": str(uuid4()), "text": "Fixture prompt",
                    "attachment_refs": [], "model_selection": {"provider_id": "fixture", "model_ref": "fixture::model"}}, target=conversation)
        assert submitted.status_code == 202, submitted.text
        assert barrier.entered.wait(5)
        try:
            for header, sub in zip((a, b), subscriptions):
                events = client.get("/api/v1/events/poll", headers=header,
                                    params={"subscription_id": sub["subscription_id"], "cursor": sub["cursor"]})
                assert events.status_code == 200, events.text
                assert any(item["event"]["type"] == "transcript.delta" for item in events.json()["events"])
            foreign = client.get("/api/v1/events/poll", headers=b,
                                 params={"subscription_id": subscriptions[0]["subscription_id"], "cursor": subscriptions[0]["cursor"]})
            assert foreign.status_code == 404
        finally:
            barrier.release.set()
        handle = service.registry.get(submitted.json()["execution_id"])
        assert handle.producer_done.wait(5)
        assert len(fake.calls) == 1
        transcript = client.get(f"/api/v1/conversations/{conversation}/transcript", headers=a)
        assert transcript.status_code == 200
        assert "Fixture answer" in transcript.text
        renamed = _command(client, a, "conversation.rename", {"title": "Renamed"}, target=conversation,
                           revision=service.get_conversation(conversation)["revision"])
        assert renamed.status_code == 200
        stale = _command(client, b, "conversation.rename", {"title": "Stale"}, target=conversation)
        assert stale.status_code == 409 and stale.json()["code"] == "revision_conflict"


def test_real_attachment_upload_replay_read_and_tamper(service):
    from row_bot import threads
    with _client(service) as client:
        _, headers = bootstrap(client)
        conversation = _command(client, headers, "conversation.create", {"title": "Files"}).json()["conversation_id"]
        data = b"fixture attachment"
        upload_headers = {**headers, "Idempotency-Key": str(uuid4()), "X-Command-Id": str(uuid4()),
                          "X-Content-Sha256": hashlib.sha256(data).hexdigest(), "Content-Type": "text/plain"}
        params = {"conversation_id": conversation, "name": "fixture.txt"}
        response = client.post("/api/v1/uploads", params=params, content=data, headers=upload_headers)
        assert response.status_code == 200, response.text
        assert client.post("/api/v1/uploads", params=params, content=data, headers=upload_headers).json() == response.json()
        ref = response.json()["attachment_ref"]
        assert str(threads._MEDIA_DIR) not in response.text
        read = client.get(f"/api/v1/attachments/{ref}", headers=headers)
        assert read.content == data
        assert read.headers["Content-Disposition"].startswith("attachment;")
        denied = client.get(f"/api/v1/attachments/{ref}")
        assert denied.status_code == 401
        attachment_id = ref.rsplit(":", 1)[1]
        (threads._MEDIA_DIR / conversation / f"attachment_{attachment_id}.bin").write_bytes(b"changed")
        assert client.get(f"/api/v1/attachments/{ref}", headers=headers).status_code == 409


def test_chunk_upload_out_of_order_retry_hash_and_session_scope(service):
    from row_bot.application.attachments import UPLOAD_CHUNK_BYTES
    with _client(service) as client:
        _, headers = bootstrap(client)
        _, foreign = bootstrap(client)
        conversation = _command(client, headers, "conversation.create", {"title": "Chunks"}).json()["conversation_id"]
        data = b"a" * UPLOAD_CHUNK_BYTES + b"tail"
        started = client.post("/api/v1/uploads/sessions", headers=headers, json={
            "conversation_id": conversation, "name": "fixture.txt", "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "batch_id": str(uuid4())})
        assert started.status_code == 200, started.text
        upload = started.json()["upload_id"]
        base = f"/api/v1/uploads/{upload}"
        assert client.get(base, headers=foreign).status_code == 404
        completion = {"command_id": str(uuid4())}
        commit_headers = {**headers, "Idempotency-Key": str(uuid4())}
        assert client.post(base + "/complete", headers=commit_headers, json=completion).json()["code"] == "upload_incomplete"
        tail = client.put(base + "/chunks", params={"offset": UPLOAD_CHUNK_BYTES}, headers=headers, content=b"tail")
        assert tail.status_code == 200 and tail.json()["received_bytes"] == 4
        assert client.put(base + "/chunks", params={"offset": UPLOAD_CHUNK_BYTES}, headers=headers, content=b"FAIL").status_code == 409
        assert client.put(base + "/chunks", params={"offset": 0}, headers=headers, content=data).status_code == 413
        for _ in range(2):
            piece = client.put(base + "/chunks", params={"offset": 0}, headers=headers, content=data[:UPLOAD_CHUNK_BYTES])
            assert piece.status_code == 200 and piece.json()["received_bytes"] == len(data)
        completed = client.post(base + "/complete", headers=commit_headers, json=completion)
        assert completed.status_code == 200, completed.text
        assert client.post(base + "/complete", headers=commit_headers, json=completion).json() == completed.json()
        ref = completed.json()["attachment_ref"]
        assert client.get(f"/api/v1/attachments/{ref}", headers=headers).content == data
        assert client.delete(base, headers=headers).status_code == 200
        assert client.get(base, headers=headers).status_code == 410


def test_upload_idle_expiry_restart_batch_and_inflight_limits(service):
    from row_bot.application.attachments import AttachmentUploads, AttachmentError, MAX_ATTACHMENT_BYTES
    conversation = str(uuid4())
    from row_bot import threads
    threads._save_thread_meta(conversation, "Fixture")
    now = [0.0]
    staging = AttachmentUploads(clock=lambda: now[0])
    common = {"conversation_id": conversation, "name": "fixture.bin", "size_bytes": MAX_ATTACHMENT_BYTES,
              "sha256": "a" * 64, "batch_id": str(uuid4())}
    try:
        identifiers = [staging.create("fixture-session", **common)["upload_id"] for _ in range(4)]
        with pytest.raises(AttachmentError, match="payload_too_large"):
            staging.create("fixture-session", **common)
        for _ in range(4):
            staging.enter_chunk("fixture-session", identifiers[0])
        with pytest.raises(AttachmentError, match="rate_limited"):
            staging.enter_chunk("fixture-session", identifiers[0])
        for _ in range(4):
            staging.leave_chunk("fixture-session")
        restarted = AttachmentUploads(clock=lambda: now[0])
        with pytest.raises(AttachmentError, match="upload_expired"):
            restarted.status("fixture-session", identifiers[0])
        now[0] = 1801
        with pytest.raises(AttachmentError, match="upload_expired"):
            staging.status("fixture-session", identifiers[0])
        assert not staging._uploads
    finally:
        staging.close()


def test_long_existing_conversation_id_has_bounded_opaque_attachment_reference(service):
    from row_bot import threads
    from row_bot.application.attachments import register_attachment, read_attachment
    from row_bot.api.v1.schemas import AttachmentView
    conversation = "c" * 128
    threads._save_thread_meta(conversation, "Imported")
    result = register_attachment(conversation, "fixture.txt", b"fixture")
    AttachmentView.model_validate(result)
    assert len(result["attachment_ref"]) == 165
    assert read_attachment(result["attachment_ref"])[1] == b"fixture"


def test_attachment_recovered_deletion_blocks_reference_and_staging(service, monkeypatch):
    from row_bot import threads
    from row_bot.application.attachments import register_attachment, read_attachment, AttachmentError
    from row_bot.runtime import admissions
    conversation = str(uuid4())
    threads._save_thread_meta(conversation, "Fixture")
    result = register_attachment(conversation, "fixture.txt", b"fixture")
    monkeypatch.setattr(admissions, "deletion_state", lambda _: "deleting")
    with pytest.raises(AttachmentError, match="action_denied"):
        read_attachment(result["attachment_ref"])
    with pytest.raises(AttachmentError, match="action_denied"):
        register_attachment(conversation, "late.txt", b"late")


def test_attachment_preflight_reads_only_manifest_and_file_size(service, monkeypatch):
    from row_bot import threads
    from row_bot.application import attachments
    conversation = str(uuid4())
    threads._save_thread_meta(conversation, "Fixture")
    expected = attachments.register_attachment(conversation, "fixture.mp4", b"\x00\x00\x00\x18ftypisom" + b"x" * 100)
    original = attachments._open
    reads = []

    class CheckedFile:
        def __init__(self, path):
            self.path = path
            self.source = original(threads._MEDIA_DIR, path)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.source.close()
        def fileno(self):
            return self.source.fileno()
        def read(self, length):
            assert self.path.suffix == ".json", "Metadata preflight read the attachment bytes"
            reads.append(length)
            return self.source.read(length)

    monkeypatch.setattr(attachments, "_open", lambda root, path: CheckedFile(path))
    assert attachments.inspect_attachment(expected["attachment_ref"]) == expected
    assert expected["mime_type"] == "video/mp4"
    assert reads == [8193]


@pytest.mark.parametrize("manifest", [b"[]", b"x" * 8193, b'{"private_path":"fixture"}'])
def test_attachment_preflight_and_read_reject_malformed_manifest(service, manifest):
    from row_bot import threads
    from row_bot.application import attachments
    conversation = str(uuid4())
    threads._save_thread_meta(conversation, "Fixture")
    expected = attachments.register_attachment(conversation, "fixture.bin", b"fixture")
    identifier = expected["attachment_ref"].rsplit(":", 1)[1]
    (threads._MEDIA_DIR / conversation / f"attachment_{identifier}.json").write_bytes(manifest)
    for operation in (attachments.inspect_attachment, attachments.read_attachment):
        with pytest.raises(attachments.AttachmentError, match="not_found"):
            operation(expected["attachment_ref"])


def test_real_durable_approval_resume_and_response_loss_replay(service):
    from langchain_core.messages import AIMessage
    fake = ScriptedAgentStream(
        (("interrupt", [{"__interrupt_id": "fixture-interrupt", "tool": "fixture", "args": {"secret": "private-argument"}}]),),
        (("token", "Approved result"), CheckpointCommit((AIMessage(id="approved-output", content="Approved result"),), "approved-output"),
         ("done", None)),
    )
    service.stream_factory, service.resume_factory = fake.stream, fake.resume
    with _client(service) as client:
        _, headers = bootstrap(client)
        conversation = _command(client, headers, "conversation.create", {"title": "Approvals"}).json()["conversation_id"]
        submitted = _command(client, headers, "conversation.submit", {"submission_id": str(uuid4()), "text": "Fixture",
                    "model_selection": {"provider_id": "fixture", "model_ref": "fixture::model"}}, target=conversation)
        assert submitted.status_code == 202, submitted.text
        handle = service.registry.get(submitted.json()["execution_id"])
        assert handle.producer_done.wait(5)
        view = client.get(f"/api/v1/approvals/{handle.approval_id}", headers=headers)
        assert view.status_code == 200, view.text
        assert "private-argument" not in view.text and "action_digest" not in view.text
        body = {"command_id": str(uuid4()), "client_session_id": headers["X-Client-Session"], "type": "approval.resolve",
                "expected_revision": view.json()["revision"], "payload": {"decision": "approve", "nonce": view.json()["nonce"]}}
        commit_headers = {**headers, "Idempotency-Key": str(uuid4())}
        endpoint = f"/api/v1/approvals/{handle.approval_id}/commands"
        resolved = client.post(endpoint, headers=commit_headers, json=body)
        assert resolved.status_code == 202, resolved.text
        assert service.registry.get(resolved.json()["execution_id"]).producer_done.wait(5)
        retry = client.post(endpoint, headers=commit_headers, json=body)
        assert retry.json() == resolved.json(), retry.text
        assert len(fake.calls) == 2 and fake.calls[1]["approved"] is True
        assert fake.calls[1]["interrupt_ids"] == ("fixture-interrupt",)
        contradictory = {**body, "command_id": str(uuid4()), "payload": {**body["payload"], "decision": "reject"}}
        assert client.post(endpoint, headers={**headers, "Idempotency-Key": str(uuid4())}, json=contradictory).status_code == 409
        assert len(fake.calls) == 2


def test_api_resources_bind_roles_describe_and_stale_revision(service, tmp_path, monkeypatch):
    import sys

    from row_bot.designer import storage as artifacts
    from row_bot.designer.state import DesignerProject
    from row_bot.developer import storage as workspaces
    from row_bot.developer.state import DeveloperWorkspace
    assert workspaces is sys.modules.get("row_bot.developer.storage"), "Workspace fixture uses a stale package module"
    assert artifacts is sys.modules.get("row_bot.designer.storage"), "Artifact fixture uses a stale package module"
    monkeypatch.setattr(workspaces, "DEVELOPER_DIR", tmp_path / "developer")
    monkeypatch.setattr(workspaces, "WORKSPACES_PATH", tmp_path / "developer" / "workspaces.json")
    monkeypatch.setattr(artifacts, "DESIGNER_DIR", tmp_path / "designer")
    monkeypatch.setattr(artifacts, "PROJECTS_DIR", tmp_path / "designer" / "projects")
    workspaces.save_workspace(DeveloperWorkspace(id="workspace", name="Workspace", path=str(tmp_path)))
    artifacts.save_project(DesignerProject(id="artifact", name="Artifact"))
    with _client(service) as client:
        _, headers = bootstrap(client)
        conversation = _command(client, headers, "conversation.create", {"title": "Resources"}).json()["conversation_id"]
        for revision, kind in enumerate(("workspace", "artifact")):
            bound = _command(client, headers, "conversation.bind", {"kind": kind, "resource_id": kind, "role": "primary"},
                             target=conversation, revision=str(revision))
            assert bound.status_code == 200, bound.text
        view = client.get(f"/api/v1/conversations/{conversation}", headers=headers)
        assert view.status_code == 200, view.text
        assert {r["kind"] for r in view.json()["resource_bindings"]} == {"workspace", "artifact"}
        binding = view.json()["resource_bindings"][0]
        descriptor = client.get(f"/api/v1/resources/{conversation}:{binding['binding_id']}", headers=headers)
        assert descriptor.status_code == 200 and descriptor.json()["binding"]["role"] == "primary"
        assert str(tmp_path) not in descriptor.text
        stale = _command(client, headers, "conversation.unbind", {"binding_id": binding["binding_id"]}, target=conversation)
        assert stale.status_code == 409
        current = _command(client, headers, "conversation.unbind", {"binding_id": binding["binding_id"]}, target=conversation, revision="2")
        assert current.status_code == 200, current.text
