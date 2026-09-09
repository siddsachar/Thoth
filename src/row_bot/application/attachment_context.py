"""Execution-local attachment context using the existing file/media owners.

The legacy renderer retains its cache outside this scope. A headless execution
never falls back to that renderer's attachments, including when it has no files.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from row_bot.file_context import IMAGE_EXTENSIONS, materialize_chat_attachments, process_attached_files, wrap_attachment_context


@dataclass
class AttachmentExecutionCaches:
    conversation_id: str
    data: dict[str, bytes] = field(default_factory=dict)
    images: dict[str, bytes] = field(default_factory=dict)
    pending_image: str | None = None
    pending_video: dict | None = None


_current: ContextVar[AttachmentExecutionCaches | None] = ContextVar("attachment_execution_caches", default=None)


def current_caches() -> AttachmentExecutionCaches | None:
    return _current.get()


def tool_cache(kind: Literal["data", "images"], legacy: dict[str, bytes]) -> dict[str, bytes]:
    """Return the explicit execution cache, or the compatibility caller's cache."""
    current = _current.get()
    if current is None:
        return legacy
    return current.data if kind == "data" else current.images


@contextmanager
def tool_attachment_scope() -> Iterator[AttachmentExecutionCaches | None]:
    """Keep each parallel tool's media result associated with its own call."""
    parent = _current.get()
    if parent is None:
        yield None
        return
    child = AttachmentExecutionCaches(parent.conversation_id, data=parent.data, images=parent.images)
    token = _current.set(child)
    try:
        yield child
    finally:
        _current.reset(token)
        # Data and image dictionaries belong to the outer execution. A tool
        # finishing cannot clear attachments still needed by its siblings.
        child.pending_image = None
        child.pending_video = None


@contextmanager
def prepared_attachments(conversation_id: str, files: list[dict], *,
                         model_ref: str | None = None) -> Iterator[str]:
    """Prepare after admission, retaining scoped caches through provider teardown."""
    from row_bot.vision_runtime import get_vision_service

    caches = AttachmentExecutionCaches(conversation_id)
    token = _current.set(caches)
    try:
        if not files:
            yield ""
            return
        manifest = materialize_chat_attachments(files)
        if any(item.get("error") for item in manifest):
            raise ValueError("attachment_materialization_failed")
        vision = get_vision_service() if any(Path(item["name"]).suffix.lower() in IMAGE_EXTENSIONS
                                            for item in files) else None
        context, _, _warnings = process_attached_files(files, vision, caches.data, model_name=model_ref)
        caches.images.update({item["name"]: item["data"] for item in files
                              if Path(item["name"]).suffix.lower() in IMAGE_EXTENSIONS})
        yield wrap_attachment_context(context)
    finally:
        _current.reset(token)
        caches.data.clear()
        caches.images.clear()
        caches.pending_image = None
        caches.pending_video = None
