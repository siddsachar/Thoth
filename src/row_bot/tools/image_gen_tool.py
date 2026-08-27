"""Image generation tool — generate and edit images via OpenAI / Google.

The agent calls ``generate_image`` to create images from text prompts, or
``edit_image`` to modify an existing image (pasted, last generated, or from
a file path).  Generated images are rendered inline in the chat via the
``captured_images`` pipeline (same as vision / browser screenshots).

The user picks a provider+model combination in Settings → Models (e.g.
``openai/gpt-image-1.5`` or ``google/gemini-3.1-flash-image-preview``).
Only providers whose API key is configured appear in the dropdown.

Google providers:
  - **Nano Banana** models use the ``generate_content`` API (same endpoint as
    chat) with ``response_modalities=['IMAGE']``.  They support both
    generation and editing.
  - **Imagen 4** models use the dedicated ``generate_images`` API.  They
    support generation only (no editing).
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.tools.base import BaseTool
from row_bot.tools import registry

logger = logging.getLogger(__name__)

# ── Available image generation models ────────────────────────────────────
# Per-provider model registries.
_OPENAI_MODELS = [
    {"id": "gpt-image-1.5", "label": "GPT Image 1.5"},
    {"id": "gpt-image-1", "label": "GPT Image 1"},
    {"id": "gpt-image-1-mini", "label": "GPT Image 1 Mini"},
]

_GOOGLE_NANO_BANANA_MODELS = [
    {"id": "gemini-3.1-flash-image-preview", "label": "Nano Banana 2"},
    {"id": "gemini-3-pro-image-preview", "label": "Nano Banana Pro"},
    {"id": "gemini-2.5-flash-image", "label": "Nano Banana"},
]

_GOOGLE_IMAGEN_MODELS = [
    {"id": "imagen-4.0-generate-001", "label": "Imagen 4"},
    {"id": "imagen-4.0-fast-generate-001", "label": "Imagen 4 Fast"},
    {"id": "imagen-4.0-ultra-generate-001", "label": "Imagen 4 Ultra"},
]

_GOOGLE_MODELS = _GOOGLE_NANO_BANANA_MODELS + _GOOGLE_IMAGEN_MODELS

_XAI_MODELS = [
    {"id": "grok-imagine-image", "label": "Grok Imagine"},
    {"id": "grok-imagine-image-quality", "label": "Grok Imagine Quality"},
]

# Flat list for backward compat (used by existing tests).
IMAGE_GEN_MODELS = _OPENAI_MODELS + _GOOGLE_MODELS + _XAI_MODELS

# Sets for quick type detection
_IMAGEN_MODEL_IDS = {m["id"] for m in _GOOGLE_IMAGEN_MODELS}
_NANO_BANANA_MODEL_IDS = {m["id"] for m in _GOOGLE_NANO_BANANA_MODELS}

# Per-provider model lists (keyed same as _PROVIDERS)
_PROVIDER_MODELS = {
    "openai": _OPENAI_MODELS,
    "google": _GOOGLE_MODELS,
    "xai": _XAI_MODELS,
}

# Provider definitions — only providers with an images API
_PROVIDERS = {
    "openai": {"key": "OPENAI_API_KEY", "label": "OpenAI", "emoji": "⬡"},
    "google": {"key": "GOOGLE_API_KEY", "label": "Google", "emoji": "💎"},
    "xai": {"key": "XAI_API_KEY", "label": "xAI", "emoji": "𝕏"},
}

DEFAULT_MODEL = "openai/gpt-image-1.5"

IMAGE_SIZES = ["auto", "1024x1024", "1536x1024", "1024x1536"]
IMAGE_QUALITIES = ["auto", "low", "medium", "high"]

# ── Size / aspect-ratio mapping ──────────────────────────────────────────
_OPENAI_SIZE_TO_ASPECT: dict[str, str] = {
    "1024x1024": "1:1",
    "1536x1024": "3:2",
    "1024x1536": "2:3",
}

_OPENAI_SIZE_TO_GOOGLE = {
    "1024x1024": ("1:1", "1K"),
    "1536x1024": ("3:2", "1K"),
    "1024x1536": ("2:3", "1K"),
}

_QUALITY_TO_RESOLUTION: dict[str, str | None] = {
    "low": "512",
    "medium": "1K",
    "high": "2K",
    "auto": None,   # let model default
}

# Imagen 4 aspect ratios (subset supported by the API)
_IMAGEN_ASPECT_RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9"}


def _map_google_params(size: str, quality: str) -> tuple[str, str | None]:
    """Convert OpenAI-style size/quality to Google (aspect_ratio, resolution).

    Returns ``("1:1", "1K")`` by default.  Quality can override the
    resolution tier (e.g. ``quality="high"`` → ``"2K"``).
    """
    if size != "auto" and size in _OPENAI_SIZE_TO_GOOGLE:
        aspect_ratio, base_res = _OPENAI_SIZE_TO_GOOGLE[size]
    else:
        aspect_ratio, base_res = "1:1", "1K"

    # Quality can override the resolution tier
    if quality != "auto" and quality in _QUALITY_TO_RESOLUTION:
        override = _QUALITY_TO_RESOLUTION[quality]
        if override is not None:
            base_res = override

    return aspect_ratio, base_res

# ── Side-channel for generated images ────────────────────────────────────
# The streaming layer reads and clears this after generate/edit calls,
# same pattern as filesystem_tool._last_displayed_image.
_last_generated_image: str | None = None  # base64-encoded image data

# ── Attachment cache for pasted/attached images ──────────────────────────
# Populated by ui/streaming.py before agent invocation.
_image_cache: dict[str, bytes] = {}  # filename → raw bytes
_image_cache_thread_id: str | None = None  # thread that owns __last_generated__


def get_and_clear_last_image() -> str | None:
    """Return and clear the pending generated image, if any."""
    global _last_generated_image
    img = _last_generated_image
    _last_generated_image = None
    return img


def _save_image_to_disk(b64_str: str, prefix: str = "gen") -> str | None:
    """Persist generated/edited image to the per-thread media directory.

    Returns the absolute path as a string, or None if saving fails.
    Uses the media pipeline from threads.py (``save_media_file`` +
    ``_next_media_filename``) so images survive reload and can be
    referenced by tools like ``send_telegram_photo``.
    """
    try:
        from row_bot.agent import _current_thread_id_var
        from row_bot.threads import save_media_file, _next_media_filename

        thread_id = _current_thread_id_var.get() or ""
        if not thread_id:
            logger.debug("_save_image_to_disk: no thread_id in context")
            return None

        filename = _next_media_filename(thread_id, prefix, "png")
        img_bytes = base64.b64decode(b64_str)
        saved_path = save_media_file(thread_id, filename, img_bytes)
        logger.info("Saved generated image to %s", saved_path)
        return str(saved_path)
    except Exception:
        logger.warning("Failed to save generated image to disk", exc_info=True)
        return None


# ── Provider resolution ──────────────────────────────────────────────────

def _parse_model_config(value: str) -> tuple[str, str]:
    """Parse a 'provider/model' config string → (provider, model_id).

    Falls back gracefully for bare model names (legacy configs).
    """
    if "/" in value:
        provider, model_id = value.split("/", 1)
        return provider, model_id
    # Legacy bare model name — default to openai
    return "openai", value


def get_available_image_models() -> dict[str, str]:
    """Return {config_value: display_label} for models whose provider key is set.

    Used by the Settings UI to populate the model dropdown.
    """
    try:
        from row_bot.models import _cloud_model_cache, _sync_custom_model_cache
        from row_bot.providers.media import media_model_options
        _sync_custom_model_cache()
        return media_model_options("image", _cloud_model_cache)
    except Exception:
        from row_bot.api_keys import get_key

        opts: dict[str, str] = {}
        for prov_id, prov in _PROVIDERS.items():
            if not get_key(prov["key"]):
                continue
            for m in _PROVIDER_MODELS.get(prov_id, []):
                config_val = f"{prov_id}/{m['id']}"
                opts[config_val] = f"{prov['emoji']}  {m['label']}  ({prov['label']})"
        return opts


def _get_client() -> tuple:
    """Return (client, provider_label, provider_id) based on the user's model selection.

    For OpenAI: returns an ``openai.OpenAI`` client.
    For Google: returns a ``google.genai.Client``.
    """
    from row_bot.api_keys import get_key

    provider, _ = _parse_model_config(_get_configured_selection())
    if provider in {"xai", "xai_oauth"}:
        from row_bot.providers.xai_media import xai_media_auth_context

        ctx = xai_media_auth_context(provider)
        return None, ctx.label, provider

    prov_info = _PROVIDERS.get(provider)
    if not prov_info:
        raise RuntimeError(
            f"Unknown image generation provider '{provider}'. "
            "Please select a valid model in Settings → Models."
        )

    api_key = get_key(prov_info["key"])
    if not api_key:
        raise RuntimeError(
            f"No API key for {prov_info['label']}. "
            f"Please add your {prov_info['label']} API key in Settings → Providers."
        )

    if provider == "google":
        from google import genai
        return genai.Client(api_key=api_key), prov_info["label"], provider

    if provider == "xai":
        import openai
        return openai.OpenAI(api_key=api_key, base_url="https://api.x.ai/v1"), prov_info["label"], provider

    import openai
    return openai.OpenAI(api_key=api_key), prov_info["label"], provider


def _get_configured_selection() -> str:
    """Return the raw 'provider/model' string from tool config."""
    val = registry.get_tool_config("image_gen", "model", None)
    if val:
        return val
    tool = registry.get_tool("image_gen")
    if tool:
        val = tool.get_config("model", DEFAULT_MODEL)
        if val:
            return val
    return DEFAULT_MODEL


def _get_configured_model() -> str:
    """Return just the model ID (e.g. 'gpt-image-1.5') from the stored config."""
    _, model_id = _parse_model_config(_get_configured_selection())
    return model_id


# ── Image resolution helpers ─────────────────────────────────────────────

def _detect_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes.  Defaults to image/png."""
    if data[:4] == b"\xff\xd8\xff\xe0" or data[:4] == b"\xff\xd8\xff\xe1":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


