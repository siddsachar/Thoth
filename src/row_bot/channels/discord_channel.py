"""
Row-Bot – Discord Channel Adapter
===================================
Discord bot using discord.py with Gateway Intents (WebSocket).

Setup:
    1. Go to https://discord.com/developers/applications → **New Application**
    2. Under **Bot**, click **Add Bot** → copy the **Bot Token**
    3. Enable **Privileged Gateway Intents**:
       ``MESSAGE CONTENT``, ``SERVER MEMBERS`` (optional)
    4. Under **OAuth2 → URL Generator**, select scopes ``bot`` +
       permissions: ``Send Messages``, ``Attach Files``, ``Read Message History``,
       ``Add Reactions``, ``Use Slash Commands``
    5. Use the generated URL to invite the bot to your server
    6. Paste the **Bot Token** in Settings → Channels → Discord
    7. (Optional) Set your Discord User ID for DM-only access control

Required keys (stored via api_keys):
    DISCORD_BOT_TOKEN  – Bot token from Developer Portal
    DISCORD_USER_ID    – Your Discord user ID (optional, for access control)
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
    ChannelStreamConfig,
    ChannelStreamConsumer,
)
from row_bot.threads import _save_thread_meta

log = logging.getLogger("row_bot.discord")


def _agent_mod():
    import row_bot.agent as agent_mod

    return agent_mod

# ──────────────────────────────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────────────────────────────
_client = None                          # discord.Client
_running = False
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_thread: threading.Thread | None = None
_pending_interrupts: dict[str, dict] = {}   # context_key -> interrupt_data
_interrupt_by_message: dict[int, str] = {}   # approval message id -> context_key
_interrupt_by_channel: dict[int, str] = {}   # channel_id -> latest context_key
_pending_task_approvals: dict[int, dict] = {}  # message_id -> {resume_token, task_name, _ts}
_task_approval_by_channel: dict[int, int] = {}  # dm_channel_id -> message_id
_pending_lock = threading.Lock()

DISCORD_MAX_LEN = 2000  # Discord message character limit


# ──────────────────────────────────────────────────────────────────────
# Helpers — credentials
# ──────────────────────────────────────────────────────────────────────
def _get_bot_token() -> str:
    return get_channel_secret("discord", "DISCORD_BOT_TOKEN")


def _get_allowed_user_id() -> int | None:
    raw = get_channel_secret("discord", "DISCORD_USER_ID")
    if raw.strip().isdigit():
        return int(raw.strip())
    return None


# ──────────────────────────────────────────────────────────────────────
# Configuration & lifecycle helpers
# ──────────────────────────────────────────────────────────────────────
def is_configured() -> bool:
    return bool(_get_bot_token())


def is_running() -> bool:
    return _running


# ──────────────────────────────────────────────────────────────────────
# Thread management
# ──────────────────────────────────────────────────────────────────────
def _make_thread_id(channel_id: int | str) -> str:
    return f"discord_{channel_id}"


def _get_or_create_thread(channel_id: int | str) -> str:
    thread_id = _make_thread_id(channel_id)
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="discord",
            target=channel_id,
            external_conversation_id=str(channel_id),
        )
    except Exception:
        log.debug("Discord thread channel ref skipped", exc_info=True)
    _save_thread_meta(
        thread_id,
        "🎮 Discord conversation",
        seed_default_skills=True,
    )  # creates or bumps updated_at
    return thread_id


def _new_thread(channel_id: int | str) -> str:
    import time
    suffix = str(int(time.time()))
    thread_id = f"discord_{channel_id}_{suffix}"
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="discord",
            target=channel_id,
            external_conversation_id=str(channel_id),
        )
    except Exception:
        log.debug("Discord thread channel ref skipped", exc_info=True)
    _save_thread_meta(thread_id, "Discord conversation", seed_default_skills=True)
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
                    event_queue=None) -> tuple[str, dict | None, list[bytes]]:
    from row_bot.tools import registry as tool_registry
    from row_bot.channels.media_capture import grab_vision_capture, grab_generated_image, grab_generated_video

    agent_mod = _agent_mod()
    config = build_channel_runtime_config(config, "message")
    from row_bot.channels import runtime as orchestration_runtime

    config = orchestration_runtime.prepare_channel_turn_config(config, user_text)
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

    from row_bot.channels.agent_output import assemble_agent_answer

    answer = assemble_agent_answer("".join(full_answer), tool_reports)
    if interrupt_data is None and orchestration_runtime.finalize_channel_orchestration(
        config,
        answer,
        enabled,
    ):
        answer = orchestration_runtime.orchestration_suspended_final()

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
        if event_queue is not None:
            event_queue.put((event_type, payload))

    from row_bot.channels.agent_output import assemble_agent_answer

    answer = assemble_agent_answer("".join(full_answer), tool_reports)

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
# Message sending
# ──────────────────────────────────────────────────────────────────────
def _split_message(text: str, max_len: int = DISCORD_MAX_LEN) -> list[str]:
    """Split text into Discord-friendly chunks."""
    if len(text) <= max_len:
        return [text]

    parts = []
    while text:
        if len(text) <= max_len:
            parts.append(text)
            break
        # Try to split at code block first, then paragraph, then line
        idx = text.rfind("\n\n", 0, max_len)
        if idx < max_len // 3:
            idx = text.rfind("\n", 0, max_len)
        if idx < max_len // 4:
            idx = text.rfind(" ", 0, max_len)
        if idx < max_len // 4:
            idx = max_len
        parts.append(text[:idx])
        text = text[idx:].lstrip()
    return parts


# ──────────────────────────────────────────────────────────────────────
# Streaming: rate-limited edit consumer
# ──────────────────────────────────────────────────────────────────────
_STREAM_EDIT_INTERVAL = 1.5


async def _discord_edit_consumer(sent_msg, event_queue: queue.Queue,
                                 loop) -> str | None:
    """Read events from queue and edit *sent_msg* progressively.

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
            if len(display) > DISCORD_MAX_LEN:
                overflow = True
                continue
            try:
                await sent_msg.edit(content=display or "⏳")
                last_sent_display = display
            except Exception:
                pass
            last_edit = now

    display = _build_stream_display(tool_lines, accumulated)
    if display and not overflow:
        try:
            await sent_msg.edit(content=display)
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


