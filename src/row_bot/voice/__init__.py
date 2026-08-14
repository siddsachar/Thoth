"""Voice service — local STT with toggle-based listening.

Pipeline:
    sounddevice  (background thread, always capturing 16 kHz mono)
    → energy VAD  (collects speech until silence)
    → faster-whisper  (transcribes the utterance)
    → result placed on a thread-safe queue for Streamlit to consume

State machine::

    stopped ──► listening   (user toggles voice on)
                    │ speech + silence
                    ▼
               transcribing  (Whisper runs)
                    │
                    ▼
               listening     (loop back)
                    │ TTS starts speaking
                    ▼
               muted         (mic paused while TTS plays)
                    │ TTS finishes → grace period
                    ▼
               listening     (continue conversation)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Literal

import numpy as np

from row_bot.data_paths import get_row_bot_data_dir

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16_000        # 16 kHz — required by Whisper
CHUNK_SAMPLES = 1280        # 80 ms at 16 kHz
CHANNELS = 1

# VAD / speech collection
_PRE_SPEECH_CHUNKS = 8      # ~640 ms of audio kept before speech trigger
_SILENCE_TIMEOUT_S = 1.5    # seconds of silence after last speech to stop recording
_MAX_RECORDING_S = 30       # hard cap on a single utterance
_MIN_SPEECH_CHUNKS = 5      # minimum chunks to count as real speech (~400 ms)

# Whisper
_DEFAULT_WHISPER_SIZE = "small"

# Grace period after TTS unmutes — discard audio to let speaker buffer drain
_UNMUTE_GRACE_S = 0.6

# Data directory
_DATA_DIR = get_row_bot_data_dir()
_VOICE_SETTINGS_FILE = _DATA_DIR / "voice_settings.json"
_WHISPER_CACHE_DIR = Path(
    os.environ.get("ROW_BOT_WHISPER_CACHE_DIR")
    or (_DATA_DIR / "cache" / "whisper")
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_voice_settings() -> dict:
    import json
    if _VOICE_SETTINGS_FILE.exists():
        try:
            return json.loads(_VOICE_SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_voice_settings(settings: dict) -> None:
    import json
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _VOICE_SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


# ── Voice Service ────────────────────────────────────────────────────────────

class VoiceService:
    """Toggle-based voice input.

    ``start()`` opens the mic and begins listening immediately.
    ``stop()``  closes the mic and kills the background thread.
    TTS calls ``mute()`` / ``unmute()`` to pause/resume the mic.
    """

    State = Literal["stopped", "listening", "transcribing", "muted"]

    def __init__(self) -> None:
        self._state: VoiceService.State = "stopped"
        self._lock = threading.Lock()

        # Queues
        self.results: SimpleQueue[str] = SimpleQueue()
        self.status_queue: SimpleQueue[str] = SimpleQueue()

        # Thread control
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Whisper (lazy loaded)
        self._whisper_model = None
        self._funasr_provider = None

        # Mic gating
        self._mute_event = threading.Event()
        self._unmute_event = threading.Event()

        # Settings
        settings = _load_voice_settings()
        self._whisper_size: str = settings.get("whisper_model", _DEFAULT_WHISPER_SIZE)
        self._sensevoice_model_path: str = str(
            settings.get("sensevoice_model_path") or ""
        )
        self._stt_model: str = f"local-whisper-{self._whisper_size}"

    # ── Properties ───────────────────────────────────────────────────────

    def _set_state(self, new: "VoiceService.State") -> None:
        self._state = new

    @property
    def state(self) -> State:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state != "stopped"

    # ── Mic gating (called by TTS) ───────────────────────────────────────

    def mute(self) -> None:
        self._mute_event.set()
        self._unmute_event.clear()

    def unmute(self) -> None:
        self._unmute_event.set()

    # ── Settings ─────────────────────────────────────────────────────────

    @property
    def whisper_size(self) -> str:
        return self._whisper_size

    @whisper_size.setter
    def whisper_size(self, value: str) -> None:
        self._whisper_size = value
        self._whisper_model = None
        s = _load_voice_settings(); s["whisper_model"] = value; _save_voice_settings(s)

    @property
    def stt_model(self) -> str:
        return self._stt_model

    @stt_model.setter
    def stt_model(self, value: str) -> None:
        self._stt_model = str(value or f"local-whisper-{self._whisper_size}")

    @property
    def sensevoice_model_path(self) -> str:
        return self._sensevoice_model_path

    # ── Model loading ────────────────────────────────────────────────────

    def _ensure_whisper(self, *, allow_download: bool = True):
        if self._whisper_model is not None:
            return
        from faster_whisper import WhisperModel

        _WHISPER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.status_queue.put(f"Loading Whisper ({self._whisper_size})…")
        self._whisper_model = WhisperModel(
            self._whisper_size,
            device="cpu",
            compute_type="int8",
            download_root=str(_WHISPER_CACHE_DIR),
            local_files_only=not allow_download,
        )
        logger.info("Loaded Whisper model: %s (cpu/int8)", self._whisper_size)

    def whisper_model_available(self) -> bool:
        """Return whether the configured model is already cached locally."""
        if self._whisper_model is not None:
            return True
        model_name = f"models--Systran--faster-whisper-{self._whisper_size}"
        model_dir = _WHISPER_CACHE_DIR / model_name
        return model_dir.is_dir() and any(model_dir.iterdir())

    def install_whisper_model(self) -> None:
        """Explicitly download and load the selected Whisper model."""
        self._ensure_whisper(allow_download=True)

    def sensevoice_status(self):
        from row_bot.voice.local_provider import LocalFunASRProvider

        return LocalFunASRProvider(
            model_path=self._sensevoice_model_path,
        ).runtime_status()

    def install_sensevoice_model(self) -> Path:
        """Explicitly download SenseVoice and persist its verified local path."""
        from row_bot.voice.local_provider import install_sensevoice_model

        model_path = install_sensevoice_model()
        settings = _load_voice_settings()
        settings["sensevoice_model_path"] = str(model_path)
        _save_voice_settings(settings)
        self._sensevoice_model_path = str(model_path)
        self._funasr_provider = None
        return model_path

    def _get_funasr_provider(self):
        if self._funasr_provider is None:
            from row_bot.voice.local_provider import LocalFunASRProvider

            self._funasr_provider = LocalFunASRProvider(
                model_path=self._sensevoice_model_path,
            )
        return self._funasr_provider

    def _ensure_funasr(self):
        self._get_funasr_provider().ensure_model()

    def _ensure_selected_stt_model(self) -> None:
        if self._stt_model == "local-funasr-sensevoice":
            self._ensure_funasr()
            return
        self._ensure_whisper()

    def _transcribe_pcm_bytes(self, audio_bytes: bytes) -> str:
        if self._stt_model == "local-funasr-sensevoice":
            return self._get_funasr_provider().transcribe_bytes(audio_bytes)
        return self.transcribe_pcm16(audio_bytes, allow_download=True)

    # ── Pipeline ─────────────────────────────────────────────────────────

    def _run(self) -> None:
        import sounddevice as sd

        try:
            self._ensure_selected_stt_model()
        except Exception as exc:
            self.status_queue.put(f"Speech-to-text init failed: {exc}")
            logger.error("Speech-to-text init failed: %s", exc)
            self._set_state("stopped")
            return

        # Pre-check: is there any input device at all?
        try:
            sd.query_devices(kind="input")
        except sd.PortAudioError:
            msg = "No microphone detected on this system"
            self.status_queue.put(msg)
            logger.warning(msg)
            self._set_state("stopped")
            return

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS,
                dtype="int16", blocksize=CHUNK_SAMPLES,
            )
            stream.start()
        except Exception as exc:
            self.status_queue.put(f"Mic error: {exc}")
            logger.error("Could not open mic: %s", exc)
            self._set_state("stopped")
            return

        self._set_state("listening")
        self.status_queue.put("🔴 Listening…")

        pre_buffer: deque[np.ndarray] = deque(maxlen=_PRE_SPEECH_CHUNKS)
        speech_chunks: list[np.ndarray] = []
        silence_counter = 0
        speech_start = 0.0
        unmute_grace_start = 0.0

        try:
            while not self._stop_event.is_set():

                # ── Muted (TTS speaking) ─────────────────────────────
                if self._mute_event.is_set():
                    if self._state != "muted":
                        self._set_state("muted")
                        self.status_queue.put("🔇 Speaking…")
                    if self._unmute_event.is_set():
                        self._unmute_event.clear()
                        self._mute_event.clear()
                        self._set_state("listening")
                        unmute_grace_start = time.monotonic()
                        pre_buffer.clear()
                        self.status_queue.put("🔴 Listening…")
                    else:
                        time.sleep(0.05)
                    continue

                # ── Read mic ─────────────────────────────────────────
                data, overflowed = stream.read(CHUNK_SAMPLES)
                if overflowed:
                    logger.debug("Audio buffer overflow")
                chunk = data[:, 0].copy()

                # ── Grace period after unmute ────────────────────────
                if unmute_grace_start > 0:
                    if (time.monotonic() - unmute_grace_start) < _UNMUTE_GRACE_S:
                        continue
                    unmute_grace_start = 0.0

                pre_buffer.append(chunk)

                # ── Listening: collect speech ────────────────────────
                if self._state == "listening":
                    energy = np.abs(chunk.astype(np.float32)).mean()

                    if energy >= 300:
                        if not speech_chunks:
                            speech_chunks = list(pre_buffer)
                            silence_counter = 0
                            speech_start = time.monotonic()
                        speech_chunks.append(chunk)
                        silence_counter = 0
                    elif speech_chunks:
                        speech_chunks.append(chunk)
                        silence_counter += 1
                        elapsed = time.monotonic() - speech_start
                        silence_s = silence_counter * (CHUNK_SAMPLES / SAMPLE_RATE)

                        if silence_s >= _SILENCE_TIMEOUT_S or elapsed >= _MAX_RECORDING_S:
                            if len(speech_chunks) < _MIN_SPEECH_CHUNKS:
                                speech_chunks = []
                                silence_counter = 0
                            else:
                                # ── Transcribe ───────────────────────
                                self._set_state("transcribing")
                                self.status_queue.put("⏳ Processing…")

                                pcm = np.concatenate(speech_chunks)
                                try:
                                    text = self._transcribe_pcm_bytes(pcm.tobytes())
                                except Exception as exc:
                                    logger.error("Transcription error: %s", exc)
                                    text = ""

                                if text:
                                    logger.info("Transcribed: %s", text)
                                    self.results.put(text)
                                    # Stay muted until agent finishes
                                    speech_chunks = []
                                    silence_counter = 0
                                    pre_buffer.clear()
                                    self._mute_event.set()
                                    self._unmute_event.clear()
                                    self._set_state("muted")
                                    self.status_queue.put("🔇 Processing…")
                                else:
                                    self.status_queue.put("Couldn't understand audio")
                                    speech_chunks = []
                                    silence_counter = 0
                                    pre_buffer.clear()
                                    self._set_state("listening")
                                    self.status_queue.put("🔴 Listening…")

        except Exception as exc:
            logger.error("Voice pipeline error: %s", exc)
            self.status_queue.put(f"Voice error: {exc}")
        finally:
            stream.stop()
            stream.close()
            self._set_state("stopped")
            self.status_queue.put("Voice stopped")
            logger.info("Voice pipeline stopped")

    # ── Public API ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Start listening immediately."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._mute_event.clear()
            self._unmute_event.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="row-bot-voice",
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop listening and kill the background thread."""
        with self._lock:
            self._stop_event.set()
            if self._thread is not None:
                self._thread.join(timeout=3)
                self._thread = None
            self._set_state("stopped")

    def get_transcription(self) -> str | None:
        try:
            return self.results.get_nowait()
        except Empty:
            return None

    def get_status(self) -> str | None:
        msg = None
        try:
            while True:
                msg = self.status_queue.get_nowait()
        except Empty:
            pass
        return msg

    def transcribe_bytes(self, audio_bytes: bytes) -> str:
        """One-shot transcription of raw PCM bytes."""
        return self._transcribe_pcm_bytes(audio_bytes)

    def transcribe_pcm16(
        self,
        audio_bytes: bytes,
        *,
        allow_download: bool = False,
    ) -> str:
        """Transcribe 16 kHz mono signed-16 PCM."""
        self._ensure_whisper(allow_download=allow_download)
        f32 = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        segs, _ = self._whisper_model.transcribe(
            f32, beam_size=5, language="en", vad_filter=True,
        )
        return " ".join(s.text.strip() for s in segs).strip()


# ── Singleton ────────────────────────────────────────────────────────────────
_instance: VoiceService | None = None
_instance_lock = threading.Lock()


def get_voice_service() -> VoiceService:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = VoiceService()
        return _instance


def get_available_whisper_sizes() -> list[str]:
    return ["tiny", "base", "small", "medium"]
