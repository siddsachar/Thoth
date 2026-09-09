"""Helpers for bounded, stale-safe chat transcript rendering."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable


LARGE_TRANSCRIPT_THRESHOLD = 80
TRANSCRIPT_WINDOW_SIZE = 60
TRANSCRIPT_CHUNK_TARGET_MS = 16.0
TRANSCRIPT_MAX_CHUNK_MESSAGES = 10


@dataclass(frozen=True)
class TranscriptWindow:
    start: int
    end: int
    total: int

    @property
    def older_count(self) -> int:
        return max(0, self.start)

    @property
    def visible_count(self) -> int:
        return max(0, self.end - self.start)


def message_key(index: int, msg: dict) -> str:
    """Return a compact stable key for one rendered message position."""
    role = str(msg.get("role", ""))
    timestamp = str(msg.get("timestamp", ""))
    content = msg.get("content", "")
    if isinstance(content, list):
        content_text = " ".join(str(item) for item in content)
    else:
        content_text = str(content or "")
    shape = "|".join(
        [
            role,
            timestamp,
            content_text[:512],
            str(len(content_text)),
            str(len(msg.get("tool_results") or [])),
            str(len(msg.get("images") or [])),
            str(len(msg.get("videos") or [])),
            str(len(msg.get("charts") or [])),
            str(len(str(msg.get("thinking") or ""))),
            str(len(msg.get("agent_run_ids") or [])),
            str(msg.get("agent_run_refresh_key") or ""),
            str((msg.get("queued_control") or {}).get("status") or ""),
            str((msg.get("queued_control") or {}).get("label") or ""),
            str(msg.get("approval_status") or ""),
            str(bool(msg.get("approval_resume_token"))),
        ]
    )
    digest = hashlib.sha1(shape.encode("utf-8", errors="replace")).hexdigest()[:12]
    message_id = str(msg.get("checkpoint_message_id") or msg.get("message_id") or "")
    if message_id:
        # Identity comes from the checkpoint; the digest only invalidates the
        # rendered representation when annotations or tool/media state change.
        return f"{role}:checkpoint:{message_id}:render:{digest}"
    return f"{index}:{role}:{digest}"


def message_keys(messages: Iterable[dict], *, start: int = 0) -> list[str]:
    return [message_key(idx, msg) for idx, msg in enumerate(messages, start=start)]


def choose_transcript_window(
    total: int,
    *,
    requested_start: int | None = None,
    threshold: int = LARGE_TRANSCRIPT_THRESHOLD,
    window_size: int = TRANSCRIPT_WINDOW_SIZE,
) -> TranscriptWindow:
    """Choose the initial visible transcript window.

    Small transcripts render fully. Large transcripts render the latest window
    unless the user has explicitly requested older history.
    """
    if total <= threshold:
        return TranscriptWindow(start=0, end=total, total=total)

    default_start = max(0, total - window_size)
    if requested_start is None:
        start = default_start
    else:
        start = max(0, min(requested_start, default_start))
    return TranscriptWindow(start=start, end=total, total=total)


def reset_transcript_request(p: object, thread_id: str | None) -> None:
    if getattr(p, "transcript_requested_thread_id", None) != thread_id:
        setattr(p, "transcript_requested_thread_id", thread_id)
        setattr(p, "transcript_requested_start", None)


def rendered_window_matches(
    rendered_keys: list[str],
    all_keys: list[str],
    *,
    start: int,
) -> bool:
    end = start + len(rendered_keys)
    if start < 0 or end > len(all_keys):
        return False
    return rendered_keys == all_keys[start:end]


def common_key_prefix(left: list[str], right: list[str]) -> int:
    """Return the number of leading message keys shared by two render states."""

    count = 0
    for left_key, right_key in zip(left, right):
        if left_key != right_key:
            break
        count += 1
    return count


def transcript_message_child_bounds(
    child_count: int,
    rendered_message_count: int,
    preserved_message_count: int,
) -> tuple[int, int]:
    """Locate rendered message children after any transcript-prefix controls."""

    rendered_count = max(0, int(rendered_message_count or 0))
    prefix_controls = max(0, int(child_count or 0) - rendered_count)
    preserved = max(0, min(int(preserved_message_count or 0), rendered_count))
    return prefix_controls + preserved, prefix_controls + rendered_count


def durable_message_key(msg: dict) -> str:
    """Return the stable identity of a backend-published UI message."""

    message_id = str(msg.get("checkpoint_message_id") or msg.get("message_id") or "")
    if message_id:
        return f"checkpoint:{message_id}"
    channel_key = str(msg.get("channel_notification_key") or "").strip()
    if channel_key:
        return f"channel:{channel_key}"
    approval_id = str(msg.get("approval_request_id") or "").strip()
    if approval_id:
        return f"approval:{approval_id}"
    orchestration_id = str(msg.get("orchestration_id") or "").strip()
    orchestration_kind = str(
        msg.get("orchestration_message_kind") or ""
    ).strip()
    if orchestration_id and orchestration_kind:
        return f"orchestration:{orchestration_id}:{orchestration_kind}"
    lifecycle = msg.get("agent_lifecycle")
    if isinstance(lifecycle, dict):
        run_id = str(lifecycle.get("run_id") or "").strip()
        if run_id:
            return f"agent_lifecycle:{run_id}"
    completion_id = str(msg.get("agent_completion_for") or "").strip()
    if completion_id:
        return f"agent_completion:{completion_id}"
    goal_id = str(msg.get("goal_completion_for") or "").strip()
    if goal_id:
        return f"goal_completion:{goal_id}"
    return ""


def match_durable_orchestration_outputs(
    loaded_messages: list[dict],
    output_messages: list[dict],
    *,
    orchestration_id: str,
) -> list[dict]:
    """Attach durable output identity when the checkpoint kept only plain text.

    The parent runner can persist its answer before the late-delivery layer
    observes it. The delivery layer intentionally avoids appending identical
    text, so reconstruct the missing UI metadata from the authoritative
    orchestration message record.
    """

    clean_orchestration_id = str(orchestration_id or "").strip()
    if not clean_orchestration_id:
        return []
    candidates = [
        (index, message)
        for index, message in enumerate(loaded_messages)
        if isinstance(message, dict)
        and str(message.get("role") or "") == "assistant"
    ]
    used_indexes: set[int] = set()
    matched: list[dict] = []
    for output in output_messages:
        if not isinstance(output, dict):
            continue
        content = str(output.get("content") or "")
        output_id = str(output.get("id") or "").strip()
        output_kind = str(output.get("kind") or "").removeprefix("parent_")
        payload = output.get("payload_json") or {}
        checkpoint_message_id = (
            str(payload.get("checkpoint_message_id") or "").strip()
            if isinstance(payload, dict)
            else ""
        )
        if not content or not output_id or not output_kind:
            continue
        selected: tuple[int, dict] | None = None
        if checkpoint_message_id:
            for index, message in candidates:
                if index in used_indexes:
                    continue
                if (
                    str(message.get("checkpoint_message_id") or "").strip()
                    == checkpoint_message_id
                ):
                    selected = (index, message)
                    break
        for index, message in candidates:
            if selected is not None:
                break
            if index in used_indexes:
                continue
            if str(message.get("content") or "") == content:
                selected = (index, message)
                break
        if selected is None:
            continue
        index, message = selected
        used_indexes.add(index)
        durable = dict(message)
        durable["orchestration_id"] = clean_orchestration_id
        durable["orchestration_message_kind"] = output_kind
        durable["channel_notification_key"] = output_id
        matched.append(durable)
    return matched


def upsert_durable_transcript_message(
    messages: list[dict],
    incoming: dict,
) -> tuple[bool, int]:
    """Merge one durable synthetic row without replacing in-memory history."""

    key = durable_message_key(incoming)
    if not key:
        return False, -1
    for index, existing in enumerate(messages):
        if not isinstance(existing, dict) or durable_message_key(existing) != key:
            continue
        changed = any(existing.get(name) != value for name, value in incoming.items())
        if changed:
            existing.update(incoming)
        return changed, index

    incoming_role = str(incoming.get("role") or "")
    incoming_content = incoming.get("content")
    for index in range(len(messages) - 1, -1, -1):
        existing = messages[index]
        if not isinstance(existing, dict) or durable_message_key(existing):
            continue
        if (
            str(existing.get("role") or "") == incoming_role
            and existing.get("content") == incoming_content
        ):
            existing.update(incoming)
            return True, index

    insert_at = len(messages)
    for index, existing in enumerate(messages):
        if not isinstance(existing, dict):
            continue
        queued = existing.get("queued_control")
        if isinstance(queued, dict) and str(queued.get("status") or "") in {
            "queued_parent_turn",
            "dispatching",
        }:
            insert_at = index
            break
    messages.insert(insert_at, incoming)
    return True, insert_at
