from __future__ import annotations

from queue import SimpleQueue

from row_bot.voice.coordinator import VoiceSessionCoordinator


class FakeVoiceService:
    def __init__(self) -> None:
        self.state = "stopped"
        self.results = SimpleQueue()
        self.started = 0
        self.stopped = 0
        self.muted = 0
        self.unmuted = 0
        self.stt_model = ""

    @property
    def is_running(self) -> bool:
        return self.state != "stopped"

    def start(self) -> None:
        self.started += 1
        self.state = "listening"

    def stop(self) -> None:
        self.stopped += 1
        self.state = "stopped"

    def mute(self) -> None:
        self.muted += 1
        self.state = "muted"

    def unmute(self) -> None:
        self.unmuted += 1
        self.state = "listening"

    def get_status(self) -> str | None:
        return self.state

    def get_transcription(self) -> str | None:
        if self.results.empty():
            return None
        return self.results.get_nowait()


def test_coordinator_starts_only_one_voice_mode_at_a_time():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    talk_session = coordinator.start_talk()
    dictation_session = coordinator.start_dictation()

    assert talk_session != dictation_session
    assert coordinator.mode == "dictate"
    assert service.stopped == 1
    assert service.started == 2


def test_coordinator_applies_selected_local_stt_model(monkeypatch):
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "row_bot.voice.runtime.load_voice_runtime_settings",
        lambda: type(
            "Settings",
            (),
            {
                "talk_model": "local-whisper-small",
                "dictation_model": "local-funasr-sensevoice",
            },
        )(),
    )

    coordinator.start_dictation()

    assert service.stt_model == "local-funasr-sensevoice"


def test_coordinator_ignores_stale_stop_and_stale_unmute_after_stop():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    old_session = coordinator.start_talk()
    new_session = coordinator.start_dictation()
    coordinator.stop(session_id=old_session)

    assert coordinator.accepts(old_session) is False
    assert coordinator.accepts(new_session) is True
    assert service.is_running is True

    coordinator.stop()
    coordinator.unmute()

    assert service.is_running is False
    assert service.unmuted == 0


def test_coordinator_drops_transcripts_when_stopped():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    coordinator.start_talk()
    service.results.put("hello")
    assert coordinator.get_transcription() == "hello"

    coordinator.stop()
    service.results.put("stale")
    assert coordinator.get_transcription() is None


def test_coordinator_drops_phantom_and_echo_transcripts():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    coordinator.start_talk()
    service.results.put("Thanks for watching")
    assert coordinator.get_transcription() is None

    coordinator.record_assistant_output("The answer is visible in the app.")
    coordinator.mute()
    service.results.put("answer is visible")
    assert coordinator.get_transcription() is None


def test_coordinator_no_speech_timeout_only_while_listening():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]
    coordinator.no_speech_timeout_seconds = 10

    coordinator.start_talk()
    assert (
        coordinator.no_speech_timed_out(now=coordinator._last_activity_at + 11) is True
    )

    coordinator.mute()
    assert (
        coordinator.no_speech_timed_out(now=coordinator._last_activity_at + 99) is False
    )


def test_coordinator_realtime_lifecycle_and_stale_events():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()

    assert coordinator.transport == "realtime"
    assert coordinator.state == "connecting"
    assert service.started == 0

    coordinator.set_realtime_state("listening", session_id=session_id)
    assert coordinator.state == "listening"

    coordinator.set_realtime_state("error", detail="old", session_id=session_id - 1)
    assert coordinator.state == "listening"

    assert (
        coordinator.record_realtime_transcript(
            "hello", session_id=session_id, item_id="early"
        )
        is None
    )
    coordinator.record_realtime_speech_started(session_id=session_id)
    coordinator.record_realtime_speech_stopped(session_id=session_id)
    assert coordinator.state == "listening"
    assert (
        coordinator.record_realtime_transcript(
            "hello there", session_id=session_id, item_id="item-1"
        )
        == "hello there"
    )
    assert (
        coordinator.record_realtime_transcript("stale", session_id=session_id - 1)
        is None
    )

    coordinator.stop()
    assert coordinator.state == "stopped"


def test_coordinator_exposes_realtime_diagnostic_snapshot():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()
    coordinator.set_realtime_state("thinking", session_id=session_id)

    snapshot = coordinator.diagnostic_snapshot()

    assert snapshot["session_id"] == session_id
    assert snapshot["active"] is True
    assert snapshot["mode"] == "talk"
    assert snapshot["transport"] == "realtime"
    assert snapshot["state"] == "thinking"
    assert snapshot["realtime_state"] == "thinking"
    assert snapshot["playback_active"] is False


