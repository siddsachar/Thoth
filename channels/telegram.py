"""
Thoth – Telegram Channel Adapter
==================================
Long-polling Telegram bot that bridges messages to the Thoth agent.

Setup:
    1. Message @BotFather on Telegram → /newbot → copy the **Bot Token**
    2. Message @userinfobot (or send /start to your bot, check logs)
       to get your **Telegram User ID**
    3. Enter both in Settings → Channels

Required keys (stored via api_keys):
    TELEGRAM_BOT_TOKEN   – Bot token from @BotFather
    TELEGRAM_USER_ID     – Your numeric Telegram user ID (access control)
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import re
import threading
import time
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReactionTypeEmoji
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import agent as agent_mod
from channels.base import Channel, ChannelCapabilities, ConfigField
from channels.auth_store import get_channel_secret
from threads import _save_thread_meta, _list_threads, _thread_exists
from tools import registry as tool_registry

log = logging.getLogger("thoth.telegram")

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
MAX_TG_MESSAGE_LEN = 4096  # Telegram text message character limit

# Bot commands registered with BotFather
BOT_COMMANDS = [
    BotCommand("help", "Show available commands"),
    BotCommand("newthread", "Start a new conversation"),
    BotCommand("model", "Switch model (cloud/local)"),
    BotCommand("tools", "List enabled tools"),
    BotCommand("status", "Check bot status"),
]

# ──────────────────────────────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────────────────────────────
_app: Application | None = None         # Telegram Application instance
_running = False                        # Whether the polling loop is active
_bot_loop: asyncio.AbstractEventLoop | None = None  # Loop the bot was started on
_pending_interrupts: dict[int, dict] = {}  # {chat_id: interrupt_data}
_pending_task_approvals: dict[int, dict] = {}  # {message_id: {resume_token, task_name}}
_pending_lock = threading.Lock()  # Guards both _pending_interrupts and _pending_task_approvals
_PENDING_TTL_SECONDS = 3600  # 1 hour — entries older than this are cleaned up


def _cleanup_stale_pending() -> None:
    """Remove entries older than _PENDING_TTL_SECONDS from pending dicts."""
    import time as _time
    now = _time.time()
    with _pending_lock:
        stale_int = [k for k, v in _pending_interrupts.items()
                     if now - v.get("_ts", now) > _PENDING_TTL_SECONDS]
        for k in stale_int:
            _pending_interrupts.pop(k, None)
        stale_task = [k for k, v in _pending_task_approvals.items()
                      if now - v.get("_ts", now) > _PENDING_TTL_SECONDS]
        for k in stale_task:
            _pending_task_approvals.pop(k, None)
    if stale_int or stale_task:
        log.info("Cleaned up %d stale interrupts, %d stale task approvals",
                 len(stale_int), len(stale_task))


# ──────────────────────────────────────────────────────────────────────
# Helpers — credentials
# ──────────────────────────────────────────────────────────────────────
def _get_bot_token() -> str:
    return get_channel_secret("telegram", "TELEGRAM_BOT_TOKEN")


def _get_allowed_user_id() -> int | None:
    """Return the authorised Telegram user ID, or None if not set."""
    raw = get_channel_secret("telegram", "TELEGRAM_USER_ID")
    if raw.strip().isdigit():
        return int(raw.strip())
    return None


def is_configured() -> bool:
    """Return True if bot token and user ID are both set."""
    return bool(_get_bot_token() and _get_allowed_user_id() is not None)


def is_running() -> bool:
    """Return True if the polling loop is currently active."""
    return _running


# ──────────────────────────────────────────────────────────────────────
# Access control
# ──────────────────────────────────────────────────────────────────────
def _is_authorised(update: Update) -> bool:
    """Check whether the message sender is the authorised user."""
    allowed = _get_allowed_user_id()
    if allowed is None:
        return False
    user_id = update.effective_user.id if update.effective_user else None
    return user_id == allowed


# ──────────────────────────────────────────────────────────────────────
# Thread management
# ──────────────────────────────────────────────────────────────────────
def _get_or_create_thread(chat_id: int) -> dict:
    """Get or create a LangGraph thread for a Telegram chat."""
    prefix = f"tg_{chat_id}"

    existing = _list_threads()
    for tid, name, _, _, *rest in existing:
        if tid == prefix or tid.startswith(prefix + "_"):
            _save_thread_meta(tid, name)  # bump updated_at
            mo = rest[0] if rest else ""
            cfg = {"configurable": {"thread_id": tid}}
            if mo:
                cfg["configurable"]["model_override"] = mo
            return cfg

    import uuid
    suffix = uuid.uuid4().hex[:6]
    thread_id = f"tg_{chat_id}_{suffix}"
    name = f"✈️ Telegram – {chat_id}"
    _save_thread_meta(thread_id, name)
    return {"configurable": {"thread_id": thread_id}}


def _new_thread(chat_id: int) -> dict:
    """Force-create a brand-new thread for the given chat."""
    import uuid
    suffix = uuid.uuid4().hex[:6]
    thread_id = f"tg_{chat_id}_{suffix}"
    name = f"✈️ Telegram – {chat_id} ({suffix})"
    _save_thread_meta(thread_id, name)
    return {"configurable": {"thread_id": thread_id}}


# ──────────────────────────────────────────────────────────────────────
# Agent invocation (synchronous — runs in executor)
# ──────────────────────────────────────────────────────────────────────
from channels.media_capture import grab_vision_capture as _grab_vision_capture
from channels.media_capture import grab_generated_image as _grab_generated_image
from channels.media_capture import grab_generated_video as _grab_generated_video


def _run_agent_sync(user_text: str, config: dict,
                    event_queue=None) -> tuple[str, dict | None, list[bytes], list[str]]:
    """Run the agent synchronously, collecting the full response.

    Returns (answer_text, interrupt_data_or_None, captured_images, captured_video_paths).
    If *event_queue* is provided, also pushes (event_type, payload) tuples
    for live streaming.  Pushes ``None`` sentinel when done.
    """
    # Ensure recursion limit matches the web UI (default is too low)
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
        img = _grab_vision_capture()
        if img:
            captured_images.append(img)
    if used_image_gen:
        img = _grab_generated_image()
        if img:
            captured_images.append(img)
    if used_video_gen:
        vid_path = _grab_generated_video()
        if vid_path:
            captured_video_paths.append(vid_path)
    return answer or "_(No response)_", interrupt_data, captured_images, captured_video_paths


def _resume_agent_sync(config: dict, approved: bool,
                       *, interrupt_ids: list[str] | None = None) -> tuple[str, dict | None, list[bytes], list[str]]:
    """Resume a paused agent after interrupt approval/denial."""
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
        img = _grab_vision_capture()
        if img:
            captured_images.append(img)
    if used_image_gen:
        img = _grab_generated_image()
        if img:
            captured_images.append(img)
    if used_video_gen:
        vid_path = _grab_generated_video()
        if vid_path:
            captured_video_paths.append(vid_path)
    return answer or "_(No response)_", interrupt_data, captured_images, captured_video_paths


# ──────────────────────────────────────────────────────────────────────
# Message splitting
# ──────────────────────────────────────────────────────────────────────
def _split_message(text: str, max_len: int = MAX_TG_MESSAGE_LEN) -> list[str]:
    """Split long text at paragraph or line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        break_at = max_len
        para = remaining.rfind("\n\n", 0, max_len)
        if para > max_len // 2:
            break_at = para + 2
        else:
            line = remaining.rfind("\n", 0, max_len)
            if line > max_len // 2:
                break_at = line + 1
            else:
                space = remaining.rfind(" ", 0, max_len)
                if space > max_len // 2:
                    break_at = space + 1

        chunks.append(remaining[:break_at])
        remaining = remaining[break_at:]

    return chunks


