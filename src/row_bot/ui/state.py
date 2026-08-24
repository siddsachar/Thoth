"""Row-Bot UI â€” shared application state.

Contains the singleton ``AppState``, the per-thread ``GenerationState``
dataclass, the per-client ``P`` page-element holder and the global
``_active_generations`` registry.

Every UI module receives these objects as explicit parameters â€” no hidden
closure captures.
"""

from __future__ import annotations

import threading
import queue
import time
from dataclasses import dataclass, field
from typing import Any

from row_bot.models import get_current_model, get_user_context_size
from row_bot.voice import get_voice_service
from row_bot.voice.coordinator import VoiceSessionCoordinator
from row_bot.voice.runtime import load_voice_runtime_settings
from row_bot.tts import TTSService
from row_bot.vision import VisionService
from row_bot.vision_runtime import set_vision_service
from nicegui import ui
from row_bot.approval_policy import DEFAULT_APPROVAL_MODE


def canonical_context_model_ref(model_name: str | None) -> str:
    """Return the provider-qualified identity used by context UI state."""
    raw = str(model_name or "").strip()
    if not raw:
        return ""
    try:
        from row_bot.providers.selection import model_choice_value

        return model_choice_value(raw)
    except Exception:
        return raw


def active_context_identity(state: Any) -> tuple[str, str]:
    """Return the active thread and provider-qualified model identity."""
    thread_id = str(getattr(state, "thread_id", "") or "")
    selected_model = (
        getattr(state, "thread_model_override", "")
        or getattr(state, "current_model", "")
    )
    return thread_id, canonical_context_model_ref(selected_model)


def context_history_present(state: Any) -> bool:
    """Return whether the active UI transcript contains a real chat turn."""
    return any(
        isinstance(message, dict)
        and str(message.get("role") or "") in {"user", "assistant", "tool"}
        for message in (getattr(state, "messages", None) or ())
    )


def clear_context_usage_projection(state: Any) -> None:
    """Clear the visible meter projection without touching per-thread caches."""
    state.context_usage = None
    state.context_usage_thread_id = ""
    state.context_usage_model_ref = ""
    if hasattr(state, "context_capacity_state"):
        state.context_capacity_state = ""
        state.context_capacity_override_tokens = None
        state.context_capacity_effective_tokens = None
        state.context_capacity_model_ref = ""


def clear_all_context_usage_state(state: Any) -> None:
    """Invalidate every in-memory context snapshot and visible projection."""
    clear_context_usage_projection(state)
    cache = getattr(state, "context_usage_cache", None)
    if isinstance(cache, dict):
        cache.clear()
    notice_keys = getattr(state, "context_policy_notice_keys", None)
    if isinstance(notice_keys, dict):
        notice_keys.clear()


