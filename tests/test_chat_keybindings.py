from pathlib import Path
import asyncio
import re
from types import SimpleNamespace

from row_bot.ui.chat_components import _submit_voice_transcript
from row_bot.ui.voice_lifecycle import stop_voice_for_thread_change


ROOT = Path(__file__).resolve().parents[1]


def test_composer_exposes_talk_and_dictate_only_for_voice():
    chat_src = (ROOT / "src" / "row_bot" / "ui" / "chat.py").read_text(encoding="utf-8")
    components_src = (ROOT / "src" / "row_bot" / "ui" / "chat_components.py").read_text(encoding="utf-8")
    app_src = (ROOT / "src" / "row_bot" / "app.py").read_text(encoding="utf-8")

    for source in (chat_src, components_src):
        assert 'ui.button(icon="record_voice_over"' in source
        assert 'ui.button(icon="keyboard_voice"' in source
        assert 'ui.switch("Talk"' not in source
        assert 'ui.switch("ðŸŽ¤ Voice"' not in source
        assert "_set_talk_button_active" in source
        assert "_set_dictate_button_active" in source
        assert 'state.voice_input_mode = "talk"' in source
        assert 'state.voice_input_mode = "dictate"' in source

    assert 'state.voice_input_mode == "dictate"' in app_src
    assert "append_dictation_text" in app_src


def test_talk_status_uses_existing_composer_status_surface():
    app_src = (ROOT / "src" / "row_bot" / "app.py").read_text(encoding="utf-8")
    streaming_src = (ROOT / "src" / "row_bot" / "ui" / "streaming.py").read_text(encoding="utf-8")

    assert 'state.voice_input_mode == "talk"' in app_src
    assert "Waiting for approval..." in app_src
    assert "Using Browser..." in app_src
    assert "Using Tool..." in app_src
    assert "Thinking..." in app_src
    assert "VoiceAgentBridge" in app_src
    assert "submit_user_transcript" in app_src
    assert "tts_active=voice_mode and (" in streaming_src
    assert "state.tts_service.enabled" in streaming_src
    assert 'state.voice_coordinator.transport in {"realtime", "browser"}' in streaming_src
    assert "state.voice_coordinator.unmute()" in streaming_src


def test_talk_provider_selection_wires_realtime_without_third_mode():
    chat_src = (ROOT / "src" / "row_bot" / "ui" / "chat.py").read_text(encoding="utf-8")
    components_src = (ROOT / "src" / "row_bot" / "ui" / "chat_components.py").read_text(encoding="utf-8")
    streaming_src = (ROOT / "src" / "row_bot" / "ui" / "streaming.py").read_text(encoding="utf-8")

    for source in (chat_src, components_src):
        assert 'talk_provider == "openai_realtime"' in source
        assert "start_realtime_talk" in source
        assert "start_realtime_client_js" in source
        assert "realtime_fallback_to_local" in source
        assert "row-bot-realtime-event" in source
        assert "make_realtime_event_handler" in source

    events_src = (ROOT / "src" / "row_bot" / "ui" / "voice_realtime_events.py").read_text(encoding="utf-8")
    assert "VoiceAgentBridge" in events_src
    assert "function_call_ready" in events_src
    assert "consult_fallback_needed" in events_src
    assert "server_error" in events_src
    assert "fatal_error" in events_src
    assert "microphone_permission" in events_src
    assert "barge_in_cancelled" in events_src
    assert "send_realtime_function_output_js" in events_src
    assert "submit_voice_text(send_fn, text, surface=\"shared_composer\")" in components_src
    assert "send_realtime_function_output_js" in streaming_src
    assert "send_realtime_run_event_js" in streaming_src
    assert "run_realtime_client_js" in streaming_src
    assert 'state.voice_coordinator.transport == "realtime"' in streaming_src
    assert "VoiceOutputController.for_generation" in streaming_src
    assert "heard_cue()" in streaming_src
    assert "tool_start_cue" in streaming_src
    assert "approval_needed_cue()" in streaming_src
    assert "voice_output.speak_final" in streaming_src
    assert "voice.realtime.pipeline" in streaming_src
    assert "generation_done_no_speech" in streaming_src
    assert "final_speech_not_started" in streaming_src
    assert "waiting_for_approval" in streaming_src
    assert "diagnostic_snapshot" in events_src
    assert "Cancellation failed: no active response found" in events_src
    assert "server_error_suppressed" in events_src
    assert "Cancellation failed: no active response found" in events_src
    assert "server_error_suppressed" in events_src
    assert "_handle_realtime_fatal_error" in events_src
    assert "realtime_fallback_to_local" in events_src
    assert "Falling back to local Talk" in events_src
    assert "OpenAI Realtime cannot start because the API key has insufficient quota." in events_src
    assert "p.voice_switch.value = True" in events_src
    assert "p.voice_switch.set_value(True)" not in events_src


