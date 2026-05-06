from __future__ import annotations

import html
import logging
import re
from typing import Any
from urllib.parse import quote

from providers.catalog import model_info_from_metadata
from providers.models import ModelInfo, TransportMode

logger = logging.getLogger(__name__)

OLLAMA_LIBRARY_URL = "https://ollama.com/library"
DEFAULT_OLLAMA_LIBRARY_FAMILIES = ("qwen3.6", "qwen3.5", "qwen3", "qwen3-coder", "qwen3-next")
_library_model_cache: list[str] | None = None
_library_family_cache: dict[str, list[str]] = {}

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
}

VISION_FAMILIES = {"bakllava", "gemma3", "llama3.2-vision", "minicpm-v", "moondream", "qwen3-vl"}
VISION_FAMILY_PREFIXES = ("llava", "qwen-vl", "qwen2-vl", "qwen2.5-vl", "qwen3-vl")
NON_CHAT_FAMILY_MARKERS = ("embed", "embedding")


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
    global _library_model_cache
    if _library_model_cache is not None:
        return list(_library_model_cache)
    try:
        import httpx

        response = httpx.get(OLLAMA_LIBRARY_URL, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        model_ids = extract_ollama_library_family_ids(response.text)
        _library_model_cache = model_ids
        return list(model_ids)
    except Exception as exc:
        logger.debug("Could not fetch Ollama library families: %s", exc)
        _library_model_cache = []
        return []


def fetch_ollama_library_family_models(family: str, *, timeout: float = 4.0) -> list[str]:
    family = normalize_ollama_family(family)
    if not family:
        return []
    if family in _library_family_cache:
        return list(_library_family_cache[family])
    try:
        import httpx

        url = f"{OLLAMA_LIBRARY_URL}/{quote(family, safe='._-')}/tags"
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        model_ids = extract_ollama_library_model_ids(response.text, family)
        _library_family_cache[family] = model_ids
        return list(model_ids)
    except Exception as exc:
        logger.debug("Could not fetch Ollama library tags for %s: %s", family, exc)
        _library_family_cache[family] = []
        return []


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


def ollama_provider_catalog_model_ids(
    installed_models: list[str],
    curated_models: list[str],
    library_families: list[str],
    family_tag_models: list[str],
) -> list[str]:
    preferred_tags = preferred_ollama_tag_models(family_tag_models)
    represented_families = {
        normalize_ollama_family(model_id)
        for model_id in [*installed_models, *curated_models, *preferred_tags]
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
    family = str(model_id or "").split(":", 1)[0]
    metadata.setdefault("tool_calling", is_ollama_tool_capable(model_id))
    metadata.setdefault("vision", is_ollama_vision_capable(model_id))
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
        risk_label="local_private",
        source=source,
    )


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
) -> list[dict[str, Any]]:
    installed = set(installed_models)
    recommended = {model_id for model_id in recommended_models if is_ollama_tool_capable(model_id) or is_ollama_vision_capable(model_id)}
    seen = set(installed)
    ordered = sorted(model_id for model_id in installed if is_ollama_chat_candidate(model_id))
    for model_id in recommended_models:
        if (is_ollama_tool_capable(model_id) or is_ollama_vision_capable(model_id)) and model_id not in seen:
            seen.add(model_id)
            ordered.append(model_id)
    rows: list[dict[str, Any]] = []
    for info in ollama_model_infos(ordered, installed=installed, context_windows=context_windows):
        rows.append({
            "provider_id": "ollama",
            "model_id": info.model_id,
            "display_name": info.display_name,
            "installed": info.model_id in installed,
            "recommended": info.model_id in recommended,
            "context_window": info.context_window,
            "capabilities_snapshot": info.capability_snapshot(),
            "risk_label": info.risk_label,
        })
    return rows