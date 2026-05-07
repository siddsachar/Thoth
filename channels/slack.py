"""
Thoth – Slack Channel Adapter
================================
Socket Mode Slack bot that bridges messages to the Thoth agent.

Setup:
    1. Go to https://api.slack.com/apps → **Create New App** → From Scratch
    2. Under **OAuth & Permissions**, add Bot Token Scopes:
       ``app_mentions:read``, ``chat:write``, ``im:history``, ``im:read``,
       ``files:read``, ``files:write``, ``reactions:write``, ``users:read``
    3. Under **Socket Mode**, enable it → create an App-Level Token
       with ``connections:write`` scope
    4. Under **Event Subscriptions**, enable events and subscribe to:
       ``app_mention``, ``message.im``
    5. Under **App Home** → **Show Tabs**, enable **Messages Tab** and
       check *"Allow users to send Slash commands and messages from the
       messages tab"*
    6. Install the app to your workspace
    7. Copy the **Bot Token** (``xoxb-…``) and **App Token** (``xapp-…``)
    8. Paste both in Settings → Channels → Slack

Required keys (stored via api_keys):
    SLACK_BOT_TOKEN  – Bot user OAuth token (xoxb-…)
    SLACK_APP_TOKEN  – App-level token for Socket Mode (xapp-…)
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

import agent as agent_mod
from channels.base import Channel, ChannelCapabilities, ConfigField
from channels import commands as ch_commands
from channels import auth as ch_auth
from threads import _save_thread_meta, _list_threads

log = logging.getLogger("thoth.slack")


# ──────────────────────────────────────────────────────────────────────
# Markdown → Slack mrkdwn converter
# ──────────────────────────────────────────────────────────────────────
import re as _re

def _md_to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack mrkdwn format.

    Handles: **bold**→*bold*, *italic*→_italic_, headings→bold,
    [text](url)→<url|text>.  Code blocks/inline code unchanged
    (Slack uses backticks too).
    """
    # Protect fenced code blocks from further transforms
    blocks: list[str] = []
    def _stash_block(m):
        blocks.append(m.group(0))
        return f"\x00CODE{len(blocks) - 1}\x00"
    text = _re.sub(r"```[\s\S]*?```", _stash_block, text)

    # Protect inline code
    inlines: list[str] = []
    def _stash_inline(m):
        inlines.append(m.group(0))
        return f"\x00INLINE{len(inlines) - 1}\x00"
    text = _re.sub(r"`[^`]+`", _stash_inline, text)

    # Markdown links [text](url) → <url|text>
    text = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    # Bold **text** → *text*  (must come before italic)
    text = _re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # Italic *text* → _text_  (only standalone, not inside bold markers)
    text = _re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"_\1_", text)
    # Headings (# ... at start of line) → bold
    text = _re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=_re.MULTILINE)

    # Restore code
    for i, blk in enumerate(blocks):
        text = text.replace(f"\x00CODE{i}\x00", blk)
    for i, inl in enumerate(inlines):
        text = text.replace(f"\x00INLINE{i}\x00", inl)

    return text


# ──────────────────────────────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────────────────────────────
_app = None                             # slack_bolt.async_app.AsyncApp
_handler = None                         # AsyncSocketModeHandler
_running = False
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_user_id: str | None = None         # Bot's own user ID (to ignore own msgs)
_pending_interrupts: dict[str, dict] = {}   # channel_id → interrupt_data
_pending_task_approvals: dict[str, dict] = {}  # message_ts → {resume_token, task_name, dm_channel_id, _ts}
_approval_by_channel: dict[str, str] = {}        # dm_channel_id → message_ts (secondary index)
_pending_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────
# Helpers — credentials
# ──────────────────────────────────────────────────────────────────────
def _get_bot_token() -> str:
    return os.environ.get("SLACK_BOT_TOKEN", "")


def _get_app_token() -> str:
    return os.environ.get("SLACK_APP_TOKEN", "")


# ──────────────────────────────────────────────────────────────────────
# Configuration & lifecycle helpers
# ──────────────────────────────────────────────────────────────────────
def is_configured() -> bool:
    return bool(_get_bot_token()) and bool(_get_app_token())


def is_running() -> bool:
    return _running


# ──────────────────────────────────────────────────────────────────────
# Thread management
# ──────────────────────────────────────────────────────────────────────
def _make_thread_id(channel_id: str) -> str:
    return f"slack_{channel_id}"


def _get_or_create_thread(channel_id: str) -> str:
    """Return the Thoth thread ID for a Slack channel/DM, creating if needed."""
    thread_id = _make_thread_id(channel_id)
    name = f"💬 Slack – {channel_id}"
    _save_thread_meta(thread_id, name)  # creates or bumps updated_at
    return thread_id


def _new_thread(channel_id: str) -> str:
    """Force-create a new thread for this DM."""
    import time
    suffix = str(int(time.time()))
    thread_id = f"slack_{channel_id}_{suffix}"
    _save_thread_meta(thread_id, f"💬 Slack – {channel_id}")
    return thread_id


