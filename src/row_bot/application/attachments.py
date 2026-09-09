"""Opaque attachment references over the existing conversation media owner."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO
from uuid import UUID, uuid4

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
UPLOAD_BATCH_BYTES = 100 * 1024 * 1024
UPLOAD_TTL_SECONDS = 1800
_LOCK = threading.RLock()
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class AttachmentError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _conversation(conversation_id: str) -> None:
    from row_bot import threads
    from row_bot.runtime import admissions
    import sqlite3
    if not _ID.fullmatch(conversation_id):
        raise AttachmentError("not_found")
    threads._ensure_thread_db()
    with sqlite3.connect(threads.DB_PATH) as conn:
        if not conn.execute("SELECT 1 FROM thread_meta WHERE thread_id=?", (conversation_id,)).fetchone():
            raise AttachmentError("not_found")
    if threads._thread_write_blocked(conversation_id) or admissions.deletion_state(conversation_id) != "active":
        raise AttachmentError("action_denied")


def _safe_path(root: Path, path: Path) -> Path:
    root = root.absolute()
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise AttachmentError("action_denied")
    try:
        relative = path.absolute().relative_to(root)
    except ValueError:
        raise AttachmentError("action_denied") from None
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise AttachmentError("action_denied")
    if not path.resolve().is_relative_to(root.resolve()):
        raise AttachmentError("action_denied")
    return path


def _managed_root(path: Path) -> Path:
    """Canonicalize trusted ancestry while rejecting a replaced managed root."""
    root = path.absolute()
    _safe_path(root, root)
    canonical = root.parent.resolve() / root.name
    return _safe_path(canonical, canonical)


def _open(root: Path, path: Path) -> BinaryIO:
    _safe_path(root, path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
    try:
        _safe_path(root, path)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, path.stat()):
            raise AttachmentError("action_denied")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _write(root: Path, path: Path, data: bytes) -> None:
    """Create immutable media bytes only after checking the opened file identity."""
    _safe_path(root, path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        _safe_path(root, path)
        if not os.path.samestat(os.fstat(descriptor), path.stat()):
            raise AttachmentError("action_denied")
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def register_attachment(conversation_id: str, name: str, data: bytes,
                        mime_type: str = "application/octet-stream") -> dict:
    """Persist one bounded immutable file and its reference in thread media."""
    from row_bot import threads
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError("payload_too_large")
    if (not name or len(name.encode("utf-8")) > 240 or name in {".", ".."}
            or any(c in name for c in '/\\:\x00\r\n')):
        raise AttachmentError("invalid_command")
    with _LOCK:
        _conversation(conversation_id)
        media_root = threads._MEDIA_DIR
        folder = media_root / conversation_id
        _safe_path(media_root, folder)
        folder.mkdir(parents=True, exist_ok=True)
        _safe_path(media_root, folder)
        attachment_id = str(uuid4())
        ref = f"{conversation_id}:{attachment_id}"
        filename = f"attachment_{attachment_id}.bin"
        metadata_name = f"attachment_{attachment_id}.json"
        # Active formats are always downloaded with attachment disposition;
        # content type is sniffed, never a client's authority claim.
        detected = ("image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else
                    "image/jpeg" if data.startswith(b"\xff\xd8\xff") else
                    "video/mp4" if len(data) >= 12 and data[4:8] == b"ftyp" else
                    "application/pdf" if data.startswith(b"%PDF-") else "application/octet-stream")
        metadata = {"attachment_ref": ref, "name": name, "mime_type": detected,
                    "size_bytes": len(data), "revision": "1",
                    "sha256": hashlib.sha256(data).hexdigest()}
        _safe_path(media_root, folder / filename)
        _write(media_root, folder / filename, data)
        _conversation(conversation_id)
        _safe_path(media_root, folder / metadata_name)
        _write(media_root, folder / metadata_name, json.dumps(metadata).encode())
        return {key: value for key, value in metadata.items() if key != "sha256"}


def _metadata(root: Path, folder: Path, attachment_id: str, reference: str) -> dict:
    try:
        with _open(root, folder / f"attachment_{attachment_id}.json") as source:
            raw = source.read(8193)
        if len(raw) > 8192:
            raise ValueError()
        metadata = json.loads(raw)
        if (not isinstance(metadata, dict)
                or set(metadata) != {"attachment_ref", "name", "mime_type", "size_bytes", "revision", "sha256"}
                or metadata["attachment_ref"] != reference
                or type(metadata["size_bytes"]) is not int
                or not 1 <= metadata["size_bytes"] <= MAX_ATTACHMENT_BYTES
                or metadata["revision"] != "1"
                or not isinstance(metadata["name"], str)
                or not metadata["name"] or len(metadata["name"].encode("utf-8")) > 240
                or metadata["name"] in {".", ".."}
                or any(c in metadata["name"] for c in '/\\:\x00\r\n')
                or not isinstance(metadata["mime_type"], str)
                or metadata["mime_type"] not in {"image/png", "image/jpeg", "video/mp4", "application/pdf", "application/octet-stream"}
                or not isinstance(metadata["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"])):
            raise ValueError()
        return metadata
    except (OSError, ValueError, TypeError):
        raise AttachmentError("not_found") from None


def read_attachment(reference: str) -> tuple[dict, bytes]:
    """Authorize existence and validate the file at open, without exposing paths."""
    from row_bot import threads
    try:
        conversation_id, value = reference.rsplit(":", 1)
        attachment_id = str(UUID(value))
    except (ValueError, AttributeError):
        raise AttachmentError("not_found") from None
    with _LOCK:
        _conversation(conversation_id)
        root = threads._MEDIA_DIR
        folder = root / conversation_id
        metadata = _metadata(root, folder, attachment_id, reference)
        try:
            with _open(root, folder / f"attachment_{attachment_id}.bin") as source:
                data = source.read(MAX_ATTACHMENT_BYTES + 1)
        except (OSError, ValueError):
            raise AttachmentError("not_found") from None
        if (metadata.get("attachment_ref") != reference or len(data) > MAX_ATTACHMENT_BYTES
                or metadata.get("size_bytes") != len(data)
                or metadata.get("sha256") != hashlib.sha256(data).hexdigest()):
            raise AttachmentError("revision_conflict")
        return {k: metadata[k] for k in ("attachment_ref", "name", "mime_type", "size_bytes", "revision")}, data


def inspect_attachment(reference: str) -> dict:
    """Bounded metadata/size preflight; full consumption still checks the hash."""
    from row_bot import threads
    try:
        conversation_id, value = reference.rsplit(":", 1)
        attachment_id = str(UUID(value))
    except (ValueError, AttributeError):
        raise AttachmentError("not_found") from None
    with _LOCK:
        _conversation(conversation_id)
        root = threads._MEDIA_DIR
        folder = root / conversation_id
        metadata = _metadata(root, folder, attachment_id, reference)
        try:
            with _open(root, folder / f"attachment_{attachment_id}.bin") as source:
                size = os.fstat(source.fileno()).st_size
            if metadata["size_bytes"] != size:
                raise AttachmentError("revision_conflict")
            return {k: metadata[k] for k in ("attachment_ref", "name", "mime_type", "size_bytes", "revision")}
        except OSError:
            raise AttachmentError("not_found") from None


@dataclass
class _Upload:
    id: str
    session_id: str
    conversation_id: str
    batch_id: str
    name: str
    size: int
    digest: str
    source: BinaryIO
    expires: float
    chunks: dict[int, str] = field(default_factory=dict)
    received: int = 0


class AttachmentUploads:
    """Ephemeral staging in the media owner's root, never a second durable store.

    TemporaryFile owns unlink-on-close/process-exit cleanup. Restart deliberately
    loses the upload IDs; committed references remain in the existing media owner.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self._uploads: dict[str, _Upload] = {}
        self._inflight: dict[str, int] = {}

    def _prune(self) -> None:
        for identifier, upload in tuple(self._uploads.items()):
            if upload.expires <= self.clock():
                upload.source.close()
                del self._uploads[identifier]

    def _get(self, session_id: str, upload_id: str) -> _Upload:
        self._prune()
        upload = self._uploads.get(upload_id)
        if upload is None:
            raise AttachmentError("upload_expired")
        if upload.session_id != session_id:
            raise AttachmentError("not_found")
        _conversation(upload.conversation_id)
        return upload

    def _view(self, upload: _Upload) -> dict:
        return {"upload_id": upload.id, "size_bytes": upload.size,
                "received_bytes": upload.received, "chunk_bytes": UPLOAD_CHUNK_BYTES,
                "expires_in_seconds": max(0, int(upload.expires - self.clock()))}

    def create(self, session_id: str, *, conversation_id: str, batch_id: str,
               name: str, size_bytes: int, sha256: str) -> dict:
        from row_bot import threads
        with self._lock:
            self._prune()
            _conversation(conversation_id)
            if (not 1 <= size_bytes <= MAX_ATTACHMENT_BYTES or not re.fullmatch(r"[a-f0-9]{64}", sha256)
                    or not name or len(name.encode()) > 240 or any(c in name for c in '/\\:\x00\r\n')):
                raise AttachmentError("invalid_command")
            batch_size = sum(u.size for u in self._uploads.values()
                             if u.session_id == session_id and u.batch_id == batch_id)
            if batch_size + size_bytes > UPLOAD_BATCH_BYTES:
                raise AttachmentError("payload_too_large")
            # Bounded reservations stop unfilled uploads exhausting local storage.
            if len(self._uploads) >= 128 or sum(u.size for u in self._uploads.values()) + size_bytes > 1024 ** 3:
                raise AttachmentError("rate_limited")
            folder = threads._MEDIA_DIR / conversation_id
            _safe_path(threads._MEDIA_DIR, folder)
            folder.mkdir(parents=True, exist_ok=True)
            _safe_path(threads._MEDIA_DIR, folder)
            source = tempfile.TemporaryFile(mode="w+b", dir=folder, prefix="upload_")
            upload = _Upload(str(uuid4()), session_id, conversation_id, batch_id, name,
                             size_bytes, sha256, source, self.clock() + UPLOAD_TTL_SECONDS)
            self._uploads[upload.id] = upload
            return self._view(upload)

    def status(self, session_id: str, upload_id: str) -> dict:
        with self._lock:
            return self._view(self._get(session_id, upload_id))

    def expire(self) -> None:
        with self._lock:
            self._prune()

    def enter_chunk(self, session_id: str, upload_id: str) -> None:
        with self._lock:
            self._get(session_id, upload_id)
            self.enter_transfer(session_id)

    def enter_transfer(self, session_id: str) -> None:
        with self._lock:
            if self._inflight.get(session_id, 0) >= 4 or sum(self._inflight.values()) >= 32:
                raise AttachmentError("rate_limited")
            self._inflight[session_id] = self._inflight.get(session_id, 0) + 1

    def leave_chunk(self, session_id: str) -> None:
        with self._lock:
            count = self._inflight.get(session_id, 0) - 1
            if count > 0:
                self._inflight[session_id] = count
            else:
                self._inflight.pop(session_id, None)

    def write(self, session_id: str, upload_id: str, offset: int, data: bytes) -> dict:
        with self._lock:
            upload = self._get(session_id, upload_id)
            if (offset < 0 or offset % UPLOAD_CHUNK_BYTES or offset >= upload.size
                    or len(data) != min(UPLOAD_CHUNK_BYTES, upload.size - offset)):
                raise AttachmentError("invalid_command")
            digest = hashlib.sha256(data).hexdigest()
            old = upload.chunks.get(offset)
            if old is not None and old != digest:
                raise AttachmentError("revision_conflict")
            if old is None:
                upload.source.seek(offset)
                upload.source.write(data)
                upload.chunks[offset] = digest
                upload.received += len(data)
            upload.expires = self.clock() + UPLOAD_TTL_SECONDS
            return self._view(upload)

    def complete(self, session_id: str, upload_id: str, commit: Callable[..., dict]) -> dict:
        with self._lock:
            upload = self._get(session_id, upload_id)
            if upload.received != upload.size:
                raise AttachmentError("upload_incomplete")
            upload.source.seek(0)
            data = upload.source.read(MAX_ATTACHMENT_BYTES + 1)
            if len(data) != upload.size or hashlib.sha256(data).hexdigest() != upload.digest:
                raise AttachmentError("revision_conflict")
            result = commit(conversation_id=upload.conversation_id, name=upload.name,
                            data=data, mime_type="application/octet-stream")
            # Retain immutable staging until TTL for safe response-loss retries.
            upload.expires = self.clock() + UPLOAD_TTL_SECONDS
            return result

    def cancel(self, session_id: str, upload_id: str) -> None:
        with self._lock:
            upload = self._get(session_id, upload_id)
            upload.source.close()
            del self._uploads[upload.id]

    def close(self) -> None:
        with self._lock:
            for upload in self._uploads.values():
                upload.source.close()
            self._uploads.clear()
            self._inflight.clear()
