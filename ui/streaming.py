"""Thoth UI — Streaming consumer, send-message, and interrupt-resume logic.

This module extracts the three heavyweight async inner functions from the
monolith:

* ``consume_generation``  — drain event queue and update the UI
* ``send_message``        — append user message, launch producer + consumer
* ``resume_after_interrupt`` — re-start the producer after an approval

Every function receives ``state``, ``p``, and named callbacks so no globals
leak in.
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import logging
import queue
import threading
import uuid
from datetime import datetime
from typing import Any, Callable

from nicegui import run, ui

from ui.state import AppState, GenerationState, P, _active_generations
from ui.constants import (

    SENTENCE_SPLIT,
    MAX_STREAM_SENTENCES,
    YT_URL_PATTERN,
    IMAGE_EXTENSIONS,
)
from ui.render import autolink_urls, _auto_fence_mermaid, render_image_with_save

logger = logging.getLogger(__name__)


# ── Captured-image memory cap ────────────────────────────────────────
# During long tool-heavy runs (dozens of browser snapshots) the per-gen
# ``captured_images`` list can hold hundreds of megabytes of base64.  We
# cap in-memory base64 entries to this many; older entries get spilled
# to disk and the list slot is replaced with the on-disk filename
# (which ``persist_thread_media_state`` treats as already-persisted).
_MAX_CAPTURED_B64_IN_MEMORY = 20


def _spill_excess_captured_images(gen: GenerationState) -> None:
    """Spill older base64 image entries to disk to keep memory bounded.

    No-op unless more than ``_MAX_CAPTURED_B64_IN_MEMORY`` b64 entries
    are resident.  The list LENGTH is unchanged; only its contents are
    swapped (b64 → filename).  ``captured_images_persist`` stays aligned.
    """
    imgs = gen.captured_images
    flags = gen.captured_images_persist
    from utils.media import is_image_filename
    # Count how many entries are still base64 (not filenames).  Anything
    # that isn't a persisted-media filename is treated as base64.
    def _is_b64(x: Any) -> bool:
        return isinstance(x, str) and bool(x) and not is_image_filename(x)

    b64_count = sum(1 for x in imgs if _is_b64(x))
    if b64_count <= _MAX_CAPTURED_B64_IN_MEMORY:
        return
    try:
        from threads import save_media_file, _next_media_filename
        from utils.media import image_ext_from_b64
    except Exception:
        logger.debug("spill: imports failed", exc_info=True)
        return

    to_spill = b64_count - _MAX_CAPTURED_B64_IN_MEMORY
    for i in range(len(imgs)):
        if to_spill <= 0:
            break
        if not _is_b64(imgs[i]):
            continue
        b64 = imgs[i]
        persist = bool(flags[i]) if i < len(flags) else False
        try:
            raw = b64.split(",", 1)[-1] if b64.startswith("data:") else b64
            data = _b64.b64decode(raw)
            ext = image_ext_from_b64(raw)
            prefix = "gen" if persist else "cap"
            fname = _next_media_filename(gen.thread_id, prefix, ext)
            save_media_file(gen.thread_id, fname, data)
            imgs[i] = fname
            to_spill -= 1
        except Exception:
            logger.debug("spill: failed to write b64 to disk", exc_info=True)
            # Move on — next tick may succeed
            break


def _format_assistant_markdown(text: str) -> str:
    """Normalise assistant markdown before rendering in streaming UI."""
    return autolink_urls(_auto_fence_mermaid(text or ""))


def _img_data_uri(b64: str) -> str:
    """Return a data URI with the correct MIME type for a base64-encoded image."""
    if b64.startswith("iVBOR"):
        return f"data:image/png;base64,{b64}"
    if b64.startswith("UklGR"):
        return f"data:image/webp;base64,{b64}"
    if b64.startswith("R0lGO"):
        return f"data:image/gif;base64,{b64}"
    return f"data:image/jpeg;base64,{b64}"


def _detach_generation(gen: GenerationState, state: AppState, reason: str) -> None:
    """Convert a live run into a detached run after the client disappears."""
    if gen.detached:
        return

    gen.detached = True
    gen.assistant_md = None
    gen.thinking_label = None
    gen.thinking_md = None
    gen.tool_col = None
    gen.wrapper = None
    gen.pending_tools.clear()

    if gen.tts_active:
        try:
            state.tts_service.stop()
        except Exception:
            logger.debug("Failed to stop TTS during detach", exc_info=True)
        gen.tts_active = False

    logger.info("Detached generation for thread %s after %s", gen.thread_id, reason)


def _handle_ui_runtime_error(
    gen: GenerationState,
    state: AppState,
    exc: Exception,
    reason: str,
) -> bool:
    """Detach the generation when NiceGUI raises after the client is gone."""
    if not isinstance(exc, RuntimeError):
        return False
    message = str(exc).strip().lower()
    if "client" not in message or "deleted" not in message:
        return False
    _detach_generation(gen, state, reason)
    return True


def _ui_handle_client_deleted(handle: Any) -> bool:
    client = getattr(handle, "client", None)
    if client is None:
        return False
    return bool(getattr(client, "_deleted", False))


def _detach_if_ui_client_deleted(
    gen: GenerationState,
    state: AppState,
    reason: str,
) -> bool:
    if gen.detached:
        return False
    for handle in (
        gen.wrapper,
        gen.assistant_md,
        gen.thinking_label,
        gen.thinking_md,
        gen.tool_col,
    ):
        if handle is not None and _ui_handle_client_deleted(handle):
            _detach_generation(gen, state, reason)
            return True
    return False


def _generation_is_terminal(gen: GenerationState) -> bool:
    return str(getattr(gen, "status", "")).lower() in {"done", "error", "stopped"}


def _drop_terminal_active_generation(thread_id: str | None) -> bool:
    if not thread_id:
        return False
    existing = _active_generations.get(thread_id)
    if existing is None or not _generation_is_terminal(existing):
        return False
    _active_generations.pop(thread_id, None)
    logger.info("Cleared terminal active generation for thread %s", thread_id)
    return True


# ── Type alias for the callback bundle ───────────────────────────────
class _Callbacks:
    """Container for all cross-cutting callbacks.

    Every field *must* be set before ``send_message`` is called.
    """

    __slots__ = (
        "rebuild_main",
        "rebuild_thread_list",
        "show_interrupt",
        "update_token_counter",
        "add_chat_message",
        "render_text_with_embeds",
    )

    def __init__(self) -> None:
        for s in self.__slots__:
            setattr(self, s, None)


Callbacks = _Callbacks  # public alias for wiring in app


# ══════════════════════════════════════════════════════════════════════
# CONSUME GENERATION
# ══════════════════════════════════════════════════════════════════════

async def consume_generation(
    gen: GenerationState,
    state: AppState,
    p: P,
    cb: _Callbacks,
) -> None:
    """Drain *gen.q* and update the UI.  Runs as an ``asyncio.Task``.

    When *gen.detached* is True (user switched to another thread), all UI
    writes are skipped but event accumulation continues so the response is
    ready when the user switches back.
    """
    from agent import get_agent_graph, repair_orphaned_tool_calls
    from buddy.events import BuddyEventType, emit_buddy_event
    from langchain_core.messages import AIMessage
    from ui.helpers import load_thread_messages, persist_detached_thread_media, persist_thread_media_state

    _stopped_shown = False
    _drain_deadline = 0.0
    _last_buddy_token_at = 0.0

    def _buddy(event_type: BuddyEventType, label: str = "", **payload: Any) -> None:
        if label:
            payload["label"] = label
        try:
            emit_buddy_event(event_type, source="ui.streaming", payload={"thread_id": gen.thread_id, **payload})
        except Exception:
            logger.debug("Buddy event emit failed", exc_info=True)

    _buddy(BuddyEventType.GENERATION_STARTED, "Thinking")

    try:
      while True:
        # ── Stop handling ────────────────────────────────────────────
        if gen.stop_event.is_set() and not _stopped_shown:
            _stopped_shown = True
            gen.status = "stopped"
            gen.accumulated += "\n\n\u23f9\ufe0f *[Stopped]*"
            _drain_deadline = asyncio.get_event_loop().time() + 30
            if not gen.detached:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                        gen.thinking_label = None
                    if gen.thinking_md:
                        gen.thinking_md.delete()
                        gen.thinking_md = None
                    if gen.assistant_md:
                        gen.assistant_md.set_visibility(True)
                        gen.assistant_md.set_content(_format_assistant_markdown(gen.accumulated))
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "stop-handling UI cleanup")
                    logger.debug("Stop-handling UI cleanup failed", exc_info=True)
            if gen.tts_active:
                state.tts_service.stop()

        if _stopped_shown and asyncio.get_event_loop().time() > _drain_deadline:
            break

        try:
            event = gen.q.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue

        if event is None:
            break

        if _stopped_shown:
            continue

        event_type, payload = event
        _break_loop = False
        if not gen.detached:
            _detach_if_ui_client_deleted(gen, state, f"before {event_type} UI update")

        # ── First content transition ─────────────────────────────────
        if not gen.first_content and event_type in ("token", "done"):
            gen.first_content = True
            if not gen.detached:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                        gen.thinking_label = None
                    if gen.thinking_text and not gen.thinking_collapsed:
                        gen.thinking_collapsed = True
                        if gen.thinking_md:
                            gen.thinking_md.delete()
                            gen.thinking_md = None
                        if gen.tool_col:
                            with gen.tool_col:
                                with ui.expansion(
                                    "\U0001f4ad Thinking", icon="psychology"
                                ).classes("w-full"):
                                    ui.code(
                                        gen.thinking_text.strip()[:8_000]
                                    ).classes("w-full text-xs")
                    if gen.assistant_md:
                        gen.assistant_md.set_visibility(True)
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "first-content transition")
                    logger.error("Error rendering thinking collapse", exc_info=True)

        if event_type == "error":
            _buddy(BuddyEventType.GENERATION_ERROR, "Error", error=str(payload)[:500])
            gen.status = "error"
            gen.error = payload
            gen.accumulated = f"\u26a0\ufe0f An error occurred: {payload}"
            if not gen.detached:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                    if gen.assistant_md:
                        gen.assistant_md.set_visibility(True)
                        gen.assistant_md.set_content(_format_assistant_markdown(gen.accumulated))
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "error-event cleanup")
                    logger.debug("Error-event UI cleanup failed", exc_info=True)
            try:
                repair_orphaned_tool_calls(gen.enabled_tools, gen.config)
            except Exception:
                logger.debug("repair_orphaned_tool_calls failed", exc_info=True)
            try:
                _agent = get_agent_graph()
                _agent.update_state(
                    gen.config,
                    {"messages": [AIMessage(content=gen.accumulated)]},
                )
            except Exception:
                logger.debug("Failed to persist error to checkpoint", exc_info=True)
            _break_loop = True

        elif event_type == "tool_call":
            _buddy(BuddyEventType.TOOL_STARTED, "Using a tool", tool=str(payload))
            if not gen.detached and gen.tool_col:
                try:
                    with gen.tool_col:
                        _pending_exp = ui.expansion(
                            f"\U0001f504 {payload}\u2026", icon="hourglass_empty"
                        ).classes("w-full")
                        # FIFO queue per tool name — parallel calls to the
                        # same tool must each get their own pending slot so
                        # later tool_done events can still match them.
                        gen.pending_tools.setdefault(payload, []).append(_pending_exp)
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "tool-call expansion")
                    logger.debug("Tool-call expansion creation failed", exc_info=True)

        elif event_type == "tool_done":
            _buddy(BuddyEventType.TOOL_FINISHED, "Tool finished")
            await _handle_tool_done(gen, state, p, payload, cb)

        elif event_type == "summarizing":
            _buddy(BuddyEventType.THINKING, "Summarizing")
            if not gen.detached and gen.wrapper:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                        gen.thinking_label = None
                    with gen.wrapper:
                        gen.thinking_label = ui.html(
                            '<span class="thoth-typing" style="font-size:0.9rem; opacity:0.6;">'
                            '\U0001f4dd Summarizing conversation history<span class="dots">'
                            '<span>.</span><span>.</span><span>.</span></span></span>',
                            sanitize=False,
                        )
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "summarizing label")
                    logger.debug("Summarizing label update failed", exc_info=True)

        elif event_type == "thinking":
            pass  # spinner already visible

        elif event_type == "thinking_token":
            _buddy(BuddyEventType.THINKING, "Reasoning")
            gen.thinking_text += payload
            if not gen.detached:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                        gen.thinking_label = None
                    if gen.thinking_md is None and gen.wrapper:
                        with gen.wrapper:
                            gen.thinking_md = ui.markdown(
                                "", extras=["code-friendly", "fenced-code-blocks"]
                            ).classes("thoth-msg w-full").style(
                                "opacity: 0.55; font-size: 0.88rem; font-style: italic;"
                            )
                    if gen.thinking_md:
                        gen.thinking_md.set_content(gen.thinking_text)
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "thinking-token rendering")
                    logger.debug("Thinking-token rendering failed", exc_info=True)

        elif event_type == "token":
            _now = asyncio.get_event_loop().time()
            if _now - _last_buddy_token_at > 0.8:
                _last_buddy_token_at = _now
                _buddy(BuddyEventType.TOKEN, "Writing")
            gen.accumulated += payload
            if not gen.detached and gen.assistant_md:
                try:
                    gen.assistant_md.set_content(_format_assistant_markdown(gen.accumulated))
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "token content update")
                    logger.debug("Token content update failed", exc_info=True)

            # Streaming TTS (only when attached)
            if gen.tts_active:
                if "```" in payload:
                    gen.tts_in_code = not gen.tts_in_code
                if not gen.tts_in_code:
                    gen.tts_buffer += payload
                    sentences = SENTENCE_SPLIT.split(gen.tts_buffer)
                    if len(sentences) > 1:
                        for s in sentences[:-1]:
                            if gen.tts_spoken >= MAX_STREAM_SENTENCES:
                                break
                            state.tts_service.speak_streaming(s)
                            gen.tts_spoken += 1
                            if gen.tts_spoken >= MAX_STREAM_SENTENCES:
                                state.tts_service.flush_streaming(
                                    "The full response is shown in the app."
                                )
                                gen.tts_active = False
                        gen.tts_buffer = sentences[-1]

        elif event_type == "interrupt":
            _buddy(BuddyEventType.APPROVAL_NEEDED, "Approval pending")
            gen.interrupt_data = payload
            gen.status = "interrupted"
            _break_loop = True

        elif event_type == "done":
            _buddy(BuddyEventType.GENERATION_DONE, "Done")
            gen.accumulated = payload
            if not gen.detached:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                        gen.thinking_label = None
                    if gen.thinking_text and not gen.thinking_collapsed:
                        gen.thinking_collapsed = True
                        if gen.thinking_md:
                            gen.thinking_md.delete()
                            gen.thinking_md = None
                        if gen.tool_col:
                            with gen.tool_col:
                                with ui.expansion(
                                    "\U0001f4ad Thinking", icon="psychology"
                                ).classes("w-full"):
                                    ui.code(
                                        gen.thinking_text.strip()[:8_000]
                                    ).classes("w-full text-xs")
                    elif gen.thinking_md:
                        gen.thinking_md.delete()
                        gen.thinking_md = None
                    if gen.assistant_md:
                        gen.assistant_md.set_visibility(True)
                        gen.assistant_md.set_content(_format_assistant_markdown(gen.accumulated))
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "done-event finalization")
                    logger.debug("Done-event UI finalization failed", exc_info=True)

        if _break_loop:
            break

    except Exception:
        logger.error("Error in generation consumer", exc_info=True)

    # ── Finalise ─────────────────────────────────────────────────────
    if gen.status == "streaming":
        gen.status = "done"

    try:
        if not gen.detached:
            _detach_if_ui_client_deleted(gen, state, "post-stream finalization")

        if not gen.detached:
            if gen.tts_active:
                state.tts_service.flush_streaming(gen.tts_buffer)

            # Re-render the streamed content via render_text_with_embeds
            # so code blocks get proper highlight.js and mermaid diagrams render.
            if gen.accumulated:
                if gen.assistant_md:
                    try:
                        gen.assistant_md.delete()
                    except (ValueError, RuntimeError) as exc:
                        _handle_ui_runtime_error(gen, state, exc, "assistant markdown delete")
                        logger.debug("assistant_md already removed from DOM", exc_info=True)
                    gen.assistant_md = None
                if gen.wrapper:
                    try:
                        with gen.wrapper:
                            cb.render_text_with_embeds(gen.accumulated)
                    except RuntimeError as exc:
                        _handle_ui_runtime_error(gen, state, exc, "final response render")
                        logger.debug("Client deleted during render_text_with_embeds", exc_info=True)

            try:
                ui.run_javascript(
                    "document.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));"
                )
                ui.run_javascript(
                    "setTimeout(function() {"
                    "  mermaid.run({nodes: document.querySelectorAll('pre.mermaid'), suppressErrors: true});"
                    "}, 150);"
                )
            except RuntimeError as exc:
                _handle_ui_runtime_error(gen, state, exc, "post-render javascript")
                logger.debug("JS runtime unavailable for hljs/mermaid", exc_info=True)
    except Exception:
        logger.error("Error in post-stream finalization", exc_info=True)

    # Store assistant message
    _has_final_output = bool(
        gen.accumulated
        or gen.tool_results
        or gen.chart_data
        or gen.captured_images
        or gen.captured_videos
    )
    _persisted_detached = False
    if _has_final_output:
        a_msg: dict = {"role": "assistant", "content": gen.accumulated}
        if gen.tool_results:
            a_msg["tool_results"] = gen.tool_results
        if gen.chart_data:
            a_msg["charts"] = gen.chart_data
        if gen.captured_images:
            a_msg["images"] = gen.captured_images
            if gen.captured_images_persist:
                a_msg["_media_persist_flags"] = list(gen.captured_images_persist)
                if any(gen.captured_images_persist):
                    a_msg["_media_persist"] = True
        if gen.captured_videos:
            a_msg["videos"] = gen.captured_videos
        if state.thread_id == gen.thread_id and not gen.detached:
            state.messages.append(a_msg)
            persist_thread_media_state(state.thread_id, state.messages)
            state.cache_active_messages()
            _persisted_detached = True
        else:
            _has_detached_media = bool(gen.captured_images or gen.captured_videos)
            if _has_detached_media:
                _persisted_detached = persist_detached_thread_media(
                    gen.thread_id,
                    gen.accumulated,
                    images=gen.captured_images,
                    image_persist_flags=gen.captured_images_persist,
                    videos=gen.captured_videos,
                )
            # Detached run wrote straight to the checkpoint; mark its
            # cached message list stale so the next select re-reads.
            state.mark_thread_dirty(gen.thread_id)
            if _has_detached_media and not _persisted_detached:
                logger.warning(
                    "Detached generation for thread %s completed but media sidecar persistence did not attach anything",
                    gen.thread_id,
                )
            if state.thread_id == gen.thread_id:
                try:
                    state.messages = load_thread_messages(gen.thread_id)
                    state.cache_active_messages()
                except Exception:
                    logger.debug("Failed to reload detached final messages for active thread %s", gen.thread_id, exc_info=True)

    # Cleanup
    if _active_generations.get(gen.thread_id) is gen:
        _active_generations.pop(gen.thread_id, None)

    # Update UI if this is still the active thread
    if state.thread_id == gen.thread_id:
        if not gen.detached:
            _detach_if_ui_client_deleted(gen, state, "final active-thread UI update")
        # If we detached mid-stream but the client came back and is
        # still looking at this thread, the in-DOM element handles are
        # stale.  Rebuild the chat view so the persisted final message
        # renders without forcing the user to click the sidebar.
        if gen.detached:
            try:
                cb.rebuild_main()
            except Exception:
                logger.debug("rebuild_main after detached finalize failed", exc_info=True)
        if p.stop_btn and not _ui_handle_client_deleted(p.stop_btn):
            try:
                p.stop_btn.props('icon=stop')
                p.stop_btn.disable()
            except Exception:
                logger.debug("stop_btn reset failed", exc_info=True)
        if state.voice_enabled and not (state.tts_service and state.tts_service.enabled):
            state.voice_service.unmute()
        if gen.interrupt_data:
            state.pending_interrupt = gen.interrupt_data
            cb.show_interrupt(gen.interrupt_data)
        try:
            cb.update_token_counter()
        except RuntimeError as exc:
            _handle_ui_runtime_error(gen, state, exc, "token counter update")
            logger.debug("Client deleted during update_token_counter", exc_info=True)

    try:
        cb.rebuild_thread_list()
    except RuntimeError as exc:
        _handle_ui_runtime_error(gen, state, exc, "thread list rebuild")
        logger.debug("Client deleted during rebuild_thread_list", exc_info=True)


# ── Tool-done sub-handler ────────────────────────────────────────────

async def _handle_tool_done(
    gen: GenerationState,
    state: AppState,
    p: P,
    payload: Any,
    cb: _Callbacks,
) -> None:
    tool_name = payload["name"] if isinstance(payload, dict) else payload
    raw_tool_name = payload.get("raw_name", tool_name) if isinstance(payload, dict) else tool_name
    tool_content = payload.get("content", "") if isinstance(payload, dict) else ""
    if not isinstance(tool_content, str):
        # content may be a list of content-blocks (e.g. Anthropic cache_control format)
        tool_content = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in tool_content) if isinstance(tool_content, list) else str(tool_content)

    if not gen.detached:
        _detach_if_ui_client_deleted(gen, state, f"tool {tool_name} result rendering")

    # Chart detection
    if tool_content and tool_content.startswith("__CHART__:"):
        marker_end = tool_content.find("\n\n", 10)
        if marker_end == -1:
            fig_json = tool_content[10:]
            display_text = "Chart created"
        else:
            fig_json = tool_content[10:marker_end]
            display_text = tool_content[marker_end + 2:]
        gen.chart_data.append(fig_json)
        if not gen.detached and gen.tool_col:
            try:
                import plotly.io as _pio
                fig = _pio.from_json(fig_json)
                with gen.tool_col:
                    ui.plotly(fig).classes("w-full")
            except Exception as exc:
                _handle_ui_runtime_error(gen, state, exc, "chart rendering")
                logger.debug("Chart rendering failed", exc_info=True)
        tool_content = display_text

    # Image marker detection (plugins / rich returns)
    if tool_content and tool_content.startswith("__IMAGE__:"):
        marker_end = tool_content.find("\n\n", 10)
        if marker_end == -1:
            _img_b64 = tool_content[10:]
            display_text = "Image generated"
        else:
            _img_b64 = tool_content[10:marker_end]
            display_text = tool_content[marker_end + 2:]
        gen.captured_images.append(_img_b64)
        gen.captured_images_persist.append(True)  # Tier 1: plugin-generated
        _spill_excess_captured_images(gen)
        if not gen.detached and gen.tool_col:
            try:
                with gen.tool_col:
                    render_image_with_save(_img_b64)
            except Exception as exc:
                _handle_ui_runtime_error(gen, state, exc, "image marker rendering")
                logger.debug("Image marker rendering failed", exc_info=True)
        tool_content = display_text

    # HTML marker detection (plugins / rich returns)
    if tool_content and tool_content.startswith("__HTML__:"):
        marker_end = tool_content.find("\n\n", 9)
        if marker_end == -1:
            _html_content = tool_content[9:]
            display_text = ""
        else:
            _html_content = tool_content[9:marker_end]
            display_text = tool_content[marker_end + 2:]
        if not gen.detached and gen.tool_col:
            try:
                with gen.tool_col:
                    ui.html(_html_content).classes("w-full")
            except Exception as exc:
                _handle_ui_runtime_error(gen, state, exc, "HTML widget rendering")
                logger.debug("HTML widget rendering failed", exc_info=True)
        tool_content = display_text

    # Update the pending expansion or create a new one
    if not gen.detached and gen.tool_col:
        try:
            _queue = gen.pending_tools.get(tool_name)
            matched_exp = _queue.pop(0) if _queue else None
            if _queue is not None and not _queue:
                gen.pending_tools.pop(tool_name, None)
            if matched_exp:
                matched_exp._props["icon"] = "check_circle"
                matched_exp._text = f"\u2705 {tool_name}"
                matched_exp.update()
                if tool_content:
                    display = tool_content[:5_000]
                    if len(tool_content) > 5_000:
                        display += "\n\n\u2026 (truncated)"
                    with matched_exp:
                        ui.code(display).classes("w-full text-xs")
            else:
                with gen.tool_col:
                    with ui.expansion(f"\u2705 {tool_name}", icon="check_circle").classes("w-full"):
                        if tool_content:
                            display = tool_content[:5_000]
                            if len(tool_content) > 5_000:
                                display += "\n\n\u2026 (truncated)"
                            ui.code(display).classes("w-full text-xs")
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, "tool expansion update")
            logger.debug("Tool expansion update failed for %s", tool_name, exc_info=True)

    gen.tool_results.append({"name": tool_name, "content": tool_content})

    # Vision capture
    if raw_tool_name in ("analyze_image",) or tool_name == "\U0001f441\ufe0f Vision":
        vsvc = state.vision_service
        if vsvc and vsvc.last_capture:
            b64_img = _b64.b64encode(vsvc.last_capture).decode("ascii")
            gen.captured_images.append(b64_img)
            gen.captured_images_persist.append(False)  # Tier 2: vision capture
            _spill_excess_captured_images(gen)
            if not gen.detached and gen.tool_col:
                try:
                    with gen.tool_col:
                        render_image_with_save(b64_img)
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "vision capture rendering")
                    logger.debug("Vision capture rendering failed", exc_info=True)
            vsvc.last_capture = None

    # Browser screenshot thumbnail — run on a thread so the Playwright
    # round-trip (200-800 ms) does not block the asyncio loop; otherwise
    # socket.io pings stall and the client is considered disconnected.
    if raw_tool_name.startswith("browser_"):
        try:
            from tools.browser_tool import get_session_manager as _get_bsm
            _bsm = _get_bsm()
            if _bsm.has_active_session():
                _bs = _bsm.get_session()
                _screenshot_bytes = await run.io_bound(
                    _bs.take_screenshot, gen.thread_id
                )
                if _screenshot_bytes:
                    _b64_ss = _b64.b64encode(_screenshot_bytes).decode("ascii")
                    gen.captured_images.append(_b64_ss)
                    gen.captured_images_persist.append(False)  # Tier 2: browser capture
                    _spill_excess_captured_images(gen)
                    if not gen.detached and gen.tool_col:
                        with gen.tool_col:
                            render_image_with_save(
                                _b64_ss,
                                extra_style="border: 1px solid #333; margin-top: 4px;",
                            )
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, "browser screenshot rendering")
            logger.debug("Browser screenshot rendering failed", exc_info=True)

    # Filesystem image display (workspace_read_file on image files)
    if raw_tool_name in ("workspace_read_file",):
        try:
            from tools.filesystem_tool import get_and_clear_displayed_image
            _fs_img = get_and_clear_displayed_image()
            if _fs_img:
                gen.captured_images.append(_fs_img["b64"])
                gen.captured_images_persist.append(False)  # Tier 2: filesystem display
                _spill_excess_captured_images(gen)
                if not gen.detached and gen.tool_col:
                    with gen.tool_col:
                        render_image_with_save(_fs_img["b64"])
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, "filesystem image rendering")
            logger.debug("Filesystem image rendering failed", exc_info=True)

    # Image generation display (generate_image / edit_image)
    if raw_tool_name in ("generate_image", "edit_image"):
        try:
            from tools.image_gen_tool import get_and_clear_last_image
            _gen_img = get_and_clear_last_image()
            if _gen_img:
                gen.captured_images.append(_gen_img)
                gen.captured_images_persist.append(True)  # Tier 1: generated image
                _spill_excess_captured_images(gen)
                if not gen.detached and gen.tool_col:
                    with gen.tool_col:
                        render_image_with_save(_gen_img)
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, "image generation rendering")
            logger.debug("Image generation rendering failed", exc_info=True)

    # Video generation display (generate_video / animate_image)
    if raw_tool_name in ("generate_video", "animate_image"):
        try:
            from tools.video_gen_tool import get_and_clear_last_video
            _gen_vid = get_and_clear_last_video()
            if _gen_vid and _gen_vid.get("path"):
                gen.captured_videos.append(_gen_vid)
                gen.captured_videos_persist.append(True)
                if not gen.detached and gen.tool_col:
                    with gen.tool_col:
                        from ui.render import render_video_with_save
                        render_video_with_save(_gen_vid["path"])
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, "video generation rendering")
            logger.debug("Video generation rendering failed", exc_info=True)


# ══════════════════════════════════════════════════════════════════════
# SEND MESSAGE
# ══════════════════════════════════════════════════════════════════════

async def send_message(
    text: str,
    *,
    state: AppState,
    p: P,
    cb: _Callbacks,
    voice_mode: bool = False,
) -> None:
    """Send a message and stream the agent response."""
    from agent import stream_agent, repair_orphaned_tool_calls, RECURSION_LIMIT_CHAT
    from threads import _save_thread_meta
    from tools import registry as tool_registry
    from ui.helpers import process_attached_files, persist_thread_media_state

    if not text.strip() and not p.pending_files:
        return
    if state.thread_id and state.thread_id in _active_generations:
        if not _drop_terminal_active_generation(state.thread_id):
            return

    # Ensure a thread exists
    if state.thread_id is None:
        tid = uuid.uuid4().hex[:12]
        name = text[:50]
        _save_thread_meta(tid, name)
        state.thread_id = tid
        state.thread_name = name
        state.messages = []
        state.show_onboarding = False
        # ``immediate=True`` so ``p.chat_container`` is ready for the
        # user-message render and streaming placeholder below (no
        # skeleton-then-hydrate race on first-send).
        try:
            cb.rebuild_main(immediate=True)
        except TypeError:
            # Older callback signature without kwargs — fall back.
            cb.rebuild_main()
        cb.rebuild_thread_list()

    gen_thread_id = state.thread_id

    # ── Snapshot & clear attached files immediately ──────────────────
    _files_snapshot: list[dict] = list(p.pending_files)
    file_names: list[str] = [f["name"] for f in _files_snapshot]
    if _files_snapshot:
        p.pending_files.clear()
        if p.file_chips_row:
            p.file_chips_row.clear()

    # ── Extract image thumbnails so the user message renders NOW ─────
    import pathlib as _plib
    user_images: list[str] = []
    for f in _files_snapshot:
        if _plib.Path(f["name"]).suffix.lower() in IMAGE_EXTENSIONS:
            user_images.append(_b64.b64encode(f["data"]).decode("ascii"))

    # ── Build display and render user message immediately ────────────
    if file_names:
        badge_text = ", ".join(f"\U0001f4ce {n}" for n in file_names)
        display_content = f"{badge_text}\n\n{text}" if text.strip() else badge_text
    else:
        display_content = text

    user_msg: dict = {"role": "user", "content": display_content}
    if user_images:
        user_msg["images"] = user_images
    state.messages.append(user_msg)
    persist_thread_media_state(state.thread_id, state.messages)
    state.cache_active_messages()
    cb.add_chat_message(user_msg)

    # ── Process attached files (slow — vision analysis etc.) ─────────
    file_context = ""
    file_warnings: list[str] = []
    if _files_snapshot:
        _has_images = any(
            _plib.Path(f["name"]).suffix.lower() in IMAGE_EXTENSIONS
            for f in _files_snapshot
        )
        _processing_note = None
        if _has_images and p.chat_container:
            with p.chat_container:
                _processing_note = ui.html(
                    '<div style="opacity:0.6; font-size:0.85rem; padding:4px 0 4px 48px;">'
                    '\U0001f50d Analyzing image<span class="dots">'
                    '<span>.</span><span>.</span><span>.</span></span></div>',
                    sanitize=False,
                )
            if p.chat_scroll:
                p.chat_scroll.scroll_to(percent=1.0)

        _effective_model = state.thread_model_override or None
        try:
            file_context, _, file_warnings = await run.io_bound(
                process_attached_files, _files_snapshot, state.vision_service,
                state.attached_data_cache, _effective_model,
            )
        except Exception as exc:
            logger.error("process_attached_files failed: %s", exc, exc_info=True)
            ui.notify(f"Failed to process attached files: {exc}", type="negative",
                      position="top", close_button=True, timeout=10000)
        for fw in file_warnings:
            ui.notify(fw, type="warning", position="top", close_button=True, timeout=8000)
        if _processing_note:
            try:
                _processing_note.delete()
            except Exception:
                logger.debug("Processing note cleanup failed", exc_info=True)

    # ── Build agent input ────────────────────────────────────────────
    agent_input = text
    if file_context:
        agent_input = f"{file_context}\n\n{text}" if text else file_context
    logger.info("send_message: file_names=%s, file_context_len=%d, agent_input_len=%d",
                file_names, len(file_context), len(agent_input))

    # Auto-name thread
    if state.thread_name and (
        state.thread_name.startswith("Thread ")
        or state.thread_name.startswith("\U0001f4bb Thread ")
    ):
        state.thread_name = f"\U0001f4bb {display_content[:50]}"
        _save_thread_meta(state.thread_id, state.thread_name)
        cb.rebuild_thread_list()
        if p.chat_header_label:
            p.chat_header_label.set_text(f"\U0001f4ac {state.thread_name}")
    else:
        _save_thread_meta(state.thread_id, state.thread_name)

    # ── Build config ─────────────────────────────────────────────────
    # Sync attachment cache to chart tool so it can read attached data files
    from tools.chart_tool import _attachment_cache as _chart_cache
    _chart_cache.clear()
    _chart_cache.update(state.attached_data_cache)

    # Sync pasted/attached images to image gen tool for edit_image
    from tools.image_gen_tool import _image_cache as _img_cache
    import tools.image_gen_tool as _igt_mod
    # Preserve all cached images within the same thread (so images from
    # earlier turns stay available); clear on thread switch.
    _same_thread = (_igt_mod._image_cache_thread_id == gen_thread_id)
    if not _same_thread:
        _img_cache.clear()
    _igt_mod._image_cache_thread_id = gen_thread_id
    # Layer new attachments on top (overwrite if same filename re-attached)
    for f in _files_snapshot:
        if _plib.Path(f["name"]).suffix.lower() in IMAGE_EXTENSIONS:
            _img_cache[f["name"]] = f["data"]

    _thread_mo = state.thread_model_override or ""
    config = {
        "configurable": {
            "thread_id": gen_thread_id,
            **({"model_override": _thread_mo} if _thread_mo else {}),
        },
        "recursion_limit": RECURSION_LIMIT_CHAT,
    }
    enabled_tools = [t.name for t in tool_registry.get_enabled_tools()]

    if voice_mode:
        agent_input = (
            "[Voice input \u2014 the user is speaking to you via microphone "
            "and your response will be read aloud. Keep responses concise "
            "and conversational.]\n\n" + agent_input
        )

    # ── Create generation state ──────────────────────────────────────
    stop_ev = threading.Event()
    gen = GenerationState(
        thread_id=gen_thread_id,
        q=queue.Queue(),
        stop_event=stop_ev,
        config=config,
        enabled_tools=enabled_tools,
        voice_mode=voice_mode,
        tts_active=voice_mode and state.tts_service.enabled,
    )
    _active_generations[gen_thread_id] = gen

    if p.stop_btn:
        p.stop_btn.enable()

    # ── Prepare assistant message placeholder ────────────────────────
    _build_assistant_placeholder(gen, p)

    if p.chat_scroll:
        p.chat_scroll.scroll_to(percent=1.0)

    # ── Start producer thread ────────────────────────────────────────
    def _sync_stream():
        try:
            for ev in stream_agent(agent_input, enabled_tools, config,
                                   stop_event=stop_ev):
                if stop_ev.is_set():
                    break
                gen.q.put(ev)
        except Exception as exc:
            if not stop_ev.is_set():
                gen.q.put(("error", str(exc)))
        finally:
            if stop_ev.is_set():
                try:
                    repair_orphaned_tool_calls(enabled_tools, config)
                except Exception:
                    logger.debug("repair_orphaned_tool_calls failed in stream finally", exc_info=True)
            gen.q.put(None)

    threading.Thread(target=_sync_stream, daemon=True).start()

    asyncio.create_task(consume_generation(gen, state, p, cb))
    cb.rebuild_thread_list()


# ══════════════════════════════════════════════════════════════════════
# RESUME AFTER INTERRUPT
# ══════════════════════════════════════════════════════════════════════

async def resume_after_interrupt(
    approved: bool,
    *,
    state: AppState,
    p: P,
    cb: _Callbacks,
) -> None:
    from agent import resume_stream_agent, repair_orphaned_tool_calls, RECURSION_LIMIT_CHAT
    from buddy.events import BuddyEventType, emit_buddy_event
    from tools import registry as tool_registry

    pending = state.pending_interrupt
    interrupt_ids = None
    if isinstance(pending, list) and len(pending) > 1:
        interrupt_ids = [
            item.get("__interrupt_id")
            for item in pending
            if isinstance(item, dict) and item.get("__interrupt_id")
        ]
    state.pending_interrupt = None

    gen_thread_id = state.thread_id
    try:
        emit_buddy_event(
            BuddyEventType.APPROVAL_APPROVED if approved else BuddyEventType.APPROVAL_DENIED,
            source="ui.streaming",
            payload={"thread_id": gen_thread_id, "label": "Approved" if approved else "Denied"},
        )
    except Exception:
        logger.debug("Buddy approval resolution event failed", exc_info=True)

    _thread_mo = state.thread_model_override or ""
    config = {
        "configurable": {
            "thread_id": gen_thread_id,
            **({"model_override": _thread_mo} if _thread_mo else {}),
        },
        "recursion_limit": RECURSION_LIMIT_CHAT,
    }
    enabled_tools = [t.name for t in tool_registry.get_enabled_tools()]

    stop_ev = threading.Event()
    gen = GenerationState(
        thread_id=gen_thread_id,
        q=queue.Queue(),
        stop_event=stop_ev,
        config=config,
        enabled_tools=enabled_tools,
    )
    _active_generations[gen_thread_id] = gen

    if p.stop_btn:
        p.stop_btn.enable()

    _build_assistant_placeholder(gen, p)

    if p.chat_scroll:
        p.chat_scroll.scroll_to(percent=1.0)

    # ── Start producer thread ────────────────────────────────────────
    def _sync_resume():
        try:
            for ev in resume_stream_agent(
                enabled_tools, config, approved,
                interrupt_ids=interrupt_ids,
                stop_event=stop_ev,
            ):
                if stop_ev.is_set():
                    break
                gen.q.put(ev)
        except Exception as exc:
            if not stop_ev.is_set():
                gen.q.put(("error", str(exc)))
        finally:
            if stop_ev.is_set():
                try:
                    repair_orphaned_tool_calls(enabled_tools, config)
                except Exception:
                    logger.debug("repair_orphaned_tool_calls failed in resume finally", exc_info=True)
            gen.q.put(None)

    threading.Thread(target=_sync_resume, daemon=True).start()

    asyncio.create_task(consume_generation(gen, state, p, cb))
    cb.rebuild_thread_list()


# ══════════════════════════════════════════════════════════════════════
# INTERRUPT DIALOG
# ══════════════════════════════════════════════════════════════════════

def build_interrupt_dialog(
    state: AppState,
    p: P,
    cb: _Callbacks,
) -> None:
    """Create the interrupt dialog and its show/close helpers.

    Attaches ``p.interrupt_dlg`` and returns the ``show_interrupt`` function
    for use as a callback.
    """
    p.interrupt_dlg = ui.dialog().props("persistent")

    def show_interrupt(data) -> None:
        p.interrupt_dlg.clear()
        items = data if isinstance(data, list) else [data]
        plural = len(items) > 1
        with p.interrupt_dlg, ui.card().classes("q-pa-none").style(
            "width: 520px; max-width: 90vw; border-radius: 16px; overflow: hidden;"
            "background: #1a1a2e; border: 1px solid #2a2a4a;"
        ):
            with ui.row().classes("w-full items-center q-pa-md").style(
                "background: linear-gradient(135deg, #2d1b00 0%, #1a1a2e 100%);"
                "border-bottom: 1px solid #3d2e00;"
            ):
                ui.icon("warning_amber", size="28px", color="amber")
                title = f"Confirm {len(items)} Actions" if plural else "Confirmation Required"
                ui.label(title).style(
                    "font-size: 1.15rem; font-weight: 700; color: #f0c040; margin-left: 8px;"
                )
            with ui.column().classes("w-full q-pa-lg"):
                subtitle = (
                    "The agent wants to perform the following actions:"
                    if plural else
                    "The agent wants to perform the following action:"
                )
                ui.label(subtitle).style(
                    "font-size: 0.85rem; color: #8888aa; margin-bottom: 8px;"
                )
                with ui.element("div").style(
                    "background: #12121e; border: 1px solid #2a2a4a; border-radius: 10px;"
                    "padding: 14px 16px; max-height: 260px; overflow-y: auto;"
                    "font-size: 0.9rem; color: #d0d0e0; line-height: 1.6;"
                    "word-wrap: break-word; white-space: pre-wrap;"
                ):
                    for i, item in enumerate(items):
                        desc = item.get("description", "Unknown action") if isinstance(item, dict) else str(item)
                        if plural:
                            ui.markdown(f"**{i + 1}.** {desc}", extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                        else:
                            ui.markdown(desc, extras=['code-friendly', 'fenced-code-blocks', 'tables'])
            btn_label = f"Approve All ({len(items)})" if plural else "Approve"
            with ui.row().classes("w-full justify-end q-pa-md gap-3").style(
                "border-top: 1px solid #2a2a4a;"
            ):
                ui.button("Deny", on_click=lambda: _close_interrupt(False)).props(
                    "flat no-caps"
                ).style(
                    "color: #ff6b6b; font-weight: 600; font-size: 0.9rem;"
                    "padding: 8px 24px; border-radius: 8px;"
                )
                ui.button(btn_label, on_click=lambda: _close_interrupt(True)).props(
                    "unelevated no-caps"
                ).style(
                    "background: #2d8a4e; color: white; font-weight: 600;"
                    "font-size: 0.9rem; padding: 8px 28px; border-radius: 8px;"
                )
        p.interrupt_dlg.open()

    def _close_interrupt(approved: bool) -> None:
        p.interrupt_dlg.close()
        asyncio.create_task(resume_after_interrupt(approved, state=state, p=p, cb=cb))

    return show_interrupt  # type: ignore[return-value]


# ══════════════════════════════════════════════════════════════════════
# HELPER – assistant placeholder
# ══════════════════════════════════════════════════════════════════════

def _build_assistant_placeholder(gen: GenerationState, p: P) -> None:
    """Build the streaming assistant message placeholder in the chat."""
    from identity import get_assistant_name
    from ui.status_bar import get_bot_avatar_html
    _ph_avatar = get_bot_avatar_html()
    _ph_name = get_assistant_name()
    with p.chat_container:
        with ui.element("div").classes("thoth-msg-row"):
            ui.html(
                f'<div class="thoth-avatar thoth-avatar-bot">{_ph_avatar}</div>',
                sanitize=False,
            )
            with ui.column().classes("thoth-msg-body gap-1") as _wrapper:
                ui.html(
                    '<div class="thoth-msg-header">'
                    f'<span class="thoth-msg-name">{_ph_name}</span>'
                    f'<span class="thoth-msg-stamp">{datetime.now().strftime("%H:%M")}</span>'
                    '</div>',
                    sanitize=False,
                )
                gen.tool_col = ui.column().classes("w-full gap-1")
                gen.thinking_label = ui.html(
                    '<span class="thoth-typing" style="font-size:0.9rem; opacity:0.6;">'
                    f'{_ph_name} is thinking<span class="dots">'
                    '<span>.</span><span>.</span><span>.</span></span></span>',
                    sanitize=False,
                )
                gen.assistant_md = ui.markdown(
                    "",
                    extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                ).classes("thoth-msg w-full")
                gen.assistant_md.set_visibility(False)
                gen.wrapper = _wrapper
