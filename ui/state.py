"""Thoth UI — shared application state.

Contains the singleton ``AppState``, the per-thread ``GenerationState``
dataclass, the per-client ``P`` page-element holder and the global
``_active_generations`` registry.

Every UI module receives these objects as explicit parameters — no hidden
closure captures.
"""

from __future__ import annotations

import threading
import queue
from dataclasses import dataclass, field
from typing import Any

from models import get_current_model, get_user_context_size
from voice import get_voice_service
from tts import TTSService
from vision import VisionService
from tools.vision_tool import set_vision_service
from nicegui import ui


# ═════════════════════════════════════════════════════════════════════════════
# SHARED APPLICATION STATE (module-level singleton)
# ═════════════════════════════════════════════════════════════════════════════

class AppState:
    """Shared backend state — lives for the lifetime of the server process."""

    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.thread_name: str | None = None
        self.thread_model_override: str = ""  # cloud model override for current thread
        self.messages: list[dict] = []
        self.current_model: str = get_current_model()
        self.context_size: int = get_user_context_size()
        self.is_generating: bool = False
        self.stop_event: threading.Event = threading.Event()
        self.pending_interrupt: dict | None = None
        self.show_onboarding: bool = False  # set by helpers._is_first_run()
        self.open_setup_center_on_next_load: bool = False
        self.voice_enabled: bool = False
        self.voice_service = get_voice_service()
        self.tts_service = TTSService()
        self.vision_service = VisionService()
        self.tts_service.voice_service = self.voice_service
        set_vision_service(self.vision_service)
        self.attached_data_cache: dict[str, bytes] = {}
        self.active_designer_project = None  # DesignerProject | None
        self.preferred_home_tab: str | None = None  # tab to select on next rebuild
        # Per-thread message cache — avoids reading the LangGraph checkpoint
        # on every thread switch.  Keys are thread_ids; values are the
        # hydrated ``messages`` lists.  ``message_cache_dirty`` marks
        # threads whose checkpoint has been written to by a background
        # task (memory extraction, dream cycle, detached run finalize,
        # summarization) and therefore MUST be re-read on next select.
        self.message_cache: dict[str, list[dict]] = {}
        self.message_cache_dirty: set[str] = set()

    # ── Message-cache helpers ────────────────────────────────────────
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


# ═════════════════════════════════════════════════════════════════════════════
# PER-THREAD GENERATION STATE
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class GenerationState:
    """Tracks an in-flight generation for one thread.

    Allows concurrent streaming across multiple threads.  The consumer
    task checks ``detached`` before every UI update — when the user
    switches away, UI writes stop but accumulation continues so the
    response is ready when the user switches back.
    """
    thread_id: str
    q: queue.Queue
    stop_event: threading.Event
    config: dict
    enabled_tools: list
    # Accumulated output
    accumulated: str = ""
    thinking_text: str = ""
    tool_results: list = field(default_factory=list)
    pending_tools: dict = field(default_factory=dict)
    chart_data: list = field(default_factory=list)
    captured_images: list = field(default_factory=list)
    captured_images_persist: list = field(default_factory=list)
    captured_videos: list = field(default_factory=list)
    captured_videos_persist: list = field(default_factory=list)
    interrupt_data: Any = None
    status: str = "streaming"  # streaming | done | error | stopped
    error: str = ""
    # TTS / voice
    voice_mode: bool = False
    tts_active: bool = False
    tts_buffer: str = ""
    tts_spoken: int = 0
    tts_in_code: bool = False
    # Consumer tracking
    first_content: bool = False
    thinking_collapsed: bool = False
    # UI attachment — set to None when detached
    detached: bool = False
    assistant_md: Any = None
    thinking_label: Any = None
    thinking_md: Any = None
    tool_col: Any = None
    wrapper: Any = None


# Global registry of active generations keyed by thread_id
_active_generations: dict[str, GenerationState] = {}


# ── Startup gate ─────────────────────────────────────────────────────────
startup_ready = False
startup_status = "Starting…"
startup_warnings: list[str] = []  # toast messages queued during startup


# ═════════════════════════════════════════════════════════════════════════════
# PER-CLIENT UI ELEMENT HOLDER
# ═════════════════════════════════════════════════════════════════════════════

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
    voice_status_label: ui.label = None  # type: ignore[assignment]
    stop_btn: ui.button = None          # type: ignore[assignment]
    voice_switch: ui.switch = None      # type: ignore[assignment]
    pending_files: list[dict] = []
    file_chips_row: ui.row = None       # type: ignore[assignment]
    chat_input: ui.textarea = None      # type: ignore[assignment]
    chat_header_label: ui.label = None  # type: ignore[assignment]
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

    def __init__(self) -> None:
        self.pending_files = []
