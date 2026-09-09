"""Authenticated JSON commands and observational SSE over application services."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response, StreamingResponse

from row_bot.access.middleware import AccessMiddleware
from row_bot.access.request_context import ACCESS_CONTEXT_SCOPE_KEY, AccessContext, request_origin_matches
from row_bot.api.v1.schemas import (Acknowledgement, Command, EVENT_LIMIT, Event, Handshake,
                                    JSON_LIMIT, Problem, PROTOCOL_VERSION)
from row_bot.api.v1.security import ClientSecurity, ProtocolError, current_policy_snapshot
from row_bot.api.v1 import schemas as dto

HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}
_STATUS = {"not_found": 404, "cursor_expired": 410, "invalid_command": 422,
           "checkpoint_unavailable": 503, "model_selection_required": 422,
           "authentication_required": 401, "action_denied": 403,
           "upload_expired": 410, "upload_incomplete": 409,
           "payload_too_large": 413, "rate_limited": 429,
           "model_selection_mismatch": 422, "invalid_resource": 422}
_STATUS.update({"approval_required": 409, "conversation_deleting": 409,
                "resource_state_invalid": 503, "capability_unavailable": 403,
                "resource_unavailable": 404, "resource_revision_conflict": 409,
                "resource_ambiguous": 409, "resource_limit": 413, "resource_binding_revoked": 403})


class ProtocolRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()
        async def safe_handler(request: Request) -> Response:
            try:
                return await original(request)
            except (RequestValidationError, ValidationError):
                return problem(ProtocolError("invalid_command", 422))
            except Exception as exc:
                return problem(exc)
        return safe_handler


def problem(exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", "dependency_unavailable")
    # Only codes are public. Never interpolate exceptions or validator input.
    known = {"revision_conflict", "idempotency_mismatch", "idempotency_expired",
             "approval_expired", "approval_already_resolved", "cursor_expired", "not_found",
             "invalid_command", "payload_too_large", "rate_limited", "protocol_incompatible",
             "authentication_required", "session_expired", "origin_rejected", "action_denied",
             "capability_revoked", "dependency_unavailable", "operation_uncertain", "generation_active",
             "generation_not_steerable", "model_selection_required", "subscription_in_use",
             "checkpoint_unavailable", "upload_expired", "upload_incomplete",
             "model_selection_mismatch", "invalid_resource"}
    if code not in known and code not in _STATUS:
        code = "dependency_unavailable"
    status = getattr(exc, "status", _STATUS.get(code, 503 if code == "dependency_unavailable" else 409))
    revision = getattr(exc, "revision", getattr(exc, "current_revision", None))
    revision = str(revision) if revision is not None else None
    envelope = Problem(type=f"urn:row-bot:error:{code}", title=code.replace("_", " ").capitalize(),
                       status=status, code=code, request_id=uuid4(), current_revision=revision,
                       retryable=status in {429, 503},
                       recovery="reload_then_review" if status == 409 else "authenticate" if status == 401
                       else "update_client" if status == 426 else "retry" if status in {429, 503} else "none")
    return JSONResponse(envelope.model_dump(mode="json", exclude_none=True), status_code=status,
                        media_type="application/problem+json", headers=HEADERS)


def _wire(model: Any, value: Any, *, status_code: int = 200) -> JSONResponse:
    # Validate the *outgoing* closed record, including UUID wire strings. Unknown
    # internal fields fail closed rather than leaking through JSONResponse.
    try:
        record = model.model_validate_json(json.dumps(value))
    except (ValueError, ValidationError, TypeError):
        raise ProtocolError("dependency_unavailable", 503) from None
    return JSONResponse(record.model_dump(mode="json", exclude_unset=True),
                        status_code=status_code, headers=HEADERS)


async def _body(request: Request, model, maximum: int = JSON_LIMIT):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise ProtocolError("invalid_command", 422)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > maximum:
            raise ProtocolError("payload_too_large", 413)
        data.extend(chunk)
    try:
        return model.model_validate_json(bytes(data))
    except (ValidationError, ValueError):
        raise ProtocolError("invalid_command", 422) from None


async def _context(request: Request) -> AccessContext:
    context = request.scope.get(ACCESS_CONTEXT_SCOPE_KEY)
    validate = request.scope.get("row_bot_revalidate_access")
    if validate is not None:
        context = await validate()
    if not isinstance(context, AccessContext) or not context.authenticated:
        raise ProtocolError("authentication_required", 401)
    if request.headers.get("origin") is not None and not request_origin_matches(context, request.scope):
        raise ProtocolError("origin_rejected", 403)
    return context


def cached_choices() -> dict:
    """Read existing caches/catalogues; opening a client never refreshes a provider."""
    from row_bot.providers.model_catalog_cache import read_model_catalog_cache
    from row_bot.providers.selection import list_model_choice_options
    from row_bot.tools import registry as tool_registry
    from row_bot.plugins import registry as plugin_registry, state as plugin_state
    from row_bot.mcp_client.runtime import get_catalog_snapshot
    snapshot = read_model_catalog_cache()
    models = [{"provider_id": row["provider_id"], "model_ref": row["value"],
               "label": row["label"], "available": bool(row.get("active")),
               "unavailable_reason": "configuration_required" if not row.get("active") else None}
              for row in list_model_choice_options(include_inactive=True)]
    capabilities = []
    def add(identifier: str, enabled: bool, destructive: bool) -> None:
        capabilities.append({"id": identifier, "available": enabled, "requires_approval": destructive,
                             "unavailable_reason": None if enabled else "unavailable"})
    for tool in tool_registry.get_all_tools():
        add(tool.name, tool_registry.is_enabled(tool.name), bool(tool.destructive_tool_names))
    for manifest in plugin_registry.get_loaded_manifests():
        for tool in plugin_registry.get_plugin_tools(manifest.id):
            # Parent capability IDs preserve native aliases in the execution
            # registry; metadata reads never invoke plugin as_langchain_tools.
            add(tool.name, plugin_state.is_plugin_enabled(manifest.id), bool(tool.destructive_tool_names))
    for tools in get_catalog_snapshot().values():
        for tool in tools:
            add(tool["prefixed_name"], bool(tool["enabled"]), bool(tool["requires_approval"]))
    return {"models": models, "capabilities": capabilities, "catalog_stale": snapshot.is_stale}


def create_router(service: Any, security: ClientSecurity, *, choices: Callable[[], dict] = cached_choices) -> APIRouter:
    from row_bot.application.attachments import AttachmentUploads
    uploads = AttachmentUploads(clock=security.clock)

    @asynccontextmanager
    async def upload_lifespan(app: FastAPI) -> Any:
        async def expire_uploads() -> None:
            while True:
                await asyncio.sleep(30)
                await asyncio.to_thread(uploads.expire)
        cleanup = asyncio.create_task(expire_uploads(), name="client-upload-expiry")
        try:
            yield
        finally:
            cleanup.cancel()
            try:
                await cleanup
            except asyncio.CancelledError:
                pass
            uploads.close()
    router = APIRouter(prefix="/api/v1", route_class=ProtocolRoute, lifespan=upload_lifespan)

    async def session(request: Request, *, lane: str = "query") -> Any:
        context = await _context(request)
        current = security.session(context, request.headers.get("x-client-session", ""),
                                   request.headers.get("x-csrf-token", ""))
        security.rate(current, lane)
        return current

    async def call(method: Callable, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(method, *args, **kwargs)

    def dispatch_validation(request: Request, current: Any) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        def validate() -> None:
            latest = asyncio.run_coroutine_threadsafe(_context(request), loop).result(timeout=5)
            security.session(latest, current.id, current.csrf)
        return validate

    async def respond(request: Request, model: Any, value: Any, *, status_code: int = 200) -> JSONResponse:
        # Query work can wait on a store lock too. Revoke delivery if authority
        # changed while the worker was reading, even for an idempotent replay.
        latest = await _context(request)
        if request.headers.get("x-client-session"):
            security.session(latest, request.headers["x-client-session"], request.headers.get("x-csrf-token", ""))
        return _wire(model, value, status_code=status_code)

    @router.post("/handshake")
    async def handshake(request: Request) -> JSONResponse:
        context = await _context(request)
        if not request_origin_matches(context, request.scope):
            raise ProtocolError("origin_rejected", 403)
        body = await _body(request, Handshake, 16384)
        if body.protocol_major != 1 or body.minimum_minor > 0 or body.maximum_minor < body.minimum_minor:
            raise ProtocolError("protocol_incompatible", 426)
        current = security.handshake(context, session_id=str(body.client_session_id) if body.client_session_id else None,
                                     group_id=str(body.client_group_id) if body.client_group_id else None)
        security.rate(current, "query")
        discovery = await call(choices)
        return await respond(request, dto.HandshakeView, {"protocol_version": PROTOCOL_VERSION, "minimum_client_version": "1.0",
                             "instance_id": security.instance_id, "server_epoch": service.server_epoch,
                             "client_session_id": current.id, "client_group_id": current.group_id,
                             "csrf_token": current.csrf, "authentication_kind": context.authentication_kind,
                             "policy_revision": security.policy_revision,
                             "session_ttl_seconds": max(0, int(current.expires - security.clock())),
                             "native_adapter": {"available": False},
                             "limits": dto.Limits().model_dump(),
                             **discovery})

    @router.get("/conversations")
    async def conversations(request: Request, limit: int = 50, cursor: str | None = None) -> JSONResponse:
        await session(request)
        if not 1 <= limit <= 200 or (cursor and len(cursor) > 2048):
            raise ProtocolError("invalid_command", 422)
        return await respond(request, dto.ConversationPage, await call(service.list_conversations, limit, cursor))

    @router.get("/conversations/{conversation_id}")
    async def conversation(conversation_id: str, request: Request) -> JSONResponse:
        await session(request)
        return await respond(request, dto.ConversationView, await call(service.get_conversation, conversation_id))

    @router.get("/conversations/{conversation_id}/transcript")
    async def transcript(conversation_id: str, request: Request, limit: int = 100, cursor: str | None = None) -> JSONResponse:
        await session(request)
        if not 1 <= limit <= 100 or (cursor and len(cursor) > 2048):
            raise ProtocolError("invalid_command", 422)
        return await respond(request, dto.TranscriptPage, await call(service.transcript, conversation_id, limit, cursor))

    @router.get("/conversations/{conversation_id}/content/{message_id}")
    async def lazy_content(conversation_id: str, message_id: str, request: Request,
                           limit_bytes: int = 65536, cursor: str | None = None) -> JSONResponse:
        await session(request)
        if not 1 <= limit_bytes <= 65536 or (cursor and len(cursor) > 2048):
            raise ProtocolError("invalid_command", 422)
        result = await call(service.lazy_content, conversation_id, message_id, limit_bytes, cursor)
        await _context(request)
        return await respond(request, dto.LazyContent, result)

    async def command(target: str, request: Request, *, approval: bool = False, create: bool = False) -> JSONResponse:
        body = await _body(request, Command, 16384 if approval else JSON_LIMIT)
        current = await session(request, lane="control" if body.type in {"conversation.stop", "approval.resolve"} else "mutation")
        if str(body.client_session_id) != current.id:
            raise ProtocolError("action_denied", 403)
        if approval != (body.type == "approval.resolve") or create != (body.type == "conversation.create"):
            raise ProtocolError("invalid_command", 422)
        try:
            key = str(UUID(request.headers.get("idempotency-key", "")))
        except ValueError:
            raise ProtocolError("invalid_command", 422) from None
        wire = body.model_dump(mode="json")
        if approval:
            try:
                previous = await call(service.receipt, security.instance_id, str(body.command_id))
            except Exception as exc:
                if getattr(exc, "code", None) != "not_found":
                    raise
                previous = None
            if not previous or previous.get("status") not in {"completed", "accepted"}:
                view = await call(service.get_approval, target)
                security.consume_nonce(current, target, body.expected_revision, str(view["action_digest"]),
                                       body.payload["nonce"], str(body.command_id))
            wire["payload"] = {k: v for k, v in wire["payload"].items() if k != "nonce"}
        # Revalidate after asynchronous reads, before entering the durable command owner.
        await _context(request)
        validate_access = dispatch_validation(request, current)
        def validate_dispatch() -> None:
            validate_access()
            if approval and (not previous or previous.get("status") not in {"completed", "accepted"}):
                # Recheck the exact current stored action, policy and nonce deadline
                # after the admission lock wait, immediately before CAS/effect.
                latest = service.get_approval(target)
                security.consume_nonce(current, target, body.expected_revision,
                                       str(latest["action_digest"]), body.payload["nonce"], str(body.command_id))
        result = await call(service.execute, owner_id=security.instance_id,
                            idempotency_key=key, command=wire, target=target, validate=validate_dispatch)
        accepted = result.get("status") == "accepted"
        return await respond(request, dto.CommandReceipt, result, status_code=202 if accepted else 200)

    @router.post("/conversations/commands")
    async def create_conversation(request: Request) -> JSONResponse:
        return await command("conversations", request, create=True)

    @router.post("/conversations/{conversation_id}/commands")
    async def conversation_command(conversation_id: str, request: Request) -> JSONResponse:
        return await command(conversation_id, request)

    @router.get("/commands/{command_id}")
    async def receipt(command_id: str, request: Request) -> JSONResponse:
        await session(request)
        return await respond(request, dto.CommandReceipt, await call(service.receipt, security.instance_id, command_id))

    @router.get("/approvals/{approval_id}")
    async def approval_view(approval_id: str, request: Request) -> JSONResponse:
        current = await session(request)
        view = await call(service.get_approval, approval_id)
        ttl = 300.0
        if view.get("expires_at"):
            expiry = datetime.fromisoformat(view["expires_at"].replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            ttl = min(ttl, (expiry - datetime.now(timezone.utc)).total_seconds())
        if view.get("status") != "pending" or ttl <= 0:
            raise ProtocolError("approval_expired", 409)
        nonce = security.approval_nonce(current, approval_id, view["revision"], view["action_digest"], ttl=ttl)
        public = {k: view[k] for k in ("id", "status", "revision", "expires_at", "summary", "policy_revision") if k in view}
        public["policy_revision"] = security.policy_revision
        return await respond(request, dto.ApprovalView, {**public, "nonce": nonce})

    @router.post("/approvals/{approval_id}/commands")
    async def approval_command(approval_id: str, request: Request) -> JSONResponse:
        return await command(approval_id, request, approval=True)

    @router.post("/conversations/{conversation_id}/subscriptions")
    async def subscribe(conversation_id: str, request: Request) -> JSONResponse:
        current = await session(request, lane="mutation")
        snap = await call(service.snapshot, conversation_id)
        sub = security.subscribe(current, conversation_id, snap["server_epoch"])
        # The exact snapshot cut is signed. A later event remains in the suffix.
        cursor = security.cursor(sub, snap["cursor"])
        return await respond(request, dto.SubscriptionView, {"subscription_id": sub.id, "snapshot": {**snap, "cursor": cursor},
                             "cursor": cursor})

    async def replay(sub: Any, cursor: str) -> dict:
        revision = security.decode_cursor(sub, cursor)
        result = await call(service.events_since, sub.conversation_id, revision)
        if result["server_epoch"] != sub.epoch or result["snapshot_required"]:
            snap = await call(service.snapshot, sub.conversation_id)
            sub.epoch = snap["server_epoch"]
            sub.delivered = sub.acknowledged = 0
            cut = security.cursor(sub, snap["cursor"])
            return {"snapshot_required": True, "snapshot": {**snap, "cursor": cut}, "events": [], "cursor": cut}
        events = []
        encoded_bytes = 0
        delivered_revision = revision
        for item in result["events"]:
            event = Event.model_validate({"topic": f"conversation.{sub.conversation_id}", **item}).model_dump(mode="json")
            encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
            if len(encoded) > EVENT_LIMIT:
                raise ProtocolError("payload_too_large", 413)
            record = {"cursor": security.cursor(sub, event["projection_revision"]), "event": event}
            size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode())
            if events and encoded_bytes + size > JSON_LIMIT - 4096:
                break
            events.append(record)
            encoded_bytes += size
            delivered_revision = event["projection_revision"]
        # Remaining retained events stay after this exact delivered cut. Polling
        # and SSE share this bound and never skip a suffix when paginating.
        return {"snapshot_required": False, "events": events, "cursor": security.cursor(sub, delivered_revision)}

    @router.get("/events/poll")
    async def poll(request: Request, subscription_id: str, cursor: str) -> JSONResponse:
        current = await session(request)
        sub = security.subscription(current, subscription_id)
        if sub.streaming:
            raise ProtocolError("subscription_in_use", 409)
        result = await replay(sub, cursor)
        await _context(request)
        return await respond(request, dto.EventPage, result)

    @router.put("/subscriptions/{subscription_id}/ack")
    async def acknowledge(subscription_id: str, request: Request) -> JSONResponse:
        current = await session(request, lane="control")
        body = await _body(request, Acknowledgement, 4096)
        sub = security.subscription(current, subscription_id)
        security.acknowledge(sub, body.cursor)
        return await respond(request, dto.Acknowledged, {"acknowledged": True})

    @router.delete("/subscriptions/{subscription_id}")
    async def unsubscribe(subscription_id: str, request: Request) -> JSONResponse:
        current = await session(request, lane="control")
        sub = security.subscription(current, subscription_id)
        security.unsubscribe(sub)
        return await respond(request, dto.Unsubscribed, {"unsubscribed": True})

    @router.get("/events")
    async def events(request: Request, subscription_id: str, cursor: str) -> StreamingResponse:
        current = await session(request)
        sub = security.subscription(current, subscription_id)
        security.decode_cursor(sub, cursor)
        security.enter_stream(sub)

        async def stream() -> Any:
            position = cursor
            heartbeat = security.clock()
            try:
                while sub.streaming and not await request.is_disconnected():
                    await _context(request)
                    security.session(await _context(request), current.id, current.csrf)
                    result = await replay(sub, position)
                    if result["snapshot_required"]:
                        await _context(request)
                        # A snapshot may exceed one event's limit. Transfer the new
                        # atomic snapshot/cut through subscription JSON, never an
                        # oversized SSE event or a silently advanced client cursor.
                        yield "event: snapshot_required\ndata: " + dto.StreamReset().model_dump_json() + "\n\n"
                        return
                    else:
                        for item in result["events"]:
                            await _context(request)
                            yield f"id: {item['cursor']}\nevent: domain\ndata: " + json.dumps(item["event"], separators=(",", ":")) + "\n\n"
                    position = result["cursor"]
                    if security.clock() - heartbeat >= 15:
                        yield ": heartbeat\n\n"
                        heartbeat = security.clock()
                    await asyncio.sleep(0.25)
            except ProtocolError:
                return
            finally:
                security.leave_stream(sub)
        return StreamingResponse(stream(), media_type="text/event-stream", headers=HEADERS)

    @router.get("/choices")
    async def model_choices(request: Request) -> JSONResponse:
        await session(request)
        return await respond(request, dto.Choices, await call(choices))

    @router.get("/resources/{reference}")
    async def resource(reference: str, request: Request) -> JSONResponse:
        await session(request)
        from row_bot.conversation_resources import list_bindings, describe
        try:
            conversation_id, binding_id = reference.rsplit(":", 1)
        except ValueError:
            raise ProtocolError("not_found", 404) from None
        snapshot = await call(list_bindings, conversation_id)
        binding = next((item for item in snapshot.bindings if item.binding_id == binding_id), None)
        if binding is None:
            raise ProtocolError("not_found", 404)
        descriptor = await call(describe, binding)
        return await respond(request, dto.ResourceView, {"resource_ref": reference, "conversation_revision": snapshot.revision,
                             **asdict(descriptor)})

    @router.post("/uploads")
    async def upload(request: Request, conversation_id: str, name: str) -> JSONResponse:
        current = await session(request, lane="mutation")
        from row_bot.application.attachments import UPLOAD_CHUNK_BYTES
        import hashlib
        try:
            key = str(UUID(request.headers.get("idempotency-key", "")))
            command_id = str(UUID(request.headers.get("x-command-id", "")))
        except ValueError:
            raise ProtocolError("invalid_command", 422) from None
        await call(service.get_conversation, conversation_id)
        # The convenience atomic upload is one chunk. Larger files use the
        # resumable session surface, with the same bounded transfer admission.
        uploads.enter_transfer(current.id)
        try:
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > UPLOAD_CHUNK_BYTES:
                    raise ProtocolError("payload_too_large", 413)
                await _context(request)
                data.extend(chunk)
        finally:
            uploads.leave_chunk(current.id)
        digest = hashlib.sha256(data).hexdigest()
        expected = request.headers.get("x-content-sha256")
        if expected is not None and expected != digest:
            raise ProtocolError("invalid_command", 422)
        await _context(request)
        result = await call(service.register_attachment, owner_id=security.instance_id, idempotency_key=key,
                            command_id=command_id, client_session_id=current.id, conversation_id=conversation_id,
                            name=name, data=bytes(data), mime_type=request.headers.get("content-type", "application/octet-stream"),
                            validate=dispatch_validation(request, current))
        return await respond(request, dto.AttachmentView, result)

    @router.post("/uploads/sessions")
    async def begin_upload(request: Request) -> JSONResponse:
        current = await session(request, lane="mutation")
        body = await _body(request, dto.UploadRequest, 4096)
        return await respond(request, dto.UploadView, await call(uploads.create, current.id, **body.model_dump(mode="json")))

    @router.get("/uploads/{upload_id}")
    async def upload_status(upload_id: str, request: Request) -> JSONResponse:
        current = await session(request)
        return await respond(request, dto.UploadView, await call(uploads.status, current.id, upload_id))

    @router.put("/uploads/{upload_id}/chunks")
    async def upload_chunk(upload_id: str, request: Request, offset: int) -> JSONResponse:
        # Chunk flow has its own four-request admission bound; token rate limits
        # would otherwise prevent completion of a legitimate 25-chunk file.
        current = security.session(await _context(request), request.headers.get("x-client-session", ""),
                                   request.headers.get("x-csrf-token", ""))
        from row_bot.application.attachments import UPLOAD_CHUNK_BYTES
        await call(uploads.enter_chunk, current.id, upload_id)
        try:
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > UPLOAD_CHUNK_BYTES:
                    raise ProtocolError("payload_too_large", 413)
                security.session(await _context(request), current.id, current.csrf)
                data.extend(chunk)
            return await respond(request, dto.UploadView, await call(uploads.write, current.id, upload_id, offset, bytes(data)))
        finally:
            uploads.leave_chunk(current.id)

    @router.post("/uploads/{upload_id}/complete")
    async def complete_upload(upload_id: str, request: Request) -> JSONResponse:
        current = await session(request, lane="mutation")
        body = await _body(request, dto.UploadCompletion, 4096)
        try:
            key = str(UUID(request.headers.get("idempotency-key", "")))
        except ValueError:
            raise ProtocolError("invalid_command", 422) from None
        validate = dispatch_validation(request, current)
        def commit(**kwargs: Any) -> dict:
            return service.register_attachment(owner_id=security.instance_id, idempotency_key=key,
                    command_id=str(body.command_id), client_session_id=current.id, validate=validate, **kwargs)
        result = await call(uploads.complete, current.id, upload_id, commit)
        return await respond(request, dto.AttachmentView, result)

    @router.delete("/uploads/{upload_id}")
    async def cancel_upload(upload_id: str, request: Request) -> JSONResponse:
        current = await session(request, lane="control")
        await call(uploads.cancel, current.id, upload_id)
        return await respond(request, dto.UploadCancelled, {"cancelled": True})

    @router.get("/attachments/{reference}")
    async def attachment(reference: str, request: Request) -> Response:
        current = await session(request)
        from row_bot.application.attachments import read_attachment
        from urllib.parse import quote
        metadata, data = await call(read_attachment, reference)
        await _context(request)
        async def chunks() -> Any:
            try:
                for offset in range(0, len(data), EVENT_LIMIT):
                    security.session(await _context(request), current.id, current.csrf)
                    yield data[offset:offset + EVENT_LIMIT]
            except ProtocolError:
                return
        return StreamingResponse(chunks(), media_type=metadata["mime_type"], headers={**HEADERS,
                                 "Content-Disposition": "attachment; filename*=UTF-8''" + quote(metadata["name"], safe="")})

    return router


def install_client_platform(app: FastAPI, service: Any, *, instance_id: str,
                            security: ClientSecurity | None = None,
                            choices: Callable[[], dict] = cached_choices) -> ClientSecurity:
    security = security or ClientSecurity(instance_id, policy=current_policy_snapshot)
    app.include_router(create_router(service, security, choices=choices))

    return security


def create_client_platform_app(service: Any, *, access_config: Any = None, session_authenticator: Any = None,
                               instance_id: str | None = None, security: ClientSecurity | None = None,
                               choices: Callable[[], dict] = cached_choices) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        lifecycle = None
        if hasattr(service, "registry"):
            from row_bot.application.lifecycle import ApplicationLifecycle
            lifecycle = ApplicationLifecycle(registry=service.registry)
            await lifecycle.startup()
        try:
            yield
        finally:
            if lifecycle is not None:
                await lifecycle.shutdown()
    app = FastAPI(title="Row-Bot client protocol", version=PROTOCOL_VERSION, lifespan=lifespan)
    install_client_platform(app, service, instance_id=instance_id or service.instance_id,
                            security=security, choices=choices)
    app.add_middleware(AccessMiddleware, config=access_config, session_authenticator=session_authenticator)
    return app
