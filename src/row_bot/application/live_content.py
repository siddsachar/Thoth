"""Ephemeral lazy public text over the existing conversation media root.

This is a bounded display spool, not a durable transcript or admission store.
Checkpoint settlement owns retirement; restart discards every spool and cursor.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import tempfile
import threading
from collections.abc import Callable, Iterator
from typing import BinaryIO

PREFIX = b'[{"type":"text","text":"'
SUFFIX = b'"}]'
MAX_SPOOL_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024


class LiveContentError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class _Spool:
    file: BinaryIO
    revision: int
    size: int


def _encoded_parts(text: str) -> Iterator[bytes]:
    # At most 24KiB of JSON scratch, including worst-case control escaping.
    for offset in range(0, len(text), 4096):
        yield json.dumps(text[offset:offset + 4096], ensure_ascii=False)[1:-1].encode("utf-8")


class LiveContentStore:
    def __init__(self, *, root: Path | None = None, validate: Callable[[str], None] | None = None) -> None:
        self._root = root
        self._validate = validate
        self._lock = threading.RLock()
        self._spools: dict[tuple[str, str], _Spool] = {}
        self._key = secrets.token_bytes(32)
        self._total = 0

    def _owner(self, conversation_id: str, content_ref: str) -> Path:
        from row_bot.application.attachments import _conversation, _safe_path
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", conversation_id)
                or not re.fullmatch(r"live:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+", content_ref)
                or len(content_ref) > 256):
            raise LiveContentError("not_found")
        (self._validate or _conversation)(conversation_id)
        root = self._root
        if root is None:
            from row_bot import threads
            root = threads._MEDIA_DIR
        return _safe_path(root, root / conversation_id)

    def append(self, conversation_id: str, content_ref: str, text: str) -> None:
        with self._lock:
            folder = self._owner(conversation_id, content_ref)
            key = (conversation_id, content_ref)
            spool = self._spools.get(key)
            additional = sum(len(part) for part in _encoded_parts(text))
            base = spool.size if spool else len(PREFIX) + len(SUFFIX)
            growth = additional + (0 if spool else base)
            if base + additional > MAX_SPOOL_BYTES or self._total + growth > MAX_TOTAL_BYTES:
                raise LiveContentError("projection_storage_limit")
            if spool is None:
                folder.mkdir(parents=True, exist_ok=True)
                self._owner(conversation_id, content_ref)
                file = tempfile.TemporaryFile(mode="w+b", dir=folder, prefix="live_")
                file.write(PREFIX + SUFFIX)
                spool = _Spool(file, 0, base)
                self._spools[key] = spool
                self._total += base
            old_size = spool.size
            try:
                spool.file.seek(old_size - len(SUFFIX))
                for part in _encoded_parts(text):
                    spool.file.write(part)
                spool.file.write(SUFFIX)
                spool.file.flush()
            except BaseException:
                spool.file.seek(old_size - len(SUFFIX))
                spool.file.write(SUFFIX)
                spool.file.truncate(old_size)
                raise
            spool.size += additional
            spool.revision += 1
            self._total += additional

    def _cursor(self, conversation_id: str, content_ref: str, spool: _Spool, offset: int) -> str:
        body = json.dumps([conversation_id, content_ref, spool.revision, spool.size, offset], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + hmac.new(self._key, body, hashlib.sha256).hexdigest()

    def read_page(self, conversation_id: str, content_ref: str, limit_bytes: int = 65536,
                  cursor: str | None = None) -> dict:
        with self._lock:
            self._owner(conversation_id, content_ref)
            spool = self._spools.get((conversation_id, content_ref))
            if spool is None:
                raise LiveContentError("cursor_expired" if cursor else "not_found")
            if not 1 <= limit_bytes <= 65536:
                raise LiveContentError("invalid_command")
            offset = 0
            if cursor:
                try:
                    if len(cursor) > 2048:
                        raise ValueError()
                    token, signature = cursor.split(".")
                    body = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
                    if not hmac.compare_digest(signature, hmac.new(self._key, body, hashlib.sha256).hexdigest()):
                        raise ValueError()
                    value = json.loads(body)
                    if value[:4] != [conversation_id, content_ref, spool.revision, spool.size]:
                        raise ValueError()
                    offset = value[4]
                    if type(offset) is not int or not 0 <= offset <= spool.size:
                        raise ValueError()
                except (ValueError, TypeError, IndexError):
                    raise LiveContentError("cursor_expired") from None
            spool.file.seek(offset)
            data = spool.file.read(limit_bytes)
            following = offset + len(data)
            more = following < spool.size
            return {"conversation_id": conversation_id, "content_ref": content_ref, "checkpoint_revision": "",
                    "encoding": "base64", "media_type": "application/json", "data": base64.b64encode(data).decode(),
                    "has_more": more,
                    "next_cursor": self._cursor(conversation_id, content_ref, spool, following) if more else None}

    def discard(self, conversation_id: str, content_ref: str) -> None:
        with self._lock:
            spool = self._spools.pop((conversation_id, content_ref), None)
            if spool is not None:
                self._total -= spool.size
                spool.file.close()

    def references(self, conversation_id: str) -> list[str]:
        with self._lock:
            return sorted(reference for conversation, reference in self._spools if conversation == conversation_id)

    def close(self) -> None:
        with self._lock:
            for spool in self._spools.values():
                spool.file.close()
            self._spools.clear()
            self._total = 0


live_content_store = LiveContentStore()


def append(conversation_id: str, content_ref: str, text: str) -> None:
    live_content_store.append(conversation_id, content_ref, text)


def read_page(conversation_id: str, content_ref: str, limit_bytes: int = 65536, cursor: str | None = None) -> dict:
    return live_content_store.read_page(conversation_id, content_ref, limit_bytes, cursor)


def discard(conversation_id: str, content_ref: str) -> None:
    live_content_store.discard(conversation_id, content_ref)


def references(conversation_id: str) -> list[str]:
    return live_content_store.references(conversation_id)


def close() -> None:
    live_content_store.close()
