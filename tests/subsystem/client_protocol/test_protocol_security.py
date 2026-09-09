from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from row_bot.access.config import AccessConfig, DeploymentMode
from row_bot.access.request_context import RequestContextResolver, SessionIdentity
from row_bot.api.v1.routes import create_client_platform_app
from row_bot.api.v1.schemas import Command, Event
from row_bot.api.v1.security import ClientSecurity, ProtocolError

pytestmark = pytest.mark.subsystem


class Service:
    instance_id = "fixture-instance"
    server_epoch = "fixture-epoch"

    def __init__(self):
        self.commands = []

    def execute(self, **kwargs):
        kwargs["validate"]()
        self.commands.append(kwargs)
        return {"status": "completed", "command_id": kwargs["command"]["command_id"]}

    def list_conversations(self, *args):
        return {"items": [], "has_more": False}


def client_app(*, remote=False):
    service = Service()
    active = {"value": True}
    config = AccessConfig(deployment_mode=DeploymentMode.SERVER if remote else DeploymentMode.DESKTOP)
    def authenticate(scope, provenance):
        if remote and active["value"]:
            return SessionIdentity("fixture-device", "fixture-session")
        return None
    app = create_client_platform_app(service, access_config=config, session_authenticator=authenticate,
                                     choices=lambda: {"models": [], "capabilities": [], "catalog_stale": True})
    return TestClient(app, base_url="http://localhost", client=("127.0.0.1", 12345)), service, active


def bootstrap(client):
    response = client.post("/api/v1/handshake", json={}, headers={"Origin": "http://localhost"})
    assert response.status_code == 200, response.text
    data = response.json()
    return data, {"Origin": "http://localhost", "X-Client-Session": data["client_session_id"],
                  "X-CSRF-Token": data["csrf_token"]}


def test_authenticated_command_revalidates_and_masks_errors():
    client, service, active = client_app(remote=True)
    with client:
        data, headers = bootstrap(client)
        command = {"command_id": str(uuid4()), "client_session_id": data["client_session_id"],
                   "type": "conversation.create", "expected_revision": "0", "payload": {"title": "Fixture"}}
        headers["Idempotency-Key"] = str(uuid4())
        assert client.post("/api/v1/conversations/commands", json=command, headers=headers).status_code == 200
        active["value"] = False
        response = client.get("/api/v1/conversations", headers=headers)
        assert response.status_code == 401
        assert response.json()["code"] == "authentication_required"
        assert len(service.commands) == 1


def test_query_revoked_during_store_wait_never_delivers_data():
    client, service, active = client_app(remote=True)
    def read(*args):
        active["value"] = False
        return {"items": [], "has_more": False, "next_cursor": None}
    service.list_conversations = read
    with client:
        _, headers = bootstrap(client)
        response = client.get("/api/v1/conversations", headers=headers)
        assert response.status_code == 401
        assert "items" not in response.json()


def test_choice_discovery_reads_metadata_without_plugin_construction_or_refresh(monkeypatch):
    from row_bot.api.v1.routes import cached_choices
    from row_bot.providers import model_catalog_cache, selection
    from row_bot.tools import registry as tool_registry
    from row_bot.plugins import registry as plugin_registry, state
    from row_bot.mcp_client import runtime
    monkeypatch.setattr(model_catalog_cache, "read_model_catalog_cache", lambda: SimpleNamespace(is_stale=True))
    monkeypatch.setattr(selection, "list_model_choice_options", lambda **kwargs: [{"provider_id": "fixture",
        "value": "fixture::model", "label": "Fixture", "active": True, "api_key": "PRIVATE_SENTINEL"}])
    tool = SimpleNamespace(name="fixture_tool", destructive_tool_names={"fixture_delete"},
                           as_langchain_tools=lambda: pytest.fail("Discovery constructed plugin tools"))
    monkeypatch.setattr(tool_registry, "get_all_tools", lambda: [])
    monkeypatch.setattr(plugin_registry, "get_loaded_manifests", lambda: [SimpleNamespace(id="fixture")])
    monkeypatch.setattr(plugin_registry, "get_plugin_tools", lambda _: [tool])
    monkeypatch.setattr(state, "is_plugin_enabled", lambda _: False)
    monkeypatch.setattr(runtime, "get_catalog_snapshot", lambda: {})
    monkeypatch.setattr(runtime, "discover_enabled_servers", lambda: pytest.fail("Discovery refreshed MCP"))
    result = cached_choices()
    assert result["catalog_stale"]
    assert result["capabilities"] == [{"id": "fixture_tool", "available": False,
                                       "requires_approval": True, "unavailable_reason": "unavailable"}]
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


