"""tts.py – Text-to-Speech service using Kokoro TTS (local, offline).

Kokoro is a fast, high-quality neural text-to-speech model that runs
entirely via ONNX Runtime.  This module manages model downloads,
preprocesses agent responses (stripping markdown, truncating long text),
and plays audio through the default output device using sounddevice.

Everything runs locally — no audio is sent to the cloud.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from queue import Queue, Empty
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
_THOTH_DIR = Path.home() / ".thoth"
_KOKORO_DIR = _THOTH_DIR / "kokoro"
_SETTINGS_PATH = _THOTH_DIR / "tts_settings.json"

# ── Model download URLs ─────────────────────────────────────────────────────
_KOKORO_RELEASE_BASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
_MODEL_FILENAME = "kokoro-v1.0.fp16.onnx"
_VOICES_FILENAME = "voices-v1.0.bin"

_MODEL_PATH = _KOKORO_DIR / _MODEL_FILENAME
_VOICES_PATH = _KOKORO_DIR / _VOICES_FILENAME

_MODEL_URL = f"{_KOKORO_RELEASE_BASE}/{_MODEL_FILENAME}"
_VOICES_URL = f"{_KOKORO_RELEASE_BASE}/{_VOICES_FILENAME}"

# ── Voice catalog (id → display name) ───────────────────────────────────────
# Curated set of the highest-rated Kokoro v1.0 voices.
VOICE_CATALOG: dict[str, str] = {
    "af_heart":   "Heart (American Female) ❤️",
    "af_bella":   "Bella (American Female)",
    "af_nicole":  "Nicole (American Female)",
    "af_sarah":   "Sarah (American Female)",
    "af_nova":    "Nova (American Female)",
    "am_michael": "Michael (American Male)",
    "am_fenrir":  "Fenrir (American Male)",
    "am_puck":    "Puck (American Male)",
    "bf_emma":    "Emma (British Female)",
    "bm_george":  "George (British Male)",
}
_DEFAULT_VOICE = "af_heart"

# ── Markdown / noise stripping patterns ──────────────────────────────────────
# Order matters: links/code first, then blocks, then line-level (bullets
# BEFORE emphasis so `* ` bullets aren't eaten by the *italic* regex),
# then emphasis (triple before double before single), then cleanup.
_MD_STRIP: list[tuple[re.Pattern, str]] = [
    # ── Fenced code blocks (must run BEFORE inline code) ─────────────
    (re.compile(r"```[\s\S]*?```"),                   ""),       # fenced code blocks

    # ── Inline formatting (keep inner text) ──────────────────────────
    (re.compile(r"`([^`]+)`"),                         r"\1"),    # inline code → text
    (re.compile(r"\[([^\]]+)\]\([^\)]*\)"),            r"\1"),    # [text](url) → text

    # ── Blocks (remove entirely) ─────────────────────────────────────
    (re.compile(r"^\|.*\|$", re.MULTILINE),           ""),       # table rows
    (re.compile(r"^[-=]{3,}$", re.MULTILINE),         ""),       # horizontal rules
    (re.compile(r"!\[.*?\]\(.*?\)"),                   ""),       # images
    (re.compile(r"https?://\S+"),                      ""),       # raw URLs
    (re.compile(r"\S+@\S+\.\S+"),                      ""),       # email addresses

    # ── Line-level (must run BEFORE emphasis) ────────────────────────
    (re.compile(r"^#{1,6}\s+", re.MULTILINE),          ""),       # headers
    (re.compile(r"^>\s?", re.MULTILINE),               ""),       # blockquotes
    (re.compile(r"^\s*[-*+•◦▪▸●○⬤◉⚫]\s+", re.MULTILINE), ""),  # bullet lists
    (re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE),      ""),       # numbered lists

    # ── Emphasis (triple before double before single) ────────────────
    (re.compile(r"\*{3}(.+?)\*{3}", re.DOTALL),       r"\1"),    # ***bold italic***
    (re.compile(r"_{3}(.+?)_{3}", re.DOTALL),          r"\1"),    # ___bold italic___
    (re.compile(r"\*{2}(.+?)\*{2}", re.DOTALL),       r"\1"),    # **bold**
    (re.compile(r"_{2}(.+?)_{2}", re.DOTALL),          r"\1"),    # __bold__
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", re.DOTALL), r"\1"),  # *italic*
    (re.compile(r"~~(.+?)~~", re.DOTALL),              r"\1"),    # ~~strikethrough~~

    # ── Emoji ────────────────────────────────────────────────────────
    (re.compile(
        r"[\U0001f000-\U0001ffff\u2600-\u27bf\ufe00-\ufe0f\u200d]"
    ), ""),

    # ── Final cleanup sweeps ─────────────────────────────────────────
    (re.compile(r"[•◦▪▸▹►▻●○⬤◉⚫·∙⋅]"),             ""),       # ALL dot/bullet chars
    (re.compile(r"[–—]"),                              " "),      # en/em dashes → space
    (re.compile(r"\*{2,}"),                            ""),       # leftover ** or ***
    (re.compile(r"(?<!\w)\*(?!\w)"),                   ""),       # stray lone *
    (re.compile(r"\n{3,}"),                            "\n\n"),   # excess newlines
    (re.compile(r"[ \t]{2,}"),                         " "),      # excess whitespace
]

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_MAX_SPEAK_SENTENCES = 3
_FALLBACK_MSG = "I've provided the response in the app."
_TRUNCATION_SUFFIX = " The full response is shown in the app."


# ═════════════════════════════════════════════════════════════════════════════
# TTSService
# ═════════════════════════════════════════════════════════════════════════════

class TTSService:
    """Text-to-speech using Kokoro TTS via ONNX Runtime (local, offline).

    Mic gating: when a ``voice_service`` is attached, this service will
    call ``voice_service.mute()`` before speaking and
    ``voice_service.unmute()`` when finished, preventing echo
    feedback loops.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playback_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Streaming TTS queue
        self._stream_queue: Queue[str | None] = Queue()
        self._stream_worker: threading.Thread | None = None

        # Reference to VoiceService for mic gating (set via property)
        self._voice_service = None

        # Lazy-loaded Kokoro instance (heavy — loads ~169 MB model)
        self._kokoro = None
        self._kokoro_lock = threading.Lock()

        # Persisted settings
        self._voice: str = _DEFAULT_VOICE
        self._speed: float = 1.0
        self._enabled: bool = False
        self._auto_speak: bool = True
        self._load_settings()

    # ── Kokoro model (lazy-loaded) ───────────────────────────────────────

    def _get_kokoro(self):
        """Return the Kokoro instance, loading the model on first call."""
        if self._kokoro is not None:
            return self._kokoro

        with self._kokoro_lock:
            if self._kokoro is not None:
                return self._kokoro

            if not self.is_installed():
                return None

            try:
                from kokoro_onnx import Kokoro
                logger.info("Loading Kokoro TTS model from %s", _MODEL_PATH)
                self._kokoro = Kokoro(str(_MODEL_PATH), str(_VOICES_PATH))
                logger.info("Kokoro TTS model loaded successfully")
            except Exception:
                logger.warning("Failed to load Kokoro TTS model", exc_info=True)
                return None

        return self._kokoro

    # ── Voice service link (mic gating) ──────────────────────────────────

    @property
    def voice_service(self):
        return self._voice_service

    @voice_service.setter
    def voice_service(self, svc) -> None:
        self._voice_service = svc

    def _mute_mic(self) -> None:
        """Tell the voice service to pause mic processing."""
        if self._voice_service and self._voice_service.is_running:
            self._voice_service.mute()

    def _unmute_mic(self) -> None:
        """Tell the voice service to resume listening."""
        if self._voice_service and self._voice_service.is_running:
            self._voice_service.unmute()

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def voice(self) -> str:
        return self._voice

    @voice.setter
    def voice(self, v: str) -> None:
        self._voice = v
        self._save_settings()

    @property
    def speed(self) -> float:
        return self._speed

    @speed.setter
    def speed(self, s: float) -> None:
        self._speed = max(0.5, min(2.0, s))
        self._save_settings()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, e: bool) -> None:
        self._enabled = e
        self._save_settings()

    @property
    def auto_speak(self) -> bool:
        return self._auto_speak

    @auto_speak.setter
    def auto_speak(self, a: bool) -> None:
        self._auto_speak = a
        self._save_settings()

    @property
    def is_speaking(self) -> bool:
        return (
            (self._playback_thread is not None
             and self._playback_thread.is_alive())
            or (self._stream_worker is not None
                and self._stream_worker.is_alive())
        )

    # ── Status checks ────────────────────────────────────────────────────

    def is_installed(self) -> bool:
        """True if the Kokoro model and voices file are both present."""
        return _MODEL_PATH.exists() and _VOICES_PATH.exists()

    # Backward-compatible aliases used by app_nicegui.py
    def is_piper_installed(self) -> bool:
        """Alias for is_installed() — kept for backward compatibility."""
        return self.is_installed()

    def is_voice_installed(self, voice_id: str | None = None) -> bool:
        """True if the TTS engine is installed (all voices are bundled)."""
        return self.is_installed()

    def get_installed_voices(self) -> list[str]:
        """Return voice IDs available in the catalog."""
        if not self.is_installed():
            return []
        return sorted(VOICE_CATALOG.keys())

    # ── Core speak / stop ────────────────────────────────────────────────

    def speak(self, text: str, from_voice_input: bool = False) -> None:
        """Auto-speak: only fires for voice input when *auto_speak* is on."""
        if not self._enabled or not self._auto_speak or not from_voice_input:
            return
        self._speak_internal(text)

    def speak_now(self, text: str) -> None:
        """Speak immediately (test button / future read-aloud)."""
        self._speak_internal(text)

    def stop(self) -> None:
        """Stop current playback and clear the streaming queue."""
        self._stop_event.set()
        # Drain the streaming queue
        try:
            while True:
                self._stream_queue.get_nowait()
        except Empty:
            pass
        # Stop any sounddevice playback immediately
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass
        # Don't block — let daemon threads wind down on their own
        if self._playback_thread and self._playback_thread.is_alive():
            self._playback_thread.join(timeout=0.5)
        if self._stream_worker and self._stream_worker.is_alive():
            # Signal worker to exit only if it's alive to consume the sentinel
            self._stream_queue.put(None)
            self._stream_worker.join(timeout=0.5)
        self._stream_worker = None

    # ── Streaming TTS (sentence-by-sentence) ─────────────────────────

    def speak_streaming(self, sentence: str) -> None:
        """Queue a sentence for streaming playback. Non-blocking."""
        if not self._enabled or not self._auto_speak:
            return
        if not self.is_installed():
            return

        clean = _prepare_text(sentence, truncate=False)
        if not clean.strip():
            return

        # Mute mic on first sentence queued
        self._mute_mic()

        # Ensure worker thread is running
        if self._stream_worker is None or not self._stream_worker.is_alive():
            self._stop_event.clear()
            # Drain any stale items left from a previous stop()/session
            try:
                while True:
                    self._stream_queue.get_nowait()
            except Empty:
                pass
            self._stream_worker = threading.Thread(
                target=self._stream_worker_loop, daemon=True,
                name="thoth-tts-stream",
            )
            self._stream_worker.start()

        self._stream_queue.put(clean)

    def flush_streaming(self, remaining: str = "") -> None:
        """Send any remaining text and signal end-of-stream."""
        if remaining and remaining.strip():
            self.speak_streaming(remaining)
        # Sentinel: worker will exit after playing everything queued
        self._stream_queue.put(None)

    def _stream_worker_loop(self) -> None:
        """Background worker: pick sentences from the queue and play them.

        When the stream ends naturally via the end-of-stream sentinel from
        ``flush_streaming``, the mic is unmuted.  If ``stop()`` kills this
        worker (sets ``_stop_event``), the mic stays muted so the caller
        can decide what to do next.
        """
        import sounddevice as sd

        natural_end = False

        while not self._stop_event.is_set():
            try:
                sentence = self._stream_queue.get(timeout=1)
            except Empty:
                continue

            if sentence is None:
                natural_end = True
                break  # end-of-stream sentinel

            if self._stop_event.is_set():
                break

            kokoro = self._get_kokoro()
            if kokoro is None:
                continue

            try:
                lang = _voice_lang(self._voice)
                samples, sample_rate = kokoro.create(
                    sentence,
                    voice=self._voice,
                    speed=self._speed,
                    lang=lang,
                )

                if self._stop_event.is_set():
                    break

                sd.play(samples, samplerate=sample_rate)
                sd.wait()

            except Exception:
                logger.debug("TTS stream playback error", exc_info=True)

        # Only unmute on NATURAL end-of-stream (not forced stop).
        if natural_end and not self._stop_event.is_set():
            time.sleep(0.5)
            if not self._stop_event.is_set():
                self._unmute_mic()

    # ── Download helpers ─────────────────────────────────────────────────

    def download_model(
        self, progress: Callable[[float], None] | None = None
    ) -> None:
        """Download the Kokoro ONNX model and voices file."""
        _KOKORO_DIR.mkdir(parents=True, exist_ok=True)

        logger.info("Downloading Kokoro TTS model from %s", _MODEL_URL)
        _download_file(_MODEL_URL, _MODEL_PATH, progress)

        logger.info("Downloading Kokoro voices from %s", _VOICES_URL)
        _download_file(_VOICES_URL, _VOICES_PATH, None)

    # Backward-compatible aliases
    def download_piper(
        self, progress: Callable[[float], None] | None = None
    ) -> None:
        """Alias for download_model() — kept for backward compatibility."""
        self.download_model(progress)

    def download_voice(
        self,
        voice_id: str | None = None,
        progress: Callable[[float], None] | None = None,
    ) -> None:
        """No-op — all Kokoro voices are bundled in a single file."""
        pass  # All voices included in voices-v1.0.bin

    # ── Internal ─────────────────────────────────────────────────────────

    def _speak_internal(self, text: str) -> None:
        """Prepare text and start background playback."""
        if not self.is_installed():
            return

        clean = _prepare_text(text)
        if not clean.strip():
            return

        self.stop()
        self._stop_event.clear()
        # Mute mic before speaking
        self._mute_mic()
        self._playback_thread = threading.Thread(
            target=self._play, args=(clean,), daemon=True,
        )
        self._playback_thread.start()

    def _play(self, text: str) -> None:
        """Generate audio with Kokoro and play it via sounddevice."""
        import sounddevice as sd

        try:
            kokoro = self._get_kokoro()
            if kokoro is None:
                return

            lang = _voice_lang(self._voice)
            samples, sample_rate = kokoro.create(
                text,
                voice=self._voice,
                speed=self._speed,
                lang=lang,
            )

            if self._stop_event.is_set():
                return

            sd.play(samples, samplerate=sample_rate)
            sd.wait()

        except Exception:
            logger.warning("TTS playback error", exc_info=True)

    def _load_settings(self) -> None:
        try:
            if _SETTINGS_PATH.exists():
                data = json.loads(_SETTINGS_PATH.read_text())
                saved_voice = data.get("voice", _DEFAULT_VOICE)
                # Migrate from Piper voice IDs (e.g. "en_US-lessac-medium")
                # to Kokoro voice IDs — fall back to default if not in catalog
                if saved_voice in VOICE_CATALOG:
                    self._voice = saved_voice
                else:
                    self._voice = _DEFAULT_VOICE
                self._speed = data.get("speed", 1.0)
                self._enabled = data.get("enabled", False)
                self._auto_speak = data.get("auto_speak", True)
        except Exception:
            logger.warning("Failed to load TTS settings from %s", _SETTINGS_PATH, exc_info=True)

    def _save_settings(self) -> None:
        try:
            _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "voice": self._voice,
                "speed": self._speed,
                "enabled": self._enabled,
                "auto_speak": self._auto_speak,
            }
            _SETTINGS_PATH.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.warning("Failed to save TTS settings", exc_info=True)


