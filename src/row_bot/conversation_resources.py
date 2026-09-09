"""Conversation resource relationships over the existing thread metadata store.

Bindings are server state. Visible Studio selection is never an execution input.
Resource domains retain their data, mutation locks and permission checks.
"""

from __future__ import annotations

import contextvars
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Iterator, Literal


ResourceKind = Literal["workspace", "artifact"]
_KINDS = {"workspace", "artifact"}
_ROLES = {"context", "primary", "reference", "output"}
_MAX_BINDINGS = 200


class ResourceError(ValueError):
    """Safe resource failure suitable for translation at the API boundary."""

    def __init__(self, code: str, current_revision: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current_revision = current_revision


@dataclass(frozen=True)
class ResourceBinding:
    binding_id: str
    kind: ResourceKind
    resource_id: str
    role: str
    revision: str


@dataclass(frozen=True)
class ResourceSnapshot:
    conversation_id: str
    revision: str
    bindings_revision: str
    bindings: tuple[ResourceBinding, ...]


@dataclass(frozen=True)
class ResourceDescriptor:
    binding: ResourceBinding
    title: str
    resource_revision: str
    available: bool


def _identifier(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 128
            or any(ord(char) < 32 or char in "/\\:" for char in value)
            or value in {".", ".."}):
        raise ResourceError("invalid_resource")
    return value


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    from row_bot import threads

    threads._ensure_thread_db()
    connection = sqlite3.connect(threads.DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _read(connection: sqlite3.Connection, conversation_id: str) -> ResourceSnapshot:
    row = connection.execute(
        "SELECT client_revision, resource_revision, resource_bindings_json, "
        "developer_workspace_id, project_workspace_id, project_id "
        "FROM thread_meta WHERE thread_id = ?", (conversation_id,),
    ).fetchone()
    if row is None:
        raise ResourceError("not_found")
    bindings: list[ResourceBinding] = []
    if row["resource_bindings_json"]:
        try:
            values = json.loads(row["resource_bindings_json"])
            if not isinstance(values, list) or len(values) > _MAX_BINDINGS:
                raise ValueError
            for value in values:
                binding = ResourceBinding(**value)
                _identifier(binding.binding_id)
                _identifier(binding.resource_id)
                if binding.kind not in _KINDS or binding.role not in _ROLES:
                    raise ValueError
                if not binding.revision.isdecimal():
                    raise ValueError
                bindings.append(binding)
            if len({binding.binding_id for binding in bindings}) != len(bindings):
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ResourceError("resource_state_invalid") from None
    else:
        # Supported old-data relationship IDs derive from existing durable IDs,
        # never names/content/selection. A mutation persists these exact IDs.
        legacy = [("workspace", row["developer_workspace_id"] or row["project_workspace_id"]),
                  ("artifact", row["project_id"])]
        for kind, resource_id in legacy:
            if resource_id:
                _identifier(resource_id)
                binding_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                    f"row-bot:binding:{conversation_id}:{kind}:{resource_id}"))
                bindings.append(ResourceBinding(binding_id, kind, resource_id, "primary", "0"))
    return ResourceSnapshot(conversation_id, str(row["client_revision"]),
                            str(row["resource_revision"]), tuple(bindings))


def list_bindings(conversation_id: str) -> ResourceSnapshot:
    """Read relationships, retaining unavailable resources for explicit recovery."""
    _identifier(conversation_id)
    with _connect() as connection:
        return _read(connection, conversation_id)


def describe(binding: ResourceBinding) -> ResourceDescriptor:
    """Read a safe descriptor without loading a renderer or normalizing assets."""
    _identifier(binding.resource_id)
    if binding.kind == "workspace":
        from row_bot.developer.storage import get_workspace

        workspace = get_workspace(binding.resource_id)
        return ResourceDescriptor(binding, workspace.name if workspace else "Workspace unavailable",
                                  workspace.updated_at if workspace else "", workspace is not None)
    if binding.kind == "artifact":
        from row_bot.designer.storage import get_project_metadata

        metadata = get_project_metadata(binding.resource_id)
        return ResourceDescriptor(binding, metadata["name"] if metadata else "Artifact unavailable",
                                  metadata["updated_at"] if metadata else "", metadata is not None)
    raise ResourceError("capability_unavailable")


def _assert_write(snapshot: ResourceSnapshot, expected_revision: int) -> None:
    from row_bot.threads import _thread_write_blocked

    if _thread_write_blocked(snapshot.conversation_id):
        raise ResourceError("conversation_deleting")
    if str(expected_revision) != snapshot.revision:
        raise ResourceError("revision_conflict", int(snapshot.revision))


def _persist(connection: sqlite3.Connection, snapshot: ResourceSnapshot,
             bindings: list[ResourceBinding]) -> ResourceSnapshot:
    def legacy_id(kind: str) -> str:
        candidates = [binding for binding in bindings if binding.kind == kind]
        primary = [binding for binding in candidates if binding.role == "primary"]
        selected = primary or candidates
        return selected[0].resource_id if len(selected) == 1 else ""

    connection.execute(
        "UPDATE thread_meta SET resource_bindings_json = ?, "
        "resource_revision = resource_revision + 1, client_revision = client_revision + 1, "
        "project_workspace_id = CASE WHEN COALESCE(developer_workspace_id, '') = ? "
        "THEN project_workspace_id ELSE ? END, "
        "developer_workspace_id = ?, project_id = ? WHERE thread_id = ?",
        (json.dumps([asdict(binding) for binding in bindings], separators=(",", ":")),
         legacy_id("workspace"), legacy_id("workspace"), legacy_id("workspace"),
         legacy_id("artifact"), snapshot.conversation_id),
    )
    return _read(connection, snapshot.conversation_id)


