"""
Thoth – Discord Channel Adapter
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
from threads import _save_thread_meta

log = logging.getLogger("thoth.discord")

# ──────────────────────────────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────────────────────────────
_client = None                          # discord.Client
_running = False
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_thread: threading.Thread | None = None
_pending_interrupts: dict[int, dict] = {}   # channel_id → interrupt_data
_pending_task_approvals: dict[int, dict] = {}  # channel_id → {resume_token, task_name, _ts}
_pending_lock = threading.Lock()

DISCORD_MAX_LEN = 2000  # Discord message character limit


# ──────────────────────────────────────────────────────────────────────
# Helpers — credentials
# ──────────────────────────────────────────────────────────────────────
def _get_bot_token() -> str:
    return os.environ.get("DISCORD_BOT_TOKEN", "")


def _get_allowed_user_id() -> int | None:
    raw = os.environ.get("DISCORD_USER_ID", "")
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
    _save_thread_meta(thread_id, "🎮 Discord conversation")  # creates or bumps updated_at
    return thread_id


def _new_thread(channel_id: int | str) -> str:
    import time
    suffix = str(int(time.time()))
    thread_id = f"discord_{channel_id}_{suffix}"
    _save_thread_meta(thread_id, "Discord conversation")
    return thread_id


# ──────────────────────────────────────────────────────────────────────
# Agent execution
# ──────────────────────────────────────────────────────────────────────
def _run_agent_sync(user_text: str, config: dict,
                    event_queue=None) -> tuple[str, dict | None, list[bytes]]:
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
                        "✅ Paired successfully! You can now chat with Thoth."
                    )
                    return
                await message.channel.send(
                    "🔒 Not authorised. Send your pairing code to get started."
                )
                return

            from channels.base import record_activity
            record_activity("discord")

            # Slash command dispatch
            if text:
                cmd_response = ch_commands.dispatch("discord", text)
                if cmd_response is not None:
                    await message.channel.send(cmd_response)
                    return

            # Handle attachments (images, files, audio)
            if message.attachments:
                await _handle_attachments(message, text)
                return

            # Check pending task approvals first (background workflows)
            from channels import approval as approval_helpers
            with _pending_lock:
                task_approval = _pending_task_approvals.get(channel_id)
            if task_approval:
                decision = approval_helpers.is_approval_text(text)
                if decision is not None:
                    with _pending_lock:
                        _pending_task_approvals.pop(channel_id, None)
                    try:
                        from tasks import respond_to_approval
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
            with _pending_lock:
                interrupt = _pending_interrupts.pop(channel_id, None)

            if interrupt:
                approved = text.lower() in approval_helpers._YES_WORDS
                config = interrupt.get("config", {})
                interrupt_ids = approval_helpers.extract_interrupt_ids(
                    interrupt.get("data")
                )

                answer, new_interrupt, captured, vid_paths = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: _resume_agent_sync(
                        config, approved, interrupt_ids=interrupt_ids
                    ),
                )
                if answer:
                    for part in _split_message(answer):
                        await message.channel.send(part)
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
                    with _pending_lock:
                        _pending_interrupts[channel_id] = {
                            "data": new_interrupt, "config": config
                        }
                    detail = approval_helpers.format_interrupt_text(new_interrupt)
                    await message.channel.send(
                        detail + "\n\nReply **yes** or **no**.")
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

            loop = asyncio.get_event_loop()

            # Send placeholder and start streaming
            try:
                sent_msg = await message.channel.send("⏳")
            except Exception:
                sent_msg = None

            eq = queue.Queue()

            try:
                executor_future = loop.run_in_executor(
                    None, _run_agent_sync, text, config, eq
                )
                if sent_msg:
                    consumer_task = asyncio.ensure_future(
                        _discord_edit_consumer(sent_msg, eq, loop)
                    )
                answer, interrupt_data, captured_images, captured_video_paths = await executor_future
                if sent_msg:
                    streamed_display = await consumer_task
                else:
                    streamed_display = None
            except Exception as exc:
                try:
                    while True:
                        eq.get_nowait()
                except Exception:
                    pass
                log.error("Agent error for channel %s: %s", channel_id, exc)
                from channels.thread_repair import is_corrupt_thread_error
                if is_corrupt_thread_error(exc):
                    try:
                        from agent import repair_orphaned_tool_calls
                        await loop.run_in_executor(
                            None, repair_orphaned_tool_calls, None, config
                        )
                        log.info("Repaired orphaned tool calls for %s, retrying", channel_id)
                        answer, interrupt_data, captured_images = await loop.run_in_executor(
                            None, _run_agent_sync, text, config
                        )
                        streamed_display = None
                    except Exception as retry_exc:
                        log.error("Retry after repair failed: %s", retry_exc)
                        _new_thread(channel_id)
                        try:
                            await message.remove_reaction("👀", client.user)
                            await message.add_reaction("💔")
                        except Exception:
                            pass
                        if sent_msg:
                            try:
                                await sent_msg.edit(
                                    content="⚠️ The previous conversation had a stuck tool call "
                                    "and couldn't be repaired.\n"
                                    "🆕 I've started a fresh thread — please resend your message.")
                            except Exception:
                                pass
                        else:
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
                    if sent_msg:
                        try:
                            await sent_msg.edit(content=f"⚠️ Error: {exc}")
                        except Exception:
                            pass
                    else:
                        await message.channel.send(f"⚠️ Error: {exc}")
                    return

            # Update reaction
            try:
                await message.remove_reaction("👀", client.user)
                await message.add_reaction("👍")
            except Exception:
                pass

            # Extract YouTube URLs for separate messages (auto-preview)
            from channels import extract_youtube_urls
            clean_answer, yt_urls = extract_youtube_urls(answer) if answer else ("", [])

            # If streaming covered the full response, skip re-sending
            if streamed_display and not interrupt_data:
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
                # Streaming overflowed or wasn't available — delete placeholder, send normally
                if sent_msg:
                    try:
                        await sent_msg.delete()
                    except Exception:
                        pass
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
                with _pending_lock:
                    _pending_interrupts[channel_id] = {
                        "data": interrupt_data, "config": config
                    }
                from channels import approval as approval_helpers
                detail = approval_helpers.format_interrupt_text(interrupt_data)
                await message.channel.send(
                    detail + "\n\nReply **yes** or **no**.")

        async def _handle_attachments(message, caption: str):
            """Process file attachments."""
            from channels.media import (
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
                        if answer:
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
                        if answer:
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
                    if answer:
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
                loop.run_until_complete(client.start(bot_token))
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.error("Discord bot error: %s", exc)
            finally:
                loop.close()

        _bot_thread = threading.Thread(target=_run, daemon=True,
                                        name="discord-bot")
        _bot_thread.start()

        # Wait briefly for connection
        import time
        for _ in range(30):
            if _running:
                return True
            time.sleep(0.5)

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
            buttons=False,       # Could add discord.ui.View later
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
        task_name = config.get("task_name", "Unknown task")
        message = config.get("message", "Approval required to continue.")
        resume_token = config.get("resume_token", "")
        text = (
            f"⚠️ **Approval Required**\n"
            f"**Task:** {task_name}\n"
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
    from channels import registry as _ch_registry
    _ch_registry.register(_discord_channel)
except Exception:
    pass
