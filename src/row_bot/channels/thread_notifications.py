"""Durable delivery of late parent-thread messages to originating channels."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from row_bot.agent_run_messages import (
    TERMINAL_STATUSES,
    agent_run_terminal_summary,
    terminal_notification_key,
    terminal_ui_metadata,
)

log = logging.getLogger(__name__)


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
) -> bool:
    """Append and deliver a late parent-thread message to its channel."""

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
        appended_locally = append_parent_thread_message_once(
            thread_id=clean_thread_id,
            key=clean_key,
            text=clean_text,
            ui_metadata=metadata,
        )
        if existing and str(existing.get("status") or "") == "delivered":
            return True
        ref = get_thread_channel_ref(clean_thread_id)
        if not ref:
            return appended_locally or _parent_thread_message_exists(
                thread_id=clean_thread_id,
                key=clean_key,
                text=clean_text,
            )
        record = upsert_channel_thread_notification(
            key=clean_key,
            thread_id=clean_thread_id,
            channel=str(ref.get("channel") or ""),
            target=str(ref.get("target") or ""),
            kind=clean_kind,
            text=clean_text,
            payload={"ui_metadata": metadata, **dict(payload or {})},
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
        if isinstance(ui_metadata, Mapping):
            append_parent_thread_message_once(
                thread_id=str(record.get("thread_id") or ""),
                key=str(record.get("key") or ""),
                text=str(record.get("text") or ""),
                ui_metadata=ui_metadata,
            )
        if _deliver_record(record):
            delivered += 1
    return delivered
