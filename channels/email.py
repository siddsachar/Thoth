"""
Thoth – Email Channel Adapter
===============================
Polls Gmail for unread emails with ``[Thoth]`` in the subject line,
runs them through the agent, and replies in the same email thread.

Uses the same OAuth credentials as the Gmail tool
(``~/.thoth/gmail/credentials.json`` + ``token.json``).

Design decisions:
    - Trigger: subject contains ``[Thoth]``
    - Sender filter: only from the authenticated user's address
    - Poll interval: configurable (default 60 s)
    - Threads: per Gmail thread ID → separate agent thread
    - Interrupts: email-based approval (reply APPROVE / DENY in-thread)
"""

from __future__ import annotations

import asyncio
import base64
import email.mime.text
import logging
import os
import pathlib
import re
import threading
import time
from typing import Any

import agent as agent_mod
from threads import _save_thread_meta, _list_threads
from tools import registry as tool_registry
from channels import config as channel_config

_THREAD_CORRUPT_PATTERNS = (
    "tool call.*without.*result",
    "tool_calls.*without.*tool_results",
    "expected.*tool.*message",
)

log = logging.getLogger("thoth.email")

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
CHANNEL_NAME = "email"
DEFAULT_POLL_INTERVAL = 60  # seconds
SUBJECT_TAG = "[Thoth]"
GMAIL_SCOPES = ["https://mail.google.com/"]

_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_GMAIL_DIR = _DATA_DIR / "gmail"
_TOKEN_PATH = str(_GMAIL_DIR / "token.json")
_CREDS_PATH = str(_GMAIL_DIR / "credentials.json")

# ──────────────────────────────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────────────────────────────
_running = False
_poll_task: asyncio.Task | None = None
_pending_interrupts: dict[str, dict] = {}  # {gmail_thread_id: {"data": ..., "config": ...}}
_last_error: str | None = None  # set on auth failure, cleared on successful start


# ──────────────────────────────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────────────────────────────
def get_poll_interval() -> int:
    return channel_config.get(CHANNEL_NAME, "poll_interval", DEFAULT_POLL_INTERVAL)


def set_poll_interval(seconds: int):
    channel_config.set(CHANNEL_NAME, "poll_interval", max(10, seconds))


# ──────────────────────────────────────────────────────────────────────
# Gmail API helpers
# ──────────────────────────────────────────────────────────────────────
def _is_gmail_ready() -> bool:
    """Check if Gmail OAuth credentials and token exist."""
    return os.path.isfile(_CREDS_PATH) and os.path.isfile(_TOKEN_PATH)


def _build_gmail_service():
    """Build a Gmail API service from stored OAuth credentials."""
    from langchain_google_community.gmail.utils import (
        build_resource_service,
        get_gmail_credentials,
    )
    credentials = get_gmail_credentials(
        token_file=_TOKEN_PATH,
        scopes=GMAIL_SCOPES,
        client_sercret_file=_CREDS_PATH,
    )
    return build_resource_service(credentials=credentials)


def _get_my_email(service) -> str:
    """Get the authenticated user's email address."""
    profile = service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "")


def _search_unread_thoth_emails(service) -> list[dict]:
    """Search for unread emails with [Thoth] in the subject from the user."""
    my_email = _get_my_email(service)
    query = f"subject:{SUBJECT_TAG} is:unread from:{my_email}"

    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=10
        ).execute()
        messages = result.get("messages", [])
    except Exception as exc:
        log.error("Gmail search failed: %s", exc)
        return []

    detailed = []
    for msg_stub in messages:
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_stub["id"], format="full"
            ).execute()
            detailed.append(msg)
        except Exception as exc:
            log.error("Failed to fetch message %s: %s", msg_stub["id"], exc)

    return detailed


def _get_thread_replies(service, thread_id: str, after_message_id: str) -> list[dict]:
    """Get messages in a Gmail thread that arrived after a specific message."""
    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()
    except Exception as exc:
        log.error("Failed to fetch thread %s: %s", thread_id, exc)
        return []

    messages = thread.get("messages", [])
    # Find messages after the one we sent
    found_ours = False
    replies = []
    for msg in messages:
        if msg["id"] == after_message_id:
            found_ours = True
            continue
        if found_ours:
            replies.append(msg)
    return replies


