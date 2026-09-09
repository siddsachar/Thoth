"""Publish each tool's generated media through the existing opaque file owner."""
from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
import re
from uuid import uuid4

from row_bot.application.attachment_context import AttachmentExecutionCaches
from row_bot.application.attachments import (
    AttachmentError, MAX_ATTACHMENT_BYTES, _conversation, _managed_root, _open, _safe_path, _write, register_attachment,
)


def save_generated_output(conversation_id: str, data: bytes, *, prefix: str, extension: str) -> str:
    """Reserve an original generated file; transport limits apply only at capture.

    The existing media owner retains large outputs. UUID names plus exclusive
    creation keep simultaneous tools from overwriting one another's originals.
    """
    from row_bot import threads
    if (not isinstance(data, bytes) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", prefix)
            or not re.fullmatch(r"[A-Za-z0-9]{1,12}", extension)):
        raise AttachmentError("invalid_command")
    _conversation(conversation_id)
    root = _managed_root(threads._MEDIA_DIR)
    folder = root / conversation_id
    _safe_path(root, folder)
    folder.mkdir(parents=True, exist_ok=True)
    _safe_path(root, folder)
    while True:
        _conversation(conversation_id)
        path = folder / f"{prefix}_{uuid4().hex}.{extension}"
        try:
            _write(root, path, data)
            return str(path)
        except FileExistsError:
            continue


def _image(conversation_id: str, encoded: str) -> dict:
    if not isinstance(encoded, str):
        raise AttachmentError("media_unavailable")
    if len(encoded) > 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3):
        raise AttachmentError("payload_too_large")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise AttachmentError("media_unavailable") from None
    if not data:
        raise AttachmentError("media_unavailable")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError("payload_too_large")
    suffix = ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg" if data.startswith(b"\xff\xd8\xff") else ".bin"
    return register_attachment(conversation_id, "generated-image" + suffix, data)


def _video(conversation_id: str, metadata: dict) -> dict:
    from row_bot import threads
    if not isinstance(metadata, dict) or not isinstance(metadata.get("path"), str) or not metadata["path"]:
        raise AttachmentError("media_unavailable")
    root = _managed_root(threads._MEDIA_DIR)
    folder = root / conversation_id
    _safe_path(root, folder)
    path = Path(metadata["path"]).absolute()
    _safe_path(path.parent, path)
    path = path.parent.resolve() / path.name
    _safe_path(folder, path)
    with _open(folder, path) as source:
        before = os.fstat(source.fileno())
        if before.st_size > MAX_ATTACHMENT_BYTES:
            raise AttachmentError("payload_too_large")
        data = source.read(MAX_ATTACHMENT_BYTES + 1)
        after = os.fstat(source.fileno())
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError("payload_too_large")
        if (len(data) != before.st_size or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or len(data) < 12 or data[4:8] != b"ftyp"):
            raise AttachmentError("media_unavailable")
    return register_attachment(conversation_id, "generated-video.mp4", data)


def capture_generated_media(conversation_id: str, caches: AttachmentExecutionCaches) -> list[dict]:
    """Drain only this call's private slots; never expose paths or remove originals.

    The ToolNode wrapper adds native issuing message/tool-call identities to each
    outcome. A failed output cannot discard another successfully captured output.
    """
    image, video = caches.pending_image, caches.pending_video
    caches.pending_image = None
    caches.pending_video = None
    outcomes = []
    for capture, value in ((_image, image), (_video, video)):
        if value is None:
            continue
        try:
            if caches.conversation_id != conversation_id:
                raise AttachmentError("action_denied")
            _conversation(conversation_id)
            result = capture(conversation_id, value)
            outcomes.append({"type": "media.available", "payload": {
                "media_ref": result["attachment_ref"], "mime_type": result["mime_type"],
            }})
        except (OSError, ValueError, TypeError) as error:
            # Only the transport-size failure is actionable in a public client;
            # path/format/authority failures share a redacted unavailable code.
            code = "payload_too_large" if isinstance(error, AttachmentError) and error.code == "payload_too_large" else "media_unavailable"
            outcomes.append({"type": "media.error", "payload": {"code": code}})
    return outcomes
