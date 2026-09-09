"""Bounded public reads of the existing SQLite MessagePack checkpoint blob.

The checkpoint remains the only transcript store. This reader skips unrelated
values by encoded length and never instantiates persisted Python objects.
"""
from __future__ import annotations

import codecs
import json
from contextlib import contextmanager
from dataclasses import dataclass
from collections.abc import Iterator
from typing import Any


@dataclass(frozen=True)
class Node:
    kind: str
    body: int
    size: int
    code: int = 0


class BlobReader:
    MAX_NODES = 2_000_000

    def __init__(self, blob: Any, revision: str) -> None:
        self.blob = blob
        self.revision = revision
        self._nodes = 0

    def _read(self, size: int) -> bytes:
        value = self.blob.read(size)
        if len(value) != size:
            raise ValueError("checkpoint_format_invalid")
        return value

    def node(self, position: int) -> Node:
        self._nodes += 1
        if self._nodes > self.MAX_NODES:
            raise ValueError("checkpoint_read_limit")
        self.blob.seek(position)
        tag = self._read(1)[0]
        if tag <= 0x7f or tag >= 0xe0 or tag in {0xc0, 0xc2, 0xc3}:
            return Node("scalar", position, 1)
        if 0xa0 <= tag <= 0xbf:
            return Node("str", self.blob.tell(), tag & 31)
        if 0x90 <= tag <= 0x9f:
            return Node("array", self.blob.tell(), tag & 15)
        if 0x80 <= tag <= 0x8f:
            return Node("map", self.blob.tell(), tag & 15)
        sizes = {0xc4: ("bin", 1), 0xc5: ("bin", 2), 0xc6: ("bin", 4),
                 0xd9: ("str", 1), 0xda: ("str", 2), 0xdb: ("str", 4),
                 0xdc: ("array", 2), 0xdd: ("array", 4), 0xde: ("map", 2), 0xdf: ("map", 4),
                 0xc7: ("ext", 1), 0xc8: ("ext", 2), 0xc9: ("ext", 4)}
        if tag in sizes:
            kind, width = sizes[tag]
            size = int.from_bytes(self._read(width), "big")
            code = self._read(1)[0] if kind == "ext" else 0
            return Node(kind, self.blob.tell(), size, code)
        if 0xd4 <= tag <= 0xd8:
            code = self._read(1)[0]
            return Node("ext", self.blob.tell(), 1 << (tag - 0xd4), code)
        widths = {0xca: 4, 0xcb: 8, 0xcc: 1, 0xcd: 2, 0xce: 4, 0xcf: 8,
                  0xd0: 1, 0xd1: 2, 0xd2: 4, 0xd3: 8}
        if tag in widths:
            return Node("scalar", position, 1 + widths[tag])
        raise ValueError("checkpoint_format_invalid")

    def end(self, position: int, depth: int = 0) -> int:
        if depth > 64:
            raise ValueError("checkpoint_format_invalid")
        node = self.node(position)
        if node.kind not in {"map", "array"}:
            return node.body + node.size
        position = node.body
        for _ in range(node.size * (2 if node.kind == "map" else 1)):
            position = self.end(position, depth + 1)
        return position

    def text(self, position: int, maximum: int = 1024) -> str:
        node = self.node(position)
        if node.kind != "str" or node.size > maximum:
            return ""
        self.blob.seek(node.body)
        return self._read(node.size).decode("utf-8")

    def fields(self, position: int, wanted: set[str]) -> dict[str, int]:
        node = self.node(position)
        if node.kind != "map":
            return {}
        found = {}
        position = node.body
        for _ in range(node.size):
            key = self.text(position)
            position = self.end(position)
            if key in wanted:
                found[key] = position
            position = self.end(position)
        return found

    def records(self, *, start: int = 0) -> Iterator[tuple[int, dict]]:
        root = self.fields(0, {"channel_values"})
        channel = self.fields(root["channel_values"], {"messages"}) if "channel_values" in root else {}
        if "messages" not in channel:
            return
        messages = self.node(channel["messages"])
        if messages.kind != "array":
            raise ValueError("checkpoint_format_invalid")
        position = messages.body
        first = max(0, messages.size + start) if start < 0 else start
        for index in range(messages.size):
            end = self.end(position)
            if index >= first:
                record = self.message(position)
                if record:
                    yield index, record
            position = end

    def message(self, position: int) -> dict | None:
        node = self.node(position)
        if node.kind != "ext" or node.code not in {4, 5}:
            return None
        envelope = self.node(node.body)
        if envelope.kind != "array" or envelope.size < 3:
            return None
        module = self.text(envelope.body)
        class_position = self.end(envelope.body)
        name = self.text(class_position)
        roles = {"HumanMessage": "user", "AIMessage": "assistant", "ToolMessage": "tool"}
        if not module.startswith("langchain_core.messages.") or name not in roles:
            return None
        fields = self.fields(self.end(class_position), {"id", "content", "tool_calls", "tool_call_id"})
        identity = self.text(fields["id"]) if "id" in fields else ""
        if not identity:
            raise ValueError("checkpoint_identity_migration_required")
        tool_ids = []
        tool_ids_lazy = False
        tool_id_bytes = 0
        if "tool_calls" in fields:
            calls = self.node(fields["tool_calls"])
            current = calls.body
            if calls.kind == "array":
                for _ in range(calls.size):
                    call = self.fields(current, {"id"})
                    if "id" in call:
                        identity_text = self.text(call["id"])
                        tool_ids.append(identity_text)
                        tool_id_bytes += len(identity_text.encode())
                        if len(tool_ids) > 128 or tool_id_bytes > 8192:
                            tool_ids, tool_ids_lazy = [], True
                            break
                    current = self.end(current)
        return {"message_id": identity, "role": roles[name], "content_position": fields.get("content"),
                "tool_call_ids": tool_ids, "tool_calls_position": fields.get("tool_calls"), "tool_ids_lazy": tool_ids_lazy,
                "tool_call_id": self.text(fields["tool_call_id"]) if "tool_call_id" in fields else ""}

    def _json_string(self, position: int) -> Iterator[bytes]:
        node = self.node(position)
        if node.kind != "str":
            yield b'""'
            return
        yield b'"'
        decoder = codecs.getincrementaldecoder("utf-8")()
        offset, remaining = node.body, node.size
        while remaining:
            size = min(512, remaining)
            self.blob.seek(offset)
            text = decoder.decode(self._read(size), final=size == remaining)
            yield json.dumps(text, ensure_ascii=False)[1:-1].encode("utf-8")
            offset += size
            remaining -= size
        yield b'"'

    def content_chunks(self, record: dict) -> Iterator[bytes]:
        position = record.get("content_position")
        yield b"["
        if position is not None:
            content = self.node(position)
            def positions() -> Iterator[int]:
                if content.kind == "str":
                    yield position
                elif content.kind == "array":
                    current = content.body
                    for _ in range(content.size):
                        fields = self.fields(current, {"type", "text"})
                        if "type" in fields and self.text(fields["type"]) == "text" and "text" in fields:
                            yield fields["text"]
                        current = self.end(current)
            for index, text_position in enumerate(positions()):
                if index:
                    yield b","
                yield b'{"type":"text","text":'
                yield from self._json_string(text_position)
                yield b"}"
        yield b"]"

    def text_only(self, record: dict) -> bool:
        """Whether public text blocks represent the complete native content."""
        position = record.get("content_position")
        if position is None:
            return False
        content = self.node(position)
        if content.kind == "str":
            return True
        if content.kind != "array":
            return False
        current = content.body
        for _ in range(content.size):
            block = self.node(current)
            if block.kind != "map" or block.size != 2:
                return False
            fields = self.fields(current, {"type", "text"})
            if (set(fields) != {"type", "text"} or self.text(fields["type"]) != "text"
                    or self.node(fields["text"]).kind != "str"):
                return False
            current = self.end(current)
        return True

    def tool_ids_chunks(self, record: dict) -> Iterator[bytes]:
        yield b"["
        position = record.get("tool_calls_position")
        emitted = False
        if position is not None:
            calls = self.node(position)
            current = calls.body
            if calls.kind == "array":
                for _ in range(calls.size):
                    fields = self.fields(current, {"id"})
                    if "id" in fields:
                        if emitted:
                            yield b","
                        yield from self._json_string(fields["id"])
                        emitted = True
                    current = self.end(current)
        yield b"]"

    def public_row(self, record: dict, maximum: int = 128 * 1024) -> dict:
        role, identity = record["role"], record["message_id"]
        row = {"id": f"user:submission:{identity}" if role == "user" else f"{role}:checkpoint:{identity}",
               "message_id": identity, "role": role, "tool_call_ids": record["tool_call_ids"],
               "tool_call_id": record["tool_call_id"], "blocks": []}
        if record["tool_ids_lazy"]:
            row["tool_calls_ref"] = identity + ":tool_calls"
        content = bytearray()
        for chunk in self.content_chunks(record):
            if len(content) + len(chunk) > maximum:
                return {**row, "content_status": "lazy", "content_ref": identity}
            content.extend(chunk)
        row["blocks"] = json.loads(content)
        return row


@contextmanager
def open_checkpoint(conversation_id: str, revision: str = "") -> Iterator[BlobReader | None]:
    from row_bot import threads
    saver = threads.checkpointer
    with saver.cursor(transaction=False) as cursor:
        query = "SELECT rowid,checkpoint_id,type FROM checkpoints WHERE thread_id=? AND checkpoint_ns=''"
        params = [conversation_id]
        if revision:
            query += " AND checkpoint_id=?"
            params.append(revision)
        row = cursor.execute(query + " ORDER BY checkpoint_id DESC LIMIT 1", params).fetchone()
        if not row:
            yield None
            return
        if row[2] != "msgpack":
            raise ValueError("checkpoint_format_unsupported")
        with saver.conn.blobopen("checkpoints", "checkpoint", int(row[0]), readonly=True) as blob:
            yield BlobReader(blob, str(row[1]))