@pytest.mark.parametrize("origin", [None, "null", "http://foreign.invalid", "http://localhost:9999"])
def test_handshake_rejects_missing_or_foreign_origin(origin):
    client, _, _ = client_app()
    with client:
        response = client.post("/api/v1/handshake", json={}, headers={"Origin": origin} if origin else {})
        assert response.status_code == 403


def test_incompatible_protocol_negotiates_update_without_session():
    client, _, _ = client_app()
    with client:
        response = client.post("/api/v1/handshake", headers={"Origin": "http://localhost"}, json={"protocol_major": 2})
        assert response.status_code == 426
        assert response.json()["recovery"] == "update_client"
        assert "client_session_id" not in response.json()


def test_no_csrf_no_command_and_no_validation_input_leak():
    client, service, _ = client_app()
    with client:
        data, headers = bootstrap(client)
        response = client.get("/api/v1/conversations", headers={"X-Client-Session": data["client_session_id"]})
        assert response.status_code == 403
        command = {"command_id": str(uuid4()), "client_session_id": data["client_session_id"],
                   "type": "conversation.create", "expected_revision": "0",
                   "payload": {"title": "fixture", "secret": "SECRET_SENTINEL"}}
        response = client.post("/api/v1/conversations/commands", json=command, headers=headers)
        assert response.status_code == 422
        assert "SECRET_SENTINEL" not in response.text
        assert not service.commands


def test_json_bound_before_decode():
    client, service, _ = client_app()
    with client:
        _, headers = bootstrap(client)
        response = client.post("/api/v1/conversations/commands", content=b"x" * (256 * 1024 + 1),
                               headers={**headers, "Content-Type": "application/json"})
        assert response.status_code == 413
        assert not service.commands


def context(*, remote=False, session_id="fixture-session"):
    scope = {"type": "http", "scheme": "http", "client": ("127.0.0.1", 1234),
             "headers": [(b"host", b"localhost")]}
    config = AccessConfig(deployment_mode=DeploymentMode.SERVER if remote else DeploymentMode.DESKTOP)
    return RequestContextResolver(config).resolve(scope, session=SessionIdentity("device", session_id) if remote else None)


def test_group_resume_cannot_choose_other_group_or_session():
    security = ClientSecurity("fixture")
    a = security.handshake(context())
    b = security.handshake(context())
    remote = security.handshake(context(remote=True))
    assert a.group_id == b.group_id
    assert remote.group_id != a.group_id
    with pytest.raises(ProtocolError):
        security.handshake(context(remote=True), group_id=a.group_id)
    with pytest.raises(ProtocolError):
        security.handshake(context(remote=True), session_id=a.id, group_id=a.group_id)
    assert security.handshake(context(remote=True), session_id=remote.id).id == remote.id


def test_cursors_ack_and_stream_ownership_are_independent():
    security = ClientSecurity("fixture")
    a, b = security.handshake(context()), security.handshake(context())
    sa, sb = security.subscribe(a, "conversation", "epoch"), security.subscribe(b, "conversation", "epoch")
    ca, cb = security.cursor(sa, "3"), security.cursor(sb, "3")
    security.acknowledge(sa, ca)
    assert sa.acknowledged == 3 and sb.acknowledged == 0
    with pytest.raises(ProtocolError):
        security.decode_cursor(sb, ca)
    with pytest.raises(ProtocolError):
        security.subscription(b, sa.id)
    assert security.decode_cursor(sb, cb) == "3"
    security.enter_stream(sa)
    with pytest.raises(ProtocolError):
        security.enter_stream(sa)
    security.leave_stream(sa)
    security.enter_stream(sa)


def test_approval_nonce_binds_session_revision_effect_and_deadline():
    now = [10.0]
    security = ClientSecurity("fixture", clock=lambda: now[0])
    a, b = security.handshake(context()), security.handshake(context())
    nonce = security.approval_nonce(a, "approval", "2", "effect", ttl=10)
    with pytest.raises(ProtocolError):
        security.consume_nonce(b, "approval", "2", "effect", nonce, "command")
    with pytest.raises(ProtocolError):
        security.consume_nonce(a, "approval", "2", "changed-effect", nonce, "command")
    security.consume_nonce(a, "approval", "2", "effect", nonce, "command")
    security.consume_nonce(a, "approval", "2", "effect", nonce, "command")
    with pytest.raises(ProtocolError):
        security.consume_nonce(a, "approval", "2", "effect", nonce, "opposite-command")
    now[0] = 21
    with pytest.raises(ProtocolError):
        security.consume_nonce(a, "approval", "2", "effect", nonce, "command")


def test_mutation_saturation_preserves_stop_reserve():
    security = ClientSecurity("fixture", clock=lambda: 10)
    current = security.handshake(context())
    for _ in range(10):
        security.rate(current, "mutation")
    with pytest.raises(ProtocolError):
        security.rate(current, "mutation")
    security.rate(current, "control")