# ──────────────────────────────────────────────────────────────────────
# Streaming: rate-limited edit consumer
# ──────────────────────────────────────────────────────────────────────
_STREAM_EDIT_INTERVAL = 1.5  # seconds between message edits


async def _tg_edit_consumer(chat, sent_msg, event_queue: queue.Queue,
                            loop) -> str | None:
    """Read events from *event_queue* and edit *sent_msg* progressively.

    Returns the final assembled display text (or None if nothing came).
    """
    accumulated = ""
    tool_lines: list[str] = []
    last_edit = 0.0
    overflow = False  # set when text exceeds MAX_TG_MESSAGE_LEN
    last_sent_display = ""

    while True:
        try:
            event = await loop.run_in_executor(None, lambda: event_queue.get(timeout=0.2))
        except Exception:
            # queue.Empty — just loop and try again
            continue
        if event is None:
            break  # sentinel

        event_type, payload = event
        if event_type == "token":
            accumulated += payload
        elif event_type == "tool_call":
            tool_lines.append(f"🔧 Using {payload}…")
        elif event_type == "tool_done":
            name = payload['name'] if isinstance(payload, dict) else payload
            tool_lines.append(f"✅ {name} done")

        # Rate-limited edit
        now = time.monotonic()
        if now - last_edit >= _STREAM_EDIT_INTERVAL and not overflow:
            display = _build_stream_display(tool_lines, accumulated)
            if len(display) > MAX_TG_MESSAGE_LEN:
                overflow = True
                continue
            try:
                html = _md_to_html(display) if display else "⏳"
                await sent_msg.edit_text(html, parse_mode="HTML")
                last_sent_display = display
            except Exception:
                try:
                    await sent_msg.edit_text(display or "⏳")
                    last_sent_display = display
                except Exception:
                    pass
            last_edit = now

    # Final edit
    display = _build_stream_display(tool_lines, accumulated)
    if display and not overflow:
        try:
            html = _md_to_html(display)
            await sent_msg.edit_text(html, parse_mode="HTML")
            last_sent_display = display
        except Exception:
            try:
                await sent_msg.edit_text(display)
                last_sent_display = display
            except Exception:
                pass
    return display if not overflow and last_sent_display == display else None


def _build_stream_display(tool_lines: list[str], accumulated: str) -> str:
    """Assemble tool status lines + accumulated tokens into display text."""
    parts = []
    if tool_lines:
        parts.append("\n".join(tool_lines))
    if accumulated:
        parts.append(accumulated)
    return ("\n\n".join(parts)) if parts else ""


# ──────────────────────────────────────────────────────────────────────
# Markdown → Telegram HTML converter
# ──────────────────────────────────────────────────────────────────────
def _md_to_html(text: str) -> str:
    """Convert common markdown to Telegram-compatible HTML.

    Handles: **bold**, *italic*, `code`, ```code blocks```,
    # headings → bold, and escapes <>&.
    """
    # Escape HTML entities first
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Fenced code blocks (``` ... ```)
    text = re.sub(r"```(?:\w*\n)?([\s\S]*?)```", r"<pre>\1</pre>", text)
    # Inline code (`...`)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold (**...**)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # Italic (*...*) — but not inside <b> tags
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", text)
    # Headings (# ... at start of line) → bold
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    return text


