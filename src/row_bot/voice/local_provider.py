from __future__ import annotations

import importlib.util
import platform
import tempfile
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from row_bot.data_paths import get_row_bot_data_dir
from row_bot.tts import TTSService
from row_bot.voice import VoiceService
from row_bot.voice.provider_base import VoiceProviderStatus


DEFAULT_FUNASR_MODEL = "iic/SenseVoiceSmall"
FUNASR_MODEL_LABEL = "SenseVoice Small"
FUNASR_MODEL_URL = "https://modelscope.cn/models/iic/SenseVoiceSmall"
FUNASR_MODEL_APPROX_SIZE = "~940 MB"
FUNASR_REQUIRED_MODULES = ("funasr", "modelscope", "torch", "torchaudio")
_SENSEVOICE_REQUIRED_FILES = ("config.yaml", "model.pt")


class SenseVoiceUnavailableError(RuntimeError):
    """Raised when the optional local SenseVoice runtime is not ready."""


@dataclass(frozen=True)
class SenseVoiceRuntimeStatus:
    state: Literal[
        "unsupported",
        "packages_missing",
        "model_missing",
        "model_invalid",
        "ready",
    ]
    ready: bool
    reason: str
    model_path: Path | None = None
    missing_packages: tuple[str, ...] = ()


def sensevoice_cache_dir() -> Path:
    return get_row_bot_data_dir() / "cache" / "sensevoice"


def sensevoice_platform_support() -> tuple[bool, str]:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin" and machine in {"amd64", "x86_64"}:
        return (
            False,
            "SenseVoice is unavailable on Intel macOS because matching CPU-only "
            "PyTorch and Torchaudio wheels are not published for this runtime.",
        )
    return True, ""


def missing_funasr_packages() -> tuple[str, ...]:
    missing = []
    for module in FUNASR_REQUIRED_MODULES:
        try:
            available = importlib.util.find_spec(module) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing.append(module)
    return tuple(missing)


def is_funasr_available() -> bool:
    supported, _reason = sensevoice_platform_support()
    return supported and not missing_funasr_packages()


def verified_sensevoice_snapshot(
    model_path: str | Path | None,
    *,
    cache_dir: str | Path | None = None,
) -> Path | None:
    if not model_path:
        return None
    try:
        root = Path(cache_dir or sensevoice_cache_dir()).resolve()
        candidate = Path(model_path).expanduser().resolve(strict=True)
        if not candidate.is_dir() or not candidate.is_relative_to(root):
            return None
        for filename in _SENSEVOICE_REQUIRED_FILES:
            required = candidate / filename
            if not required.is_file() or required.stat().st_size <= 0:
                return None
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def sensevoice_runtime_status(
    model_path: str | Path | None,
    *,
    cache_dir: str | Path | None = None,
) -> SenseVoiceRuntimeStatus:
    supported, reason = sensevoice_platform_support()
    if not supported:
        return SenseVoiceRuntimeStatus("unsupported", False, reason)

    missing = missing_funasr_packages()
    if missing:
        return SenseVoiceRuntimeStatus(
            "packages_missing",
            False,
            "Install the voice extra to use SenseVoice "
            f"(missing: {', '.join(missing)}).",
            missing_packages=missing,
        )
    if not model_path:
        return SenseVoiceRuntimeStatus(
            "model_missing",
            False,
            "SenseVoice model data is not installed. Install it explicitly in Voice settings.",
        )

    verified_path = verified_sensevoice_snapshot(model_path, cache_dir=cache_dir)
    if verified_path is None:
        return SenseVoiceRuntimeStatus(
            "model_invalid",
            False,
            "SenseVoice local model data is missing or incomplete. Reinstall it in Voice settings.",
        )
    return SenseVoiceRuntimeStatus(
        "ready",
        True,
        "SenseVoice is installed locally and normal transcription stays offline.",
        model_path=verified_path,
    )


def install_sensevoice_model(
    *,
    cache_dir: str | Path | None = None,
    snapshot_downloader: Callable[..., str] | None = None,
) -> Path:
    """Download and verify SenseVoice after an explicit user action."""
    supported, reason = sensevoice_platform_support()
    if not supported:
        raise SenseVoiceUnavailableError(reason)

    missing = missing_funasr_packages()
    if missing:
        raise SenseVoiceUnavailableError(
            "Install the voice extra before downloading SenseVoice "
            f"(missing: {', '.join(missing)})."
        )

    if snapshot_downloader is None:
        from modelscope import snapshot_download

        snapshot_downloader = snapshot_download

    destination = Path(cache_dir or sensevoice_cache_dir())
    destination.mkdir(parents=True, exist_ok=True)
    downloaded_path = snapshot_downloader(
        DEFAULT_FUNASR_MODEL,
        cache_dir=str(destination),
    )
    verified_path = verified_sensevoice_snapshot(
        downloaded_path,
        cache_dir=destination,
    )
    if verified_path is None:
        raise SenseVoiceUnavailableError(
            "The SenseVoice download completed without a valid local snapshot. "
            "Retry the installation from Voice settings."
        )
    return verified_path


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
        model_path: str | Path | None = None,
        cache_dir: str | Path | None = None,
        sample_rate: int = 16_000,
        model_factory: Callable[..., Any] | None = None,
        postprocessor: Callable[[str], str] | None = None,
    ) -> None:
        self.model_id = model_id
        self.model_path = model_path
        self.cache_dir = cache_dir
        self.sample_rate = sample_rate
        self._model_factory = model_factory
        self._postprocessor = postprocessor
        self._model = None

    def runtime_status(self) -> SenseVoiceRuntimeStatus:
        return sensevoice_runtime_status(self.model_path, cache_dir=self.cache_dir)

    def status(self) -> VoiceProviderStatus:
        status = self.runtime_status()
        return VoiceProviderStatus(
            provider_id=self.provider_id,
            display_name=self.display_name,
            ready=status.ready,
            reason=status.reason,
            local=True,
        )

    def ensure_model(self):
        if self._model is not None:
            return self._model
        status = self.runtime_status()
        if not status.ready or status.model_path is None:
            raise SenseVoiceUnavailableError(status.reason)
        if self._model_factory is None:
            from funasr import AutoModel

            self._model_factory = AutoModel
        self._model = self._model_factory(
            model=str(status.model_path),
            device="cpu",
            disable_update=True,
        )
        return self._model

    def transcribe_bytes(self, audio_bytes: bytes) -> str:
        if not audio_bytes:
            return ""
        model = self.ensure_model()
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
            if self._postprocessor is None:
                from funasr.utils.postprocess_utils import rich_transcription_postprocess

                self._postprocessor = rich_transcription_postprocess
            return self._postprocessor(_extract_funasr_text(result))
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
        LocalFunASRProvider(
            model_path=getattr(voice_service, "sensevoice_model_path", ""),
        ).status(),
        LocalKokoroProvider(tts_service).status(),
    ]
