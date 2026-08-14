from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time
from typing import Literal

from row_bot.voice import VoiceService


VoiceMode = Literal["talk", "dictate"]
VoiceTransport = Literal["local", "browser", "realtime"]


@dataclass(frozen=True)
class VoiceCoordinatorEvent:
    session_id: int
    name: str
    mode: str
    detail: str = ""


class VoiceSessionCoordinator:
    """Owns the active Talk/Dictate session around the existing VoiceService."""

    def __init__(self, voice_service: VoiceService) -> None:
        self.voice_service = voice_service
        self.mode: VoiceMode = "talk"
        self.transport: VoiceTransport = "local"
        self.realtime_state = "stopped"
        self.realtime_last_error = ""
        self._session_id = 0
        self._active = False
        self._events: deque[VoiceCoordinatorEvent] = deque(maxlen=200)
        self.output_activity = OutputActivityTracker()
        self.no_speech_timeout_seconds = 45.0
        self._last_activity_at = time.monotonic()
        self.active_realtime_response_id = ""
        self.active_realtime_output_item_id = ""
        self.active_row_bot_generation_id = ""
        self.queued_realtime_tool_call: dict[str, str] | None = None
        self.playback_active = False
        self.barge_in_reason = ""
        self._realtime_started_at = 0.0
        self._realtime_latency_marks: dict[str, float] = {}
        self._realtime_speech_window_id = 0
        self._realtime_completed_speech_window_id = 0
        self._realtime_last_transcript_window_id = 0
        self._realtime_transcript_item_ids: deque[str] = deque(maxlen=200)

    @property
    def session_id(self) -> int:
        return self._session_id

    @property
    def is_running(self) -> bool:
        if self.transport == "realtime":
            return self._active and self.realtime_state not in {"stopped", "error"}
        if self.transport == "browser":
            return self._active
        return self._active and self.voice_service.is_running

    @property
    def state(self) -> str:
        if not self._active:
            return "stopped"
        if self.transport == "realtime":
            return self.realtime_state
        if self.transport == "browser":
            return "listening"
        return self.voice_service.state

    def start_talk(self) -> int:
        return self.start("talk")

    def start_realtime_talk(self) -> int:
        if self._active:
            self.stop()
        self._session_id += 1
        self.mode = "talk"
        self.transport = "realtime"
        self.realtime_state = "connecting"
        self.realtime_last_error = ""
        self._active = True
        self._last_activity_at = time.monotonic()
        self._reset_realtime_runtime_state()
        self.mark_realtime_latency("talk_activation")
        self._emit("session_started")
        self._emit("realtime_connecting")
        return self._session_id

    def start_browser(self, mode: VoiceMode = "talk") -> int:
        """Start browser capture without opening a server audio device."""
        if self._active:
            self.stop()
        self._session_id += 1
        self.mode = mode
        self.transport = "browser"
        self.realtime_state = "stopped"
        self._active = True
        self._last_activity_at = time.monotonic()
        self._emit("session_started")
        self._emit("browser_capture_started")
        return self._session_id

    def start_dictation(self) -> int:
        return self.start("dictate")

    def start(self, mode: VoiceMode) -> int:
        if self._active and self.mode == mode and self.voice_service.is_running:
            return self._session_id
        if self._active:
            self.stop()
        self._session_id += 1
        self.mode = mode
        self.transport = "local"
        self.realtime_state = "stopped"
        self._active = True
        self._emit("session_started")
        self._apply_selected_local_stt_model(mode)
        self.voice_service.start()
        self._last_activity_at = time.monotonic()
        return self._session_id

    def _apply_selected_local_stt_model(self, mode: VoiceMode) -> None:
        try:
            from row_bot.voice.runtime import load_voice_runtime_settings

            settings = load_voice_runtime_settings()
            selected_model = (
                settings.dictation_model if mode == "dictate" else settings.talk_model
            )
            if selected_model:
                self.voice_service.stt_model = selected_model
        except Exception:
            return

    def stop(self, *, session_id: int | None = None) -> None:
        if session_id is not None and session_id != self._session_id:
            self._emit("stale_stop_ignored", detail=str(session_id))
            return
        was_active = self._active
        self._active = False
        if self.transport == "local":
            self.voice_service.stop()
        elif self.transport == "realtime":
            self.realtime_state = "stopped"
            self._reset_realtime_runtime_state(keep_latency=True)
        if was_active:
            self._emit("session_stopped")

    def mute(self) -> None:
        if self._active and self.voice_service.is_running:
            self.voice_service.mute()
            self.output_activity.mark_started()
            self._emit("assistant_audio_started")

    def unmute(self) -> None:
        if self._active and self.voice_service.is_running:
            self.voice_service.unmute()
            self.output_activity.mark_finished()
            self._emit("assistant_audio_finished")

    def get_status(self) -> str | None:
        if not self._active:
            return None
        if self.transport == "realtime":
            if self.realtime_state == "error" and self.realtime_last_error:
                return self.realtime_last_error
            return self.realtime_state
        return self.voice_service.get_status()

    def get_transcription(self) -> str | None:
        if not self._active or self.transport == "realtime":
            return None
        text = self.voice_service.get_transcription()
        if text and self._should_drop_transcript(text):
            self._emit("transcript_dropped", detail=text)
            return None
        if text:
            self._last_activity_at = time.monotonic()
            self._emit("transcript_final", detail=text)
        return text

    def accepts(self, session_id: int) -> bool:
        return self._active and session_id == self._session_id

    def drain_events(self) -> list[VoiceCoordinatorEvent]:
        events = list(self._events)
        self._events.clear()
        return events

    def diagnostic_snapshot(self) -> dict[str, object]:
        return {
            "session_id": self._session_id,
            "active": self._active,
            "mode": self.mode,
            "transport": self.transport,
            "state": self.state,
            "realtime_state": self.realtime_state,
            "realtime_last_error": self.realtime_last_error,
            "active_realtime_response_id": self.active_realtime_response_id,
            "active_realtime_output_item_id": self.active_realtime_output_item_id,
            "active_row_bot_generation_id": self.active_row_bot_generation_id,
            "playback_active": self.playback_active,
            "barge_in_reason": self.barge_in_reason,
            "realtime_latency_ms": self.realtime_latency_breakdown(),
            "realtime_latency_summary_ms": self.realtime_latency_summary(),
            "realtime_speech_window_id": self._realtime_speech_window_id,
            "realtime_completed_speech_window_id": self._realtime_completed_speech_window_id,
        }

    def record_assistant_output(self, text: str) -> None:
        self.output_activity.record_output(text)

    def set_realtime_state(self, state: str, *, detail: str = "", session_id: int | None = None) -> None:
        if session_id is not None and session_id != self._session_id:
            self._emit("stale_realtime_event_ignored", detail=str(session_id))
            return
        if not self._active or self.transport != "realtime":
            return
        normalized = str(state or "connected")
        self.realtime_state = normalized
        if normalized == "error":
            self.realtime_last_error = detail
        if normalized in {"listening", "connected", "speech_started", "speech_stopped"}:
            self._last_activity_at = time.monotonic()
        if normalized in {"listening", "connected"}:
            self.mark_realtime_latency("connected")
        elif normalized == "user_speaking":
            self.mark_realtime_latency("speech_started")
        elif normalized == "thinking":
            self.mark_realtime_latency("thinking_ui")
        elif normalized == "speaking":
            self.mark_realtime_latency("assistant_audio_started")
        self._emit(f"realtime_{normalized}", detail=detail)

    def queue_realtime_tool_call(self, call: dict[str, object]) -> None:
        if self.queued_realtime_tool_call:
            self._emit("realtime_tool_call_replaced", detail=self.queued_realtime_tool_call["name"])
        self.queued_realtime_tool_call = {
            "call_id": str(call.get("call_id") or ""),
            "name": str(call.get("name") or ""),
            "request": str(call.get("request") or ""),
        }
        self._emit("realtime_tool_call_queued", detail=self.queued_realtime_tool_call["name"])

    def consume_realtime_tool_call(self) -> dict[str, str] | None:
        call = self.queued_realtime_tool_call
        self.queued_realtime_tool_call = None
        return call

    def set_active_row_bot_generation(self, generation_id: str) -> None:
        self.active_row_bot_generation_id = str(generation_id or "")
        self.mark_realtime_latency("row_bot_consult_started")
        self._emit("realtime_row_bot_generation_active", detail=self.active_row_bot_generation_id)

    def clear_active_row_bot_generation(self, *, generation_id: str = "") -> None:
        if generation_id and generation_id != self.active_row_bot_generation_id:
            self._emit("stale_row_bot_generation_clear_ignored", detail=str(generation_id))
            return
        if self.active_row_bot_generation_id:
            self._emit("realtime_row_bot_generation_finished", detail=self.active_row_bot_generation_id)
        self.active_row_bot_generation_id = ""

    def record_realtime_output_started(
        self,
        *,
        response_id: str = "",
        output_item_id: str = "",
        session_id: int | None = None,
    ) -> None:
        if session_id is not None and session_id != self._session_id:
            self._emit("stale_realtime_output_ignored", detail=str(session_id))
            return
        self.active_realtime_response_id = str(response_id or self.active_realtime_response_id or "")
        self.active_realtime_output_item_id = str(output_item_id or self.active_realtime_output_item_id or "")
        self.playback_active = True
        self.mark_realtime_latency("first_assistant_audio")
        self.set_realtime_state("speaking", session_id=session_id)

    def record_realtime_output_done(
        self,
        *,
        session_id: int | None = None,
        clear_generation: bool = False,
    ) -> None:
        if session_id is not None and session_id != self._session_id:
            self._emit("stale_realtime_output_done_ignored", detail=str(session_id))
            return
        self.playback_active = False
        self.active_realtime_response_id = ""
        self.active_realtime_output_item_id = ""
        self.barge_in_reason = ""
        if clear_generation:
            self.clear_active_row_bot_generation()
        self.mark_realtime_latency("response_done")
        self.set_realtime_state("listening", session_id=session_id)

    def record_barge_in(self, *, reason: str, session_id: int | None = None) -> bool:
        if session_id is not None and session_id != self._session_id:
            self._emit("stale_realtime_barge_in_ignored", detail=str(session_id))
            return False
        if not self.playback_active and not self.active_realtime_response_id and not self.active_realtime_output_item_id:
            self._emit("realtime_barge_in_ignored", detail=str(reason or "no_active_output"))
            return False
        self.barge_in_reason = str(reason or "")
        self.playback_active = False
        self.mark_realtime_latency("barge_in")
        self.set_realtime_state("interrupted", detail=self.barge_in_reason, session_id=session_id)
        return True

    def record_realtime_speech_started(self, *, session_id: int | None = None) -> None:
        if session_id is not None and session_id != self._session_id:
            self._emit("stale_realtime_speech_started_ignored", detail=str(session_id))
            return
        if not self._active or self.transport != "realtime":
            return
        self._realtime_speech_window_id += 1
        self.mark_realtime_latency("speech_started")
        self.set_realtime_state("user_speaking", session_id=session_id)
        self._emit("realtime_speech_window_started", detail=str(self._realtime_speech_window_id))

    def record_realtime_speech_stopped(self, *, session_id: int | None = None) -> None:
        if session_id is not None and session_id != self._session_id:
            self._emit("stale_realtime_speech_stopped_ignored", detail=str(session_id))
            return
        if not self._active or self.transport != "realtime":
            return
        self._realtime_completed_speech_window_id = self._realtime_speech_window_id
        self.mark_realtime_latency("speech_stopped")
        self.set_realtime_state("listening", detail="speech_stopped", session_id=session_id)
        self._emit("realtime_speech_window_stopped", detail=str(self._realtime_completed_speech_window_id))

    def record_realtime_transcript(
        self,
        text: str,
        *,
        session_id: int | None = None,
        item_id: str = "",
    ) -> str | None:
        if session_id is not None and session_id != self._session_id:
            self._emit("stale_realtime_transcript_ignored", detail=str(session_id))
            return None
        if not self._active or self.transport != "realtime":
            return None
        clean_item_id = str(item_id or "").strip()
        if clean_item_id and clean_item_id in self._realtime_transcript_item_ids:
            self._emit("duplicate_realtime_transcript_ignored", detail=clean_item_id)
            return None
        if self._realtime_completed_speech_window_id <= self._realtime_last_transcript_window_id:
            self._emit("realtime_transcript_without_speech_window_ignored", detail=clean_item_id)
            return None
        if text and self._should_drop_transcript(text, drop_fragments=True):
            self._emit("transcript_dropped", detail=text)
            if clean_item_id:
                self._realtime_transcript_item_ids.append(clean_item_id)
            self._realtime_last_transcript_window_id = self._realtime_completed_speech_window_id
            return None
        if text:
            if clean_item_id:
                self._realtime_transcript_item_ids.append(clean_item_id)
            self._realtime_last_transcript_window_id = self._realtime_completed_speech_window_id
            self._last_activity_at = time.monotonic()
            self._emit("transcript_final", detail=text)
            return text
        return None

    def mark_realtime_latency(self, name: str, *, now: float | None = None) -> None:
        if not name:
            return
        current = time.monotonic() if now is None else now
        if not self._realtime_started_at:
            self._realtime_started_at = current
        self._realtime_latency_marks.setdefault(str(name), current)

    def realtime_latency_breakdown(self) -> dict[str, int]:
        if not self._realtime_started_at:
            return {}
        return {
            name: int(max(0.0, (value - self._realtime_started_at) * 1000))
            for name, value in self._realtime_latency_marks.items()
        }

    def realtime_latency_summary(self) -> dict[str, int]:
        marks = self._realtime_latency_marks

        def delta(start: str, end: str) -> int | None:
            if start not in marks or end not in marks:
                return None
            return int(max(0.0, (marks[end] - marks[start]) * 1000))

        pairs = {
            "speech_stop_to_thinking_ui": delta("speech_stopped", "thinking_ui"),
            "speech_stop_to_transcript_final": delta("speech_stopped", "transcript_final"),
            "speech_stop_to_function_call_ready": delta("speech_stopped", "function_call_ready"),
            "speech_stop_to_forced_consult": delta("speech_stopped", "forced_consult_started"),
            "speech_stop_to_row_bot_start": delta("speech_stopped", "row_bot_consult_started"),
            "speech_stop_to_first_token": delta("speech_stopped", "first_token"),
            "speech_stop_to_first_speakable_chunk": delta("speech_stopped", "first_speakable_chunk"),
            "speech_stop_to_first_spoken_row_bot": delta("speech_stopped", "first_substantive_audio_requested"),
            "transcript_final_to_forced_consult": delta("transcript_final", "forced_consult_started"),
            "consult_to_first_token": delta("row_bot_consult_started", "first_token"),
            "first_token_to_first_speakable_chunk": delta("first_token", "first_speakable_chunk"),
            "first_speakable_chunk_to_provider_response_create": delta("first_speakable_chunk", "provider_response_create"),
            "provider_response_create_to_first_audio": delta("provider_response_create", "first_assistant_audio"),
        }
        return {key: value for key, value in pairs.items() if value is not None}

    def no_speech_timed_out(self, *, now: float | None = None) -> bool:
        if not self._active or self.state != "listening":
            return False
        current = time.monotonic() if now is None else now
        return (current - self._last_activity_at) >= self.no_speech_timeout_seconds

    def _should_drop_transcript(self, text: str, *, drop_fragments: bool = False) -> bool:
        normalized = _normalize_transcript(text)
        if normalized in PHANTOM_TRANSCRIPTS:
            return True
        if drop_fragments and len(normalized) < 8 and not normalized.endswith("?"):
            return True
        return self.output_activity.should_drop_echo(normalized)

    def _reset_realtime_runtime_state(self, *, keep_latency: bool = False) -> None:
        self.active_realtime_response_id = ""
        self.active_realtime_output_item_id = ""
        self.active_row_bot_generation_id = ""
        self.queued_realtime_tool_call = None
        self.playback_active = False
        self.barge_in_reason = ""
        self._realtime_speech_window_id = 0
        self._realtime_completed_speech_window_id = 0
        self._realtime_last_transcript_window_id = 0
        self._realtime_transcript_item_ids.clear()
        if not keep_latency:
            self._realtime_started_at = 0.0
            self._realtime_latency_marks = {}

    def _emit(self, name: str, *, detail: str = "") -> None:
        self._events.append(VoiceCoordinatorEvent(
            session_id=self._session_id,
            name=name,
            mode=self.mode,
            detail=detail,
        ))


PHANTOM_TRANSCRIPTS = {
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subscribe to my channel",
}


class OutputActivityTracker:
    def __init__(self) -> None:
        self.active = False
        self._recent_output = ""

    def mark_started(self) -> None:
        self.active = True

    def mark_finished(self) -> None:
        self.active = False

    def record_output(self, text: str) -> None:
        normalized = _normalize_transcript(text)
        if normalized:
            self._recent_output = normalized[-500:]

    def should_drop_echo(self, normalized_transcript: str) -> bool:
        if not normalized_transcript or not self._recent_output:
            return False
        if normalized_transcript == self._recent_output:
            return True
        return normalized_transcript in self._recent_output


def _normalize_transcript(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())
