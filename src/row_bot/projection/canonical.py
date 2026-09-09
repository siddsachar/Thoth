"""Exact comparison validates an already named output; it never discovers one."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class OutputBinding:
    pass_id: str
    segment_id: str
    live_revision: str
    native_message_id: str
    checkpoint_revision: str
    canonical_version: str = "1"


def canonical_assistant_v1(value: Any) -> Iterator[bytes]:
    """Yield <=64 KiB canonical chunks, preserving public order and whitespace."""
    def tokens(item: Any) -> Iterator[bytes]:
        if isinstance(item, str):
            yield b'"'
            # Escape only bounded slices; JSONEncoder.iterencode may emit an
            # entire multi-megabyte string as one allocation.
            for offset in range(0, len(item), 512):
                yield json.dumps(item[offset:offset + 512], ensure_ascii=False)[1:-1].encode("utf-8")
            yield b'"'
        elif isinstance(item, (list, tuple)):
            yield b"["
            for index, child in enumerate(item):
                if index:
                    yield b","
                yield from tokens(child)
            yield b"]"
        elif isinstance(item, dict):
            yield b"{"
            for index, key in enumerate(sorted(item)):
                if index:
                    yield b","
                yield from tokens(str(key))
                yield b":"
                yield from tokens(item[key])
            yield b"}"
        else:
            yield json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()

    pending = bytearray()
    for piece in tokens(value):
        offset = 0
        while offset < len(piece):
            take = min(8192 - len(pending), len(piece) - offset)
            pending.extend(piece[offset:offset + take])
            offset += take
            if len(pending) == 8192:
                yield bytes(pending)
                pending.clear()
    if pending:
        yield bytes(pending)


def exact_assistant_equal(left: Any, right: Any) -> bool:
    sentinel = object()
    return all(a == b for a, b in itertools.zip_longest(
        canonical_assistant_v1(left), canonical_assistant_v1(right), fillvalue=sentinel))