class DiscordStreamTransport:
    """Discord edit-stream transport for the shared channel streaming engine."""

    transport_name = "discord:edit"

    def __init__(self, channel) -> None:
        self.channel = channel

    async def send_typing(self) -> None:
        try:
            await self.channel.trigger_typing()
        except Exception:
            pass

    async def start(self, text: str):
        return await self.channel.send(str(text or ""))

    async def update(self, handle, text: str, *, final: bool = False):
        return await handle.edit(content=str(text or ""))

    async def send_final(self, text: str) -> list[Any]:
        refs: list[Any] = []
        for chunk in self.split_text(str(text or "")):
            sent = await self.channel.send(chunk)
            refs.append(getattr(sent, "id", sent))
        return refs

    async def cleanup_preview(self, handle) -> None:
        try:
            await handle.delete()
        except Exception:
            pass

    def split_text(self, text: str) -> list[str]:
        return _split_message(str(text or ""))


def _discord_context_key(channel_id: int, thread_id: int | None = None) -> str:
    suffix = str(thread_id or "")
    return f"{int(channel_id)}:{suffix}" if suffix else str(int(channel_id))


def _store_discord_interrupt(
    *,
    channel_id: int,
    interrupt_data: Any,
    config: dict,
    thread_id: int | None = None,
    message_id: int | None = None,
) -> str:
    import time as _time

    context_key = _discord_context_key(channel_id, thread_id)
    with _pending_lock:
        _pending_interrupts[context_key] = {
            "data": interrupt_data,
            "config": config,
            "channel_id": int(channel_id),
            "thread_id": thread_id,
            "_ts": _time.time(),
        }
        _interrupt_by_channel[int(channel_id)] = context_key
        if message_id is not None:
            _interrupt_by_message[int(message_id)] = context_key
    return context_key


def _peek_discord_interrupt(channel_id: int, thread_id: int | None = None) -> dict | None:
    with _pending_lock:
        exact_key = _discord_context_key(channel_id, thread_id)
        pending = _pending_interrupts.get(exact_key)
        if pending is not None:
            return pending
        context_key = _interrupt_by_channel.get(int(channel_id))
        return _pending_interrupts.get(context_key) if context_key else None


def _pop_discord_interrupt(
    *,
    channel_id: int,
    thread_id: int | None = None,
    message_id: int | None = None,
) -> dict | None:
    with _pending_lock:
        context_key = None
        if message_id is not None:
            context_key = _interrupt_by_message.pop(int(message_id), None)
        if context_key is None:
            exact_key = _discord_context_key(channel_id, thread_id)
            if exact_key in _pending_interrupts:
                context_key = exact_key
        if context_key is None:
            context_key = _interrupt_by_channel.get(int(channel_id))
        if context_key is None:
            return None
        pending = _pending_interrupts.pop(context_key, None)
        if pending:
            _interrupt_by_channel.pop(int(pending.get("channel_id") or channel_id), None)
            for msg_id, mapped_key in list(_interrupt_by_message.items()):
                if mapped_key == context_key:
                    _interrupt_by_message.pop(msg_id, None)
        return pending


def _discord_channel_thread_id(channel) -> int | None:
    return int(getattr(channel, "id", 0) or 0) if getattr(channel, "type", None) else None


def _make_discord_interrupt_view():
    try:
        import discord
    except Exception:
        return None
    ui = getattr(discord, "ui", None)
    if ui is None or not hasattr(ui, "View") or not hasattr(ui, "button"):
        return None

    class InterruptApprovalView(ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=3600)

        @ui.button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id="rowbot_interrupt_approve",
        )
        async def approve(self, interaction, _button) -> None:
            try:
                await interaction.response.defer()
            except Exception:
                pass
            await _handle_discord_interrupt_button(
                interaction.channel,
                interaction.message,
                approved=True,
            )

        @ui.button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id="rowbot_interrupt_deny",
        )
        async def deny(self, interaction, _button) -> None:
            try:
                await interaction.response.defer()
            except Exception:
                pass
            await _handle_discord_interrupt_button(
                interaction.channel,
                interaction.message,
                approved=False,
            )

    return InterruptApprovalView()