@dataclass(frozen=True)
class _XAIImageRequestPlan:
    quality: str | None = None
    resolution: str | None = None
    note: str = ""


_XAI_QUALITY_RANK = {"low": 0, "medium": 1, "high": 2}
_XAI_RESOLUTION_RANK = {"1k": 0, "2k": 1}


def _plan_xai_image_request(
    provider_id: str,
    model: str,
    requested_quality: str,
) -> _XAIImageRequestPlan:
    from row_bot.providers.capability_resolution import resolve_capability_snapshot

    requested = str(requested_quality or "auto").strip().lower()
    snapshot = resolve_capability_snapshot(
        provider_id,
        model,
        include_static_fallback=False,
    )
    generation_parameters = snapshot.get("generation_parameters")
    selected: tuple[str, str] | None = None
    if requested != "auto" and isinstance(generation_parameters, Mapping):
        options = generation_parameters.get("options")
        defaults = generation_parameters.get("defaults")
        raw_combinations = generation_parameters.get("valid_combinations")
        if isinstance(options, Mapping) and isinstance(defaults, Mapping) and isinstance(raw_combinations, list):
            qualities = _xai_parameter_options(options.get("quality"))
            resolutions = _xai_parameter_options(options.get("resolution"))
            combinations: list[tuple[str, str]] = []
            for combination in raw_combinations:
                if not isinstance(combination, Mapping):
                    continue
                quality = str(combination.get("quality") or "").strip().lower()
                resolution = str(combination.get("resolution") or "").strip().lower()
                pair = (quality, resolution)
                if quality in qualities and resolution in resolutions and pair not in combinations:
                    combinations.append(pair)
            default_pair = (
                str(defaults.get("quality") or "").strip().lower(),
                str(defaults.get("resolution") or "").strip().lower(),
            )
            if default_pair in combinations:
                if requested == "high":
                    ranked = [
                        pair for pair in combinations
                        if pair[0] in _XAI_QUALITY_RANK and pair[1] in _XAI_RESOLUTION_RANK
                    ]
                    if ranked:
                        selected = max(
                            ranked,
                            key=lambda pair: (_XAI_QUALITY_RANK[pair[0]], _XAI_RESOLUTION_RANK[pair[1]]),
                        )
                else:
                    matching = [
                        pair for pair in combinations
                        if pair[0] == requested and pair[1] in _XAI_RESOLUTION_RANK
                    ]
                    if matching:
                        selected = next(
                            (pair for pair in matching if pair[1] == default_pair[1]),
                            min(matching, key=lambda pair: _XAI_RESOLUTION_RANK[pair[1]]),
                        )

    if selected:
        note = ""
        if selected[0] != requested:
            note = (
                f"Requested quality {requested}; used xAI's highest supported tier "
                f"{selected[0]}/{selected[1]}."
            )
        plan = _XAIImageRequestPlan(quality=selected[0], resolution=selected[1], note=note)
        effective = f"{selected[0]}/{selected[1]}"
    else:
        note = ""
        if requested != "auto":
            note = (
                f"Requested quality {requested}; used xAI provider defaults because the selected model "
                "did not publish a complete matching tier."
            )
        plan = _XAIImageRequestPlan(note=note)
        effective = "provider defaults"
    logger.info(
        "xAI image parameters: provider=%s model=%s requested_quality=%s effective=%s",
        provider_id,
        model,
        requested,
        effective,
    )
    return plan