# ──────────────────────────────────────────────────────────────────────
# Agent execution
# ──────────────────────────────────────────────────────────────────────
def _run_agent_sync(user_text: str, config: dict,
                    event_queue=None) -> tuple[str, dict | None, list[bytes], list[str]]:
    """Run the agent synchronously, returning (answer, interrupt_data, images, video_paths)."""
    from tools import registry as tool_registry
    from channels.media_capture import grab_vision_capture, grab_generated_image, grab_generated_video

    config = {**config, "recursion_limit": agent_mod.RECURSION_LIMIT_CHAT}
    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    interrupt_data: dict | None = None
    used_vision = False
    used_image_gen = False
    used_video_gen = False

    for event_type, payload in agent_mod.stream_agent(user_text, enabled, config):
        if event_type == "token":
            full_answer.append(payload)
        elif event_type == "tool_call":
            tool_reports.append(f"🔧 Using {payload}…")
        elif event_type == "tool_done":
            name = payload['name'] if isinstance(payload, dict) else payload
            raw_name = payload.get('raw_name', '') if isinstance(payload, dict) else ''
            tool_reports.append(f"✅ {name} done")
            if name in ("analyze_image", "👁️ Vision"):
                used_vision = True
            if raw_name in ("generate_image", "edit_image"):
                used_image_gen = True
            if raw_name in ("generate_video", "animate_image"):
                used_video_gen = True
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "error":
            full_answer.append(f"⚠️ Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)
        if event_queue is not None:
            event_queue.put((event_type, payload))

    answer = "".join(full_answer)
    if tool_reports and answer:
        answer = "\n".join(tool_reports) + "\n\n" + answer
    elif tool_reports:
        answer = "\n".join(tool_reports)

    if event_queue is not None:
        event_queue.put(None)  # sentinel

    captured_images: list[bytes] = []
    captured_video_paths: list[str] = []
    if used_vision:
        img = grab_vision_capture()
        if img:
            captured_images.append(img)
    if used_image_gen:
        img = grab_generated_image()
        if img:
            captured_images.append(img)
    if used_video_gen:
        vid_path = grab_generated_video()
        if vid_path:
            captured_video_paths.append(vid_path)
    return answer or "_(No response)_", interrupt_data, captured_images, captured_video_paths


def _resume_agent_sync(config: dict, approved: bool,
                       *, interrupt_ids: list[str] | None = None) -> tuple[str, dict | None, list[bytes], list[str]]:
    """Resume a paused agent after interrupt approval/denial."""
    from tools import registry as tool_registry
    from channels.media_capture import grab_vision_capture, grab_generated_image, grab_generated_video

    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    interrupt_data: dict | None = None
    used_vision = False
    used_image_gen = False
    used_video_gen = False

    for event_type, payload in agent_mod.resume_stream_agent(
        enabled, config, approved, interrupt_ids=interrupt_ids
    ):
        if event_type == "token":
            full_answer.append(payload)
        elif event_type == "tool_call":
            tool_reports.append(f"🔧 Using {payload}…")
        elif event_type == "tool_done":
            name = payload['name'] if isinstance(payload, dict) else payload
            raw_name = payload.get('raw_name', '') if isinstance(payload, dict) else ''
            tool_reports.append(f"✅ {name} done")
            if name in ("analyze_image", "👁️ Vision"):
                used_vision = True
            if raw_name in ("generate_image", "edit_image"):
                used_image_gen = True
            if raw_name in ("generate_video", "animate_image"):
                used_video_gen = True
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "error":
            full_answer.append(f"⚠️ Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)

    answer = "".join(full_answer)
    if tool_reports and answer:
        answer = "\n".join(tool_reports) + "\n\n" + answer
    elif tool_reports:
        answer = "\n".join(tool_reports)

    captured_images: list[bytes] = []
    captured_video_paths: list[str] = []
    if used_vision:
        img = grab_vision_capture()
        if img:
            captured_images.append(img)
    if used_image_gen:
        img = grab_generated_image()
        if img:
            captured_images.append(img)
    if used_video_gen:
        vid_path = grab_generated_video()
        if vid_path:
            captured_video_paths.append(vid_path)
    return answer or "_(No response)_", interrupt_data, captured_images, captured_video_paths


# ──────────────────────────────────────────────────────────────────────
# Streaming: rate-limited edit consumer
# ──────────────────────────────────────────────────────────────────────
SLACK_MAX_MSG_LEN = 4000
_STREAM_EDIT_INTERVAL = 1.5


async def _slack_edit_consumer(app_client, channel_id: str, msg_ts: str,
                               event_queue: queue.Queue, loop) -> str | None:
    """Read events from queue and edit the Slack placeholder progressively.

    Returns the final display text, or None if it overflowed.
    """
    accumulated = ""
    tool_lines: list[str] = []
    last_edit = 0.0
    overflow = False
    last_sent_display = ""

    while True:
        try:
            event = await loop.run_in_executor(
                None, lambda: event_queue.get(timeout=0.2))
        except Exception:
            continue
        if event is None:
            break

        event_type, payload = event
        if event_type == "token":
            accumulated += payload
        elif event_type == "tool_call":
            tool_lines.append(f"🔧 Using {payload}…")
        elif event_type == "tool_done":
            name = payload['name'] if isinstance(payload, dict) else payload
            tool_lines.append(f"✅ {name} done")

        now = time.monotonic()
        if now - last_edit >= _STREAM_EDIT_INTERVAL and not overflow:
            display = _build_stream_display(tool_lines, accumulated)
            if len(display) > SLACK_MAX_MSG_LEN:
                overflow = True
                continue
            try:
                await app_client.chat_update(
                    channel=channel_id, ts=msg_ts,
                    text=_md_to_mrkdwn(display) if display else "⏳",
                )
                last_sent_display = display
            except Exception:
                pass
            last_edit = now

    display = _build_stream_display(tool_lines, accumulated)
    if display and not overflow:
        try:
            await app_client.chat_update(
                channel=channel_id, ts=msg_ts,
                text=_md_to_mrkdwn(display),
            )
            last_sent_display = display
        except Exception:
            pass
    return display if not overflow and last_sent_display == display else None


def _build_stream_display(tool_lines: list[str], accumulated: str) -> str:
    parts = []
    if tool_lines:
        parts.append("\n".join(tool_lines))
    if accumulated:
        parts.append(accumulated)
    return ("\n\n".join(parts)) if parts else ""


# ──────────────────────────────────────────────────────────────────────
# Message sending
# ──────────────────────────────────────────────────────────────────────
def _schedule_coro(coro):
    """Schedule a coroutine on the bot's event loop."""
    if _bot_loop and _bot_loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, _bot_loop)
    raise RuntimeError("Slack bot loop not running")


def send_outbound(channel_id: str, text: str) -> None:
    """Send a text message to a Slack channel/DM."""
    if not _running or _app is None:
        raise RuntimeError("Slack bot is not running")

    async def _send():
        await _app.client.chat_postMessage(channel=channel_id, text=text)

    fut = _schedule_coro(_send())
    fut.result(timeout=30)


def _resolve_dm_channel(user_or_channel_id: str) -> str:
    """Return the DM channel ID for a user ID.

    If *user_or_channel_id* already looks like a channel/DM ID (starts
    with ``C`` or ``D``) it is returned as-is.  Otherwise we send a
    message via ``chat_postMessage`` and capture the channel from the
    response.  This relies on Slack auto-opening the DM — no extra
    OAuth scopes needed beyond ``chat:write``.

    In practice this helper is unused because ``send_approval_request``
    uses ``_send_and_capture_channel`` directly.  Kept for other
    ad-hoc resolution needs.
    """
    if user_or_channel_id.startswith(("C", "D", "G")):
        return user_or_channel_id
    return user_or_channel_id  # fall through — let chat_postMessage resolve


def _send_and_capture_channel(target: str, text: str,
                              blocks: list | None = None) -> tuple[str, str]:
    """Send a message and return ``(dm_channel_id, message_ts)``.

    ``chat_postMessage(channel=<user_id>)`` auto-opens the DM and the
    response contains the actual ``channel`` ID (``D…``) along with the
    message ``ts``.  This avoids needing the ``im:write`` scope that
    ``conversations.open`` requires.
    """
    if not _running or _app is None:
        raise RuntimeError("Slack bot is not running")

    async def _send():
        kwargs: dict[str, Any] = {"channel": target, "text": text}
        if blocks:
            kwargs["blocks"] = blocks
        resp = await _app.client.chat_postMessage(**kwargs)
        return resp["channel"], resp["ts"]

    fut = _schedule_coro(_send())
    return fut.result(timeout=30)


def send_photo(channel_id: str, file_path: str,
               caption: str | None = None) -> None:
    """Upload and send a photo to a Slack channel/DM."""
    if not _running or _app is None:
        raise RuntimeError("Slack bot is not running")

    async def _send():
        await _app.client.files_upload_v2(
            channel=channel_id,
            file=file_path,
            title=caption or Path(file_path).name,
            initial_comment=caption or "",
        )

    fut = _schedule_coro(_send())
    fut.result(timeout=60)


def send_document(channel_id: str, file_path: str,
                  caption: str | None = None) -> None:
    """Upload and send a document to a Slack channel/DM."""
    send_photo(channel_id, file_path, caption)  # Same API for files


def _update_message_blocks(channel_id: str, message_ts: str,
                           new_text: str) -> None:
    """Edit an existing message, replacing blocks with plain text."""
    if not _running or _app is None:
        raise RuntimeError("Slack bot is not running")

    async def _update():
        await _app.client.chat_update(
            channel=channel_id, ts=message_ts,
            text=new_text, blocks=[],
        )

    fut = _schedule_coro(_update())
    fut.result(timeout=30)


# ──────────────────────────────────────────────────────────────────────
# Inbound message handling
# ──────────────────────────────────────────────────────────────────────
async def _handle_dm(event: dict, say, client) -> None:
    """Handle a direct message to the bot."""
    global _pending_interrupts

    user_id = event.get("user", "")
    channel_id = event.get("channel", "")
    text = event.get("text", "").strip()

    # Ignore bot's own messages
    if user_id == _bot_user_id:
        return

    if not text:
        return

    # Auth check — approve via DM pairing or user_id config
    if not _is_authorised(user_id):
        # Check if it's a pairing code
        if ch_auth.verify_pairing_code("slack", user_id, text):
            await say("✅ Paired successfully! You can now chat with Thoth.",
                       channel=channel_id)
            return
        await say("🔒 Not authorised. Send your pairing code to get started.",
                   channel=channel_id)
        return

    from channels.base import record_activity
    record_activity("slack")

    # Slash command dispatch
    cmd_response = ch_commands.dispatch("slack", text)
    if cmd_response is not None:
        await say(_md_to_mrkdwn(cmd_response), channel=channel_id)
        return

    # Handle /new specially — update thread
    if text.lower().startswith("/new"):
        _new_thread(channel_id)

    # Check pending task approvals first (background workflows)
    from channels import approval as approval_helpers
    with _pending_lock:
        _ts_key = _approval_by_channel.get(channel_id)
        task_approval = _pending_task_approvals.get(_ts_key) if _ts_key else None
    if task_approval:
        decision = approval_helpers.is_approval_text(text)
        if decision is not None:
            with _pending_lock:
                _pending_task_approvals.pop(_ts_key, None)
                _approval_by_channel.pop(channel_id, None)
            try:
                from tasks import respond_to_approval
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: respond_to_approval(
                        task_approval["resume_token"], decision,
                        note=f"{'Approved' if decision else 'Denied'} via Slack",
                        source="slack",
                    ),
                )
                action = "✅ Approved" if decision else "❌ Denied"
                if result:
                    # Edit the original button message to show resolution
                    if _ts_key:
                        try:
                            _update_message_blocks(
                                channel_id, _ts_key,
                                f"{action} — {task_approval['task_name']}",
                            )
                        except Exception:
                            await say(f"{action} — {task_approval['task_name']}",
                                      channel=channel_id)
                    else:
                        await say(f"{action} — {task_approval['task_name']}",
                                  channel=channel_id)
                else:
                    await say("⚠️ Could not process approval — it may have expired.",
                              channel=channel_id)
            except Exception as exc:
                log.error("Task approval error: %s", exc)
                await say(f"⚠️ Error processing approval: {exc}",
                          channel=channel_id)
            return

    # Check pending live-chat interrupts
    with _pending_lock:
        interrupt = _pending_interrupts.pop(channel_id, None)

    if interrupt:
        approved = text.lower() in approval_helpers._YES_WORDS
        config = interrupt.get("config", {})
        interrupt_ids = approval_helpers.extract_interrupt_ids(
            interrupt.get("data")
        )

        loop = asyncio.get_event_loop()
        answer, new_interrupt, captured, captured_vids = await loop.run_in_executor(
            None,
            lambda: _resume_agent_sync(
                config, approved, interrupt_ids=interrupt_ids
            ),
        )
        if answer:
            await say(_md_to_mrkdwn(answer), channel=channel_id)
        for img_bytes in captured:
            try:
                import io
                await _app.client.files_upload_v2(
                    channel=channel_id,
                    file=io.BytesIO(img_bytes),
                    filename="image.png",
                    title="🖼️ Image",
                )
            except Exception as exc:
                log.warning("Failed to send Slack image: %s", exc)
        for vpath in captured_vids:
            try:
                await _app.client.files_upload_v2(
                    channel=channel_id,
                    file=vpath,
                    filename="video.mp4",
                    title="🎬 Video",
                )
            except Exception as exc:
                log.warning("Failed to send Slack video: %s", exc)
        if new_interrupt:
            with _pending_lock:
                _pending_interrupts[channel_id] = {
                    "data": new_interrupt, "config": config
                }
            detail = approval_helpers.format_interrupt_text(new_interrupt)
            await say(_md_to_mrkdwn(detail) + "\n\nReply *yes* or *no*.",
                      channel=channel_id)
        return

    # Normal message — run agent
    thread_id = _get_or_create_thread(channel_id)
    config = {"configurable": {"thread_id": thread_id}}

    # Add typing reaction
    ts = event.get("ts", "")
    try:
        if ts:
            await client.reactions_add(channel=channel_id, name="eyes",
                                        timestamp=ts)
    except Exception:
        pass

    # Send placeholder for streaming edits
    loop = asyncio.get_event_loop()
    placeholder_ts: str | None = None
    try:
        resp = await client.chat_postMessage(channel=channel_id, text="⏳")
        placeholder_ts = resp.get("ts") or resp.get("message", {}).get("ts")
    except Exception:
        pass

    eq: queue.Queue = queue.Queue()
    streamed_display: str | None = None
    consumer_task = None

    try:
        # Run agent + streaming consumer in parallel
        agent_fut = loop.run_in_executor(
            None, _run_agent_sync, text, config, eq
        )
        consumer_task = asyncio.ensure_future(
            _slack_edit_consumer(client, channel_id, placeholder_ts, eq, loop)
        ) if placeholder_ts else None

        answer, interrupt_data, captured_images, captured_video_paths = await agent_fut
        if consumer_task:
            streamed_display = await consumer_task

    except Exception as exc:
        log.error("Agent error for channel %s: %s", channel_id, exc)
        # Drain queue to unblock agent thread
        try:
            while True:
                eq.get_nowait()
        except Exception:
            pass
        if consumer_task and not consumer_task.done():
            consumer_task.cancel()

        # Delete placeholder
        if placeholder_ts:
            try:
                await client.chat_delete(channel=channel_id, ts=placeholder_ts)
            except Exception:
                pass

        from channels.thread_repair import is_corrupt_thread_error
        if is_corrupt_thread_error(exc):
            try:
                from agent import repair_orphaned_tool_calls
                await loop.run_in_executor(
                    None, repair_orphaned_tool_calls, None, config
                )
                log.info("Repaired orphaned tool calls for %s, retrying", channel_id)
                answer, interrupt_data, captured_images, captured_video_paths = await loop.run_in_executor(
                    None, _run_agent_sync, text, config
                )
            except Exception as retry_exc:
                log.error("Retry after repair failed: %s", retry_exc)
                _new_thread(channel_id)
                try:
                    if ts:
                        await client.reactions_remove(channel=channel_id,
                                                       name="eyes", timestamp=ts)
                        await client.reactions_add(channel=channel_id,
                                                    name="x", timestamp=ts)
                except Exception:
                    pass
                await say("⚠️ The previous conversation had a stuck tool call "
                          "and couldn't be repaired.\n"
                          "🆕 I've started a fresh thread — please resend your message.",
                          channel=channel_id)
                return
            # Repair succeeded — send answer normally
            try:
                if ts:
                    await client.reactions_remove(channel=channel_id,
                                                   name="eyes", timestamp=ts)
                    await client.reactions_add(channel=channel_id,
                                                name="thumbsup", timestamp=ts)
            except Exception:
                pass
            if answer:
                await say(_md_to_mrkdwn(answer), channel=channel_id)
            for img_bytes in captured_images:
                try:
                    import io
                    await _app.client.files_upload_v2(
                        channel=channel_id,
                        file=io.BytesIO(img_bytes),
                        filename="image.png",
                        title="🖼️ Image",
                    )
                except Exception as img_exc:
                    log.warning("Failed to send Slack image: %s", img_exc)
            for vpath in captured_video_paths:
                try:
                    await _app.client.files_upload_v2(
                        channel=channel_id,
                        file=vpath,
                        filename="video.mp4",
                        title="🎬 Video",
                    )
                except Exception as vid_exc:
                    log.warning("Failed to send Slack video: %s", vid_exc)
            if interrupt_data:
                with _pending_lock:
                    _pending_interrupts[channel_id] = {
                        "data": interrupt_data, "config": config
                    }
                from channels import approval as approval_helpers
                detail = approval_helpers.format_interrupt_text(interrupt_data)
                await say(_md_to_mrkdwn(detail) + "\n\nReply *yes* or *no*.",
                          channel=channel_id)
            return
        else:
            try:
                if ts:
                    await client.reactions_remove(channel=channel_id,
                                                   name="eyes", timestamp=ts)
                    await client.reactions_add(channel=channel_id,
                                                name="x", timestamp=ts)
            except Exception:
                pass
            await say(f"⚠️ Error: {exc}", channel=channel_id)
            return

    # Success — update reaction
    try:
        if ts:
            await client.reactions_remove(channel=channel_id, name="eyes",
                                           timestamp=ts)
            await client.reactions_add(channel=channel_id, name="thumbsup",
                                        timestamp=ts)
    except Exception:
        pass

    # Extract YouTube URLs for separate messages (auto-preview)
    from channels import extract_youtube_urls
    clean_answer, yt_urls = extract_youtube_urls(answer) if answer else ("", [])

    # If streaming covered the full response and no interrupt, just send images/URLs
    if streamed_display and not interrupt_data:
        for url in yt_urls:
            await say(url, channel=channel_id)
        for img_bytes in captured_images:
            try:
                import io
                await _app.client.files_upload_v2(
                    channel=channel_id,
                    file=io.BytesIO(img_bytes),
                    filename="image.png",
                    title="🖼️ Image",
                )
            except Exception as exc:
                log.warning("Failed to send Slack image: %s", exc)
        for vpath in captured_video_paths:
            try:
                await _app.client.files_upload_v2(
                    channel=channel_id,
                    file=vpath,
                    filename="video.mp4",
                    title="🎬 Video",
                )
            except Exception as exc:
                log.warning("Failed to send Slack video: %s", exc)
        return

    # Streaming overflowed or was not used — delete placeholder, send normally
    if placeholder_ts:
        try:
            await client.chat_delete(channel=channel_id, ts=placeholder_ts)
        except Exception:
            pass

    if clean_answer:
        await say(_md_to_mrkdwn(clean_answer), channel=channel_id)
    for url in yt_urls:
        await say(url, channel=channel_id)

    for img_bytes in captured_images:
        try:
            import io
            await _app.client.files_upload_v2(
                channel=channel_id,
                file=io.BytesIO(img_bytes),
                filename="image.png",
                title="🖼️ Image",
            )
        except Exception as exc:
            log.warning("Failed to send Slack image: %s", exc)

    for vpath in captured_video_paths:
        try:
            await _app.client.files_upload_v2(
                channel=channel_id,
                file=vpath,
                filename="video.mp4",
                title="🎬 Video",
            )
        except Exception as exc:
            log.warning("Failed to send Slack video: %s", exc)

    if interrupt_data:
        with _pending_lock:
            _pending_interrupts[channel_id] = {
                "data": interrupt_data, "config": config
            }
        from channels import approval as approval_helpers
        detail = approval_helpers.format_interrupt_text(interrupt_data)
        await say(_md_to_mrkdwn(detail) + "\n\nReply *yes* or *no*.",
                  channel=channel_id)