def test_coordinator_tracks_realtime_tool_output_and_barge_in_lifecycle():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()
    coordinator.queue_realtime_tool_call(
        {"call_id": "call_1", "name": "row_bot_agent_consult", "request": "do work"}
    )
    assert coordinator.consume_realtime_tool_call() == {
        "call_id": "call_1",
        "name": "row_bot_agent_consult",
        "request": "do work",
    }
    assert coordinator.consume_realtime_tool_call() is None

    coordinator.record_realtime_output_started(
        response_id="resp_1", output_item_id="item_1", session_id=session_id
    )
    assert coordinator.state == "speaking"
    assert coordinator.diagnostic_snapshot()["playback_active"] is True
    assert coordinator.diagnostic_snapshot()["active_realtime_response_id"] == "resp_1"

    coordinator.record_barge_in(reason="user_speech_started", session_id=session_id)
    assert coordinator.state == "interrupted"
    assert coordinator.diagnostic_snapshot()["barge_in_reason"] == "user_speech_started"

    coordinator.record_realtime_output_done(session_id=session_id)
    assert coordinator.state == "listening"
    assert coordinator.diagnostic_snapshot()["playback_active"] is False
    assert coordinator.diagnostic_snapshot()["barge_in_reason"] == ""


def test_coordinator_resets_realtime_runtime_state_between_sessions():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    first_session = coordinator.start_realtime_talk()
    coordinator.queue_realtime_tool_call(
        {"call_id": "call_1", "name": "row_bot_agent_consult", "request": "do work"}
    )
    coordinator.set_active_row_bot_generation("gen_1")
    coordinator.record_realtime_output_started(
        response_id="resp_1", output_item_id="item_1", session_id=first_session
    )
    coordinator.record_barge_in(reason="user_speech_started", session_id=first_session)

    second_session = coordinator.start_realtime_talk()
    snapshot = coordinator.diagnostic_snapshot()

    assert second_session != first_session
    assert snapshot["state"] == "connecting"
    assert snapshot["active_realtime_response_id"] == ""
    assert snapshot["active_realtime_output_item_id"] == ""
    assert snapshot["active_row_bot_generation_id"] == ""
    assert snapshot["playback_active"] is False
    assert snapshot["barge_in_reason"] == ""
    assert coordinator.consume_realtime_tool_call() is None


def test_coordinator_ignores_realtime_barge_in_without_active_output():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()
    coordinator.set_realtime_state("listening", session_id=session_id)

    accepted = coordinator.record_barge_in(
        reason="connect_time_cancel", session_id=session_id
    )

    assert accepted is False
    assert coordinator.state == "listening"
    assert coordinator.diagnostic_snapshot()["barge_in_reason"] == ""
    assert any(
        event.name == "realtime_barge_in_ignored"
        for event in coordinator.drain_events()
    )


def test_coordinator_clears_active_generation_when_final_response_completes():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()
    coordinator.set_active_row_bot_generation("gen_1")
    coordinator.record_realtime_output_started(
        response_id="resp_1", output_item_id="item_1", session_id=session_id
    )

    coordinator.record_realtime_output_done(
        session_id=session_id, clear_generation=True
    )

    snapshot = coordinator.diagnostic_snapshot()
    assert snapshot["active_row_bot_generation_id"] == ""
    assert snapshot["active_realtime_response_id"] == ""
    assert snapshot["active_realtime_output_item_id"] == ""
    assert snapshot["state"] == "listening"


def test_coordinator_records_realtime_latency_per_session():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    first_session = coordinator.start_realtime_talk()
    coordinator.mark_realtime_latency("mic_permission", now=101.0)
    coordinator.mark_realtime_latency("connected", now=102.0)
    first_breakdown = coordinator.diagnostic_snapshot()["realtime_latency_ms"]

    assert isinstance(first_breakdown, dict)
    assert "talk_activation" in first_breakdown
    assert first_breakdown["mic_permission"] >= 0
    assert first_breakdown["connected"] >= first_breakdown["mic_permission"]

    second_session = coordinator.start_realtime_talk()
    second_breakdown = coordinator.diagnostic_snapshot()["realtime_latency_ms"]

    assert second_session != first_session
    assert set(second_breakdown) == {"talk_activation"}