def _make_discord_task_approval_view():
    try:
        import discord
    except Exception:
        return None
    ui = getattr(discord, "ui", None)
    if ui is None or not hasattr(ui, "View") or not hasattr(ui, "button"):
        return None

    class TaskApprovalView(ui.View):
        def __init__(self) -> None:
            super().__init__(timeout=3600)

        @ui.button(
            label="Approve",
            style=discord.ButtonStyle.success,
            custom_id="rowbot_task_approve",
        )
        async def approve(self, interaction, _button) -> None:
            try:
                await interaction.response.defer()
            except Exception:
                pass
            await _handle_discord_task_approval_button(
                interaction.channel,
                interaction.message,
                approved=True,
            )

        @ui.button(
            label="Deny",
            style=discord.ButtonStyle.danger,
            custom_id="rowbot_task_deny",
        )
        async def deny(self, interaction, _button) -> None:
            try:
                await interaction.response.defer()
            except Exception:
                pass
            await _handle_discord_task_approval_button(
                interaction.channel,
                interaction.message,
                approved=False,
            )

    return TaskApprovalView()


async def _send_discord_interrupt_approval_card(
    channel,
    interrupt_data: Any,
    config: dict,
) -> int | None:
    from row_bot.channels import approval as approval_helpers

    detail = approval_helpers.format_interrupt_text(interrupt_data)
    view = _make_discord_interrupt_view()
    text = detail if view is not None else f"{detail}\n\nReply **yes** or **no**."
    sent = await channel.send(text, **({"view": view} if view is not None else {}))
    message_id = getattr(sent, "id", None)
    _store_discord_interrupt(
        channel_id=int(getattr(channel, "id", 0)),
        interrupt_data=interrupt_data,
        config=config,
        thread_id=None,
        message_id=int(message_id) if message_id is not None else None,
    )
    return int(message_id) if message_id is not None else None


async def _handle_discord_interrupt_button(channel, message, *, approved: bool) -> None:
    channel_id = int(getattr(channel, "id", 0))
    message_id = getattr(message, "id", None)
    pending = _pop_discord_interrupt(
        channel_id=channel_id,
        message_id=int(message_id) if message_id is not None else None,
    )
    if pending is None:
        try:
            await message.edit(content="This approval is no longer pending.", view=None)
        except Exception:
            pass
        return

    action = "Approved" if approved else "Denied"
    try:
        await message.edit(content=f"{action} - processing...", view=None)
    except Exception:
        log.debug("Failed to update Discord interrupt approval message", exc_info=True)

    try:
        from row_bot.channels import approval as approval_helpers

        config = pending.get("config", {})
        interrupt_ids = approval_helpers.extract_interrupt_ids(pending.get("data"))
        ch_runtime.resolve_goal_approval_for_config(config, approved)
        (
            answer,
            new_interrupt,
            captured_images,
            captured_video_paths,
            delivery,
        ) = await _stream_agent_resume_to_discord(
            channel,
            config,
            approved,
            interrupt_ids=interrupt_ids,
        )
    except Exception as exc:
        log.error("Discord interrupt resume error: %s", exc)
        await channel.send(f"Error processing approval: {exc}")
        return

    if answer and not delivery.delivered:
        await _send_discord_safe_text(channel, answer)

    try:
        import discord
    except Exception:
        discord = None
    for img_bytes in captured_images:
        try:
            import io

            if discord is not None:
                f = discord.File(io.BytesIO(img_bytes), filename="image.png")
                await channel.send(file=f)
        except Exception as exc:
            log.warning("Failed to send Discord image: %s", exc)
    for vpath in captured_video_paths:
        try:
            if discord is not None:
                f = discord.File(vpath, filename="video.mp4")
                await channel.send(file=f)
        except Exception as exc:
            log.warning("Failed to send Discord video: %s", exc)

    config = pending.get("config", {})
    if new_interrupt:
        await _send_discord_interrupt_approval_card(channel, new_interrupt, config)
        return

    thread_id = ch_runtime.thread_id_from_config(config)
    if thread_id and answer:
        _goal_run_turn, _goal_send_text = _discord_goal_callbacks(channel)
        goal_result = await ch_runtime.continue_channel_goal_after_turn_async(
            channel_name="discord",
            thread_id=thread_id,
            config=config,
            assistant_text=answer,
            interrupt_data=None,
            run_turn=_goal_run_turn,
            send_text=_goal_send_text,
        )
        if goal_result.interrupt_data:
            await _send_discord_interrupt_approval_card(
                channel,
                goal_result.interrupt_data,
                config,
            )