# ═════════════════════════════════════════════════════════════════════════════
# Module-level helpers
# ═════════════════════════════════════════════════════════════════════════════

def _voice_lang(voice_id: str) -> str:
    """Infer the ``lang`` parameter from a Kokoro voice ID prefix.

    Kokoro voice IDs use a two-letter prefix:
      a* = American English, b* = British English,
      j* = Japanese, z* = Mandarin Chinese, etc.
    """
    prefix = voice_id[:1] if voice_id else "a"
    return {
        "a": "en-us",
        "b": "en-gb",
        "j": "ja",
        "z": "cmn",
        "e": "es",
        "f": "fr-fr",
        "h": "hi",
        "i": "it",
        "p": "pt-br",
    }.get(prefix, "en-us")


def _prepare_text(text: str, *, truncate: bool = True) -> str:
    """Strip markdown formatting and optionally truncate for speech.

    When *truncate* is ``False`` the sentence-cap / truncation-suffix
    logic is skipped — useful for streaming TTS where the caller
    manages the sentence budget."""
    clean = text
    for pattern, repl in _MD_STRIP:
        clean = pattern.sub(repl, clean)

    # Split into lines, ensure each ends with punctuation so the TTS
    # pauses naturally between sentences / paragraphs.
    lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if ln and not ln[-1] in ".!?:;,":
            lines[i] = ln + "."
    clean = "\n".join(lines)

    clean = clean.strip()

    # If most content was code/tables (stripped away), use a fallback phrase
    if not clean or len(clean) < len(text.strip()) * 0.25:
        return _FALLBACK_MSG

    if not truncate:
        return clean

    # Split into sentences
    sentences = _SENTENCE_END_RE.split(clean)
    sentences = [s.strip() for s in sentences if s.strip()]
    if not sentences:
        return _FALLBACK_MSG

    # Short responses → read in full
    if len(sentences) <= _MAX_SPEAK_SENTENCES:
        return clean

    # Long responses → first N sentences + truncation notice
    truncated = " ".join(sentences[:_MAX_SPEAK_SENTENCES])
    if not truncated.endswith((".", "!", "?")):
        truncated += "."
    return truncated + _TRUNCATION_SUFFIX


def _download_file(
    url: str,
    dest: Path,
    progress: Callable[[float], None] | None = None,
) -> None:
    """Download a file with optional progress callback (0.0 → 1.0)."""
    import requests

    resp = requests.get(url, stream=True, timeout=300)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65_536):
            f.write(chunk)
            downloaded += len(chunk)
            if progress and total:
                progress(min(downloaded / total, 1.0))


def get_voice_catalog() -> dict[str, str]:
    """Return ``{voice_id: display_name}`` for all available voices."""
    return dict(VOICE_CATALOG)