def test_realtime_fatal_error_message_mentions_quota():
    from row_bot.ui.voice_realtime_events import _friendly_realtime_error

    detail = '{"type":"insufficient_quota","message":"You exceeded your current quota"}'

    assert _friendly_realtime_error(detail) == "OpenAI Realtime cannot start because the API key has insufficient quota."


def test_thread_navigation_paths_stop_active_talk():
    for path in ("src/row_bot/ui/sidebar.py", "src/row_bot/ui/chat.py", "src/row_bot/ui/command_center.py", "src/row_bot/ui/home.py", "app.py"):
        assert "stop_voice_for_thread_change" in _source(path)


def test_voice_thread_change_helper_turns_voice_off():
    class FakeTTS:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    class FakeCoordinator:
        is_running = True
        transport = "local"

        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True
            self.is_running = False

    class FakeSwitch:
        def __init__(self) -> None:
            self.value = True
            self.updated = False

        def update(self) -> None:
            self.updated = True

    tts = FakeTTS()
    coordinator = FakeCoordinator()
    switch = FakeSwitch()
    state = SimpleNamespace(
        tts_service=tts,
        voice_coordinator=coordinator,
        voice_enabled=True,
        voice_input_mode="talk",
    )
    p = SimpleNamespace(voice_switch=switch)

    assert stop_voice_for_thread_change(state, p, reason="test") is True
    assert state.voice_enabled is False
    assert state.voice_input_mode == "talk"
    assert tts.stopped is True
    assert coordinator.stopped is True
    assert switch.value is False
    assert switch.updated is True


def _source(path: str) -> str:
    if path in {"app.py", "launcher.py"}:
        path = f"src/row_bot/{path}"
    return (ROOT / path).read_text(encoding="utf-8")


def test_chat_inputs_do_not_use_nicegui_exact_enter_modifier():
    """NiceGUI treats ``exact`` as a key in this app's version, not a modifier."""
    for path in ("src/row_bot/ui/chat.py", "src/row_bot/ui/chat_components.py"):
        src = _source(path)
        assert "keydown.enter.exact.prevent" not in src


def test_main_and_designer_chat_allow_modified_enter():
    for path in ("src/row_bot/ui/chat.py", "src/row_bot/ui/chat_components.py", "src/row_bot/designer/editor.py"):
        src = _source(path)
        assert "keydown.enter" in src
        assert "e.shiftKey || e.ctrlKey || e.metaKey || e.altKey" in src
        assert "e.preventDefault();" in src
        assert "emit();" in src


def test_shared_realtime_transcript_submit_preserves_voice_mode_when_supported():
    calls = []

    async def send_fn(text: str, *, voice_mode: bool = False) -> None:
        calls.append((text, voice_mode))

    asyncio.run(_submit_voice_transcript(send_fn, "hello"))

    assert calls == [("hello", True)]


def test_shared_realtime_transcript_submit_falls_back_for_plain_senders():
    calls = []

    async def send_fn(text: str) -> None:
        calls.append(text)

    asyncio.run(_submit_voice_transcript(send_fn, "hello"))

    assert calls == ["hello"]


def test_studio_voice_senders_accept_voice_mode_keyword():
    developer_src = _source("src/row_bot/developer/ui.py")
    designer_src = _source("src/row_bot/designer/editor.py")

    assert "async def _send(text: str, *, voice_mode: bool = False)" in developer_src
    assert "await send_message(text, voice_mode=voice_mode)" in developer_src
    assert "async def _send_with_references(text: str, *, voice_mode: bool = False)" in designer_src
    assert "await send_message(text, voice_mode=voice_mode)" in designer_src


def test_active_chat_input_uses_shared_voice_control_bridge():
    streaming_src = _source("src/row_bot/ui/streaming.py")
    components_src = _source("src/row_bot/ui/chat_components.py")
    app_src = _source("src/row_bot/app.py")
    lifecycle_src = _source("src/row_bot/ui/voice_lifecycle.py")

    assert "bridge.control_active_run(text)" in streaming_src
    assert "queued_voice_controls" in streaming_src
    assert "queued_voice_controls_dispatch" in streaming_src
    assert "voice_control_queue" in _source("src/row_bot/ui/state.py")
    assert "ActiveVoiceSurfaceBinding" in components_src
    assert "active_voice_surface_bound" in components_src
    assert "binding.append_dictation(text)" in app_src
    assert "await binding.send_talk(text)" in app_src
    assert "p.active_voice_binding = None" in lifecycle_src


