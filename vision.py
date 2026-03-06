"""Vision service — camera capture, screenshot, and image analysis via Ollama vision models.

Provides ``VisionService`` which:
* captures a single frame from the default (or user-selected) webcam,
* captures a screenshot of the primary monitor,
* sends the image with a question to an Ollama vision model,
* returns the model's text description / OCR / analysis.

The vision model runs as a lightweight one-shot call — the main agent
stays text-only and calls the ``analyze_image`` tool when it needs to
interpret something visual.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import threading
from typing import Optional

import cv2
import mss
import ollama

logger = logging.getLogger(__name__)

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_VISION_MODEL = "gemma3:4b"

POPULAR_VISION_MODELS = [
    "moondream:latest",
    "gemma3:4b",
    "gemma3:12b",
    "llava:7b",
    "llava:13b",
    "llava-llama3:8b",
    "llava-phi3:3.8b",
]

# ── Persistent settings ─────────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_SETTINGS_PATH = _DATA_DIR / "vision_settings.json"


def _load_settings() -> dict:
    try:
        if _SETTINGS_PATH.exists():
            return json.loads(_SETTINGS_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_settings(settings: dict):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


# ── Camera utilities ─────────────────────────────────────────────────────────

def list_cameras(max_check: int = 5) -> list[int]:
    """Return indices of available camera devices (checks 0..max_check-1)."""
    available = []
    for idx in range(max_check):
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if cap.isOpened():
            available.append(idx)
            cap.release()
    return available


def capture_frame(camera_index: int = 0) -> Optional[bytes]:
    """Capture a single JPEG frame from the given camera.

    Returns JPEG bytes or ``None`` if the camera is unavailable.
    """
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        logger.warning("Camera %d not available", camera_index)
        return None
    try:
        # Grab a few frames to let auto-exposure settle
        for _ in range(5):
            cap.read()
        ret, frame = cap.read()
        if not ret or frame is None:
            logger.warning("Failed to read frame from camera %d", camera_index)
            return None
        # Encode as JPEG
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None
        return buf.tobytes()
    finally:
        cap.release()


# ── Screenshot utilities ─────────────────────────────────────────────────────

def capture_screenshot() -> Optional[bytes]:
    """Capture the primary monitor as JPEG bytes."""
    try:
        with mss.mss() as sct:
            # monitor 1 = primary display (0 = all monitors combined)
            shot = sct.grab(sct.monitors[1])
            # mss returns BGRA; convert to RGB via numpy then encode as JPEG
            import numpy as np
            img = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(
                shot.height, shot.width, 3
            )
            # cv2 expects BGR
            img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            ok, buf = cv2.imencode(".jpg", img_bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                return None
            return buf.tobytes()
    except Exception as exc:
        logger.error("Screenshot capture failed: %s", exc)
        return None


# ═════════════════════════════════════════════════════════════════════════════
# VisionService
# ═════════════════════════════════════════════════════════════════════════════

class VisionService:
    """Manages vision model settings and provides image analysis."""

    def __init__(self):
        saved = _load_settings()
        self._model: str = saved.get("model", DEFAULT_VISION_MODEL)
        self._camera_index: int = saved.get("camera_index", 0)
        self._enabled: bool = saved.get("enabled", True)
        self._lock = threading.Lock()
        self.last_capture: Optional[bytes] = None  # most recent JPEG frame

    # ── Properties ───────────────────────────────────────────────────────
    @property
    def model(self) -> str:
        return self._model

    @model.setter
    def model(self, value: str):
        self._model = value
        self._persist()

    @property
    def camera_index(self) -> int:
        return self._camera_index

    @camera_index.setter
    def camera_index(self, value: int):
        self._camera_index = value
        self._persist()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value
        self._persist()

    def _persist(self):
        _save_settings({
            "model": self._model,
            "camera_index": self._camera_index,
            "enabled": self._enabled,
        })

    # ── Core methods ─────────────────────────────────────────────────────

    def capture(self) -> Optional[bytes]:
        """Capture a JPEG frame from the configured camera."""
        frame = capture_frame(self._camera_index)
        if frame is not None:
            self.last_capture = frame
        return frame

    def screenshot(self) -> Optional[bytes]:
        """Capture a screenshot of the primary monitor."""
        shot = capture_screenshot()
        if shot is not None:
            self.last_capture = shot
        return shot

    def analyze(self, image_bytes: bytes, question: str) -> str:
        """Send an image + question to the vision model and return the
        text response.

        Parameters
        ----------
        image_bytes : bytes
            JPEG (or PNG) encoded image.
        question : str
            The user's question about the image.

        Returns
        -------
        str
            The vision model's response text.
        """
        if not self._enabled:
            return "Vision is disabled. Enable it in Settings → Models."

        b64 = base64.b64encode(image_bytes).decode("ascii")

        with self._lock:
            try:
                response = ollama.chat(
                    model=self._model,
                    messages=[{
                        "role": "user",
                        "content": question,
                        "images": [b64],
                    }],
                    keep_alive="5m",   # keep loaded for quick follow-ups
                )
                return response["message"]["content"]
            except Exception as exc:
                logger.error("Vision model error: %s", exc)
                return f"Vision analysis failed: {exc}"

    def capture_and_analyze(self, question: str, source: str = "camera") -> str:
        """Capture from the given source and analyze in one call.

        Parameters
        ----------
        question : str
            The user's visual question.
        source : str
            ``"camera"`` for webcam or ``"screen"`` for screenshot.
        """
        if source == "screen":
            frame = self.screenshot()
            if frame is None:
                return "Failed to capture screenshot."
        else:
            frame = self.capture()
            if frame is None:
                return (
                    "Could not access the camera. Make sure a webcam is connected "
                    "and not in use by another application."
                )
        return self.analyze(frame, question)
