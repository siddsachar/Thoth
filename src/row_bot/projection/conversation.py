"""Serialized immutable public snapshots and independently replayable events."""

from __future__ import annotations

import copy
import json
import threading
import uuid
import logging
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def public_message(message: Any) -> dict | None:
    kind = str(getattr(message, "type", ""))
    if kind not in {"human", "ai", "tool"}:
        return None
    message_id = str(getattr(message, "id", "") or "")
    if not message_id:
        return None  # Legacy IDs require an explicit compare-write migration.
    content = getattr(message, "content", "")
    blocks = []
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = [{"type": "text", "text": str(b.get("text", ""))}
                  for b in content if isinstance(b, dict) and b.get("type") == "text"]
    role = {"human": "user", "ai": "assistant", "tool": "tool"}[kind]
    row_id = f"user:submission:{message_id}" if kind == "human" else f"{role}:checkpoint:{message_id}"
    return {"id": row_id, "message_id": message_id,
            "role": role, "blocks": blocks,
            "tool_call_ids": [str(t.get("id", "")) for t in getattr(message, "tool_calls", ())],
            "tool_call_id": str(getattr(message, "tool_call_id", "") or "")}


@dataclass
class _Conversation:
    revision: int = 0
    rows: list[dict] = field(default_factory=list)
    checkpoint_revision: str = ""
    generation: dict | None = None
    live_rows: dict[str, dict] = field(default_factory=dict)
    events: deque = field(default_factory=deque)
    event_bytes: int = 0
    content_bytes: int = 0


