"""Internal runtime bridge for public plugin-owned channels.

Plugin code imports only ``plugins.api``. This module is Row-Bot core and may
adapt those public objects to the existing channel, agent, approval, media, and
Goal Mode internals.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import pathlib
import queue
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from row_bot.channels.base import Channel
from row_bot.channels.streaming import ChannelStreamConfig, ChannelStreamConsumer
from row_bot.plugins.api import (
    ChannelAttachment,
    ChannelAttachmentResult,
    ChannelInboundMessage,
    ChannelOutboundCallbacks,
    ChannelRunResult,
)

log = logging.getLogger("row_bot.plugins.channel_runtime")

_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
_SAFE_THREAD_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


@dataclass
class _AgentTurn:
    answer: str = ""
    interrupt_data: Any | None = None
    generated_files: list[str] = field(default_factory=list)
    delivered_final: bool = False


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _send_text(callbacks: ChannelOutboundCallbacks, text: str) -> None:
    if str(text or "").strip():
        await _maybe_await(callbacks.send_text(str(text)))


class _PluginCallbackStreamTransport:
    def __init__(self, callbacks: ChannelOutboundCallbacks, *, stream_active: bool) -> None:
        self.callbacks = callbacks
        self.stream_active = stream_active
        self.transport_name = "plugin:callbacks" if stream_active else "plugin:text"

    async def send_typing(self) -> None:
        if self.callbacks.send_typing:
            await _maybe_await(self.callbacks.send_typing())

    async def start(self, text: str) -> Any:
        if not self.stream_active or not self.callbacks.start_stream:
            raise RuntimeError("Plugin stream callbacks are unavailable")
        return await _maybe_await(self.callbacks.start_stream(text))

    async def update(self, handle: Any, text: str, *, final: bool = False) -> Any:
        if final and self.callbacks.finish_stream:
            return await _maybe_await(self.callbacks.finish_stream(handle, text))
        if not self.callbacks.update_stream:
            raise RuntimeError("Plugin update_stream callback is unavailable")
        return await _maybe_await(self.callbacks.update_stream(handle, text))

    async def send_final(self, text: str) -> list[Any]:
        await _send_text(self.callbacks, text)
        return ["send_text"]

    async def cleanup_preview(self, handle: Any) -> None:
        return None

    def split_text(self, text: str) -> list[str]:
        return [str(text)] if str(text or "") else []


async def handle_plugin_channel_message(
    *,
    plugin_id: str,
    message: ChannelInboundMessage,
    callbacks: ChannelOutboundCallbacks,
    channel: Channel | None,
    enabled_tool_names: list[str] | None,
    stream: bool | None,
    approval_context: dict[str, Any] | None = None,
) -> ChannelRunResult:
    """Handle one inbound plugin-channel message as a Row-Bot channel turn."""

    thread_id = _resolve_thread_id(message, channel)
    generated_files: list[str] = []
    try:
        from row_bot.channels.base import record_activity

        record_activity(message.channel_name)
        _ensure_thread_meta(thread_id, message)
        config = _runtime_config(
            thread_id=thread_id,
            channel_name=message.channel_name,
            plugin_id=plugin_id,
            purpose="message",
            stream=_stream_enabled(stream, channel, callbacks),
            approval_context=approval_context,
        )

        prompt = _prompt_from_message(message)

        from row_bot.channels import commands as channel_commands
        from row_bot.channels import runtime as channel_runtime

        goal_start = channel_runtime.prepare_channel_goal_start(message.text, thread_id)
        if goal_start is not None:
            await _send_text(callbacks, channel_runtime.format_goal_started_ack(goal_start))
            goal_result = await _run_goal_loop(
                channel_name=message.channel_name,
                thread_id=thread_id,
                config=config,
                first_prompt=goal_start.prompt,
                callbacks=callbacks,
                enabled_tool_names=enabled_tool_names,
                generated_files=generated_files,
            )
            if goal_result.interrupt_data:
                await _send_approval_request(callbacks, goal_result.interrupt_data, config)
            return ChannelRunResult(
                thread_id=thread_id,
                handled=True,
                command=True,
                interrupted=bool(goal_result.interrupt_data),
                interrupt_data=goal_result.interrupt_data,
                generated_files=generated_files,
            )

        command_response = channel_commands.dispatch(
            message.channel_name,
            message.text,
            thread_id=thread_id if channel_commands.is_thread_scoped_command(message.text) else None,
            enabled_tool_names=enabled_tool_names,
        )
        if command_response is not None:
            await _send_text(callbacks, command_response)
            return ChannelRunResult(
                thread_id=thread_id,
                answer=command_response,
                handled=True,
                command=True,
            )

        if not prompt.strip():
            return ChannelRunResult(thread_id=thread_id, handled=False)

        turn = await _collect_agent_turn(
            prompt,
            config,
            callbacks,
            enabled_tool_names=enabled_tool_names,
            use_stream=bool((config.get("configurable") or {}).get("channel_streaming")),
            deliver_final=True,
        )
        generated_files.extend(turn.generated_files)
        await _deliver_generated_files(turn.generated_files, callbacks)
        from row_bot.channels import runtime as channel_runtime

        if channel_runtime.channel_turn_is_suspended(turn.answer):
            return ChannelRunResult(
                thread_id=thread_id,
                answer="",
                handled=True,
                generated_files=generated_files,
            )
        if turn.interrupt_data:
            await _send_approval_request(callbacks, turn.interrupt_data, config)
            return ChannelRunResult(
                thread_id=thread_id,
                answer=turn.answer,
                handled=True,
                interrupted=True,
                interrupt_data=turn.interrupt_data,
                generated_files=generated_files,
            )

        if turn.answer:
            goal_result = await _continue_goal_after_turn(
                channel_name=message.channel_name,
                thread_id=thread_id,
                config=config,
                assistant_text=turn.answer,
                callbacks=callbacks,
                enabled_tool_names=enabled_tool_names,
                generated_files=generated_files,
            )
            if goal_result.interrupt_data:
                await _send_approval_request(callbacks, goal_result.interrupt_data, config)
                return ChannelRunResult(
                    thread_id=thread_id,
                    answer=turn.answer,
                    handled=True,
                    interrupted=True,
                    interrupt_data=goal_result.interrupt_data,
                    generated_files=generated_files,
                )

        return ChannelRunResult(
            thread_id=thread_id,
            answer=turn.answer,
            handled=True,
            generated_files=generated_files,
        )
    except Exception as exc:
        log.warning(
            "Plugin channel message failed for %s/%s: %s",
            plugin_id,
            message.channel_name,
            exc,
            exc_info=True,
        )
        return ChannelRunResult(thread_id=thread_id, handled=True, error=str(exc))


async def handle_plugin_channel_approval(
    *,
    plugin_id: str,
    channel_name: str,
    thread_id: str,
    approved: bool,
    callbacks: ChannelOutboundCallbacks,
    interrupt_ids: list[str] | None = None,
    source: str = "",
) -> ChannelRunResult:
    """Resume an interrupted plugin-channel turn after approval or denial."""

    generated_files: list[str] = []
    try:
        from row_bot.channels import runtime as channel_runtime

        config = _runtime_config(
            thread_id=_safe_thread_id(thread_id),
            channel_name=channel_name,
            plugin_id=plugin_id,
            purpose="approval",
            stream=False,
            approval_context={"source": source},
        )
        channel_runtime.resolve_goal_approval_for_config(config, approved)

        turn = await _collect_agent_turn(
            "",
            config,
            callbacks,
            enabled_tool_names=None,
            use_stream=_stream_enabled(None, None, callbacks),
            deliver_final=True,
            resume_approved=approved,
            interrupt_ids=interrupt_ids,
        )
        generated_files.extend(turn.generated_files)
        await _deliver_generated_files(turn.generated_files, callbacks)

        if turn.interrupt_data:
            await _send_approval_request(callbacks, turn.interrupt_data, config)
            return ChannelRunResult(
                thread_id=_safe_thread_id(thread_id),
                answer=turn.answer,
                handled=True,
                interrupted=True,
                interrupt_data=turn.interrupt_data,
                generated_files=generated_files,
            )

        if turn.answer:
            goal_result = await _continue_goal_after_turn(
                channel_name=channel_name,
                thread_id=_safe_thread_id(thread_id),
                config=config,
                assistant_text=turn.answer,
                callbacks=callbacks,
                enabled_tool_names=None,
                generated_files=generated_files,
            )
            if goal_result.interrupt_data:
                await _send_approval_request(callbacks, goal_result.interrupt_data, config)
                return ChannelRunResult(
                    thread_id=_safe_thread_id(thread_id),
                    answer=turn.answer,
                    handled=True,
                    interrupted=True,
                    interrupt_data=goal_result.interrupt_data,
                    generated_files=generated_files,
                )

        return ChannelRunResult(
            thread_id=_safe_thread_id(thread_id),
            answer=turn.answer,
            handled=True,
            generated_files=generated_files,
        )
    except Exception as exc:
        log.warning(
            "Plugin channel approval failed for %s/%s: %s",
            plugin_id,
            channel_name,
            exc,
            exc_info=True,
        )
        return ChannelRunResult(thread_id=_safe_thread_id(thread_id), handled=True, error=str(exc))


def process_plugin_channel_attachment(
    attachment: ChannelAttachment,
    *,
    question: str = "",
    max_chars: int = 80000,
) -> ChannelAttachmentResult:
    """Process an inbound attachment with the shared channel media helpers."""

    filename = pathlib.Path(str(attachment.filename or "attachment")).name or "attachment"
    content_type = str(attachment.content_type or "").lower()
    kind = str(attachment.kind or "").lower() or "file"
    try:
        data = _attachment_bytes(attachment)
        if len(data) > _MAX_ATTACHMENT_BYTES:
            return ChannelAttachmentResult(
                content_type=content_type,
                kind=kind,
                error="Attachment exceeds Row-Bot's core size limit.",
            )
        from row_bot.channels import media

        if _is_audio(kind, content_type, filename):
            text = media.transcribe_audio(data, file_ext=pathlib.Path(filename).suffix or ".bin")
            if not text:
                return ChannelAttachmentResult(
                    content_type=content_type,
                    kind="audio",
                    error="Audio transcription returned no text.",
                )
            return ChannelAttachmentResult(
                prompt_text=f"Audio attachment '{filename}' transcription:\n{text}",
                content_type=content_type,
                kind="audio",
            )

        if _is_image(kind, content_type, filename):
            prompt = question or attachment.caption or "Describe this image in detail."
            text = media.analyze_image(data, prompt)
            if not text:
                return ChannelAttachmentResult(
                    content_type=content_type,
                    kind="image",
                    error="Image analysis returned no text.",
                )
            return ChannelAttachmentResult(
                prompt_text=f"Image attachment '{filename}' analysis:\n{text}",
                content_type=content_type,
                kind="image",
            )

        saved_path = media.save_inbound_file(data, filename)
        extracted = media.extract_document_text(data, filename, max_chars=max_chars)
        workspace_path = media.copy_to_workspace(saved_path, filename) or ""
        prompt_parts = [f"File attachment '{filename}' was received."]
        if workspace_path:
            prompt_parts.append(f"Workspace path: {workspace_path}")
        if extracted:
            prompt_parts.append(f"Extracted content:\n{extracted}")
        return ChannelAttachmentResult(
            prompt_text="\n".join(prompt_parts),
            saved_path=str(saved_path),
            workspace_path=workspace_path,
            content_type=content_type,
            kind="file",
        )
    except Exception as exc:
        return ChannelAttachmentResult(
            content_type=content_type,
            kind=kind,
            error=str(exc),
        )


def _attachment_bytes(attachment: ChannelAttachment) -> bytes:
    if attachment.data is not None:
        return bytes(attachment.data)
    if attachment.local_path:
        path = pathlib.Path(attachment.local_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Attachment file not found: {path}")
        size_hint = int(attachment.size_bytes or 0)
        if size_hint > _MAX_ATTACHMENT_BYTES:
            raise ValueError("Attachment exceeds Row-Bot's core size limit.")
        return path.read_bytes()
    if attachment.url:
        raise ValueError("URL-only attachments are not fetched by the public plugin API.")
    raise ValueError("Attachment has no data or local_path.")


def _is_image(kind: str, content_type: str, filename: str) -> bool:
    return (
        kind == "image"
        or content_type.startswith("image/")
        or pathlib.Path(filename).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
    )


def _is_audio(kind: str, content_type: str, filename: str) -> bool:
    return (
        kind == "audio"
        or content_type.startswith("audio/")
        or pathlib.Path(filename).suffix.lower() in {".mp3", ".wav", ".ogg", ".m4a", ".webm", ".flac"}
    )


def _prompt_from_message(message: ChannelInboundMessage) -> str:
    parts: list[str] = []
    for attachment in message.attachments:
        result = process_plugin_channel_attachment(attachment, question=message.text)
        if result.prompt_text:
            parts.append(result.prompt_text)
        elif result.error:
            filename = pathlib.Path(str(attachment.filename or "attachment")).name
            parts.append(f"Attachment '{filename}' could not be processed: {result.error}")
    if message.text:
        parts.append(message.text)
    return "\n\n".join(part for part in parts if str(part).strip())


def _resolve_thread_id(message: ChannelInboundMessage, channel: Channel | None) -> str:
    external_id = (
        message.external_conversation_id
        or message.platform_thread_id
        or message.sender_id
        or "conversation"
    )
    if channel is not None:
        try:
            return _safe_thread_id(channel.make_thread_id(str(external_id)))
        except Exception:
            log.debug("Plugin channel make_thread_id failed", exc_info=True)
    return _safe_thread_id(f"{message.channel_name}_{external_id}")


def _safe_thread_id(value: str) -> str:
    safe = _SAFE_THREAD_RE.sub("_", str(value or "").strip())
    safe = safe.strip("._:-")
    return safe or "plugin_channel_thread"


def _ensure_thread_meta(thread_id: str, message: ChannelInboundMessage) -> None:
    try:
        from row_bot.threads import _save_thread_meta

        channel_name = str(message.channel_name or "Plugin channel").replace("_", " ").title()
        sender = str(message.sender_display_name or message.sender_id or "").strip()
        title = f"{channel_name} - {sender}" if sender else f"{channel_name} conversation"
        _save_thread_meta(thread_id, title, seed_default_skills=True)
        target = (
            message.external_conversation_id
            or message.platform_thread_id
            or message.sender_id
            or thread_id
        )
        if target:
            from row_bot.tasks import record_thread_channel_ref

            record_thread_channel_ref(
                thread_id,
                channel=str(message.channel_name or ""),
                target=str(target),
                external_conversation_id=str(target),
            )
    except Exception:
        log.debug("Plugin channel thread metadata update skipped", exc_info=True)


def _runtime_config(
    *,
    thread_id: str,
    channel_name: str,
    plugin_id: str,
    purpose: str,
    stream: bool,
    approval_context: dict[str, Any] | None,
) -> dict:
    from row_bot.channels.runtime import approval_mode_for_config

    runtime_surface = "approval" if purpose == "approval" else "channel"
    runtime_mode = "agent" if purpose == "approval" else "auto"
    config = {
        "configurable": {
            "thread_id": thread_id,
            "runtime_surface": runtime_surface,
            "runtime_mode": runtime_mode,
            "runtime_channel": channel_name,
            "plugin_id": plugin_id,
            "channel_streaming": bool(stream and purpose != "approval"),
        }
    }
    if approval_context:
        config["configurable"]["approval_context"] = dict(approval_context)
    config["configurable"]["approval_mode"] = approval_mode_for_config(config)
    return config


def _stream_enabled(
    stream: bool | None,
    channel: Channel | None,
    callbacks: ChannelOutboundCallbacks,
) -> bool:
    if stream is not None:
        return bool(stream)
    if channel is not None:
        try:
            return bool(channel.capabilities.streaming)
        except Exception:
            pass
    return bool(callbacks.start_stream and callbacks.update_stream)


async def _collect_agent_turn(
    prompt: str,
    config: dict,
    callbacks: ChannelOutboundCallbacks,
    *,
    enabled_tool_names: list[str] | None,
    use_stream: bool,
    deliver_final: bool,
    resume_approved: bool | None = None,
    interrupt_ids: list[str] | None = None,
) -> _AgentTurn:
    import row_bot.agent as agent_mod
    from row_bot.channels import runtime as channel_runtime

    enabled = _enabled_tools(enabled_tool_names)
    config = channel_runtime.prepare_channel_turn_config(config, prompt)

    if resume_approved is None:
        def events_factory():
            return agent_mod.stream_agent(prompt, enabled, config)
    else:
        def events_factory():
            return agent_mod.resume_stream_agent(
                enabled,
                config,
                bool(resume_approved),
                interrupt_ids=interrupt_ids,
            )

    orchestration_capable = bool(
        deliver_final
        and resume_approved is None
        and "agents" in enabled
    )
    turn = await _consume_stream_events(
        events_factory,
        callbacks,
        use_stream=use_stream,
        # Buffer Agent-capable turns so the shared completion guard can classify
        # this parent pass before its real model-authored output is delivered.
        deliver_final=deliver_final and not orchestration_capable,
    )
    if orchestration_capable:
        await _send_text(callbacks, turn.answer)
        turn.delivered_final = True
    return turn


def _enabled_tools(enabled_tool_names: list[str] | None) -> list[str]:
    if enabled_tool_names is not None:
        return [str(name) for name in enabled_tool_names]
    try:
        from row_bot.tools import registry as tool_registry

        return [tool.name for tool in tool_registry.get_enabled_tools()]
    except Exception:
        log.debug("Could not inspect enabled tools for plugin channel", exc_info=True)
        return []


async def _consume_stream_events(
    events_factory: Callable[[], Iterable[tuple[str, Any]]],
    callbacks: ChannelOutboundCallbacks,
    *,
    use_stream: bool,
    deliver_final: bool,
    update_interval_seconds: float = 1.5,
    min_update_chars: int = 40,
) -> _AgentTurn:
    from row_bot.channels.agent_output import assemble_agent_answer

    stream_active = bool(use_stream and callbacks.start_stream and callbacks.update_stream)
    used_vision = False
    used_image_gen = False
    used_video_gen = False

    def _track_generated_media(event_type: str, payload: Any) -> None:
        nonlocal used_vision, used_image_gen, used_video_gen
        if event_type == "tool_done":
            name, raw_name = _tool_names(payload)
            if name in {"analyze_image", "Vision"}:
                used_vision = True
            if raw_name in {"generate_image", "edit_image"}:
                used_image_gen = True
            if raw_name in {"generate_video", "animate_image"}:
                used_video_gen = True

    if deliver_final:
        transport = _PluginCallbackStreamTransport(callbacks, stream_active=stream_active)
        consumer = ChannelStreamConsumer(
            transport,
            ChannelStreamConfig(
                channel="plugin",
                transport_mode="edit" if stream_active else "off",
                update_interval_s=update_interval_seconds,
                min_update_chars=min_update_chars,
                typing_interval_s=None,
                cursor="...",
                max_message_units=100_000,
                fresh_final_after_s=None,
                sparse_progress=True,
            ),
        )

        async def _tracked_events():
            async for event_type, payload in _iter_events_async(events_factory):
                _track_generated_media(event_type, payload)
                yield event_type, payload

        delivery = await consumer.consume_events(_tracked_events())
        answer = delivery.final_text.strip()
        interrupt_data = consumer.interrupt_data
        delivered_final = delivery.delivered
    else:
        answer_tokens: list[str] = []
        tool_reports: list[str] = []
        interrupt_data = None
        delivered_final = False
        async for event_type, payload in _iter_events_async(events_factory):
            _track_generated_media(event_type, payload)
            if event_type == "token":
                answer_tokens.append(str(payload))
            elif event_type == "tool_call":
                label = str(payload)
                tool_reports.append(f"Using {label}...")
            elif event_type == "tool_done":
                name, _raw_name = _tool_names(payload)
                tool_reports.append(f"{name} done")
            elif event_type == "interrupt":
                interrupt_data = payload
            elif event_type == "error":
                answer_tokens.append(f"\nWarning: Error: {payload}")
            elif event_type == "done" and payload and not answer_tokens:
                answer_tokens.append(str(payload))
        answer = assemble_agent_answer("".join(answer_tokens), tool_reports).strip()

    if not answer and not interrupt_data:
        answer = "_(No response)_"

    generated_files = _capture_generated_files(
        used_vision=used_vision,
        used_image_gen=used_image_gen,
        used_video_gen=used_video_gen,
    )

    return _AgentTurn(
        answer=answer,
        interrupt_data=interrupt_data,
        generated_files=generated_files,
        delivered_final=delivered_final,
    )


async def _iter_events_async(
    events_factory: Callable[[], Iterable[tuple[str, Any]]],
):
    event_queue: queue.Queue[Any] = queue.Queue()

    def _producer() -> None:
        try:
            for item in events_factory():
                event_queue.put(item)
        except Exception as exc:
            event_queue.put(("error", str(exc)))
        finally:
            event_queue.put(None)

    threading.Thread(target=_producer, daemon=True).start()
    while True:
        item = await asyncio.to_thread(event_queue.get)
        if item is None:
            break
        yield item


def _tool_names(payload: Any) -> tuple[str, str]:
    if isinstance(payload, dict):
        name = str(payload.get("name") or payload.get("display_name") or payload.get("raw_name") or "tool")
        raw_name = str(payload.get("raw_name") or payload.get("name") or "")
        return name, raw_name
    name = str(payload)
    return name, name


def _stream_display(status_lines: list[str], answer_tokens: list[str]) -> str:
    parts: list[str] = []
    if status_lines:
        parts.extend(status_lines[-4:])
    answer = "".join(answer_tokens).strip()
    if answer:
        parts.append(answer)
    return "\n".join(parts).strip()


def _capture_generated_files(
    *,
    used_vision: bool,
    used_image_gen: bool,
    used_video_gen: bool,
) -> list[str]:
    files: list[str] = []
    try:
        from row_bot.channels.media_capture import (
            grab_generated_image,
            grab_generated_video,
            grab_vision_capture,
        )

        if used_vision:
            image = grab_vision_capture()
            if image:
                files.append(_save_channel_output(image, "vision.png"))
        if used_image_gen:
            image = grab_generated_image()
            if image:
                files.append(_save_channel_output(image, "generated-image.png"))
        if used_video_gen:
            video_path = grab_generated_video()
            if video_path:
                files.append(str(video_path))
    except Exception:
        log.debug("Plugin channel media capture skipped", exc_info=True)
    return files


def _save_channel_output(data: bytes, filename: str) -> str:
    from row_bot.data_paths import get_row_bot_data_dir

    output_dir = get_row_bot_data_dir() / "channel_outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = pathlib.Path(filename).name or "output.bin"
    path = output_dir / f"{int(time.time() * 1000)}_{safe_name}"
    path.write_bytes(data)
    return str(path)


async def _deliver_generated_files(
    paths: list[str],
    callbacks: ChannelOutboundCallbacks,
) -> None:
    for path in paths:
        suffix = pathlib.Path(path).suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"} and callbacks.send_photo:
            try:
                await _maybe_await(callbacks.send_photo(path, "Image"))
            except Exception:
                log.debug("Plugin channel send_photo callback failed", exc_info=True)
        elif callbacks.send_document:
            try:
                await _maybe_await(callbacks.send_document(path, "File"))
            except Exception:
                log.debug("Plugin channel send_document callback failed", exc_info=True)


async def _send_approval_request(
    callbacks: ChannelOutboundCallbacks,
    interrupt_data: Any,
    config: dict,
) -> str | None:
    if callbacks.send_approval_request:
        try:
            ref = await _maybe_await(callbacks.send_approval_request(interrupt_data, config))
            if ref:
                return str(ref)
        except Exception:
            log.debug("Plugin channel approval callback failed", exc_info=True)

    from row_bot.channels.approval import format_interrupt_text

    detail = format_interrupt_text(interrupt_data)
    await _send_text(callbacks, f"{detail}\n\nReply yes or no.")
    return None


async def _run_goal_loop(
    *,
    channel_name: str,
    thread_id: str,
    config: dict,
    first_prompt: str,
    callbacks: ChannelOutboundCallbacks,
    enabled_tool_names: list[str] | None,
    generated_files: list[str],
):
    from row_bot.channels import runtime as channel_runtime

    state: dict[str, Any] = {"answer": "", "delivered": False}

    async def _run_turn(prompt: str, cfg: dict):
        turn = await _collect_agent_turn(
            prompt,
            cfg,
            callbacks,
            enabled_tool_names=enabled_tool_names,
            use_stream=_stream_enabled(None, None, callbacks),
            deliver_final=True,
        )
        state["answer"] = turn.answer
        state["delivered"] = turn.delivered_final
        generated_files.extend(turn.generated_files)
        await _deliver_generated_files(turn.generated_files, callbacks)
        return turn.answer, turn.interrupt_data

    async def _send_text_once(message: str) -> None:
        if state.get("delivered") and str(state.get("answer") or "").strip() == str(message or "").strip():
            state["delivered"] = False
            return
        state["delivered"] = False
        await _send_text(callbacks, message)

    return await channel_runtime.run_channel_goal_async(
        channel_name=channel_name,
        thread_id=thread_id,
        config=config,
        first_prompt=first_prompt,
        run_turn=_run_turn,
        send_text=_send_text_once,
    )


async def _continue_goal_after_turn(
    *,
    channel_name: str,
    thread_id: str,
    config: dict,
    assistant_text: str,
    callbacks: ChannelOutboundCallbacks,
    enabled_tool_names: list[str] | None,
    generated_files: list[str],
):
    from row_bot.channels import runtime as channel_runtime

    state: dict[str, Any] = {"answer": "", "delivered": False}

    async def _run_turn(prompt: str, cfg: dict):
        turn = await _collect_agent_turn(
            prompt,
            cfg,
            callbacks,
            enabled_tool_names=enabled_tool_names,
            use_stream=_stream_enabled(None, None, callbacks),
            deliver_final=True,
        )
        state["answer"] = turn.answer
        state["delivered"] = turn.delivered_final
        generated_files.extend(turn.generated_files)
        await _deliver_generated_files(turn.generated_files, callbacks)
        return turn.answer, turn.interrupt_data

    async def _send_text_once(message: str) -> None:
        if state.get("delivered") and str(state.get("answer") or "").strip() == str(message or "").strip():
            state["delivered"] = False
            return
        state["delivered"] = False
        await _send_text(callbacks, message)

    return await channel_runtime.continue_channel_goal_after_turn_async(
        channel_name=channel_name,
        thread_id=thread_id,
        config=config,
        assistant_text=assistant_text,
        interrupt_data=None,
        run_turn=_run_turn,
        send_text=_send_text_once,
    )