def cache_and_project_context_usage(
    state: Any,
    thread_id: str,
    usage: dict,
    *,
    detached: bool = False,
) -> bool:
    """Cache a thread snapshot and project it only for the active identity."""
    cached_usage = dict(usage or {})
    event_thread_id = str(thread_id or "")
    usage_cache = getattr(state, "context_usage_cache", None)
    if event_thread_id and isinstance(usage_cache, dict):
        usage_cache[event_thread_id] = cached_usage

    active_thread_id, active_model_ref = active_context_identity(state)
    event_model_ref = canonical_context_model_ref(cached_usage.get("model_ref"))
    if (
        detached
        or not event_thread_id
        or event_thread_id != active_thread_id
        or not event_model_ref
        or event_model_ref != active_model_ref
    ):
        return False

    state.context_usage = cached_usage
    state.context_usage_thread_id = active_thread_id
    state.context_usage_model_ref = event_model_ref
    return True


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# SHARED APPLICATION STATE (module-level singleton)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class AppState:
    """Shared backend state â€” lives for the lifetime of the server process."""

    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.thread_name: str | None = None
        self.thread_model_override: str = ""  # cloud model override for current thread
        self.thread_approval_mode: str = DEFAULT_APPROVAL_MODE
        self.messages: list[dict] = []
        self.current_model: str = get_current_model()
        self.context_size: int = get_user_context_size()
        self.context_usage: dict | None = None
        self.context_usage_thread_id: str = ""
        self.context_usage_model_ref: str = ""
        self.context_usage_cache: dict[str, dict] = {}
        self.context_capacity_state: str = ""
        self.context_capacity_override_tokens: int | None = None
        self.context_capacity_effective_tokens: int | None = None
        self.context_capacity_model_ref: str = ""
        self.context_policy_notice_keys: dict[str, tuple] = {}
        self.is_generating: bool = False
        self.stop_event: threading.Event = threading.Event()
        self.pending_interrupt: dict | None = None
        self.pending_interrupt_generation_id: str = ""
        self.pending_interrupt_tool_groups: dict = {}
        self.pending_interrupt_runtime_surface: str = ""
        self.show_onboarding: bool = False  # set by helpers._is_first_run()
        self.open_setup_center_on_next_load: bool = False
        self.voice_enabled: bool = False
        self.voice_input_mode: str = "talk"
        self.voice_runtime_settings = load_voice_runtime_settings()
        self.voice_service = get_voice_service()
        self.voice_coordinator = VoiceSessionCoordinator(self.voice_service)
        self.tts_service = TTSService()
        self.vision_service = VisionService()
        self.tts_service.voice_service = self.voice_coordinator
        set_vision_service(self.vision_service)
        self.attached_data_cache: dict[str, bytes] = {}
        self.active_designer_project = None  # DesignerProject | None
        self.active_developer_workspace_id: str | None = None
        self.preferred_home_tab: str | None = None  # tab to select on next rebuild
        self.preferred_developer_tab: str | None = None  # Developer subtab to select on next rebuild
        # Per-thread message cache â€” avoids reading the LangGraph checkpoint
        # on every thread switch.  Keys are thread_ids; values are the
        # hydrated ``messages`` lists.  ``message_cache_dirty`` marks
        # threads whose checkpoint has been written to by a background
        # task (memory extraction, dream cycle, detached run finalize,
        # summarization) and therefore MUST be re-read on next select.
        self.message_cache: dict[str, list[dict]] = {}
        self.message_cache_dirty: set[str] = set()

    # â”€â”€ Message-cache helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def cache_active_messages(self) -> None:
        """Snapshot ``self.messages`` into the per-thread cache."""
        tid = self.thread_id
        if tid:
            self.message_cache[tid] = list(self.messages)
            self.message_cache_dirty.discard(tid)

    def invalidate_thread_cache(self, thread_id: str | None) -> None:
        """Drop a cached thread (e.g. on delete)."""
        if not thread_id:
            return
        self.message_cache.pop(thread_id, None)
        self.message_cache_dirty.discard(thread_id)

    def mark_thread_dirty(self, thread_id: str | None) -> None:
        """Mark a thread's cache stale so the next select re-reads
        the checkpoint.  Safe to call with ``None`` / unknown ids."""
        if thread_id and thread_id in self.message_cache:
            self.message_cache_dirty.add(thread_id)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# PER-THREAD GENERATION STATE
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@dataclass
class GenerationState:
    """Tracks an in-flight generation for one thread.

    Allows concurrent streaming across multiple threads.  The consumer
    task checks ``detached`` before every UI update â€” when the user
    switches away, UI writes stop but accumulation continues so the
    response is ready when the user switches back.
    """
    thread_id: str
    q: queue.Queue
    stop_event: threading.Event
    config: dict
    enabled_tools: list
    cancel_scope: Any = None
    generation_id: str = ""
    deletion_token: str = ""
    orchestration_id: str = ""
    orchestration_required: bool = False
    orchestration_suspended: bool = False
    stop_requested_at: float = 0.0
    stop_reason: str = ""
    stopped_marker_rendered: bool = False
    buddy_terminal_emitted: bool = False
    cleanup_complete: bool = False
    created_at: float = field(default_factory=time.perf_counter)
    producer_thread_started_at: float = 0.0
    first_producer_event_at: float = 0.0
    first_answer_token_at: float = 0.0
    done_event_at: float = 0.0
    consumer_finalized_at: float = 0.0
    queue_empty_wait_ms: float = 0.0
    producer_wait_ms: float = 0.0
    finalization_ms: float = 0.0
    # Accumulated output
    accumulated: str = ""
    thinking_text: str = ""
    tool_results: list = field(default_factory=list)
    pending_tools: dict = field(default_factory=dict)
    live_agent_run_ids: set[str] = field(default_factory=set)
    live_async_agent_run_ids: set[str] = field(default_factory=set)
    live_skill_ids: set[str] = field(default_factory=set)
    baseline_child_agent_run_ids: set[str] = field(default_factory=set)
    chart_data: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    context_event_ids: set[int] = field(default_factory=set)
    captured_images: list = field(default_factory=list)
    captured_images_persist: list = field(default_factory=list)
    captured_videos: list = field(default_factory=list)
    captured_videos_persist: list = field(default_factory=list)
    browser_step_count: int = 0
    browser_preview_attempted: bool = False
    refresh_model_controls_on_done: bool = False
    interrupt_data: Any = None
    interrupt_rendered: bool = False
    status: str = "streaming"  # streaming | done | error | stopped
    error: str = ""
    # TTS / voice
    voice_mode: bool = False
    tts_active: bool = False
    tts_buffer: str = ""
    tts_spoken: int = 0
    tts_in_code: bool = False
    tts_allow_long: bool = False
    voice_control_queue: list = field(default_factory=list)
    queued_message_ids: list[str] = field(default_factory=list)
    realtime_tool_call_id: str = ""
    realtime_tool_name: str = ""
    realtime_consult_request: str = ""
    realtime_forced_consult: bool = False
    realtime_tool_output_sent: bool = False
    realtime_streamed_speech_chunks: int = 0
    realtime_stream_finalized: bool = False
    # Consumer tracking
    first_content: bool = False
    thinking_collapsed: bool = False
    # UI attachment â€” set to None when detached
    detached: bool = False
    assistant_md: Any = None
    thinking_label: Any = None
    thinking_md: Any = None
    thinking_expansion: Any = None
    thinking_code: Any = None
    tool_col: Any = None
    live_row: Any = None
    wrapper: Any = None