async def _handle_discord_task_approval_button(channel, message, *, approved: bool) -> None:
    channel_id = int(getattr(channel, "id", 0))
    message_id = getattr(message, "id", None)
    with _pending_lock:
        info = (
            _pending_task_approvals.pop(int(message_id), None)
            if message_id is not None
            else None
        )
        if info is None:
            fallback_id = _task_approval_by_channel.get(channel_id)
            info = _pending_task_approvals.pop(fallback_id, None) if fallback_id else None
        if info:
            _task_approval_by_channel.pop(int(info.get("dm_channel_id") or channel_id), None)
    if info is None:
        try:
            await message.edit(content="This approval is no longer pending.", view=None)
        except Exception:
            pass
        return

    task_name = info.get("task_name", "Task")
    action = "Approved" if approved else "Denied"
    try:
        await message.edit(content=f"{action} - {task_name}", view=None)
    except Exception:
        log.debug("Failed to update Discord task approval message", exc_info=True)
    try:
        from row_bot.tasks import respond_to_approval

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: respond_to_approval(
                info["resume_token"],
                approved,
                note=f"{'Approved' if approved else 'Denied'} via Discord",
                source="discord",
            ),
        )
        if not result:
            await channel.send("Could not process approval - it may have expired.")
    except Exception as exc:
        log.error("Task approval error (button): %s", exc)
        await channel.send(f"Error processing approval: {exc}")


def _schedule_coro(coro):
    """Schedule a coroutine on the bot's event loop."""
    if _bot_loop and _bot_loop.is_running():
        return asyncio.run_coroutine_threadsafe(coro, _bot_loop)
    raise RuntimeError("Discord bot loop not running")


def send_outbound(channel_id: int, text: str) -> None:
    """Send a text message to a Discord channel/DM."""
    if not _running or _client is None:
        raise RuntimeError("Discord bot is not running")

    async def _send():
        channel = _client.get_channel(channel_id)
        if channel is None:
            # Try fetching DM channel
            try:
                user = await _client.fetch_user(channel_id)
                channel = await user.create_dm()
            except Exception:
                raise RuntimeError(f"Cannot find Discord channel {channel_id}")

        parts = _split_message(text)
        for part in parts:
            await channel.send(part)

    fut = _schedule_coro(_send())
    fut.result(timeout=30)


def _resolve_dm_channel_id(user_id: int) -> int:
    """Return the DM channel ID for a Discord user ID.

    Opens (or reuses) a DM channel and returns its integer ID so
    ``_pending_task_approvals`` keys match the ``channel_id`` seen
    in ``on_message``.
    """
    if not _running or _client is None:
        return user_id  # best-effort fallback

    async def _open():
        user = await _client.fetch_user(user_id)
        dm = await user.create_dm()
        return dm.id

    fut = _schedule_coro(_open())
    return fut.result(timeout=15)


def send_photo(channel_id: int, file_path: str,
               caption: str | None = None) -> None:
    """Send a photo as a file attachment in Discord."""
    if not _running or _client is None:
        raise RuntimeError("Discord bot is not running")

    async def _send():
        import discord
        channel = _client.get_channel(channel_id)
        if channel is None:
            user = await _client.fetch_user(channel_id)
            channel = await user.create_dm()

        f = discord.File(file_path, filename=Path(file_path).name)
        await channel.send(content=caption or "", file=f)

    fut = _schedule_coro(_send())
    fut.result(timeout=60)


def send_document(channel_id: int, file_path: str,
                  caption: str | None = None) -> None:
    """Send a document as a file attachment in Discord."""
    send_photo(channel_id, file_path, caption)  # Same API for Discord files


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


def _discord_stream_config() -> ChannelStreamConfig:
    try:
        from row_bot.channels import config as channel_config_store

        channel_config = channel_config_store.get_all("discord")
    except Exception:
        channel_config = {}
    enabled = bool(channel_config.get("streaming.enabled", True))
    return ChannelStreamConfig(
        channel="discord",
        transport_mode=str(channel_config.get("streaming.transport", "edit") if enabled else "off"),
        update_interval_s=_cfg_float(channel_config, "streaming.update_interval_s", 1.0),
        max_stale_interval_s=_cfg_float(channel_config, "streaming.max_stale_interval_s", 4.0),
        min_update_chars=_cfg_int(channel_config, "streaming.min_update_chars", 32),
        typing_interval_s=_cfg_float(channel_config, "streaming.typing_interval_s", 8.0),
        cursor=str(channel_config.get("streaming.cursor", "...")),
        max_message_units=DISCORD_MAX_LEN,
        fresh_final_after_s=_cfg_float(channel_config, "streaming.fresh_final_after_s", 60.0),
        sparse_progress=bool(channel_config.get("streaming.sparse_progress", True)),
    )


