"""Vision service — camera capture, screenshot, and provider-backed image analysis.

Provides ``VisionService`` which:
* captures a single frame from the default (or user-selected) webcam,
* captures a screenshot of the primary monitor,
* sends the image with a question to the selected Vision model,
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
import sys
import threading
from types import ModuleType
from typing import Any, Optional

from row_bot.data_paths import get_row_bot_data_dir

try:
    import ollama as _ollama_mod
except ImportError:
    _ollama_mod = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ── Platform-specific camera backend ─────────────────────────────────────────
if sys.platform == "win32":
    _CV_BACKEND = 700                     # cv2.CAP_DSHOW / DirectShow (Windows)
elif sys.platform == "darwin":
    _CV_BACKEND = 1200                    # cv2.CAP_AVFOUNDATION / AVFoundation (macOS)
else:
    _CV_BACKEND = 200                     # cv2.CAP_V4L2 / Video4Linux (Linux)

_cv2_mod: ModuleType | None = None
_cv2_error: BaseException | None = None
_mss_mod: ModuleType | None = None
_mss_error: BaseException | None = None


def _load_cv2() -> ModuleType | None:
    """Return OpenCV if importable; keep startup alive if native libs are missing."""
    global _cv2_mod, _cv2_error
    if _cv2_mod is not None:
        return _cv2_mod
    if _cv2_error is not None:
        return None
    try:
        import cv2 as loaded_cv2
    except BaseException as exc:  # OpenCV can raise OSError for missing native libs.
        _cv2_error = exc
        logger.warning("OpenCV unavailable; camera/screenshot capture disabled: %s", exc)
        return None
    _cv2_mod = loaded_cv2
    return _cv2_mod


def _load_mss() -> ModuleType | None:
    """Return mss if importable; keep startup alive if screen capture deps are missing."""
    global _mss_mod, _mss_error
    if _mss_mod is not None:
        return _mss_mod
    if _mss_error is not None:
        return None
    try:
        import mss as loaded_mss
    except BaseException as exc:
        _mss_error = exc
        logger.warning("MSS unavailable; screenshot capture disabled: %s", exc)
        return None
    _mss_mod = loaded_mss
    return _mss_mod


def native_backend_status() -> dict[str, str | bool]:
    """Return display-safe status for optional native capture backends."""
    cv2_available = _load_cv2() is not None
    mss_available = _load_mss() is not None
    return {
        "opencv_available": cv2_available,
        "opencv_error": "" if cv2_available else str(_cv2_error or "OpenCV is not available."),
        "mss_available": mss_available,
        "mss_error": "" if mss_available else str(_mss_error or "MSS is not available."),
    }

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


def _encoded_image_mime(image_bytes: bytes) -> str:
    """Return the data-URL MIME for supported encoded image bytes.

    Camera frames are normally JPEG while Computer Use captures are PNG.
    Provider requests must describe the bytes truthfully or the model can
    silently receive an undecodable image and hallucinate visual geometry.
    Unknown legacy inputs retain the historical JPEG default.
    """

    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if (
        len(image_bytes) >= 12
        and image_bytes.startswith(b"RIFF")
        and image_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"
    return "image/jpeg"

# ── Persistent settings ─────────────────────────────────────────────────────
_DATA_DIR = get_row_bot_data_dir()
_SETTINGS_PATH = _DATA_DIR / "vision_settings.json"


def _load_settings() -> dict:
    try:
        if _SETTINGS_PATH.exists():
            return json.loads(_SETTINGS_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_settings(settings: dict):
    from row_bot.docs_capture import is_docs_real_data_capture

    if is_docs_real_data_capture():
        logger.warning("Suppressed Vision-settings write during authorized real-data docs capture")
        return
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def vision_model_compatibility(model: str | None) -> dict[str, Any]:
    """Return a low-cost compatibility view for the configured Vision model.

    This intentionally reads only local, already-persisted metadata. It never
    probes endpoints, refreshes catalogs, or calls providers.
    """
    raw = str(model or "").strip()
    result: dict[str, Any] = {
        "model": raw,
        "provider_id": "",
        "model_id": raw,
        "usable": True,
        "explicit": False,
        "reason": "",
    }
    if not raw:
        return result
    try:
        from row_bot.providers.capabilities import snapshot_supports_surface
        from row_bot.providers.selection import parse_model_ref, model_ref

        parsed = parse_model_ref(raw)
        if parsed:
            provider_id, model_id = parsed
        else:
            provider_id, model_id = "", raw
        result["provider_id"] = provider_id
        result["model_id"] = model_id

        if provider_id.startswith("custom_openai_"):
            try:
                from row_bot.providers.custom import custom_endpoint_models, get_custom_endpoint

                endpoint = get_custom_endpoint(provider_id) or {}
                manual = endpoint.get("manual_capabilities")
                if isinstance(manual, dict) and manual.get("vision") is False:
                    result.update({
                        "usable": False,
                        "explicit": True,
                        "reason": "manual vision capability disabled",
                    })
                    return result
                for item in custom_endpoint_models(provider_id):
                    if str(item.get("model_id") or item.get("id") or "") != model_id:
                        continue
                    snapshot = item.get("capabilities_snapshot") if isinstance(item.get("capabilities_snapshot"), dict) else {}
                    if snapshot and not snapshot_supports_surface(snapshot, "vision"):
                        result.update({
                            "usable": False,
                            "explicit": True,
                            "reason": "capability metadata says this model is not compatible with vision",
                        })
                    return result
            except Exception:
                logger.debug("Could not inspect custom Vision model metadata", exc_info=True)

        if provider_id:
            try:
                from row_bot.providers.capability_resolution import resolve_capability_snapshot

                snapshot = resolve_capability_snapshot(provider_id, model_id)
                if snapshot:
                    if not snapshot_supports_surface(snapshot, "vision"):
                        result.update({
                            "usable": False,
                            "explicit": True,
                            "reason": "capability metadata says this model is not compatible with vision",
                        })
                    return result
            except Exception:
                logger.debug("Could not inspect provider Vision model metadata", exc_info=True)

            try:
                from row_bot.providers.config import load_provider_config

                ref = model_ref(provider_id, model_id)
                for choice in load_provider_config().get("quick_choices", []):
                    if not isinstance(choice, dict) or choice.get("id") != ref:
                        continue
                    inactive = choice.get("inactive_surfaces")
                    if isinstance(inactive, dict) and inactive.get("vision"):
                        result.update({
                            "usable": False,
                            "explicit": True,
                            "reason": str(inactive.get("vision") or "model is not compatible with vision"),
                        })
                        return result
                    snapshot = choice.get("capabilities_snapshot") if isinstance(choice.get("capabilities_snapshot"), dict) else {}
                    if snapshot and not snapshot_supports_surface(snapshot, "vision"):
                        result.update({
                            "usable": False,
                            "explicit": True,
                            "reason": "capability metadata says this model is not compatible with vision",
                        })
                    return result
            except Exception:
                logger.debug("Could not inspect Vision quick choice metadata", exc_info=True)
    except Exception:
        logger.debug("Vision compatibility check failed", exc_info=True)
    return result


def vision_provider_disclosure(model: str | None = None) -> dict[str, Any]:
    """Return local-only provider disclosure for screenshot-bearing features."""

    selected = str(model or _load_settings().get("model") or DEFAULT_VISION_MODEL)
    provider_id = _vision_provider_id(selected)
    is_cloud = provider_id not in {"", "local", "ollama"}
    return {
        "model": selected,
        "provider_id": provider_id or "ollama",
        "provider_label": _vision_provider_label(selected),
        "is_cloud": is_cloud,
    }


# ── Camera utilities ─────────────────────────────────────────────────────────

def _suppress_stderr():
    """Context manager that silences C-level stderr (e.g. OpenCV warnings).

    OpenCV prints camera-not-found messages via C++ directly to fd 2;
    Python-level logging cannot suppress them.  We temporarily redirect
    the raw file descriptor to ``/dev/null`` (or ``NUL`` on Windows).
    """
    import contextlib

    @contextlib.contextmanager
    def _redirect():
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            old_stderr = os.dup(2)
            os.dup2(devnull, 2)
            os.close(devnull)
            yield
        except OSError:
            yield  # if redirect fails, just run normally
        else:
            os.dup2(old_stderr, 2)
            os.close(old_stderr)

    return _redirect()


def _runtime_vision_model(model_name: str) -> str:
    """Return the provider runtime model id for local Vision calls."""
    try:
        from row_bot.providers.selection import model_id_from_choice_value

        return model_id_from_choice_value(model_name) or model_name
    except Exception:
        return model_name


def _vision_provider_id(model_name: str) -> str:
    try:
        from row_bot.providers.selection import parse_model_ref

        parsed = parse_model_ref(model_name)
        if parsed:
            return "ollama" if parsed[0] == "local" else parsed[0]
    except Exception:
        pass
    try:
        from row_bot.models import get_cloud_provider, is_cloud_model

        if is_cloud_model(model_name):
            return str(get_cloud_provider(model_name) or "cloud")
    except Exception:
        pass
    return ""


def _vision_provider_label(model_name: str) -> str:
    provider_id = _vision_provider_id(model_name)
    runtime_model = _runtime_vision_model(model_name)
    if provider_id in {"", "local", "ollama"}:
        return f"Ollama model {runtime_model}"
    try:
        from row_bot.providers.selection import provider_display_label

        provider_label = provider_display_label(provider_id)
    except Exception:
        provider_label = provider_id
    detail = ""
    if provider_id.startswith("custom_openai_"):
        try:
            from row_bot.providers.custom import get_custom_endpoint

            endpoint = get_custom_endpoint(provider_id) or {}
            base_url = str(endpoint.get("base_url") or "")
            if base_url:
                detail = f" at {base_url}"
        except Exception:
            detail = ""
        return f"{provider_label} ({provider_id}) model {runtime_model}{detail}"
    return f"{provider_label} model {runtime_model}{detail}"


def list_cameras(max_check: int = 5) -> list[int]:
    """Return indices of available camera devices (checks 0..max_check-1)."""
    cv2 = _load_cv2()
    if cv2 is None:
        return []
    available = []
    with _suppress_stderr():
        for idx in range(max_check):
            cap = cv2.VideoCapture(idx, _CV_BACKEND)
            if cap.isOpened():
                available.append(idx)
                cap.release()
    return available


def capture_frame(camera_index: int = 0) -> Optional[bytes]:
    """Capture a single JPEG frame from the given camera.

    Returns JPEG bytes or ``None`` if the camera is unavailable.
    """
    cv2 = _load_cv2()
    if cv2 is None:
        logger.warning("Camera capture unavailable because OpenCV could not be imported: %s", _cv2_error)
        return None
    cap = cv2.VideoCapture(camera_index, _CV_BACKEND)
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
        cv2 = _load_cv2()
        mss = _load_mss()
        if cv2 is None or mss is None:
            logger.warning("Screenshot capture unavailable: OpenCV=%s MSS=%s", cv2 is not None, mss is not None)
            return None
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

        with self._lock:
            try:
                compatibility = vision_model_compatibility(self._model)
                if compatibility.get("explicit") and not compatibility.get("usable"):
                    reason = str(compatibility.get("reason") or "not marked as image-capable")
                    return (
                        "The selected Vision model is no longer marked as image-capable. "
                        f"Choose a Vision-capable model in Settings -> Models. ({reason})"
                    )
                b64 = base64.b64encode(image_bytes).decode("ascii")
                provider_id = _vision_provider_id(self._model)
                if provider_id in {"", "local", "ollama"}:
                    return self._analyze_ollama_local(b64, question)
                return self._analyze_provider(
                    b64,
                    question,
                    mime_type=_encoded_image_mime(image_bytes),
                )
            except Exception as exc:
                logger.error("Vision model error: %s", exc)
                return f"Vision analysis failed: {exc}"

    def _analyze_provider(
        self,
        b64: str,
        question: str,
        *,
        mime_type: str = "image/jpeg",
    ) -> str:
        """Send image to the selected provider-backed vision model."""
        from row_bot.models import get_llm_for
        from langchain_core.messages import HumanMessage

        label = _vision_provider_label(self._model)
        try:
            msg = HumanMessage(content=[
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
            ])
            llm = get_llm_for(self._model)
            response = llm.invoke([msg])
            content = getattr(response, "content", "")
            if isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, str):
                        text_parts.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("text"), str):
                        text_parts.append(item["text"])
                content = "\n".join(text_parts)
            return str(content or "")
        except Exception as exc:
            logger.error("Vision provider model error for %s: %s", label, exc)
            return f"Vision analysis failed for {label}: {exc}"

    def _analyze_ollama_local(self, b64: str, question: str) -> str:
        """Send image to a local Ollama vision model."""
        if _ollama_mod is None:
            return "Ollama is not installed. Install it or switch to a provider-backed Vision model."
        from row_bot.models import _ollama_client
        client = _ollama_client()
        if client is None:
            return "Ollama is not installed. Install it or switch to a provider-backed Vision model."
        runtime_model = _runtime_vision_model(self._model)
        response = client.chat(
            model=runtime_model,
            messages=[{
                "role": "user",
                "content": question,
                "images": [b64],
            }],
            keep_alive="5m",
        )
        return response["message"]["content"]

    def _analyze_cloud(self, b64: str, question: str) -> str:
        """Compatibility wrapper for provider-backed vision models."""
        return self._analyze_provider(b64, question)

    def _analyze_local(self, b64: str, question: str) -> str:
        """Compatibility wrapper for the local Ollama vision path."""
        return self._analyze_ollama_local(b64, question)

    def capture_and_analyze(
        self,
        question: str,
        source: str = "camera",
        file_path: str = "",
    ) -> str:
        """Capture from the given source and analyze in one call.

        Parameters
        ----------
        question : str
            The user's visual question.
        source : str
            ``"camera"`` for webcam, ``"screen"`` for screenshot,
            or ``"file"`` to analyze an image file on disk.
        file_path : str
            Path to the image file when *source* is ``"file"``.
            Can be workspace-relative or absolute.
        """
        if source == "file":
            return self._analyze_from_file(file_path, question)
        if source == "screen":
            frame = self.screenshot()
            if frame is None:
                return "Failed to capture screenshot."
            question = f"This is a screenshot of the user's computer screen. {question}"
        else:
            frame = self.capture()
            if frame is None:
                return (
                    "Could not access the camera. Make sure a webcam is connected "
                    "and not in use by another application."
                )
            question = f"This is a live photo from the user's webcam. {question}"
        return self.analyze(frame, question)

    def _analyze_from_file(self, file_path: str, question: str) -> str:
        """Read an image file and analyze it."""
        resolved = self._resolve_image_path(file_path)
        if resolved is None:
            return f"Image file not found: {file_path}"
        try:
            data = resolved.read_bytes()
        except Exception as exc:
            return f"Failed to read image file '{file_path}': {exc}"
        if not data:
            return f"Image file is empty: {file_path}"
        self.last_capture = data
        return self.analyze(data, question)

    @staticmethod
    def _resolve_image_path(file_path: str) -> pathlib.Path | None:
        """Resolve a workspace-relative or absolute image path."""
        p = pathlib.Path(file_path)
        if p.is_absolute() and p.is_file():
            return p
        # Try workspace root from filesystem tool config
        try:
            from row_bot.tools import registry as _reg
            fs_tool = _reg.get_tool("filesystem")
            if fs_tool:
                ws_root = fs_tool.get_config("workspace_root", "")
                if ws_root:
                    candidate = pathlib.Path(ws_root) / file_path
                    if candidate.is_file():
                        return candidate.resolve()
        except Exception:
            pass
        # Try cwd
        cwd_candidate = pathlib.Path.cwd() / file_path
        if cwd_candidate.is_file():
            return cwd_candidate.resolve()
        return None
