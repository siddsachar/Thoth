"""Row-Bot UI - Streaming consumer, send-message, and interrupt-resume logic.

This module extracts the three heavyweight async inner functions from the
monolith:

* ``consume_generation`` - drain event queue and update the UI
* ``send_message``        - append user message, launch producer + consumer
* ``resume_after_interrupt`` - re-start the producer after an approval

Every function receives ``state``, ``p``, and named callbacks so no globals
leak in.
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from nicegui import run, ui

from row_bot.cancellation import CancellationScope, use_cancellation_scope
from row_bot.ui.state import (
    AppState,
    GenerationState,
    P,
    _active_generations,
    cache_and_project_context_usage,
)
from row_bot.ui.constants import (

    SENTENCE_SPLIT,
    MAX_STREAM_SENTENCES,
    IMAGE_EXTENSIONS,
)
from row_bot.ui.render import (
    autolink_urls,
    _auto_fence_mermaid,
    render_agent_run_cards,
    render_agent_tool_result,
    render_skill_load_stub,
    render_image_with_save,
)
from row_bot.ui.performance import log_ui_perf
from row_bot.ui.tool_trace import (
    TOOL_TRACE_EXPANSION_CLASSES,
    canonical_tool_name,
    display_tool_content,
    parse_agent_tool_payload,
    is_agent_tool_result,
    is_browser_tool_name,
    is_computer_tool_name,
    is_skill_load_noop_result,
    parse_skill_load_result,
    tool_result_failed,
)
from row_bot.voice.cues import (
    approval_needed_cue,
    error_cue,
    heard_cue,
    long_running_cue,
    results_found_cue,
    thinking_cue,
    tool_progress_cue,
    tool_start_cue,
)
from row_bot.voice.output_controller import VoiceOutputController
from row_bot.voice.speech_policy import make_speakable_response, user_requested_read_aloud

logger = logging.getLogger(__name__)

STREAM_RENDER_MIN_INTERVAL_SECONDS = 0.075
STREAM_RENDER_MIN_CHARS = 64
STREAM_RENDER_MAX_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True)
class StopResult:
    status: str
    thread_id: str
    generation_id: str = ""

    @property
    def stopped(self) -> bool:
        return self.status in {"stopped", "already_stopped"}


def _profile_runtime_config_for_thread(thread_id: str) -> dict[str, Any]:
    """Return Agent Profile runtime config for a chat thread, if one is selected."""
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return {}
    try:
        from row_bot.agent_profiles import get_agent_profile
        from row_bot.threads import _get_thread_agent_profile

        pointer = _get_thread_agent_profile(thread_id)
        ref = str(pointer.get("id") or pointer.get("slug") or "").strip()
        if not ref:
            return {}
        profile = get_agent_profile(ref, enabled_only=True)
        if not profile:
            return {}
        config: dict[str, Any] = {
            "agent_profile_id": str(profile.get("id") or ""),
            "agent_profile_snapshot": dict(profile),
        }
        tool_policy = profile.get("tool_policy_json") or {}
        if isinstance(tool_policy, dict):
            allow_tools = [
                str(item).strip()
                for item in (tool_policy.get("allow_tools") or [])
                if str(item).strip()
            ]
            if allow_tools:
                config["tool_allowlist"] = allow_tools
        return config
    except Exception:
        logger.debug("Could not build Agent Profile runtime config", exc_info=True)
        return {}


def _client_is_live(client: Any) -> bool:
    if client is None or getattr(client, "_deleted", False):
        return False
    client_id = getattr(client, "id", "")
    instances = getattr(getattr(client, "__class__", object), "instances", None)
    if client_id and isinstance(instances, dict) and instances and client_id not in instances:
        return False
    return True


def run_realtime_client_js(p: P, code: str, *, context: str = "realtime js") -> bool:
    """Send Realtime browser JS to the owning NiceGUI client, if still alive."""
    client = getattr(p, "realtime_client", None)
    if not _client_is_live(client):
        logger.warning(
            "voice.realtime.pipeline %s",
            {"stage": "browser_js_client_missing", "context": context},
        )
        return False
    try:
        client.run_javascript(code)
        logger.info(
            "voice.realtime.pipeline %s",
            {"stage": "browser_js_dispatched", "context": context, "code_chars": len(str(code or ""))},
        )
        return True
    except Exception as exc:
        logger.warning(
            "voice.realtime.pipeline %s",
            {"stage": "browser_js_dispatch_failed", "context": context, "error": str(exc)[:500]},
            exc_info=True,
        )
        return False


def _codex_auth_block_message(model_ref: str) -> str | None:
    """Return a user-facing reconnect message when the selected Codex model cannot run."""
    try:
        from row_bot.providers.selection import provider_id_from_choice_value

        if provider_id_from_choice_value(model_ref) != "codex":
            return None
    except Exception:
        return None
    try:
        from row_bot.providers.codex import codex_runtime_block_message

        return codex_runtime_block_message(refresh_if_needed=True)
    except Exception as exc:
        return (
            "ChatGPT needs to be reconnected before using this Codex model. "
            "Open Settings -> Providers -> ChatGPT / Codex, reconnect, then try again. "
            f"Could not verify ChatGPT sign-in: {exc}"
        )


def _subscription_auth_block_message(model_ref: str) -> str | None:
    codex_message = _codex_auth_block_message(model_ref)
    if codex_message:
        return codex_message
    try:
        from row_bot.providers.selection import provider_id_from_choice_value

        if provider_id_from_choice_value(model_ref) != "xai_oauth":
            return None
    except Exception:
        return None
    try:
        from row_bot.providers.xai_oauth import xai_oauth_runtime_block_message

        return xai_oauth_runtime_block_message(refresh_if_needed=True)
    except Exception as exc:
        return (
            "xAI Grok needs to be reconnected before using this OAuth model. "
            "Open Settings -> Providers -> xAI Grok, reconnect, then try again. "
            f"Could not verify xAI Grok sign-in: {exc}"
        )


async def _agent_ready_forced_surface(model_ref: str, surface: str) -> bool:
    """Return whether a forced-Agent surface can run the selected model."""
    try:
        from row_bot.providers.readiness import evaluate_agent_readiness
        from row_bot.providers.selection import model_id_from_choice_value

        readiness = await run.io_bound(lambda: evaluate_agent_readiness(model_ref))
        if readiness.ready:
            return True
        model_id = model_id_from_choice_value(model_ref)
        details = readiness.user_message()
        ui.notify(
            f"{model_id} cannot run {surface}: this area requires an Agent-ready model. {details}",
            type="negative",
            close_button=True,
            timeout=12000,
        )
        return False
    except Exception as exc:
        ui.notify(
            f"Could not verify Agent Mode readiness for {surface}: {exc}",
            type="negative",
            close_button=True,
            timeout=12000,
        )
        return False


# ── Captured-image memory cap ────────────────────────────────────────
# During long tool-heavy runs (dozens of browser snapshots) the per-gen
# ``captured_images`` list can hold hundreds of megabytes of base64.  We
# cap in-memory base64 entries to this many; older entries get spilled
# to disk and the list slot is replaced with the on-disk filename
# (which ``persist_thread_media_state`` treats as already-persisted).
async def _context_capacity_ready_for_send(model_ref: str) -> bool:
    """Block unknown+Auto before optimistic transcript or transport work."""
    try:
        from row_bot.providers.readiness import evaluate_chat_readiness

        readiness = await run.io_bound(
            lambda: evaluate_chat_readiness(
                model_ref,
                refresh_provider_status=False,
            )
        )
    except Exception as exc:
        logger.debug("Context capacity preflight could not be evaluated: %s", exc)
        return True
    if not any(
        "context window could not be determined" in str(error).lower()
        for error in readiness.errors
    ):
        return True
    ui.notify(
        readiness.user_message(),
        type="negative",
        close_button=True,
        timeout=12000,
    )
    return False


_MAX_CAPTURED_B64_IN_MEMORY = 20


def _spill_excess_captured_images(gen: GenerationState) -> None:
    """Spill older base64 image entries to disk to keep memory bounded.

    No-op unless more than ``_MAX_CAPTURED_B64_IN_MEMORY`` b64 entries
    are resident.  The list LENGTH is unchanged; only its contents are
    swapped (b64 → filename).  ``captured_images_persist`` stays aligned.
    """
    imgs = gen.captured_images
    flags = gen.captured_images_persist
    from row_bot.utils.media import is_image_filename
    # Count how many entries are still base64 (not filenames).  Anything
    # that isn't a persisted-media filename is treated as base64.
    def _is_b64(x: Any) -> bool:
        return isinstance(x, str) and bool(x) and not is_image_filename(x)

    b64_count = sum(1 for x in imgs if _is_b64(x))
    if b64_count <= _MAX_CAPTURED_B64_IN_MEMORY:
        return
    try:
        from row_bot.threads import save_media_file, _next_media_filename
        from row_bot.utils.media import image_ext_from_b64
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
            # Move on - next tick may succeed
            break


def _format_assistant_markdown(text: str) -> str:
    """Normalise assistant markdown before rendering in streaming UI."""
    return autolink_urls(_auto_fence_mermaid(text or ""))


class LiveMarkdownBatcher:
    """Coalesce live markdown renders without changing accumulated text."""

    def __init__(
        self,
        render: Callable[[str], None],
        *,
        formatter: Callable[[str], str] | None = None,
        now: Callable[[], float] = time.perf_counter,
        min_interval_seconds: float = STREAM_RENDER_MIN_INTERVAL_SECONDS,
        min_chars: int = STREAM_RENDER_MIN_CHARS,
        max_interval_seconds: float = STREAM_RENDER_MAX_INTERVAL_SECONDS,
    ) -> None:
        self._render = render
        self._formatter = formatter or (lambda value: value)
        self._now = now
        self._min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._min_chars = max(1, int(min_chars))
        self._max_interval_seconds = max(self._min_interval_seconds, float(max_interval_seconds))
        self._content = ""
        self._dirty = False
        self._has_rendered = False
        self._last_rendered_len = 0
        self._last_render_at = 0.0
        self.flush_count = 0

    def update(self, content: str, *, force: bool = False) -> bool:
        self._content = str(content or "")
        self._dirty = True
        return self.flush(force=force)

    def flush(self, *, force: bool = False) -> bool:
        if not self._dirty:
            return False
        now = self._now()
        if not force and not self._should_flush(now):
            return False
        self._render(self._formatter(self._content))
        self._dirty = False
        self._has_rendered = True
        self._last_rendered_len = len(self._content)
        self._last_render_at = now
        self.flush_count += 1
        return True

    def _should_flush(self, now: float) -> bool:
        if self._content and not self._has_rendered:
            return True
        if not self._has_rendered:
            return False
        added_chars = abs(len(self._content) - self._last_rendered_len)
        elapsed = max(0.0, now - self._last_render_at)
        return (
            added_chars >= self._min_chars
            or (added_chars > 0 and elapsed >= self._min_interval_seconds)
            or elapsed >= self._max_interval_seconds
        )


def _img_data_uri(b64: str) -> str:
    """Return a data URI with the correct MIME type for a base64-encoded image."""
    if b64.startswith("iVBOR"):
        return f"data:image/png;base64,{b64}"
    if b64.startswith("UklGR"):
        return f"data:image/webp;base64,{b64}"
    if b64.startswith("R0lGO"):
        return f"data:image/gif;base64,{b64}"
    return f"data:image/jpeg;base64,{b64}"


def _is_computer_takeover_interrupt(data: Any) -> bool:
    items = data if isinstance(data, list) else [data]
    return any(
        isinstance(item, dict)
        and str(item.get("kind") or "") == "computer_takeover"
        for item in items
    )


def _cleanup_live_control_sessions(
    thread_id: str,
    *,
    preserve_computer_pause: bool = False,
    preserve_browser_takeover: bool = False,
    context: str = "generation",
) -> None:
    """Release task-scoped control state without touching another task."""

    try:
        from row_bot.computer_use.service import get_computer_use_service

        computer_service = get_computer_use_service()
        if preserve_computer_pause:
            computer_snapshot = computer_service.status_snapshot()
            matching_active = (
                bool(computer_snapshot.get("active"))
                and str(computer_snapshot.get("thread_id") or "") == str(thread_id or "")
            )
            computer_state = str(computer_snapshot.get("state") or "")
            preserve_pause = (
                matching_active
                and computer_state in {"waiting_user", "waiting_approval"}
            )
        else:
            preserve_pause = False
        if not preserve_pause:
            computer_service.close_for_thread(thread_id)
    except Exception:
        logger.debug("Computer live-control cleanup during %s failed", context, exc_info=True)
    try:
        from row_bot.tools.browser_tool import get_session_manager

        manager = get_session_manager()
        if preserve_browser_takeover:
            manager.end_activity(thread_id, preserve_takeover=True)
        else:
            manager.end_activity(thread_id)
    except Exception:
        logger.debug("Browser live-control cleanup during %s failed", context, exc_info=True)


def _detach_generation(gen: GenerationState, state: AppState, reason: str) -> None:
    """Convert a live run into a detached run after the client disappears."""
    if gen.detached:
        return

    _cleanup_live_control_sessions(gen.thread_id, context="detach")

    gen.detached = True
    gen.assistant_md = None
    gen.thinking_label = None
    gen.thinking_md = None
    gen.thinking_expansion = None
    gen.thinking_code = None
    gen.tool_col = None
    gen.live_row = None
    gen.wrapper = None
    gen.pending_tools.clear()

    if gen.tts_active:
        try:
            state.tts_service.stop()
        except Exception:
            logger.debug("Failed to stop TTS during detach", exc_info=True)
        gen.tts_active = False

    logger.info("Detached generation for thread %s after %s", gen.thread_id, reason)


def _delete_live_generation_row(gen: GenerationState) -> None:
    row = getattr(gen, "live_row", None)
    if row is None:
        return
    try:
        row.delete()
    except Exception:
        logger.debug("Live assistant row delete failed", exc_info=True)
    gen.live_row = None
    gen.wrapper = None
    gen.tool_col = None
    gen.assistant_md = None
    gen.thinking_label = None
    gen.thinking_md = None
    gen.thinking_expansion = None
    gen.thinking_code = None


def _delete_ui_handle_safely(handle: Any, label: str) -> None:
    """Delete a NiceGUI handle that may already have left its parent slot."""

    if handle is None:
        return
    try:
        handle.delete()
    except (RuntimeError, ValueError):
        logger.debug("%s delete skipped because the UI handle is stale", label)


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
    dead_client = "client" in message and "deleted" in message
    missing_slot = "current slot cannot be determined" in message or "slot stack" in message
    if not dead_client and not missing_slot:
        return False
    _detach_generation(gen, state, reason)
    return True


def _ui_handle_client_deleted(handle: Any) -> bool:
    try:
        client = getattr(handle, "client", None)
    except RuntimeError as exc:
        message = str(exc).lower()
        if "client" in message and "deleted" in message:
            return True
        raise
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
        gen.thinking_expansion,
        gen.thinking_code,
        gen.tool_col,
        gen.live_row,
    ):
        if handle is not None and _ui_handle_client_deleted(handle):
            _detach_generation(gen, state, reason)
            return True
    return False


def _detach_if_thread_changed(
    gen: GenerationState,
    state: AppState,
    reason: str,
) -> bool:
    """Detach live UI handles when the shared page switches to another thread."""

    if gen.detached or state.thread_id == gen.thread_id:
        return False
    _detach_generation(gen, state, reason)
    return True


def _generation_is_terminal(gen: GenerationState) -> bool:
    return str(getattr(gen, "status", "")).lower() in {"done", "error", "stopped"}


def _render_thinking_collapse(gen: GenerationState) -> bool:
    """Persist streamed reasoning as a collapsed disclosure.

    The live markdown is intentionally removed only after the expansion exists.
    This avoids a UX failure where reasoning tokens briefly stream, then vanish
    if the preferred tool column cannot accept a new child.
    """
    if not gen.thinking_text or gen.thinking_collapsed or gen.thinking_expansion:
        return bool(gen.thinking_expansion)

    container = gen.tool_col or gen.wrapper
    if not container:
        return False

    with container:
        gen.thinking_expansion = ui.expansion(
            "\U0001f4ad Thinking", icon="psychology"
        ).classes("w-full")
        with gen.thinking_expansion:
            gen.thinking_code = ui.code(
                gen.thinking_text.strip()[:8_000]
            ).classes("w-full text-xs")

    if gen.thinking_md:
        _delete_ui_handle_safely(gen.thinking_md, "Thinking markdown")
        gen.thinking_md = None
    gen.thinking_collapsed = True
    return True


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
def _stop_generation_voice_outputs(gen: GenerationState, state: AppState | None, p: P | None) -> None:
    if state is None:
        return
    coordinator = getattr(state, "voice_coordinator", None)
    if coordinator is not None and getattr(coordinator, "transport", "") == "realtime":
        try:
            from row_bot.voice.realtime_client import stop_realtime_client_js

            if p is not None:
                run_realtime_client_js(p, stop_realtime_client_js(), context="stop_realtime_on_stop")
        except Exception:
            logger.debug("Realtime stop JS dispatch failed", exc_info=True)
        try:
            state.voice_enabled = False
            coordinator.stop()
        except Exception:
            logger.debug("Realtime coordinator stop failed", exc_info=True)
    tts = getattr(state, "tts_service", None)
    if tts and getattr(tts, "enabled", False):
        try:
            tts.stop()
        except Exception:
            logger.debug("TTS stop failed", exc_info=True)
        try:
            if coordinator is not None and getattr(coordinator, "is_running", False):
                coordinator.unmute()
        except Exception:
            logger.debug("Voice coordinator unmute after stop failed", exc_info=True)
    gen.tts_active = False


def _stop_generation_child_agent_runs(gen: GenerationState) -> None:
    run_ids = {
        str(run_id).strip()
        for run_id in [
            *list(getattr(gen, "live_agent_run_ids", set()) or []),
            *list(getattr(gen, "live_async_agent_run_ids", set()) or []),
        ]
        if str(run_id or "").strip()
    }
    try:
        baseline = {
            str(run_id).strip()
            for run_id in getattr(gen, "baseline_child_agent_run_ids", set()) or set()
            if str(run_id or "").strip()
        }
        run_ids.update(_child_agent_run_ids_for_thread(gen.thread_id) - baseline)
    except Exception:
        logger.debug("Could not enumerate child Agent runs during generation stop", exc_info=True)
    if not run_ids:
        return
    try:
        from row_bot.agent_runner import stop_agent_run
    except Exception:
        logger.debug("Could not import child Agent stop helper", exc_info=True)
        return
    for run_id in sorted(run_ids):
        try:
            stop_agent_run(run_id)
        except Exception:
            logger.debug("Failed to stop child Agent run %s", run_id, exc_info=True)


def _emit_buddy_terminal_stop(gen: GenerationState | None, reason: str) -> None:
    """Clear Buddy activity lanes exactly once for a terminal user stop."""

    if gen is not None:
        if gen.buddy_terminal_emitted:
            return
        gen.buddy_terminal_emitted = True
        thread_id = gen.thread_id
    else:
        thread_id = ""
    try:
        from row_bot.buddy.events import BuddyEventType, emit_buddy_event

        emit_buddy_event(
            BuddyEventType.GENERATION_STOPPED,
            source="ui.streaming",
            payload={
                "thread_id": thread_id,
                "label": "Stopped",
                "reason": str(reason or "user")[:80],
            },
        )
    except Exception:
        logger.debug("Buddy terminal stop event failed", exc_info=True)


def request_generation_stop(
    thread_id: str | None,
    *,
    state: AppState | None = None,
    p: P | None = None,
    reason: str = "user",
) -> StopResult:
    """Request cancellation for the active generation and release the UI promptly."""

    normalized_thread_id = str(thread_id or "")
    had_pending_interrupt = False
    if state is not None and str(getattr(state, "thread_id", "") or "") == normalized_thread_id:
        had_pending_interrupt = getattr(state, "pending_interrupt", None) is not None
        state.pending_interrupt = None
        state.pending_interrupt_generation_id = ""
        state.pending_interrupt_tool_groups = {}
        state.pending_interrupt_runtime_surface = ""
    if had_pending_interrupt and p is not None:
        try:
            interrupt_dialog = getattr(p, "interrupt_dlg", None)
            if interrupt_dialog is not None:
                interrupt_dialog.close()
        except Exception:
            logger.debug("Pending approval dialog close during stop failed", exc_info=True)
    gen = _active_generations.get(normalized_thread_id)
    if gen is None:
        if had_pending_interrupt:
            _emit_buddy_terminal_stop(None, reason)
        _cleanup_live_control_sessions(
            normalized_thread_id,
            context="generation stop without active producer",
        )
        fallback_event = getattr(state, "stop_event", None)
        if fallback_event is not None:
            try:
                fallback_event.set()
            except Exception:
                logger.debug("Fallback state stop_event set failed", exc_info=True)
        if p is not None and getattr(p, "stop_btn", None) and not _ui_handle_client_deleted(p.stop_btn):
            try:
                p.stop_btn.props("icon=stop")
                p.stop_btn.disable()
            except Exception:
                logger.debug("Idle stop_btn reset failed", exc_info=True)
        return StopResult(status="not_found", thread_id=normalized_thread_id)

    already_stopped = bool(gen.stop_event.is_set() or str(getattr(gen, "status", "")) == "stopped")
    gen.stop_reason = str(reason or "user")
    if not gen.stop_requested_at:
        gen.stop_requested_at = time.perf_counter()
    gen.status = "stopped"
    _emit_buddy_terminal_stop(gen, gen.stop_reason)

    scope = getattr(gen, "cancel_scope", None)
    if scope is not None:
        try:
            scope.cancel(gen.stop_reason)
        except Exception:
            logger.debug("Cancellation scope cancel failed", exc_info=True)
            gen.stop_event.set()
    else:
        gen.stop_event.set()

    _cleanup_live_control_sessions(normalized_thread_id, context="generation stop")

    _stop_generation_child_agent_runs(gen)
    _stop_generation_voice_outputs(gen, state, p)

    if p is not None and getattr(p, "stop_btn", None) and not _ui_handle_client_deleted(p.stop_btn):
        try:
            p.stop_btn.props("icon=stop")
            p.stop_btn.disable()
        except Exception:
            logger.debug("Stop button immediate disable failed", exc_info=True)

    if _active_generations.get(normalized_thread_id) is gen:
        _active_generations.pop(normalized_thread_id, None)
        logger.info(
            "Stopped and detached active generation for thread %s generation=%s",
            normalized_thread_id,
            gen.generation_id,
        )

    if not already_stopped and not getattr(gen, "stopped_marker_rendered", False):
        try:
            gen.q.put(("stopped", {"reason": gen.stop_reason}))
        except Exception:
            logger.debug("Failed to enqueue stopped event", exc_info=True)

    return StopResult(
        status="already_stopped" if already_stopped else "stopped",
        thread_id=normalized_thread_id,
        generation_id=str(getattr(gen, "generation_id", "") or ""),
    )


def _set_expansion_title(expansion: Any, title: str, icon: str) -> None:
    """Update a NiceGUI expansion title without relying on a public setter."""

    try:
        expansion._props["icon"] = icon
        expansion._props["label"] = title
        expansion._text = title
        expansion.update()
    except Exception:
        logger.debug("Failed to update expansion title", exc_info=True)


def _live_tool_group(gen: GenerationState, tool_name: str) -> dict[str, Any] | None:
    if gen.detached or not gen.tool_col:
        return None
    canonical_name = canonical_tool_name(tool_name)
    display_name = "Browser activity" if is_browser_tool_name(canonical_name) else "Computer activity" if is_computer_tool_name(canonical_name) else canonical_name
    group = gen.pending_tools.get(display_name)
    if isinstance(group, dict):
        return group
    with gen.tool_col:
        activity = _tool_activity_line(display_name)
        if activity:
            ui.label(activity).classes("text-xs text-grey-6 q-ml-sm")
        exp = ui.expansion(
            f"Running {display_name} - 0 calls",
            icon="hourglass_empty",
        ).classes(TOOL_TRACE_EXPANSION_CLASSES)
    group = {
        "name": display_name,
        "expansion": exp,
        "count": 0,
        "done": 0,
        "succeeded": 0,
        "failed_count": 0,
        "pending": [],
    }
    gen.pending_tools[display_name] = group
    return group


def _tool_activity_line(display_name: str) -> str:
    name = str(display_name or "").lower()
    if name == "developer":
        return "Developer is working in the code workspace."
    if name == "shell":
        return "Running a workspace command."
    if name == "browser activity":
        return "Browser automation is stepping through the page."
    if name == "computer activity":
        return "Computer Use is controlling the named native app; Stop or Take over remains available here."
    if name in {"file", "document"}:
        return "Reading local context."
    return ""


def _render_inline_interrupt_notice(
    gen: GenerationState,
    state: AppState,
    p: P,
    cb: _Callbacks,
) -> bool:
    """Render a redundant approval surface inside the active chat.

    The modal dialog is still the primary approval UI. Developer Studio also
    gets this inline banner so an approval remains visible if the dialog's
    client/slot is unavailable during a long run.
    """

    if not state.active_developer_workspace_id or state.thread_id != gen.thread_id:
        return False
    target_container = getattr(p, "developer_approval_container", None) or p.chat_container
    if gen.detached or target_container is None:
        return False
    try:
        def _resume_inline(approved: bool) -> None:
            try:
                if p.interrupt_dlg is not None:
                    p.interrupt_dlg.close()
            except Exception:
                logger.debug("Inline approval could not close modal dialog", exc_info=True)
            asyncio.create_task(resume_after_interrupt(approved, state=state, p=p, cb=cb))

        try:
            target_container.clear()
        except Exception:
            logger.debug("Developer approval container clear failed", exc_info=True)
        with target_container:
            with ui.card().classes("w-full q-pa-md").style(
                "border-radius: 8px; border: 1px solid rgba(245,158,11,0.55); "
                "background: rgba(120, 53, 15, 0.20);"
            ):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("warning_amber", color="amber", size="sm")
                        ui.label("Developer approval pending").classes("text-sm text-amber-3")
                    with ui.row().classes("items-center gap-2"):
                        ui.button(
                            "Deny",
                            on_click=lambda: _resume_inline(False),
                        ).props("flat dense no-caps")
                        ui.button(
                            "Approve",
                            on_click=lambda: _resume_inline(True),
                        ).props("color=amber dense no-caps")
                data = gen.interrupt_data
                items = data if isinstance(data, list) else [data]
                first = items[0] if items else {}
                if isinstance(first, dict):
                    desc = first.get("description") or first.get("label") or first.get("tool") or "Approval required"
                    ui.label(str(desc)).classes("text-xs text-grey-5")
        if p.chat_scroll:
            p.chat_scroll.scroll_to(percent=1.0)
        return True
    except Exception as exc:
        _handle_ui_runtime_error(gen, state, exc, "developer inline interrupt render")
        logger.debug("Developer inline interrupt render failed", exc_info=True)
        return False


def _add_live_tool_pending(gen: GenerationState, tool_name: str) -> None:
    group = _live_tool_group(gen, tool_name)
    if not group:
        return
    group["count"] += 1
    call_no = group["count"]
    exp = group["expansion"]
    with exp:
        row = ui.column().classes("w-full gap-1 q-ml-sm")
        with row:
            label = ui.label(f"#{call_no} running...").classes("text-xs text-grey-6")
    group["pending"].append({"row": row, "label": label, "call_no": call_no})
    unit = "step" if group["name"] in {"Browser activity", "Computer activity"} else "call"
    suffix = unit if group["count"] == 1 else f"{unit}s"
    _set_expansion_title(exp, f"Running {group['name']} - {group['count']} {suffix}", "hourglass_empty")


def _finish_live_tool_result(gen: GenerationState, tool_name: str, content: str) -> bool:
    canonical_name = canonical_tool_name(tool_name)
    display_name = "Browser activity" if is_browser_tool_name(canonical_name) else "Computer activity" if is_computer_tool_name(canonical_name) else canonical_name
    group = gen.pending_tools.get(display_name)
    if not isinstance(group, dict):
        return False
    pending = group.get("pending") or []
    item = pending.pop(0) if pending else None
    group["done"] = int(group.get("done") or 0) + 1
    row = item.get("row") if item else None
    label = item.get("label") if item else None
    call_no = item.get("call_no") if item else group["done"]
    failed = tool_result_failed(content)
    if failed:
        group["failed_count"] = int(group.get("failed_count") or 0) + 1
    else:
        group["succeeded"] = int(group.get("succeeded") or 0) + 1
    if row is None:
        with group["expansion"]:
            row = ui.column().classes("w-full gap-1 q-ml-sm")
    with row:
        if label is not None:
            label.set_text(f"#{call_no} {'failed' if failed else 'complete'}")
            label.classes(f"text-xs {'text-negative' if failed else 'text-positive'}")
        else:
            ui.label(f"#{call_no} {'failed' if failed else 'complete'}").classes(
            f"text-xs {'text-negative' if failed else 'text-positive'}"
        )
        tool_result = {"name": tool_name, "content": content}
        if not _agent_tool_result_already_live(gen, tool_result) and not render_agent_tool_result(tool_result):
            display = display_tool_content(content)
            if display:
                ui.code(display).classes("w-full text-xs")
    group["failed"] = bool(group.get("failed")) or failed
    icon = "warning" if group.get("failed") else ("check_circle" if group["done"] >= group["count"] else "hourglass_empty")
    prefix = "Needs attention" if group.get("failed") else ("Done" if icon == "check_circle" else "Running")
    if group.get("failed"):
        progress = (
            f"{int(group.get('succeeded') or 0)} succeeded · "
            f"{int(group.get('failed_count') or 0)} failed"
        )
    else:
        progress = f"{group['done']}/{group['count']} complete"
    _set_expansion_title(
        group["expansion"],
        f"{prefix} {group['name']} - {progress}",
        icon,
    )
    return True


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
        "mark_chat_message_rendered",
        "render_text_with_embeds",
        "refresh_chat_messages",
        "insert_chat_message_before_live_row",
        "refresh_parent_agent_strip",
        "refresh_goal_strip",
        "refresh_model_controls",
    )

    def __init__(self) -> None:
        for s in self.__slots__:
            setattr(self, s, None)


Callbacks = _Callbacks  # public alias for wiring in app


def _refresh_parent_agent_strip(cb: Any) -> None:
    refresh = getattr(cb, "refresh_parent_agent_strip", None)
    if not callable(refresh):
        return
    try:
        refresh()
    except Exception:
        logger.debug("Parent Agent strip refresh failed", exc_info=True)


def _schedule_parent_agent_strip_refresh(cb: Any) -> None:
    _refresh_parent_agent_strip(cb)
    for delay in (0.75, 2.0, 4.0):
        try:
            ui.timer(delay, lambda cb=cb: _refresh_parent_agent_strip(cb), once=True)
        except Exception:
            logger.debug("Parent Agent strip delayed refresh scheduling failed", exc_info=True)
            break


def _refresh_goal_strip(cb: Any) -> None:
    refresh = getattr(cb, "refresh_goal_strip", None)
    if not callable(refresh):
        return
    try:
        refresh()
    except Exception:
        logger.debug("Goal strip refresh failed", exc_info=True)


def _schedule_goal_strip_refresh(cb: Any) -> None:
    _refresh_goal_strip(cb)
    for delay in (0.25, 1.0, 3.0, 5.0):
        try:
            ui.timer(delay, lambda cb=cb: _refresh_goal_strip(cb), once=True)
        except Exception:
            logger.debug("Goal strip delayed refresh scheduling failed", exc_info=True)
            break


def _rebuild_goal_surface(cb: Any, *, reason: str) -> None:
    rebuild = getattr(cb, "rebuild_main", None)
    if not callable(rebuild):
        return
    try:
        rebuild(immediate=True, reason=reason)
    except TypeError:
        try:
            rebuild()
        except Exception:
            logger.debug("Goal surface rebuild failed", exc_info=True)
    except Exception:
        logger.debug("Goal surface rebuild failed", exc_info=True)


def _schedule_goal_surface_refresh(cb: Any, *, reason: str) -> None:
    _schedule_goal_strip_refresh(cb)
    _rebuild_goal_surface(cb, reason=reason)


def _refresh_model_controls(cb: Any) -> None:
    refresh = getattr(cb, "refresh_model_controls", None)
    if not callable(refresh):
        return
    try:
        refresh()
    except Exception:
        logger.debug("Model controls refresh failed", exc_info=True)


def _schedule_model_controls_refresh(cb: Any) -> None:
    _refresh_model_controls(cb)
    for delay in (0.25, 1.0):
        try:
            ui.timer(delay, lambda cb=cb: _refresh_model_controls(cb), once=True)
        except Exception:
            logger.debug("Model controls delayed refresh scheduling failed", exc_info=True)
            break


def _tool_event_name(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("name") or payload.get("raw_name") or "tool")
    get_value = getattr(payload, "get", None)
    if callable(get_value):
        try:
            return str(get_value("name", str(payload)) or str(payload) or "tool")
        except Exception:
            return str(payload or "tool")
    return str(payload or "tool")


def _tool_event_raw_name(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("raw_name") or payload.get("name") or "")
    get_value = getattr(payload, "get", None)
    if callable(get_value):
        try:
            return str(get_value("raw_name", "") or "")
        except Exception:
            return ""
    return ""


def _tool_event_args(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        args = payload.get("args")
        return args if isinstance(args, dict) else {}
    get_value = getattr(payload, "get", None)
    if callable(get_value):
        try:
            args = get_value("args", {})
            return args if isinstance(args, dict) else {}
        except Exception:
            return {}
    return {}


def _interrupt_changes_model_setting(pending: Any) -> bool:
    items = pending if isinstance(pending, list) else [pending]
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("tool") or "").strip() != "row_bot_update_setting":
            continue
        args = item.get("args")
        if isinstance(args, dict) and str(args.get("setting") or "").strip().lower() in {
            "model",
            "thread_model",
            "default_model",
        }:
            return True
    return False


def _tool_result_changes_model_setting(raw_tool_name: Any, content: Any) -> bool:
    """Return true for successful direct model-setting tool results."""

    if str(raw_tool_name or "").strip() != "row_bot_update_setting":
        return False
    text = str(content or "").strip()
    return text.startswith((
        "Active model changed to:",
        "Thread model override changed to:",
        "Thread model override cleared;",
        "Global default model changed to:",
    ))


def _required_orchestration_from_tool_result(
    tool_name: Any,
    content: Any,
) -> tuple[str, bool]:
    payload = parse_agent_tool_payload(
        {"name": str(tool_name or ""), "content": content}
    )
    if not payload:
        return "", False
    orchestration = payload.get("orchestration")
    if not isinstance(orchestration, dict):
        return "", False
    orchestration_id = str(orchestration.get("id") or "").strip()
    return orchestration_id, bool(orchestration_id and orchestration.get("required"))


def _looks_like_new_agent_request(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    agent_terms = (
        "use an agent",
        "use another agent",
        "spawn an agent",
        "spawn another agent",
        "start an agent",
        "start another agent",
        "delegate",
        "subagent",
        "child agent",
    )
    return any(term in normalized for term in agent_terms)


def _queued_control_message(
    text: str,
    *,
    kind: str,
    status: str,
    label: str,
    agent_run_id: str = "",
    agent_name: str = "",
    message_id: str | None = None,
) -> dict:
    return {
        "role": "user",
        "content": str(text or "").strip(),
        "timestamp": datetime.now().strftime("%H:%M"),
        "queued_control": {
            "id": message_id or uuid.uuid4().hex[:12],
            "kind": str(kind or "follow_up"),
            "status": str(status or "queued_parent_turn"),
            "label": str(label or "Queued"),
            "agent_run_id": str(agent_run_id or ""),
            "agent_name": str(agent_name or ""),
        },
    }


def _queued_control_ids(messages: list[dict], ids: list[str] | None) -> set[str]:
    return {str(item) for item in (ids or []) if str(item or "").strip()}


def _queued_id_set(ids: list[str] | None) -> set[str]:
    return {str(item) for item in (ids or []) if str(item or "").strip()}


def _queued_control_id(msg: dict) -> str:
    queued = msg.get("queued_control")
    if not isinstance(queued, dict):
        return ""
    return str(queued.get("id") or "").strip()


def _is_parent_turn_queued_control(msg: dict) -> bool:
    queued = msg.get("queued_control")
    if not isinstance(queued, dict):
        return False
    return str(queued.get("status") or "") in {
        "queued_parent_turn",
        "dispatching",
    }


def _turn_boundary_generation_id(msg: dict) -> str:
    boundary = msg.get("turn_boundary")
    if not isinstance(boundary, dict):
        return ""
    return str(boundary.get("after_generation_id") or "").strip()


def _is_future_parent_turn_boundary(
    msg: dict,
    *,
    current_queued_ids: set[str],
    current_generation_id: str = "",
) -> bool:
    if _is_parent_turn_queued_control(msg):
        queued_id = _queued_control_id(msg)
        return not (queued_id and queued_id in current_queued_ids)
    generation_id = _turn_boundary_generation_id(msg)
    return bool(current_generation_id and generation_id == current_generation_id)


def _mark_queued_controls_dispatching(messages: list[dict], ids: list[str] | None) -> bool:
    id_set = _queued_control_ids(messages, ids)
    if not id_set:
        return False
    changed = False
    for msg in messages:
        queued = msg.get("queued_control")
        if not isinstance(queued, dict):
            continue
        if str(queued.get("id") or "") not in id_set:
            continue
        queued["status"] = "dispatching"
        queued["label"] = "Sent after current response"
        changed = True
    return changed


def _settle_queued_controls(messages: list[dict], ids: list[str] | None) -> bool:
    id_set = _queued_id_set(ids)
    if not id_set:
        return False
    changed = False
    for msg in messages:
        queued_id = _queued_control_id(msg)
        if queued_id and queued_id in id_set and "queued_control" in msg:
            msg.pop("queued_control", None)
            changed = True
    return changed


def _insert_assistant_before_future_queued_turns(
    messages: list[dict],
    assistant_msg: dict,
    *,
    current_queued_ids: list[str] | None = None,
    current_generation_id: str = "",
) -> int:
    current_ids = _queued_id_set(current_queued_ids)
    insert_at = len(messages)
    for idx, msg in enumerate(messages):
        if _is_future_parent_turn_boundary(
            msg,
            current_queued_ids=current_ids,
            current_generation_id=current_generation_id,
        ):
            insert_at = idx
            break
    messages.insert(insert_at, assistant_msg)
    return insert_at


def _append_visible_queued_control(
    *,
    state: AppState,
    cb: _Callbacks,
    text: str,
    kind: str,
    status: str,
    label: str,
    agent_run_id: str = "",
    agent_name: str = "",
) -> str:
    msg = _queued_control_message(
        text,
        kind=kind,
        status=status,
        label=label,
        agent_run_id=agent_run_id,
        agent_name=agent_name,
    )
    state.messages.append(msg)
    state.cache_active_messages()
    if cb.add_chat_message:
        cb.add_chat_message(msg)
    return str((msg.get("queued_control") or {}).get("id") or "")


def _single_steering_target(parent_thread_id: str) -> dict | None:
    if not parent_thread_id:
        return None
    try:
        from row_bot.agent_runs import list_agent_runs

        queued = list_agent_runs(
            parent_thread_id=parent_thread_id,
            statuses=["queued", "waiting_user"],
            kind="subagent",
            limit=4,
        )
        if len(queued) == 1:
            return queued[0]
        if queued:
            return None
        running = list_agent_runs(
            parent_thread_id=parent_thread_id,
            statuses=["running"],
            kind="subagent",
            limit=4,
        )
        if len(running) == 1:
            return running[0]
    except Exception:
        logger.debug("Could not resolve active-run steering target", exc_info=True)
    return None


# ══════════════════════════════════════════════════════════════════════
# CONSUME GENERATION
# ══════════════════════════════════════════════════════════════════════

_DIRECT_AGENT_TERMINAL_STATUSES = {
    "completed",
    "completed_delivery_failed",
    "failed",
    "stopped",
    "blocked",
    "timed_out",
    "cancelled",
}


def _agent_run_card_refresh_key(run_row: dict | None) -> str:
    if not run_row:
        return "missing"
    return "|".join(
        [
            str(run_row.get("id") or ""),
            str(run_row.get("status") or ""),
            str(run_row.get("status_message") or ""),
            str(run_row.get("summary") or ""),
            str(run_row.get("error") or ""),
            str(run_row.get("turns_used") or 0),
            str(run_row.get("finished_at") or ""),
            str(run_row.get("stop_requested") or 0),
        ]
    )


def _agent_poll_refresh_keys(
    *,
    run_rows: list[dict],
    orchestration_rows: list[dict],
    orchestration_messages: list[dict],
    activity: dict[str, Any],
    checkpoint_revision: str,
) -> dict[str, str]:
    """Split one poll snapshot into the three visible refresh boundaries."""

    activity_key = "|".join(
        str(activity.get(field) or "")
        for field in (
            "orchestration_id",
            "state",
            "blocking",
            "background",
            "phase",
            "active_members",
            "failed_members",
        )
    )
    run_key = ";".join(_agent_run_card_refresh_key(row) for row in run_rows)
    orchestration_key = ";".join(
        "|".join(
            str(row.get(field) or "")
            for field in (
                "id",
                "status",
                "parent_state",
                "error_message",
                "completed_at",
            )
        )
        for row in orchestration_rows
    )
    message_key = ";".join(
        "|".join(
            str(row.get(field) or "")
            for field in (
                "id",
                "kind",
                "delivery_status",
                "delivered_at",
            )
        )
        for row in orchestration_messages
    )
    return {
        "sidebar": activity_key,
        "strip": "\n".join((activity_key, run_key, orchestration_key)),
        "transcript": "\n".join(
            (
                str(checkpoint_revision or ""),
                run_key,
                message_key,
                str(activity.get("phase") == "approval_wait"),
            )
        ),
    }


def _refresh_key_for_agent_run_ids(run_ids: list[str]) -> str:
    if not run_ids:
        return ""
    try:
        from row_bot.agent_runs import get_agent_run

        return ";".join(_agent_run_card_refresh_key(get_agent_run(run_id)) for run_id in run_ids)
    except Exception:
        logger.debug("Could not build Agent Run card refresh key", exc_info=True)
        return ""


def _visible_agent_run_ids(messages: list[dict]) -> set[str]:
    visible: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role") or "") != "assistant":
            continue
        for item in msg.get("agent_run_ids") or []:
            run_id = str(item or "").strip()
            if run_id:
                visible.add(run_id)
    return visible


def _child_agent_run_ids_for_thread(thread_id: str) -> set[str]:
    parent_thread_id = str(thread_id or "").strip()
    if not parent_thread_id:
        return set()
    try:
        from row_bot.agent_runs import list_agent_runs

        return {
            str(run.get("id") or "").strip()
            for run in list_agent_runs(parent_thread_id=parent_thread_id, kind="subagent", limit=200)
            if str(run.get("id") or "").strip()
        }
    except Exception:
        logger.debug("Could not list child Agent runs for thread %s", parent_thread_id, exc_info=True)
        return set()


def _agent_run_ids_from_tool_result(tool_result: dict) -> set[str]:
    return set(_ordered_agent_run_ids_from_tool_result(tool_result))


def _ordered_agent_run_ids_from_tool_result(tool_result: dict) -> list[str]:
    try:
        from row_bot.ui.tool_trace import agent_runs_from_payload, parse_agent_tool_payload

        payload = parse_agent_tool_payload(tool_result)
        run_ids: list[str] = []
        seen: set[str] = set()
        for run in agent_runs_from_payload(payload):
            run_id = str(run.get("id") or "").strip()
            if run_id and run_id not in seen:
                run_ids.append(run_id)
                seen.add(run_id)
        return run_ids
    except Exception:
        logger.debug("Could not parse Agent tool result run ids", exc_info=True)
        return []


def _ordered_agent_run_ids_from_tool_results(tool_results: list) -> list[str]:
    run_ids: list[str] = []
    seen: set[str] = set()
    for item in tool_results or []:
        if not (isinstance(item, dict) and is_agent_tool_result(item)):
            continue
        for run_id in _ordered_agent_run_ids_from_tool_result(item):
            if run_id not in seen:
                run_ids.append(run_id)
                seen.add(run_id)
    return run_ids


def _async_delegated_run_ids_from_tool_results(tool_results: list) -> list[str]:
    run_ids: list[str] = []
    seen: set[str] = set()
    for item in tool_results or []:
        if not (isinstance(item, dict) and _is_async_delegated_agent_tool_result(item)):
            continue
        for run_id in _ordered_agent_run_ids_from_tool_result(item):
            if run_id not in seen:
                run_ids.append(run_id)
                seen.add(run_id)
    return run_ids


def _wait_delegated_run_ids_from_tool_results(tool_results: list) -> set[str]:
    run_ids: set[str] = set()
    for item in tool_results or []:
        if not (isinstance(item, dict) and is_agent_tool_result(item)):
            continue
        name = str(item.get("name") or "").strip().lower()
        if name not in {"delegate_work", "agents"}:
            continue
        payload = parse_agent_tool_payload(item)
        if not isinstance(payload, dict):
            continue
        if str(payload.get("message") or "").strip().lower() == "child agent started.":
            continue
        run_ids.update(_ordered_agent_run_ids_from_tool_result(item))
    return run_ids


def _async_child_agent_run_ids_for_generation(gen: GenerationState, tool_results: list) -> list[str]:
    baseline_ids = {
        str(run_id).strip()
        for run_id in getattr(gen, "baseline_child_agent_run_ids", set())
        if str(run_id).strip()
    }
    wait_ids = _wait_delegated_run_ids_from_tool_results(tool_results)
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(run_id: object) -> None:
        clean = str(run_id or "").strip()
        if not clean or clean in wait_ids or clean in seen:
            return
        candidates.append(clean)
        seen.add(clean)

    for run_id in getattr(gen, "live_async_agent_run_ids", set()):
        _add(run_id)

    for run_id in _child_agent_run_ids_for_thread(getattr(gen, "thread_id", "")):
        clean = str(run_id or "").strip()
        if clean and clean not in baseline_ids:
            _add(clean)

    return candidates


def _filter_visible_agent_tool_results(
    messages: list[dict],
    tool_results: list,
) -> tuple[list, bool]:
    visible_ids = _visible_agent_run_ids(messages)
    if not visible_ids:
        return list(tool_results or []), False
    filtered: list = []
    removed = False
    for item in tool_results or []:
        if isinstance(item, dict) and is_agent_tool_result(item):
            run_ids = _agent_run_ids_from_tool_result(item)
            if run_ids and run_ids <= visible_ids:
                removed = True
                continue
        filtered.append(item)
    return filtered, removed


def _direct_agent_thread_messages(state: AppState, thread_id: str) -> list[dict]:
    if state.thread_id == thread_id:
        return state.messages
    cached = state.message_cache.get(thread_id)
    if cached is not None:
        return cached
    try:
        from row_bot.ui.helpers import load_thread_messages

        messages = load_thread_messages(thread_id)
        state.message_cache[thread_id] = list(messages)
        state.message_cache_dirty.discard(thread_id)
        return messages
    except Exception:
        logger.debug("Could not load parent thread messages for Agent refresh", exc_info=True)
        return []


def _thread_has_attached_live_generation(thread_id: str) -> bool:
    gen = _active_generations.get(str(thread_id or ""))
    if gen is None:
        return False
    if str(getattr(gen, "status", "") or "").lower() != "streaming":
        return False
    if bool(getattr(gen, "orchestration_suspended", False)):
        return False
    return not bool(getattr(gen, "detached", False)) and getattr(gen, "live_row", None) is not None


def _render_live_agent_run_card(gen: GenerationState, run_row: dict | None) -> bool:
    run_id = str((run_row or {}).get("id") or "").strip()
    if not run_id or gen.detached or not gen.tool_col:
        return False
    if run_id in gen.live_agent_run_ids:
        return False
    try:
        with gen.tool_col:
            rendered = render_agent_run_cards([run_id])
    except Exception:
        logger.debug("Live delegated Agent card render failed", exc_info=True)
        return False
    if rendered:
        gen.live_agent_run_ids.add(run_id)
    return rendered


def _update_direct_agent_refresh_keys(messages: list[dict], run_ids: list[str]) -> tuple[bool, bool]:
    clean_ids = [str(run_id) for run_id in run_ids if str(run_id or "").strip()]
    if not clean_ids:
        return False, True
    key = _refresh_key_for_agent_run_ids(clean_ids)
    if not key:
        return False, False
    changed = False
    id_set = set(clean_ids)
    for msg in messages:
        msg_ids = {str(item) for item in (msg.get("agent_run_ids") or []) if str(item or "").strip()}
        if not msg_ids or not (msg_ids & id_set):
            continue
        if msg.get("agent_run_refresh_key") != key:
            msg["agent_run_refresh_key"] = key
            changed = True
    try:
        from row_bot.agent_runs import get_agent_run

        rows = [get_agent_run(run_id) for run_id in clean_ids]
        terminal = all(
            (not row) or str(row.get("status") or "") in _DIRECT_AGENT_TERMINAL_STATUSES
            for row in rows
        )
    except Exception:
        terminal = False
    return changed, terminal


def _direct_agent_start_ack(run_row: dict | None, request: Any) -> str:
    profile = str(
        (run_row or {}).get("profile_display_name")
        or (run_row or {}).get("profile_slug")
        or getattr(request, "profile_slug", "")
        or "Worker"
    ).strip()
    if profile:
        profile = profile[:1].upper() + profile[1:]
    else:
        profile = "Worker"
    return f"Started a {profile} agent for this. I'll keep this thread updated."


def _direct_agent_terminal_summary(run_row: dict | None) -> str:
    try:
        from row_bot.agent_run_messages import agent_run_terminal_summary

        return agent_run_terminal_summary(run_row)
    except Exception:
        return ""


def _checkpoint_ui_metadata(msg: dict) -> dict:
    metadata: dict = {}
    for key in (
        "timestamp",
        "turn_boundary",
        "agent_run_ids",
        "agent_run_refresh_key",
        "agent_lifecycle",
        "agent_completion_for",
        "agent_approval_for",
        "approval_request_id",
        "approval_resume_token",
        "approval_status",
        "channel_notification_key",
        "orchestration_id",
        "orchestration_message_kind",
        "goal_completion_for",
        "goal_run_id",
        "goal_status",
    ):
        if key in msg:
            metadata[key] = msg[key]
    return metadata


def _append_agent_completion_to_checkpoint(thread_id: str, run_row: dict, summary: str) -> None:
    if not thread_id or not run_row or not summary:
        return
    try:
        from row_bot.agent_run_messages import terminal_notification_key, terminal_ui_metadata
        from row_bot.channels.thread_notifications import append_parent_thread_message_once

        key = terminal_notification_key(run_row)
        append_parent_thread_message_once(
            thread_id=thread_id,
            key=key,
            text=summary,
            ui_metadata=terminal_ui_metadata(run_row, key=key),
        )
    except Exception:
        logger.debug("Could not append Agent completion to checkpoint", exc_info=True)


def _append_ui_messages_to_checkpoint(thread_id: str, messages: list[dict]) -> None:
    if not thread_id or not messages:
        return
    try:
        from langchain_core.messages import AIMessage, HumanMessage
        from row_bot.threads import append_checkpoint_messages

        checkpoint_messages: list = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip()
            content = str(msg.get("content") or "")
            metadata = _checkpoint_ui_metadata(msg)
            kwargs = {"row_bot_ui": metadata} if metadata else {}
            if role == "user":
                checkpoint_messages.append(HumanMessage(content=content, additional_kwargs=kwargs))
            elif role == "assistant":
                checkpoint_messages.append(AIMessage(content=content, additional_kwargs=kwargs))
        if checkpoint_messages:
            append_checkpoint_messages(thread_id, checkpoint_messages)
    except Exception:
        logger.debug("Could not append UI Agent messages to checkpoint", exc_info=True)


def _coalesce_agent_completion_message(messages: list[dict], run_id: str, summary: str) -> tuple[bool, bool]:
    """Return whether *summary* already exists, removing duplicate plain rows."""
    clean_run_id = str(run_id or "").strip()
    clean_summary = str(summary or "").strip()
    if not clean_run_id or not clean_summary:
        return False, False
    found = False
    changed = False
    compacted: list[dict] = []
    for msg in messages:
        if not (
            isinstance(msg, dict)
            and str(msg.get("role") or "") == "assistant"
            and str(msg.get("content") or "").strip() == clean_summary
        ):
            compacted.append(msg)
            continue
        completion_for = str(msg.get("agent_completion_for") or "").strip()
        if completion_for and completion_for != clean_run_id:
            compacted.append(msg)
            continue
        if found:
            changed = True
            continue
        found = True
        if msg.get("agent_completion_for") != clean_run_id:
            msg["agent_completion_for"] = clean_run_id
            changed = True
        compacted.append(msg)
    if len(compacted) != len(messages):
        messages[:] = compacted
    return found, changed


def _lifecycle_agent_run_ids(messages: list[dict]) -> set[str]:
    run_ids: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        lifecycle = msg.get("agent_lifecycle")
        if not isinstance(lifecycle, dict):
            continue
        kind = str(lifecycle.get("kind") or "")
        if kind not in {"direct_agent_spawn", "delegated_agent_spawn"}:
            continue
        run_id = str(lifecycle.get("run_id") or "").strip()
        if run_id:
            run_ids.add(run_id)
    return run_ids


def _append_child_agent_approval_messages(
    messages: list[dict],
    run_ids: list[str],
    *,
    checkpoint_thread_id: str = "",
) -> bool:
    clean_run_ids = list(
        dict.fromkeys(
            str(run_id).strip()
            for run_id in run_ids
            if str(run_id or "").strip()
        )
    )
    clean_ids = set(clean_run_ids)
    if not clean_ids:
        return False
    try:
        from row_bot.approval_messages import compact_message, payload_from_row
        from row_bot.tasks import get_pending_approval_for_agent_run
    except Exception:
        logger.debug("Could not import child Agent approval helpers", exc_info=True)
        return False

    existing_ids = {
        str(msg.get("approval_request_id") or "").strip()
        for msg in messages
        if isinstance(msg, dict)
    }
    changed = False
    handled_run_ids: set[str] = set()

    def _append_for_run(run_id: str, lifecycle: dict | None = None) -> bool:
        nonlocal changed
        if run_id not in clean_ids:
            return False
        try:
            approval = get_pending_approval_for_agent_run(run_id)
        except Exception:
            logger.debug("Could not load pending approval for Agent Run %s", run_id, exc_info=True)
            return False
        if not approval:
            return False
        approval_id = str(approval.get("id") or "").strip()
        if not approval_id:
            return False
        if approval_id in existing_ids:
            if lifecycle is not None:
                if lifecycle.get("approval_id") != approval_id:
                    lifecycle["approval_id"] = approval_id
                    changed = True
                if not lifecycle.get("approval_message_emitted"):
                    lifecycle["approval_message_emitted"] = True
                    changed = True
            return True

        payload = payload_from_row(approval)
        content = compact_message(payload) or str(approval.get("message") or "Approval required.")
        approval_msg = {
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now().strftime("%H:%M"),
            "agent_approval_for": run_id,
            "approval_request_id": approval_id,
            "approval_resume_token": str(approval.get("resume_token") or ""),
            "approval_status": "pending",
            "agent_run_ids": [run_id],
            "channel_notification_key": f"agent_approval:{approval_id}",
        }
        _insert_assistant_before_future_queued_turns(
            messages,
            approval_msg,
            current_generation_id=str((lifecycle or {}).get("source_generation_id") or ""),
        )
        if checkpoint_thread_id:
            try:
                from row_bot.channels.thread_notifications import (
                    notify_agent_run_approval,
                )

                notify_agent_run_approval(approval)
            except Exception:
                logger.debug(
                    "Could not repair child Agent approval checkpoint message",
                    exc_info=True,
                )
        if lifecycle is not None:
            lifecycle["approval_message_emitted"] = True
            lifecycle["approval_id"] = approval_id
        existing_ids.add(approval_id)
        changed = True
        return True

    for msg in list(messages):
        lifecycle = msg.get("agent_lifecycle")
        if not isinstance(lifecycle, dict):
            continue
        lifecycle_kind = str(lifecycle.get("kind") or "")
        if lifecycle_kind not in {"direct_agent_spawn", "delegated_agent_spawn"}:
            continue
        run_id = str(lifecycle.get("run_id") or "").strip()
        if run_id and _append_for_run(run_id, lifecycle):
            handled_run_ids.add(run_id)
    for msg in list(messages):
        if not isinstance(msg, dict) or str(msg.get("role") or "") != "assistant":
            continue
        if msg.get("approval_request_id"):
            continue
        for item in msg.get("agent_run_ids") or []:
            run_id = str(item or "").strip()
            if not run_id or run_id in handled_run_ids:
                continue
            if _append_for_run(run_id):
                handled_run_ids.add(run_id)
    # Retry attempts are durable child runs but do not have to appear on the
    # original spawn card. Reconcile every observed child id so replacement
    # approvals surface in the parent transcript too.
    for run_id in clean_run_ids:
        if run_id in handled_run_ids:
            continue
        if _append_for_run(run_id):
            handled_run_ids.add(run_id)
    return changed


def _merge_checkpoint_approval_messages(
    messages: list[dict],
    checkpoint_messages: list[dict],
    *,
    approval_statuses: Mapping[str, str] | None = None,
) -> bool:
    """Upsert only durable approval cards from an authoritative checkpoint."""

    from row_bot.ui.transcript import upsert_durable_transcript_message

    changed = False
    for incoming in checkpoint_messages:
        if not isinstance(incoming, dict) or not incoming.get("approval_request_id"):
            continue
        approval_id = str(incoming.get("approval_request_id") or "").strip()
        status = str((approval_statuses or {}).get(approval_id) or "").strip()
        if status:
            incoming = {**incoming, "approval_status": status}
            if status != "pending":
                incoming["approval_resume_token"] = ""
        item_changed, _index = upsert_durable_transcript_message(
            messages,
            incoming,
        )
        changed = changed or item_changed
    return changed


def _sync_thread_approval_messages(
    *,
    state: AppState,
    cb: _Callbacks,
    thread_id: str,
    run_ids: list[str],
    refresh_transcript: bool = False,
) -> bool:
    """Merge child rows plus parent/child checkpoint approval cards."""

    changed = _append_child_agent_approval_messages(
        state.messages,
        run_ids,
        checkpoint_thread_id=thread_id,
    )
    try:
        from row_bot.ui.helpers import load_thread_messages
        from row_bot.tasks import get_approval_request_statuses

        checkpoint_messages = load_thread_messages(thread_id)
        approval_ids = [
            str(message.get("approval_request_id") or "").strip()
            for message in checkpoint_messages
            if isinstance(message, dict)
            and str(message.get("approval_request_id") or "").strip()
        ]
        changed = (
            _merge_checkpoint_approval_messages(
                state.messages,
                checkpoint_messages,
                approval_statuses=get_approval_request_statuses(approval_ids),
            )
            or changed
        )
    except Exception:
        logger.debug("Could not reconcile thread checkpoint approvals", exc_info=True)
    if not changed:
        return False
    state.cache_active_messages()
    if refresh_transcript and state.thread_id == thread_id:
        try:
            cb.refresh_chat_messages()
        except Exception:
            logger.debug(
                "Live child Agent approval transcript refresh failed",
                exc_info=True,
            )
    return True


def _sync_child_agent_approval_messages(**kwargs: Any) -> bool:
    """Compatibility alias for older UI adapters."""

    return _sync_thread_approval_messages(**kwargs)


def _append_direct_agent_completion_messages(
    messages: list[dict],
    run_ids: list[str],
    *,
    checkpoint_thread_id: str = "",
) -> bool:
    clean_ids = {str(run_id) for run_id in run_ids if str(run_id or "").strip()}
    if not clean_ids:
        return False
    try:
        from row_bot.agent_runs import get_agent_run
    except Exception:
        logger.debug("Could not import Agent Run store for completion summary", exc_info=True)
        return False

    changed = False
    for msg in list(messages):
        lifecycle = msg.get("agent_lifecycle")
        if not isinstance(lifecycle, dict):
            continue
        lifecycle_kind = str(lifecycle.get("kind") or "")
        if lifecycle_kind not in {"direct_agent_spawn", "delegated_agent_spawn"}:
            continue
        run_id = str(lifecycle.get("run_id") or "").strip()
        if run_id not in clean_ids:
            continue
        if lifecycle_kind == "delegated_agent_spawn" and lifecycle.get("wait_mode"):
            continue
        try:
            run_row = get_agent_run(run_id)
        except Exception:
            logger.debug("Could not load Agent Run %s for completion summary", run_id, exc_info=True)
            continue
        summary = _direct_agent_terminal_summary(run_row)
        if not summary:
            continue
        exists, coalesced = _coalesce_agent_completion_message(messages, run_id, summary)
        changed = changed or coalesced
        if exists:
            if not lifecycle.get("completion_summary_emitted"):
                lifecycle["completion_summary_emitted"] = True
                changed = True
            completed_status = str((run_row or {}).get("status") or "")
            if lifecycle.get("completed_status") != completed_status:
                lifecycle["completed_status"] = completed_status
                changed = True
            continue
        if lifecycle.get("completion_summary_emitted"):
            continue
        lifecycle["completion_summary_emitted"] = True
        lifecycle["completed_status"] = str((run_row or {}).get("status") or "")
        completion_msg = {
            "role": "assistant",
            "content": summary,
            "timestamp": datetime.now().strftime("%H:%M"),
            "agent_completion_for": run_id,
        }
        _insert_assistant_before_future_queued_turns(
            messages,
            completion_msg,
            current_generation_id=str(lifecycle.get("source_generation_id") or ""),
        )
        if checkpoint_thread_id:
            _append_agent_completion_to_checkpoint(checkpoint_thread_id, run_row or {}, summary)
        changed = True
    return changed


def _is_async_delegated_agent_tool_result(tool_result: dict) -> bool:
    if not is_agent_tool_result(tool_result):
        return False
    name = str(tool_result.get("name") or "").strip().lower()
    if name not in {"delegate_work", "agents"}:
        return False
    payload = parse_agent_tool_payload(tool_result)
    if not isinstance(payload, dict):
        return False
    orchestration = payload.get("orchestration")
    if isinstance(orchestration, dict) and orchestration.get("required"):
        return False
    # wait=True returns "Child Agent completed." or a timeout message.  Only
    # the non-waiting spawn needs a later natural completion update.
    return str(payload.get("message") or "").strip().lower() == "child agent started."


def _append_async_delegated_agent_completion_messages(
    messages: list[dict],
    run_ids: list[str] | None = None,
    *,
    candidate_run_ids: list[str] | None = None,
    checkpoint_thread_id: str = "",
) -> bool:
    target_ids = {str(run_id) for run_id in (run_ids or []) if str(run_id or "").strip()}
    candidate_ids: list[str] = []
    seen_candidates: set[str] = set()

    def _add_candidate(run_id: object) -> None:
        clean = str(run_id or "").strip()
        if not clean:
            return
        try:
            from row_bot.agent_orchestrator import get_member_for_run

            member = get_member_for_run(clean)
            if member and (
                member.get("required")
                or str(member.get("status") or "") in {"retried", "retrying"}
            ):
                return
        except Exception:
            logger.debug(
                "Could not inspect orchestration membership for Agent completion",
                exc_info=True,
            )
        if target_ids and clean not in target_ids:
            return
        if clean not in seen_candidates:
            candidate_ids.append(clean)
            seen_candidates.add(clean)

    for run_id in candidate_run_ids or []:
        _add_candidate(run_id)

    for msg in messages:
        if not isinstance(msg, dict) or str(msg.get("role") or "") != "assistant":
            continue
        for tool_result in msg.get("tool_results") or []:
            if not (isinstance(tool_result, dict) and _is_async_delegated_agent_tool_result(tool_result)):
                continue
            for run_id in _ordered_agent_run_ids_from_tool_result(tool_result):
                _add_candidate(run_id)

    if not candidate_ids:
        return False

    wait_candidate_ids: set[str] = set()
    for msg in messages:
        if not isinstance(msg, dict) or str(msg.get("role") or "") != "assistant":
            continue
        wait_candidate_ids.update(_wait_delegated_run_ids_from_tool_results(msg.get("tool_results") or []))

    existing_completion_ids = {
        str(msg.get("agent_completion_for") or "").strip()
        for msg in messages
        if isinstance(msg, dict)
    }
    lifecycle_run_ids = _lifecycle_agent_run_ids(messages)

    changed = False
    try:
        from row_bot.agent_runs import get_agent_run
    except Exception:
        logger.debug("Could not import Agent Run store for async delegated completion", exc_info=True)
        return False

    for run_id in candidate_ids:
        if run_id in wait_candidate_ids:
            continue
        try:
            run_row = get_agent_run(run_id)
        except Exception:
            logger.debug("Could not load delegated Agent Run %s for completion", run_id, exc_info=True)
            continue
        summary = _direct_agent_terminal_summary(run_row)
        if not summary:
            continue
        exists, coalesced = _coalesce_agent_completion_message(messages, run_id, summary)
        changed = changed or coalesced
        if exists or run_id in existing_completion_ids or run_id in lifecycle_run_ids:
            continue
        completion_msg = {
            "role": "assistant",
            "content": summary,
            "timestamp": datetime.now().strftime("%H:%M"),
            "agent_completion_for": run_id,
        }
        _insert_assistant_before_future_queued_turns(messages, completion_msg)
        if checkpoint_thread_id:
            _append_agent_completion_to_checkpoint(checkpoint_thread_id, run_row or {}, summary)
        existing_completion_ids.add(run_id)
        changed = True
    return changed


def _schedule_direct_agent_card_refresh(
    *,
    state: AppState,
    cb: _Callbacks,
    thread_id: str,
    run_ids: list[str],
) -> None:
    clean_ids = [str(run_id) for run_id in run_ids if str(run_id or "").strip()]
    if not clean_ids:
        return
    attempts = {"count": 0}
    timer_ref: dict[str, Any] = {"timer": None}
    pending_transcript_refresh = {"value": False}

    def _tick() -> None:
        attempts["count"] += 1
        messages = _direct_agent_thread_messages(state, thread_id)
        changed, terminal = _update_direct_agent_refresh_keys(messages, clean_ids)
        approval_changed = _append_child_agent_approval_messages(
            messages,
            clean_ids,
            checkpoint_thread_id=thread_id,
        )
        summary_changed = _append_direct_agent_completion_messages(
            messages,
            clean_ids,
            checkpoint_thread_id=thread_id,
        )
        if changed or approval_changed or summary_changed:
            try:
                from row_bot.ui.helpers import persist_thread_media_state

                persist_thread_media_state(thread_id, messages)
            except Exception:
                logger.debug("Direct Agent card refresh persistence failed", exc_info=True)
            if state.thread_id == thread_id:
                state.messages = messages
                state.cache_active_messages()
                if _thread_has_attached_live_generation(thread_id):
                    pending_transcript_refresh["value"] = True
                else:
                    pending_transcript_refresh["value"] = False
                    try:
                        cb.refresh_chat_messages()
                    except Exception:
                        logger.debug("Direct Agent card transcript refresh failed", exc_info=True)
            else:
                state.message_cache[thread_id] = list(messages)
                state.message_cache_dirty.discard(thread_id)
        elif pending_transcript_refresh["value"] and state.thread_id == thread_id:
            if _thread_has_attached_live_generation(thread_id):
                pass
            else:
                pending_transcript_refresh["value"] = False
                try:
                    cb.refresh_chat_messages()
                except Exception:
                    logger.debug("Deferred Agent card transcript refresh failed", exc_info=True)
        _refresh_parent_agent_strip(cb)
        if (terminal and not pending_transcript_refresh["value"]) or attempts["count"] >= 240:
            timer_obj = timer_ref.get("timer")
            if timer_obj is not None:
                try:
                    timer_obj.deactivate()
                except Exception:
                    logger.debug("Direct Agent card refresh timer deactivate failed", exc_info=True)

    try:
        from row_bot.ui.timer_utils import safe_timer

        timer_ref["timer"] = safe_timer(1.0, _tick)
    except Exception:
        logger.debug("Direct Agent card refresh scheduling failed", exc_info=True)


def _agent_runs_terminal(run_ids: list[str]) -> bool:
    clean_ids = [str(run_id) for run_id in run_ids if str(run_id or "").strip()]
    if not clean_ids:
        return True
    try:
        from row_bot.agent_runs import get_agent_run

        rows = [get_agent_run(run_id) for run_id in clean_ids]
        return all(
            (not row) or str(row.get("status") or "") in _DIRECT_AGENT_TERMINAL_STATUSES
            for row in rows
        )
    except Exception:
        logger.debug("Could not determine Agent Run terminal state", exc_info=True)
        return False


def _schedule_agent_tool_result_card_refresh(
    *,
    state: AppState,
    cb: Any,
    thread_id: str,
    run_ids: list[str],
    async_completion_run_ids: list[str] | None = None,
) -> None:
    clean_ids = [str(run_id) for run_id in run_ids if str(run_id or "").strip()]
    if not clean_ids:
        return
    clean_async_completion_ids = [
        str(run_id)
        for run_id in (async_completion_run_ids or [])
        if str(run_id or "").strip()
    ]
    attempts = {"count": 0}
    timer_ref: dict[str, Any] = {"timer": None}
    last_key = {"value": _refresh_key_for_agent_run_ids(clean_ids)}
    pending_transcript_refresh = {"value": False}

    def _tick() -> None:
        attempts["count"] += 1
        key = _refresh_key_for_agent_run_ids(clean_ids)
        changed = bool(key and key != last_key.get("value"))
        if key:
            last_key["value"] = key
        terminal = _agent_runs_terminal(clean_ids)
        summary_changed = False
        messages = _direct_agent_thread_messages(state, thread_id)
        approval_changed = _append_child_agent_approval_messages(
            messages,
            clean_ids,
            checkpoint_thread_id=thread_id,
        )
        if terminal:
            summary_changed = _append_async_delegated_agent_completion_messages(
                messages,
                clean_ids,
                candidate_run_ids=clean_async_completion_ids,
                checkpoint_thread_id=thread_id,
            )
            if approval_changed or summary_changed:
                try:
                    from row_bot.ui.helpers import persist_thread_media_state

                    persist_thread_media_state(thread_id, messages)
                except Exception:
                    logger.debug("Async delegated completion persistence failed", exc_info=True)
                if state.thread_id == thread_id:
                    state.messages = messages
                    state.cache_active_messages()
                else:
                    state.message_cache[thread_id] = list(messages)
                    state.message_cache_dirty.discard(thread_id)
        elif approval_changed:
            try:
                from row_bot.ui.helpers import persist_thread_media_state

                persist_thread_media_state(thread_id, messages)
            except Exception:
                logger.debug("Async delegated approval persistence failed", exc_info=True)
            if state.thread_id == thread_id:
                state.messages = messages
                state.cache_active_messages()
            else:
                state.message_cache[thread_id] = list(messages)
                state.message_cache_dirty.discard(thread_id)
        if (changed or terminal or approval_changed or summary_changed) and state.thread_id == thread_id:
            if _thread_has_attached_live_generation(thread_id):
                pending_transcript_refresh["value"] = True
            else:
                pending_transcript_refresh["value"] = False
                try:
                    refresh = getattr(cb, "refresh_chat_messages", None)
                    if callable(refresh):
                        refresh()
                except Exception:
                    logger.debug("Agent tool-result card transcript refresh failed", exc_info=True)
            if terminal:
                try:
                    rebuild = getattr(cb, "rebuild_main", None)
                    if callable(rebuild) and not _thread_has_attached_live_generation(thread_id):
                        try:
                            rebuild(immediate=True, reason="agent_tool_result_terminal")
                        except TypeError:
                            rebuild()
                except Exception:
                    logger.debug("Agent tool-result terminal main refresh failed", exc_info=True)
        elif pending_transcript_refresh["value"] and state.thread_id == thread_id:
            if not _thread_has_attached_live_generation(thread_id):
                pending_transcript_refresh["value"] = False
                try:
                    refresh = getattr(cb, "refresh_chat_messages", None)
                    if callable(refresh):
                        refresh()
                except Exception:
                    logger.debug("Deferred Agent tool-result transcript refresh failed", exc_info=True)
        _refresh_parent_agent_strip(cb)
        if (terminal and not pending_transcript_refresh["value"]) or attempts["count"] >= 240:
            timer_obj = timer_ref.get("timer")
            if timer_obj is not None:
                try:
                    timer_obj.deactivate()
                except Exception:
                    logger.debug("Agent tool-result card refresh timer deactivate failed", exc_info=True)

    try:
        from row_bot.ui.timer_utils import safe_timer

        timer_ref["timer"] = safe_timer(1.0, _tick)
    except Exception:
        logger.debug("Agent tool-result card refresh scheduling failed", exc_info=True)


def _delegated_agent_start_ack(run_row: dict | None) -> str:
    profile = str(
        (run_row or {}).get("profile_display_name")
        or (run_row or {}).get("profile_slug")
        or (run_row or {}).get("kind")
        or "Agent"
    ).strip()
    if profile:
        profile = profile[:1].upper() + profile[1:]
    else:
        profile = "Agent"
    return f"Started a {profile} agent for this. I'll keep this thread updated."


def _append_delegated_agent_card_message(
    *,
    state: AppState,
    cb: _Callbacks,
    thread_id: str,
    run_row: dict,
    generation_id: str = "",
    queued_message_ids: list[str] | None = None,
    wait_mode: bool = False,
    orchestration_id: str = "",
    required: bool = False,
) -> bool:
    run_id = str((run_row or {}).get("id") or "").strip()
    if not thread_id or not run_id:
        return False
    messages = _direct_agent_thread_messages(state, thread_id)
    if run_id in _visible_agent_run_ids(messages):
        metadata_changed = False
        for existing in messages:
            if run_id not in {
                str(item).strip()
                for item in (existing.get("agent_run_ids") or [])
                if str(item or "").strip()
            }:
                continue
            lifecycle = existing.get("agent_lifecycle")
            if not isinstance(lifecycle, dict):
                lifecycle = {
                    "kind": "delegated_agent_spawn",
                    "run_id": run_id,
                    "source_generation_id": str(generation_id or ""),
                    "wait_mode": bool(wait_mode),
                    "completion_summary_emitted": False,
                }
                existing["agent_lifecycle"] = lifecycle
                metadata_changed = True
            if orchestration_id:
                if lifecycle.get("orchestration_id") != str(orchestration_id):
                    lifecycle["orchestration_id"] = str(orchestration_id)
                    metadata_changed = True
                if lifecycle.get("required") is not bool(required):
                    lifecycle["required"] = bool(required)
                    metadata_changed = True
            refresh_key = _agent_run_card_refresh_key(run_row)
            if existing.get("agent_run_refresh_key") != refresh_key:
                existing["agent_run_refresh_key"] = refresh_key
                metadata_changed = True
        if metadata_changed:
            if state.thread_id == thread_id:
                state.messages = messages
                state.cache_active_messages()
            else:
                state.message_cache[thread_id] = list(messages)
                state.message_cache_dirty.discard(thread_id)
        return False
    msg = {
        "role": "assistant",
        "content": _delegated_agent_start_ack(run_row),
        "timestamp": datetime.now().strftime("%H:%M"),
        "agent_run_ids": [run_id],
        "agent_run_refresh_key": _agent_run_card_refresh_key(run_row),
        "agent_lifecycle": {
            "kind": "delegated_agent_spawn",
            "run_id": run_id,
            "source_generation_id": str(generation_id or ""),
            "orchestration_id": str(orchestration_id or ""),
            "required": bool(required),
            "wait_mode": bool(wait_mode),
            "completion_summary_emitted": False,
        },
    }
    _insert_assistant_before_future_queued_turns(
        messages,
        msg,
        current_queued_ids=queued_message_ids,
        current_generation_id=generation_id,
    )
    try:
        from row_bot.threads import touch_thread
        from row_bot.ui.helpers import persist_thread_media_state

        persist_thread_media_state(thread_id, messages)
        touch_thread(thread_id)
    except Exception:
        logger.debug("Delegated Agent card persistence failed", exc_info=True)
    if state.thread_id == thread_id:
        state.messages = messages
        state.cache_active_messages()
        has_live_generation = _thread_has_attached_live_generation(thread_id)
        if has_live_generation:
            insert_before_live_row = getattr(
                cb,
                "insert_chat_message_before_live_row",
                None,
            )
            live_generation = _active_generations.get(thread_id)
            live_row = getattr(live_generation, "live_row", None)
            if callable(insert_before_live_row) and live_row is not None:
                try:
                    insert_before_live_row(msg, live_row)
                except Exception:
                    logger.debug(
                        "Delegated Agent card live transcript insertion failed",
                        exc_info=True,
                    )
        else:
            refresh = getattr(cb, "refresh_chat_messages", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    logger.debug(
                        "Delegated Agent card transcript refresh failed",
                        exc_info=True,
                    )
    else:
        state.message_cache[thread_id] = list(messages)
        state.message_cache_dirty.discard(thread_id)
    _schedule_direct_agent_card_refresh(
        state=state,
        cb=cb,
        thread_id=thread_id,
        run_ids=[run_id],
    )
    _schedule_parent_agent_strip_refresh(cb)
    return True


def _register_delegated_agent_tool_result(
    gen: GenerationState,
    state: AppState,
    cb: _Callbacks,
    *,
    raw_tool_name: str,
    tool_content: str,
) -> list[str]:
    """Register authoritative delegated run ids at the tool completion boundary."""

    tool_key = str(raw_tool_name or "").strip().lower()
    if tool_key not in {"delegate_work", "agents"}:
        return []
    tool_result = {"name": tool_key, "content": tool_content}
    payload = parse_agent_tool_payload(tool_result)
    if not isinstance(payload, dict) or payload.get("ok") is False:
        return []
    run_ids = _ordered_agent_run_ids_from_tool_result(tool_result)
    if not run_ids:
        return []
    orchestration = payload.get("orchestration")
    orchestration_id = ""
    required = False
    if isinstance(orchestration, dict):
        orchestration_id = str(orchestration.get("id") or "").strip()
        required = bool(orchestration_id and orchestration.get("required"))
    wait_mode = (
        str(payload.get("message") or "").strip().lower()
        != "child agent started."
    )
    try:
        from row_bot.agent_runs import get_agent_run
        from row_bot.channels.thread_notifications import (
            append_agent_lifecycle_message_once,
        )
    except Exception:
        logger.debug("Could not import delegated Agent registration helpers", exc_info=True)
        return []

    registered: list[str] = []
    for run_id in run_ids:
        try:
            run_row = get_agent_run(run_id)
        except Exception:
            logger.debug(
                "Could not load delegated Agent Run %s at tool completion",
                run_id,
                exc_info=True,
            )
            continue
        if not run_row:
            continue
        append_agent_lifecycle_message_once(
            run_row,
            source_generation_id=str(gen.generation_id or ""),
            orchestration_id=orchestration_id,
            required=required,
            wait_mode=wait_mode,
        )
        if not wait_mode:
            gen.live_async_agent_run_ids.add(run_id)
        _render_live_agent_run_card(gen, run_row)
        _append_delegated_agent_card_message(
            state=state,
            cb=cb,
            thread_id=str(gen.thread_id or ""),
            run_row=run_row,
            generation_id=str(gen.generation_id or ""),
            queued_message_ids=list(gen.queued_message_ids or []),
            wait_mode=wait_mode,
            orchestration_id=orchestration_id,
            required=required,
        )
        registered.append(run_id)
    return registered


def _republish_required_orchestration_lifecycle(
    orchestration_id: str,
    *,
    source_generation_id: str,
) -> int:
    """Ensure required child cards remain visible beside parent progress."""

    clean_orchestration_id = str(orchestration_id or "").strip()
    if not clean_orchestration_id:
        return 0
    try:
        from row_bot.agent_orchestrator import list_members
        from row_bot.channels.thread_notifications import (
            append_agent_lifecycle_message_once,
        )

        published = 0
        for member in list_members(clean_orchestration_id):
            if not member.get("required") or str(member.get("status") or "") == "transferred":
                continue
            run_row = member.get("run") or {}
            if not isinstance(run_row, dict) or not str(run_row.get("id") or "").strip():
                continue
            if append_agent_lifecycle_message_once(
                run_row,
                source_generation_id=str(source_generation_id or ""),
                orchestration_id=clean_orchestration_id,
                required=True,
                wait_mode=False,
            ):
                published += 1
        return published
    except Exception:
        logger.debug(
            "Required orchestration lifecycle checkpoint repair failed",
            exc_info=True,
        )
        return 0


def _agent_tool_result_already_live(gen: GenerationState, tool_result: dict) -> bool:
    if not is_agent_tool_result(tool_result):
        return False
    run_ids = _agent_run_ids_from_tool_result(tool_result)
    return bool(run_ids) and run_ids <= set(getattr(gen, "live_agent_run_ids", set()))


def _schedule_delegated_agent_card_probe(
    gen: GenerationState,
    state: AppState,
    cb: _Callbacks,
    *,
    tool_call_id: str = "",
    wait_mode: bool = False,
) -> None:
    del tool_call_id
    parent_thread_id = str(gen.thread_id or "")
    if not parent_thread_id:
        return
    messages = _direct_agent_thread_messages(state, parent_thread_id)
    baseline_ids = set(_visible_agent_run_ids(messages))
    try:
        from row_bot.agent_runs import list_agent_runs

        baseline_ids.update(
            str(run.get("id") or "").strip()
            for run in list_agent_runs(parent_thread_id=parent_thread_id, kind="subagent", limit=50)
            if str(run.get("id") or "").strip()
        )
    except Exception:
        logger.debug("Could not capture delegated Agent probe baseline", exc_info=True)

    attempts = {"count": 0}
    timer_ref: dict[str, Any] = {"timer": None}

    def _deactivate() -> None:
        timer_obj = timer_ref.get("timer")
        if timer_obj is not None:
            try:
                timer_obj.deactivate()
            except Exception:
                logger.debug("Delegated Agent card probe deactivate failed", exc_info=True)

    def _tick() -> None:
        attempts["count"] += 1
        try:
            from row_bot.agent_runs import list_agent_runs

            current_messages = _direct_agent_thread_messages(state, parent_thread_id)
            visible_ids = _visible_agent_run_ids(current_messages)
            runs = list_agent_runs(parent_thread_id=parent_thread_id, kind="subagent", limit=20)
            appended = False
            for run_row in reversed(runs):
                run_id = str(run_row.get("id") or "").strip()
                if not run_id or run_id in baseline_ids or run_id in visible_ids:
                    continue
                if not wait_mode:
                    gen.live_async_agent_run_ids.add(run_id)
                _render_live_agent_run_card(gen, run_row)
                appended = _append_delegated_agent_card_message(
                    state=state,
                    cb=cb,
                    thread_id=parent_thread_id,
                    run_row=run_row,
                    generation_id=gen.generation_id,
                    queued_message_ids=gen.queued_message_ids,
                    wait_mode=wait_mode,
                ) or appended
                visible_ids.add(run_id)
            if appended:
                _deactivate()
                return
        except Exception:
            logger.debug("Delegated Agent card probe tick failed", exc_info=True)
        if attempts["count"] >= 60 or _active_generations.get(parent_thread_id) is not gen:
            _deactivate()

    try:
        from row_bot.ui.timer_utils import safe_timer

        timer_ref["timer"] = safe_timer(0.5, _tick)
    except Exception:
        logger.debug("Delegated Agent card probe scheduling failed", exc_info=True)


async def _handle_direct_agent_spawn(
    request: Any,
    *,
    state: AppState,
    p: P,
    cb: _Callbacks,
    original_text: str,
    enabled_tool_names: list[str],
    active_generation_id: str = "",
) -> None:
    from row_bot.agent_commands import spawn_agent_from_request
    from row_bot.threads import touch_thread
    from row_bot.ui.helpers import persist_thread_media_state

    parent_thread_id = str(state.thread_id or "")
    if not parent_thread_id:
        return
    parent_messages = state.messages
    timestamp = datetime.now().strftime("%H:%M")
    user_msg: dict = {
        "role": "user",
        "content": str(original_text or "").strip(),
        "timestamp": timestamp,
    }
    if active_generation_id:
        user_msg["turn_boundary"] = {"after_generation_id": active_generation_id}
    run_id = ""
    try:
        run_row = await run.io_bound(
            lambda: spawn_agent_from_request(
                parent_thread_id,
                request,
                enabled_tool_names=enabled_tool_names,
                developer_workspace_id=str(
                    getattr(state, "active_developer_workspace_id", None) or ""
                ),
            )
        )
        run_id = str((run_row or {}).get("id") or "").strip()
        assistant_msg: dict = {
            "role": "assistant",
            "content": _direct_agent_start_ack(run_row, request),
            "timestamp": timestamp,
            "agent_run_ids": [run_id] if run_id else [],
            "agent_run_refresh_key": _agent_run_card_refresh_key(run_row),
            "agent_lifecycle": {
                "kind": "direct_agent_spawn",
                "run_id": run_id,
                "completion_summary_emitted": False,
            },
        }
    except Exception as exc:
        logger.warning("Direct Agent spawn failed: %s", exc, exc_info=True)
        assistant_msg = {
            "role": "assistant",
            "content": f"Could not start Agent: {exc}",
            "timestamp": timestamp,
        }

    parent_messages.extend([user_msg, assistant_msg])
    _append_ui_messages_to_checkpoint(parent_thread_id, [user_msg, assistant_msg])
    persist_thread_media_state(parent_thread_id, parent_messages)
    if state.thread_id == parent_thread_id:
        state.cache_active_messages()
        if cb.add_chat_message:
            cb.add_chat_message(user_msg)
            cb.add_chat_message(assistant_msg)
    else:
        state.message_cache[parent_thread_id] = list(parent_messages)
        state.message_cache_dirty.discard(parent_thread_id)
    touch_thread(parent_thread_id)
    _schedule_parent_agent_strip_refresh(cb)
    if run_id:
        _schedule_direct_agent_card_refresh(
            state=state,
            cb=cb,
            thread_id=parent_thread_id,
            run_ids=[run_id],
        )
        try:
            ui.notify("Agent started.", type="info", close_button=True, timeout=3000)
        except Exception:
            logger.debug("Direct Agent spawn notification failed", exc_info=True)
    try:
        cb.rebuild_thread_list()
    except Exception:
        logger.debug("Direct Agent spawn thread list refresh failed", exc_info=True)
    if state.thread_id == parent_thread_id and p.chat_scroll:
        try:
            p.chat_scroll.scroll_to(percent=1.0)
        except Exception:
            logger.debug("Direct Agent spawn scroll failed", exc_info=True)


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
    from row_bot.agent import get_agent_graph, repair_orphaned_tool_calls
    from row_bot.buddy.events import BuddyEventType, emit_buddy_event
    from langchain_core.messages import AIMessage
    from row_bot.ui.helpers import (
        attach_thinking_to_message,
        persist_detached_thread_media,
        persist_thread_media_state,
    )

    _stopped_shown = False
    _last_buddy_token_at = 0.0
    _consume_started = time.perf_counter()
    if not gen.generation_id:
        gen.generation_id = f"{gen.thread_id}:{id(gen)}"
    _stream_updates = 0
    _answer_stream_updates = 0
    _thinking_stream_updates = 0

    generation_scope_id = gen.generation_id
    realtime_chunker = None
    realtime_speech_queue = None
    if gen.voice_mode and state.voice_coordinator.transport == "realtime":
        from row_bot.voice.realtime_presenter import RealtimeSpeechChunker, RealtimeSpeechQueue

        realtime_chunker = RealtimeSpeechChunker()
        realtime_speech_queue = RealtimeSpeechQueue()
    realtime_last_voice_attempt_at = _consume_started
    realtime_last_queue_flush_at = -999.0
    realtime_tool_events_since_cue = 0
    realtime_tool_done_since_cue = 0

    def _mark_realtime_voice_attempt(origin: str, text: str) -> None:
        nonlocal realtime_last_voice_attempt_at
        if gen.voice_mode and state.voice_coordinator.transport == "realtime" and str(text or "").strip():
            realtime_last_voice_attempt_at = time.perf_counter()
            _voice_diag("realtime_voice_attempt", origin=origin, chars=len(str(text or "").strip()))

    def _speak_realtime_text(text: str, *, origin: str = "final") -> bool:
        from row_bot.voice.realtime_client import send_realtime_function_output_js, send_realtime_run_event_js

        if state.voice_coordinator.transport == "browser":
            from row_bot.voice.browser_client import speak_browser_voice_js

            return run_realtime_client_js(
                p,
                speak_browser_voice_js(text),
                context=f"browser_voice_{origin}",
            )
        if (
            origin == "final"
            and realtime_speech_queue is not None
        ):
            decision_basis = gen.accumulated if str(gen.accumulated or "").strip() else text
            decision = realtime_speech_queue.final_decision(decision_basis)
            if not decision.get("speak"):
                gen.realtime_stream_finalized = True
                _voice_diag("realtime_final_suppressed_after_stream", **decision)
                if gen.realtime_tool_call_id and not gen.realtime_tool_output_sent:
                    gen.realtime_tool_output_sent = True
                    code = send_realtime_function_output_js(
                        call_id=gen.realtime_tool_call_id,
                        output={
                            "status": "completed",
                            "speakable": "",
                            "worker": "row_bot",
                        },
                        thread_id=gen.thread_id,
                        generation_id=generation_scope_id,
                        silent=True,
                    )
                    sent = run_realtime_client_js(p, code, context="realtime_function_output_silent_after_stream")
                    _voice_diag("realtime_function_output_silent_after_stream", sent=sent)
                return False
        if origin == "final" and (gen.realtime_tool_call_id or gen.realtime_forced_consult):
            if gen.realtime_tool_output_sent:
                return False
            gen.realtime_tool_output_sent = True
            if gen.realtime_tool_call_id:
                code = send_realtime_function_output_js(
                    call_id=gen.realtime_tool_call_id,
                    output={
                        "status": "completed",
                        "speakable": text,
                        "worker": "row_bot",
                    },
                    thread_id=gen.thread_id,
                    generation_id=generation_scope_id,
                    silent=False,
                )
                context = "realtime_function_output"
            else:
                if gen.realtime_streamed_speech_chunks > 0:
                    gen.realtime_stream_finalized = True
                    return False
                code = send_realtime_run_event_js(
                    text,
                    origin="forced_consult_result",
                    thread_id=gen.thread_id,
                    generation_id=generation_scope_id,
                )
                context = "realtime_forced_consult_result"
            sent = run_realtime_client_js(p, code, context=context)
            if sent:
                _mark_realtime_voice_attempt(origin, text)
            return sent
        sent = run_realtime_client_js(
            p,
            send_realtime_run_event_js(
                text,
                origin=origin,
                thread_id=gen.thread_id,
                generation_id=generation_scope_id,
            ),
            context="realtime_run_event",
        )
        if sent:
            _mark_realtime_voice_attempt(origin, text)
        return sent

    def _speak_realtime_stream_chunk(text: str) -> None:
        clean = str(text or "").strip()
        if not clean:
            return
        if realtime_speech_queue is not None:
            playback_active = bool(getattr(state.voice_coordinator, "playback_active", False))
            queued = realtime_speech_queue.offer_stream_chunk(clean, playback_active=playback_active)
            if not queued:
                _voice_diag(
                    "realtime_stream_chunk_queued",
                    chunks=gen.realtime_streamed_speech_chunks,
                    chars=len(clean),
                    playback_active=playback_active,
                    spoken_stream_chars=realtime_speech_queue.spoken_stream_chars,
                    coalesced_chars=len(realtime_speech_queue.coalesced_stream_text),
                )
                return
            clean = queued
        if _speak_realtime_text(clean, origin="stream_chunk"):
            gen.realtime_streamed_speech_chunks += 1
            if gen.realtime_streamed_speech_chunks == 1:
                state.voice_coordinator.mark_realtime_latency("first_speakable_chunk")
                state.voice_coordinator.mark_realtime_latency("first_substantive_audio_requested")
            _voice_diag(
                "realtime_stream_chunk_spoken",
                chunks=gen.realtime_streamed_speech_chunks,
                chars=len(clean),
            )

    def _flush_realtime_speech_queue(reason: str) -> bool:
        nonlocal realtime_last_queue_flush_at
        if realtime_speech_queue is None or not gen.tts_active:
            return False
        if bool(getattr(state.voice_coordinator, "playback_active", False)):
            return False
        now = time.perf_counter()
        if now - realtime_last_queue_flush_at < 1.2:
            return False
        chunk = realtime_speech_queue.flush_queued(playback_active=False)
        if not chunk:
            return False
        realtime_last_queue_flush_at = now
        if _speak_realtime_text(chunk, origin="stream_chunk"):
            gen.realtime_streamed_speech_chunks += 1
            _voice_diag(
                "realtime_stream_chunk_flushed",
                reason=reason,
                chunks=gen.realtime_streamed_speech_chunks,
                chars=len(chunk),
                spoken_stream_chars=realtime_speech_queue.spoken_stream_chars,
                coalesced_chars=len(realtime_speech_queue.coalesced_stream_text),
            )
            return True
        return False

    voice_output = VoiceOutputController.for_generation(
        voice_mode=gen.voice_mode,
        transport=state.voice_coordinator.transport,
        tts_service=state.tts_service,
        realtime_speaker=_speak_realtime_text,
        now=time.perf_counter,
    )

    def _generation_elapsed() -> float:
        return max(0.0, time.perf_counter() - _consume_started)

    def _voice_diag(stage: str, **extra: Any) -> None:
        if not gen.voice_mode and state.voice_coordinator.transport != "realtime":
            return
        active_gen = _active_generations.get(state.thread_id)
        snapshot = state.voice_coordinator.diagnostic_snapshot()
        snapshot.update({
            "stage": stage,
            "voice_enabled": state.voice_enabled,
            "voice_input_mode": state.voice_input_mode,
            "thread_id": state.thread_id,
            "generation_thread_id": gen.thread_id,
            "generation_id": generation_scope_id,
            "generation_status": gen.status,
            "generation_voice_mode": gen.voice_mode,
            "generation_tts_active": gen.tts_active,
            "active_generation_status": getattr(active_gen, "status", None),
            **extra,
        })
        logger.info("voice.realtime.pipeline %s", snapshot)

    def _maybe_speak_realtime_progress(reason: str) -> bool:
        nonlocal realtime_tool_events_since_cue, realtime_tool_done_since_cue
        if not (gen.voice_mode and state.voice_coordinator.transport == "realtime"):
            return False
        if _stopped_shown or gen.status != "streaming":
            return False
        if bool(getattr(state.voice_coordinator, "playback_active", False)):
            return False
        if str(getattr(state.voice_coordinator, "state", "")).lower() in {"user_speaking", "speaking"}:
            return False

        elapsed = _generation_elapsed()
        quiet_for = time.perf_counter() - realtime_last_voice_attempt_at
        if quiet_for < 9.0:
            return False

        cue = None
        cue_reason = ""
        if realtime_tool_done_since_cue >= 2 and quiet_for >= 9.0:
            cue = results_found_cue()
            cue_reason = "tool_results_ready"
        elif realtime_tool_events_since_cue > 0 and quiet_for >= 12.0:
            cue = tool_progress_cue()
            cue_reason = "tools_still_running"
        elif not gen.first_content:
            cue = heard_cue() if elapsed < 3.0 else (thinking_cue() if elapsed < 10.0 else long_running_cue())
            cue_reason = "awaiting_first_content"
        elif quiet_for >= 18.0:
            cue = long_running_cue()
            cue_reason = "quiet_after_content"

        if cue is None:
            return False
        spoke = voice_output.speak_cue(cue, generation_elapsed=elapsed)
        _voice_diag(
            "realtime_cue_spoken" if spoke else "realtime_cue_suppressed",
            reason=reason,
            cue_type=str(getattr(cue.type, "value", cue.type)),
            cue_reason=cue_reason,
            quiet_for=round(quiet_for, 3),
            elapsed=round(elapsed, 3),
        )
        _voice_diag(
            "realtime_silence_watchdog_tick",
            reason=reason,
            cue_reason=cue_reason,
            spoke=spoke,
            quiet_for=round(quiet_for, 3),
            elapsed=round(elapsed, 3),
            tool_events_since_cue=realtime_tool_events_since_cue,
            tool_done_since_cue=realtime_tool_done_since_cue,
        )
        if spoke and cue.type.value in {"tool_start", "tool_progress"}:
            realtime_tool_events_since_cue = 0
            realtime_tool_done_since_cue = 0
        return spoke

    def _set_realtime_generation_state(state_name: str, *, detail: str = "") -> None:
        if gen.voice_mode and state.voice_coordinator.transport == "realtime":
            state.voice_coordinator.set_realtime_state(state_name, detail=detail)
            _voice_diag(f"coordinator_state:{state_name}", detail=detail)

    def _buddy(event_type: BuddyEventType, label: str = "", **payload: Any) -> None:
        if label:
            payload["label"] = label
        try:
            emit_buddy_event(event_type, source="ui.streaming", payload={"thread_id": gen.thread_id, **payload})
        except Exception:
            logger.debug("Buddy event emit failed", exc_info=True)

    def _render_answer_content(content: str) -> None:
        if gen.assistant_md:
            gen.assistant_md.set_content(content)

    answer_batcher = LiveMarkdownBatcher(
        _render_answer_content,
        formatter=_format_assistant_markdown,
        now=time.perf_counter,
    )

    def _render_thinking_content(content: str) -> None:
        if gen.thinking_label:
            gen.thinking_label.delete()
            gen.thinking_label = None
        if gen.thinking_collapsed:
            if gen.thinking_code:
                gen.thinking_code.set_content(str(content or "").strip()[:8_000])
            return
        if gen.thinking_md is None and gen.wrapper:
            with gen.wrapper:
                gen.thinking_md = ui.markdown(
                    "", extras=["code-friendly", "fenced-code-blocks"]
                ).classes("row-bot-msg w-full").style(
                    "opacity: 0.55; font-size: 0.88rem; font-style: italic;"
                )
        if gen.thinking_md and not gen.thinking_collapsed:
            gen.thinking_md.set_content(content)

    thinking_batcher = LiveMarkdownBatcher(_render_thinking_content, now=time.perf_counter)

    def _update_answer_render(reason: str, *, force: bool = False) -> bool:
        if gen.detached or not gen.assistant_md:
            return False
        try:
            return answer_batcher.update(gen.accumulated, force=force)
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, reason)
            logger.debug("%s failed", reason, exc_info=True)
            return False

    def _flush_answer_render(reason: str, *, force: bool = False) -> bool:
        if gen.detached or not gen.assistant_md:
            return False
        try:
            return answer_batcher.flush(force=force)
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, reason)
            logger.debug("%s failed", reason, exc_info=True)
            return False

    def _update_thinking_render(reason: str, *, force: bool = False) -> bool:
        if gen.detached:
            return False
        try:
            return thinking_batcher.update(gen.thinking_text, force=force)
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, reason)
            logger.debug("%s failed", reason, exc_info=True)
            return False

    def _flush_thinking_render(reason: str, *, force: bool = False) -> bool:
        if gen.detached:
            return False
        try:
            return thinking_batcher.flush(force=force)
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, reason)
            logger.debug("%s failed", reason, exc_info=True)
            return False

    def _flush_live_renders(reason: str, *, force: bool = False) -> None:
        _flush_answer_render(f"{reason} answer render", force=force)
        _flush_thinking_render(f"{reason} thinking render", force=force)

    def _show_stopped(reason: str) -> None:
        nonlocal _stopped_shown
        if _stopped_shown:
            return
        _stopped_shown = True
        gen.status = "stopped"
        _emit_buddy_terminal_stop(gen, reason)
        gen.stopped_marker_rendered = True
        _set_realtime_generation_state("stopped", detail=reason)
        if "*[Stopped]*" not in str(gen.accumulated or ""):
            gen.accumulated += "\n\n*[Stopped]*"
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
                _flush_live_renders("stop-handling", force=True)
            except Exception as exc:
                _handle_ui_runtime_error(gen, state, exc, "stop-handling UI cleanup")
                logger.debug("Stop-handling UI cleanup failed", exc_info=True)
        if gen.tts_active:
            try:
                state.tts_service.stop()
            except Exception:
                logger.debug("TTS stop failed during generation stop", exc_info=True)
            gen.tts_active = False

    _buddy(BuddyEventType.GENERATION_STARTED, "Thinking")
    _set_realtime_generation_state("thinking", detail="generation_started")
    _voice_diag("generation_consumer_started")
    if gen.voice_mode and state.voice_coordinator.transport == "realtime":
        state.voice_coordinator.set_active_row_bot_generation(generation_scope_id)

    try:
      while True:
        _detach_if_thread_changed(gen, state, "active thread changed")
        # ── Stop handling ────────────────────────────────────────────
        if gen.stop_event.is_set() and not _stopped_shown:
            _show_stopped("stop_event")
            break

        try:
            event = gen.q.get_nowait()
        except queue.Empty:
            _empty_wait_started = time.perf_counter()
            if not gen.detached:
                _flush_live_renders("queue-empty live render")
            if gen.voice_mode and state.voice_coordinator.transport == "realtime":
                if not _flush_realtime_speech_queue("queue_empty"):
                    _maybe_speak_realtime_progress("queue_empty")
            if (
                not gen.first_content
                and not _stopped_shown
                and gen.status == "streaming"
                and state.voice_coordinator.transport != "realtime"
            ):
                elapsed = _generation_elapsed()
                voice_output.speak_cue(
                    heard_cue() if elapsed < 3.0 else (thinking_cue() if elapsed < 10.0 else long_running_cue()),
                    generation_elapsed=elapsed,
                )
            await asyncio.sleep(0.05)
            gen.queue_empty_wait_ms += (time.perf_counter() - _empty_wait_started) * 1000.0
            continue

        if event is None:
            break

        if _stopped_shown:
            continue

        event_type, payload = event
        if event_type == "stopped":
            _show_stopped("stopped_event")
            break
        event_seen_at = time.perf_counter()
        if not gen.first_producer_event_at:
            gen.first_producer_event_at = event_seen_at
        if event_type == "done" and not gen.done_event_at:
            gen.done_event_at = event_seen_at
        _break_loop = False
        if not gen.detached:
            _detach_if_ui_client_deleted(gen, state, f"before {event_type} UI update")

        # ── First content transition ─────────────────────────────────
        if (
            not gen.first_content
            and (
                (event_type == "token" and str(payload or "").strip())
                or (event_type == "done" and str(payload or "").strip())
            )
        ):
            gen.first_content = True
            if not gen.first_answer_token_at:
                gen.first_answer_token_at = event_seen_at
            if not gen.detached:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                        gen.thinking_label = None
                    if gen.thinking_text and not gen.thinking_collapsed:
                        _flush_thinking_render("first-content thinking render", force=True)
                        _render_thinking_collapse(gen)
                    if gen.assistant_md:
                        gen.assistant_md.set_visibility(True)
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "first-content transition")
                    logger.error("Error rendering thinking collapse", exc_info=True)

        if (
            event_type in {"warning", "error", "tool_call", "tool_done", "summarizing", "interrupt", "done"}
            or event_type in {
                "context_usage", "compaction_started", "compaction_succeeded",
                "compaction_failed",
            }
        ):
            _flush_live_renders(f"{event_type} boundary", force=True)

        if event_type in {
            "context_usage",
            "compaction_started",
            "compaction_succeeded",
            "compaction_failed",
        }:
            usage = dict(payload or {}) if isinstance(payload, dict) else {}
            meter_status = {
                "compaction_started": "compacting",
                "compaction_succeeded": "ready",
                "compaction_failed": "failed",
            }.get(event_type, str(usage.get("status") or "ready"))
            usage["status"] = meter_status
            projected = cache_and_project_context_usage(
                state,
                gen.thread_id,
                usage,
                detached=gen.detached,
            )
            if projected and p.context_meter:
                try:
                    p.context_meter.update(
                        usage,
                        capacity_state=state.context_capacity_state,
                    )
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "context meter event")
            if event_type in {"compaction_succeeded", "compaction_failed"}:
                event_id = int(usage.get("event_id") or 0)
                if event_id and event_id not in gen.context_event_ids and not any(
                    int(message.get("event_id") or 0) == event_id
                    for message in state.messages
                    if isinstance(message, dict)
                ):
                    gen.context_event_ids.add(event_id)
                    context_message = {
                        "role": "context_event",
                        "event_id": event_id,
                        "event_type": str(
                            usage.get("event_type")
                            or ("context_compacted" if event_type == "compaction_succeeded" else "context_compaction_failed")
                        ),
                        "content": str(usage.get("display_copy") or ""),
                        "severity": "info" if event_type == "compaction_succeeded" else "warning",
                        "icon": "compress" if event_type == "compaction_succeeded" else "warning",
                        "timestamp": time.strftime("%H:%M"),
                    }
                    state.messages.append(context_message)
                    if not gen.detached and cb.add_chat_message:
                        try:
                            cb.add_chat_message(context_message)
                        except Exception as exc:
                            _handle_ui_runtime_error(gen, state, exc, "context event row")

        elif event_type == "warning":
            notice = dict(payload) if isinstance(payload, dict) else {
                "title": "Memory recall fallback",
                "message": str(payload or "Semantic memory recall is unavailable."),
                "action": "Open Settings -> Documents -> Embedding Engine.",
            }
            if not any(
                existing.get("code") == notice.get("code")
                for existing in gen.warnings
                if isinstance(existing, dict)
            ):
                gen.warnings.append(notice)
                if not gen.detached and gen.wrapper:
                    try:
                        with gen.wrapper:
                            with ui.card().classes(
                                "w-full bg-amber-1 text-amber-10 border border-amber-4 q-pa-sm"
                            ):
                                ui.label(str(notice.get("title") or "Memory recall fallback")).classes(
                                    "text-sm font-semibold"
                                )
                                ui.label(str(notice.get("message") or "")).classes("text-xs")
                                ui.label(f"Reason: {notice.get('detail') or ''}").classes("text-xs")
                                ui.label(f"Next: {notice.get('action') or ''}").classes("text-xs font-medium")
                    except Exception as exc:
                        _handle_ui_runtime_error(gen, state, exc, "memory fallback warning")
                        logger.debug("Memory fallback warning render failed", exc_info=True)

        elif event_type == "error":
            _buddy(BuddyEventType.GENERATION_ERROR, "Error", error=str(payload)[:500])
            _voice_diag("generation_event:error", payload_preview=str(payload)[:500])
            voice_output.speak_cue(error_cue(), generation_elapsed=_generation_elapsed())
            _set_realtime_generation_state("error", detail=str(payload)[:500])
            gen.status = "error"
            gen.error = payload
            gen.accumulated = f"\u26a0\ufe0f An error occurred: {payload}"
            if not gen.detached:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                    if gen.assistant_md:
                        gen.assistant_md.set_visibility(True)
                        _update_answer_render("error-event answer render", force=True)
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "error-event cleanup")
                    logger.debug("Error-event UI cleanup failed", exc_info=True)
            try:
                configurable = gen.config.get("configurable", {}) if isinstance(gen.config, dict) else {}
                if configurable.get("runtime_mode") == "agent":
                    try:
                        repair_orphaned_tool_calls(gen.enabled_tools, gen.config)
                    except Exception:
                        logger.debug("repair_orphaned_tool_calls failed", exc_info=True)
                if configurable.get("runtime_mode") == "agent":
                    _agent = get_agent_graph(
                        gen.enabled_tools,
                        model_override=configurable.get("model_override"),
                    )
                    _agent.update_state(
                        gen.config,
                        {"messages": [AIMessage(content=gen.accumulated)]},
                    )
                else:
                    from row_bot.threads import append_checkpoint_messages

                    append_checkpoint_messages(gen.thread_id, [AIMessage(content=gen.accumulated)])
            except Exception:
                logger.debug("Failed to persist error to checkpoint", exc_info=True)
            _break_loop = True

        elif event_type == "tool_call":
            tool_name = _tool_event_name(payload)
            raw_tool_name = _tool_event_raw_name(payload)
            tool_key = (raw_tool_name or tool_name).strip().lower()
            _buddy(BuddyEventType.TOOL_STARTED, "Using a tool", tool=tool_name)
            _voice_diag("generation_event:tool_call", tool=tool_name)
            if tool_key == "delegate_work" or (not raw_tool_name and tool_name.strip().lower() == "agents"):
                _schedule_delegated_agent_card_probe(
                    gen,
                    state,
                    cb,
                    tool_call_id=str(getattr(payload, "get", lambda *_args: "")("id", "")),
                    wait_mode=bool(_tool_event_args(payload).get("wait")),
                )
            if tool_key in {"agents", "delegate_work", "agent_wait", "agent_status"} or tool_name.strip().lower() == "agents":
                _schedule_parent_agent_strip_refresh(cb)
            if gen.voice_mode and state.voice_coordinator.transport == "realtime":
                realtime_tool_events_since_cue += 1
                state.voice_coordinator.mark_realtime_latency("row_bot_tool_started")
            _set_realtime_generation_state("row_bot_tool_running", detail=tool_name)
            spoke_tool_cue = False
            if tool_key not in {"agents", "delegate_work"}:
                spoke_tool_cue = voice_output.speak_cue(
                    tool_start_cue(tool_name),
                    generation_elapsed=_generation_elapsed(),
                )
            if spoke_tool_cue:
                realtime_tool_events_since_cue = 0
            _voice_diag(
                "realtime_cue_spoken" if spoke_tool_cue else "realtime_cue_suppressed",
                cue_type="tool_start",
                reason="tool_call",
                spoke=spoke_tool_cue,
                tool_events_since_cue=realtime_tool_events_since_cue,
            )
            _grouped_tool_call = False
            if not gen.detached and gen.tool_col:
                try:
                    _add_live_tool_pending(gen, tool_name)
                    _grouped_tool_call = True
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "tool-call group")
                    logger.debug("Tool-call group creation failed", exc_info=True)
            if not _grouped_tool_call and not gen.detached and gen.tool_col:
                try:
                    with gen.tool_col:
                        _pending_exp = ui.expansion(
                            f"\U0001f504 {tool_name}\u2026", icon="hourglass_empty"
                        ).classes(TOOL_TRACE_EXPANSION_CLASSES)
                        # FIFO queue per tool name - parallel calls to the
                        # same tool must each get their own pending slot so
                        # later tool_done events can still match them.
                        gen.pending_tools.setdefault(tool_name, []).append(_pending_exp)
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "tool-call expansion")
                    logger.debug("Tool-call expansion creation failed", exc_info=True)

        elif event_type == "tool_done":
            _buddy(BuddyEventType.TOOL_FINISHED, "Tool finished")
            _voice_diag("generation_event:tool_done")
            if gen.voice_mode and state.voice_coordinator.transport == "realtime":
                realtime_tool_done_since_cue += 1
                state.voice_coordinator.mark_realtime_latency("row_bot_tool_done")
            await _handle_tool_done(gen, state, p, payload, cb)

        elif event_type == "summarizing":
            _buddy(BuddyEventType.THINKING, "Compacting context")
            if not gen.detached and gen.wrapper:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                        gen.thinking_label = None
                    with gen.wrapper:
                        gen.thinking_label = ui.html(
                            '<span class="row-bot-typing" style="font-size:0.9rem; opacity:0.6;">'
                            '\U0001f5dc Compacting context<span class="dots">'
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
            _stream_updates += 1
            _thinking_stream_updates += 1
            if not gen.detached:
                _update_thinking_render("thinking-token rendering")

        elif event_type == "token":
            if not str(payload or "").strip() and not str(gen.accumulated or "").strip():
                continue
            if not gen.first_content:
                _voice_diag("generation_event:first_token")
                if gen.voice_mode and state.voice_coordinator.transport == "realtime":
                    state.voice_coordinator.mark_realtime_latency("first_token")
            _stream_updates += 1
            _answer_stream_updates += 1
            _now = asyncio.get_event_loop().time()
            if _now - _last_buddy_token_at > 0.8:
                _last_buddy_token_at = _now
                _buddy(BuddyEventType.TOKEN, "Writing")
            gen.accumulated += payload
            if not gen.detached and gen.assistant_md:
                _update_answer_render("token content update")

            realtime_tts = gen.tts_active and state.voice_coordinator.transport == "realtime"
            if realtime_tts:
                if "```" in payload:
                    gen.tts_in_code = not gen.tts_in_code
                if not gen.tts_in_code:
                    if realtime_chunker is not None:
                        for chunk in realtime_chunker.push(str(payload or "")):
                            _speak_realtime_stream_chunk(chunk)
                    else:
                        gen.tts_buffer += payload

            # Streaming TTS (only when attached)
            if gen.tts_active and not realtime_tts:
                if "```" in payload:
                    gen.tts_in_code = not gen.tts_in_code
                if not gen.tts_in_code:
                    gen.tts_buffer += payload
                    sentences = SENTENCE_SPLIT.split(gen.tts_buffer)
                    if len(sentences) > 1:
                        for s in sentences[:-1]:
                            if gen.tts_spoken >= MAX_STREAM_SENTENCES:
                                break
                            speakable = make_speakable_response(
                                s,
                                allow_long=gen.tts_allow_long,
                                reason="assistant_stream_sentence",
                            )
                            state.tts_service.speak_streaming(speakable.text)
                            gen.tts_spoken += 1
                            if gen.tts_spoken >= MAX_STREAM_SENTENCES:
                                state.tts_service.flush_streaming(
                                    "The full response is shown in the app."
                                )
                                gen.tts_active = False
                        gen.tts_buffer = sentences[-1]

        elif event_type == "interrupt":
            takeover_pause = _is_computer_takeover_interrupt(payload)
            _buddy(
                BuddyEventType.GENERATION_INTERRUPTED if takeover_pause else BuddyEventType.APPROVAL_NEEDED,
                "Waiting for you" if takeover_pause else "Approval pending",
            )
            _voice_diag("generation_event:interrupt")
            if not takeover_pause:
                voice_output.speak_cue(approval_needed_cue(), generation_elapsed=_generation_elapsed())
            _set_realtime_generation_state(
                "waiting_for_user" if takeover_pause else "waiting_for_approval",
                detail="computer_takeover" if takeover_pause else "interrupt",
            )
            gen.interrupt_data = payload
            state.pending_interrupt = payload
            state.pending_interrupt_generation_id = gen.generation_id
            state.pending_interrupt_tool_groups = gen.pending_tools
            state.pending_interrupt_runtime_surface = str(
                ((gen.config.get("configurable") or {}).get("runtime_surface") or "")
            )
            gen.status = "interrupted"
            if not gen.detached and gen.thinking_label:
                try:
                    gen.thinking_label.delete()
                    gen.thinking_label = None
                except Exception as exc:
                    _handle_ui_runtime_error(
                        gen,
                        state,
                        exc,
                        "approval thinking indicator cleanup",
                    )
                    logger.debug(
                        "Approval thinking indicator cleanup failed",
                        exc_info=True,
                    )
            rendered_inline = False if takeover_pause else _render_inline_interrupt_notice(gen, state, p, cb)
            if takeover_pause:
                gen.interrupt_rendered = True
                refresh_live_control = getattr(p, "live_control_refresh", None)
                if callable(refresh_live_control):
                    refresh_live_control()
            elif not gen.detached and not _ui_handle_client_deleted(p.interrupt_dlg):
                try:
                    cb.show_interrupt(payload)
                    gen.interrupt_rendered = True
                except RuntimeError as exc:
                    _handle_ui_runtime_error(gen, state, exc, "interrupt dialog render")
                    logger.warning("Approval is pending but the interrupt dialog could not be rendered", exc_info=True)
            elif rendered_inline:
                gen.interrupt_rendered = True
            _break_loop = True

        elif event_type == "done":
            _voice_diag("generation_event:done", final_chars=len(str(payload or "")))
            gen.accumulated = payload
            if not gen.detached:
                try:
                    if gen.thinking_label:
                        gen.thinking_label.delete()
                        gen.thinking_label = None
                    if gen.thinking_text and not gen.thinking_collapsed:
                        _flush_thinking_render("done-event thinking render", force=True)
                        _render_thinking_collapse(gen)
                    elif gen.thinking_md and gen.thinking_expansion:
                        gen.thinking_md.delete()
                        gen.thinking_md = None
                    if gen.assistant_md:
                        gen.assistant_md.set_visibility(True)
                        _update_answer_render("done-event answer render", force=True)
                except Exception as exc:
                    _handle_ui_runtime_error(gen, state, exc, "done-event finalization")
                    logger.debug("Done-event UI finalization failed", exc_info=True)

        elif event_type == "orchestration_waiting":
            orchestration_payload = payload if isinstance(payload, dict) else {}
            gen.orchestration_id = str(
                orchestration_payload.get("orchestration_id") or ""
            )
            gen.orchestration_required = True
            gen.orchestration_suspended = True
            state.mark_thread_dirty(gen.thread_id)
            _refresh_parent_agent_strip(cb)

        if _break_loop:
            break

    except Exception:
        logger.error("Error in generation consumer", exc_info=True)

    # ── Finalise ─────────────────────────────────────────────────────
    if gen.status == "streaming":
        gen.status = "done"
    _orchestration_suspended = bool(
        getattr(gen, "orchestration_suspended", False)
    )
    try:
        from row_bot.agent_orchestrator import (
            get_generation_orchestration,
        )

        orchestration = await run.io_bound(
            lambda: get_generation_orchestration(gen.thread_id, gen.generation_id)
        )
        if orchestration and int(orchestration.get("required_total") or 0) > 0:
            await run.io_bound(
                lambda: _republish_required_orchestration_lifecycle(
                    str(orchestration["id"]),
                    source_generation_id=str(gen.generation_id or ""),
                )
            )
            if _orchestration_suspended:
                gen.orchestration_id = str(orchestration["id"])
                gen.orchestration_required = True
                state.mark_thread_dirty(gen.thread_id)
                _refresh_parent_agent_strip(cb)
    except Exception:
        logger.exception("Required Agent orchestration presentation failed")
    if _orchestration_suspended:
        _buddy(
            BuddyEventType.ORCHESTRATION_ACTIVE,
            "Child Agents working",
            orchestration_id=str(getattr(gen, "orchestration_id", "") or ""),
        )
    elif gen.status == "done":
        _buddy(BuddyEventType.GENERATION_DONE, "Done")
    _voice_diag("generation_finalizing")
    _finalization_started = time.perf_counter()

    try:
        if not gen.detached:
            _detach_if_ui_client_deleted(gen, state, "post-stream finalization")

        if not gen.detached:
            _flush_live_renders("post-stream finalization", force=True)
            if gen.tts_active and gen.status == "done":
                if (
                    state.voice_coordinator.transport == "realtime"
                    and realtime_chunker is not None
                    and not gen.realtime_stream_finalized
                ):
                    for chunk in realtime_chunker.flush():
                        _speak_realtime_stream_chunk(chunk)
                    _flush_realtime_speech_queue("finalizing")
                speakable = make_speakable_response(
                    gen.accumulated if state.voice_coordinator.transport == "realtime" else gen.tts_buffer,
                    allow_long=gen.tts_allow_long or state.voice_coordinator.transport == "realtime",
                    reason="assistant_stream_final",
                )
                if state.voice_coordinator.transport == "realtime":
                    _set_realtime_generation_state("speaking", detail="final_speech_requested")
                    if realtime_speech_queue is not None:
                        _voice_diag(
                            "realtime_final_speech_decision",
                            **realtime_speech_queue.final_decision(gen.accumulated),
                        )
                spoke = voice_output.speak_final(speakable.text)
                _voice_diag(
                    "final_speech_requested",
                    spoke=spoke,
                    speakable_chars=len(speakable.text),
                    speakable_reason=speakable.reason,
                    speakable_truncated=speakable.truncated,
                    speakable_fallback=speakable.fallback,
                )
                if state.voice_coordinator.transport == "realtime" and not spoke:
                    _set_realtime_generation_state("listening", detail="final_speech_not_started")
            elif state.voice_coordinator.transport == "realtime" and gen.voice_mode and gen.status == "done":
                _set_realtime_generation_state("listening", detail="generation_done_no_speech")

            # Re-render normal chat content via render_text_with_embeds so code
            # blocks get proper highlight.js and mermaid diagrams render. Keep
            # Developer Studio's streamed markdown node in place: deleting and
            # recreating the final message is the path most likely to detach a
            # still-active Developer workspace and collapse the inspector.
            if (
                gen.accumulated and not state.active_developer_workspace_id
            ):
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
                    "if (window.rowBotHighlightCodeBlocks) { window.rowBotHighlightCodeBlocks(); } "
                    "else { setTimeout(function() { document.querySelectorAll('pre code:not([data-highlighted=\"yes\"])').forEach(function(el) { if (!el.closest('.row-bot-live-stream') && !el.children.length) hljs.highlightElement(el); }); }, 80); }"
                )
                ui.run_javascript(
                    "setTimeout(function() {"
                    "  var nodes = Array.from(document.querySelectorAll('pre.mermaid')).filter(function(node) { return !node.closest('.row-bot-live-stream'); });"
                    "  mermaid.run({nodes: nodes, suppressErrors: true});"
                    "}, 150);"
                )
            except RuntimeError as exc:
                # Syntax highlighting / mermaid enhancement is optional. Treat
                # failures here as cosmetic; marking the generation detached
                # after the final row has rendered causes detached recovery to
                # append the persisted assistant message as a duplicate.
                logger.debug("JS runtime unavailable for hljs/mermaid: %s", exc)
    except Exception:
        logger.error("Error in post-stream finalization", exc_info=True)

    # Store assistant message
    final_tool_results = list(gen.tool_results or [])
    unfiltered_final_tool_results = list(final_tool_results)
    removed_visible_agent_results = False
    final_agent_run_ids: list[str] = []
    promoted_agent_run_ids: list[str] = []
    async_delegated_run_ids: list[str] = []
    if state.thread_id == gen.thread_id:
        if final_tool_results:
            final_agent_run_ids = _ordered_agent_run_ids_from_tool_results(final_tool_results)
            async_delegated_run_ids = _async_delegated_run_ids_from_tool_results(final_tool_results)
            if final_agent_run_ids:
                visible_agent_ids = _visible_agent_run_ids(state.messages)
                promoted_agent_run_ids = [
                    run_id for run_id in final_agent_run_ids
                    if run_id not in visible_agent_ids
                ]
            filter_messages = state.messages
            if promoted_agent_run_ids:
                filter_messages = [
                    *state.messages,
                    {"role": "assistant", "agent_run_ids": promoted_agent_run_ids},
                ]
            final_tool_results, removed_visible_agent_results = _filter_visible_agent_tool_results(
                filter_messages,
                final_tool_results,
            )
        for run_id in _async_child_agent_run_ids_for_generation(gen, unfiltered_final_tool_results):
            if run_id not in async_delegated_run_ids:
                async_delegated_run_ids.append(run_id)
    _has_final_output = bool(
        str(gen.accumulated or "").strip()
        or final_tool_results
        or promoted_agent_run_ids
        or gen.chart_data
        or gen.captured_images
        or gen.captured_videos
    )
    _persisted_detached = False
    if _has_final_output:
        await _capture_balanced_browser_screenshot(gen, state)
        visible_content = gen.accumulated if str(gen.accumulated or "").strip() else ""
        a_msg: dict = {"role": "assistant", "content": visible_content}
        attach_thinking_to_message(a_msg, gen.thinking_text)
        if gen.warnings:
            a_msg["warnings"] = list(gen.warnings)
        if promoted_agent_run_ids:
            a_msg["agent_run_ids"] = promoted_agent_run_ids
            a_msg["agent_run_refresh_key"] = _refresh_key_for_agent_run_ids(promoted_agent_run_ids)
        if final_tool_results:
            a_msg["tool_results"] = final_tool_results
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
        if state.thread_id == gen.thread_id:
            inserted_idx = _insert_assistant_before_future_queued_turns(
                state.messages,
                a_msg,
                current_queued_ids=gen.queued_message_ids,
                current_generation_id=gen.generation_id,
            )
            settled_queued_controls = _settle_queued_controls(
                state.messages,
                gen.queued_message_ids,
            )
            persist_thread_media_state(state.thread_id, state.messages)
            state.cache_active_messages()
            inserted_at_tail = inserted_idx == len(state.messages) - 1
            has_agent_tool_results = any(
                isinstance(item, dict) and is_agent_tool_result(item)
                for item in (final_tool_results or [])
            )
            has_refreshable_agent_cards = bool(promoted_agent_run_ids) or has_agent_tool_results
            needs_transcript_reconcile = (
                (not inserted_at_tail)
                or settled_queued_controls
                or has_refreshable_agent_cards
            )
            if (
                not gen.detached
                and cb.mark_chat_message_rendered
                and not needs_transcript_reconcile
            ):
                try:
                    cb.mark_chat_message_rendered(a_msg)
                except Exception:
                    logger.debug("Final assistant render-state mark failed", exc_info=True)
            elif not gen.detached:
                try:
                    _delete_live_generation_row(gen)
                    cb.refresh_chat_messages()
                except Exception:
                    logger.debug("Final assistant queued-turn transcript refresh failed", exc_info=True)
            if promoted_agent_run_ids:
                _schedule_direct_agent_card_refresh(
                    state=state,
                    cb=cb,
                    thread_id=state.thread_id,
                    run_ids=promoted_agent_run_ids,
                )
            agent_refresh_run_ids: list[str] = []
            for run_id in [*final_agent_run_ids, *async_delegated_run_ids]:
                if run_id not in agent_refresh_run_ids:
                    agent_refresh_run_ids.append(run_id)
            if agent_refresh_run_ids:
                _schedule_agent_tool_result_card_refresh(
                    state=state,
                    cb=cb,
                    thread_id=state.thread_id,
                    run_ids=agent_refresh_run_ids,
                    async_completion_run_ids=async_delegated_run_ids,
                )
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
            # Detached run for a different thread wrote straight to the
            # checkpoint; mark its cached message list stale so the next
            # select re-reads.  Do not reload the active thread here: an
            # active but UI-detached run may have newer optimistic user
            # messages in memory than the checkpoint has flushed yet.
            state.mark_thread_dirty(gen.thread_id)
            if _has_detached_media and not _persisted_detached:
                logger.warning(
                    "Detached generation for thread %s completed but media sidecar persistence did not attach anything",
                    gen.thread_id,
                )
    elif state.thread_id == gen.thread_id and (gen.queued_message_ids or removed_visible_agent_results):
        settled_empty_queued_controls = _settle_queued_controls(state.messages, gen.queued_message_ids)
        if removed_visible_agent_results and not gen.detached:
            try:
                _delete_live_generation_row(gen)
            except Exception:
                logger.debug("Duplicate Agent tool-only live row cleanup failed", exc_info=True)
        if settled_empty_queued_controls or removed_visible_agent_results:
            state.cache_active_messages()
            if not gen.detached:
                try:
                    cb.refresh_chat_messages()
                except Exception:
                    logger.debug("Empty queued-turn transcript refresh failed", exc_info=True)

    # Cleanup
    queued_voice_controls = [] if gen.stop_event.is_set() else list(getattr(gen, "voice_control_queue", []) or [])
    if _active_generations.get(gen.thread_id) is gen:
        _active_generations.pop(gen.thread_id, None)
    _cleanup_live_control_sessions(
        gen.thread_id,
        preserve_computer_pause=True,
        preserve_browser_takeover=True,
        context="finalization",
    )

    # Update UI if this is still the active thread
    if state.thread_id == gen.thread_id:
        if not gen.detached:
            _detach_if_ui_client_deleted(gen, state, "final active-thread UI update")
        # If we detached mid-stream but the client came back and is
        # still looking at this thread, the in-DOM element handles are
        # stale. Refresh only the transcript container; rebuilding the
        # whole main area causes visible flashes and can race optimistic
        # user-message rendering.
        if gen.detached:
            try:
                cb.refresh_chat_messages()
                logger.info(
                    "Detached finalize refreshed transcript without full main rebuild for thread %s",
                    gen.thread_id,
                )
            except RuntimeError as exc:
                _handle_ui_runtime_error(gen, state, exc, "scoped transcript refresh")
                logger.info(
                    "Detached finalize could not refresh transcript because UI client is detached for thread %s",
                    gen.thread_id,
                )
            except Exception:
                logger.debug("Scoped transcript refresh failed", exc_info=True)
        if p.stop_btn and not _ui_handle_client_deleted(p.stop_btn):
            try:
                p.stop_btn.props('icon=stop')
                p.stop_btn.disable()
            except Exception:
                logger.debug("stop_btn reset failed", exc_info=True)
        if state.voice_enabled and not (state.tts_service and state.tts_service.enabled):
            state.voice_coordinator.unmute()
        if gen.interrupt_data:
            takeover_pause = _is_computer_takeover_interrupt(gen.interrupt_data)
            _set_realtime_generation_state(
                "waiting_for_user" if takeover_pause else "waiting_for_approval",
                detail="computer_takeover" if takeover_pause else "approval_pending",
            )
            state.pending_interrupt = gen.interrupt_data
            state.pending_interrupt_generation_id = gen.generation_id
            state.pending_interrupt_tool_groups = gen.pending_tools
            state.pending_interrupt_runtime_surface = str(
                ((gen.config.get("configurable") or {}).get("runtime_surface") or "")
            )
            if takeover_pause:
                rendered_inline = True
                gen.interrupt_rendered = True
            elif not gen.interrupt_rendered:
                rendered_inline = _render_inline_interrupt_notice(gen, state, p, cb)
            else:
                rendered_inline = True
            if gen.detached or _ui_handle_client_deleted(p.interrupt_dlg) or gen.interrupt_rendered:
                logger.info(
                    "Approval pending for thread %s; dialog render skipped because UI client is detached or approval was already rendered",
                    gen.thread_id,
                )
            elif not takeover_pause:
                try:
                    cb.show_interrupt(gen.interrupt_data)
                    gen.interrupt_rendered = True
                except RuntimeError as exc:
                    _handle_ui_runtime_error(gen, state, exc, "interrupt dialog render")
                    logger.warning("Approval is pending but the interrupt dialog could not be rendered", exc_info=True)
            if rendered_inline:
                gen.interrupt_rendered = True
        if gen.refresh_model_controls_on_done and gen.status == "done":
            _schedule_model_controls_refresh(cb)
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

    if state.active_developer_workspace_id and state.thread_id == gen.thread_id:
        try:
            from row_bot.developer.inspector_snapshot import request_snapshot_refresh

            request_snapshot_refresh(
                state.active_developer_workspace_id,
                gen.thread_id,
                reason="developer_generation_final",
            )
        except Exception:
            logger.debug("Developer inspector final refresh scheduling failed", exc_info=True)

    goal_continuation_prompt = ""
    if (
        (gen.status == "done" or gen.interrupt_data)
        and (_has_final_output or gen.interrupt_data)
        and not gen.stop_event.is_set()
        and not _orchestration_suspended
    ):
        try:
            from row_bot import goals

            configurable = gen.config.get("configurable", {}) if isinstance(gen.config, dict) else {}
            model_override = str(configurable.get("model_override") or "")
            decision = await run.io_bound(
                lambda: goals.after_turn(
                    thread_id=gen.thread_id,
                    turn_id=gen.generation_id or f"{gen.thread_id}:{id(gen)}",
                    assistant_text=gen.accumulated,
                    model_override=model_override,
                    pending_approval=bool(gen.interrupt_data),
                )
            )
            if decision.should_continue and decision.continuation_prompt:
                goal_continuation_prompt = decision.continuation_prompt
            refresh_goal = decision.goal
            if refresh_goal is None:
                refresh_goal = await run.io_bound(
                    lambda: goals.get_current_goal(gen.thread_id, include_terminal=True)
                )
            if refresh_goal and state.thread_id == gen.thread_id and not gen.detached:
                _schedule_goal_strip_refresh(cb)
        except Exception:
            logger.debug("Goal Mode post-turn evaluation failed", exc_info=True)

    if state.thread_id == gen.thread_id and not gen.detached:
        _schedule_goal_strip_refresh(cb)

    if state.thread_id == gen.thread_id and not gen.detached:
        try:
            from row_bot import goals

            visible_goal = await run.io_bound(
                lambda: goals.get_current_goal(gen.thread_id, include_terminal=True)
            )
            if visible_goal:
                _schedule_goal_surface_refresh(
                    cb,
                    reason=f"goal_visible_final_{str(visible_goal.get('status') or 'updated')}",
                )
        except Exception:
            logger.debug("Goal visible final surface refresh failed", exc_info=True)

    if goal_continuation_prompt and state.thread_id == gen.thread_id and not gen.detached:
        logger.info(
            "Goal Mode continuation dispatch for thread %s",
            gen.thread_id[:8] if gen.thread_id else "?",
        )
        asyncio.create_task(
            send_message(
                goal_continuation_prompt,
                state=state,
                p=p,
                cb=cb,
                voice_mode=gen.voice_mode,
                internal_goal_continuation=True,
            )
        )
        queued_voice_controls = []

    if queued_voice_controls and state.thread_id == gen.thread_id and not gen.detached:
        follow_ups = [
            str(item.get("text") or "").strip()
            for item in queued_voice_controls
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        pending_ids = [
            str(item.get("pending_message_id") or "").strip()
            for item in queued_voice_controls
            if isinstance(item, dict) and str(item.get("pending_message_id") or "").strip()
        ]
        if follow_ups:
            follow_up_text = "\n".join(follow_ups)
            logger.info(
                "voice.realtime.pipeline %s",
                {
                    "stage": "queued_voice_controls_dispatch",
                    "thread_id": gen.thread_id,
                    "count": len(follow_ups),
                    "text_chars": len(follow_up_text),
                },
            )
            asyncio.create_task(
                send_message(
                    follow_up_text,
                    state=state,
                    p=p,
                    cb=cb,
                    voice_mode=gen.voice_mode,
                    queued_message_ids=pending_ids,
                )
            )

    gen.consumer_finalized_at = time.perf_counter()
    gen.finalization_ms = (gen.consumer_finalized_at - _finalization_started) * 1000.0
    if gen.producer_thread_started_at and gen.first_producer_event_at:
        gen.producer_wait_ms = max(
            0.0,
            (gen.first_producer_event_at - gen.producer_thread_started_at) * 1000.0,
        )
    _lifecycle_elapsed_ms = (gen.consumer_finalized_at - (gen.created_at or _consume_started)) * 1000.0
    _first_event_ms = (
        max(0.0, (gen.first_producer_event_at - gen.created_at) * 1000.0)
        if gen.first_producer_event_at and gen.created_at
        else None
    )
    _first_answer_token_ms = (
        max(0.0, (gen.first_answer_token_at - gen.created_at) * 1000.0)
        if gen.first_answer_token_at and gen.created_at
        else None
    )
    _done_event_ms = (
        max(0.0, (gen.done_event_at - gen.created_at) * 1000.0)
        if gen.done_event_at and gen.created_at
        else None
    )
    log_ui_perf(
        "generation.lifecycle",
        _lifecycle_elapsed_ms,
        threshold_ms=1000.0,
        thread_id=gen.thread_id,
        generation_id=gen.generation_id,
        status=gen.status,
        detached=gen.detached,
        producer_wait_ms=round(gen.producer_wait_ms, 1),
        queue_empty_wait_ms=round(gen.queue_empty_wait_ms, 1),
        first_producer_event_ms=round(_first_event_ms, 1) if _first_event_ms is not None else None,
        first_answer_token_ms=round(_first_answer_token_ms, 1) if _first_answer_token_ms is not None else None,
        done_event_ms=round(_done_event_ms, 1) if _done_event_ms is not None else None,
        finalization_ms=round(gen.finalization_ms, 1),
        stream_updates=_stream_updates,
        answer_stream_updates=_answer_stream_updates,
        thinking_stream_updates=_thinking_stream_updates,
        ui_flushes=answer_batcher.flush_count,
        thinking_ui_flushes=thinking_batcher.flush_count,
        answer_chars=len(gen.accumulated or ""),
        low_ui_flush_count=answer_batcher.flush_count <= 4,
    )

    log_ui_perf(
        "streaming.consume_generation",
        (time.perf_counter() - _consume_started) * 1000.0,
        rows=_stream_updates,
        stream_updates=_stream_updates,
        answer_stream_updates=_answer_stream_updates,
        thinking_stream_updates=_thinking_stream_updates,
        ui_flushes=answer_batcher.flush_count,
        thinking_ui_flushes=thinking_batcher.flush_count,
        thread_id=gen.thread_id,
        generation_id=gen.generation_id,
        status=gen.status,
        detached=gen.detached,
        producer_wait_ms=round(gen.producer_wait_ms, 1),
        queue_empty_wait_ms=round(gen.queue_empty_wait_ms, 1),
        first_answer_token_ms=round(_first_answer_token_ms, 1) if _first_answer_token_ms is not None else None,
        finalization_ms=round(gen.finalization_ms, 1),
        thinking_chars=len(gen.thinking_text or ""),
        answer_chars=len(gen.accumulated or ""),
        chars_per_stream_update=round(len(gen.accumulated or "") / max(1, _answer_stream_updates), 3),
        chars_per_ui_flush=round(len(gen.accumulated or "") / max(1, answer_batcher.flush_count), 3),
    )


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

    orchestration_id, orchestration_required = _required_orchestration_from_tool_result(
        raw_tool_name,
        tool_content,
    )
    if orchestration_required:
        gen.orchestration_id = orchestration_id
        gen.orchestration_required = True
        # Required voice work speaks a compact acknowledgement at the durable
        # suspension boundary, not provisional parent text or child progress.
        gen.tts_active = False
        gen.tts_buffer = ""

    _register_delegated_agent_tool_result(
        gen,
        state,
        cb,
        raw_tool_name=str(raw_tool_name or ""),
        tool_content=tool_content,
    )

    if _tool_result_changes_model_setting(raw_tool_name, tool_content):
        gen.refresh_model_controls_on_done = True

    if not gen.detached:
        _detach_if_ui_client_deleted(gen, state, f"tool {tool_name} result rendering")

    if state.active_developer_workspace_id and state.thread_id == gen.thread_id:
        try:
            from row_bot.developer.inspector_snapshot import request_snapshot_refresh

            request_snapshot_refresh(
                state.active_developer_workspace_id,
                gen.thread_id,
                reason=f"tool_done:{tool_name}",
                debounce=0.8,
            )
        except Exception:
            logger.debug("Developer inspector tool refresh scheduling failed", exc_info=True)

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
    _grouped_live_result = False
    failed = tool_result_failed(tool_content)
    _skill_result = {"name": str(raw_tool_name or tool_name), "content": tool_content}
    _skill_payload = parse_skill_load_result(_skill_result)
    _skill_noop = is_skill_load_noop_result(_skill_result)
    if _skill_payload or _skill_noop:
        if not gen.detached and gen.tool_col:
            try:
                _skill_group_name = canonical_tool_name(tool_name)
                _skill_group = gen.pending_tools.get(_skill_group_name)
                _skill_expansion = (
                    _skill_group.get("expansion")
                    if isinstance(_skill_group, dict)
                    else None
                )
                _grouped_live_result = _finish_live_tool_result(
                    gen,
                    tool_name,
                    tool_content,
                )
                if _grouped_live_result and isinstance(_skill_group, dict):
                    if not (_skill_group.get("pending") or []):
                        if gen.pending_tools.get(_skill_group_name) is _skill_group:
                            gen.pending_tools.pop(_skill_group_name, None)
                        if _skill_expansion:
                            _skill_expansion.set_visibility(False)
                if (
                    _grouped_live_result
                    and _skill_payload
                    and _skill_payload["skill_id"] not in gen.live_skill_ids
                ):
                    with gen.tool_col:
                        render_skill_load_stub(_skill_payload)
                    gen.live_skill_ids.add(_skill_payload["skill_id"])
                    refresh_skill_chips = getattr(p, "refresh_skill_chips", None)
                    if callable(refresh_skill_chips):
                        refresh_skill_chips()
            except Exception as exc:
                _handle_ui_runtime_error(gen, state, exc, "skill activation rendering")
                logger.debug("Skill activation rendering failed", exc_info=True)
    if not _grouped_live_result and not gen.detached and gen.tool_col:
        try:
            _grouped_live_result = _finish_live_tool_result(gen, tool_name, tool_content)
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, "tool group update")
            logger.debug("Tool group update failed for %s", tool_name, exc_info=True)
    if not _grouped_live_result and not gen.detached and gen.tool_col:
        try:
            _queue = gen.pending_tools.get(tool_name)
            matched_exp = _queue.pop(0) if _queue else None
            if _queue is not None and not _queue:
                gen.pending_tools.pop(tool_name, None)
            if matched_exp:
                matched_exp._props["icon"] = "error" if failed else "check_circle"
                matched_exp._text = f"{'Failed' if failed else 'Done'} {tool_name}"
                matched_exp.update()
                if tool_content:
                    with matched_exp:
                        tool_result_for_live = {"name": tool_name, "content": tool_content}
                        if (
                            not _agent_tool_result_already_live(gen, tool_result_for_live)
                            and not render_agent_tool_result(tool_result_for_live)
                        ):
                            display = tool_content[:5_000]
                            if len(tool_content) > 5_000:
                                display += "\n\n\u2026 (truncated)"
                            ui.code(display).classes("w-full text-xs")
            else:
                with gen.tool_col:
                    with ui.expansion(
                        f"{'Failed' if failed else 'Done'} {tool_name}",
                        icon="error" if failed else "check_circle",
                    ).classes(TOOL_TRACE_EXPANSION_CLASSES):
                        if tool_content:
                            tool_result_for_live = {"name": tool_name, "content": tool_content}
                            if (
                                not _agent_tool_result_already_live(gen, tool_result_for_live)
                                and not render_agent_tool_result(tool_result_for_live)
                            ):
                                display = tool_content[:5_000]
                                if len(tool_content) > 5_000:
                                    display += "\n\n\u2026 (truncated)"
                                ui.code(display).classes("w-full text-xs")
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, "tool expansion update")
            logger.debug("Tool expansion update failed for %s", tool_name, exc_info=True)

    tool_result = {"name": tool_name, "content": tool_content, "error": failed}
    if str(raw_tool_name or "") == "tool_invoke":
        tool_result["raw_name"] = "tool_invoke"
    gen.tool_results.append(tool_result)
    if is_agent_tool_result(tool_result):
        _refresh_parent_agent_strip(cb)
    if (
        raw_tool_name in ("goal_update", "goal_status")
        or "goal" in str(raw_tool_name).lower()
        or "goal" in str(tool_name).lower()
    ):
        _schedule_goal_strip_refresh(cb)

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

    # Browser screenshot thumbnail - run on a thread so the Playwright
    # round-trip (200-800 ms) does not block the asyncio loop; otherwise
    # socket.io pings stall and the client is considered disconnected.
    if raw_tool_name.startswith("browser_"):
        gen.browser_step_count += 1
        await _capture_balanced_browser_screenshot(gen, state)

    # Filesystem image display (workspace_read_file on image files)
    if raw_tool_name in ("workspace_read_file",):
        try:
            from row_bot.tools.filesystem_tool import get_and_clear_displayed_image
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
            from row_bot.tools.image_gen_tool import get_and_clear_last_image
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
            from row_bot.tools.video_gen_tool import get_and_clear_last_video
            _gen_vid = get_and_clear_last_video()
            if _gen_vid and _gen_vid.get("path"):
                gen.captured_videos.append(_gen_vid)
                gen.captured_videos_persist.append(True)
                if not gen.detached and gen.tool_col:
                    with gen.tool_col:
                        from row_bot.ui.render import render_video_with_save
                        render_video_with_save(_gen_vid["path"])
        except Exception as exc:
            _handle_ui_runtime_error(gen, state, exc, "video generation rendering")
            logger.debug("Video generation rendering failed", exc_info=True)


async def _capture_balanced_browser_screenshot(gen: GenerationState, state: AppState) -> None:
    """Move the existing one-per-run Browser frame into the live panel."""

    if gen.browser_step_count <= 0 or gen.browser_preview_attempted:
        return
    gen.browser_preview_attempted = True
    try:
        from row_bot.tools.browser_tool import get_session_manager as _get_bsm

        _bsm = _get_bsm()
        if not _bsm.has_active_session():
            return
        await run.io_bound(_bsm.take_screenshot, gen.thread_id)
    except Exception as exc:
        _handle_ui_runtime_error(gen, state, exc, "balanced browser screenshot capture")
        logger.debug("Browser live-panel screenshot capture failed", exc_info=True)


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
    queued_message_ids: list[str] | None = None,
    internal_goal_continuation: bool = False,
) -> None:
    """Send a message and stream the agent response."""
    from row_bot.agent import stream_agent, repair_orphaned_tool_calls
    from row_bot.threads import (
        build_auto_thread_title,
        create_thread,
        rename_thread,
        should_auto_rename_thread,
        touch_thread,
    )
    from row_bot.tools import registry as tool_registry
    from row_bot.ui.helpers import (
        materialize_chat_attachments,
        process_attached_files,
        persist_thread_media_state,
        wrap_attachment_context,
    )

    if not text.strip() and not p.pending_files:
        return
    direct_agent_request = None
    direct_agent_command_text = False
    if not internal_goal_continuation and not p.pending_files:
        try:
            from row_bot.agent_commands import is_agent_spawn_command, parse_agent_spawn_text

            direct_agent_command_text = is_agent_spawn_command(text)
            if direct_agent_command_text:
                direct_agent_request = await run.io_bound(lambda: parse_agent_spawn_text(text))
        except Exception:
            logger.debug("Could not parse direct Agent request", exc_info=True)

    def _direct_spawn_enabled_tools() -> list[str]:
        names = [tool.name for tool in tool_registry.get_enabled_tools()]
        if getattr(state, "active_developer_workspace_id", None):
            try:
                from row_bot.developer.profile import effective_tool_names

                names = effective_tool_names(names)
            except Exception:
                logger.debug("Could not apply Developer tool profile to direct Agent spawn", exc_info=True)
        return names

    if state.thread_id and state.thread_id in _active_generations:
        if not _drop_terminal_active_generation(state.thread_id):
            active_gen = _active_generations.get(state.thread_id)
            if direct_agent_request is not None:
                await _handle_direct_agent_spawn(
                    direct_agent_request,
                    state=state,
                    p=p,
                    cb=cb,
                    original_text=text,
                    enabled_tool_names=_direct_spawn_enabled_tools(),
                    active_generation_id=str(getattr(active_gen, "generation_id", "") or ""),
                )
                return
            if direct_agent_command_text:
                from row_bot.agent_commands import format_agent_spawn_usage

                user_msg = {"role": "user", "content": text}
                assistant_msg = {"role": "assistant", "content": format_agent_spawn_usage()}
                state.messages.extend([user_msg, assistant_msg])
                persist_thread_media_state(state.thread_id, state.messages)
                state.cache_active_messages()
                cb.add_chat_message(user_msg)
                cb.add_chat_message(assistant_msg)
                touch_thread(state.thread_id)
                return
            from row_bot.voice.actions import classify_active_run_control
            from row_bot.voice.agent_bridge import VoiceAgentBridge

            control_kind = classify_active_run_control(text)
            if (
                control_kind == "steer"
                and not _looks_like_new_agent_request(text)
            ):
                target = await run.io_bound(lambda: _single_steering_target(str(state.thread_id or "")))
                if target:
                    target_status = str(target.get("status") or "")
                    target_name = str(target.get("display_name") or target.get("id") or "Agent")
                    run_id = str(target.get("id") or "")
                    if target_status in {"queued", "waiting_user"}:
                        label = f"Added to {target_name} before it starts"
                        queued_status = "queued_agent_message"
                    else:
                        label = f"Recorded for {target_name}; active calls cannot be interrupted"
                        queued_status = "running_agent_message"
                    _append_visible_queued_control(
                        state=state,
                        cb=cb,
                        text=text,
                        kind=control_kind,
                        status=queued_status,
                        label=label,
                        agent_run_id=run_id,
                        agent_name=target_name,
                    )
                    try:
                        from row_bot.tools import agent_tool as _agent_tool

                        await run.io_bound(lambda: _agent_tool._agent_message(run_id, text))
                    except Exception:
                        logger.debug("Could not record active-run child steering", exc_info=True)
                    try:
                        ui.notify(label, type="info", close_button=True, timeout=4000)
                    except Exception:
                        logger.debug("Active-run child steering notification failed", exc_info=True)
                    return

            bridge = VoiceAgentBridge(
                send_message=lambda *args, **kwargs: None,
                active_generation=lambda: _active_generations.get(state.thread_id),
                cancel_generation=lambda _gen: request_generation_stop(
                    state.thread_id,
                    state=state,
                    p=p,
                    reason="voice_control",
                ),
            )
            control = bridge.control_active_run(text)
            if control.get("handled"):
                pending_id = ""
                if control_kind in {"follow_up", "steer"}:
                    pending_id = _append_visible_queued_control(
                        state=state,
                        cb=cb,
                        text=text,
                        kind=control_kind,
                        status="queued_parent_turn",
                        label="Queued as your next chat message",
                    )
                    gen = _active_generations.get(state.thread_id)
                    control_queue = getattr(gen, "voice_control_queue", None) if gen else None
                    if control_queue and isinstance(control_queue[-1], dict):
                        control_queue[-1]["pending_message_id"] = pending_id
                        control_queue[-1]["queued_label"] = "Queued as your next chat message"
                speakable = str(control.get("speakable") or "")
                if speakable:
                    logger.info(
                        "voice.realtime.pipeline %s",
                        {
                            "stage": "active_run_control_notification",
                            "thread_id": state.thread_id,
                            "control": control.get("control"),
                            "speakable": speakable,
                            "pending_message_id": pending_id,
                        },
                    )
                    try:
                        ui.notify(speakable, type="info", close_button=True, timeout=4000)
                    except Exception:
                        logger.debug("Active-run control notification failed", exc_info=True)
                if state.voice_coordinator.transport == "realtime":
                    state.voice_coordinator.set_realtime_state(
                        "listening",
                        detail=f"active_run_control:{control.get('control')}",
                    )
            return

    # Ensure a thread exists
    if state.thread_id is None:
        from row_bot.approval_policy import DEFAULT_APPROVAL_MODE, normalize_approval_mode
        tid = uuid.uuid4().hex[:12]
        name = text[:50]
        state.thread_approval_mode = normalize_approval_mode(
            getattr(state, "thread_approval_mode", "") or DEFAULT_APPROVAL_MODE,
            DEFAULT_APPROVAL_MODE,
        )
        create_thread(name, thread_id=tid, approval_mode=state.thread_approval_mode)
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
            # Older callback signature without kwargs - fall back.
            cb.rebuild_main()
        cb.rebuild_thread_list()

    gen_thread_id = state.thread_id
    goal_agent_input_override: str | None = None
    queued_visible_user_msg = False
    if queued_message_ids:
        queued_visible_user_msg = _mark_queued_controls_dispatching(
            state.messages,
            queued_message_ids,
        )
        if queued_visible_user_msg:
            state.cache_active_messages()
            try:
                cb.refresh_chat_messages()
            except Exception:
                logger.debug("Queued control transcript refresh failed", exc_info=True)

    if direct_agent_request is not None and not p.pending_files:
        await _handle_direct_agent_spawn(
            direct_agent_request,
            state=state,
            p=p,
            cb=cb,
            original_text=text,
            enabled_tool_names=_direct_spawn_enabled_tools(),
        )
        return

    if not internal_goal_continuation and text.strip().startswith("/") and not p.pending_files:
        from row_bot.slash_commands import dispatch_text_command, resolve_command_text

        enabled_tool_names = [t.name for t in tool_registry.get_enabled_tools()]
        resolved_command = await run.io_bound(lambda: resolve_command_text(text, include_skills=True))
        if resolved_command is not None:
            spec, arg = resolved_command
            if spec.id == "goal":
                from row_bot import goals

                if await run.io_bound(lambda: goals.is_goal_start_argument(arg)):
                    goal = await run.io_bound(lambda: goals.start_goal(gen_thread_id, arg))
                    goal_agent_input_override = goals.build_initial_goal_prompt(goal)
                    _schedule_goal_strip_refresh(cb)
                else:
                    command_response = await run.io_bound(
                        lambda: dispatch_text_command(
                            gen_thread_id,
                            text,
                            enabled_tool_names=enabled_tool_names,
                        )
                    )
                    if command_response is not None:
                        user_msg = {"role": "user", "content": text}
                        assistant_msg = {"role": "assistant", "content": command_response}
                        if queued_visible_user_msg:
                            state.messages.append(assistant_msg)
                        else:
                            state.messages.extend([user_msg, assistant_msg])
                        persist_thread_media_state(state.thread_id, state.messages)
                        state.cache_active_messages()
                        if not queued_visible_user_msg:
                            cb.add_chat_message(user_msg)
                        cb.add_chat_message(assistant_msg)
                        touch_thread(state.thread_id)
                        try:
                            _schedule_goal_strip_refresh(cb)
                            cb.rebuild_main()
                        except TypeError:
                            pass
                        return
            else:
                command_response = await run.io_bound(
                    lambda: dispatch_text_command(
                        gen_thread_id,
                        text,
                        enabled_tool_names=enabled_tool_names,
                    )
                )
                if command_response is not None:
                    user_msg = {"role": "user", "content": text}
                    assistant_msg = {"role": "assistant", "content": command_response}
                    if queued_visible_user_msg:
                        state.messages.append(assistant_msg)
                    else:
                        state.messages.extend([user_msg, assistant_msg])
                    persist_thread_media_state(state.thread_id, state.messages)
                    state.cache_active_messages()
                    if not queued_visible_user_msg:
                        cb.add_chat_message(user_msg)
                    cb.add_chat_message(assistant_msg)
                    touch_thread(state.thread_id)
                    try:
                        cb.rebuild_main()
                    except TypeError:
                        pass
                    return

    # ── Snapshot & clear attached files immediately ──────────────────
    from row_bot.models import get_current_model

    selected_model = state.thread_model_override or get_current_model()
    if not await _context_capacity_ready_for_send(selected_model):
        return

    _files_snapshot: list[dict] = [] if internal_goal_continuation else list(p.pending_files)
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
    if not queued_visible_user_msg and not internal_goal_continuation:
        state.messages.append(user_msg)
        persist_thread_media_state(state.thread_id, state.messages)
        state.cache_active_messages()
        cb.add_chat_message(user_msg)

    auth_block_message = None
    try:
        from row_bot.models import get_current_model

        selected_model = state.thread_model_override or get_current_model()
        auth_block_message = await run.io_bound(lambda: _subscription_auth_block_message(selected_model))
    except Exception:
        logger.debug("Subscription auth preflight skipped unexpectedly", exc_info=True)
    if auth_block_message:
        assistant_msg = {"role": "assistant", "content": auth_block_message}
        state.messages.append(assistant_msg)
        persist_thread_media_state(state.thread_id, state.messages)
        state.cache_active_messages()
        cb.add_chat_message(assistant_msg)
        touch_thread(state.thread_id)
        return

    # Process attached files (slow - vision analysis etc.)
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
            await run.io_bound(materialize_chat_attachments, _files_snapshot)
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
    agent_input = goal_agent_input_override or text
    if file_context:
        marked_file_context = wrap_attachment_context(file_context)
        agent_input = f"{marked_file_context}\n\n{agent_input}" if agent_input else marked_file_context
    developer_context = ""
    if getattr(state, "active_developer_workspace_id", None):
        try:
            from row_bot.developer.agent_context import build_developer_agent_context

            developer_context = await run.io_bound(
                build_developer_agent_context,
                state.active_developer_workspace_id,
                state.thread_id,
            )
        except Exception:
            logger.debug("Failed to build Developer Studio context", exc_info=True)
    logger.info("send_message: file_names=%s, file_context_len=%d, agent_input_len=%d",
                file_names, len(file_context), len(agent_input))

    # Auto-name thread
    if not internal_goal_continuation and should_auto_rename_thread(state.thread_id, state.thread_name):
        state.thread_name = rename_thread(
            state.thread_id,
            build_auto_thread_title(display_content, current_name=state.thread_name),
            source="auto",
        )
        cb.rebuild_thread_list()
        if p.chat_header_label:
            p.chat_header_label.set_text(str(state.thread_name))
    else:
        touch_thread(state.thread_id)

    # ── Build config ─────────────────────────────────────────────────
    # Sync attachment cache to chart tool so it can read attached data files
    from row_bot.tools.chart_tool import _attachment_cache as _chart_cache
    _chart_cache.clear()
    _chart_cache.update(state.attached_data_cache)

    # Sync pasted/attached images to image gen tool for edit_image
    from row_bot.tools.image_gen_tool import _image_cache as _img_cache
    import row_bot.tools.image_gen_tool as _igt_mod
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

    from row_bot.approval_policy import DEFAULT_APPROVAL_MODE, normalize_approval_mode
    from row_bot.threads import _get_thread_approval_mode

    _thread_mo = state.thread_model_override or ""
    _thread_approval_mode = normalize_approval_mode(
        getattr(state, "thread_approval_mode", "") or await run.io_bound(_get_thread_approval_mode, gen_thread_id),
        DEFAULT_APPROVAL_MODE,
    )
    state.thread_approval_mode = _thread_approval_mode
    is_developer = bool(getattr(state, "active_developer_workspace_id", None))
    is_designer = bool(getattr(state, "active_designer_project", None))
    project_workspace_id = ""
    if is_developer:
        try:
            from row_bot.threads import _get_thread_project_workspace

            project_workspace_id = str(
                await run.io_bound(_get_thread_project_workspace, gen_thread_id) or ""
            )
        except Exception:
            logger.debug("Could not resolve Developer project binding", exc_info=True)
    runtime_surface = "developer" if is_developer else "designer" if is_designer else "normal_chat"
    runtime_mode = "agent" if is_developer or is_designer else "auto"
    generation_id = f"{gen_thread_id}:{uuid.uuid4().hex[:12]}"
    if runtime_mode == "agent":
        from row_bot.models import get_current_model

        if not await _agent_ready_forced_surface(_thread_mo or get_current_model(), runtime_surface):
            return
    profile_runtime_config = await run.io_bound(_profile_runtime_config_for_thread, gen_thread_id)
    config = {
        "configurable": {
            "thread_id": gen_thread_id,
            "runtime_surface": runtime_surface,
            "runtime_mode": runtime_mode,
            "generation_id": generation_id,
            "root_objective": str(text or "").strip(),
            "approval_mode": _thread_approval_mode,
            "voice_mode": bool(voice_mode),
            "voice_transport": str(state.voice_coordinator.transport or ""),
            **({"internal_goal_continuation": True} if internal_goal_continuation else {}),
            **({"model_override": _thread_mo} if _thread_mo else {}),
            **({"developer_workspace_id": state.active_developer_workspace_id} if getattr(state, "active_developer_workspace_id", None) else {}),
            **({"project_workspace_id": project_workspace_id} if project_workspace_id else {}),
            **({"designer_project_id": str(state.active_designer_project.id)} if getattr(state, "active_designer_project", None) else {}),
            **({"designer_mode": str(state.active_designer_project.mode)} if getattr(state, "active_designer_project", None) else {}),
            **({"developer_context": developer_context} if developer_context else {}),
            **profile_runtime_config,
        },
    }
    logger.info(
        "send_message: thread=%s developer=%s",
        gen_thread_id[:8] if gen_thread_id else "?",
        is_developer,
    )
    enabled_tools = [t.name for t in tool_registry.get_enabled_tools()]
    if getattr(state, "active_developer_workspace_id", None):
        from row_bot.developer.profile import effective_tool_names
        enabled_tools = effective_tool_names(enabled_tools)

    if voice_mode:
        agent_input = (
            "[Voice input \u2014 the user is speaking to you via microphone "
            "and your response will be read aloud. Keep responses concise "
            "and conversational.]\n\n" + agent_input
        )

    # ── Create generation state ──────────────────────────────────────
    stop_ev = threading.Event()
    cancel_scope = CancellationScope(stop_ev)
    gen = GenerationState(
        thread_id=gen_thread_id,
        q=queue.Queue(),
        stop_event=stop_ev,
        cancel_scope=cancel_scope,
        config=config,
        enabled_tools=enabled_tools,
        generation_id=generation_id,
        queued_message_ids=list(queued_message_ids or []),
        voice_mode=voice_mode,
        tts_active=voice_mode and (
            state.tts_service.enabled
            or state.voice_coordinator.transport in {"realtime", "browser"}
        ),
        tts_allow_long=voice_mode and not internal_goal_continuation and user_requested_read_aloud(text),
        baseline_child_agent_run_ids=_child_agent_run_ids_for_thread(gen_thread_id),
    )
    if voice_mode and state.voice_coordinator.transport == "realtime":
        realtime_call = state.voice_coordinator.consume_realtime_tool_call()
        if realtime_call:
            gen.realtime_tool_call_id = str(realtime_call.get("call_id") or "")
            gen.realtime_tool_name = str(realtime_call.get("name") or "")
            gen.realtime_consult_request = str(realtime_call.get("request") or "")
            gen.realtime_forced_consult = gen.realtime_tool_name == "forced_consult"
    _active_generations[gen_thread_id] = gen
    if voice_mode or state.voice_coordinator.transport == "realtime":
        snapshot = state.voice_coordinator.diagnostic_snapshot()
        snapshot.update({
            "stage": "generation_created",
            "voice_enabled": state.voice_enabled,
            "voice_input_mode": state.voice_input_mode,
            "thread_id": gen_thread_id,
            "generation_status": gen.status,
            "generation_voice_mode": gen.voice_mode,
            "generation_tts_active": gen.tts_active,
            "input_chars": len(str(text or "")),
        })
        logger.info("voice.realtime.pipeline %s", snapshot)

    if p.stop_btn:
        p.stop_btn.enable()

    # ── Prepare assistant message placeholder ────────────────────────
    _build_assistant_placeholder(gen, p)

    if p.chat_scroll:
        p.chat_scroll.scroll_to(percent=1.0)

    # ── Start producer thread ────────────────────────────────────────
    def _sync_stream():
        scope_cm = use_cancellation_scope(cancel_scope)
        scope_cm.__enter__()
        gen.producer_thread_started_at = time.perf_counter()
        first_event_logged = False
        try:
            if voice_mode or state.voice_coordinator.transport == "realtime":
                logger.info(
                    "voice.realtime.pipeline %s",
                    {
                        "stage": "producer_thread_started",
                        "thread_id": gen_thread_id,
                        "voice_mode": voice_mode,
                        "transport": state.voice_coordinator.transport,
                    },
                )
            for ev in stream_agent(agent_input, enabled_tools, config,
                                   stop_event=stop_ev):
                if stop_ev.is_set():
                    break
                event_now = time.perf_counter()
                if not gen.first_producer_event_at:
                    gen.first_producer_event_at = event_now
                if isinstance(ev, tuple) and ev:
                    ev_type = ev[0]
                    ev_payload = ev[1] if len(ev) > 1 else None
                    if (
                        not gen.first_answer_token_at
                        and ev_type in {"token", "done"}
                        and str(ev_payload or "").strip()
                    ):
                        gen.first_answer_token_at = event_now
                    if ev_type == "done" and not gen.done_event_at:
                        gen.done_event_at = event_now
                if not first_event_logged and (voice_mode or state.voice_coordinator.transport == "realtime"):
                    first_event_logged = True
                    logger.info(
                        "voice.realtime.pipeline %s",
                        {
                            "stage": "producer_first_event",
                            "thread_id": gen_thread_id,
                            "event_type": ev[0] if isinstance(ev, tuple) and ev else type(ev).__name__,
                        },
                    )
                gen.q.put(ev)
        except Exception as exc:
            if voice_mode or state.voice_coordinator.transport == "realtime":
                logger.exception(
                    "voice.realtime.pipeline %s",
                    {"stage": "producer_thread_error", "thread_id": gen_thread_id},
                )
            if not stop_ev.is_set():
                gen.q.put(("error", str(exc)))
        finally:
            if stop_ev.is_set():
                try:
                    repair_orphaned_tool_calls(enabled_tools, config)
                except Exception:
                    logger.debug("repair_orphaned_tool_calls failed in stream finally", exc_info=True)
            try:
                from row_bot.skills_activation import consume_one_shot_skills

                consume_one_shot_skills(gen_thread_id)
            except Exception:
                logger.debug("consume_one_shot_skills failed in stream finally", exc_info=True)
            if voice_mode or state.voice_coordinator.transport == "realtime":
                logger.info(
                    "voice.realtime.pipeline %s",
                    {
                        "stage": "producer_thread_finished",
                        "thread_id": gen_thread_id,
                        "stop_requested": stop_ev.is_set(),
                        "first_event_logged": first_event_logged,
                    },
                )
            try:
                gen.q.put(None)
            finally:
                scope_cm.__exit__(None, None, None)

    threading.Thread(target=_sync_stream, daemon=True).start()

    asyncio.create_task(consume_generation(gen, state, p, cb))
    cb.rebuild_thread_list()


# ══════════════════════════════════════════════════════════════════════
# RESUME AFTER INTERRUPT
# ══════════════════════════════════════════════════════════════════════

def _approval_resume_runtime_surface(
    source_surface: str,
    *,
    is_developer: bool,
    is_designer: bool,
) -> str:
    """Preserve the allowlisted origin surface across an approval lifecycle phase."""

    expected = "developer" if is_developer else "designer" if is_designer else "normal_chat"
    source = str(source_surface or "").strip()
    if source and source != expected:
        raise ValueError(
            f"Approval resume surface mismatch: expected {expected}, received {source}."
        )
    return expected


def _approval_resume_events(
    events: Any,
    *,
    approved: bool,
    pending: Any,
    stop_requested: bool = False,
):
    """Settle a denial once without allowing a fresh model/tool retry."""

    iterator = iter(events)
    try:
        if approved:
            yield from iterator
            return

        items = pending if isinstance(pending, list) else [pending]
        items = [item for item in items if item is not None]
        expected_results = max(1, len(items))
        computer_pending = any(
            isinstance(item, dict) and item.get("tool") == "computer_use"
            for item in items
        )
        if stop_requested:
            denial_message = (
                "Computer Use was stopped. No action was taken."
                if computer_pending
                else "The task was stopped. No action was taken."
            )
        else:
            denial_message = (
                "Computer Use access was denied. No action was taken."
                if computer_pending
                else "The requested action was denied. No action was taken."
            )
        settled_results = 0
        for event in iterator:
            event_type = event[0] if isinstance(event, tuple) and event else ""
            if event_type == "tool_done":
                yield event
                settled_results += 1
                if settled_results >= expected_results:
                    yield ("done", denial_message)
                    return
                continue
            if event_type == "error":
                yield event
                return
            if event_type in {"tool_call", "thinking", "thinking_token", "token", "summarizing"}:
                # A denial is terminal for this turn. Ignore replayed pending
                # calls and never expose a new provider/tool attempt.
                continue
            if event_type in {"interrupt", "done"}:
                yield ("done", denial_message)
                return
        yield ("done", denial_message)
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()


async def resume_after_interrupt(
    approved: bool,
    *,
    state: AppState,
    p: P,
    cb: _Callbacks,
    stop_requested: bool = False,
) -> None:
    pending = state.pending_interrupt
    takeover_resume = _is_computer_takeover_interrupt(pending)
    source_generation_id = str(getattr(state, "pending_interrupt_generation_id", "") or "")
    pending_tool_groups = (
        getattr(state, "pending_interrupt_tool_groups", {})
        if source_generation_id
        else {}
    )
    source_runtime_surface = str(
        getattr(state, "pending_interrupt_runtime_surface", "") or ""
    )
    refresh_model_controls_on_done = bool(approved and _interrupt_changes_model_setting(pending))
    interrupt_ids = None
    if isinstance(pending, list):
        interrupt_ids = [
            item.get("__interrupt_id")
            for item in pending
            if isinstance(item, dict) and item.get("__interrupt_id")
        ]
        if not interrupt_ids:
            interrupt_ids = None
    state.pending_interrupt = None
    state.pending_interrupt_generation_id = ""
    state.pending_interrupt_tool_groups = {}
    state.pending_interrupt_runtime_surface = ""
    try:
        approval_container = getattr(p, "developer_approval_container", None)
        if approval_container is not None:
            approval_container.clear()
    except Exception:
        logger.debug("Developer approval container clear after resume failed", exc_info=True)

    gen_thread_id = state.thread_id
    if pending is None:
        _cleanup_live_control_sessions(
            gen_thread_id,
            context="stale approval response after cancellation",
        )
        return

    from row_bot.agent import resume_stream_agent, repair_orphaned_tool_calls
    from row_bot.buddy.events import BuddyEventType, emit_buddy_event
    from row_bot.tools import registry as tool_registry

    if gen_thread_id and not takeover_resume:
        try:
            from row_bot import goals

            waiting_goal = await run.io_bound(
                lambda: goals.get_current_goal(gen_thread_id, include_terminal=True)
            )
            if waiting_goal and str(waiting_goal.get("status") or "") == "waiting_approval":
                if approved:
                    await run.io_bound(lambda: goals.resume_goal(gen_thread_id))
                else:
                    await run.io_bound(
                        lambda: goals.block_goal(
                            gen_thread_id,
                            reason="Approval was denied by the user.",
                        )
                    )
                _schedule_goal_strip_refresh(cb)
                try:
                    cb.rebuild_main()
                except TypeError:
                    pass
        except Exception:
            logger.debug("Goal approval state transition failed", exc_info=True)
    try:
        emit_buddy_event(
            (
                BuddyEventType.GENERATION_STARTED
                if takeover_resume and approved
                else BuddyEventType.GENERATION_STOPPED
                if takeover_resume
                else BuddyEventType.APPROVAL_APPROVED
                if approved
                else BuddyEventType.APPROVAL_DENIED
            ),
            source="ui.streaming",
            payload={
                "thread_id": gen_thread_id,
                "label": (
                    "Resuming"
                    if takeover_resume and approved
                    else "Stopped"
                    if takeover_resume
                    else "Approved"
                    if approved
                    else "Denied"
                ),
            },
        )
    except Exception:
        logger.debug("Buddy interrupt resolution event failed", exc_info=True)

    from row_bot.approval_policy import DEFAULT_APPROVAL_MODE, normalize_approval_mode
    from row_bot.threads import _get_thread_approval_mode

    _thread_mo = state.thread_model_override or ""
    _thread_approval_mode = normalize_approval_mode(
        getattr(state, "thread_approval_mode", "") or await run.io_bound(_get_thread_approval_mode, gen_thread_id),
        DEFAULT_APPROVAL_MODE,
    )
    state.thread_approval_mode = _thread_approval_mode
    developer_context = ""
    if getattr(state, "active_developer_workspace_id", None):
        try:
            from row_bot.developer.agent_context import build_developer_agent_context

            developer_context = await run.io_bound(
                build_developer_agent_context,
                state.active_developer_workspace_id,
                state.thread_id,
            )
        except Exception:
            logger.debug("Failed to build Developer Studio context for resume", exc_info=True)
    is_developer = bool(getattr(state, "active_developer_workspace_id", None))
    is_designer = bool(getattr(state, "active_designer_project", None))
    project_workspace_id = ""
    if is_developer:
        try:
            from row_bot.threads import _get_thread_project_workspace

            project_workspace_id = str(
                await run.io_bound(_get_thread_project_workspace, gen_thread_id) or ""
            )
        except Exception:
            logger.debug(
                "Could not resolve Developer project binding for resume",
                exc_info=True,
            )
    try:
        runtime_surface = _approval_resume_runtime_surface(
            source_runtime_surface,
            is_developer=is_developer,
            is_designer=is_designer,
        )
    except ValueError as exc:
        logger.error("Approval resume refused: %s", exc)
        try:
            from row_bot.computer_use.service import get_computer_use_service

            get_computer_use_service().stop()
        except Exception:
            logger.debug("Computer cleanup after refused approval resume failed", exc_info=True)
        ui.notify(
            "This approval could not resume safely. Start the task again.",
            type="negative",
        )
        return
    from row_bot.models import get_current_model

    if not await _agent_ready_forced_surface(_thread_mo or get_current_model(), runtime_surface):
        return
    generation_id = source_generation_id or f"{gen_thread_id}:{uuid.uuid4().hex[:12]}"
    profile_runtime_config = await run.io_bound(_profile_runtime_config_for_thread, gen_thread_id)
    config = {
        "configurable": {
            "thread_id": gen_thread_id,
            "runtime_surface": runtime_surface,
            "runtime_mode": "agent",
            "generation_id": generation_id,
            "approval_mode": _thread_approval_mode,
            **({"model_override": _thread_mo} if _thread_mo else {}),
            **({"developer_workspace_id": state.active_developer_workspace_id} if getattr(state, "active_developer_workspace_id", None) else {}),
            **({"project_workspace_id": project_workspace_id} if project_workspace_id else {}),
            **({"designer_project_id": str(state.active_designer_project.id)} if getattr(state, "active_designer_project", None) else {}),
            **({"designer_mode": str(state.active_designer_project.mode)} if getattr(state, "active_designer_project", None) else {}),
            **({"developer_context": developer_context} if developer_context else {}),
            **profile_runtime_config,
        },
    }
    logger.info(
        "resume_after_interrupt: thread=%s developer=%s",
        gen_thread_id[:8] if gen_thread_id else "?",
        is_developer,
    )
    enabled_tools = [t.name for t in tool_registry.get_enabled_tools()]
    if getattr(state, "active_developer_workspace_id", None):
        from row_bot.developer.profile import effective_tool_names
        enabled_tools = effective_tool_names(enabled_tools)

    stop_ev = threading.Event()
    cancel_scope = CancellationScope(stop_ev)
    gen = GenerationState(
        thread_id=gen_thread_id,
        q=queue.Queue(),
        stop_event=stop_ev,
        cancel_scope=cancel_scope,
        config=config,
        enabled_tools=enabled_tools,
        generation_id=generation_id,
        refresh_model_controls_on_done=refresh_model_controls_on_done,
        baseline_child_agent_run_ids=_child_agent_run_ids_for_thread(gen_thread_id),
    )
    _active_generations[gen_thread_id] = gen

    if p.stop_btn:
        p.stop_btn.enable()

    _build_assistant_placeholder(gen, p)
    # Keep one visible activity group across the approval boundary so the
    # resumed result settles the exact row that requested approval.
    if isinstance(pending_tool_groups, dict):
        gen.pending_tools = pending_tool_groups

    if p.chat_scroll:
        p.chat_scroll.scroll_to(percent=1.0)

    # ── Start producer thread ────────────────────────────────────────
    def _sync_resume():
        scope_cm = use_cancellation_scope(cancel_scope)
        scope_cm.__enter__()
        gen.producer_thread_started_at = time.perf_counter()
        try:
            resumed_events = resume_stream_agent(
                enabled_tools,
                config,
                approved,
                interrupt_ids=interrupt_ids,
                stop_event=stop_ev,
            )
            for ev in _approval_resume_events(
                resumed_events,
                approved=approved,
                pending=pending,
                stop_requested=stop_requested,
            ):
                if stop_ev.is_set():
                    break
                event_now = time.perf_counter()
                if not gen.first_producer_event_at:
                    gen.first_producer_event_at = event_now
                if isinstance(ev, tuple) and ev:
                    ev_type = ev[0]
                    ev_payload = ev[1] if len(ev) > 1 else None
                    if (
                        not gen.first_answer_token_at
                        and ev_type in {"token", "done"}
                        and str(ev_payload or "").strip()
                    ):
                        gen.first_answer_token_at = event_now
                    if ev_type == "done" and not gen.done_event_at:
                        gen.done_event_at = event_now
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
            try:
                gen.q.put(None)
            finally:
                scope_cm.__exit__(None, None, None)

    threading.Thread(target=_sync_resume, daemon=True).start()

    asyncio.create_task(consume_generation(gen, state, p, cb))
    cb.rebuild_thread_list()


# ══════════════════════════════════════════════════════════════════════
# INTERRUPT DIALOG
# ══════════════════════════════════════════════════════════════════════

async def _stop_pending_approval(state: AppState, p: P, cb: _Callbacks) -> None:
    """Cancel an approval-paused turn from the modal's direct Stop control."""

    try:
        if p.interrupt_dlg is not None:
            p.interrupt_dlg.close()
    finally:
        # Resume the suspended call with a terminal rejection before its
        # task-scoped service cleanup runs.  Pre-emptive cleanup destroys the
        # very lease/client state that the suspended invocation must settle,
        # leaving its activity trace stuck in a running state.
        await resume_after_interrupt(
            False,
            state=state,
            p=p,
            cb=cb,
            stop_requested=True,
        )


