"""Video generation tool — generate videos via Google Veo / xAI.

The agent calls ``generate_video`` to create videos from text prompts, or
``animate_image`` to turn a still image into a short video clip.

Generated videos are saved to the per-thread media directory and rendered
inline in the desktop chat via an HTML5 video player.  Channel adapters
attach the saved MP4 as a file.

The user picks a provider+model combination in Settings → Models (e.g.
``google/veo-3.1-generate-preview`` or ``xai/grok-imagine-video``).
Only providers whose API key is configured appear in the dropdown.

Google Veo:
  - Uses the ``generate_videos`` long-running operation API.
  - Supports text-to-video and image-to-video (first frame).
  - Aspect ratios: 16:9 (default), 9:16.
  - Resolutions: 720p (default), 1080p (8s only), 4k (8s only).
  - Durations: 4, 6, or 8 seconds.
  - Natively generates audio.

xAI (grok-imagine-video):
  - Uses REST POST /v1/videos/generations + poll GET /v1/videos/{request_id}.
  - Supports text-to-video and image-to-video.
  - Aspect ratios: 1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3.
  - Resolutions: 480p (default), 720p.
  - Durations: 1–15 seconds.
  - URLs are ephemeral — must download immediately.
"""

from __future__ import annotations

import base64
import contextvars
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

logger = logging.getLogger(__name__)

# ── Timeouts & polling ───────────────────────────────────────────────────
_GOOGLE_POLL_INTERVAL = 10   # seconds
_XAI_POLL_INTERVAL = 5       # seconds
_POLL_TIMEOUT = 360           # 6 minutes hard ceiling

# ── Available video generation models ────────────────────────────────────

_GOOGLE_MODELS = [
    {"id": "veo-3.1-generate-preview", "label": "Veo 3.1"},
    {"id": "veo-3.1-fast-generate-preview", "label": "Veo 3.1 Fast"},
]

_XAI_MODELS = [
    {"id": "grok-imagine-video", "label": "Grok Imagine Video"},
]

VIDEO_GEN_MODELS = _GOOGLE_MODELS + _XAI_MODELS

_PROVIDER_MODELS = {
    "google": _GOOGLE_MODELS,
    "xai": _XAI_MODELS,
}

_PROVIDERS = {
    "google": {"key": "GOOGLE_API_KEY", "label": "Google", "emoji": "💎"},
    "xai":    {"key": "XAI_API_KEY",    "label": "xAI",    "emoji": "𝕏"},
}

DEFAULT_MODEL = "google/veo-3.1-generate-preview"

# ── Google constraints ───────────────────────────────────────────────────
_GOOGLE_ASPECT_RATIOS = {"16:9", "9:16"}
_GOOGLE_DURATIONS = {4, 6, 8}
_GOOGLE_RESOLUTIONS = {"720p", "1080p", "4k"}
_GOOGLE_HIGH_RES_DURATION = 8  # 1080p / 4k require 8s

# ── xAI constraints ─────────────────────────────────────────────────────
_XAI_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
_XAI_MIN_DURATION = 1
_XAI_MAX_DURATION = 15
_XAI_RESOLUTIONS = {"480p", "720p"}


# ── Parameter normalization ──────────────────────────────────────────────

def _normalize_google_params(
    duration: int, aspect_ratio: str, resolution: str,
) -> tuple[int, str, str]:
    """Snap values to Google-valid ranges and return (duration, aspect, res)."""
    # Duration: snap to nearest valid
    if duration <= 4:
        dur = 4
    elif duration <= 6:
        dur = 6
    else:
        dur = 8

    # Aspect ratio
    ar = aspect_ratio if aspect_ratio in _GOOGLE_ASPECT_RATIOS else "16:9"

    # Resolution
    res = resolution if resolution in _GOOGLE_RESOLUTIONS else "720p"
    if res in ("1080p", "4k") and dur != _GOOGLE_HIGH_RES_DURATION:
        dur = _GOOGLE_HIGH_RES_DURATION

    return dur, ar, res


def _normalize_xai_params(
    duration: int, aspect_ratio: str, resolution: str,
) -> tuple[int, str, str]:
    """Clamp values to xAI-valid ranges and return (duration, aspect, res)."""
    dur = max(_XAI_MIN_DURATION, min(_XAI_MAX_DURATION, duration))
    ar = aspect_ratio if aspect_ratio in _XAI_ASPECT_RATIOS else "16:9"
    res = resolution if resolution in _XAI_RESOLUTIONS else "480p"
    return dur, ar, res


