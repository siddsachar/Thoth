"""Canonical wire DTOs. Generated clients derive from these closed commands."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, create_model, model_validator

OpaqueId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")]
Reference = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9:_-]+$")]
Revision = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,19})$")]
PROTOCOL_VERSION = "1.0"
JSON_LIMIT = 256 * 1024
EVENT_LIMIT = 64 * 1024


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModelSelection(WireModel):
    provider_id: OpaqueId
    model_ref: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class SubmitPayload(WireModel):
    submission_id: UUID
    text: Annotated[str, StringConstraints(min_length=1, max_length=200000)]
    attachment_refs: list[Reference] = Field(default_factory=list, max_length=32)
    model_selection: ModelSelection


class RenamePayload(WireModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class CreatePayload(WireModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)] = "New conversation"


class PinPayload(WireModel):
    pinned: bool


class EmptyPayload(WireModel):
    pass


class SteerPayload(WireModel):
    steering_id: UUID
    text: Annotated[str, StringConstraints(min_length=1, max_length=16000)]


class ResumePayload(WireModel):
    model_selection: ModelSelection


class BindPayload(WireModel):
    kind: Literal["workspace", "artifact"]
    resource_id: OpaqueId
    role: Literal["context", "primary", "reference", "output"] = "context"
    expected_resource_revision: str | None = Field(default=None, max_length=128)


class UnbindPayload(WireModel):
    binding_id: OpaqueId


class ApprovalPayload(WireModel):
    decision: Literal["approve", "reject"]
    nonce: Annotated[str, StringConstraints(min_length=32, max_length=256)]


class Command(WireModel):
    command_id: UUID
    client_session_id: UUID
    type: Literal["conversation.create", "conversation.rename", "conversation.pin",
                  "conversation.delete", "conversation.submit", "conversation.stop",
                  "conversation.steer", "conversation.resume", "conversation.bind",
                  "conversation.unbind", "approval.resolve"]
    expected_revision: Revision
    payload: dict

    @model_validator(mode="after")
    def typed_payload(self) -> Command:
        # JSON validation permits UUID wire strings while Python callers remain strict.
        import json
        payload_type = COMMAND_PAYLOADS[self.type]
        self.payload = payload_type.model_validate_json(json.dumps(self.payload)).model_dump(mode="json")
        return self

    @classmethod
    def model_json_schema(cls, **kwargs: Any) -> dict:
        return _variant_schema(cls, COMMAND_PAYLOADS, **kwargs)


COMMAND_PAYLOADS = {
    "conversation.create": CreatePayload, "conversation.rename": RenamePayload,
    "conversation.pin": PinPayload, "conversation.delete": EmptyPayload,
    "conversation.submit": SubmitPayload, "conversation.stop": EmptyPayload,
    "conversation.steer": SteerPayload, "conversation.resume": ResumePayload,
    "conversation.bind": BindPayload, "conversation.unbind": UnbindPayload,
    "approval.resolve": ApprovalPayload,
}


class Handshake(WireModel):
    protocol_major: int = Field(default=1, ge=0, le=65535)
    minimum_minor: int = Field(default=0, ge=0, le=65535)
    maximum_minor: int = Field(default=0, ge=0, le=65535)
    client_build: Annotated[str, StringConstraints(max_length=128)] = "unknown"
    client_session_id: UUID | None = None
    client_group_id: UUID | None = None
    presentation_features: list[OpaqueId] = Field(default_factory=list, max_length=32)


class Problem(WireModel):
    type: str
    title: str
    status: int
    code: str
    request_id: UUID
    retryable: bool = False
    current_revision: Revision | None = None
    recovery: Literal["reload_then_review", "authenticate", "retry", "update_client", "none"] = "none"


class ResourceBinding(WireModel):
    binding_id: OpaqueId
    kind: Literal["workspace", "artifact", "browser_session", "task", "document"]
    resource_id: OpaqueId
    role: Literal["context", "primary", "reference", "output"]
    revision: Revision


class Outcome(WireModel):
    mutation_status: Literal["accepted", "committed", "rejected", "uncertain"]
    projection_status: Literal["pending", "finalizing", "ready", "degraded"]
    external_outcome: Literal["not_applicable", "known_not_sent", "sent", "uncertain"] = "not_applicable"


class GenerationState(WireModel):
    execution_id: OpaqueId
    conversation_id: OpaqueId
    generation_id: OpaqueId
    pass_id: OpaqueId
    segment_id: OpaqueId | None = None
    status: Literal["running", "stopping", "stopped", "waiting_approval", "completed", "interrupted"]
    revision: Revision
    cancel_requested: bool
    quiesced: bool
    cleanup_complete: bool
    external_outcome: Literal["known_not_sent", "sent", "uncertain", "not_applicable"]
    approval_id: OpaqueId | None
    can_stop: bool


class TranscriptDelta(WireModel):
    pass_id: OpaqueId
    segment_id: OpaqueId
    row_id: OpaqueId
    render_revision: Revision
    public_text_delta: Annotated[str, StringConstraints(max_length=60000)]


class ToolActivity(WireModel):
    state: Literal["tool_call", "tool_done"]
    tool_call_id: str = Field(default="", max_length=256)
    message_id: str = Field(default="", max_length=256)
    pass_id: OpaqueId | None = None
    segment_id: OpaqueId | None = None


class GenerationActivity(WireModel):
    state: Literal["thinking"]


class ApprovalRequired(WireModel):
    status: Literal["waiting_approval"]
    approval_id: OpaqueId | None = None


class GenerationError(WireModel):
    code: Literal["generation_failed"]


class TranscriptCheckpoint(WireModel):
    checkpoint_revision: Annotated[str, StringConstraints(max_length=128)]


class TranscriptSettled(WireModel):
    row_id: OpaqueId
    adoption: Literal["exact", "no_adoption"]


class ResourceChanged(WireModel):
    revision: Revision


class ProjectionReset(WireModel):
    reason: Literal["content_evicted"]


class AgentActivity(WireModel):
    run_id: OpaqueId
    status: Literal["queued", "running", "waiting_approval", "waiting_user", "paused", "interrupted",
                    "completed", "completed_delivery_failed", "failed", "stopped", "stopping",
                    "blocked", "timed_out", "cancelled"]
    revision: Revision


class QueueUpdated(WireModel):
    submission_ids: list[OpaqueId] = Field(max_length=256)
    revision: Revision


class MediaAvailable(WireModel):
    media_ref: Reference
    mime_type: Literal["image/png", "image/jpeg", "video/mp4", "application/pdf", "application/octet-stream"]
    tool_call_id: str = Field(default="", max_length=256)
    message_id: str = Field(default="", max_length=256)


class MediaError(WireModel):
    code: Literal["payload_too_large", "media_unavailable"]
    tool_call_id: str = Field(default="", max_length=256)
    message_id: str = Field(default="", max_length=256)


EVENT_PAYLOADS = {"generation.state": GenerationState, "transcript.delta": TranscriptDelta,
                  "tool.activity": ToolActivity, "generation.activity": GenerationActivity,
                  "approval.required": ApprovalRequired, "generation.error": GenerationError,
                  "transcript.checkpoint": TranscriptCheckpoint, "transcript.settled": TranscriptSettled,
                  "resource.changed": ResourceChanged, "agent.activity": AgentActivity,
                  "queue.updated": QueueUpdated, "media.available": MediaAvailable}
EVENT_PAYLOADS["projection.reset"] = ProjectionReset
EVENT_PAYLOADS["media.error"] = MediaError


class Event(WireModel):
    protocol_version: Literal["1.0"] = "1.0"
    event_id: OpaqueId
    topic: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    server_epoch: OpaqueId
    conversation_id: OpaqueId
    projection_revision: Revision
    source: Literal["runtime", "checkpoint", "approval", "resource"] = "runtime"
    source_stream_id: OpaqueId
    source_epoch: OpaqueId
    source_sequence_start: Revision
    source_sequence_end: Revision
    type: Literal["generation.state", "transcript.delta", "tool.activity", "generation.activity",
                  "approval.required", "generation.error", "transcript.checkpoint", "transcript.settled", "resource.changed",
                  "agent.activity", "queue.updated", "media.available", "projection.reset", "media.error"]
    payload: dict

    @model_validator(mode="after")
    def typed_payload(self) -> Event:
        if int(self.source_sequence_start) > int(self.source_sequence_end):
            raise ValueError("Invalid source sequence range")
        self.payload = EVENT_PAYLOADS[self.type].model_validate(self.payload).model_dump(mode="json")
        return self

    @classmethod
    def model_json_schema(cls, **kwargs: Any) -> dict:
        return _variant_schema(cls, EVENT_PAYLOADS, **kwargs)


def _variant_schema(base: type[BaseModel], payloads: dict, **kwargs) -> dict:
    """Expand the same executable payload registry to a closed tagged wire union."""
    return TypeAdapter(_variant_type(base, payloads)).json_schema(**kwargs)


def _variant_type(base: type[BaseModel], payloads: dict) -> Any:
    variants = []
    for tag, payload in payloads.items():
        name = "".join(word.title() for word in tag.replace(".", "_").split("_")) + base.__name__
        fields = {key: (field.rebuild_annotation(), field.default if not field.is_required() else ...)
                  for key, field in base.model_fields.items() if key not in {"type", "payload"}}
        fields["type"] = (Literal[tag], ...)
        fields["payload"] = (payload, ...)
        variants.append(create_model(name, __base__=WireModel, **fields))
    return Annotated[Union[tuple(variants)], Field(discriminator="type")]


class Acknowledgement(WireModel):
    cursor: Annotated[str, StringConstraints(min_length=1, max_length=2048)]


class PreviewContract(WireModel):
    """Phase 3 boundary only; this does not enable generated preview execution."""
    artifact_ref: OpaqueId
    revision: Revision
    sandbox: Literal["allow-scripts"] = "allow-scripts"
    owner_origin_access: Literal[False] = False
    message_limit_bytes: Literal[16384] = 16384
    allowed_messages: tuple[Literal["selection.proposed", "edit.proposed"], ...] = ("selection.proposed", "edit.proposed")


class CommandReceipt(WireModel):
    command_id: UUID
    status: Literal["accepted", "completed", "cancel_requested", "DeleteCompleted", "DeleteBlocked",
                    "DeleteNotFound", "AlreadyDeleting", "DeleteRejected", "admitting", "rejected"]
    conversation_id: OpaqueId | None = None
    generation_id: OpaqueId | None = None
    execution_id: OpaqueId | None = None
    pass_id: OpaqueId | None = None
    submission_id: OpaqueId | None = None
    admission_sequence: Revision | None = None
    revision: Revision | None = None
    approval_id: OpaqueId | None = None
    attachment_ref: Reference | None = None
    binding_id: OpaqueId | None = None
    code: str | None = Field(default=None, max_length=80)
    current_revision: Revision | None = None


class AttachmentView(WireModel):
    attachment_ref: Reference
    name: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    mime_type: Literal["image/png", "image/jpeg", "video/mp4", "application/pdf", "application/octet-stream"]
    size_bytes: int = Field(ge=1, le=26214400)
    revision: Revision


class SessionProof(WireModel):
    client_session_id: UUID
    csrf_token: Annotated[str, StringConstraints(min_length=32, max_length=256)]


Cursor = Annotated[str, StringConstraints(max_length=2048)]


class ConversationView(WireModel):
    id: OpaqueId
    revision: Revision
    title: str = Field(max_length=256)
    pinned: bool
    generation_state: list[GenerationState] = Field(default_factory=list, max_length=32)
    resource_bindings: list[ResourceBinding] = Field(default_factory=list, max_length=200)


class ConversationPage(WireModel):
    items: list[ConversationView] = Field(max_length=200)
    has_more: bool
    next_cursor: Cursor | None = None


class TextBlock(WireModel):
    type: Literal["text"]
    text: str = Field(max_length=2097152)


class TranscriptRow(WireModel):
    id: str = Field(min_length=1, max_length=1024)
    message_id: str | None = Field(default=None, max_length=256)
    role: Literal["user", "assistant", "tool"]
    blocks: list[TextBlock] = Field(max_length=256)
    tool_call_ids: list[str] = Field(default_factory=list, max_length=256)
    tool_call_id: str = Field(default="", max_length=256)
    tool_calls_ref: str | None = Field(default=None, max_length=256)
    render_revision: Revision | None = None
    content_status: Literal["inline", "lazy", "oversized"] | None = None
    content_ref: str | None = Field(default=None, max_length=256)


class Snapshot(WireModel):
    conversation_id: OpaqueId
    server_epoch: OpaqueId
    projection_revision: Revision
    cursor: Cursor
    checkpoint_revision: str = Field(max_length=128)
    rows: list[TranscriptRow] = Field(max_length=200)
    generation: GenerationState | None


class TranscriptPage(Snapshot):
    has_more: bool
    previous_cursor: Cursor | None
    next_cursor: Cursor | None


class SubscriptionView(WireModel):
    subscription_id: UUID
    snapshot: Snapshot
    cursor: Cursor


EventUnion = _variant_type(Event, EVENT_PAYLOADS)


class EventRecord(WireModel):
    cursor: Cursor
    event: EventUnion

    @model_validator(mode="after")
    def valid_sequence(self) -> EventRecord:
        if int(self.event.source_sequence_start) > int(self.event.source_sequence_end):
            raise ValueError("Invalid source sequence range")
        return self


class EventPage(WireModel):
    snapshot_required: bool
    snapshot: Snapshot | None = None
    events: list[EventRecord] = Field(max_length=4096)
    cursor: Cursor


class ModelChoice(WireModel):
    provider_id: OpaqueId
    model_ref: str = Field(min_length=1, max_length=256)
    label: str = Field(max_length=256)
    available: bool
    unavailable_reason: Literal["configuration_required", "unavailable"] | None = None


class CapabilityChoice(WireModel):
    id: OpaqueId
    available: bool
    requires_approval: bool
    unavailable_reason: Literal["configuration_required", "unavailable"] | None = None


class Choices(WireModel):
    models: list[ModelChoice] = Field(max_length=4096)
    capabilities: list[CapabilityChoice] = Field(max_length=4096)
    catalog_stale: bool = True


class NativeAdapter(WireModel):
    available: Literal[False] = False


class Limits(WireModel):
    json_bytes: Literal[262144] = 262144
    event_bytes: Literal[65536] = 65536
    query_rows: Literal[200] = 200
    transcript_rows: Literal[100] = 100
    stream_connections_per_session: Literal[4] = 4
    attachment_bytes: Literal[26214400] = 26214400
    upload_chunk_bytes: Literal[1048576] = 1048576
    upload_batch_bytes: Literal[104857600] = 104857600
    upload_chunks_in_flight: Literal[4] = 4
    upload_ttl_seconds: Literal[1800] = 1800


class HandshakeView(Choices):
    protocol_version: Literal["1.0"]
    minimum_client_version: Literal["1.0"]
    instance_id: OpaqueId
    server_epoch: OpaqueId
    client_session_id: UUID
    client_group_id: UUID
    csrf_token: str = Field(min_length=32, max_length=256)
    authentication_kind: Literal["local_owner", "session"]
    policy_revision: Revision
    session_ttl_seconds: int = Field(ge=0, le=43200)
    native_adapter: NativeAdapter
    limits: Limits


class ApprovalView(WireModel):
    id: OpaqueId
    status: Literal["pending"]
    revision: Revision
    expires_at: str | None = Field(default=None, max_length=80)
    summary: str | None = Field(default=None, max_length=4096)
    policy_revision: Revision
    nonce: str = Field(min_length=32, max_length=256)


class ResourceView(WireModel):
    resource_ref: Reference
    conversation_revision: Revision
    binding: ResourceBinding
    title: str = Field(max_length=256)
    resource_revision: str = Field(max_length=128)
    available: bool


class Acknowledged(WireModel):
    acknowledged: Literal[True]


class Unsubscribed(WireModel):
    unsubscribed: Literal[True]


class UploadRequest(WireModel):
    conversation_id: OpaqueId
    name: str = Field(min_length=1, max_length=240)
    size_bytes: int = Field(ge=1, le=26214400)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    batch_id: UUID


class UploadView(WireModel):
    upload_id: UUID
    size_bytes: int = Field(ge=1, le=26214400)
    received_bytes: int = Field(ge=0, le=26214400)
    chunk_bytes: Literal[1048576] = 1048576
    expires_in_seconds: int = Field(ge=0, le=1800)


class UploadCompletion(WireModel):
    command_id: UUID


class UploadCancelled(WireModel):
    cancelled: Literal[True]


class StreamReset(WireModel):
    snapshot_required: Literal[True] = True
    recovery: Literal["resubscribe"] = "resubscribe"


class LazyContent(WireModel):
    conversation_id: OpaqueId
    content_ref: str = Field(min_length=1, max_length=256)
    checkpoint_revision: str = Field(max_length=128)
    encoding: Literal["base64"]
    media_type: Literal["application/json"]
    data: str = Field(max_length=87384)
    has_more: bool
    next_cursor: Cursor | None