def _escape_html(text: str) -> str:
    """Escape only the characters required by Telegram HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ──────────────────────────────────────────────────────────────────────
# Interrupt formatting
# ──────────────────────────────────────────────────────────────────────
def _format_interrupt(data) -> str:
    """Format interrupt data (single dict or list of dicts) as HTML."""
    items = data if isinstance(data, list) else [data]
    parts: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            parts.append(_escape_html(str(item)))
            continue
        tool_name = item.get("tool", item.get("name", "Unknown tool"))
        desc = item.get("description", "")
        args = item.get("args", {})

        parts.append(f"⚠️ <b>{_escape_html(tool_name)}</b> needs your approval:")
        if desc:
            parts.append(f"<i>{_escape_html(desc)}</i>")
        elif args:
            for k, v in args.items():
                parts.append(f"• <b>{_escape_html(str(k))}</b>: {_escape_html(str(v))}")

    return "\n".join(parts)


def _extract_interrupt_ids(data) -> list[str] | None:
    """Extract __interrupt_id values from interrupt data for multi-interrupt resume."""
    items = data if isinstance(data, list) else [data]
    ids = [item.get("__interrupt_id") for item in items
           if isinstance(item, dict) and item.get("__interrupt_id")]
    return ids if len(ids) > 1 else None


# ──────────────────────────────────────────────────────────────────────
# Telegram handlers
# ──────────────────────────────────────────────────────────────────────
async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    if not _is_authorised(update):
        await update.message.reply_text(
            f"⛔ Unauthorised. Your user ID is {update.effective_user.id}. "
            "Add this ID in Thoth → Settings → Channels to authorise."
        )
        return
    await _send_html(
        update.message,
        "𓁟 <b>Thoth</b> is connected!\n\n"
        "Send me any message and I'll respond using your configured agent.\n\n"
        "Commands:\n"
        "/newthread — Start a fresh conversation\n"
        "/model — Switch model (cloud/local)\n"
        "/tools — List enabled tools\n"
        "/status — Check connection status\n"
        "/help — Show this message",
    )


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    if not _is_authorised(update):
        return
    await _cmd_start(update, context)


async def _cmd_newthread(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /newthread — start a fresh conversation."""
    if not _is_authorised(update):
        return
    chat_id = update.effective_chat.id
    config = _new_thread(chat_id)
    # Store the new thread config so subsequent messages use it
    context.chat_data["thread_config"] = config
    # Clear any pending interrupts
    with _pending_lock:
        _pending_interrupts.pop(chat_id, None)
    await update.message.reply_text("🆕 Started a new conversation thread.")


