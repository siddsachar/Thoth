"""Durable delivery of late parent-thread messages to originating channels."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from typing import Any

from row_bot.agent_run_messages import (
    TERMINAL_STATUSES,
    agent_run_terminal_summary,
    terminal_notification_key,
    terminal_ui_metadata,
)

log = logging.getLogger(__name__)
_LOCAL_APPEND_LOCK = threading.RLock()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _message_content(message: object) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _message_ui_metadata(message: object) -> dict[str, Any]:
    kwargs = getattr(message, "additional_kwargs", None) or {}
    if not isinstance(kwargs, dict):
        return {}
    metadata = kwargs.get("row_bot_ui")
    return dict(metadata) if isinstance(metadata, dict) else {}


def append_parent_thread_message_once(
    *,
    thread_id: str,
    key: str,
    text: str,
    ui_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Append a synthetic assistant message to a parent checkpoint once."""

    clean_thread_id = _clean_text(thread_id)
    clean_key = _clean_text(key)
    clean_text = _clean_text(text)
    if not clean_thread_id or not clean_key or not clean_text:
        return False
    metadata = dict(ui_metadata or {})
    metadata.setdefault("channel_notification_key", clean_key)
    agent_completion_for = _clean_text(metadata.get("agent_completion_for"))
    goal_completion_for = _clean_text(metadata.get("goal_completion_for"))
    try:
        from row_bot.threads import append_checkpoint_messages, get_latest_checkpoint_messages
        from langchain_core.messages import AIMessage

        with _LOCAL_APPEND_LOCK:
            for message in get_latest_checkpoint_messages(clean_thread_id):
                if getattr(message, "type", "") != "ai":
                    continue
                existing_metadata = _message_ui_metadata(message)
                if _clean_text(existing_metadata.get("channel_notification_key")) == clean_key:
                    return False
                if agent_completion_for and _clean_text(existing_metadata.get("agent_completion_for")) == agent_completion_for:
                    return False
                if goal_completion_for and _clean_text(existing_metadata.get("goal_completion_for")) == goal_completion_for:
                    return False
                if _message_content(message).strip() == clean_text:
                    return False
            return bool(
                append_checkpoint_messages(
                    clean_thread_id,
                    [AIMessage(content=clean_text, additional_kwargs={"row_bot_ui": metadata})],
                )
            )
    except Exception:
        log.debug("Could not append parent-thread notification to checkpoint", exc_info=True)
        return False


def append_agent_lifecycle_message_once(
    run: Mapping[str, Any],
    *,
    source_generation_id: str = "",
    orchestration_id: str = "",
    required: bool = False,
    wait_mode: bool = False,
) -> bool:
    """Persist one durable parent-transcript card for a delegated Agent Run."""

    run_id = _clean_text(run.get("id"))
    parent_thread_id = _clean_text(run.get("parent_thread_id"))
    if not run_id or not parent_thread_id:
        return False
    profile = _clean_text(
        run.get("profile_display_name")
        or run.get("profile_slug")
        or run.get("kind")
        or "Agent"
    )
    profile = profile[:1].upper() + profile[1:] if profile else "Agent"
    key = f"agent_lifecycle:{run_id}"
    text = f"Started a {profile} agent for this. I'll keep this thread updated."
    return append_parent_thread_message_once(
        thread_id=parent_thread_id,
        key=key,
        text=text,
        ui_metadata={
            "agent_run_ids": [run_id],
            "agent_run_refresh_key": "|".join(
                [
                    run_id,
                    _clean_text(run.get("status")),
                    _clean_text(run.get("updated_at")),
                ]
            ),
            "agent_lifecycle": {
                "kind": "delegated_agent_spawn",
                "run_id": run_id,
                "source_generation_id": _clean_text(source_generation_id),
                "orchestration_id": _clean_text(orchestration_id),
                "required": bool(required),
                "wait_mode": bool(wait_mode),
                "completion_summary_emitted": False,
            },
            "channel_notification_key": key,
        },
    )


