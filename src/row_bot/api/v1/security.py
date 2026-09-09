"""Bounded presentation sessions, nonce proofs and authenticated replay cursors.

This is ephemeral transport state. AccessService remains the credential owner;
the application runtime owns admissions, approvals and the replay journal.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import secrets
import threading
import time
import sys
from typing import Callable
from uuid import uuid4

from row_bot.access.request_context import AccessContext


def current_policy_snapshot() -> dict:
    """Read existing policy owners only when issuing/checking an approval.

    This private snapshot is HMACed, never returned or persisted. No provider,
    plugin tool construction, network discovery or per-SSE scan is performed.
    """
    from row_bot.tools import registry as tool_registry
    from row_bot.plugins import registry as plugin_registry, state as plugin_state
    from row_bot.mcp_client import runtime as mcp_runtime
    native = [(tool.name, tool_registry.is_enabled(tool.name), sorted(tool.destructive_tool_names))
              for tool in tool_registry.get_all_tools()]
    plugins = [(manifest.id, str(manifest.version), plugin_state.is_plugin_enabled(manifest.id),
                plugin_state.get_all_plugin_config(manifest.id))
               for manifest in plugin_registry.get_loaded_manifests()]
    loader = sys.modules.get("row_bot.plugins.loader")
    registrations = []
    if loader is not None:
        with loader._registration_lock:
            registrations = sorted((name, id(api), bool(api._registration_revoked))
                                   for name, api in loader._registrations.items())
    config = mcp_runtime._get_effective_config()
    with mcp_runtime._runtime_lock:
        mcp = {"enabled": bool(config.get("enabled")),
               "servers": [(name, bool(value.get("enabled")), value.get("tools", {}))
                           for name, value in sorted(config.get("servers", {}).items())],
               "registrations": sorted((name, id(runtime)) for name, runtime in mcp_runtime._servers.items()),
               "effects": sorted((server, info.name, info.destructive, info.requires_approval)
                                 for server, catalog in mcp_runtime._catalog.items() for info in catalog.values())}
    tool_registry._load_global_config()
    return {"native": native, "native_config": tool_registry._tool_configs,
            "global_config": tool_registry._global_config,
            "plugins": plugins, "registrations": registrations, "mcp": mcp}


class ProtocolError(Exception):
    def __init__(self, code: str, status: int = 409, *, revision: str | None = None):
        self.code = code
        self.status = status
        self.revision = revision
        super().__init__(code)


@dataclass
class ClientSession:
    id: str
    group_id: str
    binding: str
    csrf: str
    expires: float
    buckets: dict[str, tuple[float, float]] = field(default_factory=dict)


@dataclass
class Subscription:
    id: str
    session_id: str
    conversation_id: str
    epoch: str
    acknowledged: int = 0
    delivered: int = 0
    streaming: bool = False


class ClientSecurity:
    def __init__(self, instance_id: str, *, clock: Callable[[], float] = time.monotonic,
                 policy: Callable[[], dict] | None = None):
        self.instance_id = instance_id
        self.clock = clock
        self._key = secrets.token_bytes(32)
        self._lock = threading.RLock()
        self._sessions: dict[str, ClientSession] = {}
        self._subscriptions: dict[str, Subscription] = {}
        self._local_group = str(uuid4())
        self._nonces: dict[str, tuple] = {}
        self._policy = policy
        self._policy_revision = "1"

    @property
    def policy_revision(self) -> str:
        if self._policy is None:
            return self._policy_revision
        try:
            snapshot = json.dumps(self._policy(), sort_keys=True, separators=(",", ":")).encode()
        except Exception:
            raise ProtocolError("dependency_unavailable", 503) from None
        return str(int.from_bytes(hmac.new(self._key, snapshot, hashlib.sha256).digest()[:8], "big"))

    @policy_revision.setter
    def policy_revision(self, value: str) -> None:
        self._policy = None
        self._policy_revision = value

    def _binding(self, context: AccessContext) -> str:
        if not context.authenticated:
            raise ProtocolError("authentication_required", 401)
        return ("local:" + context.origin if context.is_local_owner
                else "session:" + str(context.session_id) + ":" + context.origin)

    def _prune(self) -> None:
        now = self.clock()
        self._sessions = {k: v for k, v in self._sessions.items() if v.expires > now}
        self._subscriptions = {k: v for k, v in self._subscriptions.items() if v.session_id in self._sessions}
        self._nonces = {k: v for k, v in self._nonces.items() if v[4] > now and v[0] in self._sessions}

    def handshake(self, context: AccessContext, *, session_id: str | None = None,
                  group_id: str | None = None) -> ClientSession:
        with self._lock:
            self._prune()
            binding = self._binding(context)
            if session_id:
                session = self._sessions.get(session_id)
                if session is None or session.binding != binding:
                    raise ProtocolError("session_expired", 401)
                if group_id and session.group_id != group_id:
                    raise ProtocolError("action_denied", 403)
                return session
            if group_id:
                raise ProtocolError("action_denied", 403)
            if len(self._sessions) >= 256:
                raise ProtocolError("rate_limited", 429)
            session = ClientSession(str(uuid4()), self._local_group if context.is_local_owner else str(uuid4()),
                                    binding, secrets.token_urlsafe(32), self.clock() + 12 * 3600)
            self._sessions[session.id] = session
            return session

    def session(self, context: AccessContext, session_id: str, csrf: str) -> ClientSession:
        with self._lock:
            self._prune()
            session = self._sessions.get(session_id)
            if session is None or session.binding != self._binding(context):
                raise ProtocolError("session_expired", 401)
            if not csrf or not hmac.compare_digest(csrf, session.csrf):
                raise ProtocolError("origin_rejected", 403)
            return session

    def rate(self, session: ClientSession, lane: str) -> None:
        capacity, per_minute = {"query": (30, 120), "mutation": (10, 60), "control": (20, 20)}[lane]
        with self._lock:
            now = self.clock()
            tokens, then = session.buckets.get(lane, (float(capacity), now))
            tokens = min(float(capacity), tokens + max(0, now - then) * per_minute / 60)
            if tokens < 1:
                raise ProtocolError("rate_limited", 429)
            session.buckets[lane] = (tokens - 1, now)

    def subscribe(self, session: ClientSession, conversation: str, epoch: str) -> Subscription:
        with self._lock:
            if len(self._subscriptions) >= 1024 or sum(v.session_id == session.id for v in self._subscriptions.values()) >= 32:
                raise ProtocolError("rate_limited", 429)
            sub = Subscription(str(uuid4()), session.id, conversation, epoch)
            self._subscriptions[sub.id] = sub
            return sub

    def subscription(self, session: ClientSession, subscription_id: str) -> Subscription:
        with self._lock:
            sub = self._subscriptions.get(subscription_id)
            if sub is None or sub.session_id != session.id:
                raise ProtocolError("not_found", 404)
            return sub

    def cursor(self, sub: Subscription, revision: str) -> str:
        body = json.dumps([self.instance_id, sub.id, sub.session_id, sub.conversation_id,
                           sub.epoch, str(revision)], separators=(",", ":")).encode()
        with self._lock:
            sub.delivered = max(sub.delivered, int(revision))
        return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + hmac.new(self._key, body, hashlib.sha256).hexdigest()

    def decode_cursor(self, sub: Subscription, cursor: str) -> str:
        try:
            if len(cursor) > 2048:
                raise ValueError()
            token, signature = cursor.split(".")
            body = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            if not hmac.compare_digest(signature, hmac.new(self._key, body, hashlib.sha256).hexdigest()):
                raise ValueError()
            value = json.loads(body)
            if value[:5] != [self.instance_id, sub.id, sub.session_id, sub.conversation_id, sub.epoch]:
                raise ValueError()
            revision = value[5]
            if not isinstance(revision, str) or not revision.isdecimal() or int(revision) > sub.delivered:
                raise ValueError()
            return revision
        except (ValueError, IndexError, TypeError):
            raise ProtocolError("cursor_expired", 410) from None

    def acknowledge(self, sub: Subscription, cursor: str) -> None:
        revision = int(self.decode_cursor(sub, cursor))
        with self._lock:
            sub.acknowledged = max(sub.acknowledged, revision)

    def enter_stream(self, sub: Subscription) -> None:
        with self._lock:
            active = [v for v in self._subscriptions.values() if v.streaming]
            if sub.streaming:
                raise ProtocolError("subscription_in_use", 409)
            if len(active) >= 32 or sum(v.session_id == sub.session_id for v in active) >= 4:
                raise ProtocolError("rate_limited", 429)
            sub.streaming = True

    def leave_stream(self, sub: Subscription) -> None:
        with self._lock:
            sub.streaming = False

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            sub.streaming = False
            self._subscriptions.pop(sub.id, None)

    def approval_nonce(self, session: ClientSession, approval_id: str, revision: str,
                       action_digest: str, *, ttl: float = 300) -> str:
        with self._lock:
            self._prune()
            if len(self._nonces) >= 1024:
                raise ProtocolError("rate_limited", 429)
            nonce = secrets.token_urlsafe(32)
            self._nonces[hashlib.sha256(nonce.encode()).hexdigest()] = (
                session.id, approval_id, revision, action_digest,
                self.clock() + max(0, min(ttl, 300)), self.policy_revision, None)
            return nonce

    def consume_nonce(self, session: ClientSession, approval_id: str, revision: str,
                      action_digest: str, nonce: str, command_id: str) -> None:
        key = hashlib.sha256(nonce.encode()).hexdigest()
        with self._lock:
            value = self._nonces.get(key)
            if (value is None or value[:4] != (session.id, approval_id, revision, action_digest)
                    or value[4] <= self.clock() or value[5] != self.policy_revision):
                raise ProtocolError("approval_expired", 409)
            if value[6] is not None and value[6] != command_id:
                raise ProtocolError("approval_already_resolved", 409)
            self._nonces[key] = (*value[:6], command_id)