async def _cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model — switch the model for this thread."""
    if not _is_authorised(update):
        return

    from models import (
        is_cloud_model, is_cloud_available,
        get_current_model, get_cloud_provider, get_provider_emoji,
    )
    from providers.selection import list_quick_model_ids, resolve_selection
    from threads import _set_thread_model_override

    args = (update.message.text or "").split(maxsplit=1)
    if len(args) < 2:
        # Show current model + channel-visible Quick Choices
        config = context.chat_data.get("thread_config") or {}
        current_ov = (config.get("configurable") or {}).get("model_override", "")
        _def = get_current_model()
        if current_ov:
            _prov = get_cloud_provider(current_ov)
            _tag = "☁️" if _prov else "🖥️"
            current_display = f"{_tag} {current_ov}"
        else:
            _prov = get_cloud_provider(_def)
            _tag = "☁️" if _prov else "🖥️"
            current_display = f"{_tag} {_def} (default)"
        lines = [f"<b>Current model:</b> {_escape_html(current_display)}\n"]
        choices = list(dict.fromkeys([get_current_model()] + list_quick_model_ids("channels")))
        if choices:
            lines.append("<b>Channel Quick Choices:</b>")
            for cid in choices:
                lines.append(f"• {_escape_html(get_provider_emoji(cid))} <code>{cid}</code>")
            lines.append("\nUsage: <code>/model gpt-4o</code>")
            lines.append("Reset to default: <code>/model default</code>")
        elif is_cloud_available():
            lines.append("No provider Quick Choices. Pin models in Thoth → Settings → Providers.")
        else:
            lines.append("No provider API keys configured.\nSet them in Thoth → Settings → Providers.")
        await _send_html(update.message, "\n".join(lines))
        return

    model_id = args[1].strip()

    if model_id.lower() == "default":
        # Reset to global default
        config = context.chat_data.get("thread_config")
        if config:
            tid = (config.get("configurable") or {}).get("thread_id", "")
            config["configurable"]["model_override"] = ""
            _set_thread_model_override(tid, "")
            context.chat_data["thread_config"] = config
        await update.message.reply_text(f"✅ Switched to default: {get_current_model()}")
        return

    resolved = resolve_selection(model_id)
    if not resolved or resolved.kind != "model" or not resolved.model_id:
        await update.message.reply_text(
            f"Unknown model choice: {model_id}\n"
            "Use /model to see available Quick Choices."
        )
        return

    model_id = resolved.model_id
    channel_choices = set(list_quick_model_ids("channels")) | {get_current_model()}
    if model_id not in channel_choices and not resolved.legacy_value.startswith("model:"):
        await update.message.reply_text(
            f"{model_id} is not a channel Quick Choice. Pin it in Settings → Providers first."
        )
        return

    if is_cloud_model(model_id) and not is_cloud_available():
        await update.message.reply_text("No provider API keys configured.")
        return

    # Set the model override
    config = context.chat_data.get("thread_config")
    if config is None:
        chat_id = update.effective_chat.id
        config = _get_or_create_thread(chat_id)
    tid = (config.get("configurable") or {}).get("thread_id", "")
    config["configurable"]["model_override"] = model_id
    _set_thread_model_override(tid, model_id)
    context.chat_data["thread_config"] = config

    await update.message.reply_text(f"Switched to {model_id}")


async def _cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tools — list enabled tools."""
    if not _is_authorised(update):
        return
    enabled = tool_registry.get_enabled_tools()
    if not enabled:
        await update.message.reply_text("No tools are currently enabled.")
        return
    lines = ["🔧 <b>Enabled tools:</b>\n"]
    for t in enabled:
        lines.append(f"• {_escape_html(t.name)}")
    await _send_html(update.message, "\n".join(lines))


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status — show connection status."""
    if not _is_authorised(update):
        return
    enabled_count = len(tool_registry.get_enabled_tools())
    await update.message.reply_text(
        f"✅ Thoth Telegram bot is running.\n"
        f"🔧 {enabled_count} tools enabled.\n"
        f"👤 Authorised user: {_get_allowed_user_id()}"
    )


async def _send_html(target, text: str, **kwargs) -> None:
    """Send a message as HTML, falling back to plain text on parse errors."""
    try:
        await target.reply_text(text, parse_mode="HTML", **kwargs)
    except Exception:
        # Strip HTML tags and send as plain text
        plain = re.sub(r"<[^>]+>", "", text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        await target.reply_text(plain, **kwargs)


async def _send_html_msg(chat, text: str, **kwargs) -> None:
    """Send a message via chat.send_message as HTML with plain-text fallback."""
    try:
        await chat.send_message(text, parse_mode="HTML", **kwargs)
    except Exception:
        plain = re.sub(r"<[^>]+>", "", text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        await chat.send_message(plain, **kwargs)


from channels.thread_repair import is_corrupt_thread_error as _is_corrupt_thread_error


# ──────────────────────────────────────────────────────────────────────
# Reactions & shared response helpers
# ──────────────────────────────────────────────────────────────────────

async def _react(message, emoji: str) -> None:
    """Set an emoji reaction on *message*.  Fails silently."""
    try:
        await message.set_reaction(ReactionTypeEmoji(emoji))
    except Exception:
        log.debug("Could not set reaction %s: bot may lack permission", emoji)


async def _send_agent_response(
    chat, message, chat_id: int, config: dict,
    answer: str, interrupt_data: dict | None,
    captured_images: list[bytes],
    captured_video_paths: list[str] | None = None,
) -> None:
    """Send agent output back to the user (shared by all handlers)."""
    # Send captured images (vision captures + generated images)
    for img_bytes in captured_images:
        try:
            import io
            await chat.send_photo(
                photo=io.BytesIO(img_bytes), caption="🖼️ Image"
            )
        except Exception as exc:
            log.warning("Failed to send image to Telegram: %s", exc)

    # Send captured videos
    for vpath in (captured_video_paths or []):
        try:
            await chat.send_document(
                document=open(vpath, "rb"), caption="🎬 Video",
                filename=os.path.basename(vpath),
            )
        except Exception as exc:
            log.warning("Failed to send video to Telegram: %s", exc)

    if interrupt_data:
        import time as _time
        with _pending_lock:
            _pending_interrupts[chat_id] = {
                "data": interrupt_data,
                "config": config,
                "_ts": _time.time(),
            }
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data="interrupt_approve"),
                InlineKeyboardButton("❌ Deny", callback_data="interrupt_deny"),
            ]
        ])
        await _send_html_msg(chat, _format_interrupt(interrupt_data),
                             reply_markup=keyboard)
    else:
        html = _md_to_html(answer)
        for chunk in _split_message(html):
            await _send_html_msg(chat, chunk)


async def _run_agent_for_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    user_text: str,
) -> None:
    """Shared flow: auth → interrupt check → agent → response.

    Used by text, voice, photo and document handlers.
    """
    chat_id = update.effective_chat.id
    msg = update.message

    # Block while interrupt is pending
    if chat_id in _pending_interrupts:  # race-free: dict 'in' is atomic under GIL
        await msg.reply_text(
            "⏸️ There's a pending approval — please tap ✅ Approve or ❌ Deny first."
        )
        return

    # Get or create thread config (validate cache against DB)
    config = context.chat_data.get("thread_config")
    if config is not None:
        cached_tid = (config.get("configurable") or {}).get("thread_id", "")
        if not cached_tid or not _thread_exists(cached_tid):
            config = None
            context.chat_data.pop("thread_config", None)
    if config is None:
        config = _get_or_create_thread(chat_id)
        context.chat_data["thread_config"] = config

    # React + typing
    await _react(msg, "👀")
    await update.effective_chat.send_action("typing")

    loop = asyncio.get_event_loop()

    # Send placeholder and start streaming
    try:
        sent_msg = await update.effective_chat.send_message("⏳")
    except Exception:
        sent_msg = None

    eq = queue.Queue()

    try:
        executor_future = loop.run_in_executor(
            None, _run_agent_sync, user_text, config, eq
        )
        # Run streaming consumer in parallel
        if sent_msg:
            consumer_task = asyncio.ensure_future(
                _tg_edit_consumer(update.effective_chat, sent_msg, eq, loop)
            )
        answer, interrupt_data, captured_images, captured_video_paths = await executor_future
        if sent_msg:
            streamed_display = await consumer_task
        else:
            streamed_display = None
    except Exception as exc:
        # Drain the queue to prevent leaks
        try:
            while True:
                eq.get_nowait()
        except Exception:
            pass
        log.error("Agent error for chat %s: %s", chat_id, exc)
        if _is_corrupt_thread_error(exc):
            try:
                from agent import repair_orphaned_tool_calls
                await loop.run_in_executor(
                    None, repair_orphaned_tool_calls, None, config
                )
                log.info("Repaired orphaned tool calls for chat %s, retrying", chat_id)
                answer, interrupt_data, captured_images, captured_video_paths = await loop.run_in_executor(
                    None, _run_agent_sync, user_text, config
                )
                streamed_display = None
            except Exception as retry_exc:
                log.error("Retry after repair failed: %s", retry_exc)
                config = _new_thread(chat_id)
                context.chat_data["thread_config"] = config
                await _react(msg, "💔")
                if sent_msg:
                    try:
                        await sent_msg.edit_text(
                            "⚠️ The previous conversation had a stuck tool call "
                            "and couldn't be repaired.\n"
                            "🆕 I've started a fresh thread — please resend your message."
                        )
                    except Exception:
                        pass
                else:
                    await msg.reply_text(
                        "⚠️ The previous conversation had a stuck tool call "
                        "and couldn't be repaired.\n"
                        "🆕 I've started a fresh thread — please resend your message."
                    )
                return
        else:
            await _react(msg, "💔")
            if sent_msg:
                try:
                    await sent_msg.edit_text(f"⚠️ Error: {exc}")
                except Exception:
                    pass
            else:
                await msg.reply_text(f"⚠️ Error: {exc}")
            return

    await _react(msg, "👍")

    # Extract YouTube URLs for separate messages (auto-preview)
    from channels import extract_youtube_urls
    clean_answer, yt_urls = extract_youtube_urls(answer) if answer else ("", [])

    # If streaming covered the full response, skip re-sending the text
    if streamed_display and not interrupt_data:
        # Send YouTube URLs as separate messages for auto-preview
        for url in yt_urls:
            await update.effective_chat.send_message(url)
        # Still need to send images
        for img_bytes in captured_images:
            try:
                import io
                await update.effective_chat.send_photo(
                    photo=io.BytesIO(img_bytes), caption="🖼️ Image"
                )
            except Exception as exc_img:
                log.warning("Failed to send image to Telegram: %s", exc_img)
        # Send captured videos
        for vpath in captured_video_paths:
            try:
                await update.effective_chat.send_document(
                    document=open(vpath, "rb"), caption="🎬 Video",
                    filename=os.path.basename(vpath),
                )
            except Exception as exc_vid:
                log.warning("Failed to send video to Telegram: %s", exc_vid)
    else:
        # Streaming overflowed or wasn't available — delete placeholder, send normally
        if sent_msg:
            try:
                await sent_msg.delete()
            except Exception:
                pass
        await _send_agent_response(
            update.effective_chat, msg, chat_id, config,
            clean_answer, interrupt_data, captured_images,
            captured_video_paths,
        )
        for url in yt_urls:
            await update.effective_chat.send_message(url)


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages — route to agent."""
    if not _is_authorised(update):
        await update.message.reply_text(
            f"⛔ Unauthorised. Your user ID is {update.effective_user.id}."
        )
        return

    from channels.base import record_activity
    record_activity("telegram")

    text = (update.message.text or "").strip()
    if not text:
        return

    await _run_agent_for_message(update, context, text)


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses (interrupt approve/deny + task approvals)."""
    query = update.callback_query
    await query.answer()  # Acknowledge the button press

    if not _is_authorised(update):
        return

    # ── Task approval buttons (task_approve:<token> / task_deny:<token>) ──
    cb_data = query.data or ""
    if cb_data.startswith("task_approve:") or cb_data.startswith("task_deny:"):
        token_prefix = cb_data.split(":", 1)[1]
        approved = cb_data.startswith("task_approve:")
        # Find full resume token from pending map
        msg_id = query.message.message_id
        with _pending_lock:
            info = _pending_task_approvals.pop(msg_id, None)
        if info is None:
            await query.edit_message_text("ℹ️ This approval is no longer pending.")
            return
        action = "Approved ✅" if approved else "Denied ❌"
        await query.edit_message_text(
            f"{action} — {_escape_html(info['task_name'])}"
        )
        # Respond to approval in a thread pool — respond_to_approval
        # calls _resolve_approval_on_channels which may block on
        # other channels' send_outbound (e.g. Slack's fut.result()).
        try:
            from tasks import respond_to_approval
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: respond_to_approval(
                    info["resume_token"], approved,
                    note="Approved via Telegram" if approved else "Denied via Telegram",
                    source="telegram",
                ),
            )
            if not result:
                await update.effective_chat.send_message(
                    "⚠️ Could not process approval — request may have expired."
                )
        except Exception as exc:
            log.error("Task approval error: %s", exc)
            await update.effective_chat.send_message(f"⚠️ Error: {exc}")
        return

    # ── Live chat interrupt buttons (interrupt_approve / interrupt_deny) ──
    chat_id = update.effective_chat.id
    with _pending_lock:
        pending = _pending_interrupts.pop(chat_id, None)
    if pending is None:
        await query.edit_message_text("ℹ️ No pending approval to respond to.")
        return

    approved = query.data == "interrupt_approve"
    action = "Approved ✅" if approved else "Denied ❌"
    await query.edit_message_text(f"{action} — processing…")

    config = pending["config"]
    interrupt_ids = _extract_interrupt_ids(pending["data"])

    # Send typing indicator
    await update.effective_chat.send_action("typing")

    loop = asyncio.get_event_loop()
    try:
        answer, new_interrupt, captured_images, captured_video_paths = await loop.run_in_executor(
            None, lambda: _resume_agent_sync(config, approved, interrupt_ids=interrupt_ids),
        )
    except Exception as exc:
        log.error("Agent resume error: %s", exc)
        if _is_corrupt_thread_error(exc):
            new_config = _new_thread(chat_id)
            context.chat_data["thread_config"] = new_config
            await update.effective_chat.send_message(
                "⚠️ The conversation had a stuck tool call.\n"
                "🆕 Started a fresh thread — please resend your message."
            )
        else:
            await update.effective_chat.send_message(f"⚠️ Error: {exc}")
        return

    # Send captured images (vision + generated)
    for img_bytes in captured_images:
        try:
            import io
            await update.effective_chat.send_photo(
                photo=io.BytesIO(img_bytes), caption="🖼️ Image"
            )
        except Exception as exc:
            log.warning("Failed to send image to Telegram: %s", exc)

    # Send captured videos
    for vpath in captured_video_paths:
        try:
            await update.effective_chat.send_document(
                document=open(vpath, "rb"), caption="🎬 Video",
                filename=os.path.basename(vpath),
            )
        except Exception as exc:
            log.warning("Failed to send video to Telegram: %s", exc)

    if new_interrupt:
        import time as _time
        with _pending_lock:
            _pending_interrupts[chat_id] = {
                "data": new_interrupt,
                "config": config,
                "_ts": _time.time(),
            }
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data="interrupt_approve"),
                InlineKeyboardButton("❌ Deny", callback_data="interrupt_deny"),
            ]
        ])
        await _send_html_msg(
            update.effective_chat,
            _format_interrupt(new_interrupt),
            reply_markup=keyboard,
        )
    else:
        html = _md_to_html(answer)
        for chunk in _split_message(html):
            await _send_html_msg(update.effective_chat, chunk)


# ──────────────────────────────────────────────────────────────────────
# Inbound media handlers (voice, photo, document)
# ──────────────────────────────────────────────────────────────────────

async def _handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming voice notes / audio — transcribe and route to agent."""
    if not _is_authorised(update):
        await update.message.reply_text(
            f"⛔ Unauthorised. Your user ID is {update.effective_user.id}."
        )
        return

    voice = update.message.voice or update.message.audio
    if voice is None:
        return

    await _react(update.message, "🎤")

    try:
        tg_file = await voice.get_file()
        data = await tg_file.download_as_bytearray()
    except Exception as exc:
        log.error("Failed to download voice file: %s", exc)
        await update.message.reply_text("⚠️ Could not download voice note.")
        return

    # Determine file extension
    mime = getattr(voice, "mime_type", "") or ""
    if "ogg" in mime:
        ext = ".ogg"
    elif "mp3" in mime or "mpeg" in mime:
        ext = ".mp3"
    elif "webm" in mime:
        ext = ".webm"
    elif "wav" in mime:
        ext = ".wav"
    else:
        ext = ".ogg"  # Telegram default

    from channels.media import transcribe_audio
    text = transcribe_audio(bytes(data), file_ext=ext)
    if not text:
        await update.message.reply_text("⚠️ Could not transcribe your voice note.")
        return

    await update.message.reply_text(f"🎤 _{text}_", parse_mode="Markdown")
    await _run_agent_for_message(update, context, text)


