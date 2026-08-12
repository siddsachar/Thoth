"""
Row-Bot – SMS Channel Adapter (Twilio)
=======================================
SMS/MMS channel using Twilio REST API for outbound.  Inbound messages
arrive via a ``POST /sms`` route mounted on the main NiceGUI/Starlette
app (same port as the web UI), so a single ngrok tunnel covers both.

Setup:
    1. Create a Twilio account at https://www.twilio.com/
    2. Get your **Account SID** and **Auth Token** from the console
    3. Buy or use a Twilio phone number
    4. Enable the main-app tunnel in Settings → System → Tunnel Settings
       (or manually set the Twilio webhook to ``<public-url>/sms``)
    5. Enter credentials in Settings → Channels → SMS

Required keys (stored via api_keys):
    TWILIO_ACCOUNT_SID  – Twilio Account SID
    TWILIO_AUTH_TOKEN    – Twilio Auth Token
    TWILIO_PHONE_NUMBER  – Your Twilio phone number (E.164 format, e.g. +1234567890)
    SMS_USER_PHONE       – Your personal phone number (E.164 format, access control)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

from row_bot.app_port import get_app_port
from row_bot.channels.base import Channel, ChannelCapabilities, ConfigField
from row_bot.channels.auth_store import get_channel_secret
from row_bot.channels import commands as ch_commands
from row_bot.channels import auth as ch_auth
from row_bot.channels import config as ch_config
from row_bot.channels import runtime as ch_runtime
from row_bot.threads import _save_thread_meta

log = logging.getLogger("row_bot.sms")

# ──────────────────────────────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────────────────────────────
_client = None                          # twilio.rest.Client
_running = False
_webhook_public_url: str | None = None  # main-app tunnel URL (set by start_bot)
_route_mounted = False                  # /sms route registered on Starlette app
_pending_interrupts: dict[str, dict] = {}   # phone_number → interrupt_data
_pending_task_approvals: dict[str, dict] = {}  # phone_number → {resume_token, task_name, _ts}
_pending_lock = threading.Lock()

SMS_MAX_LEN = 1600  # Twilio max segment length

# Webhook hardening
_rate_limits: dict[str, list[float]] = {}   # client IP → list of timestamps
_RATE_LIMIT = 30                            # max requests per window
_RATE_WINDOW = 60                           # window in seconds
_seen_sids: dict[str, float] = {}           # MessageSid → timestamp (dedup)


def _agent_mod():
    import row_bot.agent as agent_mod

    return agent_mod


# ──────────────────────────────────────────────────────────────────────
# Markdown stripping for plain-text SMS
# ──────────────────────────────────────────────────────────────────────
import re as _re


def _strip_markdown(text: str) -> str:
    """Remove Markdown formatting for plain-text SMS delivery."""
    # Protect fenced code blocks (keep content, drop fences)
    text = _re.sub(r"```(?:\w*\n)?([\s\S]*?)```", r"\1", text)
    # Remove inline code backticks
    text = _re.sub(r"`([^`]+)`", r"\1", text)
    # Markdown links [text](url) → text (url)
    text = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    # Strikethrough ~~text~~ → text
    text = _re.sub(r"~~(.+?)~~", r"\1", text)
    # Bold **text** or __text__ → text
    text = _re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = _re.sub(r"__(.+?)__", r"\1", text)
    # Italic *text* or _text_ → text
    text = _re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"\1", text)
    text = _re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", r"\1", text)
    # Headings (# ... at start of line) → plain text
    text = _re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=_re.MULTILINE)
    # Horizontal rules
    text = _re.sub(r"^[-*_]{3,}\s*$", "", text, flags=_re.MULTILINE)
    return text


# ──────────────────────────────────────────────────────────────────────
# Helpers — credentials
# ──────────────────────────────────────────────────────────────────────
def _get_account_sid() -> str:
    return get_channel_secret("sms", "TWILIO_ACCOUNT_SID")


def _get_auth_token() -> str:
    return get_channel_secret("sms", "TWILIO_AUTH_TOKEN")


def _get_twilio_number() -> str:
    return get_channel_secret("sms", "TWILIO_PHONE_NUMBER")


def _get_user_phone() -> str:
    return get_channel_secret("sms", "SMS_USER_PHONE")


# ──────────────────────────────────────────────────────────────────────
# Configuration & lifecycle helpers
# ──────────────────────────────────────────────────────────────────────
def is_configured() -> bool:
    return bool(_get_account_sid()) and bool(_get_auth_token()) and bool(_get_twilio_number())


def is_running() -> bool:
    return _running


# ──────────────────────────────────────────────────────────────────────
# Thread management
# ──────────────────────────────────────────────────────────────────────
def _make_thread_id(phone: str) -> str:
    # Normalise phone to safe string
    safe = phone.replace("+", "").replace("-", "").replace(" ", "")
    return f"sms_{safe}"


def _get_or_create_thread(phone: str) -> str:
    thread_id = _make_thread_id(phone)
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="sms",
            target=phone,
            external_conversation_id=str(phone),
        )
    except Exception:
        log.debug("SMS thread channel ref skipped", exc_info=True)
    name = f"📱 SMS – {phone}"
    _save_thread_meta(thread_id, name, seed_default_skills=True)  # creates or bumps updated_at
    return thread_id


def _new_thread(phone: str) -> str:
    """Force-create a new thread for this phone number."""
    import time as _time
    suffix = str(int(_time.time()))
    thread_id = f"sms_{phone.replace('+', '').replace('-', '')}_{suffix}"
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="sms",
            target=phone,
            external_conversation_id=str(phone),
        )
    except Exception:
        log.debug("SMS thread channel ref skipped", exc_info=True)
    _save_thread_meta(thread_id, f"📱 SMS – {phone}", seed_default_skills=True)
    return thread_id


# ──────────────────────────────────────────────────────────────────────
# Agent execution
# ──────────────────────────────────────────────────────────────────────
def build_channel_runtime_config(config: dict, purpose: str) -> dict:
    from row_bot.channels.runtime import approval_mode_for_config

    if purpose == "approval":
        runtime_surface = "approval"
        runtime_mode = "agent"
    else:
        runtime_surface = "channel"
        runtime_mode = "auto"
    return {
        **config,
        "configurable": {
            **(config.get("configurable") or {}),
            "runtime_surface": runtime_surface,
            "runtime_mode": runtime_mode,
            "channel_streaming": False,
            "approval_mode": approval_mode_for_config(config),
        },
    }


def _run_agent_sync(user_text: str, config: dict) -> tuple[str, dict | None, list]:
    from row_bot.tools import registry as tool_registry

    agent_mod = _agent_mod()
    config = build_channel_runtime_config(config, "message")
    from row_bot.channels import runtime as orchestration_runtime

    config = orchestration_runtime.prepare_channel_turn_config(config, user_text)
    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    context_notices: list[dict] = []
    interrupt_data: dict | None = None

    for event_type, payload in agent_mod.stream_agent(user_text, enabled, config):
        if event_type == "token":
            full_answer.append(payload)
        elif event_type == "tool_call":
            tool_reports.append(f"🔧 Using {payload}…")
        elif event_type == "tool_done":
            name = payload['name'] if isinstance(payload, dict) else payload
            tool_reports.append(f"✅ {name} done")
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "compaction_succeeded" and isinstance(payload, dict):
            if int(payload.get("event_id") or 0):
                context_notices.append({
                    "event_id": int(payload["event_id"]),
                    "display_copy": str(payload.get("display_copy") or ""),
                })
        elif event_type == "error":
            full_answer.append(f"⚠️ Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)

    answer = "".join(full_answer)
    if tool_reports and answer:
        answer = "\n".join(tool_reports) + "\n\n" + answer
    if tool_reports and not answer:
        answer = "\n".join(tool_reports)

    return answer or "(No response)", interrupt_data, context_notices


def _resume_agent_sync(config: dict, approved: bool,
                       *, interrupt_ids: list[str] | None = None) -> tuple[str, dict | None, list]:
    """Resume a paused agent after interrupt approval/denial."""
    config = build_channel_runtime_config(config, "approval")
    from row_bot.tools import registry as tool_registry

    agent_mod = _agent_mod()
    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    context_notices: list[dict] = []
    interrupt_data: dict | None = None

    for event_type, payload in agent_mod.resume_stream_agent(
        enabled, config, approved, interrupt_ids=interrupt_ids
    ):
        if event_type == "token":
            full_answer.append(payload)
        elif event_type == "tool_call":
            tool_reports.append(f"🔧 Using {payload}…")
        elif event_type == "tool_done":
            name = payload['name'] if isinstance(payload, dict) else payload
            tool_reports.append(f"✅ {name} done")
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "compaction_succeeded" and isinstance(payload, dict):
            if int(payload.get("event_id") or 0):
                context_notices.append({
                    "event_id": int(payload["event_id"]),
                    "display_copy": str(payload.get("display_copy") or ""),
                })
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

    return answer or "(No response)", interrupt_data, context_notices


# ──────────────────────────────────────────────────────────────────────
# Message sending
# ──────────────────────────────────────────────────────────────────────
def _split_sms(text: str, max_len: int = SMS_MAX_LEN) -> list[str]:
    """Split long text into SMS-friendly chunks."""
    if len(text) <= max_len:
        return [text]

    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        # Try to split at newline
        idx = text.rfind("\n", 0, max_len)
        if idx < max_len // 2:
            idx = text.rfind(" ", 0, max_len)
        if idx < max_len // 4:
            idx = max_len
        parts.append(text[:idx])
        text = text[idx:].lstrip()
    return parts


def send_outbound(phone: str, text: str) -> None:
    """Send an SMS to a phone number."""
    if not _running or _client is None:
        raise RuntimeError("SMS channel is not running")

    parts = _split_sms(_strip_markdown(text))
    for part in parts:
        _client.messages.create(
            body=part,
            from_=_get_twilio_number(),
            to=phone,
        )


def send_mms(phone: str, file_path: str, caption: str | None = None) -> None:
    """Send an MMS with a media attachment."""
    if not _running or _client is None:
        raise RuntimeError("SMS channel is not running")

    # For MMS, we need a publicly accessible URL for the media
    # In practice, this would require a media hosting step
    # For now, send the caption as text with a note about the file
    body = caption or ""
    if not body:
        body = f"[File: {os.path.basename(file_path)}]"
    else:
        body += f"\n[File: {os.path.basename(file_path)}]"

    _client.messages.create(
        body=body,
        from_=_get_twilio_number(),
        to=phone,
    )


# ──────────────────────────────────────────────────────────────────────
# Inbound webhook handler (Starlette — mounted on main NiceGUI app)
# ──────────────────────────────────────────────────────────────────────
async def _handle_inbound_sms(request) -> Any:
    """Handle inbound SMS via Twilio webhook (POST /sms)."""
    from starlette.responses import Response
    global _seen_sids

    _XML = "text/xml"

    if not _running:
        return Response("SMS channel not running", status_code=503)

    # ── Body size limit (1 MB) ───────────────────────────────────
    content_length = int(request.headers.get("content-length", 0))
    if content_length > 1_048_576:
        return Response("Payload too large", status_code=413)

    # ── Rate limiting (per IP) ───────────────────────────────────
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = _rate_limits.setdefault(client_ip, [])
    hits[:] = [t for t in hits if now - t < _RATE_WINDOW]
    if len(hits) >= _RATE_LIMIT:
        return Response("Too many requests", status_code=429)
    hits.append(now)

    # ── Twilio signature validation ──────────────────────────────
    insecure = os.environ.get("SMS_INSECURE_NO_SIGNATURE", "").lower() == "true"
    if not insecure:
        try:
            from twilio.request_validator import RequestValidator
            auth_token = _get_auth_token()
            if auth_token:
                validator = RequestValidator(auth_token)
                signature = request.headers.get("X-Twilio-Signature", "")
                validation_url = (
                    _webhook_public_url + "/sms"
                    if _webhook_public_url
                    else str(request.url)
                )
                form = await request.form()
                params = dict(form)
                if not validator.validate(validation_url, params, signature):
                    log.warning("Invalid Twilio signature from %s", client_ip)
                    return Response("Forbidden", status_code=403)
        except ImportError:
            pass  # twilio.request_validator not available — skip

    try:
        data = await request.form()
    except Exception:
        return Response("", status_code=400)

    # ── MessageSid dedup (1-hour TTL) ────────────────────────────
    message_sid = data.get("MessageSid", "")
    if message_sid:
        now_dedup = time.time()
        if len(_seen_sids) > 100:
            _seen_sids = {k: v for k, v in _seen_sids.items()
                          if now_dedup - v < 3600}
        if message_sid in _seen_sids:
            return Response("<Response/>", media_type=_XML)
        _seen_sids[message_sid] = now_dedup

    from_number = data.get("From", "")
    body = data.get("Body", "").strip()

    if not from_number or not body:
        return Response("<Response/>", media_type=_XML)

    # Auth check
    if not _is_authorised(from_number):
        if ch_auth.verify_pairing_code("sms", from_number, body):
            _send_reply(from_number, "✅ Paired! You can now text Row-Bot.")
            return Response("<Response/>", media_type=_XML)
        # Don't respond to unauthorised numbers
        return Response("<Response/>", media_type=_XML)

    from row_bot.channels.base import record_activity
    record_activity("sms")

    # Slash command dispatch
    _cmd_thread_id = (
        _get_or_create_thread(from_number)
        if ch_commands.is_thread_scoped_command(body)
        else None
    )
    goal_start = ch_runtime.prepare_channel_goal_start(body, _cmd_thread_id)
    if goal_start is not None and _cmd_thread_id:
        config = {"configurable": {"thread_id": _cmd_thread_id}}
        _send_reply(from_number, ch_runtime.format_goal_started_ack(goal_start))
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: ch_runtime.run_channel_goal_sync(
                channel_name="sms",
                thread_id=_cmd_thread_id,
                config=config,
                first_prompt=goal_start.prompt,
                run_turn=lambda prompt, cfg: _run_agent_sync(prompt, cfg),
                send_text=lambda message: _send_reply(from_number, message),
            ),
        )
        if result.interrupt_data:
            with _pending_lock:
                _pending_interrupts[from_number] = {
                    "data": result.interrupt_data, "config": config
                }
            from row_bot.channels import approval as approval_helpers
            detail = approval_helpers.format_interrupt_text(result.interrupt_data)
            _send_reply(from_number, detail + "\nReply YES or NO.")
        return Response("<Response/>", media_type=_XML)
    cmd_response = ch_commands.dispatch("sms", body, thread_id=_cmd_thread_id)
    if cmd_response is not None:
        _send_reply(from_number, cmd_response)
        return Response("<Response/>", media_type=_XML)

    # Check pending task approvals first (background workflows)
    from row_bot.channels import approval as approval_helpers
    with _pending_lock:
        task_approval = _pending_task_approvals.get(from_number)
    if task_approval:
        decision = approval_helpers.is_approval_text(body)
        if decision is None:
            _send_reply(from_number, "Approval pending. Reply YES or NO.")
            return Response("<Response/>", media_type=_XML)
        if decision is not None:
            with _pending_lock:
                _pending_task_approvals.pop(from_number, None)
            try:
                from row_bot.tasks import respond_to_approval
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: respond_to_approval(
                        task_approval["resume_token"], decision,
                        note=f"{'Approved' if decision else 'Denied'} via SMS",
                        source="sms",
                    ),
                )
                action = "✅ Approved" if decision else "❌ Denied"
                if result:
                    _send_reply(from_number,
                                f"{action} — {task_approval['task_name']}")
                else:
                    _send_reply(from_number,
                                "Could not process approval — it may have expired.")
            except Exception as exc:
                log.error("Task approval error: %s", exc)
                _send_reply(from_number,
                            f"Error processing approval: {exc}")
            return Response("<Response/>", media_type=_XML)

    # Check pending live-chat interrupts
    with _pending_lock:
        interrupt = _pending_interrupts.get(from_number)

    if interrupt:
        decision = approval_helpers.is_approval_text(body)
        if decision is None:
            _send_reply(from_number, "Approval pending. Reply YES or NO.")
            return Response("<Response/>", media_type=_XML)
        with _pending_lock:
            interrupt = _pending_interrupts.pop(from_number, None)
        if interrupt is None:
            _send_reply(from_number, "This approval is no longer pending.")
            return Response("<Response/>", media_type=_XML)
        approved = bool(decision)
        config = interrupt.get("config", {})
        interrupt_ids = approval_helpers.extract_interrupt_ids(
            interrupt.get("data")
        )
        ch_runtime.resolve_goal_approval_for_config(config, approved)

        loop = asyncio.get_event_loop()
        answer, new_interrupt, context_notices = await loop.run_in_executor(
            None,
            lambda: _resume_agent_sync(
                config, approved, interrupt_ids=interrupt_ids
            ),
        )
        if answer and not ch_runtime.channel_turn_is_suspended(answer):
            _send_reply(from_number, answer)
        if context_notices:
            from row_bot.channels.streaming import deliver_context_notices_sync

            deliver_context_notices_sync(
                context_notices,
                channel="sms",
                send_text=lambda message: _send_reply(from_number, message),
            )
        if new_interrupt:
            with _pending_lock:
                _pending_interrupts[from_number] = {
                    "data": new_interrupt, "config": config
                }
            detail = approval_helpers.format_interrupt_text(new_interrupt)
            _send_reply(from_number, detail + "\nReply YES or NO.")
        elif answer and not ch_runtime.channel_turn_is_suspended(answer):
            thread_id = ch_runtime.thread_id_from_config(config)
            if thread_id:
                goal_result = await loop.run_in_executor(
                    None,
                    lambda: ch_runtime.continue_channel_goal_after_turn_sync(
                        channel_name="sms",
                        thread_id=thread_id,
                        config=config,
                        assistant_text=answer,
                        interrupt_data=None,
                        run_turn=lambda prompt, cfg: _run_agent_sync(prompt, cfg),
                        send_text=lambda message: _send_reply(from_number, message),
                    ),
                )
                if goal_result.interrupt_data:
                    with _pending_lock:
                        _pending_interrupts[from_number] = {
                            "data": goal_result.interrupt_data, "config": config
                        }
                    detail = approval_helpers.format_interrupt_text(goal_result.interrupt_data)
                    _send_reply(from_number, detail + "\nReply YES or NO.")
        return Response("<Response/>", media_type=_XML)

    # Normal message
    thread_id = _get_or_create_thread(from_number)
    config = {"configurable": {"thread_id": thread_id}}

    loop = asyncio.get_event_loop()
    try:
        answer, interrupt_data, context_notices = await loop.run_in_executor(
            None, _run_agent_sync, body, config
        )
    except Exception as exc:
        log.error("Agent error for %s: %s", from_number, exc)
        from row_bot.channels.thread_repair import is_corrupt_thread_error
        if is_corrupt_thread_error(exc):
            try:
                from row_bot.agent import repair_orphaned_tool_calls
                await loop.run_in_executor(
                    None, repair_orphaned_tool_calls, None, config
                )
                log.info("Repaired orphaned tool calls for %s, retrying", from_number)
                answer, interrupt_data, context_notices = await loop.run_in_executor(
                    None, _run_agent_sync, body, config
                )
            except Exception as retry_exc:
                log.error("Retry after repair failed: %s", retry_exc)
                _new_thread(from_number)
                _send_reply(from_number,
                    "The previous conversation had a stuck tool call "
                    "and couldn't be repaired. "
                    "I've started a fresh thread - please resend your message.")
                return Response("<Response/>", media_type=_XML)
        else:
            _send_reply(from_number, f"Error: {exc}")
            return Response("<Response/>", media_type=_XML)

    if answer and not ch_runtime.channel_turn_is_suspended(answer):
        _send_reply(from_number, answer)
    if context_notices:
        from row_bot.channels.streaming import deliver_context_notices_sync

        deliver_context_notices_sync(
            context_notices,
            channel="sms",
            send_text=lambda message: _send_reply(from_number, message),
        )

    if interrupt_data:
        with _pending_lock:
            _pending_interrupts[from_number] = {
                "data": interrupt_data, "config": config
            }
        from row_bot.channels import approval as approval_helpers
        detail = approval_helpers.format_interrupt_text(interrupt_data)
        _send_reply(from_number, detail + "\nReply YES or NO.")

    return Response("<Response/>", media_type=_XML)


def _send_reply(phone: str, text: str) -> None:
    """Send a reply via Twilio REST API (non-async helper)."""
    try:
        send_outbound(phone, text)
    except Exception as exc:
        log.warning("Failed to send SMS reply to %s: %s", phone, exc)


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────
def _is_authorised(phone: str) -> bool:
    """Check if a phone number is authorised."""
    allowed = _get_user_phone()
    if allowed and phone == allowed:
        return True
    return ch_auth.is_user_approved("sms", phone)


# ──────────────────────────────────────────────────────────────────────
# Bot lifecycle
# ──────────────────────────────────────────────────────────────────────
async def start_bot() -> bool:
    """Start the SMS channel (Twilio client + mount /sms route)."""
    global _client, _running, _route_mounted, _webhook_public_url

    if _running:
        log.info("SMS channel already running")
        return True

    sid = _get_account_sid()
    token = _get_auth_token()
    number = _get_twilio_number()

    if not sid or not token or not number:
        log.info("Twilio credentials not configured")
        return False

    try:
        from twilio.rest import Client
        _client = Client(sid, token)

        # Mount /sms on the main NiceGUI/Starlette app (once)
        if not _route_mounted:
            from nicegui import app as _nicegui_app
            _nicegui_app.add_route("/sms", _handle_inbound_sms, methods=["POST"])
            _route_mounted = True
            log.info("Mounted /sms webhook route on main app")

        _running = True
        log.info("SMS channel started")

        # ── Resolve public URL from main-app tunnel ──────────────
        tunnel_enabled = ch_config.get("sms", "tunnel_enabled", True)
        if tunnel_enabled:
            try:
                from row_bot.tunnel import tunnel_manager
                # Use the main-app tunnel rather than a
                # dedicated SMS port — avoids the ngrok one-tunnel limit.
                app_port = get_app_port()
                public_url = tunnel_manager.get_url(app_port)
                if not public_url and tunnel_manager.is_available():
                    log.info("SMS starting shared main-app tunnel on port %s", app_port)
                    public_url = tunnel_manager.start_tunnel(app_port, label="sms")
                    ch_config.set("tunnel", "tunnel_main_app", True)
                if public_url:
                    _webhook_public_url = public_url
                    log.info("SMS using main-app tunnel: %s/sms", public_url)
                    _schedule_twilio_webhook_registration(public_url)
                else:
                    _status_code, detail = tunnel_manager.status()
                    log.warning("SMS tunnel enabled but unavailable: %s", detail)
                    log.info(
                        "Main-app tunnel not active — enable 'Expose task "
                        "webhook endpoint' in Settings → System → Tunnel "
                        "Settings, or set the Twilio webhook URL manually."
                    )
                    _webhook_public_url = None
            except ImportError:
                _webhook_public_url = None
            except Exception as exc:
                log.error("SMS failed to open shared main-app tunnel: %s", exc)
                _webhook_public_url = None
        else:
            _webhook_public_url = None

        return True

    except ImportError:
        log.error("twilio not installed. Run: pip install twilio")
        return False
    except Exception as exc:
        log.error("Failed to start SMS channel: %s", exc)
        _running = False
        return False


def _auto_register_twilio_webhook(public_url: str) -> None:
    """Update the Twilio phone number's SMS webhook URL via API."""
    try:
        if _client is None:
            log.warning("Twilio webhook auto-registration skipped: client unavailable")
            return
        phone = _get_twilio_number()
        numbers = _client.incoming_phone_numbers.list(phone_number=phone)
        if numbers:
            sms_url = f"{public_url}/sms"
            numbers[0].update(sms_url=sms_url, sms_method="POST")
            log.info("Twilio webhook auto-registered: %s", sms_url)
        else:
            log.warning("Could not find Twilio number %s for auto-registration", phone)
    except Exception as exc:
        log.warning("Twilio webhook auto-registration failed: %s", exc)


