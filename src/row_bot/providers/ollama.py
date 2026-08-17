from __future__ import annotations

import logging
import re
import time
from typing import Any

from row_bot.providers.catalog import model_info_from_metadata
from row_bot.providers.models import ModelInfo, TransportMode

logger = logging.getLogger(__name__)

_tool_probe_cache: dict[str, dict[str, Any]] = {}
_TOOL_PROBE_TTL_SECONDS = 10 * 60

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

REASONING_FAMILIES = {
    "deepseek-r1",
    "gpt-oss",
    "magistral",
    "qwen3",
    "qwen3.5",
    "qwen3.6",
    "qwen3-next",
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


def _native_ollama_tool_capability(metadata: dict[str, Any]) -> bool | None:
    capabilities = metadata.get("capabilities")
    if not isinstance(capabilities, (list, tuple, set, frozenset)):
        return None
    normalized = {str(value).strip().lower() for value in capabilities}
    return "tools" in normalized


def is_ollama_reasoning_model(model_id: str) -> bool:
    raw = str(model_id or "").strip().lower()
    family = normalize_ollama_family(raw)
    tag = raw.rsplit(":", 1)[1] if ":" in raw else ""
    return family in REASONING_FAMILIES or "thinking" in tag


def is_ollama_vision_capable(model_id: str) -> bool:
    family = normalize_ollama_family(model_id)
    return family in VISION_FAMILIES or family.startswith(VISION_FAMILY_PREFIXES)


def is_ollama_chat_candidate(model_id: str) -> bool:
    family = normalize_ollama_family(model_id)
    if not family:
        return False
    return not any(marker in family for marker in NON_CHAT_FAMILY_MARKERS)


def probe_ollama_tool_round_trip(model_id: str, *, force: bool = False, timeout: float = 20.0) -> dict[str, Any]:
    """Probe an installed Ollama model for native tool-call round-trip support."""
    model_id = str(model_id or "").strip()
    if not model_id:
        return {"ok": False, "tool_calling": False, "tool_round_trip": False, "error": "missing model id"}
    now = time.time()
    cached = _tool_probe_cache.get(model_id)
    if cached and not force and now - float(cached.get("checked_at") or 0.0) < _TOOL_PROBE_TTL_SECONDS:
        return dict(cached)

    result: dict[str, Any] = {
        "ok": False,
        "tool_calling": False,
        "tool_round_trip": False,
        "checked_at": now,
        "source": "ollama_tool_probe",
    }
    tool = {
        "type": "function",
        "function": {
            "name": "row_bot_probe",
            "description": "Return the requested probe value.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        },
    }
    try:
        import httpx
        from row_bot.models import _ollama_base_url

        base_url = _ollama_base_url().rstrip("/")
        first = httpx.post(
            f"{base_url}/api/chat",
            json={
                "model": model_id,
                "messages": [{
                    "role": "user",
                    "content": "Call the row_bot_probe tool with value set to ok. Do not answer in prose.",
                }],
                "tools": [tool],
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        result["status_code"] = first.status_code
        if first.status_code >= 400:
            result["error"] = first.text[:300]
            _tool_probe_cache[model_id] = dict(result)
            return result
        first_payload = first.json()
        assistant_message = first_payload.get("message") if isinstance(first_payload, dict) else {}
        tool_calls = assistant_message.get("tool_calls") if isinstance(assistant_message, dict) else []
        if not isinstance(tool_calls, list) or not tool_calls:
            result["error"] = "model did not emit a tool call"
            _tool_probe_cache[model_id] = dict(result)
            return result
        result["tool_calling"] = True

        second = httpx.post(
            f"{base_url}/api/chat",
            json={
                "model": model_id,
                "messages": [
                    {"role": "user", "content": "Call the row_bot_probe tool with value set to ok. Do not answer in prose."},
                    assistant_message,
                    {"role": "tool", "content": "{\"value\":\"ok\"}"},
                ],
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=timeout,
        )
        result["round_trip_status_code"] = second.status_code
        if second.status_code >= 400:
            result["error"] = second.text[:300]
            _tool_probe_cache[model_id] = dict(result)
            return result
        result["tool_round_trip"] = True
        result["ok"] = True
        _tool_probe_cache[model_id] = dict(result)
        return result
    except Exception as exc:
        result["error"] = str(exc)
        logger.debug("Ollama tool probe failed for %s: %s", model_id, exc)
        _tool_probe_cache[model_id] = dict(result)
        return result


def is_ollama_cloud_offload_model(model_id: str) -> bool:
    """Return True for local Ollama model tags that run via Ollama Cloud."""
    raw = str(model_id or "").strip().lower()
    if not raw or ":" not in raw:
        return False
    tag = raw.rsplit(":", 1)[1]
    return tag == "cloud" or tag.endswith("-cloud") or any(
        marker == tag or tag.endswith(f"-{marker}") for marker in OLLAMA_CLOUD_TAG_MARKERS
    )


def ollama_model_info(
    model_id: str,
    *,
    installed: bool = False,
    context_window: int = 0,
    metadata: dict[str, Any] | None = None,
    source: str = "ollama_catalog",
) -> ModelInfo:
    metadata = dict(metadata or {})
    if not isinstance(metadata.get("tool_calling"), bool):
        native_tool_capability = _native_ollama_tool_capability(metadata)
        metadata["tool_calling"] = (
            native_tool_capability
            if native_tool_capability is not None
            else is_ollama_tool_capable(model_id)
        )
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
    runtime_installed = installed | ready_cloud_offload
    ordered = sorted(model_id for model_id in runtime_installed if is_ollama_chat_candidate(model_id))
    rows: list[dict[str, Any]] = []
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
        local_installed = info.model_id in installed
        availability = "installed_local" if local_installed else "cloud_offload_ready"
        rows.append({
            "provider_id": "ollama",
            "model_id": info.model_id,
            "display_name": info.display_name,
            "installed": info.model_id in runtime_installed,
            "downloadable": False,
            "recommended": False,
            "context_window": info.context_window,
            "capabilities_snapshot": info.capability_snapshot(),
            "risk_label": info.risk_label,
            "availability": availability,
            "source": info.source,
        })
    return rows