def test_coordinator_summarizes_realtime_turn_latency():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    coordinator.start_realtime_talk()
    coordinator.mark_realtime_latency("speech_stopped", now=100.0)
    coordinator.mark_realtime_latency("thinking_ui", now=100.01)
    coordinator.mark_realtime_latency("transcript_final", now=100.25)
    coordinator.mark_realtime_latency("function_call_ready", now=100.3)
    coordinator.mark_realtime_latency("forced_consult_started", now=100.4)
    coordinator.mark_realtime_latency("row_bot_consult_started", now=100.5)
    coordinator.mark_realtime_latency("first_token", now=101.0)
    coordinator.mark_realtime_latency("first_speakable_chunk", now=101.25)
    coordinator.mark_realtime_latency("first_substantive_audio_requested", now=101.3)
    coordinator.mark_realtime_latency("provider_response_create", now=101.5)
    coordinator.mark_realtime_latency("first_assistant_audio", now=102.0)

    summary = coordinator.realtime_latency_summary()

    assert summary == {
        "speech_stop_to_thinking_ui": 10,
        "speech_stop_to_transcript_final": 250,
        "speech_stop_to_function_call_ready": 299,
        "speech_stop_to_forced_consult": 400,
        "speech_stop_to_row_bot_start": 500,
        "speech_stop_to_first_token": 1000,
        "speech_stop_to_first_speakable_chunk": 1250,
        "speech_stop_to_first_spoken_row_bot": 1299,
        "transcript_final_to_forced_consult": 150,
        "consult_to_first_token": 500,
        "first_token_to_first_speakable_chunk": 250,
        "first_speakable_chunk_to_provider_response_create": 250,
        "provider_response_create_to_first_audio": 500,
    }
    assert coordinator.diagnostic_snapshot()["realtime_latency_summary_ms"] == summary


def test_coordinator_drops_realtime_transcript_without_speech_window():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()

    assert (
        coordinator.record_realtime_transcript(
            "please inspect the repo", session_id=session_id, item_id="item-1"
        )
        is None
    )
    assert any(
        event.name == "realtime_transcript_without_speech_window_ignored"
        for event in coordinator.drain_events()
    )


def test_coordinator_keeps_realtime_listening_after_idle_speech_stop():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()
    coordinator.set_realtime_state("listening", session_id=session_id)
    coordinator.record_realtime_speech_started(session_id=session_id)
    coordinator.record_realtime_speech_stopped(session_id=session_id)

    assert coordinator.state == "listening"
    assert any(
        event.name == "realtime_speech_window_stopped"
        for event in coordinator.drain_events()
    )


def test_coordinator_drops_duplicate_realtime_transcript_item_ids():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()
    coordinator.record_realtime_speech_started(session_id=session_id)
    coordinator.record_realtime_speech_stopped(session_id=session_id)

    assert coordinator.record_realtime_transcript(
        "please inspect the repo", session_id=session_id, item_id="item-1"
    )
    coordinator.record_realtime_speech_started(session_id=session_id)
    coordinator.record_realtime_speech_stopped(session_id=session_id)
    assert (
        coordinator.record_realtime_transcript(
            "please inspect the repo", session_id=session_id, item_id="item-1"
        )
        is None
    )


def test_coordinator_drops_realtime_assistant_echo_and_short_fragments():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()
    coordinator.record_assistant_output("The answer is visible in the app.")
    coordinator.record_realtime_output_started(
        response_id="resp", output_item_id="out", session_id=session_id
    )
    coordinator.record_realtime_speech_started(session_id=session_id)
    coordinator.record_realtime_speech_stopped(session_id=session_id)

    assert (
        coordinator.record_realtime_transcript(
            "answer is visible", session_id=session_id, item_id="item-1"
        )
        is None
    )

    coordinator.record_realtime_speech_started(session_id=session_id)
    coordinator.record_realtime_speech_stopped(session_id=session_id)
    assert (
        coordinator.record_realtime_transcript(
            "ok", session_id=session_id, item_id="item-2"
        )
        is None
    )


def test_coordinator_accepts_realtime_question_fragment_after_speech_window():
    service = FakeVoiceService()
    coordinator = VoiceSessionCoordinator(service)  # type: ignore[arg-type]

    session_id = coordinator.start_realtime_talk()
    coordinator.record_realtime_speech_started(session_id=session_id)
    coordinator.record_realtime_speech_stopped(session_id=session_id)

    assert (
        coordinator.record_realtime_transcript(
            "why?", session_id=session_id, item_id="item-1"
        )
        == "why?"
    )