def bind(conversation_id: str, kind: ResourceKind, resource_id: str, *,
         expected_revision: int | str, role: str = "context",
         expected_resource_revision: str | None = None) -> ResourceSnapshot:
    """Bind an existing resource with conversation and optional source version CAS."""
    _identifier(conversation_id)
    _identifier(resource_id)
    if kind not in _KINDS or role not in _ROLES:
        raise ResourceError("invalid_resource")
    binding = ResourceBinding(str(uuid.uuid4()), kind, resource_id, role, "1")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        snapshot = _read(connection, conversation_id)
        _assert_write(snapshot, expected_revision)
        descriptor = describe(binding)
        if not descriptor.available:
            raise ResourceError("resource_unavailable")
        if expected_resource_revision is not None and expected_resource_revision != descriptor.resource_revision:
            raise ResourceError("resource_revision_conflict")
        if any(item.kind == kind and item.resource_id == resource_id for item in snapshot.bindings):
            return snapshot
        if role == "primary" and any(item.kind == kind and item.role == "primary" for item in snapshot.bindings):
            raise ResourceError("resource_ambiguous")
        if len(snapshot.bindings) >= _MAX_BINDINGS:
            raise ResourceError("resource_limit")
        return _persist(connection, snapshot, [*snapshot.bindings, binding])


def unbind(conversation_id: str, binding_id: str, *, expected_revision: int | str) -> ResourceSnapshot:
    """Remove a relationship only; never delete the shared domain resource."""
    _identifier(conversation_id)
    _identifier(binding_id)
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        snapshot = _read(connection, conversation_id)
        _assert_write(snapshot, expected_revision)
        if not any(item.binding_id == binding_id for item in snapshot.bindings):
            raise ResourceError("not_found")
        return _persist(connection, snapshot,
                        [item for item in snapshot.bindings if item.binding_id != binding_id])


def inherit_bindings(parent_id: str, child_id: str, *, expected_parent_revision: int | str,
                     expected_child_revision: int | str,
                     preserve_child_workspace: bool = False) -> ResourceSnapshot:
    """Copy accepted relationships, preserving a separately allocated child workspace."""
    _identifier(parent_id)
    _identifier(child_id)
    if parent_id == child_id:
        raise ResourceError("invalid_resource")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        parent = _read(connection, parent_id)
        child = _read(connection, child_id)
        _assert_write(parent, expected_parent_revision)
        _assert_write(child, expected_child_revision)
        if child.bindings and not preserve_child_workspace:
            raise ResourceError("resource_ambiguous")
        preserved = [item for item in child.bindings if item.kind == "workspace"] if preserve_child_workspace else []
        inherited_source = list(parent.bindings)
        if preserve_child_workspace:
            parent_keys = {(item.kind, item.resource_id) for item in parent.bindings}
            if any((item.kind, item.resource_id) not in parent_keys
                   for item in child.bindings if item.kind != "workspace"):
                raise ResourceError("resource_ambiguous")
            if preserved:
                primary = [item for item in preserved if item.role == "primary"]
                if len(primary) > 1 or (len(preserved) > 1 and not primary):
                    raise ResourceError("resource_ambiguous")
                existing_ids = {item.resource_id for item in preserved}
                inherited_source = [item for item in parent.bindings
                                    if item.kind != "workspace" or (
                                        bool(primary) and item.role != "primary"
                                        and item.resource_id not in existing_ids)]
        if any(not describe(item).available for item in [*preserved, *inherited_source]):
            raise ResourceError("resource_unavailable")
        inherited = [*preserved, *(ResourceBinding(str(uuid.uuid4()), item.kind, item.resource_id, item.role, "1")
                                  for item in inherited_source)]
        if len(inherited) > _MAX_BINDINGS:
            raise ResourceError("resource_limit")
        return _persist(connection, child, inherited)


@dataclass(frozen=True)
class ResourceExecutionContext:
    conversation_id: str
    bindings: tuple[ResourceBinding, ...]

    def resolve(self, kind: ResourceKind) -> ResourceBinding | None:
        """Revalidate current binding before dispatch; ambiguity never picks a UI view."""
        candidates = [item for item in self.bindings if item.kind == kind]
        primary = [item for item in candidates if item.role == "primary"]
        candidates = primary or candidates
        if len(candidates) > 1:
            raise ResourceError("resource_ambiguous")
        if not candidates:
            return None
        selected = candidates[0]
        current = list_bindings(self.conversation_id)
        if selected not in current.bindings:
            raise ResourceError("resource_binding_revoked")
        if not describe(selected).available:
            raise ResourceError("resource_unavailable")
        return selected


_execution_context: contextvars.ContextVar[ResourceExecutionContext | None] = contextvars.ContextVar(
    "row_bot_resource_execution_context", default=None,
)


def current_execution_context() -> ResourceExecutionContext | None:
    return _execution_context.get()


@contextmanager
def execution_context(conversation_id: str, *, binding_ids: tuple[str, ...] | None = None
                      ) -> Iterator[ResourceExecutionContext]:
    """Capture explicit accepted relationships for one execution or child handoff."""
    snapshot = list_bindings(conversation_id)
    bindings = snapshot.bindings
    if binding_ids is not None:
        if not set(binding_ids) <= {item.binding_id for item in bindings}:
            raise ResourceError("resource_binding_revoked")
        bindings = tuple(item for item in bindings if item.binding_id in binding_ids)
    context = ResourceExecutionContext(conversation_id, bindings)
    token = _execution_context.set(context)
    try:
        yield context
    finally:
        _execution_context.reset(token)
