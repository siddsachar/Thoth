from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from row_bot.voice.browser_client import (
    speak_browser_voice_js,
    start_browser_voice_capture_js,
    stop_browser_voice_js,
)
from row_bot.voice.browser_local import (
    BrowserLocalVoiceService,
    BrowserVoiceError,
    MAX_INPUT_BYTES,
)
from row_bot.voice.coordinator import VoiceSessionCoordinator


class _Voice:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.calls: list[tuple[bytes, bool]] = []
        self.is_running = False
        self.state = "stopped"

    def whisper_model_available(self) -> bool:
        return self.ready

    def transcribe_pcm16(self, pcm: bytes, *, allow_download: bool) -> str:
        self.calls.append((pcm, allow_download))
        return "hello browser"

    def install_whisper_model(self) -> None:
        self.ready = True

    def start(self) -> None:
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False

    def get_status(self):
        return None

    def get_transcription(self):
        return None


class _TTS:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.texts: list[str] = []

    def is_installed(self) -> bool:
        return self.ready

    def synthesize_wav_bytes(self, text: str) -> bytes:
        self.texts.append(text)
        return b"RIFF" + b"\0" * 64

    def download_model(self, _progress=None) -> None:
        self.ready = True


def _runner(_command, **_kwargs):
    return SimpleNamespace(returncode=0, stdout=b"\0" * 3_200, stderr=b"")


def test_browser_audio_is_decoded_and_transcribed_without_download() -> None:
    voice = _Voice()
    service = BrowserLocalVoiceService(
        voice_service=voice,
        tts_service=_TTS(),
        runner=_runner,
        ffmpeg_path="ffmpeg",
    )

    assert service.transcribe("session-a", b"encoded", "audio/webm;codecs=opus") == (
        "hello browser"
    )
    assert voice.calls == [(b"\0" * 3_200, False)]


@pytest.mark.parametrize(
    ("payload", "content_type", "code"),
    [
        (b"x", "application/octet-stream", "unsupported_audio_type"),
        (b"", "audio/webm", "empty_audio"),
        (b"x" * (MAX_INPUT_BYTES + 1), "audio/webm", "audio_too_large"),
    ],
    ids=("unsupported-type", "empty", "too-large"),
)
def test_browser_audio_rejects_invalid_inputs(
    payload: bytes,
    content_type: str,
    code: str,
) -> None:
    service = BrowserLocalVoiceService(
        voice_service=_Voice(),
        tts_service=_TTS(),
        runner=_runner,
        ffmpeg_path="ffmpeg",
    )
    with pytest.raises(BrowserVoiceError, match=code):
        service.transcribe("session-a", payload, content_type)


def test_decode_timeout_is_privacy_safe() -> None:
    def timeout_runner(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["ffmpeg"], 15, output=b"private-audio")

    service = BrowserLocalVoiceService(
        voice_service=_Voice(),
        tts_service=_TTS(),
        runner=timeout_runner,
        ffmpeg_path="ffmpeg",
    )
    with pytest.raises(BrowserVoiceError, match="audio_decode_timeout") as raised:
        service.transcribe("session-a", b"encoded", "audio/webm")
    assert "private-audio" not in str(raised.value)


def test_missing_models_are_explicit_and_synthesis_is_session_scoped() -> None:
    service = BrowserLocalVoiceService(
        voice_service=_Voice(ready=False),
        tts_service=_TTS(ready=False),
        runner=_runner,
        ffmpeg_path="ffmpeg",
    )
    with pytest.raises(BrowserVoiceError, match="whisper_model_missing"):
        service.transcribe("session-a", b"encoded", "audio/webm")
    with pytest.raises(BrowserVoiceError, match="kokoro_model_missing"):
        service.synthesize("session-b", "Hello")

    service.install_whisper("session-a")
    service.install_kokoro("session-b")
    assert service.status()["whisper_ready"] is True
    assert service.status()["kokoro_ready"] is True
    assert service.synthesize("session-b", "Hello").startswith(b"RIFF")


def test_browser_transport_never_starts_the_device_voice_service() -> None:
    voice = _Voice()
    coordinator = VoiceSessionCoordinator(voice)  # type: ignore[arg-type]

    coordinator.start_browser("talk")

    assert coordinator.transport == "browser"
    assert coordinator.is_running
    assert voice.is_running is False


def test_browser_scripts_check_secure_context_and_clean_up_private_media() -> None:
    capture = start_browser_voice_capture_js(sink_id=42, mode="talk")
    cleanup = stop_browser_voice_js(cancel=True)
    playback = speak_browser_voice_js("hello")

    assert capture.index("isSecureContext") < capture.index("getUserMedia")
    assert "credentials: 'same-origin'" in capture
    assert "URL.revokeObjectURL" in cleanup
    assert "/api/voice/local/synthesize" in playback
    assert "URL.revokeObjectURL" in playback


def test_kokoro_runtime_temp_is_created_only_under_app_data(
    tmp_path,
    monkeypatch,
) -> None:
    import row_bot.tts as tts

    runtime_tmp = tmp_path / "runtime-tmp"
    monkeypatch.setattr(tts, "_ROW_BOT_DIR", tmp_path)
    monkeypatch.setenv("TMPDIR", str(runtime_tmp))
    monkeypatch.setattr(tts.tempfile, "tempdir", None)

    tts._prepare_configured_runtime_tmp()

    assert runtime_tmp.is_dir()
    assert tts.tempfile.tempdir == str(runtime_tmp.resolve())