def notify_agent_run_approval(approval_or_id: Mapping[str, Any] | str) -> bool:
    """Publish a child approval to its parent checkpoint and bound channel."""

    try:
        from row_bot.approval_messages import compact_message, payload_from_row
        from row_bot.tasks import (
            get_pending_approvals,
            get_thread_channel_ref,
            push_approval_to_parent_channel,
        )

        if isinstance(approval_or_id, Mapping):
            approval = dict(approval_or_id)
        else:
            rows = get_pending_approvals(approval_id=_clean_text(approval_or_id))
            approval = rows[0] if rows else {}
        approval_id = _clean_text(approval.get("id"))
        parent_thread_id = _clean_text(approval.get("parent_thread_id"))
        agent_run_id = _clean_text(approval.get("agent_run_id"))
        payload = payload_from_row(approval)
        orchestration_id = _clean_text(payload.get("orchestration_id"))
        if not orchestration_id:
            orchestration_id = _clean_text(approval.get("step_id")).removeprefix(
                "orchestration:"
            )
        if (
            not approval_id
            or not parent_thread_id
            or (not agent_run_id and not orchestration_id)
        ):
            return False
        text = compact_message(payload) or _clean_text(approval.get("message"))
        if not text:
            text = "Approval required."
        key = f"agent_approval:{approval_id}"
        metadata = {
            "approval_request_id": approval_id,
            "approval_resume_token": _clean_text(approval.get("resume_token")),
            "approval_status": _clean_text(approval.get("status")) or "pending",
            "channel_notification_key": key,
        }
        if agent_run_id:
            metadata["agent_approval_for"] = agent_run_id
            metadata["agent_run_ids"] = [agent_run_id]
        if orchestration_id:
            metadata["orchestration_id"] = orchestration_id
            metadata["orchestration_message_kind"] = "parent_approval"
        appended = append_parent_thread_message_once(
            thread_id=parent_thread_id,
            key=key,
            text=text,
            ui_metadata=metadata,
        )
        local_exists = appended or _parent_thread_message_exists(
            thread_id=parent_thread_id,
            key=key,
            text=text,
        )
        if not get_thread_channel_ref(parent_thread_id):
            return local_exists
        return bool(push_approval_to_parent_channel(approval_id)) and local_exists
    except Exception:
        log.debug("Could not publish Agent Run approval", exc_info=True)
        return False


def _parent_thread_message_exists(*, thread_id: str, key: str, text: str) -> bool:
    """Return whether a local checkpoint already contains this delivery."""

    try:
        from row_bot.threads import get_latest_checkpoint_messages

        for message in get_latest_checkpoint_messages(thread_id):
            if getattr(message, "type", "") != "ai":
                continue
            metadata = _message_ui_metadata(message)
            if _clean_text(metadata.get("channel_notification_key")) == key:
                return True
            if _message_content(message).strip() == text:
                return True
    except Exception:
        log.debug("Could not verify parent-thread notification checkpoint", exc_info=True)
    return False


def _tool_payloads_for_parent_thread(thread_id: str) -> list[dict[str, Any]]:
    try:
        from row_bot.threads import get_latest_checkpoint_messages

        messages = get_latest_checkpoint_messages(thread_id)
    except Exception:
        log.debug("Could not inspect parent checkpoint for Agent notification eligibility", exc_info=True)
        return []
    payloads: list[dict[str, Any]] = []
    for message in messages:
        msg_type = str(getattr(message, "type", "") or "")
        name = str(getattr(message, "name", "") or "").strip().lower()
        if msg_type != "tool" or name not in {"delegate_work", "agents"}:
            continue
        text = _message_content(message)
        try:
            payload = json.loads(text)
        except Exception:
            payload = {"message": text}
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _payload_run_id(payload: Mapping[str, Any]) -> str:
    run = payload.get("run")
    if isinstance(run, Mapping):
        return _clean_text(run.get("id"))
    return ""


def _skip_subagent_terminal_channel_update(run: Mapping[str, Any]) -> bool:
    """Return True when the parent already received a wait-mode result."""

    run_id = _clean_text(run.get("id"))
    parent_thread_id = _clean_text(run.get("parent_thread_id"))
    if not run_id or not parent_thread_id:
        return True
    for payload in _tool_payloads_for_parent_thread(parent_thread_id):
        if _payload_run_id(payload) != run_id:
            continue
        message = _clean_text(payload.get("message")).lower()
        if message == "child agent completed.":
            return True
    return False


def _deliver_record(record: Mapping[str, Any]) -> bool:
    key = _clean_text(record.get("key"))
    channel_name = _clean_text(record.get("channel"))
    target = _clean_text(record.get("target"))
    text = _clean_text(record.get("text"))
    if not key or not channel_name or not target or not text:
        return False
    try:
        from row_bot.channels import registry as channel_registry
        from row_bot.tasks import (
            mark_channel_thread_notification_delivered,
            mark_channel_thread_notification_failed,
        )

        channel = channel_registry.get(channel_name)
        if not channel:
            mark_channel_thread_notification_failed(key, f"Unknown channel: {channel_name}")
            return False
        if not channel.is_running():
            mark_channel_thread_notification_failed(key, f"{channel.display_name} is not running")
            return False
        channel.send_message(target, text)
        mark_channel_thread_notification_delivered(key)
        return True
    except Exception as exc:
        try:
            from row_bot.tasks import mark_channel_thread_notification_failed

            mark_channel_thread_notification_failed(key, str(exc))
        except Exception:
            log.debug("Could not mark channel notification failed", exc_info=True)
        log.warning("Parent-thread channel notification failed for %s: %s", key, exc)
        return False


