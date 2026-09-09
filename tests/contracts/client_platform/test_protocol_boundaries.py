"""Protocol routes exercised with the real service, projection and task store."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest

from row_bot.access.config import AccessConfig, DeploymentMode
from row_bot.api.v1.routes import create_client_platform_app
from row_bot.api.v1.schemas import Event
from row_bot.api.v1.security import ClientSecurity, current_policy_snapshot
from tests.contracts.client_platform.test_headless_lifecycle import command, platform as platform
from tests.helpers.client_platform_fakes import FakeMonotonicClock, ScriptedAgentStream, StreamBarrier, fixture_id

pytestmark = pytest.mark.contract


def test_discovery_reads_metadata_without_constructing_tool_adapters(monkeypatch):
    from types import SimpleNamespace
    from row_bot.api.v1.routes import cached_choices
    from row_bot.providers import model_catalog_cache, selection
    from row_bot.tools import registry as tool_registry
    from row_bot.plugins import registry as plugin_registry, state as plugin_state
    from row_bot.mcp_client import runtime as mcp_runtime

    def forbidden_construction():
        raise AssertionError("Discovery must not construct an executable tool adapter")

    native = SimpleNamespace(name="synthetic_native", destructive_tool_names=(),
                             as_langchain_tools=forbidden_construction)
    plugin = SimpleNamespace(name="synthetic_plugin", destructive_tool_names=("synthetic_write",),
                             as_langchain_tools=forbidden_construction)
    monkeypatch.setattr(model_catalog_cache, "read_model_catalog_cache", lambda: SimpleNamespace(is_stale=True))
    monkeypatch.setattr(selection, "list_model_choice_options", lambda **kwargs: [
        {"provider_id": "fixture", "value": "model:fixture:synthetic", "label": "Synthetic", "active": False}])
    monkeypatch.setattr(tool_registry, "get_all_tools", lambda: [native])
    monkeypatch.setattr(tool_registry, "is_enabled", lambda name: True)
    monkeypatch.setattr(plugin_registry, "get_loaded_manifests", lambda: [SimpleNamespace(id="synthetic")])
    monkeypatch.setattr(plugin_registry, "get_plugin_tools", lambda name: [plugin])
    monkeypatch.setattr(plugin_state, "is_plugin_enabled", lambda name: True)
    monkeypatch.setattr(mcp_runtime, "get_catalog_snapshot", lambda: {"synthetic": [
        {"prefixed_name": "synthetic_mcp", "enabled": False, "requires_approval": True}]})
    result = cached_choices()
    assert result["catalog_stale"]
    assert not result["models"][0]["available"]
    assert {row["id"] for row in result["capabilities"]} == {"synthetic_native", "synthetic_plugin", "synthetic_mcp"}
    assert next(row for row in result["capabilities"] if row["id"] == "synthetic_plugin")["requires_approval"]


@pytest.fixture
def protocol_clock():
    return FakeMonotonicClock()


@pytest.fixture
def client(platform, protocol_clock):
    app = create_client_platform_app(platform, access_config=AccessConfig(deployment_mode=DeploymentMode.DESKTOP),
                                    security=ClientSecurity(platform.instance_id, clock=protocol_clock,
                                                            policy=current_policy_snapshot),
                                    choices=lambda: {"models": [], "capabilities": [], "catalog_stale": False})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 18181)) as client:
        response = client.post("/api/v1/handshake", json={}, headers={"Origin": "http://localhost"})
        assert response.status_code == 200, response.text
        handshake = response.json()
        client.headers.update({"Origin": "http://localhost", "X-Client-Session": handshake["client_session_id"],
                               "X-CSRF-Token": handshake["csrf_token"]})
        yield client


def test_f_p03_snapshot_cut_covers_event_between_capture_and_response(platform, client, monkeypatch):
    original = platform.snapshot
    captured = []

    def snapshot_then_event(conversation):
        snapshot = original(conversation)
        captured.append(snapshot)
        platform.projection.publish(conversation, "generation.activity", {"state": "thinking"})
        return snapshot

    monkeypatch.setattr(platform, "snapshot", snapshot_then_event)
    response = client.post("/api/v1/conversations/conversation-a/subscriptions")
    assert response.status_code == 200, response.text
    subscription = response.json()
    response = client.get("/api/v1/events/poll", params={"subscription_id": subscription["subscription_id"],
                                                       "cursor": subscription["cursor"]})
    assert response.status_code == 200, response.text
    suffix = response.json()
    assert not suffix["snapshot_required"]
    assert len(suffix["events"]) == 1
    assert int(suffix["events"][0]["event"]["projection_revision"]) > int(captured[0]["projection_revision"])
    Event.model_validate(suffix["events"][0]["event"])
    repeat = client.get("/api/v1/events/poll", params={"subscription_id": subscription["subscription_id"],
                                                     "cursor": subscription["cursor"]})
    assert repeat.json() == suffix


def test_f_p03_epoch_change_returns_snapshot_with_new_bound_cursor(platform, client):
    subscription = client.post("/api/v1/conversations/conversation-a/subscriptions").json()
    platform.projection.server_epoch = fixture_id("restarted-epoch")
    response = client.get("/api/v1/events/poll", params={"subscription_id": subscription["subscription_id"],
                                                       "cursor": subscription["cursor"]})
    assert response.status_code == 200, response.text
    reset = response.json()
    assert reset["snapshot_required"] is True
    assert reset["snapshot"]["server_epoch"] == fixture_id("restarted-epoch")
    assert reset["snapshot"]["cursor"] == reset["cursor"] != subscription["cursor"]


def test_f_p04_http_command_replay_uses_actual_durable_receipt(platform, client):
    request = command("conversation.rename", "http-rename", {"title": "Durable HTTP rename"})
    request["client_session_id"] = client.headers["X-Client-Session"]
    headers = {"Idempotency-Key": fixture_id("http-rename:key")}
    path = "/api/v1/conversations/conversation-a/commands"
    first = client.post(path, json=request, headers=headers)
    second = client.post(path, json=request, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    receipt = client.get("/api/v1/commands/" + request["command_id"])
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["revision"] == "1"
    assert platform.get_conversation("conversation-a")["revision"] == "1"


@pytest.mark.parametrize("origin", ["", "null", "http://foreign.invalid"])
def test_f_s01_mutations_reject_missing_null_and_foreign_origin_even_with_csrf(client, origin):
    request = command("conversation.rename", "bad-origin", {"title": "Must stay absent"})
    request["client_session_id"] = client.headers["X-Client-Session"]
    client.headers.pop("Origin")
    response = client.post("/api/v1/conversations/conversation-a/commands", json=request,
                           headers={"Idempotency-Key": fixture_id("bad-origin:key"), **({"Origin": origin} if origin else {})})
    assert response.status_code == 403
    assert response.json()["code"] == "origin_rejected"


def test_f_p02_large_unicode_delta_remains_valid_wire_or_explicit_snapshot_reset(platform, client):
    barrier = StreamBarrier()
    fake = ScriptedAgentStream((("token", "\U0001f600" * 17000), barrier))
    platform.stream_factory = fake.stream
    subscription = client.post("/api/v1/conversations/conversation-a/subscriptions").json()
    request = command("conversation.submit", "unicode-stream", {
        "submission_id": fixture_id("unicode-input"), "text": "Synthetic Unicode stream",
        "attachment_refs": [], "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}})
    request["client_session_id"] = client.headers["X-Client-Session"]
    accepted = client.post("/api/v1/conversations/conversation-a/commands", json=request,
                           headers={"Idempotency-Key": fixture_id("unicode-stream:key")})
    assert accepted.status_code == 202, accepted.text
    handle = platform.registry.get(accepted.json()["execution_id"])
    try:
        assert barrier.entered.wait(10)
        response = client.get("/api/v1/events/poll", params={"subscription_id": subscription["subscription_id"],
                                                           "cursor": subscription["cursor"]})
        assert response.status_code == 200, response.text
        output = response.json()
        assert output["snapshot_required"] or output["events"]
        for wrapped in output["events"]:
            Event.model_validate(wrapped["event"])
            assert len(json.dumps(wrapped["event"], ensure_ascii=False).encode()) <= 65536
        rows = (output["snapshot"] if output["snapshot_required"] else platform.snapshot("conversation-a"))["rows"]
        assert any(block.get("text") == "\U0001f600" * 17000 for row in rows for block in row.get("blocks", []))
    finally:
        platform.registry.stop("conversation-a")
        barrier.release.set()
        assert handle.producer_done.wait(10)


def test_f_p07_registered_attachment_sniffs_mime_and_never_exposes_path(platform, client):
    response = client.post("/api/v1/uploads", params={"conversation_id": "conversation-a", "name": "fixture.html"},
                           content=b"<html>synthetic untrusted content</html>",
                           headers={"Content-Type": "text/html", "Idempotency-Key": fixture_id("upload:key"),
                                    "X-Command-ID": fixture_id("upload:command")})
    assert response.status_code == 200, response.text
    result = response.json()
    reference = result.get("attachment_ref") or result["attachment"]["attachment_ref"]
    download = client.get("/api/v1/attachments/" + reference)
    assert download.status_code == 200, download.text
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert b"synthetic untrusted content" in download.content
    assert "path" not in json.dumps(result).lower()