async def _handle_approval_button(body: dict, client, *, approved: bool) -> None:
    """Handle a Block Kit approval button press (task_approve / task_deny)."""
    action = body.get("actions", [{}])[0]
    resume_token = action.get("value", "")
    message = body.get("message", {})
    msg_ts = message.get("ts", "")
    channel_id = body.get("channel", {}).get("id", "")

    with _pending_lock:
        info = _pending_task_approvals.pop(msg_ts, None)
        if info:
            _approval_by_channel.pop(info.get("dm_channel_id", ""), None)

    if info is None:
        # Already handled (e.g. via text reply or another channel)
        try:
            await client.chat_update(
                channel=channel_id, ts=msg_ts,
                text="ℹ️ This approval is no longer pending.",
                blocks=[],
            )
        except Exception:
            pass
        return

    task_name = info.get("task_name", "Task")
    status_text = f"{'Approved ✅' if approved else 'Denied ❌'} — {task_name}"

    # Edit the message to show resolution (remove buttons)
    try:
        await client.chat_update(
            channel=channel_id, ts=msg_ts,
            text=status_text, blocks=[],
        )
    except Exception as exc:
        log.warning("Failed to update Slack approval message: %s", exc)

    # Run respond_to_approval in executor to avoid blocking
    try:
        from tasks import respond_to_approval
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: respond_to_approval(
                info["resume_token"], approved,
                note=f"{'Approved' if approved else 'Denied'} via Slack",
                source="slack",
            ),
        )
        if not result:
            try:
                await client.chat_postMessage(
                    channel=channel_id,
                    text="⚠️ Could not process approval — it may have expired.",
                )
            except Exception:
                pass
    except Exception as exc:
        log.error("Task approval error (button): %s", exc)
        try:
            await client.chat_postMessage(
                channel=channel_id, text=f"⚠️ Error: {exc}",
            )
        except Exception:
            pass