def _xai_parameter_options(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {str(item).strip().lower() for item in value if str(item).strip()}


def _resolve_image_source(image_source: str) -> bytes:
    """Resolve an image_source string to raw bytes.

    Priority:
      1. "last" → last generated image (from _last_generated_image backup)
      2. Key match in _image_cache → pasted/attached image
      3. File path on disk
    """
    # "last" — use the last generated image
    if image_source.strip().lower() == "last":
        last_b64 = _image_cache.get("__last_generated__")
        if last_b64:
            return last_b64
        raise ValueError(
            "No previously generated image available. "
            "Generate an image first, then use edit_image with image_source='last'."
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


# ── Core generation functions ────────────────────────────────────────────

def _generate_image(
    prompt: str,
    size: str = "auto",
    quality: str = "auto",
) -> str:
    """Generate an image from a text prompt."""
    global _last_generated_image

    try:
        client, provider_label, provider_id = _get_client()
    except Exception as e:
        logger.error("Image generation failed: %s", e, exc_info=True)
        return f"Image generation failed: {e}"
    model = _get_configured_model()

    logger.info("generate_image: model=%s, size=%s, quality=%s, provider=%s",
                model, size, quality, provider_label)

    # ── Google provider ──────────────────────────────────────────────────
    if provider_id == "google":
        from google.genai import types

        aspect_ratio, resolution = _map_google_params(size, quality)

        try:
            if model in _IMAGEN_MODEL_IDS:
                # Imagen 4 — dedicated generate_images endpoint
                cfg = types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio=aspect_ratio if aspect_ratio in _IMAGEN_ASPECT_RATIOS else "1:1",
                )
                response = client.models.generate_images(
                    model=model, prompt=prompt, config=cfg,
                )
                if not response.generated_images:
                    return "Image generation returned no images."
                img_bytes = response.generated_images[0].image.image_bytes
            else:
                # Nano Banana — generate_content with IMAGE modality
                img_cfg_kwargs: dict = {"aspect_ratio": aspect_ratio}
                if resolution:
                    img_cfg_kwargs["image_size"] = resolution
                cfg = types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(**img_cfg_kwargs),
                )
                response = client.models.generate_content(
                    model=model, contents=[prompt], config=cfg,
                )
                # Extract image bytes from response parts
                img_bytes = None
                for part in (response.parts or []):
                    if part.inline_data and part.inline_data.data:
                        img_bytes = part.inline_data.data
                        break
                if not img_bytes:
                    return "Image generation returned no image data."
        except Exception as e:
            logger.error("Image generation failed: %s", e, exc_info=True)
            return f"Image generation failed: {e}"

        b64_str = base64.b64encode(img_bytes).decode("ascii")
        _last_generated_image = b64_str
        _image_cache["__last_generated__"] = img_bytes
        saved = _save_image_to_disk(b64_str, "gen")
        result = (
            f"Image generated successfully. Model: {model} | "
            f"Aspect ratio: {aspect_ratio} | Provider: {provider_label}"
        )
        if saved:
            result += f"\nSaved to: {saved}"
        return result

    # ── xAI provider ──────────────────────────────────────────────────────
    # xAI does NOT support 'size' or 'style'. Uses 'aspect_ratio' & 'resolution'.
    # These are xAI-specific params so must go via extra_body.
    if provider_id in {"xai", "xai_oauth"}:
        from row_bot.providers.xai_media import (
            XAI_IMAGE_GENERATION_READ_TIMEOUT,
            XAI_MEDIA_DOWNLOAD_READ_TIMEOUT,
            xai_media_get,
            xai_media_json_request,
        )

        aspect = _OPENAI_SIZE_TO_ASPECT.get(size, "1:1") if size != "auto" else "auto"
        body: dict = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
        }
        if aspect != "auto":
            body["aspect_ratio"] = aspect
        plan = _plan_xai_image_request(provider_id, model, quality)
        if plan.quality:
            body["quality"] = plan.quality
        if plan.resolution:
            body["resolution"] = plan.resolution

        try:
            data = xai_media_json_request(
                provider_id,
                "POST",
                "/images/generations",
                json=body,
                timeout=XAI_IMAGE_GENERATION_READ_TIMEOUT,
            )
        except Exception as e:
            logger.error("Image generation failed: %s", e, exc_info=True)
            return f"Image generation failed: {e}"

        items = data.get("data") if isinstance(data.get("data"), list) else []
        if not items:
            return "Image generation returned no image data."
        image_data = items[0] if isinstance(items[0], dict) else {}
        if image_data.get("b64_json"):
            b64_str = str(image_data["b64_json"])
        elif image_data.get("url"):
            try:
                resp = xai_media_get(
                    provider_id,
                    str(image_data["url"]),
                    timeout=XAI_MEDIA_DOWNLOAD_READ_TIMEOUT,
                    follow_redirects=True,
                )
                b64_str = base64.b64encode(resp.content).decode("ascii")
            except Exception as e:
                logger.error("Image download failed: %s", e, exc_info=True)
                return f"Image generated but download failed: {e}"
        else:
            return "Image generation returned no image data."

        _last_generated_image = b64_str
        _image_cache["__last_generated__"] = base64.b64decode(b64_str)
        saved = _save_image_to_disk(b64_str, "gen")
        disp_aspect = aspect if aspect != "auto" else "auto"
        result = (
            f"Image generated successfully. Model: {model} | "
            f"Aspect ratio: {disp_aspect} | Provider: {provider_label}"
        )
        if saved:
            result += f"\nSaved to: {saved}"
        if plan.note:
            result += f"\n{plan.note}"
        return result

    # ── OpenAI provider ──────────────────────────────────────────────────
    kwargs: dict = {
        "model": model,
        "prompt": prompt,
        "n": 1,
        "size": size if size != "auto" else "1024x1024",
    }
    if quality != "auto":
        kwargs["quality"] = quality

    try:
        response = client.images.generate(**kwargs)
    except Exception as e:
        logger.error("Image generation failed: %s", e, exc_info=True)
        return f"Image generation failed: {e}"

    # Extract image data
    image_data = response.data[0]
    if hasattr(image_data, "b64_json") and image_data.b64_json:
        b64_str = image_data.b64_json
    elif hasattr(image_data, "url") and image_data.url:
        # Download the URL and convert to base64
        import urllib.request
        with urllib.request.urlopen(image_data.url) as resp:
            b64_str = base64.b64encode(resp.read()).decode("ascii")
    else:
        return "Image generation returned no image data."

    # Store in side-channel for UI rendering
    _last_generated_image = b64_str
    # Also store in cache for edit_image "last" reference
    _image_cache["__last_generated__"] = base64.b64decode(b64_str)

    saved = _save_image_to_disk(b64_str, "gen")
    revised_prompt = getattr(image_data, "revised_prompt", None)
    result = f"Image generated successfully. Model: {model} | Size: {kwargs['size']} | Provider: {provider_label}"
    if saved:
        result += f"\nSaved to: {saved}"
    if revised_prompt:
        result += f"\nRevised prompt: {revised_prompt}"
    return result