def deliver_parent_thread_notification(
    *,
    key: str,
    thread_id: str,
    kind: str,
    text: str,
    ui_metadata: Mapping[str, Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    checkpoint_authoritative: bool = False,
) -> bool:
    """Verify or append, then deliver a late parent-thread channel message."""

    clean_key = _clean_text(key)
    clean_thread_id = _clean_text(thread_id)
    clean_kind = _clean_text(kind)
    clean_text = _clean_text(text)
    if not clean_key or not clean_thread_id or not clean_kind or not clean_text:
        return False
    try:
        from row_bot.tasks import (
            get_channel_thread_notification,
            get_thread_channel_ref,
            upsert_channel_thread_notification,
        )

        existing = get_channel_thread_notification(clean_key)
        metadata = dict(ui_metadata or {})
        metadata.setdefault("channel_notification_key", clean_key)
        appended_locally = False
        if not checkpoint_authoritative:
            appended_locally = append_parent_thread_message_once(
                thread_id=clean_thread_id,
                key=clean_key,
                text=clean_text,
                ui_metadata=metadata,
            )
        local_exists = _parent_thread_message_exists(
            thread_id=clean_thread_id,
            key=clean_key,
            text=clean_text,
        )
        if checkpoint_authoritative and not local_exists:
            return False
        if existing and str(existing.get("status") or "") == "delivered":
            return True
        ref = get_thread_channel_ref(clean_thread_id)
        if not ref:
            return appended_locally or local_exists
        record = upsert_channel_thread_notification(
            key=clean_key,
            thread_id=clean_thread_id,
            channel=str(ref.get("channel") or ""),
            target=str(ref.get("target") or ""),
            kind=clean_kind,
            text=clean_text,
            payload={
                "ui_metadata": metadata,
                "checkpoint_authoritative": bool(checkpoint_authoritative),
                **dict(payload or {}),
            },
        )
        if not record:
            return False
        return _deliver_record(record)
    except Exception:
        log.debug("Could not deliver parent-thread notification %s", clean_key, exc_info=True)
        return False


def notify_agent_run_terminal(run_or_id: Mapping[str, Any] | str) -> bool:
    """Notify a channel-owned parent thread that an Agent Run has finished."""

    try:
        if isinstance(run_or_id, Mapping):
            run = dict(run_or_id)
        else:
            from row_bot.agent_runs import get_agent_run

            run = get_agent_run(str(run_or_id)) or {}
        run_id = _clean_text(run.get("id"))
        parent_thread_id = _clean_text(run.get("parent_thread_id"))
        kind = _clean_text(run.get("kind") or "subagent").lower()
        status = _clean_text(run.get("status")).lower()
        if not run_id or not parent_thread_id or status not in TERMINAL_STATUSES:
            return False
        if kind not in {"subagent", "goal"}:
            return False
        from row_bot.tasks import get_thread_channel_ref

        if not get_thread_channel_ref(parent_thread_id):
            return False
        if kind == "subagent" and _skip_subagent_terminal_channel_update(run):
            return False
        text = agent_run_terminal_summary(run)
        if not text:
            return False
        key = terminal_notification_key(run)
        metadata = terminal_ui_metadata(run, key=key)
        return deliver_parent_thread_notification(
            key=key,
            thread_id=parent_thread_id,
            kind=f"{kind}_terminal",
            text=text,
            ui_metadata=metadata,
            payload={
                "agent_run_id": run_id,
                "kind": kind,
                "status": status,
                "goal_id": _clean_text(run.get("goal_id")),
            },
        )
    except Exception:
        log.debug("Could not notify Agent Run terminal state", exc_info=True)
        return False


def reconcile_pending_channel_notifications(limit: int = 50) -> int:
    """Retry pending late parent-thread channel notifications."""

    try:
        from row_bot.tasks import list_pending_channel_thread_notifications

        records = list_pending_channel_thread_notifications(limit=limit)
    except Exception:
        log.debug("Could not load pending channel thread notifications", exc_info=True)
        return 0
    delivered = 0
    for record in records:
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(str(record.get("payload_json") or "{}"))
        except Exception:
            payload = {}
        ui_metadata = payload.get("ui_metadata") if isinstance(payload, dict) else None
        checkpoint_authoritative = bool(
            payload.get("checkpoint_authoritative")
            if isinstance(payload, dict)
            else False
        )
        if isinstance(ui_metadata, Mapping) and not checkpoint_authoritative:
            append_parent_thread_message_once(
                thread_id=str(record.get("thread_id") or ""),
                key=str(record.get("key") or ""),
                text=str(record.get("text") or ""),
                ui_metadata=ui_metadata,
            )
        if checkpoint_authoritative and not _parent_thread_message_exists(
            thread_id=str(record.get("thread_id") or ""),
            key=str(record.get("key") or ""),
            text=str(record.get("text") or ""),
        ):
            continue
        if _deliver_record(record):
            delivered += 1
    return delivered
