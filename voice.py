"""Voice service — local STT with wake-word detection.

Pipeline:
    sounddevice (background thread, always capturing 16 kHz mono)
    → openwakeword (ONNX, detects wake word in 80 ms chunks)
    → VAD collects speech until silence
    → faster-whisper transcribes the utterance
    → result placed on a thread-safe queue for Streamlit to consume
"""

from __future__ import annotations

import io
import logging
import os
import struct
import threading
import time
import wave
from collections import deque
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Literal

import numpy as np

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16_000        # 16 kHz — required by both openwakeword and Whisper
CHUNK_SAMPLES = 1280        # 80 ms at 16 kHz — openwakeword's expected frame size
CHANNELS = 1

# VAD / speech collection
_PRE_SPEECH_CHUNKS = 8      # ~640 ms of audio kept before wake word trigger
_SILENCE_TIMEOUT_S = 1.5    # seconds of silence after last speech to stop recording
_MAX_RECORDING_S = 30       # hard cap on a single utterance
_MIN_SPEECH_CHUNKS = 5      # minimum chunks to count as real speech (~400 ms)

# Wake-word
_DEFAULT_WAKE_MODEL = "hey_jarvis_v0.1"
_WAKE_THRESHOLD = 0.5       # prediction score threshold (0–1)

# Whisper
_DEFAULT_WHISPER_SIZE = "base"

# Follow-up mode (listen without wake word after TTS finishes)
_FOLLOW_UP_TIMEOUT_S = 8.0  # seconds to wait for speech before returning to sleeping
_FOLLOW_UP_GRACE_S = 0.6    # seconds to ignore audio after entering follow-up (speaker drain)

# Browser heartbeat — auto-pause mic when browser tab is closed
_HEARTBEAT_STALE_S = 5.0    # seconds without heartbeat before auto-pausing

# Data directory
_DATA_DIR = Path.home() / ".thoth"
_VOICE_SETTINGS_FILE = _DATA_DIR / "voice_settings.json"

# ── Wake-word chime (generated once) ─────────────────────────────────────────
_CHIME_PATH: Path | None = None

def _ensure_chime() -> Path:
    """Generate a short two-tone chime WAV (if not already on disk)."""
    global _CHIME_PATH
    if _CHIME_PATH and _CHIME_PATH.exists():
        return _CHIME_PATH
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _DATA_DIR / "wake_chime.wav"
    if not path.exists():
        sr = 22050
        dur1, dur2 = 0.08, 0.10  # two short tones
        t1 = np.linspace(0, dur1, int(sr * dur1), endpoint=False)
        t2 = np.linspace(0, dur2, int(sr * dur2), endpoint=False)
        # Rising two-tone: 880 Hz then 1320 Hz, with fade-in/out
        tone1 = (np.sin(2 * np.pi * 880 * t1) * 0.5)
        tone2 = (np.sin(2 * np.pi * 1320 * t2) * 0.5)
        # Apply fade envelopes
        fade1 = np.minimum(t1 / 0.01, 1.0) * np.minimum((dur1 - t1) / 0.01, 1.0)
        fade2 = np.minimum(t2 / 0.01, 1.0) * np.minimum((dur2 - t2) / 0.01, 1.0)
        tone1 *= fade1
        tone2 *= fade2
        silence = np.zeros(int(sr * 0.02))  # 20 ms gap
        audio = np.concatenate([tone1, silence, tone2])
        pcm = (audio * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm.tobytes())
    _CHIME_PATH = path
    return path

def _play_chime() -> None:
    """Play the wake-word chime asynchronously (non-blocking)."""
    try:
        path = _ensure_chime()
        if os.name == "nt":
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            # Fallback: fire-and-forget in a thread
            threading.Thread(
                target=lambda: __import__("subprocess").run(
                    ["aplay", "-q", str(path)], capture_output=True
                ),
                daemon=True,
            ).start()
    except Exception:
        pass  # never let chime failure break voice