def _edit_image(
    prompt: str,
    image_source: str = "last",
    size: str = "auto",
    quality: str = "auto",
) -> str:
    """Edit an existing image using a text prompt."""
    global _last_generated_image

    try:
        client, provider_label, provider_id = _get_client()
    except Exception as e:
        logger.error("Image edit failed: %s", e, exc_info=True)
        return f"Image edit failed: {e}"
    model = _get_configured_model()

    # Imagen 4 does not support editing
    if model in _IMAGEN_MODEL_IDS:
        return (
            f"Image editing is not supported by {model}. "
            "Imagen 4 models can only generate new images. "
            "Please switch to a Nano Banana or OpenAI model for editing."
        )

    # Resolve the source image to raw bytes
    try:
        image_bytes = _resolve_image_source(image_source)
    except ValueError as e:
        return str(e)

    logger.info("edit_image: model=%s, size=%s, source=%s, provider=%s",
                model, size, image_source, provider_label)

    # ── Google Nano Banana — send image+text via generate_content ─────
    if provider_id == "google":
        from google.genai import types

        mime = _detect_mime(image_bytes)
        img_part = types.Part.from_bytes(data=image_bytes, mime_type=mime)

        try:
            cfg = types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            )
            response = client.models.generate_content(
                model=model, contents=[prompt, img_part], config=cfg,
            )
            img_out = None
            for part in (response.parts or []):
                if part.inline_data and part.inline_data.data:
                    img_out = part.inline_data.data
                    break
            if not img_out:
                return "Image edit returned no image data."
        except Exception as e:
            logger.error("Image edit failed: %s", e, exc_info=True)
            return f"Image edit failed: {e}"

        b64_str = base64.b64encode(img_out).decode("ascii")
        _last_generated_image = b64_str
        _image_cache["__last_generated__"] = img_out
        saved = _save_image_to_disk(b64_str, "edit")
        result = (
            f"Image edited successfully. Model: {model} | "
            f"Provider: {provider_label}"
        )
        if saved:
            result += f"\nSaved to: {saved}"
        return result

    # ── xAI provider — uses JSON body with image URL, not multipart ─────
    if provider_id in {"xai", "xai_oauth"}:
        from row_bot.providers.xai_media import (
            XAI_IMAGE_GENERATION_READ_TIMEOUT,
            XAI_MEDIA_DOWNLOAD_READ_TIMEOUT,
            xai_media_get,
            xai_media_json_request,
        )

        b64_src = base64.b64encode(image_bytes).decode("ascii")
        mime = _detect_mime(image_bytes)
        data_uri = f"data:{mime};base64,{b64_src}"

        body: dict = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
            "image": {"url": data_uri, "type": "image_url"},
        }
        aspect = _OPENAI_SIZE_TO_ASPECT.get(size, None) if size != "auto" else None
        if aspect:
            body["aspect_ratio"] = aspect
        plan = _plan_xai_image_request(provider_id, model, quality)
        if plan.quality:
            body["quality"] = plan.quality
        if plan.resolution:
            body["resolution"] = plan.resolution

        try:
            data = xai_media_json_request(
                provider_id,
                "POST",
                "/images/edits",
                json=body,
                timeout=XAI_IMAGE_GENERATION_READ_TIMEOUT,
            )
        except Exception as e:
            logger.error("Image edit failed: %s", e, exc_info=True)
            return f"Image edit failed: {e}"

        items = data.get("data", [])
        if not items:
            return "Image edit returned no image data."
        item = items[0]
        b64_str = item.get("b64_json") or ""
        if not b64_str and item.get("url"):
            try:
                resp = xai_media_get(
                    provider_id,
                    str(item["url"]),
                    timeout=XAI_MEDIA_DOWNLOAD_READ_TIMEOUT,
                    follow_redirects=True,
                )
                b64_str = base64.b64encode(resp.content).decode("ascii")
            except Exception as e:
                logger.error("Image edit download failed: %s", e, exc_info=True)
                return f"Image edited but download failed: {e}"
        if not b64_str:
            return "Image edit returned no image data."

        _last_generated_image = b64_str
        _image_cache["__last_generated__"] = base64.b64decode(b64_str)
        saved = _save_image_to_disk(b64_str, "edit")
        result = (
            f"Image edited successfully. Model: {model} | "
            f"Provider: {provider_label}"
        )
        if saved:
            result += f"\nSaved to: {saved}"
        if plan.note:
            result += f"\n{plan.note}"
        return result

    # ── OpenAI provider ──────────────────────────────────────────────────
    mime = _detect_mime(image_bytes)
    image_file = ("image.png", image_bytes, mime)

    kwargs: dict = {
        "model": model,
        "prompt": prompt,
        "image": [image_file],
        "n": 1,
        "size": size if size != "auto" else "1024x1024",
    }

    try:
        response = client.images.edit(**kwargs)
    except Exception as e:
        logger.error("Image edit failed: %s", e, exc_info=True)
        return f"Image edit failed: {e}"

    # Extract image data
    image_data = response.data[0]
    if hasattr(image_data, "b64_json") and image_data.b64_json:
        b64_str = image_data.b64_json
    elif hasattr(image_data, "url") and image_data.url:
        import urllib.request
        with urllib.request.urlopen(image_data.url) as resp:
            b64_str = base64.b64encode(resp.read()).decode("ascii")
    else:
        return "Image edit returned no image data."

    # Store in side-channel for UI rendering
    _last_generated_image = b64_str
    _image_cache["__last_generated__"] = base64.b64decode(b64_str)

    saved = _save_image_to_disk(b64_str, "edit")
    revised_prompt = getattr(image_data, "revised_prompt", None)
    result = f"Image edited successfully. Model: {model} | Size: {kwargs['size']} | Provider: {provider_label}"
    if saved:
        result += f"\nSaved to: {saved}"
    if revised_prompt:
        result += f"\nRevised prompt: {revised_prompt}"
    return result