def _schedule_twilio_webhook_registration(public_url: str):
    """Register the Twilio webhook without blocking channel startup."""
    async def _run() -> None:
        await asyncio.to_thread(_auto_register_twilio_webhook, public_url)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _auto_register_twilio_webhook(public_url)
        return None

    task = loop.create_task(_run(), name="sms-webhook-auto-registration")

    def _log_failure(done_task: asyncio.Task) -> None:
        if done_task.cancelled():
            return
        try:
            exc = done_task.exception()
        except Exception as callback_exc:
            log.debug("Could not inspect SMS webhook registration task: %s", callback_exc)
            return
        if exc is not None:
            log.warning("Twilio webhook auto-registration task failed: %s", exc)

    task.add_done_callback(_log_failure)
    return task


async def stop_bot() -> None:
    """Stop the SMS channel gracefully."""
    global _client, _running, _webhook_public_url

    if not _running:
        return

    # The /sms route stays mounted (cheap no-op when _running is False)
    # — the handler returns 503 when the channel isn't running.
    _webhook_public_url = None
    _client = None
    _running = False
    log.info("SMS channel stopped")


# ──────────────────────────────────────────────────────────────────────
# Channel class
# ──────────────────────────────────────────────────────────────────────

class SMSChannel(Channel):
    """SMS channel adapter via Twilio."""

    @property
    def name(self) -> str:
        return "sms"

    @property
    def display_name(self) -> str:
        return "SMS"

    @property
    def icon(self) -> str:
        return "sms"

    @property
    def webhook_port(self) -> int | None:
        return None  # /sms is mounted on the main NiceGUI app

    @property
    def needs_tunnel(self) -> bool:
        return True  # SMS still benefits from a public URL (main-app tunnel)

    @property
    def setup_guide(self) -> str:
        return (
            "### Quick Setup\n"
            "1. Create a [Twilio account](https://www.twilio.com/)\n"
            "2. Get your **Account SID** and **Auth Token** from the console\n"
            "3. Buy or use a Twilio phone number\n"
            "4. Enable **Expose task webhook endpoint** in **Settings → System → Tunnel Settings**\n"
            "   (the `/sms` webhook shares the main app's tunnel)\n"
            "5. Paste credentials below and click **Save**\n"
            "6. Click **▶️ Start** — Twilio webhook auto-registers"
        )

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            photo_in=False,       # MMS technically possible but complex
            voice_in=False,
            document_in=False,
            photo_out=False,      # MMS requires public media URL
            document_out=False,
            buttons=False,
            streaming=False,
            typing=False,
            reactions=False,
            slash_commands=True,   # Text-based /commands
        )

    @property
    def config_fields(self) -> list[ConfigField]:
        return [
            ConfigField(
                key="account_sid",
                label="Account SID",
                field_type="password",
                storage="env",
                env_key="TWILIO_ACCOUNT_SID",
                help_text="Twilio Account SID from console",
            ),
            ConfigField(
                key="auth_token",
                label="Auth Token",
                field_type="password",
                storage="env",
                env_key="TWILIO_AUTH_TOKEN",
                help_text="Twilio Auth Token from console",
            ),
            ConfigField(
                key="phone_number",
                label="Twilio Phone Number",
                field_type="text",
                storage="env",
                env_key="TWILIO_PHONE_NUMBER",
                help_text="Your Twilio number in E.164 format (e.g. +1234567890)",
            ),
            ConfigField(
                key="user_phone",
                label="Your Phone Number",
                field_type="text",
                storage="env",
                env_key="SMS_USER_PHONE",
                help_text="Your phone number for auto-auth (E.164 format)",
            ),
        ]

    async def start(self) -> bool:
        return await start_bot()

    async def stop(self) -> None:
        await stop_bot()

    def is_configured(self) -> bool:
        return is_configured()

    def is_running(self) -> bool:
        return is_running()

    def get_default_target(self) -> str:
        phone = _get_user_phone()
        if phone:
            return phone
        approved = ch_auth.get_approved_users("sms")
        if approved:
            return approved[0]
        raise RuntimeError("No SMS user configured — set SMS_USER_PHONE or pair via SMS")

    def send_message(self, target: str | int, text: str) -> None:
        send_outbound(str(target), text)

    def send_approval_request(self, target: str | int,
                              interrupt_data: Any,
                              config: dict) -> str | None:
        """Send a task-approval prompt and store pending state."""
        task_name = config.get("task_name", "Unknown task")
        message = config.get("message", "Approval required.")
        resume_token = config.get("resume_token", "")
        text = f"Approval: {task_name}\n{message}\nReply YES or NO."
        try:
            send_outbound(str(target), text)
            import time as _time
            with _pending_lock:
                _pending_task_approvals[str(target)] = {
                    "resume_token": resume_token,
                    "task_name": task_name,
                    "_ts": _time.time(),
                }
            return str(target)
        except Exception as exc:
            log.warning("Failed to send SMS approval: %s", exc)
            return None

    def update_approval_message(self, message_ref: str,
                                status: str,
                                source: str = "") -> None:
        """Clear pending task approval and notify that it was resolved elsewhere."""
        with _pending_lock:
            _pending_task_approvals.pop(message_ref, None)
        action = "Approved" if status == "approved" else "Denied"
        try:
            send_outbound(message_ref,
                          f"{action} (via {source})" if source else action)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# Register with channel registry
# ──────────────────────────────────────────────────────────────────────
_sms_channel = SMSChannel()

try:
    from row_bot.channels import registry as _ch_registry
    _ch_registry.register(_sms_channel)
except Exception:
    pass