async def _handle_mention(event: dict, say, client) -> None:
    """Handle @mention of the bot in a channel."""
    # Strip the mention prefix and treat like a DM
    text = event.get("text", "")
    # Remove <@BOT_ID> mentions
    if _bot_user_id:
        import re
        text = re.sub(rf"<@{_bot_user_id}>", "", text).strip()

    # Create a synthetic event with cleaned text
    clean_event = dict(event)
    clean_event["text"] = text
    await _handle_dm(clean_event, say, client)


async def _handle_file(event: dict, say, client) -> None:
    """Handle file uploads (images, documents, voice memos)."""
    user_id = event.get("user", "")
    channel_id = event.get("channel", "")

    if user_id == _bot_user_id:
        return

    if not _is_authorised(user_id):
        return

    files = event.get("files", [])
    if not files:
        return

    from channels.media import (
        transcribe_audio, analyze_image, save_inbound_file,
        extract_document_text, copy_to_workspace,
    )

    for file_info in files:
        mimetype = file_info.get("mimetype", "")
        filename = file_info.get("name", "file")
        url = file_info.get("url_private_download", "")

        if not url:
            continue

        # Download the file
        try:
            resp = await client.api_call(
                "files.info",
                params={"file": file_info["id"]}
            )
            import httpx
            async with httpx.AsyncClient() as http:
                dl_resp = await http.get(
                    url,
                    headers={"Authorization": f"Bearer {_get_bot_token()}"},
                )
                dl_resp.raise_for_status()
                file_data = dl_resp.content
        except Exception as exc:
            log.warning("Failed to download Slack file %s: %s", filename, exc)
            continue

        caption = event.get("text", "").strip()
        thread_id = _get_or_create_thread(channel_id)
        config = {"configurable": {"thread_id": thread_id}}

        if mimetype.startswith("audio/"):
            ext = Path(filename).suffix or ".ogg"
            transcript = transcribe_audio(file_data, ext)
            if transcript:
                user_text = f"[Voice message transcription]: {transcript}"
                if caption:
                    user_text += f"\n\nCaption: {caption}"
                loop = asyncio.get_event_loop()
                answer, _, _, _ = await loop.run_in_executor(
                    None, _run_agent_sync, user_text, config
                )
                if answer:
                    await say(_md_to_mrkdwn(answer), channel=channel_id)

        elif mimetype.startswith("image/"):
            analysis = analyze_image(file_data, caption or "Describe this image")
            if analysis:
                user_text = f"[Image analysis]: {analysis}"
                if caption:
                    user_text += f"\n\nUser said: {caption}"
                loop = asyncio.get_event_loop()
                answer, _, _, _ = await loop.run_in_executor(
                    None, _run_agent_sync, user_text, config
                )
                if answer:
                    await say(_md_to_mrkdwn(answer), channel=channel_id)

        else:
            saved = save_inbound_file(file_data, filename)
            extracted = extract_document_text(file_data, filename)
            ws_rel = copy_to_workspace(saved)

            parts = [f"[User sent a file: {filename}]"]
            if ws_rel:
                parts.append(f"Workspace path: {ws_rel}")
            else:
                parts.append(f"Saved to: {saved}")
            if extracted:
                parts.append(
                    f"\n--- File content ---\n{extracted}"
                    f"\n--- End of file ---")
            else:
                parts.append(
                    "(Could not extract text — file may be "
                    "image-based or unsupported format)")
            if caption:
                parts.append(f"\nUser's message: {caption}")

            user_text = "\n".join(parts)
            loop = asyncio.get_event_loop()
            answer, _, _, _ = await loop.run_in_executor(
                None, _run_agent_sync, user_text, config
            )
            if answer:
                await say(_md_to_mrkdwn(answer), channel=channel_id)


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────
def _is_authorised(user_id: str) -> bool:
    """Check if a Slack user is authorised."""
    # Check config-based user ID
    allowed = os.environ.get("SLACK_USER_ID", "").strip()
    if allowed and user_id == allowed:
        return True
    # Check DM pairing approved list
    return ch_auth.is_user_approved("slack", user_id)