def _extract_body(msg: dict) -> str:
    """Extract plain text body from a Gmail message."""
    payload = msg.get("payload", {})

    # Simple single-part message
    if payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # Multipart message — look for text/plain
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        # Nested multipart
        for subpart in part.get("parts", []):
            if subpart.get("mimeType") == "text/plain":
                data = subpart.get("body", {}).get("data", "")
                if data:
                    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    return ""


def _extract_subject(msg: dict) -> str:
    """Extract subject from a Gmail message."""
    headers = msg.get("payload", {}).get("headers", [])
    for h in headers:
        if h["name"].lower() == "subject":
            return h["value"]
    return ""


def _extract_header(msg: dict, name: str) -> str:
    """Extract a header value from a Gmail message."""
    headers = msg.get("payload", {}).get("headers", [])
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _mark_as_read(service, msg_id: str):
    """Mark a Gmail message as read."""
    try:
        service.users().messages().modify(
            userId="me", id=msg_id,
            body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except Exception as exc:
        log.error("Failed to mark message %s as read: %s", msg_id, exc)


def _send_reply(service, original_msg: dict, reply_text: str):
    """Reply to a Gmail message in-thread."""
    thread_id = original_msg.get("threadId", "")
    msg_id_header = _extract_header(original_msg, "Message-ID")
    subject = _extract_subject(original_msg)
    to = _extract_header(original_msg, "From")  # reply to sender (yourself)

    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    mime = email.mime.text.MIMEText(reply_text, "plain", "utf-8")
    mime["to"] = to
    mime["subject"] = subject
    if msg_id_header:
        mime["In-Reply-To"] = msg_id_header
        mime["References"] = msg_id_header

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")

    try:
        sent = service.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": thread_id}
        ).execute()
        log.info("Reply sent in thread %s", thread_id)
        # Mark our own reply as read so the next poll doesn't re-process it
        sent_id = sent.get("id")
        if sent_id:
            _mark_as_read(service, sent_id)
    except Exception as exc:
        log.error("Failed to send reply: %s", exc)


def _send_reply_and_get_id(service, original_msg: dict, reply_text: str) -> str | None:
    """Reply to a Gmail message and return the sent message ID."""
    thread_id = original_msg.get("threadId", "")
    msg_id_header = _extract_header(original_msg, "Message-ID")
    subject = _extract_subject(original_msg)
    to = _extract_header(original_msg, "From")

    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    mime = email.mime.text.MIMEText(reply_text, "plain", "utf-8")
    mime["to"] = to
    mime["subject"] = subject
    if msg_id_header:
        mime["In-Reply-To"] = msg_id_header
        mime["References"] = msg_id_header

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")

    try:
        sent = service.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": thread_id}
        ).execute()
        sent_id = sent.get("id")
        # Mark our own reply as read so the next poll doesn't re-process it
        if sent_id:
            _mark_as_read(service, sent_id)
        return sent_id
    except Exception as exc:
        log.error("Failed to send reply: %s", exc)
        return None


# ──────────────────────────────────────────────────────────────────────
# Agent thread management
# ──────────────────────────────────────────────────────────────────────
def _get_or_create_thread(gmail_thread_id: str, subject: str) -> dict:
    """Get or create a LangGraph agent thread for a Gmail thread."""
    thread_id = f"email_{gmail_thread_id}"

    existing = _list_threads()
    for tid, name, _, _ in existing:
        if tid == thread_id:
            _save_thread_meta(tid, name)
            return {"configurable": {"thread_id": tid}}

    # Clean subject for display (remove [Thoth] prefix)
    clean_subject = subject.replace(SUBJECT_TAG, "").strip()
    name = f"📧 Email – {clean_subject[:40]}" if clean_subject else f"📧 Email – {gmail_thread_id[:8]}"
    _save_thread_meta(thread_id, name)
    return {"configurable": {"thread_id": thread_id}}