def build_interrupt_dialog(
    state: AppState,
    p: P,
    cb: _Callbacks,
) -> None:
    """Create the interrupt dialog and its show/close helpers.

    Attaches ``p.interrupt_dlg`` and returns the ``show_interrupt`` function
    for use as a callback.
    """
    p.interrupt_dlg = ui.dialog().props(
        "persistent transition-show=fade transition-hide=fade data-docs-id=approval-dialog"
    )

    def show_interrupt(data) -> None:
        p.interrupt_dlg.clear()
        items = data if isinstance(data, list) else [data]
        plural = len(items) > 1
        computer_pending = any(isinstance(item, dict) and item.get("tool") == "computer_use" for item in items)
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
                        if isinstance(item, dict):
                            desc = item.get("description") or item.get("reason") or item.get("expected_effect") or item.get("label") or "Approval required"
                            details = [
                                f"App/window: {item.get('app', '')} · {item.get('window', '')}" if item.get("app") or item.get("window") else "",
                                f"Target: {item.get('target', '')}" if item.get("target") else "",
                                f"Data: {item.get('data_summary', '')}" if item.get("data_summary") else "",
                                f"Reversible: {'yes' if item.get('reversible') else 'no or unknown'}" if "reversible" in item else "",
                            ]
                            detail_text = "\n".join(part for part in details if part)
                            if detail_text:
                                desc = f"{desc}\n\n{detail_text}"
                        else:
                            desc = str(item)
                        if plural:
                            ui.markdown(f"**{i + 1}.** {desc}", extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                        else:
                            ui.markdown(desc, extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                if computer_pending:
                    try:
                        from row_bot.computer_use.service import get_computer_use_service

                        preview = get_computer_use_service().ephemeral_screenshot()
                        if preview:
                            ui.label("Current in-memory target preview").classes("text-xs text-grey-6")
                            ui.image(_img_data_uri(_b64.b64encode(preview).decode("ascii"))).classes("w-full").style("max-height: 220px; object-fit: contain;")
                    except Exception:
                        logger.debug("Computer approval preview unavailable", exc_info=True)
            btn_label = f"Approve All ({len(items)})" if plural else "Approve"
            with ui.row().classes("w-full justify-end q-pa-md gap-3").style(
                "border-top: 1px solid #2a2a4a;"
            ):
                if computer_pending:
                    ui.button(
                        "Stop task",
                        icon="stop",
                        on_click=lambda: asyncio.create_task(
                            _stop_pending_approval(state, p, cb)
                        ),
                    ).props("flat no-caps color=negative")
                ui.button("Deny", on_click=lambda: _close_interrupt(False)).props(
                    "flat no-caps"
                ).style(
                    "color: #ff6b6b; font-weight: 600; font-size: 0.9rem;"
                    "padding: 8px 24px; border-radius: 8px;"
                )
                if computer_pending:
                    def _take_over_pending() -> None:
                        try:
                            from row_bot.computer_use.service import get_computer_use_service

                            service = get_computer_use_service()
                            service.take_over(
                                thread_id=str(getattr(state, "thread_id", "") or ""),
                                generation_id=str(
                                    getattr(state, "pending_interrupt_generation_id", "") or ""
                                ),
                            )
                            pending_items = (
                                state.pending_interrupt
                                if isinstance(state.pending_interrupt, list)
                                else [state.pending_interrupt]
                            )
                            for pending_item in pending_items:
                                if isinstance(pending_item, dict) and pending_item.get("tool") == "computer_use":
                                    pending_item["kind"] = "computer_takeover"
                                    pending_item["label"] = "Computer paused for you"
                                    pending_item["description"] = (
                                        "Computer control is paused. Use Resume or Stop in the live panel."
                                    )
                            p.interrupt_dlg.close()
                            refresh_live_control = getattr(p, "live_control_refresh", None)
                            if callable(refresh_live_control):
                                refresh_live_control()
                        except Exception as exc:
                            ui.notify(str(exc), type="negative")

                    ui.button("Take over", icon="pan_tool", on_click=_take_over_pending).props(
                        "outline no-caps"
                    )
                ui.button(btn_label, on_click=lambda: _close_interrupt(True)).props(
                    "unelevated no-caps"
                ).style(
                    "background: #2d8a4e; color: white; font-weight: 600;"
                    "font-size: 0.9rem; padding: 8px 28px; border-radius: 8px;"
                )
        try:
            from row_bot.ui.timer_utils import defer_ui

            if defer_ui(p.interrupt_dlg.open, delay=0.01) is None:
                p.interrupt_dlg.open()
        except Exception:
            logger.debug("Interrupt dialog deferred open failed", exc_info=True)
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
    from row_bot.identity import get_assistant_name
    from row_bot.ui.status_bar import get_bot_avatar_html
    _ph_avatar = get_bot_avatar_html()
    _ph_name = get_assistant_name()
    with p.chat_container:
        with ui.element("div").classes("row-bot-msg-row") as _live_row:
            gen.live_row = _live_row
            ui.html(
                f'<div class="row-bot-avatar row-bot-avatar-bot">{_ph_avatar}</div>',
                sanitize=False,
            )
            with ui.column().classes("row-bot-msg-body gap-1") as _wrapper:
                ui.html(
                    '<div class="row-bot-msg-header">'
                    f'<span class="row-bot-msg-name">{_ph_name}</span>'
                    f'<span class="row-bot-msg-stamp">{datetime.now().strftime("%H:%M")}</span>'
                    '</div>',
                    sanitize=False,
                )
                gen.tool_col = ui.column().classes("w-full gap-1")
                gen.thinking_label = ui.html(
                    '<span class="row-bot-typing" style="font-size:0.9rem; opacity:0.6;">'
                    f'{_ph_name} is thinking<span class="dots">'
                    '<span>.</span><span>.</span><span>.</span></span></span>',
                    sanitize=False,
                )
                gen.assistant_md = ui.markdown(
                    "",
                    extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                ).classes("row-bot-msg row-bot-live-stream w-full")
                gen.assistant_md.set_visibility(False)
                gen.wrapper = _wrapper
