"""
Row-Bot – Telegram Channel Adapter
==================================
Long-polling Telegram bot that bridges messages to the Row-Bot agent.

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
import hashlib
import inspect
import logging
import os
import queue
import re
import secrets
import threading
import time
from typing import Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, ReactionTypeEmoji
from telegram.error import InvalidToken, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from row_bot.brand import APP_DISPLAY_NAME
from row_bot.channels import commands as ch_commands
from row_bot.channels import runtime as ch_runtime
from row_bot.channels.base import Channel, ChannelCapabilities, ConfigField
from row_bot.channels.auth_store import get_channel_secret
from row_bot.channels.streaming import (
    ChannelDeliveryResult,
    ChannelRateLimitError,
    ChannelStreamConfig,
    ChannelStreamConsumer,
)
from row_bot.threads import _save_thread_meta, _list_threads, _thread_exists

log = logging.getLogger("row_bot.telegram")


def _agent_mod():
    import row_bot.agent as agent_mod

    return agent_mod


def _tool_registry():
    from row_bot.tools import registry as tool_registry

    return tool_registry

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
MAX_TG_MESSAGE_LEN = 4096  # Telegram text message character limit

# Bot commands registered with BotFather
BOT_COMMANDS = [
    BotCommand("help", "Show available commands"),
    BotCommand("newthread", "Start a new conversation"),
    BotCommand("model", "Switch model (cloud/local)"),
    BotCommand("approval", "Switch approval mode"),
    BotCommand("tools", "List enabled tools"),
    BotCommand("profiles", "List Agent Profiles"),
    BotCommand("profile", "Show or set Agent Profile"),
    BotCommand("agents", "Show Agent Runs"),
    BotCommand("agent", "Start a child Agent"),
    BotCommand("goal", "Start or control Goal Mode"),
    BotCommand("stop", "Stop the current generation"),
    BotCommand("skill", "Use a skill in this conversation"),
    BotCommand("skills", "Show active and suggested skills"),
    BotCommand("noskill", "Disable a skill in this conversation"),
    BotCommand("skillreset", "Reset skills in this conversation"),
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
_pending_skill_choices: dict[str, dict] = {}  # {token: {thread_id, action, choices}}
_pending_lock = threading.Lock()  # Guards both _pending_interrupts and _pending_task_approvals
_PENDING_TTL_SECONDS = 3600  # 1 hour — entries older than this are cleaned up
_INITIALIZE_RETRY_DELAY_SECONDS = 1


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
        stale_skills = [k for k, v in _pending_skill_choices.items()
                        if now - v.get("_ts", now) > 600]
        for k in stale_skills:
            _pending_skill_choices.pop(k, None)
    if stale_int or stale_task or stale_skills:
        log.info("Cleaned up %d stale interrupts, %d stale task approvals, %d stale skill choices",
                 len(stale_int), len(stale_task), len(stale_skills))


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
            try:
                from row_bot.tasks import record_thread_channel_ref

                record_thread_channel_ref(
                    tid,
                    channel="telegram",
                    target=chat_id,
                    external_conversation_id=str(chat_id),
                )
            except Exception:
                log.debug("Telegram thread channel ref skipped", exc_info=True)
            mo = rest[0] if rest else ""
            cfg = {"configurable": {"thread_id": tid}}
            if mo:
                cfg["configurable"]["model_override"] = mo
            return cfg

    import uuid
    suffix = uuid.uuid4().hex[:6]
    thread_id = f"tg_{chat_id}_{suffix}"
    name = f"✈️ Telegram – {chat_id}"
    _save_thread_meta(thread_id, name, seed_default_skills=True)
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="telegram",
            target=chat_id,
            external_conversation_id=str(chat_id),
        )
    except Exception:
        log.debug("Telegram thread channel ref skipped", exc_info=True)
    return {"configurable": {"thread_id": thread_id}}


def _new_thread(chat_id: int) -> dict:
    """Force-create a brand-new thread for the given chat."""
    import uuid
    suffix = uuid.uuid4().hex[:6]
    thread_id = f"tg_{chat_id}_{suffix}"
    name = f"✈️ Telegram – {chat_id} ({suffix})"
    _save_thread_meta(thread_id, name, seed_default_skills=True)
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="telegram",
            target=chat_id,
            external_conversation_id=str(chat_id),
        )
    except Exception:
        log.debug("Telegram thread channel ref skipped", exc_info=True)
    return {"configurable": {"thread_id": thread_id}}


# ──────────────────────────────────────────────────────────────────────
# Agent invocation (synchronous — runs in executor)
# ──────────────────────────────────────────────────────────────────────
def _refresh_thread_model_override(config: dict | None) -> dict:
    """Refresh a cached Telegram config's model override from thread metadata."""
    if not isinstance(config, dict):
        return {"configurable": {}}
    configurable = dict(config.get("configurable") or {})
    thread_id = str(configurable.get("thread_id") or "").strip()
    if not thread_id:
        return config
    try:
        from row_bot.threads import _get_thread_model_override

        model_override = _get_thread_model_override(thread_id)
    except Exception:
        log.debug("Could not refresh Telegram model override for %s", thread_id, exc_info=True)
        return config
    if model_override:
        configurable["model_override"] = model_override
    else:
        configurable.pop("model_override", None)
    return {**config, "configurable": configurable}


from row_bot.channels.media_capture import grab_vision_capture as _grab_vision_capture
from row_bot.channels.media_capture import grab_generated_image as _grab_generated_image
from row_bot.channels.media_capture import grab_generated_video as _grab_generated_video