def test_normal_chat_registers_active_voice_surface_binding():
    chat_src = _source("src/row_bot/ui/chat.py")

    assert "def _register_active_voice_binding" in chat_src
    assert "ActiveVoiceSurfaceBinding" in chat_src
    assert 'surface="normal_chat"' in chat_src
    assert "send_talk_text=send_message" in chat_src
    assert "active_voice_surface_bound" in chat_src
    assert "Smart Skills draft suggestions were not synced for voice text" in chat_src
    assert re.search(
        r"def _start_local_talk\(\) -> None:.*?if _browser_voice_required\(\):.*?"
        r"_start_browser_voice\(\"talk\"\).*?return.*?_register_active_voice_binding\(\).*?"
        r"state\.voice_input_mode = \"talk\"",
        chat_src,
        re.S,
    )
    assert re.search(
        r"def _start_realtime_talk\(\) -> None:.*?state\.voice_input_mode = \"talk\".*?"
        r"_register_active_voice_binding\(\).*?start_realtime_talk",
        chat_src,
        re.S,
    )
    assert re.search(
        r"def _stop_talk\(\) -> None:.*?binding = getattr\(p, \"active_voice_binding\", None\).*?"
        r"binding\.clear\(\).*?p\.active_voice_binding = None",
        chat_src,
        re.S,
    )
    assert re.search(
        r"def _toggle_dictate\(\):.*?binding = getattr\(p, \"active_voice_binding\", None\).*?"
        r"binding\.clear\(\).*?p\.active_voice_binding = None.*?"
        r"state\.voice_input_mode = \"dictate\".*?_register_active_voice_binding\(\).*?"
        r"state\.voice_coordinator\.start_dictation\(\)",
        chat_src,
        re.S,
    )


def test_normal_chat_voice_bridge_falls_back_when_binding_is_missing():
    app_src = _source("src/row_bot/app.py")

    assert "surface = _active_voice_surface()" in app_src
    assert 'if surface == "normal_chat":' in app_src
    assert '"stage": "active_voice_surface_fallback"' in app_src
    assert "return await _send_message(text, voice_mode=True)" in app_src
    assert '"stage": "active_voice_surface_missing"' in app_src


def test_realtime_voice_uses_guarded_progress_cues_during_row_bot_stream():
    streaming_src = _source("src/row_bot/ui/streaming.py")
    presenter_src = _source("src/row_bot/voice/realtime_presenter.py")
    client_src = _source("src/row_bot/voice/realtime_client.py")

    assert "realtime_silence_watchdog_tick" in streaming_src
    assert "realtime_cue_spoken" in streaming_src
    assert "realtime_cue_suppressed" in streaming_src
    assert "tool_progress_cue" in streaming_src
    assert "results_found_cue" in streaming_src
    assert 'mark_realtime_latency("first_token")' in streaming_src
    assert 'mark_realtime_latency("row_bot_tool_started")' in streaming_src
    assert 'mark_realtime_latency("row_bot_tool_done")' in streaming_src
    assert "RealtimeSpeechQueue" in streaming_src
    assert "realtime_stream_chunk_queued" in streaming_src
    assert "realtime_stream_chunk_flushed" in streaming_src
    assert "realtime_final_speech_decision" in streaming_src
    assert "realtime_function_output_silent_after_stream" in streaming_src
    assert 'allow_long=gen.tts_allow_long or state.voice_coordinator.transport == "realtime"' in streaming_src
    assert "spoken_stream_chars" in presenter_src
    assert "Speak this Row-Bot response naturally and faithfully" in client_src
    assert "Speak exactly this brief Row-Bot status" in client_src


def test_realtime_browser_event_logging_is_not_duplicated_for_ordinary_events():
    events_src = _source("src/row_bot/ui/voice_realtime_events.py")

    assert "_LOG_RECEIVED_EVENTS" in events_src
    assert "_QUIET_OBSERVED_EVENTS" in events_src
    assert "if event_type in _LOG_RECEIVED_EVENTS:" in events_src
    assert "if event_type not in _QUIET_OBSERVED_EVENTS:" in events_src
    assert "logger.debug(\"voice.realtime.browser_event" in events_src
    assert '"server_error"' in events_src
    assert "logger.warning(\"OpenAI Realtime server event error" in events_src
    assert "response_arbiter_state" in events_src