# ── Pydantic input schemas ───────────────────────────────────────────────

class _GenerateImageInput(BaseModel):
    prompt: str = Field(
        description=(
            "A detailed description of the image to generate. Be specific "
            "about style, composition, colors, and subject matter. "
            "Example: 'A watercolor painting of a sunset over mountains "
            "with warm orange and purple tones'"
        )
    )
    size: str = Field(
        default="auto",
        description=(
            "Image dimensions. Options: 'auto' (default 1024x1024), "
            "'1024x1024' (square), '1536x1024' (landscape), "
            "'1024x1536' (portrait)."
        ),
    )
    quality: str = Field(
        default="auto",
        description=(
            "Image quality. Options: 'auto' (default), 'low' (fastest), "
            "'medium', 'high' (best quality, slower)."
        ),
    )


class _EditImageInput(BaseModel):
    prompt: str = Field(
        description=(
            "What to change in the image. Be specific about the edit. "
            "Example: 'Add a red hat to the cat', 'Remove the background', "
            "'Make it look more realistic'"
        )
    )
    image_source: str = Field(
        default="last",
        description=(
            "Where to get the image to edit. Use 'last' for the last "
            "generated image (default). Use the filename for an attached/"
            "pasted image (e.g. 'photo.jpg'). Or use a file path on disk."
        ),
    )
    size: str = Field(
        default="auto",
        description=(
            "Output image dimensions. Options: 'auto' (same as original), "
            "'1024x1024' (square), '1536x1024' (landscape), "
            "'1024x1536' (portrait)."
        ),
    )
    quality: str = Field(
        default="auto",
        description=(
            "Image quality. Options: 'auto' (default), 'low' (fastest), "
            "'medium', 'high' (best quality, slower)."
        ),
    )


