from __future__ import annotations

import html
import logging
import re
import time
from typing import Any
from urllib.parse import quote

from providers.catalog import model_info_from_metadata
from providers.models import ModelInfo, TransportMode

logger = logging.getLogger(__name__)

OLLAMA_LIBRARY_URL = "https://ollama.com/library"
OLLAMA_CLOUD_SEARCH_URL = "https://ollama.com/search?c=cloud"
DEFAULT_OLLAMA_LIBRARY_FAMILIES = (
    "gemma4",
    "gemma3",
    "gpt-oss",
    "qwen3.6",
    "qwen3.5",
    "qwen3",
    "qwen3-vl",
    "qwen3-coder",
    "qwen3-coder-next",
    "qwen3-next",
)
OLLAMA_LIBRARY_CACHE_TTL_SECONDS = 6 * 60 * 60
_library_model_cache: list[str] | None = None
_library_model_cache_at = 0.0
_library_cloud_model_cache: list[str] | None = None
_library_cloud_model_cache_at = 0.0
_library_family_cache: dict[str, list[str]] = {}
_library_family_cache_at: dict[str, float] = {}

QUANTIZED_OR_BACKEND_TAG_MARKERS = (
    "-bf16",
    "-coding-bf16",
    "-coding-mxfp8",
    "-coding-nvfp4",
    "-fp16",
    "-int4",
    "-int8",
    "-mlx-",
    "-mxfp8",
    "-nvfp4",
    "-q4_",
    "-q8_",
)

TOOL_CAPABLE_FAMILIES = {
    "qwen3", "qwen3.5", "qwen3.6", "qwen3-coder", "llama3.1", "llama3.3",
    "llama3-groq-tool-use", "mistral", "mistral-nemo", "mistral-small",
    "mistral-small3.1", "mistral-small3.2", "mistral-large", "mixtral",
    "magistral", "ministral-3", "nemotron", "nemotron-3-nano",
    "devstral-small-2", "devstral-2", "olmo-3.1", "lfm2", "gpt-oss",
    "firefunction-v2", "glm-4.7-flash", "rnj-1", "qwen3-next",
    "gemma4", "qwen3-coder-next", "deepseek-v4-flash", "deepseek-v4-pro",
    "deepseek-v3.2", "deepseek-v3.1", "glm-5.1", "glm-5", "minimax-m2.7",
    "minimax-m2.5", "minimax-m2.1", "nemotron-3-super", "kimi-k2.6",
}

VISION_FAMILIES = {"bakllava", "gemma3", "llama3.2-vision", "minicpm-v", "moondream", "qwen3-vl"}
VISION_FAMILY_PREFIXES = ("llava", "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3-vl", "qwen3.5-vl")
NON_CHAT_FAMILY_MARKERS = ("embed", "embedding")
OLLAMA_CLOUD_TAG_MARKERS = ("cloud",)


def normalize_ollama_family(model_or_family: str) -> str:
    family = str(model_or_family or "").strip().split(":", 1)[0].split("/", 1)[-1].lower()
    return re.sub(r"[^a-z0-9_.-]", "", family)


def is_ollama_tool_capable(model_id: str) -> bool:
    return normalize_ollama_family(model_id) in TOOL_CAPABLE_FAMILIES


def is_ollama_vision_capable(model_id: str) -> bool:
    family = normalize_ollama_family(model_id)
    return family in VISION_FAMILIES or family.startswith(VISION_FAMILY_PREFIXES)


def is_ollama_chat_candidate(model_id: str) -> bool:
    family = normalize_ollama_family(model_id)
    if not family:
        return False
    return not any(marker in family for marker in NON_CHAT_FAMILY_MARKERS)


def is_ollama_cloud_offload_model(model_id: str) -> bool:
    """Return True for local Ollama model tags that run via Ollama Cloud."""
    raw = str(model_id or "").strip().lower()
    if not raw or ":" not in raw:
        return False
    tag = raw.rsplit(":", 1)[1]
    return tag == "cloud" or tag.endswith("-cloud") or any(
        marker == tag or tag.endswith(f"-{marker}") for marker in OLLAMA_CLOUD_TAG_MARKERS
    )


def is_preferred_ollama_tag(model_id: str) -> bool:
    if not (is_ollama_tool_capable(model_id) or is_ollama_vision_capable(model_id)):
        return False
    if ":" not in model_id:
        return True
    tag = model_id.split(":", 1)[1].lower()
    if tag in {"latest", "cloud"} or "cloud" in tag:
        return False
    return not any(marker in tag for marker in QUANTIZED_OR_BACKEND_TAG_MARKERS)


def extract_ollama_library_model_ids(page_html: str, family: str) -> list[str]:
    family = normalize_ollama_family(family)
    if not family:
        return []
    pattern = re.compile(rf"(?:https://ollama\.com)?/library/{re.escape(family)}(?::([^\"'#?<>\s]+))?")
    seen: set[str] = set()
    model_ids: list[str] = []
    for match in pattern.finditer(page_html):
        tag = html.unescape(match.group(1) or "latest")
        model_id = f"{family}:{tag}"
        if model_id not in seen:
            seen.add(model_id)
            model_ids.append(model_id)
    return model_ids