def test_approval_response_uses_control_reserve_after_mutation_saturation():
    service = Service()
    service.get_approval = lambda _: {"id": "fixture-approval", "status": "pending", "revision": "0",
                                     "expires_at": None, "policy_revision": "1", "action_digest": "fixture-digest"}
    def missing(*args):
        raise ProtocolError("not_found", 404)
    service.receipt = missing
    security = ClientSecurity("fixture-instance", clock=lambda: 10)
    app = create_client_platform_app(service, security=security, choices=lambda: {"models": [], "capabilities": []})
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as client:
        handshake, headers = bootstrap(client)
        nonce = client.get("/api/v1/approvals/fixture-approval", headers=headers).json()["nonce"]
        current = security._sessions[handshake["client_session_id"]]
        for _ in range(10):
            security.rate(current, "mutation")
        body = {"command_id": str(uuid4()), "client_session_id": current.id, "type": "approval.resolve",
                "expected_revision": "0", "payload": {"decision": "approve", "nonce": nonce}}
        response = client.post("/api/v1/approvals/fixture-approval/commands", json=body,
                               headers={**headers, "Idempotency-Key": str(uuid4())})
        assert response.status_code == 200, response.text
        assert len(service.commands) == 1


def test_nonce_current_policy_fingerprint_revokes_same_action_without_leaking_policy():
    private_policy = {"enabled": True, "registration": 1, "private_key": "PRIVATE_SENTINEL"}
    security = ClientSecurity("fixture", policy=lambda: private_policy)
    current = security.handshake(context())
    revision = security.policy_revision
    assert revision.isdecimal() and len(revision) <= 20 and "PRIVATE_SENTINEL" not in revision
    nonce = security.approval_nonce(current, "approval", "0", "same-action")
    private_policy["enabled"] = False
    assert security.policy_revision != revision
    with pytest.raises(ProtocolError, match="approval_expired"):
        security.consume_nonce(current, "approval", "0", "same-action", nonce, "command")
    private_policy["enabled"] = True
    private_policy["registration"] = 2
    with pytest.raises(ProtocolError, match="approval_expired"):
        security.consume_nonce(current, "approval", "0", "same-action", nonce, "command")
    fresh = security.approval_nonce(current, "approval", "0", "same-action")
    security.consume_nonce(current, "approval", "0", "same-action", fresh, "command")


def test_policy_snapshot_observes_real_owner_enablement_and_registration_epochs(monkeypatch):
    from row_bot.api.v1.security import current_policy_snapshot
    from row_bot.tools import registry as tool_registry
    from row_bot.plugins import registry as plugin_registry
    from row_bot.mcp_client import runtime
    monkeypatch.setattr(tool_registry, "get_all_tools", lambda: [SimpleNamespace(name="fixture", destructive_tool_names={"delete"})])
    enabled = [True]
    monkeypatch.setattr(tool_registry, "is_enabled", lambda _: enabled[0])
    monkeypatch.setattr(tool_registry, "_load_global_config", lambda: None)
    monkeypatch.setattr(tool_registry, "_global_config", {"fixture_policy": True})
    monkeypatch.setattr(plugin_registry, "get_loaded_manifests", lambda: [])
    monkeypatch.setattr(runtime, "_get_effective_config", lambda: {"enabled": True, "servers": {}})
    monkeypatch.setattr(runtime, "_servers", {"fixture": object()})
    monkeypatch.setattr(runtime, "_catalog", {})
    security = ClientSecurity("fixture", policy=current_policy_snapshot)
    before = security.policy_revision
    enabled[0] = False
    after = security.policy_revision
    assert before != after
    runtime._servers["fixture"] = object()
    assert security.policy_revision != after
    after = security.policy_revision
    tool_registry._global_config["fixture_policy"] = False
    assert security.policy_revision != after


def test_cookie_scheme_and_instance_are_not_interchangeable():
    from row_bot.access.cookies import AccessCookieManager
    first, second = AccessCookieManager("fixture-a"), AccessCookieManager("fixture-b")
    insecure = f"{first.names.http}=insecure"
    assert first.extract(insecure, context="https") == ""
    assert second.extract(insecure, context="http") == ""
    both = insecure + f"; {first.names.https}=secure"
    assert first.extract(both, context="https") == "secure"


def test_command_and_event_schema_are_closed_tagged_unions():
    for cls in (Command, Event):
        schema = cls.model_json_schema()
        assert schema["discriminator"]["propertyName"] == "type"
        assert schema["oneOf"]
        for branch in schema["oneOf"]:
            variant = schema["$defs"][branch["$ref"].split("/")[-1]]
            assert variant["additionalProperties"] is False
            payload = variant["properties"]["payload"]
            assert "$ref" in payload