# ── Side-channel for generated videos ────────────────────────────────────
# The streaming layer reads and clears this after generate/animate calls.
_last_generated_video: dict | None = None  # {path, filename, provider, model, duration, mode}
_video_output_dir_var: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "video_output_dir",
    default=None,
)
_video_output_filename_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "video_output_filename",
    default=None,
)


@contextmanager
def video_output_override(directory: str | Path, filename: str | None = None) -> Iterator[None]:
    """Temporarily save generated video output to a specific directory.

    Normal chat/tool calls keep using per-thread media. In-app flows such as
    Buddy Hatch do not always run inside an agent thread, so they need a narrow
    way to persist generated motion directly into their own asset directory.
    """

    dir_token = _video_output_dir_var.set(Path(directory).expanduser())
    name_token = _video_output_filename_var.set(filename)
    try:
        yield
    finally:
        _video_output_filename_var.reset(name_token)
        _video_output_dir_var.reset(dir_token)


def get_and_clear_last_video() -> dict | None:
    """Return and clear the pending generated video metadata, if any."""
    global _last_generated_video
    vid = _last_generated_video
    _last_generated_video = None
    return vid


# ── Provider resolution ──────────────────────────────────────────────────

def _parse_model_config(value: str) -> tuple[str, str]:
    """Parse a 'provider/model' config string → (provider, model_id)."""
    if "/" in value:
        provider, model_id = value.split("/", 1)
        return provider, model_id
    return "google", value


def get_available_video_models() -> dict[str, str]:
    """Return {config_value: display_label} for models whose provider key is set.

    Used by the Settings UI to populate the model dropdown.
    """
    try:
        from models import _cloud_model_cache, _sync_custom_model_cache
        from providers.media import media_model_options
        _sync_custom_model_cache()
        return media_model_options("video", _cloud_model_cache)
    except Exception:
        from api_keys import get_key

        opts: dict[str, str] = {}
        for prov_id, prov in _PROVIDERS.items():
            if not get_key(prov["key"]):
                continue
            for m in _PROVIDER_MODELS.get(prov_id, []):
                config_val = f"{prov_id}/{m['id']}"
                opts[config_val] = f"{prov['emoji']}  {m['label']}  ({prov['label']})"
        return opts


def _get_configured_selection() -> str:
    """Return the raw 'provider/model' string from tool config."""
    tool = registry.get_tool("video_gen")
    if tool:
        val = tool.get_config("model", DEFAULT_MODEL)
        if val:
            return val
    return DEFAULT_MODEL


def _get_configured_model() -> str:
    """Return just the model ID from the stored config."""
    _, model_id = _parse_model_config(_get_configured_selection())
    return model_id


def _get_google_client():
    """Return a google.genai.Client configured with the user's API key."""
    from api_keys import get_key
    api_key = get_key("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No Google API key configured. "
            "Please add your Google API key in Settings → Providers."
        )
    from google import genai
    return genai.Client(api_key=api_key)


def _get_xai_key() -> str:
    """Return the xAI API key or raise."""
    from api_keys import get_key
    api_key = get_key("XAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No xAI API key configured. "
            "Please add your xAI API key in Settings → Providers."
        )
    return api_key


# ── Image source resolution (reuses image_gen_tool patterns) ─────────────

def _resolve_image_source(image_source: str) -> bytes:
    """Resolve an image_source string to raw bytes.

    Priority:
      1. "last" → last generated image (from image_gen_tool cache)
      2. Key match in image_gen_tool._image_cache → pasted/attached image
      3. File path on disk / workspace-relative path
    """
    from tools.image_gen_tool import _image_cache

    # "last" — use the last generated image
    if image_source.strip().lower() == "last":
        last_bytes = _image_cache.get("__last_generated__")
        if last_bytes:
            return last_bytes
        raise ValueError(
            "No previously generated image available. "
            "Generate an image first, then use animate_image with image_source='last'."
        )

    # Check attachment cache (pasted images)
    if image_source in _image_cache:
        return _image_cache[image_source]

    # Partial filename match in cache
    for cached_name, cached_data in _image_cache.items():
        if cached_name != "__last_generated__" and image_source.lower() in cached_name.lower():
            return cached_data

    # File path on disk
    path = Path(image_source).expanduser()
    if path.exists() and path.is_file():
        return path.read_bytes()

    # Try workspace-relative
    tool = registry.get_tool("filesystem")
    if tool:
        ws_root = tool.get_config("workspace_root", "")
        if ws_root:
            ws_path = Path(ws_root) / image_source
            if ws_path.exists() and ws_path.is_file():
                return ws_path.read_bytes()

    raise ValueError(
        f"Could not find image '{image_source}'. "
        "Use 'last' for the last generated image, paste/attach an image, "
        "or provide a valid file path."
    )