# ── Tool registration ───────────────────────────────────────────────────

class ImageGenTool(BaseTool):

    @property
    def name(self) -> str:
        return "image_gen"

    @property
    def display_name(self) -> str:
        return "🎨 Image Generation"

    @property
    def description(self) -> str:
        return (
            "Generate images from text descriptions and edit existing images. "
            "Creates images using AI models (OpenAI GPT Image or Google "
            "Nano Banana / Imagen 4). Requires an OpenAI or Google API key."
        )

    @property
    def enabled_by_default(self) -> bool:
        # Only enable if a cloud key is available
        from row_bot.models import is_cloud_available
        return is_cloud_available()

    @property
    def config_schema(self) -> dict[str, dict]:
        # The model selector is rendered directly in the Models tab
        # (settings.py) using get_available_image_models(), not via
        # the generic config_schema renderer.
        return {}

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(
                func=_generate_image,
                name="generate_image",
                description=(
                    "Generate an image from a text description. Use this "
                    "when the user asks you to create, draw, design, or "
                    "generate any kind of image, illustration, artwork, "
                    "diagram, logo, or visual content. Provide a detailed "
                    "prompt describing the desired image."
                ),
                args_schema=_GenerateImageInput,
            ),
            StructuredTool.from_function(
                func=_edit_image,
                name="edit_image",
                description=(
                    "Edit or modify an existing image using a text prompt. "
                    "Use 'last' to edit the most recently generated image, "
                    "or specify a filename for a pasted/attached image, or "
                    "a file path. Use this when the user wants to change, "
                    "modify, adjust, or transform an existing image."
                ),
                args_schema=_EditImageInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return _generate_image(prompt=query)


registry.register(ImageGenTool())
