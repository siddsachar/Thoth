"""tts.py – Text-to-Speech service using Piper TTS (local, offline).

Piper is a fast, local neural text-to-speech system.  This module manages
the Piper binary and voice models, preprocesses agent responses (stripping
markdown, truncating long text), and plays audio through the default output
device using sounddevice.

Everything runs locally — no audio is sent to the cloud.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from queue import Queue, Empty
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
_THOTH_DIR = Path.home() / ".thoth"
_PIPER_DIR = _THOTH_DIR / "piper"
_VOICES_DIR = _PIPER_DIR / "voices"
# After extracting the release zip, contents are inside a 'piper' subfolder
_PIPER_EXE = _PIPER_DIR / "piper" / "piper.exe"
_SETTINGS_PATH = _THOTH_DIR / "tts_settings.json"

# ── Download URLs ────────────────────────────────────────────────────────────
_PIPER_RELEASE = "2023.11.14-2"
_PIPER_DOWNLOAD_URL = (
    f"https://github.com/rhasspy/piper/releases/download/"
    f"{_PIPER_RELEASE}/piper_windows_amd64.zip"
)
_VOICES_BASE_URL = (
    "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
)

# ── Voice catalog (id → display name) ───────────────────────────────────────
VOICE_CATALOG: dict[str, str] = {
    "en_US-lessac-medium":     "Lessac (US English, Male)",
    "en_US-amy-medium":        "Amy (US English, Female)",
    "en_US-ryan-medium":       "Ryan (US English, Male)",
    "en_US-hfc_female-medium": "HFC (US English, Female)",
    "en_US-hfc_male-medium":   "HFC (US English, Male)",
    "en_GB-alan-medium":       "Alan (British English, Male)",
    "en_GB-alba-medium":       "Alba (British English, Female)",
    "en_GB-cori-medium":       "Cori (British English, Female)",
}
_DEFAULT_VOICE = "en_US-lessac-medium"

# ── Markdown / noise stripping patterns ──────────────────────────────────────
# Order matters: links/code first, then blocks, then line-level (bullets
# BEFORE emphasis so `* ` bullets aren't eaten by the *italic* regex),
# then emphasis (triple before double before single), then cleanup.
_MD_STRIP: list[tuple[re.Pattern, str]] = [
    # ── Inline formatting (keep inner text) ──────────────────────────
    (re.compile(r"`([^`]+)`"),                         r"\1"),    # inline code → text
    (re.compile(r"\[([^\]]+)\]\([^\)]*\)"),            r"\1"),    # [text](url) → text

    # ── Blocks (remove entirely) ─────────────────────────────────────
    (re.compile(r"```[\s\S]*?```"),                   ""),       # fenced code blocks
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
    """Text-to-speech using Piper TTS (local, offline).

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

        # Persisted settings
        self._voice: str = _DEFAULT_VOICE
        self._speed: float = 1.0
        self._enabled: bool = False
        self._auto_speak: bool = True
        self._load_settings()

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

    def is_piper_installed(self) -> bool:
        """True if the Piper binary is available."""
        return _PIPER_EXE.exists()

    def is_voice_installed(self, voice_id: str | None = None) -> bool:
        """True if the specified (or current) voice model is downloaded."""
        vid = voice_id or self._voice
        return (_VOICES_DIR / f"{vid}.onnx").exists()

    def get_installed_voices(self) -> list[str]:
        """Return voice IDs that are downloaded locally."""
        if not _VOICES_DIR.exists():
            return []
        return sorted(
            p.stem
            for p in _VOICES_DIR.glob("*.onnx")
            if not p.name.endswith(".onnx.json")
        )

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
        # Kill audio immediately on Windows
        if os.name == "nt":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
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
        if not self.is_piper_installed() or not self.is_voice_installed():
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
        import tempfile
        counter = 0
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

            # Generate and play this sentence
            model_path = _VOICES_DIR / f"{self._voice}.onnx"
            if not model_path.exists():
                continue

            try:
                length_scale = 1.0 / self._speed
                wav_path = os.path.join(
                    tempfile.gettempdir(), f"_thoth_tts_s{counter}.wav"
                )
                counter += 1

                cmd = [
                    str(_PIPER_EXE),
                    "--model", str(model_path),
                    "--output_file", wav_path,
                    "--length-scale", f"{length_scale:.2f}",
                ]
                kwargs: dict = {}
                if os.name == "nt":
                    kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **kwargs,
                )
                logger.debug("Piper stream TTS subprocess started (PID %s)", proc.pid)
                proc.communicate(input=sentence.encode("utf-8"), timeout=30)

                if self._stop_event.is_set():
                    break

                if os.name == "nt":
                    import winsound
                    winsound.PlaySound(wav_path, winsound.SND_FILENAME)
                else:
                    import sounddevice as sd
                    import wave
                    with wave.open(wav_path, "rb") as wf:
                        sr = wf.getframerate()
                        frames = wf.readframes(wf.getnframes())
                    audio = (
                        np.frombuffer(frames, dtype=np.int16)
                        .astype(np.float32) / 32768.0
                    )
                    sd.play(audio, samplerate=sr)
                    sd.wait()

            except subprocess.TimeoutExpired:
                logger.warning("Piper stream TTS timed out, killing process")
                if proc:
                    proc.kill()
            except Exception:
                logger.debug("TTS stream playback error", exc_info=True)
            finally:
                try:
                    if os.path.exists(wav_path):
                        os.remove(wav_path)
                except Exception:
                    pass

        # Only unmute on NATURAL end-of-stream (not forced stop).
        # Double-check _stop_event in case stop() was called during
        # the sleep window.
        if natural_end and not self._stop_event.is_set():
            time.sleep(0.5)
            if not self._stop_event.is_set():
                self._unmute_mic()

    # ── Download helpers ─────────────────────────────────────────────────

    def download_piper(
        self, progress: Callable[[float], None] | None = None
    ) -> None:
        """Download and extract the Piper TTS binary."""
        _PIPER_DIR.mkdir(parents=True, exist_ok=True)
        zip_path = _PIPER_DIR / "piper_windows_amd64.zip"
        logger.info("Downloading Piper TTS from %s", _PIPER_DOWNLOAD_URL)
        _download_file(_PIPER_DOWNLOAD_URL, zip_path, progress)

        # Extract (creates _PIPER_DIR/piper/ with piper.exe + libs)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(_PIPER_DIR)
        zip_path.unlink(missing_ok=True)

    def download_voice(
        self,
        voice_id: str | None = None,
        progress: Callable[[float], None] | None = None,
    ) -> None:
        """Download a voice model from Hugging Face."""
        vid = voice_id or self._voice
        logger.info("Downloading voice model: %s", vid)
        _VOICES_DIR.mkdir(parents=True, exist_ok=True)
        onnx_url, json_url = _voice_urls(vid)

        # ONNX model (large, ~20-75 MB)
        _download_file(onnx_url, _VOICES_DIR / f"{vid}.onnx", progress)
        # Config JSON (tiny)
        _download_file(json_url, _VOICES_DIR / f"{vid}.onnx.json", None)

    # ── Internal ─────────────────────────────────────────────────────────

    def _speak_internal(self, text: str) -> None:
        """Prepare text and start background playback."""
        if not self.is_piper_installed() or not self.is_voice_installed():
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
        """Generate audio with Piper and play it via the OS audio subsystem."""
        model_path = _VOICES_DIR / f"{self._voice}.onnx"
        if not model_path.exists():
            return

        proc = None
        try:
            # length_scale: <1.0 = faster, >1.0 = slower
            length_scale = 1.0 / self._speed

            # Let Piper write a proper WAV to a temp file, then play
            # it with the platform's native player.  This completely
            # avoids sounddevice/PortAudio resampling artefacts.
            import tempfile
            wav_path = os.path.join(
                tempfile.gettempdir(), "_thoth_tts.wav"
            )

            cmd = [
                str(_PIPER_EXE),
                "--model", str(model_path),
                "--output_file", wav_path,
                "--length-scale", f"{length_scale:.2f}",
                "--sentence_silence", "0.5",
            ]
            kwargs: dict = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **kwargs,
            )
            logger.debug("Piper TTS subprocess started (PID %s)", proc.pid)
            proc.communicate(input=text.encode("utf-8"), timeout=60)

            if self._stop_event.is_set():
                return

            # Play the WAV using the platform's native audio
            if os.name == "nt":
                import winsound
                # SND_FILENAME | SND_NODEFAULT
                # winsound.PlaySound is blocking, which is fine since
                # we're already in a background thread.
                winsound.PlaySound(wav_path, winsound.SND_FILENAME)
            else:
                # Fallback for non-Windows: use sounddevice
                import sounddevice as sd
                import wave
                with wave.open(wav_path, "rb") as wf:
                    sr = wf.getframerate()
                    frames = wf.readframes(wf.getnframes())
                audio = (
                    np.frombuffer(frames, dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
                sd.play(audio, samplerate=sr)
                sd.wait()

        except subprocess.TimeoutExpired:
            logger.warning("Piper TTS timed out, killing process")
            if proc:
                proc.kill()
        except Exception:
            logger.warning("TTS playback error", exc_info=True)
        finally:
            # Do NOT unmute here — only the streaming worker should
            # trigger follow-up.  speak_now() is used for short phrases
            # like "Ok. Working." while the agent is still processing.
            # Clean up temp WAV
            try:
                wav_p = os.path.join(
                    __import__("tempfile").gettempdir(), "_thoth_tts.wav"
                )
                if os.path.exists(wav_p):
                    os.remove(wav_p)
            except Exception:
                pass

    def _load_settings(self) -> None:
        try:
            if _SETTINGS_PATH.exists():
                data = json.loads(_SETTINGS_PATH.read_text())
                self._voice = data.get("voice", _DEFAULT_VOICE)
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

def _prepare_text(text: str, *, truncate: bool = True) -> str:
    """Strip markdown formatting and optionally truncate for speech.

    When *truncate* is ``False`` the sentence-cap / truncation-suffix
    logic is skipped — useful for streaming TTS where the caller
    manages the sentence budget."""
    clean = text
    for pattern, repl in _MD_STRIP:
        clean = pattern.sub(repl, clean)

    # Split into lines, ensure each ends with punctuation so Piper
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


def _voice_urls(voice_id: str) -> tuple[str, str]:
    """Return ``(onnx_url, json_url)`` for a voice model on Hugging Face."""
    parts = voice_id.split("-")
    locale = parts[0]                    # e.g. en_US
    quality = parts[-1]                  # e.g. medium
    speaker = "-".join(parts[1:-1])      # e.g. lessac  /  hfc_female
    lang = locale.split("_")[0]          # e.g. en

    base = f"{_VOICES_BASE_URL}/{lang}/{locale}/{speaker}/{quality}"
    return (
        f"{base}/{voice_id}.onnx",
        f"{base}/{voice_id}.onnx.json",
    )


def _download_file(
    url: str,
    dest: Path,
    progress: Callable[[float], None] | None = None,
) -> None:
    """Download a file with optional progress callback (0.0 → 1.0)."""
    import requests

    resp = requests.get(url, stream=True, timeout=120)
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