def _detect_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes.  Defaults to image/png."""
    if data[:4] == b"\xff\xd8\xff\xe0" or data[:4] == b"\xff\xd8\xff\xe1":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


# ── Save video to thread media ───────────────────────────────────────────

def _save_video_to_disk(video_bytes: bytes, prefix: str = "vid") -> str | None:
    """Persist generated video to the per-thread media directory.

    Returns the absolute path as a string, or None if saving fails.
    """
    override_dir = _video_output_dir_var.get()
    if override_dir is not None:
        try:
            override_dir.mkdir(parents=True, exist_ok=True)
            filename = _video_output_filename_var.get() or f"{prefix}.mp4"
            if not filename.lower().endswith(".mp4"):
                filename = f"{filename}.mp4"
            saved_path = override_dir / filename
            saved_path.write_bytes(video_bytes)
            logger.info("Saved generated video to %s", saved_path)
            return str(saved_path)
        except Exception:
            logger.warning("Failed to save generated video to override directory", exc_info=True)
            return None

    try:
        from agent import _current_thread_id_var
        from threads import save_media_file, _next_media_filename

        thread_id = _current_thread_id_var.get() or ""
        if not thread_id:
            logger.debug("_save_video_to_disk: no thread_id in context")
            return None

        filename = _next_media_filename(thread_id, prefix, "mp4")
        saved_path = save_media_file(thread_id, filename, video_bytes)
        logger.info("Saved generated video to %s", saved_path)
        return str(saved_path)
    except Exception:
        logger.warning("Failed to save generated video to disk", exc_info=True)
        return None


# ── Google Veo generation ────────────────────────────────────────────────

def _generate_video_google(
    prompt: str,
    duration: int,
    aspect_ratio: str,
    resolution: str,
    image_bytes: bytes | None = None,
) -> str:
    """Generate a video via Google Veo.  Blocks until completion or timeout."""
    global _last_generated_video

    client = _get_google_client()
    model = _get_configured_model()
    dur, ar, res = _normalize_google_params(duration, aspect_ratio, resolution)

    from google.genai import types

    # Do not force person_generation here. Google's accepted values vary by
    # mode and region, and the default request shape is the most compatible.
    config = types.GenerateVideosConfig(
        aspect_ratio=ar,
        resolution=res,
        duration_seconds=dur,
    )

    logger.info(
        "generate_video (Google): model=%s, duration=%ds, aspect=%s, res=%s",
        model, dur, ar, res,
    )

    try:
        kwargs: dict = {"model": model, "prompt": prompt, "config": config}
        if image_bytes is not None:
            mime = _detect_mime(image_bytes)
            img = types.Image(image_bytes=image_bytes, mime_type=mime)
            kwargs["image"] = img

        operation = client.models.generate_videos(**kwargs)
    except Exception as e:
        logger.error("Video generation request failed: %s", e, exc_info=True)
        return f"Video generation failed: {e}"

    # Poll until done
    elapsed = 0
    while not operation.done:
        if elapsed >= _POLL_TIMEOUT:
            op_name = getattr(operation, "name", "unknown")
            return (
                f"Video generation timed out after {_POLL_TIMEOUT}s. "
                f"Operation: {op_name}. The video may still complete on the "
                "server — try again shortly."
            )
        time.sleep(_GOOGLE_POLL_INTERVAL)
        elapsed += _GOOGLE_POLL_INTERVAL
        try:
            operation = client.operations.get(operation)
        except Exception as e:
            logger.error("Polling failed: %s", e, exc_info=True)
            return f"Video generation polling failed: {e}"

    # Download the result
    try:
        generated_video = operation.response.generated_videos[0]
        # Download to a temporary buffer
        client.files.download(file=generated_video.video)

        # The SDK saves video data into the file object — read it
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
        generated_video.video.save(tmp_path)
        video_bytes = Path(tmp_path).read_bytes()
        os.unlink(tmp_path)
    except Exception as e:
        logger.error("Video download failed: %s", e, exc_info=True)
        return f"Video generated but download failed: {e}"

    if not video_bytes:
        return "Video generation returned no video data."

    saved = _save_video_to_disk(video_bytes)
    mode = "image-to-video" if image_bytes else "text-to-video"

    _last_generated_video = {
        "path": saved,
        "filename": Path(saved).name if saved else None,
        "provider": "Google",
        "model": model,
        "duration": dur,
        "mode": mode,
    }

    result = (
        f"Video generated successfully. Model: {model} | "
        f"Duration: {dur}s | Aspect ratio: {ar} | Resolution: {res} | "
        f"Mode: {mode} | Provider: Google"
    )
    if saved:
        result += f"\nSaved to: {saved}"
    return result


# ── xAI video generation ────────────────────────────────────────────────

def _generate_video_xai(
    prompt: str,
    duration: int,
    aspect_ratio: str,
    resolution: str,
    image_bytes: bytes | None = None,
) -> str:
    """Generate a video via xAI.  Blocks until completion or timeout."""
    global _last_generated_video

    api_key = _get_xai_key()
    model = _get_configured_model()
    dur, ar, res = _normalize_xai_params(duration, aspect_ratio, resolution)

    logger.info(
        "generate_video (xAI): model=%s, duration=%ds, aspect=%s, res=%s",
        model, dur, ar, res,
    )

    body: dict = {
        "model": model,
        "prompt": prompt,
        "duration": dur,
        "aspect_ratio": ar,
        "resolution": res,
    }

    if image_bytes is not None:
        mime = _detect_mime(image_bytes)
        b64_src = base64.b64encode(image_bytes).decode("ascii")
        body["image_url"] = f"data:{mime};base64,{b64_src}"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Step 1: Start generation
    try:
        resp = httpx.post(
            "https://api.x.ai/v1/videos/generations",
            headers=headers,
            json=body,
            timeout=30,
        )
        resp.raise_for_status()
        request_id = resp.json().get("request_id")
        if not request_id:
            return "xAI video generation returned no request_id."
    except Exception as e:
        logger.error("xAI video generation request failed: %s", e, exc_info=True)
        return f"Video generation failed: {e}"

    logger.info("xAI video generation started: request_id=%s", request_id)

    # Step 2: Poll for result
    elapsed = 0
    while True:
        if elapsed >= _POLL_TIMEOUT:
            return (
                f"Video generation timed out after {_POLL_TIMEOUT}s. "
                f"Request ID: {request_id}. The video may still be processing."
            )
        time.sleep(_XAI_POLL_INTERVAL)
        elapsed += _XAI_POLL_INTERVAL

        try:
            poll_resp = httpx.get(
                f"https://api.x.ai/v1/videos/{request_id}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=30,
            )
            poll_resp.raise_for_status()
            data = poll_resp.json()
        except Exception as e:
            logger.error("xAI polling failed: %s", e, exc_info=True)
            return f"Video generation polling failed: {e}"

        status = data.get("status", "")
        if status == "done":
            break
        if status == "failed":
            return f"Video generation failed on xAI side. Request ID: {request_id}"
        if status == "expired":
            return f"Video generation request expired. Request ID: {request_id}"
        # status == "pending" → keep polling

    # Step 3: Download video from ephemeral URL
    video_url = data.get("video", {}).get("url")
    if not video_url:
        return f"Video generation completed but no URL returned. Request ID: {request_id}"

    try:
        dl_resp = httpx.get(video_url, timeout=120, follow_redirects=True)
        dl_resp.raise_for_status()
        video_bytes = dl_resp.content
    except Exception as e:
        logger.error("xAI video download failed: %s", e, exc_info=True)
        return f"Video generated but download failed: {e}"

    if not video_bytes:
        return "Video download returned no data."

    saved = _save_video_to_disk(video_bytes)
    mode = "image-to-video" if image_bytes else "text-to-video"

    _last_generated_video = {
        "path": saved,
        "filename": Path(saved).name if saved else None,
        "provider": "xAI",
        "model": model,
        "duration": dur,
        "mode": mode,
    }

    result = (
        f"Video generated successfully. Model: {model} | "
        f"Duration: {dur}s | Aspect ratio: {ar} | Resolution: {res} | "
        f"Mode: {mode} | Provider: xAI"
    )
    if saved:
        result += f"\nSaved to: {saved}"
    return result


# ── Core generation functions (dispatchers) ──────────────────────────────

def _generate_video(
    prompt: str,
    duration_seconds: int = 8,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
) -> str:
    """Generate a video from a text prompt."""
    provider, _ = _parse_model_config(_get_configured_selection())

    if provider == "google":
        return _generate_video_google(prompt, duration_seconds, aspect_ratio, resolution)
    if provider == "xai":
        return _generate_video_xai(prompt, duration_seconds, aspect_ratio, resolution)

    return f"Unknown video generation provider '{provider}'. Please select a valid model in Settings."


def _animate_image(
    prompt: str,
    image_source: str = "last",
    duration_seconds: int = 8,
    aspect_ratio: str = "16:9",
    resolution: str = "720p",
) -> str:
    """Turn a still image into a short video clip."""
    try:
        image_bytes = _resolve_image_source(image_source)
    except ValueError as e:
        return str(e)

    provider, _ = _parse_model_config(_get_configured_selection())

    if provider == "google":
        return _generate_video_google(prompt, duration_seconds, aspect_ratio, resolution, image_bytes)
    if provider == "xai":
        return _generate_video_xai(prompt, duration_seconds, aspect_ratio, resolution, image_bytes)

    return f"Unknown video generation provider '{provider}'. Please select a valid model in Settings."


# ── Pydantic input schemas ───────────────────────────────────────────────

class _GenerateVideoInput(BaseModel):
    prompt: str = Field(
        description=(
            "A detailed description of the video to generate. Include "
            "subject, action, style, camera motion, and ambiance. "
            "Example: 'A cinematic drone shot of a sunset over mountains, "
            "warm orange tones, slow pan left'"
        )
    )
    duration_seconds: int = Field(
        default=8,
        description=(
            "Video length in seconds. Google supports 4, 6, or 8. "
            "xAI supports 1–15. Default: 8."
        ),
    )
    aspect_ratio: str = Field(
        default="16:9",
        description=(
            "Video aspect ratio. Google: '16:9' or '9:16'. "
            "xAI: '1:1', '16:9', '9:16', '4:3', '3:4', '3:2', '2:3'. "
            "Default: '16:9' (widescreen)."
        ),
    )
    resolution: str = Field(
        default="720p",
        description=(
            "Video resolution. Google: '720p', '1080p', '4k' "
            "(1080p/4k require 8s duration). "
            "xAI: '480p', '720p'. Default: '720p'."
        ),
    )


class _AnimateImageInput(BaseModel):
    prompt: str = Field(
        description=(
            "A description of how the image should be animated. "
            "Example: 'The cat slowly turns its head and blinks, "
            "soft ambient lighting, gentle camera zoom'"
        )
    )
    image_source: str = Field(
        default="last",
        description=(
            "Where to get the image to animate. Use 'last' for the last "
            "generated image (default). Use the filename for an attached/"
            "pasted image (e.g. 'photo.jpg'). Or use a file path on disk."
        ),
    )
    duration_seconds: int = Field(
        default=8,
        description="Video length in seconds. Default: 8.",
    )
    aspect_ratio: str = Field(
        default="16:9",
        description="Video aspect ratio. Default: '16:9'.",
    )
    resolution: str = Field(
        default="720p",
        description="Video resolution. Default: '720p'.",
    )


# ── Tool registration ───────────────────────────────────────────────────

class VideoGenTool(BaseTool):

    @property
    def name(self) -> str:
        return "video_gen"

    @property
    def display_name(self) -> str:
        return "🎬 Video Generation"

    @property
    def description(self) -> str:
        return (
            "Generate videos from text descriptions and animate still images. "
            "Creates short video clips using AI models (Google Veo or xAI "
            "Grok Imagine Video). Requires a Google or xAI API key."
        )

    @property
    def enabled_by_default(self) -> bool:
        from models import is_cloud_available
        return is_cloud_available()

    @property
    def config_schema(self) -> dict[str, dict]:
        # Model selector is rendered in the Models tab (settings.py)
        return {}

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(
                func=_generate_video,
                name="generate_video",
                description=(
                    "Generate a video from a text description. Use this "
                    "when the user asks you to create, produce, or generate "
                    "any kind of video, animation, clip, or motion content. "
                    "Provide a detailed prompt describing the desired video "
                    "including subject, action, style, and camera work."
                ),
                args_schema=_GenerateVideoInput,
            ),
            StructuredTool.from_function(
                func=_animate_image,
                name="animate_image",
                description=(
                    "Turn a still image into a short video clip. Use this "
                    "when the user wants to animate an existing image — "
                    "the last generated image, an attached/pasted image, "
                    "or a file on disk. Describe how the image should move."
                ),
                args_schema=_AnimateImageInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return _generate_video(prompt=query)


registry.register(VideoGenTool())