def build_channel_runtime_config(config: dict, purpose: str) -> dict:
    """Return a runtime config for Telegram message vs approval surfaces."""
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


_SETTING_AUDIT_PREFIXES = (
    "Thread model override changed to:",
    "Thread model override cleared;",
    "Global default model changed to:",
    "Active model changed to:",
    "Thread model change cancelled.",
    "Default model change cancelled.",
    "Model change cancelled.",
    "Model scope is ambiguous",
)


def _setting_tool_audit_line(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    if str(payload.get("raw_name") or "").strip() != "row_bot_update_setting":
        return ""
    text = str(payload.get("content") or "").strip()
    if not text:
        return ""
    if any(text.startswith(prefix) for prefix in _SETTING_AUDIT_PREFIXES):
        return text
    return ""


def _assemble_telegram_agent_answer(model_text: str, tool_reports: list[str], setting_audits: list[str]) -> str:
    from row_bot.channels.agent_output import assemble_agent_answer

    deduped_audits = list(dict.fromkeys(report for report in setting_audits if str(report).strip()))
    if deduped_audits:
        audit_text = "\n".join(deduped_audits)
        if str(model_text or "").strip():
            answer = assemble_agent_answer(model_text, tool_reports)
            if audit_text not in answer:
                return f"{answer.rstrip()}\n\n{audit_text}"
            return answer
        return audit_text
    return assemble_agent_answer(model_text, tool_reports)


def _run_agent_sync(user_text: str, config: dict,
                    event_queue=None) -> tuple[str, dict | None, list[bytes], list[str]]:
    """Run the agent synchronously, collecting the full response.

    Returns (answer_text, interrupt_data_or_None, captured_images, captured_video_paths).
    If *event_queue* is provided, also pushes (event_type, payload) tuples
    for live streaming.  Pushes ``None`` sentinel when done.
    """
    # Ensure recursion limit matches the web UI (default is too low)
    agent_mod = _agent_mod()
    config = build_channel_runtime_config(config, "message")
    from row_bot.channels import runtime as orchestration_runtime

    config = orchestration_runtime.prepare_channel_turn_config(config, user_text)

    tool_registry = _tool_registry()
    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    setting_audits: list[str] = []
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
            audit_line = _setting_tool_audit_line(payload)
            if audit_line:
                setting_audits.append(audit_line)
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

    answer = _assemble_telegram_agent_answer("".join(full_answer), tool_reports, setting_audits)

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
    tool_registry = _tool_registry()
    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    setting_audits: list[str] = []
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
            audit_line = _setting_tool_audit_line(payload)
            if audit_line:
                setting_audits.append(audit_line)
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "error":
            full_answer.append(f"⚠️ Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)
        if event_queue is not None:
            event_queue.put((event_type, payload))

    answer = _assemble_telegram_agent_answer("".join(full_answer), tool_reports, setting_audits)

    if event_queue is not None:
        event_queue.put(None)

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
def _telegram_text_units(text: str) -> int:
    """Return Telegram-relevant UTF-16 code units for *text*."""
    return len(str(text or "").encode("utf-16-le")) // 2


def _prefix_units_index(text: str, max_units: int) -> int:
    units = 0
    for index, char in enumerate(text):
        units += 2 if ord(char) > 0xFFFF else 1
        if units > max_units:
            return index
    return len(text)


def _split_message(text: str, max_len: int = MAX_TG_MESSAGE_LEN) -> list[str]:
    """Split long text at paragraph or line boundaries using UTF-16 units."""
    text = str(text or "")
    if _telegram_text_units(text) <= max_len:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text

    while remaining:
        if _telegram_text_units(remaining) <= max_len:
            chunks.append(remaining)
            break

        hard_limit = max(1, _prefix_units_index(remaining, max_len))
        break_at = hard_limit
        para = remaining.rfind("\n\n", 0, hard_limit)
        if para > hard_limit // 2:
            break_at = para + 2
        else:
            line = remaining.rfind("\n", 0, hard_limit)
            if line > hard_limit // 2:
                break_at = line + 1
            else:
                space = remaining.rfind(" ", 0, hard_limit)
                if space > hard_limit // 2:
                    break_at = space + 1

        chunks.append(remaining[:break_at])
        remaining = remaining[break_at:].lstrip()

    return chunks


def _telegram_draft_id(seed: Any | None) -> int:
    """Return a deterministic non-zero Telegram draft id for one stream turn."""
    if seed is not None:
        try:
            value = abs(int(seed)) % 2_147_483_647
            return value or 1
        except Exception:
            pass
    text = str(seed or "telegram-stream")
    digest = hashlib.blake2s(
        text.encode("utf-8", errors="replace"),
        digest_size=4,
    ).digest()
    value = int.from_bytes(digest, "big") % 2_147_483_647
    return value or 1


def _telegram_suffix_units(text: str, max_units: int) -> str:
    """Return the longest suffix of *text* that fits within *max_units*."""
    if max_units <= 0:
        return ""
    value = str(text or "")
    units = 0
    for index in range(len(value) - 1, -1, -1):
        char = value[index]
        units += 2 if ord(char) > 0xFFFF else 1
        if units > max_units:
            return value[index + 1:]
    return value


def _telegram_overflow_preview_text(
    text: str,
    *,
    cursor: str = "...",
    max_units: int = MAX_TG_MESSAGE_LEN,
) -> str:
    note = "Streaming preview; final answer will be sent in parts.\n\n...\n"
    budget = max_units - _telegram_text_units(note) - _telegram_text_units(cursor)
    tail = _telegram_suffix_units(str(text or ""), budget).lstrip()
    candidate = f"{note}{tail}{cursor}"
    while _telegram_text_units(candidate) > max_units and tail:
        tail = tail[1:]
        candidate = f"{note}{tail.lstrip()}{cursor}"
    return candidate


def _is_message_not_modified(exc: Exception) -> bool:
    return "message is not modified" in str(exc).lower()


def _retry_after_from_exception(exc: Exception) -> float | None:
    for name in ("retry_after", "retry_after_seconds"):
        value = getattr(exc, name, None)
        if value is not None:
            try:
                return float(value)
            except Exception:
                pass
    return None


class TelegramStreamTransport:
    """Telegram transport for the shared channel streaming engine."""

    def __init__(
        self,
        chat,
        *,
        transport_mode: str = "auto",
        message_thread_id: int | None = None,
        draft_id: Any | None = None,
        cursor: str = "...",
    ) -> None:
        self.chat = chat
        self.requested_mode = str(transport_mode or "auto").lower()
        self.message_thread_id = message_thread_id
        draft_seed = (
            draft_id
            if draft_id is not None
            else f"{getattr(chat, 'id', '')}:{message_thread_id or ''}"
        )
        self.draft_id = _telegram_draft_id(draft_seed)
        self.cursor = cursor
        self.transport_name = f"telegram:{self.requested_mode}"
        self._selected_mode = "edit"
        self.freeze_overflow_preview = True

    @property
    def final_delivery(self) -> str:
        return "send" if self._selected_mode == "draft" else "update"

    def split_text(self, text: str) -> list[str]:
        return _split_message(str(text or ""))

    def build_overflow_preview(self, text: str, *, cursor: str = "...") -> str:
        return _telegram_overflow_preview_text(text, cursor=cursor)

    async def send_typing(self) -> None:
        try:
            await self.chat.send_action("typing")
            return
        except AttributeError:
            pass
        await self.chat.send_chat_action(action="typing")

    async def start(self, text: str):
        mode = self._select_mode()
        self._selected_mode = mode
        self.transport_name = f"telegram:{mode}"
        self.freeze_overflow_preview = mode != "draft"
        if mode == "draft":
            try:
                await self._send_draft(text)
                return {"mode": "draft"}
            except Exception:
                log.info(
                    "Telegram draft streaming failed; falling back to edit",
                    exc_info=True,
                )
                self._selected_mode = "edit"
                self.transport_name = "telegram:edit"
                self.freeze_overflow_preview = True
        return await self._send_message(text)

    async def update(self, handle, text: str, *, final: bool = False):
        if self._selected_mode == "draft":
            await self._send_draft(text)
            return handle
        html = _md_to_html(str(text or ""))
        try:
            result = await self._edit_message(handle, html, parse_mode="HTML")
        except Exception as exc:
            if _is_message_not_modified(exc):
                return handle
            retry_after = _retry_after_from_exception(exc)
            if retry_after is not None:
                raise ChannelRateLimitError(str(exc), retry_after=retry_after) from exc
            try:
                result = await self._edit_message(handle, str(text or ""))
            except Exception as plain_exc:
                if _is_message_not_modified(plain_exc):
                    return handle
                retry_after = _retry_after_from_exception(plain_exc)
                if retry_after is not None:
                    raise ChannelRateLimitError(
                        str(plain_exc),
                        retry_after=retry_after,
                    ) from plain_exc
                raise
        if not final:
            try:
                await self.send_typing()
            except Exception:
                pass
        return result or handle

    async def send_final(self, text: str) -> list[Any]:
        refs: list[Any] = []
        for chunk in self.split_text(str(text or "")):
            sent = await self._send_message_with_retry(chunk)
            refs.append(getattr(sent, "message_id", sent))
        return refs

    async def cleanup_preview(self, handle) -> None:
        if self._selected_mode == "draft":
            return
        try:
            await handle.delete()
        except Exception:
            pass

    def _select_mode(self) -> str:
        if self.requested_mode == "off":
            return "off"
        if self.requested_mode in {"draft", "auto"}:
            if self._is_private_chat() and self._draft_method() is not None:
                return "draft"
            if self.requested_mode == "draft":
                log.info("Telegram draft streaming unavailable; falling back to edit")
        return "edit"

    def _is_private_chat(self) -> bool:
        chat_type = str(getattr(self.chat, "type", "") or "").lower()
        return chat_type == "private" or bool(getattr(self.chat, "is_private", False))

    def _draft_method(self):
        method = getattr(self.chat, "send_message_draft", None)
        if callable(method):
            return method
        bot = getattr(self.chat, "bot", None) or getattr(self.chat, "_bot", None)
        method = getattr(bot, "send_message_draft", None)
        return method if callable(method) else None

    async def _send_draft(self, text: str) -> Any:
        method = self._draft_method()
        if method is None:
            raise RuntimeError("Telegram draft streaming is unavailable")
        kwargs = self._thread_kwargs()
        value = str(text or "")
        try:
            params = inspect.signature(method).parameters
        except (TypeError, ValueError):
            params = {}
        if "draft_id" in params:
            if "chat_id" in params:
                return await method(
                    chat_id=getattr(self.chat, "id", None),
                    draft_id=self.draft_id,
                    text=value,
                    **kwargs,
                )
            return await method(self.draft_id, text=value, **kwargs)
        try:
            return await method(text=value, **kwargs)
        except TypeError:
            return await method(
                chat_id=getattr(self.chat, "id", None),
                text=value,
                **kwargs,
            )

    async def _send_message(self, text: str) -> Any:
        kwargs = self._thread_kwargs()
        html = _md_to_html(str(text or ""))
        try:
            return await self.chat.send_message(html, parse_mode="HTML", **kwargs)
        except Exception as exc:
            retry_after = _retry_after_from_exception(exc)
            if retry_after is not None:
                raise ChannelRateLimitError(str(exc), retry_after=retry_after) from exc
            plain = re.sub(r"<[^>]+>", "", html).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            return await self.chat.send_message(plain, **kwargs)

    async def _send_message_with_retry(self, text: str) -> Any:
        attempts = 2
        last_error: Exception | None = None
        for attempt in range(attempts + 1):
            try:
                return await self._send_message(text)
            except ChannelRateLimitError as exc:
                last_error = exc
                if attempt >= attempts:
                    raise
                delay = max(
                    0.0,
                    float(
                        getattr(exc, "retry_after", 0.0)
                        or (0.25 * (2 ** attempt))
                    ),
                )
                await asyncio.sleep(delay)
        if last_error is not None:
            raise last_error
        return await self._send_message(text)

    async def _edit_message(self, handle, text: str, **kwargs) -> Any:
        return await handle.edit_text(text, **kwargs)

    def _thread_kwargs(self) -> dict[str, Any]:
        if self.message_thread_id:
            return {"message_thread_id": self.message_thread_id}
        return {}


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
    """Extract explicit interrupt IDs for identity-bound resume."""
    items = data if isinstance(data, list) else [data]
    ids = [item.get("__interrupt_id") for item in items
           if isinstance(item, dict) and item.get("__interrupt_id")]
    return ids or None


# ──────────────────────────────────────────────────────────────────────
# Telegram handlers
# ──────────────────────────────────────────────────────────────────────
async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    if not _is_authorised(update):
        await update.message.reply_text(
            f"⛔ Unauthorised. Your user ID is {update.effective_user.id}. "
            f"Add this ID in {APP_DISPLAY_NAME} → Settings → Channels to authorise."
        )
        return
    await _send_html(
        update.message,
        f"<b>{APP_DISPLAY_NAME}</b> is connected!\n\n"
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
    await update.message.reply_text(ch_commands.cmd_help("telegram"))


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

    from row_bot.models import get_current_model, get_cloud_provider
    from row_bot.providers.selection import (
        ModelSelectionError,
        canonicalize_model_selection,
        list_quick_model_ids,
        list_model_choice_options,
    )
    from row_bot.threads import _set_thread_model_override

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
        quick_model_ids = list_quick_model_ids("channels")
        choices = list_model_choice_options(
            "channels",
            include_values=[get_current_model(), current_ov, *quick_model_ids],
            include_inactive=True,
        )
        if choices:
            lines.append("<b>Channel Quick Choices:</b>")
            visible_choices = choices[:12]
            for choice in visible_choices:
                value = str(choice.get("value") or "")
                label = str(choice.get("label") or value)
                lines.append(f"• {_escape_html(label)} — <code>{_escape_html(value)}</code>")
            remaining = len(choices) - len(visible_choices)
            if remaining > 0:
                lines.append(f"...and {remaining} more pinned choice(s) in Settings -> Models.")
            lines.append("\nUsage: <code>/model model:provider:model-id</code>")
            lines.append("Reset to default: <code>/model default</code>")
        else:
            lines.append("No provider Quick Choices. Pin models in Row-Bot → Settings → Providers.")
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

    try:
        canonical = canonicalize_model_selection(model_id, "channels")
    except ModelSelectionError as exc:
        await update.message.reply_text(
            f"Unknown model choice: {model_id}\n{exc}\n"
            "Use /model to see available Quick Choices."
        )
        return

    # Set the model override
    config = context.chat_data.get("thread_config")
    if config is None:
        chat_id = update.effective_chat.id
        config = _get_or_create_thread(chat_id)
    tid = (config.get("configurable") or {}).get("thread_id", "")
    config["configurable"]["model_override"] = canonical.ref
    _set_thread_model_override(tid, canonical.ref)
    context.chat_data["thread_config"] = config

    await update.message.reply_text(f"Switched to {canonical.display_label or canonical.ref}")


async def _cmd_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /approval for this Telegram conversation."""
    if not _is_authorised(update):
        return
    config = context.chat_data.get("thread_config")
    if config is None:
        chat_id = update.effective_chat.id
        config = _get_or_create_thread(chat_id)
        context.chat_data["thread_config"] = config
    thread_id = (config.get("configurable") or {}).get("thread_id", "")
    args = (update.message.text or "").split(maxsplit=1)
    arg = args[1] if len(args) > 1 else ""
    response = ch_commands.cmd_approval("telegram", arg, thread_id=thread_id)
    await update.message.reply_text(response)


def _thread_id_for_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    config = context.chat_data.get("thread_config")
    if config is None:
        chat_id = update.effective_chat.id
        config = _get_or_create_thread(chat_id)
        context.chat_data["thread_config"] = config
    return str((config.get("configurable") or {}).get("thread_id", ""))


async def _cmd_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /profiles for this Telegram conversation."""
    if not _is_authorised(update):
        return
    args = (update.message.text or "").split(maxsplit=1)
    arg = args[1] if len(args) > 1 else ""
    response = ch_commands.cmd_profiles("telegram", arg)
    await update.message.reply_text(response)


async def _cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /profile for this Telegram conversation."""
    if not _is_authorised(update):
        return
    thread_id = _thread_id_for_command(update, context)
    args = (update.message.text or "").split(maxsplit=1)
    arg = args[1] if len(args) > 1 else ""
    response = ch_commands.cmd_profile("telegram", arg, thread_id=thread_id)
    await update.message.reply_text(response)


async def _cmd_agents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /agents for this Telegram conversation."""
    if not _is_authorised(update):
        return
    thread_id = _thread_id_for_command(update, context)
    args = (update.message.text or "").split(maxsplit=1)
    arg = args[1] if len(args) > 1 else ""
    response = ch_commands.cmd_agents("telegram", arg, thread_id=thread_id)
    await update.message.reply_text(response)


async def _cmd_agent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /agent for this Telegram conversation."""
    if not _is_authorised(update):
        return
    thread_id = _thread_id_for_command(update, context)
    tool_registry = _tool_registry()
    enabled_tool_names = [tool.name for tool in tool_registry.get_enabled_tools()]
    response = ch_commands.cmd_agent(
        "telegram",
        update.message.text or "",
        thread_id=thread_id,
        enabled_tool_names=enabled_tool_names,
    )
    await update.message.reply_text(response)


async def _cmd_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /goal for this Telegram conversation."""
    if not _is_authorised(update):
        return
    thread_id = _thread_id_for_command(update, context)
    text = update.message.text or ""
    goal_start = ch_runtime.prepare_channel_goal_start(text, thread_id)
    if goal_start is not None:
        chat_id = update.effective_chat.id
        config = {"configurable": {"thread_id": thread_id}}
        _goal_run_turn, _goal_send_text = _telegram_goal_callbacks(
            update.effective_chat,
            message_thread_id=_telegram_message_thread_id(update.message),
            draft_id_seed=getattr(update.message, "message_id", None),
        )

        await _send_telegram_safe_text(
            update.effective_chat,
            ch_runtime.format_goal_started_ack(goal_start),
            message_thread_id=_telegram_message_thread_id(update.message),
        )
        result = await ch_runtime.run_channel_goal_async(
            channel_name="telegram",
            thread_id=thread_id,
            config=config,
            first_prompt=goal_start.prompt,
            run_turn=_goal_run_turn,
            send_text=_goal_send_text,
        )
        if result.interrupt_data:
            await _send_interrupt_approval_card(
                update.effective_chat,
                chat_id,
                result.interrupt_data,
                config,
                message_thread_id=_telegram_message_thread_id(update.message),
            )
        return
    args = text.split(maxsplit=1)
    arg = args[1] if len(args) > 1 else ""
    response = ch_commands.cmd_goal("telegram", arg, thread_id=thread_id)
    await update.message.reply_text(response)


async def _cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tools — list enabled tools."""
    if not _is_authorised(update):
        return
    tool_registry = _tool_registry()
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
    tool_registry = _tool_registry()
    enabled_count = len(tool_registry.get_enabled_tools())
    await update.message.reply_text(
        f"✅ Row-Bot Telegram bot is running.\n"
        f"🔧 {enabled_count} tools enabled.\n"
        f"👤 Authorised user: {_get_allowed_user_id()}"
    )


async def _cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stop through the shared channel command path."""
    if not _is_authorised(update):
        return
    response = ch_commands.dispatch("telegram", "/stop")
    await update.message.reply_text(response or "Stop is not available in this context.")


def _skill_reply_markup(result, *, thread_id: str, action: str) -> InlineKeyboardMarkup | None:
    choices = list(getattr(result, "choices", ()) or ())
    if not choices:
        return None
    token = secrets.token_urlsafe(8)
    with _pending_lock:
        _pending_skill_choices[token] = {
            "thread_id": thread_id,
            "action": action,
            "choices": choices,
            "_ts": time.time(),
        }
    rows = []
    for index, choice in enumerate(choices[:10]):
        rows.append([
            InlineKeyboardButton(
                choice.display_name[:48],
                callback_data=f"skill_pick:{token}:{index}",
            )
        ])
    return InlineKeyboardMarkup(rows)


async def _reply_skill_result(message, result, *, thread_id: str, action: str) -> None:
    markup = _skill_reply_markup(result, thread_id=thread_id, action=action)
    if markup is not None:
        await message.reply_text(result.text, reply_markup=markup)
        return
    await message.reply_text(result.text)


async def _cmd_skill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle Smart Skills commands for the current Telegram thread."""
    if not _is_authorised(update):
        return
    chat_id = update.effective_chat.id
    config = context.chat_data.get("thread_config")
    if config is None:
        config = _get_or_create_thread(chat_id)
        context.chat_data["thread_config"] = config
    thread_id = (config.get("configurable") or {}).get("thread_id", "")
    text = update.message.text or ""
    result = ch_commands.cmd_skill_result(
        "telegram",
        text,
        thread_id=thread_id,
    )
    action = "disable" if ch_commands.command_token(text) == "/noskill" else "activate"
    await _reply_skill_result(update.message, result, thread_id=thread_id, action=action)


async def _send_html(target, text: str, **kwargs) -> None:
    """Send a message as HTML, falling back to plain text on parse errors."""
    for chunk in _split_message(text):
        try:
            await target.reply_text(chunk, parse_mode="HTML", **kwargs)
        except Exception:
            # Strip HTML tags and send as plain text.
            plain = re.sub(r"<[^>]+>", "", chunk).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            for plain_chunk in _split_message(plain):
                await target.reply_text(plain_chunk, **kwargs)


async def _send_html_msg(chat, text: str, **kwargs) -> None:
    """Send a message via chat.send_message as HTML with plain-text fallback."""
    for chunk in _split_message(text):
        try:
            await chat.send_message(chunk, parse_mode="HTML", **kwargs)
        except Exception:
            plain = re.sub(r"<[^>]+>", "", chunk).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            for plain_chunk in _split_message(plain):
                await chat.send_message(plain_chunk, **kwargs)


from row_bot.channels.thread_repair import is_corrupt_thread_error as _is_corrupt_thread_error


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
    message_thread_id: int | None = None,
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
        await _send_interrupt_approval_card(
            chat,
            chat_id,
            interrupt_data,
            config,
            message_thread_id=message_thread_id,
        )
    else:
        html = _md_to_html(answer)
        for chunk in _split_message(html):
            await _send_html_msg(chat, chunk)


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


def _telegram_stream_config() -> ChannelStreamConfig:
    try:
        from row_bot.channels import config as channel_config_store

        channel_config = channel_config_store.get_all("telegram")
    except Exception:
        channel_config = {}
    enabled = bool(channel_config.get("streaming.enabled", True))
    return ChannelStreamConfig(
        channel="telegram",
        transport_mode=str(channel_config.get("streaming.transport", "auto") if enabled else "off"),
        update_interval_s=_cfg_float(channel_config, "streaming.update_interval_s", 0.8),
        max_stale_interval_s=_cfg_float(channel_config, "streaming.max_stale_interval_s", 3.0),
        min_update_chars=_cfg_int(channel_config, "streaming.min_update_chars", 24),
        typing_interval_s=_cfg_float(channel_config, "streaming.typing_interval_s", 4.0),
        cursor=str(channel_config.get("streaming.cursor", "...")),
        max_message_units=MAX_TG_MESSAGE_LEN,
        fresh_final_after_s=_cfg_float(channel_config, "streaming.fresh_final_after_s", 60.0),
        sparse_progress=bool(channel_config.get("streaming.sparse_progress", True)),
    )


def _telegram_message_thread_id(message) -> int | None:
    value = getattr(message, "message_thread_id", None)
    try:
        return int(value) if value else None
    except Exception:
        return None


async def _stream_agent_turn_to_telegram(
    chat,
    user_text: str,
    config: dict,
    *,
    message_thread_id: int | None = None,
    draft_id_seed: Any | None = None,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    loop = asyncio.get_event_loop()
    event_queue: queue.Queue = queue.Queue()
    stream_config = _telegram_stream_config()
    transport = TelegramStreamTransport(
        chat,
        transport_mode=stream_config.transport_mode,
        message_thread_id=message_thread_id,
        draft_id=draft_id_seed,
        cursor=stream_config.cursor,
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
            channel_name="telegram",
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


async def _stream_agent_resume_to_telegram(
    chat,
    config: dict,
    approved: bool,
    *,
    interrupt_ids: list[str] | None = None,
    message_thread_id: int | None = None,
    draft_id_seed: Any | None = None,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    loop = asyncio.get_event_loop()
    event_queue: queue.Queue = queue.Queue()
    stream_config = _telegram_stream_config()
    transport = TelegramStreamTransport(
        chat,
        transport_mode=stream_config.transport_mode,
        message_thread_id=message_thread_id,
        draft_id=draft_id_seed,
        cursor=stream_config.cursor,
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
            channel_name="telegram",
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


async def _send_telegram_safe_text(
    chat,
    message: str,
    *,
    message_thread_id: int | None = None,
) -> None:
    kwargs = {"message_thread_id": message_thread_id} if message_thread_id else {}
    await _send_html_msg(chat, _md_to_html(str(message or "")), **kwargs)


async def _send_telegram_media(
    chat,
    captured_images: list[bytes],
    captured_video_paths: list[str] | None = None,
) -> None:
    for img_bytes in captured_images:
        try:
            import io
            await chat.send_photo(photo=io.BytesIO(img_bytes), caption="🖼️ Image")
        except Exception as exc:
            log.warning("Failed to send image to Telegram: %s", exc)
    for vpath in (captured_video_paths or []):
        try:
            await chat.send_document(
                document=open(vpath, "rb"),
                caption="🎬 Video",
                filename=os.path.basename(vpath),
            )
        except Exception as exc:
            log.warning("Failed to send video to Telegram: %s", exc)


def _interrupt_approval_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data="interrupt_approve"),
            InlineKeyboardButton("❌ Deny", callback_data="interrupt_deny"),
        ]
    ])


async def _send_interrupt_approval_card(
    chat,
    chat_id: int,
    interrupt_data: Any,
    config: dict,
    *,
    message_thread_id: int | None = None,
) -> None:
    with _pending_lock:
        _pending_interrupts[chat_id] = {
            "data": interrupt_data,
            "config": config,
            "_ts": time.time(),
        }
    kwargs = {"message_thread_id": message_thread_id} if message_thread_id else {}
    await _send_html_msg(
        chat,
        _format_interrupt(interrupt_data),
        reply_markup=_interrupt_approval_keyboard(),
        **kwargs,
    )


def _telegram_goal_callbacks(
    chat,
    *,
    message_thread_id: int | None = None,
    draft_id_seed: Any | None = None,
):
    state: dict[str, ChannelDeliveryResult | None] = {"delivery": None}
    turn_state: dict[str, int] = {"index": 0}

    async def _goal_run_turn(prompt: str, cfg: dict):
        turn_state["index"] += 1
        seed = (
            f"{draft_id_seed}:{turn_state['index']}"
            if draft_id_seed is not None
            else (
                f"{getattr(chat, 'id', '')}:{message_thread_id or ''}:goal:"
                f"{turn_state['index']}"
            )
        )
        (
            answer,
            interrupt_data,
            captured_images,
            captured_video_paths,
            delivery,
        ) = await _stream_agent_turn_to_telegram(
            chat,
            prompt,
            cfg,
            message_thread_id=message_thread_id,
            draft_id_seed=seed,
        )
        state["delivery"] = delivery
        await _send_telegram_media(chat, captured_images, captured_video_paths)
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
        await _send_telegram_safe_text(chat, message, message_thread_id=message_thread_id)

    return _goal_run_turn, _goal_send_text


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
    config = _refresh_thread_model_override(config)
    context.chat_data["thread_config"] = config

    # React + typing
    await _react(msg, "👀")
    await update.effective_chat.send_action("typing")

    try:
        answer, interrupt_data, captured_images, captured_video_paths, delivery = await _stream_agent_turn_to_telegram(
            update.effective_chat,
            user_text,
            config,
            message_thread_id=_telegram_message_thread_id(msg),
            draft_id_seed=getattr(msg, "message_id", None),
        )
    except Exception as exc:
        log.error("Agent error for chat %s: %s", chat_id, exc)
        if _is_corrupt_thread_error(exc):
            try:
                from row_bot.agent import repair_orphaned_tool_calls
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, repair_orphaned_tool_calls, None, config
                )
                log.info("Repaired orphaned tool calls for chat %s, retrying", chat_id)
                answer, interrupt_data, captured_images, captured_video_paths = await loop.run_in_executor(
                    None, _run_agent_sync, user_text, config
                )
                delivery = ChannelDeliveryResult(
                    delivered=False,
                    streamed=False,
                    finalized=False,
                    fallback_sent=False,
                    transport="telegram:repair",
                    final_text=answer,
                    error="repair retry used non-streaming fallback",
                )
            except Exception as retry_exc:
                log.error("Retry after repair failed: %s", retry_exc)
                config = _new_thread(chat_id)
                context.chat_data["thread_config"] = config
                await _react(msg, "💔")
                await msg.reply_text(
                    "⚠️ The previous conversation had a stuck tool call "
                    "and couldn't be repaired.\n"
                    "🆕 I've started a fresh thread — please resend your message."
                )
                return
        else:
            await _react(msg, "💔")
            await msg.reply_text(f"⚠️ Error: {exc}")
            return

    await _react(msg, "👍")

    # Extract YouTube URLs for separate messages (auto-preview)
    from row_bot.channels import extract_youtube_urls
    if ch_runtime.channel_turn_is_suspended(answer):
        return
    clean_answer, yt_urls = extract_youtube_urls(answer) if answer else ("", [])

    # If streaming/final delivery covered the response, skip re-sending text.
    if delivery.delivered and not interrupt_data:
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
        # Streaming was unavailable or final delivery failed; send normally.
        await _send_agent_response(
            update.effective_chat, msg, chat_id, config,
            clean_answer, interrupt_data, captured_images,
            captured_video_paths,
            message_thread_id=_telegram_message_thread_id(msg),
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

    from row_bot.channels.base import record_activity
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
    if cb_data.startswith("skill_pick:"):
        parts = cb_data.split(":")
        if len(parts) != 3:
            await query.edit_message_text("Skill choice is no longer available.")
            return
        token, raw_index = parts[1], parts[2]
        try:
            index = int(raw_index)
        except ValueError:
            await query.edit_message_text("Skill choice is no longer available.")
            return
        with _pending_lock:
            pending = _pending_skill_choices.pop(token, None)
        if pending is None:
            await query.edit_message_text("Skill choice expired. Send /skills again.")
            return
        choices = list(pending.get("choices") or [])
        if index < 0 or index >= len(choices):
            await query.edit_message_text("Skill choice is no longer available.")
            return
        choice = choices[index]
        action = pending.get("action") or "activate"
        thread_id = pending.get("thread_id") or ""
        command_text = f"/noskill {choice.slug}" if action == "disable" else f"/skill {choice.slug}"
        result = ch_commands.cmd_skill_result(
            "telegram",
            command_text,
            thread_id=thread_id,
        )
        await query.edit_message_text(result.text)
        return

    if cb_data.startswith("task_approve:") or cb_data.startswith("task_deny:"):
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
            from row_bot.tasks import respond_to_approval
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

    config = _refresh_thread_model_override(pending["config"])
    context.chat_data["thread_config"] = config
    interrupt_ids = _extract_interrupt_ids(pending["data"])
    ch_runtime.resolve_goal_approval_for_config(config, approved)

    # Send typing indicator
    await update.effective_chat.send_action("typing")

    try:
        answer, new_interrupt, captured_images, captured_video_paths, delivery = await _stream_agent_resume_to_telegram(
            update.effective_chat,
            config,
            approved,
            interrupt_ids=interrupt_ids,
            message_thread_id=_telegram_message_thread_id(query.message),
            draft_id_seed=getattr(query.message, "message_id", None),
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

    await _send_telegram_media(update.effective_chat, captured_images, captured_video_paths)

    if new_interrupt:
        await _send_interrupt_approval_card(
            update.effective_chat,
            chat_id,
            new_interrupt,
            config,
            message_thread_id=_telegram_message_thread_id(query.message),
        )
    else:
        if not delivery.delivered:
            await _send_telegram_safe_text(
                update.effective_chat,
                answer,
                message_thread_id=_telegram_message_thread_id(query.message),
            )
        thread_id = ch_runtime.thread_id_from_config(config)
        if thread_id and answer:
            _goal_run_turn, _goal_send_text = _telegram_goal_callbacks(
                update.effective_chat,
                message_thread_id=_telegram_message_thread_id(query.message),
                draft_id_seed=getattr(query.message, "message_id", None),
            )

            goal_result = await ch_runtime.continue_channel_goal_after_turn_async(
                channel_name="telegram",
                thread_id=thread_id,
                config=config,
                assistant_text=answer,
                interrupt_data=None,
                run_turn=_goal_run_turn,
                send_text=_goal_send_text,
            )
            if goal_result.interrupt_data:
                await _send_interrupt_approval_card(
                    update.effective_chat,
                    chat_id,
                    goal_result.interrupt_data,
                    config,
                    message_thread_id=_telegram_message_thread_id(query.message),
                )


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

    from row_bot.channels.media import transcribe_audio
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
        import row_bot.tools.image_gen_tool as _igt_mod

        same_thread = (_igt_mod._image_cache_thread_id == thread_id)
        if not same_thread:
            _igt_mod._image_cache.clear()
        _igt_mod._image_cache_thread_id = thread_id
        _igt_mod._image_cache[cache_name] = bytes(data)
    except Exception:
        log.debug("Failed to cache inbound Telegram photo '%s'", cache_name, exc_info=True)

    from row_bot.channels.media import analyze_image
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

    from row_bot.channels.media import save_inbound_file, extract_document_text, copy_to_workspace
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
async def _cleanup_failed_start(application: Application) -> None:
    """Best-effort cleanup for a partially started Telegram application."""
    updater = application.updater
    cleanup_steps = (
        ("updater stop", updater.stop if updater and updater.running else None),
        ("application stop", application.stop if application.running else None),
        ("application shutdown", application.shutdown),
        ("bot shutdown", application.bot.shutdown),
    )
    for step, operation in cleanup_steps:
        if operation is None:
            continue
        try:
            await operation()
        except Exception as exc:
            log.warning(
                "Telegram startup cleanup failed during %s (%s)",
                step,
                type(exc).__name__,
            )


async def _register_bot_commands(application: Application) -> None:
    """Register Telegram commands without affecting the live polling bot."""
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:
        log.warning("Could not register bot commands (%s)", type(exc).__name__)


async def start_bot() -> bool:
    """Initialise and start the Telegram bot (long polling).

    Returns True on success, False if not configured or already running.
    """
    global _app, _running, _bot_loop

    if _running:
        log.info("Telegram bot is already running")
        return True

    if not is_configured():
        log.info("Telegram bot not configured — skipping start")
        return False

    token = _get_bot_token()

    _app = Application.builder().token(token).build()

    # Register handlers
    _app.add_handler(CommandHandler("start", _cmd_start))
    _app.add_handler(CommandHandler("help", _cmd_help))
    _app.add_handler(CommandHandler("newthread", _cmd_newthread))
    _app.add_handler(CommandHandler("model", _cmd_model))
    _app.add_handler(CommandHandler("approval", _cmd_approval))
    _app.add_handler(CommandHandler("tools", _cmd_tools))
    _app.add_handler(CommandHandler("profiles", _cmd_profiles))
    _app.add_handler(CommandHandler("profile", _cmd_profile))
    _app.add_handler(CommandHandler("agents", _cmd_agents))
    _app.add_handler(CommandHandler("agent", _cmd_agent))
    _app.add_handler(CommandHandler("goal", _cmd_goal))
    _app.add_handler(CommandHandler("status", _cmd_status))
    _app.add_handler(CommandHandler("stop", _cmd_stop))
    _app.add_handler(CommandHandler("skill", _cmd_skill))
    _app.add_handler(CommandHandler("skills", _cmd_skill))
    _app.add_handler(CommandHandler("noskill", _cmd_skill))
    _app.add_handler(CommandHandler("skillreset", _cmd_skill))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message))
    _app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _handle_voice))
    _app.add_handler(MessageHandler(filters.PHOTO, _handle_photo))
    _app.add_handler(MessageHandler(filters.Document.ALL, _handle_document))
    _app.add_handler(CallbackQueryHandler(_handle_callback))

    # Initialise and start polling
    try:
        for attempt in range(2):
            try:
                await _app.initialize()
                break
            except NetworkError as exc:
                if attempt == 1:
                    raise
                log.warning(
                    "Telegram initialization failed (%s); retrying once in one second",
                    type(exc).__name__,
                )
                await asyncio.sleep(_INITIALIZE_RETRY_DELAY_SECONDS)
        await _app.start()
        await _app.updater.start_polling(
            drop_pending_updates=True,
            bootstrap_retries=1,
        )
    except InvalidToken:
        await _cleanup_failed_start(_app)
        _app = None
        _running = False
        _bot_loop = None
        raise RuntimeError(
            "Telegram rejected the configured bot token; update it in Settings."
        ) from None
    except Exception:
        await _cleanup_failed_start(_app)
        _app = None
        _running = False
        _bot_loop = None
        raise

    _bot_loop = asyncio.get_running_loop()
    _running = True
    asyncio.create_task(
        _register_bot_commands(_app),
        name="telegram-register-commands",
    )

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
        from row_bot.channels.telegram import _get_allowed_user_id
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
    from row_bot.channels import registry as _ch_registry
    _ch_registry.register(_tg_channel)
except Exception:
    pass  # registry not yet available (shouldn't happen)
