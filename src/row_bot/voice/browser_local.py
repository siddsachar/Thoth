"""Bounded server-side operations for browser-mediated local voice."""

from __future__ import annotations

from collections import defaultdict, deque
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from row_bot.tts import TTSService
from row_bot.voice import SAMPLE_RATE, VoiceService, get_voice_service

MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_UTTERANCE_SECONDS = 30
MAX_TEXT_CHARS = 4_000
DECODE_TIMEOUT_SECONDS = 15
REQUESTS_PER_MINUTE = 12
GLOBAL_CONCURRENCY = 2

ALLOWED_AUDIO_TYPES = frozenset(
    {
        "audio/webm",
        "audio/ogg",
        "audio/wav",
        "audio/x-wav",
        "audio/mp4",
        "audio/mpeg",
    }
)


class BrowserVoiceError(RuntimeError):
    """Privacy-safe browser voice failure with an HTTP-friendly code."""

    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class BrowserLocalVoiceService:
    """Decode, transcribe, and synthesize without using host audio devices."""

    def __init__(
        self,
        *,
        voice_service: VoiceService | None = None,
        tts_service: TTSService | None = None,
        runner: Callable[..., Any] = subprocess.run,
        ffmpeg_path: str | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.voice_service = voice_service or get_voice_service()
        self.tts_service = tts_service or TTSService()
        self._runner = runner
        self._ffmpeg_path = ffmpeg_path
        self._now = now
        self._guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._request_times: dict[str, deque[float]] = defaultdict(deque)
        self._global_slots = threading.BoundedSemaphore(GLOBAL_CONCURRENCY)

    def status(self) -> dict[str, object]:
        return {
            "transport": "browser_local",
            "whisper_ready": self.voice_service.whisper_model_available(),
            "kokoro_ready": self.tts_service.is_installed(),
            "secure_context_required": True,
            "max_input_bytes": MAX_INPUT_BYTES,
            "max_utterance_seconds": MAX_UTTERANCE_SECONDS,
        }

    def transcribe(
        self,
        session_key: str,
        audio_bytes: bytes,
        content_type: str,
    ) -> str:
        mime = str(content_type or "").partition(";")[0].strip().lower()
        if mime not in ALLOWED_AUDIO_TYPES:
            raise BrowserVoiceError("unsupported_audio_type", status_code=415)
        if not audio_bytes:
            raise BrowserVoiceError("empty_audio")
        if len(audio_bytes) > MAX_INPUT_BYTES:
            raise BrowserVoiceError("audio_too_large", status_code=413)
        with self._job(session_key):
            if not self.voice_service.whisper_model_available():
                raise BrowserVoiceError("whisper_model_missing", status_code=409)
            pcm = self._decode(audio_bytes)
            return self.voice_service.transcribe_pcm16(
                pcm,
                allow_download=False,
            )

    def synthesize(self, session_key: str, text: str) -> bytes:
        clean = str(text or "").strip()
        if not clean:
            raise BrowserVoiceError("text_required")
        if len(clean) > MAX_TEXT_CHARS:
            raise BrowserVoiceError("text_too_large", status_code=413)
        with self._job(session_key):
            if not self.tts_service.is_installed():
                raise BrowserVoiceError("kokoro_model_missing", status_code=409)
            payload = self.tts_service.synthesize_wav_bytes(clean)
            if not payload or len(payload) > MAX_OUTPUT_BYTES:
                raise BrowserVoiceError("audio_output_too_large", status_code=413)
            return payload

    def install_whisper(self, session_key: str) -> None:
        with self._job(session_key):
            self.voice_service.install_whisper_model()

    def install_kokoro(
        self,
        session_key: str,
        progress: Callable[[float], None] | None = None,
    ) -> None:
        with self._job(session_key):
            self.tts_service.download_model(progress)

    def _decode(self, audio_bytes: bytes) -> bytes:
        executable = self._ffmpeg_path or shutil.which("ffmpeg")
        if not executable:
            raise BrowserVoiceError("ffmpeg_unavailable", status_code=503)
        command = [
            executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-t",
            str(MAX_UTTERANCE_SECONDS),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ]
        try:
            completed = self._runner(
                command,
                input=audio_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=DECODE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrowserVoiceError("audio_decode_timeout", status_code=408) from exc
        except OSError as exc:
            raise BrowserVoiceError("audio_decode_unavailable", status_code=503) from exc
        if int(getattr(completed, "returncode", 1)) != 0:
            raise BrowserVoiceError("malformed_audio")
        pcm = bytes(getattr(completed, "stdout", b"") or b"")
        max_decoded = SAMPLE_RATE * 2 * MAX_UTTERANCE_SECONDS
        if len(pcm) < SAMPLE_RATE // 10 or len(pcm) > max_decoded:
            raise BrowserVoiceError("audio_duration_invalid")
        return pcm

    def _check_rate(self, session_key: str) -> None:
        now = self._now()
        with self._guard:
            recent = self._request_times[session_key]
            while recent and now - recent[0] >= 60:
                recent.popleft()
            if len(recent) >= REQUESTS_PER_MINUTE:
                raise BrowserVoiceError("voice_rate_limited", status_code=429)
            recent.append(now)

    def _lock_for(self, session_key: str) -> threading.Lock:
        with self._guard:
            return self._session_locks.setdefault(session_key, threading.Lock())

    class _Job:
        def __init__(
            self,
            owner: "BrowserLocalVoiceService",
            session_key: str,
        ) -> None:
            self.owner = owner
            self.session_key = session_key
            self.session_lock = owner._lock_for(session_key)

        def __enter__(self) -> None:
            self.owner._check_rate(self.session_key)
            if not self.session_lock.acquire(blocking=False):
                raise BrowserVoiceError("voice_session_busy", status_code=429)
            if not self.owner._global_slots.acquire(blocking=False):
                self.session_lock.release()
                raise BrowserVoiceError("voice_service_busy", status_code=503)

        def __exit__(self, *_args: object) -> None:
            self.owner._global_slots.release()
            self.session_lock.release()

    def _job(self, session_key: str) -> "_Job":
        key = str(session_key or "").strip()
        if not key:
            raise BrowserVoiceError("voice_session_required", status_code=401)
        return self._Job(self, key)


_service: BrowserLocalVoiceService | None = None
_service_lock = threading.Lock()


def get_browser_local_voice_service() -> BrowserLocalVoiceService:
    global _service
    with _service_lock:
        if _service is None:
            _service = BrowserLocalVoiceService()
        return _service


def _set_browser_local_voice_service_for_tests(
    service: BrowserLocalVoiceService | None,
) -> None:
    global _service
    with _service_lock:
        _service = service