# Global registry of active generations keyed by thread_id
_active_generations: dict[str, GenerationState] = {}


# â”€â”€ Startup gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
startup_ready = False
startup_status = "Startingâ€¦"
startup_warnings: list[str] = []  # toast messages queued during startup


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# PER-CLIENT UI ELEMENT HOLDER
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class P:
    """Per-client page element references.

    Created once per ``@ui.page`` visit.  Passed to every builder function
    so they can read/write shared UI elements without closures.
    """
    main_col: ui.column = None          # type: ignore[assignment]
    chat_scroll: ui.scroll_area = None  # type: ignore[assignment]
    chat_container: ui.column = None    # type: ignore[assignment]
    thread_container: ui.column = None  # type: ignore[assignment]
    thread_filter_container: ui.row = None  # type: ignore[assignment]
    token_label: ui.label = None        # type: ignore[assignment]
    token_bar: ui.linear_progress = None  # type: ignore[assignment]
    context_meter: Any = None
    voice_status_label: ui.label = None  # type: ignore[assignment]
    stop_btn: ui.button = None          # type: ignore[assignment]
    voice_switch: Any = None           # type: ignore[assignment]
    dictate_btn: ui.button = None       # type: ignore[assignment]
    realtime_event_sink: ui.element = None  # type: ignore[assignment]
    realtime_client: Any = None
    active_voice_binding: Any = None
    pending_files: list[dict] = []
    transcript_thread_id: str | None = None
    transcript_generation: int = 0
    transcript_rendered_keys: list[str] = []
    transcript_window_start: int = 0
    transcript_window_size: int = 0
    transcript_total: int = 0
    transcript_requested_thread_id: str | None = None
    transcript_requested_start: int | None = None
    chat_shell_generation: int = 0
    chat_upload_js_installed: bool = False
    file_chips_row: ui.row = None       # type: ignore[assignment]
    chat_input: ui.textarea = None      # type: ignore[assignment]
    chat_header_label: ui.label = None  # type: ignore[assignment]
    model_banner_container: Any = None
    model_controls_container: Any = None
    refresh_model_controls: Any = None
    refresh_skill_chips: Any = None
    parent_agent_strip_container: Any = None
    refresh_parent_agent_strip: Any = None
    parent_agent_dialog_open: bool = False
    goal_strip_container: Any = None
    refresh_goal_strip: Any = None
    goal_strip_refresh_timer: Any = None
    live_control_container: Any = None
    live_control_refresh: Any = None
    live_control_cleanup: Any = None
    streaming_callbacks: Any = None
    settings_dlg: ui.dialog = None      # type: ignore[assignment]
    export_dlg: ui.dialog = None        # type: ignore[assignment]
    interrupt_dlg: ui.dialog = None     # type: ignore[assignment]
    task_dlg: ui.dialog = None          # type: ignore[assignment]
    # Terminal panel elements
    terminal_toggle_bar: Any = None
    terminal_panel: ui.column = None    # type: ignore[assignment]

    terminal_chevron: ui.button = None  # type: ignore[assignment]
    # Command Center (right drawer)
    command_center_col: ui.column = None  # type: ignore[assignment]
    # Sidebar avatar
    sidebar_avatar: Any = None
    sidebar_avatar_label: Any = None
    # Designer
    designer_preview_container: Any = None
    designer_chat_container: Any = None
    designer_page_nav: Any = None
    developer_approval_container: Any = None

    def __init__(self) -> None:
        self.pending_files = []
        self.active_voice_binding = None
        self.live_control_container = None
        self.live_control_refresh = None
        self.live_control_cleanup = None
        self.streaming_callbacks = None
        self.chat_shell_generation = 0
        self.chat_upload_js_installed = False
        self.parent_agent_dialog_open = False
        self.refresh_skill_chips = None