async def _handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming photos — analyse via vision and route to agent."""
    if not _is_authorised(update):
        await update.message.reply_text(
            f"⛔ Unauthorised. Your user ID is {update.effective_user.id}."
        )
        return

    photos = update.message.photo
    if not photos:
        return

    # Take highest resolution
    photo = photos[-1]

    # Resolve the same thread config that _run_agent_for_message will use so
    # the attachment cache stays scoped correctly per Telegram conversation.
    chat_id = update.effective_chat.id
    config = context.chat_data.get("thread_config")
    if config is not None:
        cached_tid = (config.get("configurable") or {}).get("thread_id", "")
        if not cached_tid or not _thread_exists(cached_tid):
            config = None
            context.chat_data.pop("thread_config", None)
    if config is None:
        config = _get_or_create_thread(chat_id)
        context.chat_data["thread_config"] = config
    thread_id = (config.get("configurable") or {}).get("thread_id", "")

    try:
        tg_file = await photo.get_file()
        data = await tg_file.download_as_bytearray()
    except Exception as exc:
        log.error("Failed to download photo: %s", exc)
        await update.message.reply_text("⚠️ Could not download the photo.")
        return

    caption = (update.message.caption or "").strip()
    question = caption if caption else "Describe this image in detail."

    cache_name = f"telegram_photo_{getattr(photo, 'file_unique_id', '') or 'image'}.jpg"
    try:
        import tools.image_gen_tool as _igt_mod

        same_thread = (_igt_mod._image_cache_thread_id == thread_id)
        if not same_thread:
            _igt_mod._image_cache.clear()
        _igt_mod._image_cache_thread_id = thread_id
        _igt_mod._image_cache[cache_name] = bytes(data)
    except Exception:
        log.debug("Failed to cache inbound Telegram photo '%s'", cache_name, exc_info=True)

    from channels.media import analyze_image
    description = analyze_image(bytes(data), question=question)

    if not description:
        await update.message.reply_text("⚠️ Could not analyse the photo.")
        return

    # Build context for the agent
    if caption:
        agent_text = (
            f"[Attached image: {cache_name} — ALREADY ANALYZED, do NOT call analyze_image]\n"
            f"{description}\n\n"
            f"User said: {caption}"
        )
    else:
        agent_text = (
            f"[Attached image: {cache_name} — ALREADY ANALYZED, do NOT call analyze_image]\n"
            f"{description}"
        )

    await _run_agent_for_message(update, context, agent_text)


async def _handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming documents/files — save to inbox, extract text, and route to agent."""
    if not _is_authorised(update):
        await update.message.reply_text(
            f"⛔ Unauthorised. Your user ID is {update.effective_user.id}."
        )
        return

    doc = update.message.document
    if doc is None:
        return

    filename = doc.file_name or "unknown_file"

    try:
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
    except Exception as exc:
        log.error("Failed to download document: %s", exc)
        await update.message.reply_text("⚠️ Could not download the document.")
        return

    raw_bytes = bytes(data)

    from channels.media import save_inbound_file, extract_document_text, copy_to_workspace
    saved_path = save_inbound_file(raw_bytes, filename)

    # Extract text content so the agent can work with it directly
    extracted = extract_document_text(raw_bytes, filename)

    # Copy into workspace so agent can re-read via filesystem tool
    ws_rel = copy_to_workspace(saved_path)

    caption = (update.message.caption or "").strip()

    # Build rich context for the agent
    parts = [f"[User sent a file: {filename}]"]
    if ws_rel:
        parts.append(f"Workspace path: {ws_rel}")
    else:
        parts.append(f"Saved to: {saved_path}")
    if extracted:
        parts.append(f"\n--- File content ---\n{extracted}\n--- End of file ---")
    else:
        parts.append("(Could not extract text — file may be image-based or unsupported format)")
    if caption:
        parts.append(f"\nUser's message: {caption}")

    await _run_agent_for_message(update, context, "\n".join(parts))


