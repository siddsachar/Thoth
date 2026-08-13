from __future__ import annotations

import importlib.util
import tempfile
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from row_bot.tts import TTSService
from row_bot.voice import VoiceService
from row_bot.voice.provider_base import VoiceProviderStatus


DEFAULT_FUNASR_MODEL = "iic/SenseVoiceSmall"
FUNASR_MODEL_LABEL = "SenseVoice Small"


def is_funasr_available() -> bool:
    return importlib.util.find_spec("funasr") is not None


class LocalWhisperProvider:
    provider_id = "local_whisper"
    display_name = "Local Whisper"

    def __init__(self, voice_service: VoiceService) -> None:
        self.voice_service = voice_service

    def status(self) -> VoiceProviderStatus:
        return VoiceProviderStatus(
            provider_id=self.provider_id,
            display_name=self.display_name,
            ready=True,
            reason=f"Configured model size: {self.voice_service.whisper_size}",
            local=True,
        )

    def transcribe_bytes(self, audio_bytes: bytes) -> str:
        return self.voice_service.transcribe_bytes(audio_bytes)


class LocalFunASRProvider:
    provider_id = "local_funasr"
    display_name = "Local FunASR / SenseVoice"

    def __init__(
        self,
        model_id: str = DEFAULT_FUNASR_MODEL,
        *,
        sample_rate: int = 16_000,
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_id = model_id
        self.sample_rate = sample_rate
        self._model_factory = model_factory
        self._model = None

    def status(self) -> VoiceProviderStatus:
        ready = is_funasr_available()
        return VoiceProviderStatus(
            provider_id=self.provider_id,
            display_name=self.display_name,
            ready=ready,
            reason=(
                f"Installed locally. Default model: {self.model_id}."
                if ready
                else "Install the voice extra with FunASR to use local SenseVoice transcription."
            ),
            local=True,
        )

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        if self._model_factory is None:
            from funasr import AutoModel

            self._model_factory = AutoModel
        self._model = self._model_factory(model=self.model_id, disable_update=True)
        return self._model

    def transcribe_bytes(self, audio_bytes: bytes) -> str:
        if not audio_bytes:
            return ""
        model = self._ensure_model()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            audio_path = Path(handle.name)
        try:
            with wave.open(str(audio_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(self.sample_rate)
                wav.writeframes(audio_bytes)
            result = model.generate(
                input=str(audio_path), language="auto", use_itn=True
            )
            from funasr.utils.postprocess_utils import rich_transcription_postprocess

            return rich_transcription_postprocess(_extract_funasr_text(result))
        finally:
            try:
                audio_path.unlink()
            except FileNotFoundError:
                pass


def _extract_funasr_text(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if isinstance(result, dict):
        return str(result.get("text") or "").strip()
    if isinstance(result, list):
        parts = [_extract_funasr_text(item) for item in result]
        return " ".join(part for part in parts if part).strip()
    return ""


class LocalKokoroProvider:
    provider_id = "local_kokoro"
    display_name = "Local Kokoro"

    def __init__(self, tts_service: TTSService) -> None:
        self.tts_service = tts_service

    def status(self) -> VoiceProviderStatus:
        installed = self.tts_service.is_installed()
        return VoiceProviderStatus(
            provider_id=self.provider_id,
            display_name=self.display_name,
            ready=installed,
            reason="Installed locally."
            if installed
            else "Kokoro model files are not installed.",
            local=True,
        )

    def speak_now(self, text: str) -> None:
        self.tts_service.speak_now(text)


def local_voice_provider_statuses(
    voice_service: VoiceService,
    tts_service: TTSService,
) -> list[VoiceProviderStatus]:
    return [
        LocalWhisperProvider(voice_service).status(),
        LocalFunASRProvider().status(),
        LocalKokoroProvider(tts_service).status(),
    ]