def extract_ollama_library_family_ids(page_html: str) -> list[str]:
    pattern = re.compile(r"(?:https://ollama\.com)?/library/([a-zA-Z0-9_.-]+)(?=[\"'#?<>\s])")
    seen: set[str] = set()
    family_ids: list[str] = []
    for match in pattern.finditer(page_html):
        family = normalize_ollama_family(match.group(1))
        if family and family not in seen:
            seen.add(family)
            family_ids.append(family)
    return family_ids


def fetch_ollama_library_models(*, timeout: float = 4.0) -> list[str]:
    global _library_model_cache, _library_model_cache_at
    now = time.time()
    if _library_model_cache is not None and now - _library_model_cache_at < OLLAMA_LIBRARY_CACHE_TTL_SECONDS:
        return list(_library_model_cache)
    try:
        import httpx

        response = httpx.get(OLLAMA_LIBRARY_URL, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        model_ids = extract_ollama_library_family_ids(response.text)
        _library_model_cache = model_ids
        _library_model_cache_at = now
        return list(model_ids)
    except Exception as exc:
        logger.debug("Could not fetch Ollama library families: %s", exc)
        return list(_library_model_cache or [])


def fetch_ollama_cloud_library_models(*, timeout: float = 4.0) -> list[str]:
    """Fetch model families from Ollama's public cloud-model library page."""
    global _library_cloud_model_cache, _library_cloud_model_cache_at
    now = time.time()
    if _library_cloud_model_cache is not None and now - _library_cloud_model_cache_at < OLLAMA_LIBRARY_CACHE_TTL_SECONDS:
        return list(_library_cloud_model_cache)
    try:
        import httpx

        response = httpx.get(OLLAMA_CLOUD_SEARCH_URL, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        model_ids = extract_ollama_library_family_ids(response.text)
        _library_cloud_model_cache = model_ids
        _library_cloud_model_cache_at = now
        return list(model_ids)
    except Exception as exc:
        logger.debug("Could not fetch Ollama cloud library families: %s", exc)
        return list(_library_cloud_model_cache or [])


def fetch_ollama_library_family_models(family: str, *, timeout: float = 4.0) -> list[str]:
    family = normalize_ollama_family(family)
    if not family:
        return []
    now = time.time()
    if family in _library_family_cache and now - _library_family_cache_at.get(family, 0.0) < OLLAMA_LIBRARY_CACHE_TTL_SECONDS:
        return list(_library_family_cache[family])
    try:
        import httpx

        url = f"{OLLAMA_LIBRARY_URL}/{quote(family, safe='._-')}/tags"
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        model_ids = extract_ollama_library_model_ids(response.text, family)
        _library_family_cache[family] = model_ids
        _library_family_cache_at[family] = now
        return list(model_ids)
    except Exception as exc:
        logger.debug("Could not fetch Ollama library tags for %s: %s", family, exc)
        return list(_library_family_cache.get(family, []))


def preferred_ollama_tag_models(model_ids: list[str], *, max_per_family: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    preferred: list[str] = []
    seen: set[str] = set()
    for model_id in model_ids:
        family = normalize_ollama_family(model_id)
        if model_id in seen or not is_preferred_ollama_tag(model_id):
            continue
        if counts.get(family, 0) >= max_per_family:
            continue
        seen.add(model_id)
        counts[family] = counts.get(family, 0) + 1
        preferred.append(model_id)
    return preferred


def preferred_ollama_cloud_offload_models(model_ids: list[str], *, max_per_family: int = 4) -> list[str]:
    counts: dict[str, int] = {}
    preferred: list[str] = []
    seen: set[str] = set()
    for model_id in model_ids:
        family = normalize_ollama_family(model_id)
        if model_id in seen or not is_ollama_cloud_offload_model(model_id):
            continue
        if not (is_ollama_tool_capable(model_id) or is_ollama_vision_capable(model_id)):
            continue
        if counts.get(family, 0) >= max_per_family:
            continue
        seen.add(model_id)
        counts[family] = counts.get(family, 0) + 1
        preferred.append(model_id)
    return preferred


def ollama_provider_catalog_model_ids(
    installed_models: list[str],
    curated_models: list[str],
    library_families: list[str],
    family_tag_models: list[str],
) -> list[str]:
    preferred_tags = preferred_ollama_tag_models(family_tag_models)
    cloud_offload_tags = preferred_ollama_cloud_offload_models(family_tag_models)
    represented_families = {
        normalize_ollama_family(model_id)
        for model_id in [*installed_models, *curated_models, *preferred_tags, *cloud_offload_tags]
        if is_ollama_tool_capable(model_id) or is_ollama_vision_capable(model_id) or model_id in installed_models
    }
    library_choices = [
        family
        for family in library_families
        if (is_ollama_tool_capable(family) or is_ollama_vision_capable(family))
        and normalize_ollama_family(family) not in represented_families
    ]
    ordered: list[str] = []
    seen: set[str] = set()
    for model_id in [
        *installed_models,
        *curated_models,
        *library_choices,
        *preferred_tags,
        *cloud_offload_tags,
    ]:
        known_catalog_candidate = is_ollama_tool_capable(model_id) or is_ollama_vision_capable(model_id)
        installed_chat_candidate = model_id in installed_models and is_ollama_chat_candidate(model_id)
        if not (known_catalog_candidate or installed_chat_candidate) or model_id in seen:
            continue
        seen.add(model_id)
        ordered.append(model_id)
    return ordered


def ollama_model_info(
    model_id: str,
    *,
    installed: bool = False,
    context_window: int = 0,
    metadata: dict[str, Any] | None = None,
    source: str = "ollama_catalog",
) -> ModelInfo:
    metadata = dict(metadata or {})
    metadata.setdefault("tool_calling", is_ollama_tool_capable(model_id))
    metadata.setdefault("vision", is_ollama_vision_capable(model_id) or _metadata_suggests_vision(metadata))
    if not is_ollama_chat_candidate(model_id):
        metadata.setdefault("embedding", True)
    metadata.setdefault("installed", installed)
    return model_info_from_metadata(
        "ollama",
        model_id,
        metadata,
        display_name=model_id,
        context_window=context_window,
        transport=TransportMode.OLLAMA_CHAT,
        risk_label="cloud_provider" if is_ollama_cloud_offload_model(model_id) else "local_private",
        source=source,
    )


def _metadata_suggests_vision(metadata: dict[str, Any]) -> bool:
    raw = " ".join(str(value).lower() for value in metadata.values() if isinstance(value, (str, int, float, bool)))
    if "vision" in raw or "image" in raw:
        return True
    capabilities = metadata.get("capabilities")
    if isinstance(capabilities, list) and any(str(item).lower() in {"vision", "image"} for item in capabilities):
        return True
    modalities = metadata.get("input_modalities") or metadata.get("input")
    if isinstance(modalities, list) and any(str(item).lower() == "image" for item in modalities):
        return True
    return False


def ollama_model_infos(
    model_ids: list[str],
    *,
    installed: set[str] | None = None,
    context_windows: dict[str, int] | None = None,
) -> list[ModelInfo]:
    installed = installed or set()
    context_windows = context_windows or {}
    return [
        ollama_model_info(
            model_id,
            installed=model_id in installed,
            context_window=context_windows.get(model_id, 0),
        )
        for model_id in model_ids
    ]


def ollama_catalog_rows(
    installed_models: list[str],
    recommended_models: list[str],
    *,
    context_windows: dict[str, int] | None = None,
    ready_cloud_offload: set[str] | None = None,
    metadata_by_model: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    installed = set(installed_models)
    ready_cloud_offload = ready_cloud_offload or set()
    metadata_by_model = metadata_by_model or {}
    recommended = {model_id for model_id in recommended_models if is_ollama_tool_capable(model_id) or is_ollama_vision_capable(model_id)}
    seen = set(installed)
    ordered = sorted(model_id for model_id in installed if is_ollama_chat_candidate(model_id))
    for model_id in recommended_models:
        if (is_ollama_tool_capable(model_id) or is_ollama_vision_capable(model_id)) and model_id not in seen:
            seen.add(model_id)
            ordered.append(model_id)
    rows: list[dict[str, Any]] = []
    runtime_installed = installed | ready_cloud_offload
    for model_id in ordered:
        metadata = dict(metadata_by_model.get(model_id) or {})
        context_window = context_windows.get(model_id, 0) if context_windows else 0
        context_window = int(metadata.get("context_window") or context_window or 0)
        info = ollama_model_info(
            model_id,
            installed=model_id in runtime_installed,
            context_window=context_window,
            metadata=metadata,
            source="ollama_cloud_offload_catalog" if is_ollama_cloud_offload_model(model_id) else "ollama_catalog",
        )
        cloud_offload = is_ollama_cloud_offload_model(info.model_id)
        cloud_ready = info.model_id in ready_cloud_offload
        local_installed = info.model_id in installed
        availability = "installed_local" if local_installed else (
            "cloud_offload_ready" if cloud_ready else (
                "cloud_offload_available" if cloud_offload else "downloadable_local"
            )
        )
        rows.append({
            "provider_id": "ollama",
            "model_id": info.model_id,
            "display_name": info.display_name,
            "installed": info.model_id in runtime_installed,
            "downloadable": availability in {"downloadable_local", "cloud_offload_available"},
            "recommended": info.model_id in recommended,
            "context_window": info.context_window,
            "capabilities_snapshot": info.capability_snapshot(),
            "risk_label": info.risk_label,
            "availability": availability,
            "source": info.source,
        })
    return rows