async def _stream_agent_turn_to_discord(
    channel,
    user_text: str,
    config: dict,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    loop = asyncio.get_event_loop()
    event_queue: queue.Queue = queue.Queue()
    stream_config = _discord_stream_config()
    consumer = ChannelStreamConsumer(DiscordStreamTransport(channel), stream_config)
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
            channel_name="discord",
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


async def _stream_agent_resume_to_discord(
    channel,
    config: dict,
    approved: bool,
    *,
    interrupt_ids: list[str] | None = None,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    loop = asyncio.get_event_loop()
    event_queue: queue.Queue = queue.Queue()
    stream_config = _discord_stream_config()
    consumer = ChannelStreamConsumer(DiscordStreamTransport(channel), stream_config)
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
            channel_name="discord",
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


async def _send_discord_safe_text(channel, text: str) -> None:
    for part in _split_message(str(text or "")):
        await channel.send(part)


def _discord_goal_callbacks(channel):
    state: dict[str, ChannelDeliveryResult | None] = {"delivery": None}

    async def _goal_run_turn(prompt: str, cfg: dict):
        answer, interrupt_data, captured_images, captured_video_paths, delivery = await _stream_agent_turn_to_discord(
            channel,
            prompt,
            cfg,
        )
        state["delivery"] = delivery
        try:
            import discord
        except Exception:
            discord = None
        for img_bytes in captured_images:
            try:
                import io
                if discord is not None:
                    f = discord.File(io.BytesIO(img_bytes), filename="image.png")
                    await channel.send(file=f)
            except Exception as exc:
                log.warning("Failed to send Discord image: %s", exc)
        for vpath in captured_video_paths:
            try:
                if discord is not None:
                    f = discord.File(vpath, filename="video.mp4")
                    await channel.send(file=f)
            except Exception as exc:
                log.warning("Failed to send Discord video: %s", exc)
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
        await _send_discord_safe_text(channel, message)

    return _goal_run_turn, _goal_send_text


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────
def _is_authorised(user_id: int) -> bool:
    """Check if a Discord user is authorised."""
    allowed = _get_allowed_user_id()
    if allowed is not None and user_id == allowed:
        return True
    return ch_auth.is_user_approved("discord", str(user_id))


# ──────────────────────────────────────────────────────────────────────
# Bot lifecycle
# ──────────────────────────────────────────────────────────────────────
async def start_bot() -> bool:
    """Start the Discord bot."""
    global _client, _running, _bot_loop, _bot_thread

    if _running:
        log.info("Discord bot already running")
        return True

    bot_token = _get_bot_token()
    if not bot_token:
        log.warning("Discord bot token not configured")
        return False

    try:
        import discord

        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.guild_messages = True
        intents.reactions = True

        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            global _running
            _running = True
            log.info("Discord bot ready as %s", client.user)

        @client.event
        async def on_message(message):
            # Ignore bot's own messages
            if message.author == client.user:
                return

            # Handle DMs
            if isinstance(message.channel, discord.DMChannel):
                await _process_message(message)
                return

            # Handle mentions in servers
            if client.user in message.mentions:
                await _process_message(message, is_mention=True)

        async def _process_message(message, is_mention: bool = False):
            user_id = message.author.id
            channel_id = message.channel.id
            text = message.content.strip()

            # Strip bot mention
            if is_mention and client.user:
                text = text.replace(f"<@{client.user.id}>", "").strip()

            if not text and not message.attachments:
                return

            # Auth check
            if not _is_authorised(user_id):
                if text and ch_auth.verify_pairing_code("discord", str(user_id), text):
                    await message.channel.send(
                        "✅ Paired successfully! You can now chat with Row-Bot."
                    )
                    return
                await message.channel.send(
                    "🔒 Not authorised. Send your pairing code to get started."
                )
                return

            from row_bot.channels.base import record_activity
            record_activity("discord")

            # Slash command dispatch
            if text:
                _cmd_thread_id = (
                    _get_or_create_thread(channel_id)
                    if ch_commands.is_thread_scoped_command(text)
                    else None
                )
                goal_start = ch_runtime.prepare_channel_goal_start(text, _cmd_thread_id)
                if goal_start is not None and _cmd_thread_id:
                    config = {"configurable": {"thread_id": _cmd_thread_id}}
                    _goal_run_turn, _goal_send_text = _discord_goal_callbacks(message.channel)

                    await _send_discord_safe_text(message.channel, ch_runtime.format_goal_started_ack(goal_start))
                    result = await ch_runtime.run_channel_goal_async(
                        channel_name="discord",
                        thread_id=_cmd_thread_id,
                        config=config,
                        first_prompt=goal_start.prompt,
                        run_turn=_goal_run_turn,
                        send_text=_goal_send_text,
                    )
                    if result.interrupt_data:
                        await _send_discord_interrupt_approval_card(
                            message.channel,
                            result.interrupt_data,
                            config,
                        )
                    return
                cmd_response = ch_commands.dispatch(
                    "discord",
                    text,
                    thread_id=_cmd_thread_id,
                )
                if cmd_response is not None:
                    await message.channel.send(cmd_response)
                    return

            # Handle attachments (images, files, audio)
            if message.attachments:
                await _handle_attachments(message, text)
                return

            # Check pending task approvals first (background workflows)
            from row_bot.channels import approval as approval_helpers
            with _pending_lock:
                _msg_key = _task_approval_by_channel.get(channel_id)
                task_approval = _pending_task_approvals.get(_msg_key) if _msg_key else None
            if task_approval:
                decision = approval_helpers.is_approval_text(text)
                if decision is None:
                    await message.channel.send(
                        "There's a pending approval - use the buttons, "
                        "or reply yes/no."
                    )
                    return
                if decision is not None:
                    with _pending_lock:
                        _pending_task_approvals.pop(_msg_key, None)
                        _task_approval_by_channel.pop(channel_id, None)
                    try:
                        from row_bot.tasks import respond_to_approval
                        result = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda: respond_to_approval(
                                task_approval["resume_token"], decision,
                                note=f"{'Approved' if decision else 'Denied'} via Discord",
                                source="discord",
                            ),
                        )
                        action = "✅ Approved" if decision else "❌ Denied"
                        if result:
                            await message.channel.send(
                                f"{action} — {task_approval['task_name']}")
                        else:
                            await message.channel.send(
                                "⚠️ Could not process approval — it may have expired.")
                    except Exception as exc:
                        log.error("Task approval error: %s", exc)
                        await message.channel.send(
                            f"⚠️ Error processing approval: {exc}")
                    return

            # Check pending live-chat interrupts
            interrupt = _peek_discord_interrupt(channel_id)

            if interrupt:
                decision = approval_helpers.is_approval_text(text)
                if decision is None:
                    await message.channel.send(
                        "There's a pending approval - please use the approval "
                        "buttons or reply yes/no first."
                    )
                    return
                interrupt = _pop_discord_interrupt(channel_id=channel_id)
                if interrupt is None:
                    await message.channel.send("This approval is no longer pending.")
                    return
                approved = bool(decision)
                config = interrupt.get("config", {})
                interrupt_ids = approval_helpers.extract_interrupt_ids(
                    interrupt.get("data")
                )
                ch_runtime.resolve_goal_approval_for_config(config, approved)

                answer, new_interrupt, captured, vid_paths, delivery = await _stream_agent_resume_to_discord(
                    message.channel,
                    config,
                    approved,
                    interrupt_ids=interrupt_ids,
                )
                if answer and not delivery.delivered:
                    await _send_discord_safe_text(message.channel, answer)
                for img_bytes in captured:
                    try:
                        import io
                        f = discord.File(io.BytesIO(img_bytes), filename="image.png")
                        await message.channel.send(file=f)
                    except Exception as exc:
                        log.warning("Failed to send Discord image: %s", exc)
                for vpath in vid_paths:
                    try:
                        f = discord.File(vpath, filename="video.mp4")
                        await message.channel.send(file=f)
                    except Exception as exc:
                        log.warning("Failed to send Discord video: %s", exc)
                if new_interrupt:
                    await _send_discord_interrupt_approval_card(
                        message.channel,
                        new_interrupt,
                        config,
                    )
                elif answer and not ch_runtime.channel_turn_is_suspended(answer):
                    thread_id = ch_runtime.thread_id_from_config(config)
                    if thread_id:
                        _goal_run_turn, _goal_send_text = _discord_goal_callbacks(message.channel)

                        goal_result = await ch_runtime.continue_channel_goal_after_turn_async(
                            channel_name="discord",
                            thread_id=thread_id,
                            config=config,
                            assistant_text=answer,
                            interrupt_data=None,
                            run_turn=_goal_run_turn,
                            send_text=_goal_send_text,
                        )
                        if goal_result.interrupt_data:
                            await _send_discord_interrupt_approval_card(
                                message.channel,
                                goal_result.interrupt_data,
                                config,
                            )
                return

            # Normal message
            thread_id = _get_or_create_thread(channel_id)
            config = {"configurable": {"thread_id": thread_id}}

            # Add reaction to show processing + typing indicator
            try:
                await message.add_reaction("👀")
            except Exception:
                pass
            try:
                await message.channel.trigger_typing()
            except Exception:
                pass

            try:
                answer, interrupt_data, captured_images, captured_video_paths, delivery = await _stream_agent_turn_to_discord(
                    message.channel,
                    text,
                    config,
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
                            transport="discord:repair",
                            final_text=answer,
                            error="repair retry used non-streaming fallback",
                        )
                    except Exception as retry_exc:
                        log.error("Retry after repair failed: %s", retry_exc)
                        _new_thread(channel_id)
                        try:
                            await message.remove_reaction("👀", client.user)
                            await message.add_reaction("💔")
                        except Exception:
                            pass
                        await message.channel.send(
                            "⚠️ The previous conversation had a stuck tool call "
                            "and couldn't be repaired.\n"
                            "🆕 I've started a fresh thread — please resend your message.")
                        return
                else:
                    try:
                        await message.remove_reaction("👀", client.user)
                        await message.add_reaction("💔")
                    except Exception:
                        pass
                    await message.channel.send(f"⚠️ Error: {exc}")
                    return

            # Update reaction
            try:
                await message.remove_reaction("👀", client.user)
                await message.add_reaction("👍")
            except Exception:
                pass

            # Extract YouTube URLs for separate messages (auto-preview)
            from row_bot.channels import extract_youtube_urls
            if ch_runtime.channel_turn_is_suspended(answer):
                return
            clean_answer, yt_urls = extract_youtube_urls(answer) if answer else ("", [])

            # If streaming/final delivery covered the response, skip re-sending.
            if delivery.delivered and not interrupt_data:
                for url in yt_urls:
                    await message.channel.send(url)
                for img_bytes in captured_images:
                    try:
                        import io
                        f = discord.File(io.BytesIO(img_bytes), filename="image.png")
                        await message.channel.send(file=f)
                    except Exception as exc:
                        log.warning("Failed to send Discord image: %s", exc)
                for vpath in captured_video_paths:
                    try:
                        f = discord.File(vpath, filename="video.mp4")
                        await message.channel.send(file=f)
                    except Exception as exc:
                        log.warning("Failed to send Discord video: %s", exc)
            else:
                if clean_answer:
                    for part in _split_message(clean_answer):
                        await message.channel.send(part)
                for url in yt_urls:
                    await message.channel.send(url)
                for img_bytes in captured_images:
                    try:
                        import io
                        f = discord.File(io.BytesIO(img_bytes), filename="image.png")
                        await message.channel.send(file=f)
                    except Exception as exc:
                        log.warning("Failed to send Discord image: %s", exc)
                for vpath in captured_video_paths:
                    try:
                        f = discord.File(vpath, filename="video.mp4")
                        await message.channel.send(file=f)
                    except Exception as exc:
                        log.warning("Failed to send Discord video: %s", exc)

            # Store interrupt
            if interrupt_data:
                await _send_discord_interrupt_approval_card(
                    message.channel,
                    interrupt_data,
                    config,
                )

        async def _handle_attachments(message, caption: str):
            """Process file attachments."""
            from row_bot.channels.media import (
                transcribe_audio, analyze_image, save_inbound_file,
                extract_document_text, copy_to_workspace,
            )

            channel_id = message.channel.id
            thread_id = _get_or_create_thread(channel_id)
            config = {"configurable": {"thread_id": thread_id}}

            for attachment in message.attachments:
                content_type = attachment.content_type or ""
                filename = attachment.filename

                try:
                    file_data = await attachment.read()
                except Exception as exc:
                    log.warning("Failed to download Discord attachment %s: %s",
                                 filename, exc)
                    continue

                if content_type.startswith("audio/"):
                    ext = Path(filename).suffix or ".ogg"
                    transcript = transcribe_audio(file_data, ext)
                    if transcript:
                        user_text = f"[Voice message transcription]: {transcript}"
                        if caption:
                            user_text += f"\n\nCaption: {caption}"
                        answer, _, _ = await asyncio.get_event_loop().run_in_executor(
                            None, _run_agent_sync, user_text, config
                        )
                        if answer and not ch_runtime.channel_turn_is_suspended(answer):
                            for part in _split_message(answer):
                                await message.channel.send(part)

                elif content_type.startswith("image/"):
                    analysis = analyze_image(file_data,
                                              caption or "Describe this image")
                    if analysis:
                        user_text = f"[Image analysis]: {analysis}"
                        if caption:
                            user_text += f"\n\nUser said: {caption}"
                        answer, _, _ = await asyncio.get_event_loop().run_in_executor(
                            None, _run_agent_sync, user_text, config
                        )
                        if answer and not ch_runtime.channel_turn_is_suspended(answer):
                            for part in _split_message(answer):
                                await message.channel.send(part)

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
                    answer, _, _ = await asyncio.get_event_loop().run_in_executor(
                        None, _run_agent_sync, user_text, config
                    )
                    if answer and not ch_runtime.channel_turn_is_suspended(answer):
                        for part in _split_message(answer):
                            await message.channel.send(part)

        # Run bot in background thread
        _client = client

        def _run():
            global _bot_loop
            loop = asyncio.new_event_loop()
            _bot_loop = loop
            asyncio.set_event_loop(loop)
            try:
                from row_bot.stability import install_asyncio_exception_handler

                install_asyncio_exception_handler(loop)
            except Exception:
                pass
            try:
                loop.run_until_complete(client.start(bot_token))
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.error("Discord bot error: %s", exc)
            finally:
                try:
                    if not client.is_closed():
                        loop.run_until_complete(client.close())
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception as exc:
                    log.debug("Discord bot loop cleanup failed: %s", exc, exc_info=True)
                loop.close()

        _bot_thread = threading.Thread(target=_run, daemon=True,
                                        name="discord-bot")
        _bot_thread.start()

        # Wait briefly for connection without blocking the app event loop.
        for _ in range(30):
            if _running:
                return True
            await asyncio.sleep(0.5)

        if not _running:
            log.warning("Discord bot did not connect within 15s")
            return False

        return True

    except ImportError:
        log.error("discord.py not installed. Run: pip install discord.py")
        return False
    except Exception as exc:
        log.error("Failed to start Discord bot: %s", exc)
        _running = False
        return False


async def stop_bot() -> None:
    """Stop the Discord bot gracefully."""
    global _client, _running, _bot_loop, _bot_thread

    if not _running:
        return

    try:
        if _client and _bot_loop and _bot_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(_client.close(), _bot_loop)
            fut.result(timeout=10)
    except Exception as exc:
        log.warning("Error stopping Discord bot: %s", exc)

    _client = None
    _running = False
    _bot_loop = None
    _bot_thread = None
    log.info("Discord bot stopped")


# ──────────────────────────────────────────────────────────────────────
# Outbound helpers (used by tool_factory)
# ──────────────────────────────────────────────────────────────────────
_send_photo_fn = send_photo
_send_document_fn = send_document


class DiscordChannel(Channel):
    """Discord channel adapter implementing the Channel ABC.

    Uses discord.py with Gateway Intents — WebSocket-based.
    """

    @property
    def name(self) -> str:
        return "discord"

    @property
    def display_name(self) -> str:
        return "Discord"

    @property
    def icon(self) -> str:
        return "forum"  # Discord-like icon

    @property
    def setup_guide(self) -> str:
        return (
            "### Quick Setup\n"
            "1. Go to [discord.com/developers](https://discord.com/developers/applications) "
            "→ **New Application**\n"
            "2. Under **Bot**, click **Add Bot** → copy the **Bot Token**\n"
            "3. Enable **Privileged Gateway Intents**: `MESSAGE CONTENT`\n"
            "4. **OAuth2 → URL Generator** → scopes: `bot` → "
            "permissions: Send Messages, Attach Files, "
            "Read Message History, Add Reactions\n"
            "5. Use the URL to invite the bot to your server\n"
            "6. Paste the **Bot Token** below and click **Save**\n"
            "7. Click **▶️ Start Bot**"
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
            slash_commands=True,
        )

    @property
    def config_fields(self) -> list[ConfigField]:
        return [
            ConfigField(
                key="bot_token",
                label="Bot Token",
                field_type="password",
                storage="env",
                env_key="DISCORD_BOT_TOKEN",
                help_text="Bot token from Discord Developer Portal",
            ),
            ConfigField(
                key="user_id",
                label="Your Discord User ID (optional)",
                field_type="text",
                storage="env",
                env_key="DISCORD_USER_ID",
                help_text="Your Discord user ID for auto-auth (or use DM pairing)",
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
        if uid is not None:
            return uid
        approved = ch_auth.get_approved_users("discord")
        if approved:
            return int(approved[0])
        raise RuntimeError(
            "No Discord user configured — set DISCORD_USER_ID or pair via DM"
        )

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
        """Send a task-approval prompt and store pending state.

        *target* is typically a user ID (from ``get_default_target``).
        We resolve the DM channel ID so the pending lookup matches
        the ``channel_id`` in ``on_message``.
        """
        approval_kind = str(config.get("approval_kind") or "task")
        subject_label = "Agent" if approval_kind == "agent_run" else "Task"
        task_name = config.get("task_name", "Unknown task")
        message = config.get("message", "Approval required to continue.")
        resume_token = config.get("resume_token", "")
        rich_text = (
            "Approval Required\n"
            f"**{subject_label}:** {task_name}\n"
            f"{message}\n\n"
            "Reply **yes** or **no**."
        )
        try:
            if not _running or _client is None:
                raise RuntimeError("Discord bot is not running")

            async def _send():
                user = await _client.fetch_user(int(target))
                channel = await user.create_dm()
                view = _make_discord_task_approval_view()
                kwargs = {"view": view} if view is not None else {}
                sent = await channel.send(rich_text, **kwargs)
                return int(channel.id), int(getattr(sent, "id", channel.id))

            dm_channel_id, msg_id = _schedule_coro(_send()).result(timeout=30)
            import time as _time
            with _pending_lock:
                _pending_task_approvals[msg_id] = {
                    "resume_token": resume_token,
                    "task_name": task_name,
                    "dm_channel_id": dm_channel_id,
                    "_ts": _time.time(),
                }
                _task_approval_by_channel[dm_channel_id] = msg_id
            return str(msg_id)
        except Exception as exc:
            log.warning("Failed to send Discord approval: %s", exc)
            return None
        text = (
            f"⚠️ **Approval Required**\n"
            f"**{subject_label}:** {task_name}\n"
            f"{message}\n\n"
            "Reply **yes** or **no**."
        )
        try:
            dm_channel_id = _resolve_dm_channel_id(int(target))
            send_outbound(dm_channel_id, text)
            import time as _time
            with _pending_lock:
                _pending_task_approvals[dm_channel_id] = {
                    "resume_token": resume_token,
                    "task_name": task_name,
                    "_ts": _time.time(),
                }
            return str(dm_channel_id)
        except Exception as exc:
            log.warning("Failed to send Discord approval: %s", exc)
            return None

    def update_approval_message(self, message_ref: str,
                                status: str,
                                source: str = "") -> None:
        """Clear pending task approval and notify that it was resolved elsewhere."""
        try:
            ref_int = int(message_ref)
        except (ValueError, TypeError):
            return
        with _pending_lock:
            info = _pending_task_approvals.pop(ref_int, None)
            if info:
                _task_approval_by_channel.pop(int(info.get("dm_channel_id", ref_int)), None)
        action = "Approved" if status == "approved" else "Denied"
        target = int(info.get("dm_channel_id", ref_int)) if info else ref_int
        try:
            send_outbound(target,
                          f"{action} (via {source})" if source else action)
        except Exception:
            pass
        return
        with _pending_lock:
            _pending_task_approvals.pop(ref_int, None)
        action = "Approved ✅" if status == "approved" else "Denied ❌"
        try:
            send_outbound(ref_int,
                          f"{action} (via {source})" if source else action)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────
# Register with channel registry
# ──────────────────────────────────────────────────────────────────────
_discord_channel = DiscordChannel()

try:
    from row_bot.channels import registry as _ch_registry
    _ch_registry.register(_discord_channel)
except Exception:
    pass