# ──────────────────────────────────────────────────────────────────────
# Lifecycle: start / stop the polling bot
# ──────────────────────────────────────────────────────────────────────
async def start_bot() -> bool:
    """Initialise and start the Telegram bot (long polling).

    Returns True on success, False if not configured or already running.
    """
    global _app, _running, _bot_loop

    if _running:
        log.info("Telegram bot is already running")
        return True

    if not is_configured():
        log.warning("Telegram bot not configured — skipping start")
        return False

    token = _get_bot_token()

    _app = Application.builder().token(token).build()

    # Register handlers
    _app.add_handler(CommandHandler("start", _cmd_start))
    _app.add_handler(CommandHandler("help", _cmd_help))
    _app.add_handler(CommandHandler("newthread", _cmd_newthread))
    _app.add_handler(CommandHandler("model", _cmd_model))
    _app.add_handler(CommandHandler("tools", _cmd_tools))
    _app.add_handler(CommandHandler("status", _cmd_status))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    _app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _handle_voice))
    _app.add_handler(MessageHandler(filters.PHOTO, _handle_photo))
    _app.add_handler(MessageHandler(filters.Document.ALL, _handle_document))
    _app.add_handler(CallbackQueryHandler(_handle_callback))

    # Register commands with BotFather for autocomplete
    try:
        await _app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:
        log.warning("Could not register bot commands: %s", exc)

    # Initialise and start polling
    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(drop_pending_updates=True)

    _bot_loop = asyncio.get_running_loop()
    _running = True

    # Periodic cleanup of stale pending dicts (every 10 minutes)
    async def _periodic_cleanup():
        while _running:
            await asyncio.sleep(600)
            try:
                _cleanup_stale_pending()
            except Exception as exc:
                log.warning("Pending cleanup error: %s", exc)
    asyncio.ensure_future(_periodic_cleanup())

    log.info("Telegram bot started (polling)")
    return True