# ──────────────────────────────────────────────────────────────────────
# Bot lifecycle
# ──────────────────────────────────────────────────────────────────────
async def start_bot() -> bool:
    """Start the Slack bot with Socket Mode."""
    global _app, _handler, _running, _bot_loop, _bot_user_id

    if _running:
        log.info("Slack bot already running")
        return True

    bot_token = _get_bot_token()
    app_token = _get_app_token()

    if not bot_token or not app_token:
        log.warning("Slack bot token or app token not configured")
        return False

    try:
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import (
            AsyncSocketModeHandler,
        )

        _app = AsyncApp(token=bot_token)

        # Register action handlers for Block Kit approval buttons
        @_app.action("task_approve")
        async def _on_task_approve(ack, body, client):
            await ack()
            await _handle_approval_button(body, client, approved=True)

        @_app.action("task_deny")
        async def _on_task_deny(ack, body, client):
            await ack()
            await _handle_approval_button(body, client, approved=False)

        # Register event handlers
        @_app.event("message")
        async def _on_message(event, say, client):
            # Only handle DMs (im) and file shares
            channel_type = event.get("channel_type", "")
            subtype = event.get("subtype", "")

            # Skip message_changed, message_deleted, etc.
            if subtype and subtype not in ("file_share",):
                return

            if subtype == "file_share" or event.get("files"):
                await _handle_file(event, say, client)
            elif channel_type == "im":
                await _handle_dm(event, say, client)

        @_app.event("app_mention")
        async def _on_mention(event, say, client):
            await _handle_mention(event, say, client)

        # Get bot's own user ID
        try:
            auth_resp = await _app.client.auth_test()
            _bot_user_id = auth_resp.get("user_id")
            log.info("Slack bot user ID: %s", _bot_user_id)
        except Exception as exc:
            log.warning("Could not determine bot user ID: %s", exc)

        _handler = AsyncSocketModeHandler(_app, app_token)
        _bot_loop = asyncio.get_event_loop()

        # Connect without blocking (start_async sleeps forever)
        await _handler.connect_async()
        _running = True
        log.info("Slack bot started (Socket Mode)")
        return True

    except ImportError:
        log.error("slack-bolt not installed. Run: pip install slack-bolt")
        return False
    except Exception as exc:
        log.error("Failed to start Slack bot: %s", exc)
        _running = False
        return False