# ──────────────────────────────────────────────────────────────────────
# Agent invocation (synchronous — runs in executor)
# ──────────────────────────────────────────────────────────────────────
def _run_agent_sync(user_text: str, config: dict) -> tuple[str, dict | None]:
    """Run the agent synchronously, collecting the full response."""
    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    interrupt_data: dict | None = None

    for event_type, payload in agent_mod.stream_agent(user_text, enabled, config):
        if event_type == "token":
            full_answer.append(payload)
        elif event_type == "tool_call":
            tool_reports.append(f"[Using {payload}]")
        elif event_type == "tool_done":
            if isinstance(payload, dict):
                tool_reports.append(f"[{payload['name']} done]")
            else:
                tool_reports.append(f"[{payload} done]")
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "error":
            full_answer.append(f"Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)

    answer = "".join(full_answer)
    if tool_reports and answer:
        answer = "\n".join(tool_reports) + "\n\n" + answer
    elif tool_reports:
        answer = "\n".join(tool_reports)

    return answer or "(No response)", interrupt_data


def _resume_agent_sync(config: dict, approved: bool,
                       *, interrupt_ids: list[str] | None = None) -> tuple[str, dict | None]:
    """Resume a paused agent after interrupt approval/denial."""
    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    interrupt_data: dict | None = None

    for event_type, payload in agent_mod.resume_stream_agent(
        enabled, config, approved, interrupt_ids=interrupt_ids
    ):
        if event_type == "token":
            full_answer.append(payload)
        elif event_type == "tool_call":
            tool_reports.append(f"[Using {payload}]")
        elif event_type == "tool_done":
            if isinstance(payload, dict):
                tool_reports.append(f"[{payload['name']} done]")
            else:
                tool_reports.append(f"[{payload} done]")
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "error":
            full_answer.append(f"Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)

    answer = "".join(full_answer)
    if tool_reports and answer:
        answer = "\n".join(tool_reports) + "\n\n" + answer
    elif tool_reports:
        answer = "\n".join(tool_reports)

    return answer or "(No response)", interrupt_data


# ──────────────────────────────────────────────────────────────────────
# Interrupt formatting & helpers
# ──────────────────────────────────────────────────────────────────────
def _format_interrupt(data) -> str:
    """Format interrupt data (single dict or list of dicts) for email."""
    items = data if isinstance(data, list) else [data]
    parts: list[str] = []

    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        tool_name = item.get("tool", item.get("name", "Unknown tool"))
        desc = item.get("description", "")
        args = item.get("args", {})

        prefix = f"{i}. " if len(items) > 1 else ""
        parts.append(f"{prefix}APPROVAL REQUIRED: {tool_name}")
        if desc:
            parts.append(f"   {desc}")
        elif args:
            parts.append("   Details:")
            for k, v in args.items():
                parts.append(f"     - {k}: {v}")

    parts.append("\n---")
    parts.append("Reply to this email with APPROVE or DENY.")
    return "\n".join(parts)


def _extract_interrupt_ids(data) -> list[str] | None:
    """Extract __interrupt_id values from interrupt data for multi-interrupt resume."""
    items = data if isinstance(data, list) else [data]
    ids = [item.get("__interrupt_id") for item in items
           if isinstance(item, dict) and item.get("__interrupt_id")]
    return ids if len(ids) > 1 else None


def _is_corrupt_thread_error(exc: Exception) -> bool:
    """Return True if the exception indicates a stuck/corrupt thread."""
    msg = str(exc).lower()
    return any(re.search(p, msg) for p in _THREAD_CORRUPT_PATTERNS)


# ──────────────────────────────────────────────────────────────────────
# Main polling loop
# ──────────────────────────────────────────────────────────────────────
async def _poll_loop():
    """Main polling coroutine — checks for new emails and processes them."""
    global _running, _last_error
    log.info("Email channel polling started (interval: %ds)", get_poll_interval())

    while _running:
        try:
            await _poll_once()
            _last_error = None  # clear on success
        except Exception as exc:
            err_str = str(exc)
            log.error("Email poll error: %s", exc)

            # Stop on auth errors — no point retrying with a bad token
            if any(kw in err_str.lower() for kw in (
                "invalid_grant", "token has been expired", "revoked",
                "credentials", "refresh",
            )):
                _last_error = (
                    "Gmail token expired or revoked. "
                    "Re-authenticate via the Gmail tool in Settings → Tools, "
                    "then restart polling."
                )
                log.error("Auth failure — stopping email polling: %s", _last_error)
                _running = False
                return

            _last_error = err_str

        await asyncio.sleep(get_poll_interval())


async def _poll_once():
    """Single poll iteration — check for new emails and pending interrupt replies."""
    loop = asyncio.get_event_loop()

    service = await loop.run_in_executor(None, _build_gmail_service)

    # ── Check for pending interrupt replies ──────────────────────────
    for gmail_thread_id in list(_pending_interrupts.keys()):
        pending = _pending_interrupts[gmail_thread_id]
        approval_msg_id = pending.get("approval_msg_id")
        if not approval_msg_id:
            continue

        replies = await loop.run_in_executor(
            None, _get_thread_replies, service, gmail_thread_id, approval_msg_id
        )

        for reply in replies:
            body = await loop.run_in_executor(None, _extract_body, reply)
            body_lower = body.strip().lower().split("\n")[0].strip()  # first line only

            if body_lower in ("approve", "approved", "yes", "y", "ok", "go ahead"):
                approved = True
            elif body_lower in ("deny", "denied", "no", "n", "cancel", "stop"):
                approved = False
            else:
                continue  # not a clear answer, keep waiting

            # Mark reply as read
            await loop.run_in_executor(None, _mark_as_read, service, reply["id"])

            # Resume agent
            config = pending["config"]
            original_msg = pending["original_msg"]
            interrupt_ids = _extract_interrupt_ids(pending.get("data"))
            del _pending_interrupts[gmail_thread_id]

            action = "Approved" if approved else "Denied"
            log.info("Interrupt %s for thread %s", action, gmail_thread_id)

            try:
                answer, new_interrupt = await loop.run_in_executor(
                    None, lambda: _resume_agent_sync(
                        config, approved, interrupt_ids=interrupt_ids
                    ),
                )
            except Exception as exc:
                log.error("Agent resume error: %s", exc)
                if _is_corrupt_thread_error(exc):
                    await loop.run_in_executor(
                        None, _send_reply, service, original_msg,
                        "The conversation had a stuck tool call and couldn't continue. "
                        "Please start a new email thread with [Thoth] to retry."
                    )
                else:
                    await loop.run_in_executor(
                        None, _send_reply, service, original_msg, f"Error: {exc}"
                    )
                break

            if new_interrupt:
                interrupt_text = _format_interrupt(new_interrupt)
                sent_id = await loop.run_in_executor(
                    None, _send_reply_and_get_id, service, original_msg, interrupt_text
                )
                _pending_interrupts[gmail_thread_id] = {
                    "data": new_interrupt,
                    "config": config,
                    "original_msg": original_msg,
                    "approval_msg_id": sent_id,
                }
            else:
                await loop.run_in_executor(
                    None, _send_reply, service, original_msg, answer
                )
            break  # one reply per poll

    # ── Check for new unread [Thoth] emails ──────────────────────────
    messages = await loop.run_in_executor(None, _search_unread_thoth_emails, service)

    for msg in messages:
        msg_id = msg["id"]
        gmail_thread_id = msg.get("threadId", msg_id)
        subject = _extract_subject(msg)
        body = _extract_body(msg)

        # Skip if this thread already has a pending interrupt
        if gmail_thread_id in _pending_interrupts:
            continue

        # Strip [Thoth] from the body query (it's just the trigger tag)
        # Clean the body — remove quoted reply content and signatures
        clean_body = _clean_email_body(body)
        if not clean_body:
            await loop.run_in_executor(None, _mark_as_read, service, msg_id)
            continue

        log.info("Processing email: %s (thread: %s)", subject, gmail_thread_id)

        # Mark as read immediately
        await loop.run_in_executor(None, _mark_as_read, service, msg_id)

        # Get or create agent thread
        config = _get_or_create_thread(gmail_thread_id, subject)

        # Run agent
        try:
            answer, interrupt_data = await loop.run_in_executor(
                None, _run_agent_sync, clean_body, config
            )
        except Exception as exc:
            log.error("Agent error for email %s: %s", msg_id, exc)
            if _is_corrupt_thread_error(exc):
                await loop.run_in_executor(
                    None, _send_reply, service, msg,
                    "The conversation had a stuck tool call and couldn't continue. "
                    "Please start a new email thread with [Thoth] to retry."
                )
            else:
                await loop.run_in_executor(
                    None, _send_reply, service, msg,
                    f"Error processing your request: {exc}"
                )
            continue

        if interrupt_data:
            interrupt_text = _format_interrupt(interrupt_data)
            sent_id = await loop.run_in_executor(
                None, _send_reply_and_get_id, service, msg, interrupt_text
            )
            _pending_interrupts[gmail_thread_id] = {
                "data": interrupt_data,
                "config": config,
                "original_msg": msg,
                "approval_msg_id": sent_id,
            }
        else:
            await loop.run_in_executor(
                None, _send_reply, service, msg, answer
            )


def _clean_email_body(body: str) -> str:
    """Remove quoted replies, signatures, and the [Thoth] tag from email body."""
    lines = body.split("\n")
    clean = []
    for line in lines:
        # Stop at quoted reply markers
        if line.strip().startswith(">"):
            break
        if line.strip().startswith("On ") and line.strip().endswith("wrote:"):
            break
        # Stop at common signature markers
        if line.strip() == "--":
            break
        if line.strip() == "---":
            break
        clean.append(line)

    text = "\n".join(clean).strip()
    # Remove [Thoth] tag if it appears in the body text
    text = text.replace(SUBJECT_TAG, "").strip()
    return text


# ──────────────────────────────────────────────────────────────────────
# Lifecycle: start / stop
# ──────────────────────────────────────────────────────────────────────
def is_configured() -> bool:
    """Return True if Gmail OAuth is set up (credentials + token exist)."""
    return _is_gmail_ready()


def is_running() -> bool:
    return _running


def get_last_error() -> str | None:
    """Return the last error message, or None if no error."""
    return _last_error


async def start_polling() -> bool:
    """Start the email polling loop.

    Returns True on success, False if not configured or already running.
    """
    global _running, _poll_task

    if _running:
        log.info("Email channel already running")
        return True

    if not is_configured():
        log.warning("Email channel not configured (Gmail OAuth not set up)")
        return False

    global _last_error
    _last_error = None
    _running = True
    _poll_task = asyncio.create_task(_poll_loop())
    log.info("Email channel started")
    return True


async def stop_polling():
    """Stop the email polling loop."""
    global _running, _poll_task

    if not _running:
        return

    _running = False
    if _poll_task:
        _poll_task.cancel()
        try:
            await _poll_task
        except asyncio.CancelledError:
            pass
        _poll_task = None

    _pending_interrupts.clear()
    log.info("Email channel stopped")


# ──────────────────────────────────────────────────────────────────────
# Outbound messages (called by the task engine)
# ──────────────────────────────────────────────────────────────────────
def send_outbound(to: str, subject: str, body: str) -> None:
    """Send a new email to *to* with the given subject and body.

    Called synchronously by ``tasks._deliver_to_channel()``.
    Uses the same Gmail OAuth credentials as the inbound channel.
    """
    if not _is_gmail_ready():
        raise RuntimeError("Gmail OAuth not configured — cannot deliver email")

    service = _build_gmail_service()

    full_body = body.rstrip() + "\n\n— sent by Thoth"
    mime = email.mime.text.MIMEText(full_body, "plain", "utf-8")
    mime["to"] = to
    mime["subject"] = subject

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()
    log.info("Outbound email sent to %s (subject: %s)", to, subject)