async def stop_bot() -> None:
    """Stop the Telegram bot gracefully."""
    global _app, _running, _bot_loop

    if not _running or _app is None:
        return

    try:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
    except Exception as exc:
        log.warning("Error stopping Telegram bot: %s", exc)
    finally:
        _running = False
        _app = None
        _bot_loop = None
        log.info("Telegram bot stopped")


# ──────────────────────────────────────────────────────────────────────
# Outbound messages (called by the task engine)
# ──────────────────────────────────────────────────────────────────────
def send_outbound(chat_id: int, text: str) -> None:
    """Send a message to a Telegram chat from *outside* the handler context.

    Called synchronously by ``tasks._deliver_to_channel()``.
    The bot's httpx HTTP client is bound to the loop it was created on,
    so we schedule the send on that same loop via *run_coroutine_threadsafe*.
    """
    if not _running or _app is None:
        raise RuntimeError("Telegram bot is not running — cannot deliver message")
    if _bot_loop is None or not _bot_loop.is_running():
        raise RuntimeError("Telegram bot event loop is not available")

    async def _send():
        html = _md_to_html(text)
        for chunk in _split_message(html):
            try:
                await _app.bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
            except Exception:
                plain = re.sub(r"<[^>]+>", "", chunk).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                await _app.bot.send_message(chat_id=chat_id, text=plain)

    future = asyncio.run_coroutine_threadsafe(_send(), _bot_loop)
    future.result(timeout=30)


def send_task_approval(chat_id: int, resume_token: str,
                       task_name: str, message: str) -> str | None:
    """Send a task approval request with inline ✅/❌ buttons.

    Called synchronously from ``tasks.py`` after pausing a pipeline.
    Returns the message_id as a string (for cross-channel resolution),
    or ``None`` on failure.
    """
    if not _running or _app is None:
        raise RuntimeError("Telegram bot is not running — cannot send approval")
    if _bot_loop is None or not _bot_loop.is_running():
        raise RuntimeError("Telegram bot event loop is not available")

    _sent_msg_id: list[int | None] = [None]

    async def _send():
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"task_approve:{resume_token[:48]}",
                ),
                InlineKeyboardButton(
                    "❌ Deny",
                    callback_data=f"task_deny:{resume_token[:48]}",
                ),
            ]
        ])
        _MAX_TG = 4096
        _overhead = len("⏸️ <b>Approval Required</b>\n\n<b></b>\n") + len(_escape_html(task_name)) + 50
        _max_msg = _MAX_TG - _overhead
        _esc_msg = _escape_html(message)
        if len(_esc_msg) > _max_msg:
            _esc_msg = _esc_msg[:_max_msg] + "…"
        text = (
            f"⏸️ <b>Approval Required</b>\n\n"
            f"<b>{_escape_html(task_name)}</b>\n"
            f"{_esc_msg}"
        )
        try:
            sent = await _app.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode="HTML", reply_markup=keyboard,
            )
        except Exception:
            plain = re.sub(r"<[^>]+>", "", text).replace(
                "&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            sent = await _app.bot.send_message(
                chat_id=chat_id, text=plain, reply_markup=keyboard,
            )
        import time as _time
        with _pending_lock:
            _pending_task_approvals[sent.message_id] = {
                "resume_token": resume_token,
                "task_name": task_name,
                "_ts": _time.time(),
            }
        _sent_msg_id[0] = sent.message_id

    future = asyncio.run_coroutine_threadsafe(_send(), _bot_loop)
    future.result(timeout=30)
    return str(_sent_msg_id[0]) if _sent_msg_id[0] is not None else None


