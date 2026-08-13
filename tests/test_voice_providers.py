from __future__ import annotations

from row_bot.voice.local_provider import (
    LocalFunASRProvider,
    LocalKokoroProvider,
    LocalWhisperProvider,
    local_voice_provider_statuses,
)


class FakeVoiceService:
    whisper_size = "base"

    def __init__(self) -> None:
        self.audio = b""

    def transcribe_bytes(self, audio_bytes: bytes) -> str:
        self.audio = audio_bytes
        return "transcribed"


class FakeTTSService:
    def __init__(self, installed: bool) -> None:
        self.installed = installed
        self.spoken: list[str] = []

    def is_installed(self) -> bool:
        return self.installed

    def speak_now(self, text: str) -> None:
        self.spoken.append(text)


class FakeFunASRModel:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def generate(self, **kwargs):
        self.inputs.append(kwargs["input"])
        return [{"text": ("<|en|><|NEUTRAL|><|Speech|><|woitn|>sensevoice text")}]


def test_local_whisper_provider_wraps_existing_voice_service():
    service = FakeVoiceService()
    provider = LocalWhisperProvider(service)  # type: ignore[arg-type]

    status = provider.status()

    assert status.ready is True
    assert status.local is True
    assert "base" in status.reason
    assert provider.transcribe_bytes(b"audio") == "transcribed"
    assert service.audio == b"audio"


def test_local_funasr_provider_transcribes_raw_pcm_bytes():
    model = FakeFunASRModel()
    provider = LocalFunASRProvider(model_factory=lambda **kwargs: model)

    text = provider.transcribe_bytes(b"\x00\x00\x01\x00")

    assert text == "sensevoice text"
    assert model.inputs


def test_local_kokoro_provider_wraps_existing_tts_service():
    tts = FakeTTSService(installed=True)
    provider = LocalKokoroProvider(tts)  # type: ignore[arg-type]

    status = provider.status()
    provider.speak_now("hello")

    assert status.ready is True
    assert status.local is True
    assert tts.spoken == ["hello"]


def test_local_voice_provider_statuses_report_missing_kokoro():
    statuses = local_voice_provider_statuses(
        FakeVoiceService(),  # type: ignore[arg-type]
        FakeTTSService(installed=False),  # type: ignore[arg-type]
    )

    assert [status.provider_id for status in statuses] == [
        "local_whisper",
        "local_funasr",
        "local_kokoro",
    ]
    assert statuses[0].ready is True
    assert statuses[2].ready is False
    assert "not installed" in statuses[2].reason