# Project-local wake-word models (committed to repo / shipped with installer)
_WAKE_MODELS_DIR = Path(__file__).resolve().parent / "wake_models"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _pcm_to_wav_bytes(pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Convert int16 PCM numpy array to WAV file bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _load_voice_settings() -> dict:
    """Load persisted voice settings."""
    import json
    if _VOICE_SETTINGS_FILE.exists():
        try:
            return json.loads(_VOICE_SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_voice_settings(settings: dict) -> None:
    """Persist voice settings."""
    import json
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _VOICE_SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


# ── Voice Service ────────────────────────────────────────────────────────────

class VoiceService:
    """Manages the full voice-input pipeline in a background thread.

    State machine::

        stopped ─► sleeping (wake-word listening)
                      │
                      ▼
                  listening (VAD collecting speech)
                      │
                      ▼
                  transcribing (Whisper)
                      │
                      ├─► sleeping  (text-only, no TTS)
                      └─► muted    (TTS is playing — mic paused)
                             │
                             ▼
                         follow_up  (listen without wake word for 8 s)
                             │
                             ├─► listening  (speech detected)
                             └─► sleeping   (timeout — require wake word)
    """

    # Possible states
    State = Literal["stopped", "sleeping", "listening", "transcribing", "muted", "follow_up"]

    def __init__(self) -> None:
        self._state: VoiceService.State = "stopped"
        self._lock = threading.Lock()

        # Thread-safe queue: transcribed strings ready for the UI
        self.results: SimpleQueue[str] = SimpleQueue()

        # Status messages for the UI (non-blocking)
        self.status_queue: SimpleQueue[str] = SimpleQueue()

        # Background thread
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Models (lazy loaded)
        self._oww_model = None
        self._whisper_model = None

        # Mic gating — TTS sets these via mute() / enter_follow_up()
        self._mute_event = threading.Event()      # set = mic should be muted
        self._follow_up_event = threading.Event()  # set = enter follow-up mode

        # Browser heartbeat
        self._last_heartbeat: float = time.monotonic()
        self._heartbeat_paused: bool = False

        # Settings
        settings = _load_voice_settings()
        self._whisper_size: str = settings.get("whisper_model", _DEFAULT_WHISPER_SIZE)
        self._wake_model: str = settings.get("wake_model", _DEFAULT_WAKE_MODEL)
        self._wake_threshold: float = settings.get("wake_threshold", _WAKE_THRESHOLD)

    # ── Properties ───────────────────────────────────────────────────────

    # ── Status file (IPC with launcher tray) ────────────────────────

    def _set_state(self, new_state: "VoiceService.State") -> None:
        """Change state atomically."""
        self._state = new_state

    @property
    def state(self) -> State:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state != "stopped"

    # ── Mic gating API (called by TTSService) ────────────────────────

    def mute(self) -> None:
        """Pause mic processing — called when TTS starts speaking."""
        self._mute_event.set()
        self._follow_up_event.clear()
        logger.debug("Voice muted (TTS speaking)")

    def enter_follow_up(self) -> None:
        """Resume mic in follow-up mode — called when TTS finishes.

        Do NOT clear ``_mute_event`` here.  The pipeline loop detects
        ``_follow_up_event`` while still inside the muted block and
        clears both events atomically during the transition.  Clearing
        ``_mute_event`` early causes a race where the pipeline exits
        the muted block before seeing the follow-up signal, leaving the
        state stuck on ``muted`` with no handler matching.
        """
        self._follow_up_event.set()
        logger.debug("Entering follow-up mode")

    # ── Browser heartbeat ────────────────────────────────────────────

    def update_heartbeat(self) -> None:
        """Called by the Streamlit poll fragment each tick."""
        self._last_heartbeat = time.monotonic()
        if self._heartbeat_paused:
            self._heartbeat_paused = False
            logger.info("Browser reconnected — resuming voice")
            self.status_queue.put("Browser reconnected")

    def _check_heartbeat(self) -> bool:
        """Return True if the browser heartbeat is fresh (tab is open)."""
        stale = (time.monotonic() - self._last_heartbeat) > _HEARTBEAT_STALE_S
        if stale and not self._heartbeat_paused:
            self._heartbeat_paused = True
            logger.info("Browser heartbeat stale — pausing voice")
            self.status_queue.put("Browser tab closed — mic paused")
        return not stale

    @property
    def whisper_size(self) -> str:
        return self._whisper_size

    @whisper_size.setter
    def whisper_size(self, value: str) -> None:
        self._whisper_size = value
        self._whisper_model = None  # force reload
        settings = _load_voice_settings()
        settings["whisper_model"] = value
        _save_voice_settings(settings)

    @property
    def wake_model(self) -> str:
        return self._wake_model

    @wake_model.setter
    def wake_model(self, value: str) -> None:
        self._wake_model = value
        self._oww_model = None  # force reload
        settings = _load_voice_settings()
        settings["wake_model"] = value
        _save_voice_settings(settings)

    @property
    def wake_threshold(self) -> float:
        return self._wake_threshold

    @wake_threshold.setter
    def wake_threshold(self, value: float) -> None:
        self._wake_threshold = value
        settings = _load_voice_settings()
        settings["wake_threshold"] = value
        _save_voice_settings(settings)

    # ── Model loading ────────────────────────────────────────────────────

    def _ensure_oww(self):
        """Load openwakeword model (ONNX) if not already loaded."""
        if self._oww_model is not None:
            return

        from openwakeword.model import Model as OWWModel

        onnx_path = _WAKE_MODELS_DIR / f"{self._wake_model}.onnx"
        if not onnx_path.exists():
            available = [f.stem for f in _WAKE_MODELS_DIR.glob("*.onnx")]
            raise FileNotFoundError(
                f"Wake-word model not found: {onnx_path}\n"
                f"Available models in wake_models/: {available}"
            )

        self._oww_model = OWWModel(
            wakeword_models=[str(onnx_path)],
            inference_framework="onnx",
        )
        logger.info("Loaded wake-word model: %s", self._wake_model)

    def _ensure_whisper(self):
        """Load faster-whisper model if not already loaded."""
        if self._whisper_model is not None:
            return

        from faster_whisper import WhisperModel

        self.status_queue.put(f"Loading Whisper ({self._whisper_size})…")
        self._whisper_model = WhisperModel(
            self._whisper_size, device="cpu", compute_type="int8"
        )
        logger.info("Loaded Whisper model: %s", self._whisper_size)

    # ── Core pipeline (runs in background thread) ────────────────────────

    def _run_pipeline(self) -> None:
        """Main loop with mic gating and follow-up mode.

        States handled:
            sleeping     – listen for wake word via openwakeword
            listening    – VAD collecting speech chunks
            transcribing – Whisper processing
            muted        – TTS is playing, mic processing paused
            follow_up    – listen for speech without wake word (time-limited)
        """
        import sounddevice as sd

        try:
            self._ensure_oww()
        except Exception as exc:
            self.status_queue.put(f"Wake-word init failed: {exc}")
            logger.error("Wake-word init failed: %s", exc)
            self._state = "stopped"
            return

        try:
            self._ensure_whisper()
        except Exception as exc:
            self.status_queue.put(f"Whisper init failed: {exc}")
            logger.error("Whisper init failed: %s", exc)
            self._state = "stopped"
            return

        self.status_queue.put("Voice ready — say the wake word")
        self._set_state("sleeping")

        # Ring buffer for pre-speech audio
        pre_buffer: deque[np.ndarray] = deque(maxlen=_PRE_SPEECH_CHUNKS)

        # Open mic stream
        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SAMPLES,
            )
            stream.start()
        except Exception as exc:
            self.status_queue.put(f"Mic error: {exc}")
            logger.error("Could not open mic: %s", exc)
            self._state = "stopped"
            return

        # Variables used across states
        speech_chunks: list[np.ndarray] = []
        silence_counter = 0
        speech_start = 0.0
        follow_up_start = 0.0
        follow_up_silence = 0

        try:
            while not self._stop_event.is_set():

                # ── Browser heartbeat check ──────────────────────────
                if not self._check_heartbeat():
                    # Browser tab is closed — pause processing
                    time.sleep(0.1)
                    continue

                # ── Muted state (TTS speaking) ───────────────────────
                if self._mute_event.is_set():
                    if self._state != "muted":
                        self._set_state("muted")
                        self.status_queue.put("🔇 Speaking…")
                    # Check if TTS finished and signalled follow-up
                    if self._follow_up_event.is_set():
                        self._follow_up_event.clear()
                        self._mute_event.clear()
                        self._set_state("follow_up")
                        follow_up_start = time.monotonic()
                        follow_up_silence = 0
                        pre_buffer.clear()
                        _play_chime()
                        self.status_queue.put("👂 Listening (no wake word needed)…")
                        logger.info("Entered follow-up mode")
                    else:
                        time.sleep(0.05)  # don't busy-loop while muted
                    continue

                # Read a chunk from the mic
                data, overflowed = stream.read(CHUNK_SAMPLES)
                if overflowed:
                    logger.debug("Audio buffer overflow")

                chunk = data[:, 0].copy()  # shape (1280,) int16

                # ── Follow-up mode (listen without wake word) ────────
                if self._state == "follow_up":
                    elapsed_fu = time.monotonic() - follow_up_start

                    # Grace period — discard audio to let speaker buffer drain
                    if elapsed_fu < _FOLLOW_UP_GRACE_S:
                        continue

                    energy = np.abs(chunk.astype(np.float32)).mean()

                    if energy >= 300:
                        # Speech detected — transition to listening
                        logger.info("Speech detected in follow-up mode")
                        self.status_queue.put("Listening…")
                        self._set_state("listening")
                        speech_chunks = list(pre_buffer) + [chunk]
                        silence_counter = 0
                        speech_start = time.monotonic()
                        continue
                    else:
                        follow_up_silence += 1

                    # Timeout — back to wake-word mode
                    if elapsed_fu >= _FOLLOW_UP_TIMEOUT_S:
                        logger.info("Follow-up timeout — returning to sleeping")
                        self.status_queue.put("Say the wake word…")
                        self._set_state("sleeping")
                        pre_buffer.clear()
                    else:
                        pre_buffer.append(chunk)
                    continue

                pre_buffer.append(chunk)

                # ── Phase 1: Wake word detection ─────────────────────
                if self._state == "sleeping":
                    prediction = self._oww_model.predict(chunk)
                    for model_name, score in prediction.items():
                        if score >= self._wake_threshold:
                            logger.info(
                                "Wake word detected: %s (score=%.3f)",
                                model_name, score,
                            )
                            self.status_queue.put("Listening…")
                            self._set_state("listening")
                            _play_chime()
                            self._oww_model.reset()

                            # Start collecting speech — include pre-buffer
                            speech_chunks = list(pre_buffer)
                            silence_counter = 0
                            speech_start = time.monotonic()
                            break

                # ── Phase 2: Collect speech until silence ────────────
                elif self._state == "listening":
                    speech_chunks.append(chunk)

                    # Simple energy-based silence detection
                    energy = np.abs(chunk.astype(np.float32)).mean()
                    if energy < 300:  # ~silence threshold for int16
                        silence_counter += 1
                    else:
                        silence_counter = 0

                    elapsed = time.monotonic() - speech_start
                    silence_s = silence_counter * (CHUNK_SAMPLES / SAMPLE_RATE)

                    # Stop conditions
                    if silence_s >= _SILENCE_TIMEOUT_S or elapsed >= _MAX_RECORDING_S:
                        if len(speech_chunks) < _MIN_SPEECH_CHUNKS:
                            # Too short — probably a false trigger
                            logger.debug("Utterance too short, ignoring")
                            self.status_queue.put("Say the wake word…")
                            self._set_state("sleeping")
                            continue

                        # ── Phase 3: Transcribe ──────────────────────
                        self._set_state("transcribing")
                        self.status_queue.put("Transcribing…")

                        audio_pcm = np.concatenate(speech_chunks)
                        audio_f32 = audio_pcm.astype(np.float32) / 32768.0

                        try:
                            segments, info = self._whisper_model.transcribe(
                                audio_f32,
                                beam_size=5,
                                language=None,  # auto-detect
                                vad_filter=True,
                            )
                            text = " ".join(
                                seg.text.strip() for seg in segments
                            ).strip()
                        except Exception as exc:
                            logger.error("Transcription error: %s", exc)
                            text = ""

                        if text:
                            logger.info("Transcribed: %s", text)
                            self.results.put(text)
                        else:
                            self.status_queue.put("Couldn't understand audio")

                        # Back to sleeping (TTS will transition to
                        # muted → follow_up if it speaks the response)
                        self.status_queue.put("Say the wake word…")
                        self._set_state("sleeping")
                        pre_buffer.clear()

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
        """Start the voice pipeline in a background thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return  # already running
            self._stop_event.clear()
            self._mute_event.clear()
            self._follow_up_event.clear()
            self._last_heartbeat = time.monotonic()
            self._heartbeat_paused = False
            self._set_state("sleeping")
            self._thread = threading.Thread(
                target=self._run_pipeline,
                daemon=True,
                name="thoth-voice",
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the voice pipeline."""
        with self._lock:
            self._stop_event.set()
            if self._thread is not None:
                self._thread.join(timeout=3)
                self._thread = None
            self._set_state("stopped")

    def get_transcription(self) -> str | None:
        """Non-blocking: return a transcribed string or None."""
        try:
            return self.results.get_nowait()
        except Empty:
            return None

    def get_status(self) -> str | None:
        """Non-blocking: return the latest status message or None."""
        msg = None
        # Drain to get the latest
        try:
            while True:
                msg = self.status_queue.get_nowait()
        except Empty:
            pass
        return msg

    def transcribe_bytes(self, audio_bytes: bytes) -> str:
        """One-shot transcription of WAV/PCM bytes (for manual recording)."""
        self._ensure_whisper()
        audio_f32 = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = self._whisper_model.transcribe(
            audio_f32, beam_size=5, language=None, vad_filter=True,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()


# ── Module-level singleton ───────────────────────────────────────────────────
# Shared across all Streamlit sessions — persists as long as the server process
# is alive, so the wake-word listener survives browser tab closes.

_voice_instance: VoiceService | None = None
_voice_lock = threading.Lock()


def get_voice_service() -> VoiceService:
    """Return the process-wide VoiceService singleton."""
    global _voice_instance
    with _voice_lock:
        if _voice_instance is None:
            _voice_instance = VoiceService()
        return _voice_instance

def get_available_wake_models() -> list[str]:
    """Return names of ONNX wake-word models in the wake_models/ folder."""
    _skip = ("silero", "embedding", "melspectrogram")
    if not _WAKE_MODELS_DIR.is_dir():
        return []
    return sorted(
        f.stem
        for f in _WAKE_MODELS_DIR.glob("*.onnx")
        if not f.name.startswith(_skip)
    )


def get_available_whisper_sizes() -> list[str]:
    """Return supported Whisper model sizes."""
    return ["tiny", "base", "small"]