def _update_approval_msg(message_id: int, status: str,
                         source: str = "") -> None:
    """Edit a Telegram approval message to show it's resolved."""
    if not _running or _app is None or _bot_loop is None:
        return

    icon = "✅" if status == "approved" else "❌"
    label = "Approved" if status == "approved" else "Denied"
    source_txt = f" (from {source})" if source else ""

    async def _edit():
        from channels.telegram import _get_allowed_user_id
        chat_id = _get_allowed_user_id()
        if chat_id is None:
            return
        try:
            await _app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"{icon} <b>{label}</b>{source_txt}",
                parse_mode="HTML",
            )
        except Exception as exc:
            log.warning("Could not edit approval message %d: %s", message_id, exc)
        with _pending_lock:
            _pending_task_approvals.pop(message_id, None)

    future = asyncio.run_coroutine_threadsafe(_edit(), _bot_loop)
    try:
        future.result(timeout=15)
    except Exception:
        pass


def send_photo(chat_id: int, file_path: str, *, caption: str | None = None) -> None:
    """Send a photo to a Telegram chat from *outside* the handler context.

    Parameters
    ----------
    chat_id : int
        Telegram chat / user ID.
    file_path : str
        Absolute path to a local image file (PNG, JPG, etc.).
    caption : str, optional
        Optional caption text displayed below the photo.
    """
    if not _running or _app is None:
        raise RuntimeError("Telegram bot is not running — cannot send photo")
    if _bot_loop is None or not _bot_loop.is_running():
        raise RuntimeError("Telegram bot event loop is not available")

    async def _send():
        with open(file_path, "rb") as f:
            await _app.bot.send_photo(chat_id=chat_id, photo=f, caption=caption)

    future = asyncio.run_coroutine_threadsafe(_send(), _bot_loop)
    future.result(timeout=60)


def send_document(chat_id: int, file_path: str, *, caption: str | None = None) -> None:
    """Send a document/file to a Telegram chat from *outside* the handler context.

    Parameters
    ----------
    chat_id : int
        Telegram chat / user ID.
    file_path : str
        Absolute path to the file to send.
    caption : str, optional
        Optional caption text displayed with the document.
    """
    if not _running or _app is None:
        raise RuntimeError("Telegram bot is not running — cannot send document")
    if _bot_loop is None or not _bot_loop.is_running():
        raise RuntimeError("Telegram bot event loop is not available")

    import os as _os
    filename = _os.path.basename(file_path)

    async def _send():
        with open(file_path, "rb") as f:
            await _app.bot.send_document(
                chat_id=chat_id, document=f, filename=filename, caption=caption,
            )

    future = asyncio.run_coroutine_threadsafe(_send(), _bot_loop)
    future.result(timeout=60)


# ──────────────────────────────────────────────────────────────────────
# Channel ABC implementation
# ──────────────────────────────────────────────────────────────────────

# Alias to avoid name collision with class methods
_send_photo_fn = send_photo
_send_document_fn = send_document


class TelegramChannel(Channel):
    """Telegram channel adapter implementing the Channel ABC.

    Delegates to the module-level functions above for full backward compat.
    """

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def display_name(self) -> str:
        return "Telegram"

    @property
    def icon(self) -> str:
        return "send"

    @property
    def setup_guide(self) -> str:
        return (
            "### Quick Setup\n"
            "1. Message [@BotFather](https://t.me/BotFather) → `/newbot`\n"
            "2. Copy the **Bot Token**\n"
            "3. Message [@userinfobot](https://t.me/userinfobot) for your **User ID**\n"
            "4. Paste both below and click **Save**\n"
            "5. Click **▶️ Start Bot**"
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
            typing=True,
            reactions=True,
            slash_commands=False,
        )

    @property
    def config_fields(self) -> list[ConfigField]:
        return [
            ConfigField(
                key="bot_token",
                label="Bot Token",
                field_type="password",
                storage="env",
                env_key="TELEGRAM_BOT_TOKEN",
                help_text="Bot token from @BotFather",
            ),
            ConfigField(
                key="user_id",
                label="Your Telegram User ID",
                field_type="text",
                storage="env",
                env_key="TELEGRAM_USER_ID",
                help_text="Your numeric user ID (access control)",
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

    def get_default_target(self) -> int:
        uid = _get_allowed_user_id()
        if uid is None:
            raise RuntimeError("TELEGRAM_USER_ID is not configured")
        return uid

    def send_message(self, target: str | int, text: str) -> None:
        send_outbound(int(target), text)

    def send_photo(self, target: str | int, file_path: str,
                   caption: str | None = None) -> None:
        _send_photo_fn(int(target), file_path, caption=caption)

    def send_document(self, target: str | int, file_path: str,
                      caption: str | None = None) -> None:
        _send_document_fn(int(target), file_path, caption=caption)

    def send_approval_request(self, target: str | int,
                              interrupt_data: Any,
                              config: dict) -> str | None:
        """Send a task approval request with inline buttons to Telegram."""
        task_name = config.get("task_name", "Unknown task")
        resume_token = config.get("resume_token", "")
        message = config.get("message", "Approval required to continue.")
        msg_ref = send_task_approval(int(target), resume_token, task_name, message)
        return msg_ref

    def update_approval_message(self, message_ref: str,
                                status: str,
                                source: str = "") -> None:
        """Edit the Telegram approval message to show resolution."""
        try:
            _update_approval_msg(int(message_ref), status, source)
        except Exception as exc:
            log.warning("Failed to update Telegram approval message: %s", exc)


# ──────────────────────────────────────────────────────────────────────
# Register with channel registry
# ──────────────────────────────────────────────────────────────────────
_tg_channel = TelegramChannel()

try:
    from channels import registry as _ch_registry
    _ch_registry.register(_tg_channel)
except Exception:
    pass  # registry not yet available (shouldn't happen)
