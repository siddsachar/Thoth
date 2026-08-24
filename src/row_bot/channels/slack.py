"""
Row-Bot – Slack Channel Adapter
================================
Socket Mode Slack bot that bridges messages to the Row-Bot agent.

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
import queue
import threading
import time
from pathlib import Path
from typing import Any

from row_bot.channels.base import Channel, ChannelCapabilities, ConfigField
from row_bot.channels.auth_store import get_channel_secret
from row_bot.channels import commands as ch_commands
from row_bot.channels import auth as ch_auth
from row_bot.channels import runtime as ch_runtime
from row_bot.channels.streaming import (
    ChannelDeliveryResult,
    ChannelRateLimitError,
    ChannelStreamConfig,
    ChannelStreamConsumer,
    default_split_text,
)
from row_bot.threads import _save_thread_meta

log = logging.getLogger("row_bot.slack")


def _agent_mod():
    import row_bot.agent as agent_mod

    return agent_mod


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
_pending_interrupts: dict[str, dict] = {}   # context_key -> interrupt_data
_interrupt_by_message: dict[str, str] = {}   # approval message ts -> context_key
_interrupt_by_channel: dict[str, str] = {}   # channel_id -> latest context_key
_pending_task_approvals: dict[str, dict] = {}  # message_ts → {resume_token, task_name, dm_channel_id, _ts}
_approval_by_channel: dict[str, str] = {}        # dm_channel_id → message_ts (secondary index)
_pending_lock = threading.Lock()


# ──────────────────────────────────────────────────────────────────────
# Helpers — credentials
# ──────────────────────────────────────────────────────────────────────
def _get_bot_token() -> str:
    return get_channel_secret("slack", "SLACK_BOT_TOKEN")


def _get_app_token() -> str:
    return get_channel_secret("slack", "SLACK_APP_TOKEN")


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
    """Return the Row-Bot thread ID for a Slack channel/DM, creating if needed."""
    thread_id = _make_thread_id(channel_id)
    name = f"💬 Slack – {channel_id}"
    _save_thread_meta(
        thread_id,
        name,
        seed_default_skills=True,
        allow_recreate=True,
    )  # creates or bumps updated_at
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="slack",
            target=channel_id,
            external_conversation_id=str(channel_id),
        )
    except Exception:
        log.debug("Slack thread channel ref skipped", exc_info=True)
    return thread_id


def _new_thread(channel_id: str) -> str:
    """Force-create a new thread for this DM."""
    import time
    suffix = str(int(time.time()))
    thread_id = f"slack_{channel_id}_{suffix}"
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="slack",
            target=channel_id,
            external_conversation_id=str(channel_id),
        )
    except Exception:
        log.debug("Slack thread channel ref skipped", exc_info=True)
    _save_thread_meta(
        thread_id,
        f"💬 Slack – {channel_id}",
        seed_default_skills=True,
        allow_recreate=True,
    )
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
            "channel_streaming": purpose != "approval",
            "approval_mode": approval_mode_for_config(config),
        },
    }


def _run_agent_sync(user_text: str, config: dict,
                    event_queue=None) -> tuple[str, dict | None, list[bytes], list[str]]:
    """Run the agent synchronously, returning (answer, interrupt_data, images, video_paths)."""
    from row_bot.tools import registry as tool_registry
    from row_bot.channels.media_capture import grab_vision_capture, grab_generated_image, grab_generated_video

    agent_mod = _agent_mod()
    config = build_channel_runtime_config(config, "message")
    from row_bot.channels import runtime as orchestration_runtime

    config = orchestration_runtime.prepare_channel_turn_config(config, user_text)
    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    reasoning_notices: list[str] = []
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
        elif event_type == "reasoning_fallback":
            reasoning_notices.append(str((payload or {}).get("message") or "Provider default reasoning was used."))
        elif event_type == "error":
            full_answer.append(f"⚠️ Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)
        if event_queue is not None:
            event_queue.put((event_type, payload))

    from row_bot.channels.agent_output import assemble_agent_answer

    answer = assemble_agent_answer("".join(full_answer), tool_reports, reasoning_notices)

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


def _resume_agent_sync(
    config: dict,
    approved: bool,
    *,
    interrupt_ids: list[str] | None = None,
    event_queue=None,
) -> tuple[str, dict | None, list[bytes], list[str]]:
    """Resume a paused agent after interrupt approval/denial."""
    agent_mod = _agent_mod()
    config = build_channel_runtime_config(config, "approval")
    from row_bot.tools import registry as tool_registry
    from row_bot.channels.media_capture import grab_vision_capture, grab_generated_image, grab_generated_video

    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    reasoning_notices: list[str] = []
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
        elif event_type == "reasoning_fallback":
            reasoning_notices.append(str((payload or {}).get("message") or "Provider default reasoning was used."))
        elif event_type == "error":
            full_answer.append(f"⚠️ Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)
        if event_queue is not None:
            event_queue.put((event_type, payload))

    from row_bot.channels.agent_output import assemble_agent_answer

    answer = assemble_agent_answer("".join(full_answer), tool_reports, reasoning_notices)

    if event_queue is not None:
        event_queue.put(None)

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


def _slack_retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if isinstance(headers, dict):
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is not None:
            try:
                return float(value)
            except Exception:
                pass
    value = getattr(exc, "retry_after", None)
    if value is not None:
        try:
            return float(value)
        except Exception:
            pass
    if "ratelimited" in str(exc).lower() or "rate_limited" in str(exc).lower():
        return 1.0
    return None


class SlackStreamTransport:
    """Slack native-stream/edit transport for the shared streaming engine."""

    def __init__(
        self,
        client,
        *,
        channel_id: str,
        transport_mode: str = "auto",
        thread_ts: str | None = None,
    ) -> None:
        self.client = client
        self.channel_id = str(channel_id)
        self.thread_ts = str(thread_ts or "") or None
        self.requested_mode = str(transport_mode or "auto").lower()
        self._selected_mode = "edit"
        self._native_text = ""
        self.transport_name = f"slack:{self.requested_mode}"

    async def send_typing(self) -> None:
        return None

    async def start(self, text: str):
        mode = self._select_mode()
        self._selected_mode = mode
        self.transport_name = f"slack:{mode}"
        if mode == "native":
            try:
                handle = await self._native_start(text)
                self._native_text = str(text or "")
                return handle
            except Exception:
                log.info("Slack native stream failed; falling back to edit", exc_info=True)
                self._selected_mode = "edit"
                self.transport_name = "slack:edit"
        return await self._edit_start(text)

    async def update(self, handle, text: str, *, final: bool = False):
        if self._selected_mode == "native":
            return await self._native_update(handle, text, final=final)
        try:
            return await self.client.chat_update(
                channel=self.channel_id,
                ts=self._handle_ts(handle),
                text=_md_to_mrkdwn(str(text or "")),
            )
        except Exception as exc:
            retry_after = _slack_retry_after(exc)
            if retry_after is not None:
                raise ChannelRateLimitError(str(exc), retry_after=retry_after) from exc
            raise

    async def send_final(self, text: str) -> list[Any]:
        refs: list[Any] = []
        for chunk in self.split_text(str(text or "")):
            try:
                resp = await self.client.chat_postMessage(
                    channel=self.channel_id,
                    text=_md_to_mrkdwn(chunk),
                    **self._thread_kwargs(),
                )
            except Exception as exc:
                retry_after = _slack_retry_after(exc)
                if retry_after is not None:
                    raise ChannelRateLimitError(str(exc), retry_after=retry_after) from exc
                raise
            refs.append(resp.get("ts") if isinstance(resp, dict) else resp)
        return refs

    async def cleanup_preview(self, handle) -> None:
        if self._selected_mode == "native":
            return
        try:
            await self.client.chat_delete(channel=self.channel_id, ts=self._handle_ts(handle))
        except Exception:
            pass

    def split_text(self, text: str) -> list[str]:
        return default_split_text(str(text or ""), SLACK_MAX_MSG_LEN)

    def _select_mode(self) -> str:
        if self.requested_mode == "off":
            return "off"
        native_capable = all(
            callable(getattr(self.client, name, None))
            for name in ("chat_startStream", "chat_appendStream", "chat_stopStream")
        )
        if self.requested_mode in {"auto", "native"} and native_capable and self.thread_ts:
            return "native"
        return "edit"

    async def _edit_start(self, text: str):
        return await self.client.chat_postMessage(
            channel=self.channel_id,
            text=_md_to_mrkdwn(str(text or "")),
            **self._thread_kwargs(),
        )

    async def _native_start(self, text: str):
        return await self.client.chat_startStream(
            channel=self.channel_id,
            thread_ts=self.thread_ts,
            markdown_text=_md_to_mrkdwn(str(text or "")),
        )

    async def _native_update(self, handle, text: str, *, final: bool = False):
        clean_text = str(text or "")
        delta = clean_text[len(self._native_text):] if clean_text.startswith(self._native_text) else clean_text
        if delta and not final:
            await self.client.chat_appendStream(
                channel=self.channel_id,
                ts=self._handle_ts(handle),
                markdown_text=_md_to_mrkdwn(delta),
            )
            self._native_text = clean_text
            return handle
        if final:
            if delta:
                await self.client.chat_appendStream(
                    channel=self.channel_id,
                    ts=self._handle_ts(handle),
                    markdown_text=_md_to_mrkdwn(delta),
                )
            await self.client.chat_stopStream(
                channel=self.channel_id,
                ts=self._handle_ts(handle),
                markdown_text=_md_to_mrkdwn(clean_text),
            )
            self._native_text = clean_text
        return handle

    def _thread_kwargs(self) -> dict[str, str]:
        return {"thread_ts": self.thread_ts} if self.thread_ts else {}

    def _handle_ts(self, handle) -> str:
        if isinstance(handle, dict):
            return str(
                handle.get("ts")
                or handle.get("stream_ts")
                or (handle.get("message") or {}).get("ts")
                or ""
            )
        return str(handle or "")


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


def _cfg_float(channel_config: dict, key: str, default: float) -> float:
    try:
        return float(channel_config.get(key, default))
    except Exception:
        return default


def _cfg_int(channel_config: dict, key: str, default: int) -> int:
    try:
        return int(channel_config.get(key, default))
    except Exception:
        return default


def _slack_stream_config() -> ChannelStreamConfig:
    try:
        from row_bot.channels import config as channel_config_store

        channel_config = channel_config_store.get_all("slack")
    except Exception:
        channel_config = {}
    enabled = bool(channel_config.get("streaming.enabled", True))
    return ChannelStreamConfig(
        channel="slack",
        transport_mode=str(channel_config.get("streaming.transport", "auto") if enabled else "off"),
        update_interval_s=_cfg_float(channel_config, "streaming.update_interval_s", 0.8),
        max_stale_interval_s=_cfg_float(channel_config, "streaming.max_stale_interval_s", 3.0),
        min_update_chars=_cfg_int(channel_config, "streaming.min_update_chars", 32),
        typing_interval_s=None,
        cursor=str(channel_config.get("streaming.cursor", "...")),
        max_message_units=SLACK_MAX_MSG_LEN,
        fresh_final_after_s=_cfg_float(channel_config, "streaming.fresh_final_after_s", 60.0),
        sparse_progress=bool(channel_config.get("streaming.sparse_progress", True)),
    )


async def _stream_agent_turn_to_slack(
    client,
    channel_id: str,
    user_text: str,
    config: dict,
    *,
    thread_ts: str | None = None,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    loop = asyncio.get_event_loop()
    event_queue: queue.Queue = queue.Queue()
    stream_config = _slack_stream_config()
    transport = SlackStreamTransport(
        client,
        channel_id=channel_id,
        transport_mode=stream_config.transport_mode,
        thread_ts=thread_ts,
    )
    consumer = ChannelStreamConsumer(transport, stream_config)
    agent_future = loop.run_in_executor(None, _run_agent_sync, user_text, config, event_queue)
    consumer_task = asyncio.ensure_future(
        consumer.consume_queue(event_queue, final_text_source=agent_future)
    )
    try:
        answer, interrupt_data, captured_images, captured_video_paths = await agent_future
        delivery = await consumer_task
        ch_runtime.persist_channel_assistant_message(
            config,
            answer or delivery.final_text,
            channel_name="slack",
            delivery=delivery,
        )
        return answer, interrupt_data, captured_images, captured_video_paths, delivery
    except Exception:
        if not consumer_task.done():
            consumer_task.cancel()
        try:
            while True:
                event_queue.get_nowait()
        except Exception:
            pass
        raise


async def _stream_agent_resume_to_slack(
    client,
    channel_id: str,
    config: dict,
    approved: bool,
    *,
    interrupt_ids: list[str] | None = None,
    thread_ts: str | None = None,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    loop = asyncio.get_event_loop()
    event_queue: queue.Queue = queue.Queue()
    stream_config = _slack_stream_config()
    transport = SlackStreamTransport(
        client,
        channel_id=channel_id,
        transport_mode=stream_config.transport_mode,
        thread_ts=thread_ts,
    )
    consumer = ChannelStreamConsumer(transport, stream_config)
    agent_future = loop.run_in_executor(
        None,
        lambda: _resume_agent_sync(
            config,
            approved,
            interrupt_ids=interrupt_ids,
            event_queue=event_queue,
        ),
    )
    consumer_task = asyncio.ensure_future(
        consumer.consume_queue(event_queue, final_text_source=agent_future)
    )
    try:
        answer, interrupt_data, captured_images, captured_video_paths = await agent_future
        delivery = await consumer_task
        ch_runtime.persist_channel_assistant_message(
            config,
            answer or delivery.final_text,
            channel_name="slack",
            delivery=delivery,
        )
        return answer, interrupt_data, captured_images, captured_video_paths, delivery
    except Exception:
        if not consumer_task.done():
            consumer_task.cancel()
        try:
            while True:
                event_queue.get_nowait()
        except Exception:
            pass
        raise


async def _send_slack_safe_text(
    say,
    channel_id: str,
    message: str,
    *,
    thread_ts: str | None = None,
) -> None:
    for chunk in default_split_text(str(message or ""), SLACK_MAX_MSG_LEN):
        kwargs: dict[str, Any] = {"channel": channel_id}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await say(_md_to_mrkdwn(chunk), **kwargs)


async def _post_slack_safe_text(
    client,
    channel_id: str,
    message: str,
    *,
    thread_ts: str | None = None,
) -> None:
    for chunk in default_split_text(str(message or ""), SLACK_MAX_MSG_LEN):
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "text": _md_to_mrkdwn(chunk),
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await client.chat_postMessage(**kwargs)


def _slack_interrupt_context_key(channel_id: str, thread_ts: str | None = None) -> str:
    suffix = str(thread_ts or "")
    return f"{channel_id}:{suffix}" if suffix else str(channel_id)


def _slack_interrupt_blocks(interrupt_data: Any, resume_token: str = "") -> list[dict[str, Any]]:
    from row_bot.channels import approval as approval_helpers

    detail = _md_to_mrkdwn(approval_helpers.format_interrupt_text(interrupt_data))
    if len(detail) > 2800:
        detail = detail[:2797].rstrip() + "..."
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": detail},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "interrupt_approve",
                    "value": resume_token or "approve",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": "interrupt_deny",
                    "value": resume_token or "deny",
                },
            ],
        },
    ]


def _store_slack_interrupt(
    *,
    channel_id: str,
    interrupt_data: Any,
    config: dict,
    thread_ts: str | None = None,
    message_ts: str | None = None,
) -> str:
    import time as _time

    context_key = _slack_interrupt_context_key(channel_id, thread_ts)
    with _pending_lock:
        _pending_interrupts[context_key] = {
            "data": interrupt_data,
            "config": config,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "_ts": _time.time(),
        }
        _interrupt_by_channel[str(channel_id)] = context_key
        if message_ts:
            _interrupt_by_message[str(message_ts)] = context_key
    return context_key


def _pop_slack_interrupt(
    *,
    channel_id: str,
    thread_ts: str | None = None,
    message_ts: str | None = None,
) -> dict | None:
    with _pending_lock:
        context_key = None
        if message_ts:
            context_key = _interrupt_by_message.pop(str(message_ts), None)
        if context_key is None:
            exact_key = _slack_interrupt_context_key(channel_id, thread_ts)
            if exact_key in _pending_interrupts:
                context_key = exact_key
        if context_key is None:
            context_key = _interrupt_by_channel.get(str(channel_id))
        if context_key is None:
            return None
        pending = _pending_interrupts.pop(context_key, None)
        if pending:
            _interrupt_by_channel.pop(str(pending.get("channel_id") or channel_id), None)
            for msg_ts, mapped_key in list(_interrupt_by_message.items()):
                if mapped_key == context_key:
                    _interrupt_by_message.pop(msg_ts, None)
        return pending


def _peek_slack_interrupt(
    *,
    channel_id: str,
    thread_ts: str | None = None,
) -> dict | None:
    with _pending_lock:
        exact_key = _slack_interrupt_context_key(channel_id, thread_ts)
        pending = _pending_interrupts.get(exact_key)
        if pending is not None:
            return pending
        context_key = _interrupt_by_channel.get(str(channel_id))
        return _pending_interrupts.get(context_key) if context_key else None


async def _send_slack_interrupt_approval_card(
    client,
    say,
    *,
    channel_id: str,
    interrupt_data: Any,
    config: dict,
    thread_ts: str | None = None,
) -> str | None:
    from row_bot.channels import approval as approval_helpers

    detail = approval_helpers.format_interrupt_text(interrupt_data)
    try:
        resp = await client.chat_postMessage(
            channel=channel_id,
            text=_md_to_mrkdwn(detail),
            blocks=_slack_interrupt_blocks(interrupt_data),
            **({"thread_ts": thread_ts} if thread_ts else {}),
        )
        msg_ts = str(resp.get("ts") or "") if isinstance(resp, dict) else ""
        _store_slack_interrupt(
            channel_id=channel_id,
            interrupt_data=interrupt_data,
            config=config,
            thread_ts=thread_ts,
            message_ts=msg_ts or None,
        )
        return msg_ts or None
    except Exception:
        log.warning("Failed to send Slack interrupt approval card", exc_info=True)
        _store_slack_interrupt(
            channel_id=channel_id,
            interrupt_data=interrupt_data,
            config=config,
            thread_ts=thread_ts,
        )
        kwargs: dict[str, Any] = {"channel": channel_id}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        await say(_md_to_mrkdwn(detail) + "\n\nReply *yes* or *no*.", **kwargs)
        return None


def _slack_goal_callbacks(client, say, *, channel_id: str, thread_ts: str | None = None):
    state: dict[str, ChannelDeliveryResult | None] = {"delivery": None}

    async def _goal_run_turn(prompt: str, cfg: dict):
        answer, interrupt_data, captured_images, captured_video_paths, delivery = await _stream_agent_turn_to_slack(
            client,
            channel_id,
            prompt,
            cfg,
            thread_ts=thread_ts,
        )
        state["delivery"] = delivery
        for img_bytes in captured_images:
            try:
                import io
                await client.files_upload_v2(
                    channel=channel_id,
                    file=io.BytesIO(img_bytes),
                    filename="image.png",
                    title="🖼️ Image",
                )
            except Exception as exc:
                log.warning("Failed to send Slack image: %s", exc)
        for vpath in captured_video_paths:
            try:
                await client.files_upload_v2(
                    channel=channel_id,
                    file=vpath,
                    filename="video.mp4",
                    title="🎬 Video",
                )
            except Exception as exc:
                log.warning("Failed to send Slack video: %s", exc)
        return answer, interrupt_data

    async def _goal_send_text(message: str) -> None:
        delivery = state.get("delivery")
        if (
            delivery
            and delivery.delivered
            and str(delivery.final_text or "").strip() == str(message or "").strip()
        ):
            state["delivery"] = None
            return
        state["delivery"] = None
        await _send_slack_safe_text(say, channel_id, message, thread_ts=thread_ts)

    return _goal_run_turn, _goal_send_text


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
            await say("✅ Paired successfully! You can now chat with Row-Bot.",
                       channel=channel_id)
            return
        await say("🔒 Not authorised. Send your pairing code to get started.",
                   channel=channel_id)
        return

    from row_bot.channels.base import record_activity
    record_activity("slack")

    # Slash command dispatch
    _cmd_thread_id = (
        _get_or_create_thread(channel_id)
        if ch_commands.is_thread_scoped_command(text)
        else None
    )
    goal_start = ch_runtime.prepare_channel_goal_start(text, _cmd_thread_id)
    if goal_start is not None and _cmd_thread_id:
        config = {"configurable": {"thread_id": _cmd_thread_id}}
        thread_ts = event.get("thread_ts") or event.get("ts")
        _goal_run_turn, _goal_send_text = _slack_goal_callbacks(
            client,
            say,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )

        await _send_slack_safe_text(
            say,
            channel_id,
            ch_runtime.format_goal_started_ack(goal_start),
            thread_ts=thread_ts,
        )
        result = await ch_runtime.run_channel_goal_async(
            channel_name="slack",
            thread_id=_cmd_thread_id,
            config=config,
            first_prompt=goal_start.prompt,
            run_turn=_goal_run_turn,
            send_text=_goal_send_text,
        )
        if result.interrupt_data:
            await _send_slack_interrupt_approval_card(
                client,
                say,
                channel_id=channel_id,
                interrupt_data=result.interrupt_data,
                config=config,
                thread_ts=thread_ts,
            )
        return
    cmd_response = ch_commands.dispatch("slack", text, thread_id=_cmd_thread_id)
    if cmd_response is not None:
        await say(_md_to_mrkdwn(cmd_response), channel=channel_id)
        return

    # Handle /new specially — update thread
    if text.lower().startswith("/new"):
        _new_thread(channel_id)

    # Check pending task approvals first (background workflows)
    from row_bot.channels import approval as approval_helpers
    with _pending_lock:
        _ts_key = _approval_by_channel.get(channel_id)
        task_approval = _pending_task_approvals.get(_ts_key) if _ts_key else None
    if task_approval:
        decision = approval_helpers.is_approval_text(text)
        if decision is None:
            await say(
                "There's a pending approval - use the buttons, or reply yes/no.",
                channel=channel_id,
            )
            return
        if decision is not None:
            with _pending_lock:
                _pending_task_approvals.pop(_ts_key, None)
                _approval_by_channel.pop(channel_id, None)
            try:
                from row_bot.tasks import respond_to_approval
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
    incoming_thread_ts = event.get("thread_ts") or event.get("ts")
    interrupt = _peek_slack_interrupt(
        channel_id=channel_id,
        thread_ts=incoming_thread_ts,
    )

    if interrupt:
        decision = approval_helpers.is_approval_text(text)
        if decision is None:
            await say(
                "There's a pending approval - please use the approval buttons "
                "or reply yes/no first.",
                channel=channel_id,
                **({"thread_ts": interrupt.get("thread_ts")} if interrupt.get("thread_ts") else {}),
            )
            return
        interrupt = _pop_slack_interrupt(
            channel_id=channel_id,
            thread_ts=interrupt.get("thread_ts") or incoming_thread_ts,
        )
        if interrupt is None:
            await say("This approval is no longer pending.", channel=channel_id)
            return
        approved = bool(decision)
        config = interrupt.get("config", {})
        interrupt_ids = approval_helpers.extract_interrupt_ids(
            interrupt.get("data")
        )
        ch_runtime.resolve_goal_approval_for_config(config, approved)

        thread_ts = interrupt.get("thread_ts") or event.get("thread_ts") or event.get("ts")
        answer, new_interrupt, captured, captured_vids, delivery = await _stream_agent_resume_to_slack(
            client,
            channel_id,
            config,
            approved,
            interrupt_ids=interrupt_ids,
            thread_ts=thread_ts,
        )
        if answer and not delivery.delivered:
            await _send_slack_safe_text(say, channel_id, answer, thread_ts=thread_ts)
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
            await _send_slack_interrupt_approval_card(
                client,
                say,
                channel_id=channel_id,
                interrupt_data=new_interrupt,
                config=config,
                thread_ts=thread_ts,
            )
        elif answer and not ch_runtime.channel_turn_is_suspended(answer):
            thread_id = ch_runtime.thread_id_from_config(config)
            if thread_id:
                _goal_run_turn, _goal_send_text = _slack_goal_callbacks(
                    client,
                    say,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                )

                goal_result = await ch_runtime.continue_channel_goal_after_turn_async(
                    channel_name="slack",
                    thread_id=thread_id,
                    config=config,
                    assistant_text=answer,
                    interrupt_data=None,
                    run_turn=_goal_run_turn,
                    send_text=_goal_send_text,
                )
                if goal_result.interrupt_data:
                    await _send_slack_interrupt_approval_card(
                        client,
                        say,
                        channel_id=channel_id,
                        interrupt_data=goal_result.interrupt_data,
                        config=config,
                        thread_ts=thread_ts,
                    )
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

    try:
        answer, interrupt_data, captured_images, captured_video_paths, delivery = await _stream_agent_turn_to_slack(
            client,
            channel_id,
            text,
            config,
            thread_ts=event.get("thread_ts") or ts,
        )

    except Exception as exc:
        log.error("Agent error for channel %s: %s", channel_id, exc)

        from row_bot.channels.thread_repair import is_corrupt_thread_error
        if is_corrupt_thread_error(exc):
            try:
                from row_bot.agent import repair_orphaned_tool_calls
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, repair_orphaned_tool_calls, None, config
                )
                log.info("Repaired orphaned tool calls for %s, retrying", channel_id)
                answer, interrupt_data, captured_images, captured_video_paths = await loop.run_in_executor(
                    None, _run_agent_sync, text, config
                )
                delivery = ChannelDeliveryResult(
                    delivered=False,
                    streamed=False,
                    finalized=False,
                    fallback_sent=False,
                    transport="slack:repair",
                    final_text=answer,
                    error="repair retry used non-streaming fallback",
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
            if answer and not ch_runtime.channel_turn_is_suspended(answer):
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
                await _send_slack_interrupt_approval_card(
                    client,
                    say,
                    channel_id=channel_id,
                    interrupt_data=interrupt_data,
                    config=config,
                    thread_ts=event.get("thread_ts") or ts,
                )
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
    from row_bot.channels import extract_youtube_urls
    if ch_runtime.channel_turn_is_suspended(answer):
        return
    clean_answer, yt_urls = extract_youtube_urls(answer) if answer else ("", [])

    # If streaming/final delivery covered the response, just send images/URLs.
    if delivery.delivered and not interrupt_data:
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
        await _send_slack_interrupt_approval_card(
            client,
            say,
            channel_id=channel_id,
            interrupt_data=interrupt_data,
            config=config,
            thread_ts=event.get("thread_ts") or ts,
        )


async def _handle_approval_button(body: dict, client, *, approved: bool) -> None:
    """Handle a Block Kit approval button press (task_approve / task_deny)."""
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
        from row_bot.tasks import respond_to_approval
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


async def _handle_interrupt_button(body: dict, client, *, approved: bool) -> None:
    """Handle a Block Kit button press for a live/goal agent interrupt."""
    message = body.get("message", {}) or {}
    msg_ts = str(message.get("ts") or "")
    channel_id = str(
        (body.get("channel") or {}).get("id")
        or message.get("channel")
        or ""
    )
    thread_ts = str(message.get("thread_ts") or "") or None

    pending = _pop_slack_interrupt(
        channel_id=channel_id,
        thread_ts=thread_ts,
        message_ts=msg_ts,
    )
    if pending is None:
        try:
            await client.chat_update(
                channel=channel_id,
                ts=msg_ts,
                text="This approval is no longer pending.",
                blocks=[],
            )
        except Exception:
            pass
        return

    action = "Approved" if approved else "Denied"
    try:
        await client.chat_update(
            channel=channel_id,
            ts=msg_ts,
            text=f"{action} - processing...",
            blocks=[],
        )
    except Exception:
        log.debug("Failed to update Slack interrupt approval message", exc_info=True)

    async def _say(text: str, **kwargs: Any) -> None:
        await client.chat_postMessage(text=text, **kwargs)

    config = pending.get("config", {})
    interrupt_ids = None
    try:
        from row_bot.channels import approval as approval_helpers

        interrupt_ids = approval_helpers.extract_interrupt_ids(pending.get("data"))
        ch_runtime.resolve_goal_approval_for_config(config, approved)
        resume_thread_ts = str(pending.get("thread_ts") or thread_ts or "") or None
        (
            answer,
            new_interrupt,
            captured_images,
            captured_video_paths,
            delivery,
        ) = await _stream_agent_resume_to_slack(
            client,
            channel_id,
            config,
            approved,
            interrupt_ids=interrupt_ids,
            thread_ts=resume_thread_ts,
        )
    except Exception as exc:
        log.error("Slack interrupt resume error: %s", exc)
        await _post_slack_safe_text(
            client,
            channel_id,
            f"Error processing approval: {exc}",
            thread_ts=thread_ts,
        )
        return

    if answer and not delivery.delivered:
        await _post_slack_safe_text(client, channel_id, answer, thread_ts=thread_ts)

    for img_bytes in captured_images:
        try:
            import io

            await client.files_upload_v2(
                channel=channel_id,
                file=io.BytesIO(img_bytes),
                filename="image.png",
                title="Image",
            )
        except Exception as exc:
            log.warning("Failed to send Slack image: %s", exc)
    for vpath in captured_video_paths:
        try:
            await client.files_upload_v2(
                channel=channel_id,
                file=vpath,
                filename="video.mp4",
                title="Video",
            )
        except Exception as exc:
            log.warning("Failed to send Slack video: %s", exc)

    resume_thread_ts = str(pending.get("thread_ts") or thread_ts or "") or None
    if new_interrupt:
        await _send_slack_interrupt_approval_card(
            client,
            _say,
            channel_id=channel_id,
            interrupt_data=new_interrupt,
            config=config,
            thread_ts=resume_thread_ts,
        )
        return

    thread_id = ch_runtime.thread_id_from_config(config)
    if thread_id and answer:
        _goal_run_turn, _goal_send_text = _slack_goal_callbacks(
            client,
            _say,
            channel_id=channel_id,
            thread_ts=resume_thread_ts,
        )
        goal_result = await ch_runtime.continue_channel_goal_after_turn_async(
            channel_name="slack",
            thread_id=thread_id,
            config=config,
            assistant_text=answer,
            interrupt_data=None,
            run_turn=_goal_run_turn,
            send_text=_goal_send_text,
        )
        if goal_result.interrupt_data:
            await _send_slack_interrupt_approval_card(
                client,
                _say,
                channel_id=channel_id,
                interrupt_data=goal_result.interrupt_data,
                config=config,
                thread_ts=resume_thread_ts,
            )


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

    from row_bot.channels.media import (
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
            await client.api_call(
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
                if answer and not ch_runtime.channel_turn_is_suspended(answer):
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
                if answer and not ch_runtime.channel_turn_is_suspended(answer):
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
            if answer and not ch_runtime.channel_turn_is_suspended(answer):
                await say(_md_to_mrkdwn(answer), channel=channel_id)


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────
def _is_authorised(user_id: str) -> bool:
    """Check if a Slack user is authorised."""
    # Check config-based user ID
    allowed = get_channel_secret("slack", "SLACK_USER_ID").strip()
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
        log.info("Slack bot token or app token not configured")
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

        @_app.action("interrupt_approve")
        async def _on_interrupt_approve(ack, body, client):
            await ack()
            await _handle_interrupt_button(body, client, approved=True)

        @_app.action("interrupt_deny")
        async def _on_interrupt_deny(ack, body, client):
            await ack()
            await _handle_interrupt_button(body, client, approved=False)

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
        uid = get_channel_secret("slack", "SLACK_USER_ID").strip()
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
        approval_kind = str(config.get("approval_kind") or "task")
        subject_label = "Agent" if approval_kind == "agent_run" else "Task"
        task_name = config.get("task_name", "Unknown task")
        message = config.get("message", "Approval required to continue.")
        resume_token = config.get("resume_token", "")
        fallback_text = (
            f"⚠️ Approval Required\n"
            f"{subject_label}: {task_name}\n"
            f"{message}"
        )
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"⚠️ *Approval Required*\n\n"
                        f"*{subject_label}:* {task_name}\n"
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
    from row_bot.channels import registry as _ch_registry
    _ch_registry.register(_slack_channel)
except Exception:
    pass