class ConversationProjection:
    MAX_EVENTS = 4096
    MAX_HISTORY_BYTES = 4 * 1024 * 1024
    MAX_EVENT_BYTES = 64 * 1024
    MAX_CONTENT_BYTES = 2 * 1024 * 1024
    MAX_GLOBAL_HISTORY_BYTES = 64 * 1024 * 1024
    MAX_GLOBAL_CONTENT_BYTES = 64 * 1024 * 1024

    def __init__(self, server_epoch: str) -> None:
        self.server_epoch = server_epoch
        self._lock = threading.RLock()
        self._states: dict[str, _Conversation] = {}
        self._listeners: dict[str, dict[str, Callable[[str, str], None]]] = {}
        self._pending_notifications: list[tuple[str, str]] = []

    @staticmethod
    def _content_size(rows: list[dict], live_rows: dict[str, dict]) -> int:
        if not rows and not live_rows:
            return 0
        return len(json.dumps([*rows, *live_rows.values()], ensure_ascii=False).encode("utf-8"))

    def _trim_global(self, current_id: str) -> None:
        history_size = sum(state.event_bytes for state in self._states.values())
        while history_size > self.MAX_GLOBAL_HISTORY_BYTES:
            candidates = [state for state in self._states.values() if state.events]
            if not candidates:
                break
            oldest = min(candidates, key=lambda state: state.events[0][2])
            _, size, _ = oldest.events.popleft()
            oldest.event_bytes -= size
            history_size -= size
        content_size = sum(state.content_bytes for state in self._states.values())
        for conversation_id, state in self._states.items():
            if content_size <= self.MAX_GLOBAL_CONTENT_BYTES:
                break
            if conversation_id == current_id or state.live_rows or (
                    state.generation and not state.generation.get("quiesced")):
                continue
            content_size -= state.content_bytes
            state.rows = []
            state.content_bytes = 0
            state.checkpoint_revision = ""  # Rehydrate from durable storage on the next query.
        changed = set()
        if content_size > self.MAX_GLOBAL_CONTENT_BYTES:
            for conversation_id, state in self._states.items():
                if content_size <= self.MAX_GLOBAL_CONTENT_BYTES:
                    break
                before = state.content_bytes
                state.rows = []
                state.checkpoint_revision = ""
                for row_id, row in state.live_rows.items():
                    row.update({"blocks": [], "content_status": "lazy", "content_ref": row_id.removeprefix("assistant:")})
                state.content_bytes = self._content_size(state.rows, state.live_rows)
                content_size -= before - state.content_bytes
                if before != state.content_bytes:
                    changed.add(conversation_id)
        # References are also materialized content. At saturation they can be
        # recreated on demand from the live content owner; the producer remains
        # registered and its complete text remains in that owner's spool.
        if content_size > self.MAX_GLOBAL_CONTENT_BYTES:
            for conversation_id, state in self._states.items():
                if content_size <= self.MAX_GLOBAL_CONTENT_BYTES:
                    break
                if not state.content_bytes:
                    continue
                content_size -= state.content_bytes
                state.rows.clear()
                state.live_rows.clear()
                state.content_bytes = 0
                state.checkpoint_revision = ""
                changed.add(conversation_id)
        # All content is within bounds before journaling resets, so reentrant
        # publication cannot recurse while eviction is partially applied.
        for conversation_id in changed:
            event = self.publish(conversation_id, "projection.reset", {"reason": "content_evicted"}, _notify_observers=False)
            self._pending_notifications.append((conversation_id, event["projection_revision"]))

    def subscribe(self, conversation_id: str, callback: Callable[[str, str], None]) -> Callable[[], None]:
        token = str(uuid.uuid4())
        with self._lock:
            self._listeners.setdefault(conversation_id, {})[token] = callback
        def unsubscribe() -> None:
            with self._lock:
                self._listeners.get(conversation_id, {}).pop(token, None)
        return unsubscribe

    def _notify(self, conversation_id: str, revision: str) -> None:
        with self._lock:
            notifications = [(conversation_id, revision), *self._pending_notifications]
            self._pending_notifications.clear()
            callbacks = [(target, value, tuple(self._listeners.get(target, {}).values())) for target, value in notifications]
        for target, value, listeners in callbacks:
            for callback in listeners:
                try:
                    callback(target, value)
                except Exception:
                    logging.getLogger(__name__).debug("Projection observer unavailable", exc_info=True)

    def publish(self, conversation_id: str, event_type: str, payload: dict, *, _notify_observers: bool = True) -> dict:
        with self._lock:
            state = self._states.setdefault(conversation_id, _Conversation())
            next_live = state.live_rows
            next_rows = state.rows
            if event_type == "transcript.delta":
                row_id = str(payload["row_id"])
                next_live = copy.deepcopy(state.live_rows)
                live = next_live.setdefault(row_id, {"id": row_id, "role": "assistant", "blocks": [{"type": "text", "text": ""}]})
                if live.get("content_status") != "lazy":
                    current = live["blocks"][0]["text"]
                    live["blocks"][0]["text"] = current + str(payload["public_text_delta"])
                live["render_revision"] = str(state.revision + 1)
                next_size = self._content_size(state.rows, next_live)
                active_size = sum(s.content_bytes for key, s in self._states.items()
                                  if key != conversation_id and (s.live_rows or (s.generation and not s.generation.get("quiesced"))))
                budget = min(self.MAX_CONTENT_BYTES, max(2, self.MAX_GLOBAL_CONTENT_BYTES - active_size))
                if next_size > budget:
                    live.update({"blocks": [], "content_status": "lazy",
                                 "content_ref": f"live:{payload['pass_id']}:{payload['segment_id']}"})
                    next_rows = list(state.rows)
                    while next_rows and self._content_size(next_rows, next_live) > budget:
                        next_rows.pop(0)
            # A failed publication cannot allocate a source sequence or revision.
            if len(json.dumps(payload, ensure_ascii=False).encode()) > self.MAX_EVENT_BYTES - 2048:
                raise ValueError("projection_event_limit")
            state.revision += 1
            event = {"event_id": str(uuid.uuid4()), "type": event_type,
                     "conversation_id": conversation_id, "server_epoch": self.server_epoch,
                     "projection_revision": str(state.revision), "payload": copy.deepcopy(payload)}
            event.update({"protocol_version": "1.0", "source": "runtime", "source_epoch": self.server_epoch,
                          "source_stream_id": conversation_id,
                          "source_sequence_start": str(state.revision), "source_sequence_end": str(state.revision)})
            encoded = len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
            if event_type == "generation.state":
                state.generation = copy.deepcopy(payload)
            if event_type == "transcript.delta":
                state.rows = next_rows
                state.live_rows = next_live
                state.content_bytes = self._content_size(state.rows, next_live)
            self._journal_sequence = getattr(self, "_journal_sequence", 0) + 1
            state.events.append((event, encoded, self._journal_sequence))
            state.event_bytes += encoded
            while len(state.events) > self.MAX_EVENTS or state.event_bytes > self.MAX_HISTORY_BYTES:
                _, size, _ = state.events.popleft()
                state.event_bytes -= size
            self._trim_global(conversation_id)
            result = copy.deepcopy(event)
        if _notify_observers:
            self._notify(conversation_id, result["projection_revision"])
        return result

    def install_checkpoint(self, conversation_id: str, checkpoint_revision: str,
                           messages: list) -> None:
        rows = [row for msg in messages[-100:] if (row := public_message(msg)) is not None]
        self.install_rows(conversation_id, checkpoint_revision, rows)

    def install_rows(self, conversation_id: str, checkpoint_revision: str, rows: list[dict]) -> None:
        """Install a bounded result from the durable checkpoint reader."""
        size = 0
        bounded = []
        for row in reversed(rows):
            encoded_size = len(json.dumps(row, ensure_ascii=False).encode())
            if encoded_size > self.MAX_CONTENT_BYTES // 2:
                row = {**row, "blocks": [], "content_status": "lazy", "content_ref": row["message_id"]}
                encoded_size = len(json.dumps(row).encode())
            if size + encoded_size > self.MAX_CONTENT_BYTES:
                break
            bounded.append(row)
            size += encoded_size
        with self._lock:
            state = self._states.setdefault(conversation_id, _Conversation())
            if state.checkpoint_revision == checkpoint_revision:
                return
            active_size = sum(s.content_bytes for key, s in self._states.items()
                              if key != conversation_id and (s.live_rows or (s.generation and not s.generation.get("quiesced"))))
            budget = min(self.MAX_CONTENT_BYTES, max(2, self.MAX_GLOBAL_CONTENT_BYTES - active_size))
            while bounded and self._content_size(list(reversed(bounded)), state.live_rows) > budget:
                bounded.pop()
            state.rows = list(reversed(bounded))
            state.content_bytes = self._content_size(state.rows, state.live_rows)
            state.checkpoint_revision = checkpoint_revision
            event = self.publish(conversation_id, "transcript.checkpoint", {"checkpoint_revision": checkpoint_revision}, _notify_observers=False)
        self._notify(conversation_id, event["projection_revision"])

    def snapshot(self, conversation_id: str) -> dict:
        with self._lock:
            state = self._states.setdefault(conversation_id, _Conversation())
            return copy.deepcopy({"conversation_id": conversation_id, "server_epoch": self.server_epoch,
                                  "projection_revision": str(state.revision), "cursor": str(state.revision),
                                  "checkpoint_revision": state.checkpoint_revision,
                                  "rows": [*state.rows, *state.live_rows.values()], "generation": state.generation})

    def settle_live(self, conversation_id: str, row_id: str, *, adopted: bool) -> None:
        with self._lock:
            state = self._states.setdefault(conversation_id, _Conversation())
            if state.live_rows.pop(row_id, None) is None:
                return
            state.content_bytes = self._content_size(state.rows, state.live_rows)
            event = self.publish(conversation_id, "transcript.settled", {"row_id": row_id,
                         "adoption": "exact" if adopted else "no_adoption"}, _notify_observers=False)
        self._notify(conversation_id, event["projection_revision"])

    def events_since(self, conversation_id: str, cursor: str | None = None) -> dict:
        with self._lock:
            state = self._states.setdefault(conversation_id, _Conversation())
            try:
                after = int(cursor) if cursor is not None else state.revision
            except (TypeError, ValueError):
                after = -1
            first = int(state.events[0][0]["projection_revision"]) if state.events else state.revision + 1
            reset = after < first - 1 or after > state.revision
            return {"events": [] if reset else copy.deepcopy([
                        event for event, _, _ in state.events if int(event["projection_revision"]) > after]),
                    "snapshot_required": reset, "cursor": str(state.revision),
                    "server_epoch": self.server_epoch}


from row_bot.runtime.executions import generation_registry

conversation_projection = ConversationProjection(generation_registry.server_epoch)