async def stop_bot() -> None:
    """Stop the Slack bot gracefully."""
    global _app, _handler, _running, _bot_loop, _bot_user_id

    if not _running:
        return

    try:
        if _handler:
            await _handler.close_async()
    except Exception as exc:
        log.warning("Error stopping Slack handler: %s", exc)

    _app = None
    _handler = None
    _running = False
    _bot_loop = None
    _bot_user_id = None
    log.info("Slack bot stopped")


# ──────────────────────────────────────────────────────────────────────
# Outbound helpers (used by tool_factory)
# ──────────────────────────────────────────────────────────────────────
_send_photo_fn = send_photo
_send_document_fn = send_document


class SlackChannel(Channel):
    """Slack channel adapter implementing the Channel ABC.

    Uses slack-bolt with Socket Mode — no public URL required.
    """

    @property
    def name(self) -> str:
        return "slack"

    @property
    def display_name(self) -> str:
        return "Slack"

    @property
    def icon(self) -> str:
        return "tag"  # Slack-like icon from Material Design

    @property
    def setup_guide(self) -> str:
        return (
            "### Quick Setup\n"
            "1. Go to [api.slack.com/apps](https://api.slack.com/apps) → "
            "**Create New App** → From Scratch\n"
            "2. **OAuth & Permissions** → add scopes: `app_mentions:read`, "
            "`chat:write`, `im:history`, `im:read`, `files:read`, "
            "`files:write`, `reactions:write`, `users:read`\n"
            "3. **Socket Mode** → enable → create App-Level Token "
            "with `connections:write`\n"
            "4. **Event Subscriptions** → enable → subscribe to: "
            "`app_mention`, `message.im`\n"
            "5. **App Home** → **Show Tabs** → enable **Messages Tab** "
            "and check *\"Allow users to send Slash commands and messages "
            "from the messages tab\"*\n"
            "6. Install to workspace, copy **Bot Token** (`xoxb-…`) "
            "and **App Token** (`xapp-…`)\n"
            "7. Paste both below and click **Save**, then **▶️ Start Bot**"
        )

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            photo_in=True,
            voice_in=True,
            document_in=True,
            photo_out=True,
            document_out=True,
            buttons=True,
            streaming=True,
            typing=False,        # Slack bot typing indicator is limited
            reactions=True,
            slash_commands=True,
        )

    @property
    def config_fields(self) -> list[ConfigField]:
        return [
            ConfigField(
                key="bot_token",
                label="Bot Token (xoxb-…)",
                field_type="password",
                storage="env",
                env_key="SLACK_BOT_TOKEN",
                help_text="Bot user OAuth token from Slack app settings",
            ),
            ConfigField(
                key="app_token",
                label="App Token (xapp-…)",
                field_type="password",
                storage="env",
                env_key="SLACK_APP_TOKEN",
                help_text="App-level token for Socket Mode",
            ),
            ConfigField(
                key="user_id",
                label="Your Slack User ID (optional)",
                field_type="text",
                storage="env",
                env_key="SLACK_USER_ID",
                help_text="Your Slack user ID for auto-auth (or use DM pairing)",
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
        uid = os.environ.get("SLACK_USER_ID", "").strip()
        if uid:
            return uid
        # Fall back to first approved user
        approved = ch_auth.get_approved_users("slack")
        if approved:
            return approved[0]
        raise RuntimeError("No Slack user configured — set SLACK_USER_ID or pair via DM")

    def send_message(self, target: str | int, text: str) -> None:
        send_outbound(str(target), text)

    def send_photo(self, target: str | int, file_path: str,
                   caption: str | None = None) -> None:
        _send_photo_fn(str(target), file_path, caption=caption)

    def send_document(self, target: str | int, file_path: str,
                      caption: str | None = None) -> None:
        _send_document_fn(str(target), file_path, caption=caption)

    def send_approval_request(self, target: str | int,
                              interrupt_data: Any,
                              config: dict) -> str | None:
        """Send a task-approval prompt with Block Kit buttons.

        *target* is typically a user ID (from ``get_default_target``).
        We use ``chat_postMessage`` with blocks containing ✅/❌ buttons.
        The pending approval is stored keyed by ``message_ts`` (for
        button clicks) with a secondary index by channel (for text
        fallback "yes"/"no" replies).
        """
        task_name = config.get("task_name", "Unknown task")
        message = config.get("message", "Approval required to continue.")
        resume_token = config.get("resume_token", "")
        fallback_text = (
            f"⚠️ Approval Required\n"
            f"Task: {task_name}\n"
            f"{message}"
        )
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"⚠️ *Approval Required*\n\n"
                        f"*Task:* {task_name}\n"
                        f"{message}"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve"},
                        "style": "primary",
                        "action_id": "task_approve",
                        "value": resume_token,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Deny"},
                        "style": "danger",
                        "action_id": "task_deny",
                        "value": resume_token,
                    },
                ],
            },
        ]
        try:
            dm_channel_id, msg_ts = _send_and_capture_channel(
                str(target), fallback_text, blocks=blocks,
            )
            import time as _time
            with _pending_lock:
                _pending_task_approvals[msg_ts] = {
                    "resume_token": resume_token,
                    "task_name": task_name,
                    "dm_channel_id": dm_channel_id,
                    "_ts": _time.time(),
                }
                _approval_by_channel[dm_channel_id] = msg_ts
            return msg_ts
        except Exception as exc:
            log.warning("Failed to send Slack approval: %s", exc)
            return None

    def update_approval_message(self, message_ref: str,
                                status: str,
                                source: str = "") -> None:
        """Edit the original Block Kit message to show resolution status."""
        with _pending_lock:
            info = _pending_task_approvals.pop(message_ref, None)
            if info:
                _approval_by_channel.pop(info.get("dm_channel_id", ""), None)
        action = "Approved ✅" if status == "approved" else "Denied ❌"
        task_name = info["task_name"] if info else "Task"
        msg = f"{action} — {task_name}"
        if source:
            msg += f" (via {source})"
        dm_channel = info.get("dm_channel_id") if info else None
        if dm_channel and message_ref:
            try:
                _update_message_blocks(dm_channel, message_ref, msg)
                return
            except Exception:
                pass
        # Fallback: send a new message
        if dm_channel:
            try:
                send_outbound(dm_channel, msg)
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────
# Register with channel registry
# ──────────────────────────────────────────────────────────────────────
_slack_channel = SlackChannel()

try:
    from channels import registry as _ch_registry
    _ch_registry.register(_slack_channel)
except Exception:
    pass
