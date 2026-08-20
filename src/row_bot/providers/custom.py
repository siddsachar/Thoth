from __future__ import annotations

import logging
import json
import re
from typing import Any
from urllib.parse import quote

from row_bot.providers.auth_store import delete_provider_secret, get_provider_secret, set_provider_secret
from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
from row_bot.providers.config import load_provider_config, save_provider_config
from row_bot.providers.models import AuthMethod, ModelInfo, ProviderDefinition, TransportMode

CUSTOM_OPENAI_PREFIX = "custom_openai_"
DEFAULT_CUSTOM_ENDPOINT_PROFILE = "generic_openai"
DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK = 32_768
TOOL_PROBE_MAX_TOKENS = 1024
TOOL_ROUND_TRIP_PROBE_MAX_TOKENS = 256
STREAMING_PROBE_MAX_TOKENS = 128
VISION_PROBE_MAX_TOKENS = 256
VISION_PROBE_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
logger = logging.getLogger(__name__)


CUSTOM_ENDPOINT_REASONING_DEFAULTS: dict[str, Any] = {
    "supports_reasoning_content": False,
    "supports_reasoning_replay": False,
    "reasoning_mode": "auto",
    "preserve_thinking": True,
}


class CustomEndpointModelRefreshResult(list):
    def __init__(
        self,
        infos: list[ModelInfo],
        *,
        stale_pin_count: int = 0,
        default_reset: bool = False,
    ) -> None:
        super().__init__(infos)
        self.stale_pin_count = int(stale_pin_count or 0)
        self.default_reset = bool(default_reset)


def _positive_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _reasoning_mode(value: Any, default: str = "auto") -> str:
    mode = str(value or default or "auto").strip().lower()
    return mode if mode in {"auto", "on", "off"} else "auto"


CUSTOM_ENDPOINT_PROFILES: dict[str, dict[str, Any]] = {
    "generic_openai": {
        "display_name": "OpenAI-compatible",
        "transport": TransportMode.OPENAI_CHAT.value,
        "system_message_mode": "system_first",
        "tool_history_mode": "native_required",
        "message_content_mode": "openai",
        "drop_unsupported_params": True,
        "supports_runtime_context_override": False,
        "context_param_name": "",
        "unknown_context_fallback": DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK,
    },
    "omlx": {
        "display_name": "oMLX",
        "transport": TransportMode.OPENAI_CHAT.value,
        "system_message_mode": "system_first",
        "tool_history_mode": "native_required",
        "message_content_mode": "string_text",
        "drop_unsupported_params": True,
        "supports_runtime_context_override": False,
        "context_param_name": "",
        "unknown_context_fallback": DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK,
    },
    "vllm": {
        "display_name": "vLLM",
        "transport": TransportMode.OPENAI_CHAT.value,
        "system_message_mode": "system_first",
        "tool_history_mode": "native_required",
        "message_content_mode": "openai",
        "drop_unsupported_params": True,
        "supports_runtime_context_override": False,
        "context_param_name": "",
        "unknown_context_fallback": DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK,
    },
    "lmstudio": {
        "display_name": "LM Studio",
        "transport": TransportMode.OPENAI_CHAT.value,
        "system_message_mode": "system_first",
        "tool_history_mode": "native_required",
        "message_content_mode": "string_text",
        "drop_unsupported_params": True,
        "supports_runtime_context_override": False,
        "context_param_name": "",
        "unknown_context_fallback": DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK,
    },
    "llama_cpp": {
        "display_name": "llama.cpp",
        "transport": TransportMode.OPENAI_CHAT.value,
        "system_message_mode": "system_first",
        "tool_history_mode": "native_required",
        "message_content_mode": "string_text",
        "drop_unsupported_params": True,
        "supports_runtime_context_override": True,
        "context_param_name": "n_ctx",
        "unknown_context_fallback": DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK,
    },
    "localai": {
        "display_name": "LocalAI",
        "transport": TransportMode.OPENAI_CHAT.value,
        "system_message_mode": "system_first",
        "tool_history_mode": "native_required",
        "message_content_mode": "openai",
        "drop_unsupported_params": True,
        "supports_runtime_context_override": False,
        "context_param_name": "",
        "unknown_context_fallback": DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK,
    },
    "litellm": {
        "display_name": "LiteLLM",
        "transport": TransportMode.OPENAI_CHAT.value,
        "system_message_mode": "system_first",
        "tool_history_mode": "native_required",
        "message_content_mode": "openai",
        "drop_unsupported_params": True,
        "supports_runtime_context_override": False,
        "context_param_name": "",
        "unknown_context_fallback": DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK,
    },
    "sglang": {
        "display_name": "SGLang",
        "transport": TransportMode.OPENAI_CHAT.value,
        "system_message_mode": "system_first",
        "tool_history_mode": "native_required",
        "message_content_mode": "openai",
        "drop_unsupported_params": True,
        "supports_runtime_context_override": False,
        "context_param_name": "",
        "unknown_context_fallback": DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK,
    },
}


def custom_endpoint_profile(profile_id: str | None) -> dict[str, Any]:
    profile = str(profile_id or DEFAULT_CUSTOM_ENDPOINT_PROFILE).strip().lower()
    result = dict(CUSTOM_ENDPOINT_REASONING_DEFAULTS)
    result.update(CUSTOM_ENDPOINT_PROFILES.get(profile) or CUSTOM_ENDPOINT_PROFILES[DEFAULT_CUSTOM_ENDPOINT_PROFILE])
    if profile in {"llama_cpp", "lmstudio", "vllm", "sglang", "litellm"}:
        result["supports_reasoning_content"] = True
    return result


def slugify_endpoint_id(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "endpoint"


def custom_provider_id(endpoint_id: str) -> str:
    return f"{CUSTOM_OPENAI_PREFIX}{slugify_endpoint_id(endpoint_id)}"


def endpoint_id_from_provider_id(provider_id: str) -> str:
    provider_id = str(provider_id or "")
    if provider_id.startswith(CUSTOM_OPENAI_PREFIX):
        return provider_id.removeprefix(CUSTOM_OPENAI_PREFIX)
    return slugify_endpoint_id(provider_id)


def is_custom_openai_provider(provider_id: str | None) -> bool:
    return str(provider_id or "").startswith(CUSTOM_OPENAI_PREFIX)


def normalize_custom_endpoint(endpoint: dict[str, Any]) -> dict[str, Any]:
    endpoint_id = slugify_endpoint_id(str(endpoint.get("id") or endpoint.get("name") or "endpoint"))
    base_url = str(endpoint.get("base_url") or endpoint.get("url") or "").rstrip("/")
    name = str(endpoint.get("name") or endpoint.get("display_name") or endpoint_id)
    profile = str(endpoint.get("profile") or endpoint.get("compatibility_profile") or DEFAULT_CUSTOM_ENDPOINT_PROFILE).strip().lower()
    profile_defaults = custom_endpoint_profile(profile)
    transport = str(endpoint.get("transport") or profile_defaults.get("transport") or TransportMode.OPENAI_CHAT.value)
    execution_location = str(endpoint.get("execution_location") or endpoint.get("privacy") or "remote")
    risk_label = str(endpoint.get("risk_label") or ("local_private" if execution_location == "local" else "custom_endpoint"))
    unknown_context_fallback = max(
        _positive_int(endpoint.get("unknown_context_fallback")),
        _positive_int(profile_defaults.get("unknown_context_fallback")),
        DEFAULT_CUSTOM_ENDPOINT_CONTEXT_FALLBACK,
    )
    system_message_mode = str(
        endpoint.get("system_message_mode")
        or profile_defaults.get("system_message_mode")
        or "system_first"
    )
    if profile == "litellm" and system_message_mode == "provider_default":
        system_message_mode = "system_first"
    supports_runtime_context_override = bool(
        endpoint.get("supports_runtime_context_override", profile_defaults.get("supports_runtime_context_override", False))
    )
    context_param_name = str(endpoint.get("context_param_name") or profile_defaults.get("context_param_name") or "")
    if profile in {"vllm", "sglang"} and context_param_name in {"max_model_len", "context_length"}:
        supports_runtime_context_override = False
        context_param_name = ""

    normalized = {
        "id": endpoint_id,
        "provider_id": custom_provider_id(endpoint_id),
        "name": name,
        "display_name": name,
        "base_url": base_url,
        "profile": profile if profile in CUSTOM_ENDPOINT_PROFILES else DEFAULT_CUSTOM_ENDPOINT_PROFILE,
        "transport": transport,
        "auth_required": bool(endpoint.get("auth_required", bool(endpoint.get("api_key")))),
        "api_key_header": str(endpoint.get("api_key_header") or "Authorization"),
        "execution_location": execution_location,
        "risk_label": risk_label,
        "enabled": bool(endpoint.get("enabled", True)),
        "capability_probe": bool(endpoint.get("capability_probe", True)),
        "source_confidence": str(endpoint.get("source_confidence") or "user_configured"),
        "system_message_mode": system_message_mode,
        "tool_history_mode": _normalize_tool_history_mode(
            endpoint.get("tool_history_mode") or profile_defaults.get("tool_history_mode") or "native_required"
        ),
        "message_content_mode": str(endpoint.get("message_content_mode") or profile_defaults.get("message_content_mode") or "openai"),
        "drop_unsupported_params": bool(endpoint.get("drop_unsupported_params", profile_defaults.get("drop_unsupported_params", True))),
        "supports_runtime_context_override": supports_runtime_context_override,
        "context_param_name": context_param_name,
        "unknown_context_fallback": unknown_context_fallback,
        "supports_reasoning_content": bool(endpoint.get("supports_reasoning_content", profile_defaults.get("supports_reasoning_content", False))),
        "supports_reasoning_replay": bool(endpoint.get("supports_reasoning_replay", profile_defaults.get("supports_reasoning_replay", False))),
        "reasoning_mode": _reasoning_mode(endpoint.get("reasoning_mode"), str(profile_defaults.get("reasoning_mode") or "auto")),
        "preserve_thinking": bool(endpoint.get("preserve_thinking", profile_defaults.get("preserve_thinking", True))),
    }
    thinking_budget = _positive_int(endpoint.get("thinking_budget"))
    if thinking_budget:
        normalized["thinking_budget"] = thinking_budget
    manual = endpoint.get("manual_capabilities")
    if isinstance(manual, dict):
        normalized["manual_capabilities"] = dict(manual)
    headers = endpoint.get("headers")
    if isinstance(headers, dict):
        normalized["headers"] = {str(k): str(v) for k, v in headers.items()}
    models = endpoint.get("models")
    if isinstance(models, list):
        normalized["models"] = [dict(item) for item in models if isinstance(item, dict)]
    last_probe = endpoint.get("last_probe")
    if isinstance(last_probe, dict):
        normalized["last_probe"] = dict(last_probe)
    extra_body = endpoint.get("extra_body")
    if isinstance(extra_body, dict):
        normalized["extra_body"] = dict(extra_body)
    return normalized


def list_custom_endpoints() -> list[dict]:
    return [normalize_custom_endpoint(item) for item in load_provider_config().get("custom_endpoints", []) if isinstance(item, dict)]


def get_custom_endpoint(endpoint_or_provider_id: str) -> dict[str, Any] | None:
    endpoint_id = endpoint_id_from_provider_id(endpoint_or_provider_id)
    for endpoint in list_custom_endpoints():
        if endpoint.get("id") == endpoint_id or endpoint.get("provider_id") == endpoint_or_provider_id:
            return endpoint
    return None


def list_custom_provider_definitions() -> list[ProviderDefinition]:
    definitions: list[ProviderDefinition] = []
    for endpoint in list_custom_endpoints():
        if not endpoint.get("enabled", True):
            continue
        try:
            transport = TransportMode(str(endpoint.get("transport") or TransportMode.OPENAI_CHAT.value))
        except ValueError:
            transport = TransportMode.OPENAI_CHAT
        definitions.append(ProviderDefinition(
            id=str(endpoint["provider_id"]),
            display_name=str(endpoint.get("display_name") or endpoint["id"]),
            auth_methods=(AuthMethod.API_KEY, AuthMethod.NONE),
            default_transport=transport,
            base_url=str(endpoint.get("base_url") or ""),
            risk_label=str(endpoint.get("risk_label") or "custom_endpoint"),
            supports_catalog=True,
            experimental=True,
            icon="↔",
        ))
    return definitions


def save_custom_endpoint(endpoint: dict) -> None:
    extra_body = endpoint.get("extra_body")
    if isinstance(extra_body, dict) and _extra_body_contains_credentials(extra_body):
        raise ValueError("Custom endpoint extra_body must not contain credentials or secrets.")
    cfg = load_provider_config()
    endpoints = [item for item in cfg.get("custom_endpoints", []) if isinstance(item, dict)]
    secret = str(endpoint.get("api_key") or "")
    normalized = normalize_custom_endpoint(endpoint)
    endpoint_id = normalized.get("id")
    endpoints = [item for item in endpoints if item.get("id") != endpoint_id]
    endpoints.append(normalized)
    cfg["custom_endpoints"] = endpoints
    save_provider_config(cfg)
    if secret:
        set_provider_secret(str(normalized["provider_id"]), "api_key", secret)
    elif not normalized.get("auth_required"):
        delete_provider_secret(str(normalized["provider_id"]), "api_key")


def _extra_body_contains_credentials(value: Any) -> bool:
    secret_keys = {
        "authorization", "api_key", "api-key", "x-api-key", "access_token",
        "refresh_token", "password", "secret", "bearer",
    }
    if isinstance(value, dict):
        return any(
            str(key).strip().lower() in secret_keys or _extra_body_contains_credentials(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_extra_body_contains_credentials(item) for item in value)
    return False


def delete_custom_endpoint(endpoint_or_provider_id: str) -> int:
    endpoint_id = endpoint_id_from_provider_id(endpoint_or_provider_id)
    provider_id = custom_provider_id(endpoint_id)
    cfg = load_provider_config()
    removed_model_ids: set[str] = set()
    for item in cfg.get("custom_endpoints", []):
        if not isinstance(item, dict) or normalize_custom_endpoint(item).get("id") != endpoint_id:
            continue
        models = item.get("models") if isinstance(item.get("models"), list) else []
        removed_model_ids = {
            str(model.get("model_id") or model.get("id") or "")
            for model in models
            if isinstance(model, dict) and str(model.get("model_id") or model.get("id") or "")
        }
        break
    cfg["custom_endpoints"] = [
        item for item in cfg.get("custom_endpoints", [])
        if not isinstance(item, dict) or normalize_custom_endpoint(item).get("id") != endpoint_id
    ]
    save_provider_config(cfg)
    removed_pins = 0
    try:
        from row_bot.providers.selection import remove_quick_choices_for_provider

        removed_pins = remove_quick_choices_for_provider(provider_id)
    except Exception:
        logger.debug("Failed to remove quick choices for custom provider %s", provider_id, exc_info=True)
    try:
        from row_bot.models import reset_current_model_if_removed

        reset_current_model_if_removed(provider_id, removed_model_ids=removed_model_ids)
    except Exception:
        logger.debug("Failed to reset current model after deleting %s", provider_id, exc_info=True)
    delete_provider_secret(provider_id, "api_key")
    return removed_pins


def custom_endpoint_secret(endpoint_or_provider_id: str) -> str:
    endpoint = get_custom_endpoint(endpoint_or_provider_id)
    provider_id = str(endpoint.get("provider_id") if endpoint else endpoint_or_provider_id)
    return get_provider_secret(provider_id, "api_key")


def custom_endpoint_auth_missing_message(endpoint: dict[str, Any]) -> str:
    label = str(endpoint.get("display_name") or endpoint.get("id") or endpoint.get("provider_id") or "custom endpoint")
    return (
        f"Custom endpoint '{label}' requires an API key, but no key is available. "
        "Re-enter the key for this session or configure a secure keyring backend for persistent storage."
    )


def custom_endpoint_models(endpoint_or_provider_id: str) -> list[dict[str, Any]]:
    endpoint = get_custom_endpoint(endpoint_or_provider_id)
    if not endpoint:
        return []
    models = endpoint.get("models")
    return [dict(item) for item in models if isinstance(item, dict)] if isinstance(models, list) else []


def custom_model_cache_entries() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for endpoint in list_custom_endpoints():
        for item in custom_endpoint_models(str(endpoint["id"])):
            model_id = str(item.get("model_id") or item.get("id") or "")
            if not model_id:
                continue
            entry = dict(item)
            entry.setdefault("label", entry.get("display_name") or model_id)
            entry.setdefault("ctx", int(entry.get("context_window") or 0))
            entry.setdefault("provider", endpoint["provider_id"])
            entry.setdefault("risk_label", endpoint.get("risk_label") or "custom_endpoint")
            entries[model_id] = entry
    return entries


def refresh_custom_endpoint_models(endpoint_or_provider_id: str) -> list[ModelInfo]:
    endpoint = get_custom_endpoint(endpoint_or_provider_id)
    if not endpoint:
        raise ValueError("Custom endpoint not found.")
    if not endpoint.get("base_url"):
        raise ValueError("Custom endpoint is missing a base URL.")

    import httpx

    headers = _custom_endpoint_headers(endpoint)
    url = f"{str(endpoint['base_url']).rstrip('/')}/models"
    response = httpx.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    payload = response.json()
    native_metadata = _fetch_custom_endpoint_native_model_metadata(
        endpoint,
        headers=headers,
        model_ids=_catalog_model_ids(payload),
    )
    infos = model_infos_from_openai_compatible_catalog(endpoint, payload, native_metadata_by_model=native_metadata)
    _store_custom_endpoint_models(endpoint["id"], infos)
    valid_model_ids = {info.model_id for info in infos}
    stale_pin_count = 0
    default_reset = False
    try:
        from row_bot.providers.selection import remove_quick_choices_for_missing_models

        stale_pin_count = remove_quick_choices_for_missing_models(str(endpoint["provider_id"]), valid_model_ids)
    except Exception:
        logger.debug("Failed to prune stale quick choices for %s", endpoint["provider_id"], exc_info=True)
    try:
        previous_model_ids = {
            str(item.get("model_id") or item.get("id") or "")
            for item in (endpoint.get("models") if isinstance(endpoint.get("models"), list) else [])
            if isinstance(item, dict) and str(item.get("model_id") or item.get("id") or "")
        }
        removed_model_ids = previous_model_ids - valid_model_ids
        try:
            from row_bot.models import get_current_model
            from row_bot.providers.selection import parse_model_ref

            parsed_current = parse_model_ref(get_current_model())
            if (
                parsed_current
                and parsed_current[0] == str(endpoint["provider_id"])
                and parsed_current[1] not in valid_model_ids
            ):
                removed_model_ids.add(parsed_current[1])
        except Exception:
            pass
        if removed_model_ids:
            from row_bot.models import reset_current_model_if_removed

            default_reset = reset_current_model_if_removed(str(endpoint["provider_id"]), removed_model_ids=removed_model_ids)
    except Exception:
        logger.debug("Failed to reset current model after refreshing %s", endpoint["provider_id"], exc_info=True)
    return CustomEndpointModelRefreshResult(
        infos,
        stale_pin_count=stale_pin_count,
        default_reset=default_reset,
    )


def probe_custom_endpoint(endpoint_or_provider_id: str, model_id: str | None = None) -> dict[str, Any]:
    endpoint = get_custom_endpoint(endpoint_or_provider_id)
    if not endpoint:
        raise ValueError("Custom endpoint not found.")
    if not endpoint.get("base_url"):
        raise ValueError("Custom endpoint is missing a base URL.")

    import httpx

    headers = _custom_endpoint_headers(endpoint)
    result: dict[str, Any] = {
        "ok": False,
        "agent_ok": False,
        "chat_only_ok": False,
        "classification": "unavailable",
        "models_ok": False,
        "chat_ok": False,
        "streaming_ok": None,
        "tool_calling": None,
        "tool_round_trip": None,
        "streaming_tool_calling": None,
        "vision_ok": None,
        "vision_probed": False,
        "vision_probe_skip_reason": "",
        "vision_error": "",
        "vision_probe_response": "",
        "vision_model": "",
        "vision_content_format": "",
        "context_window": 0,
        "profile": endpoint.get("profile") or DEFAULT_CUSTOM_ENDPOINT_PROFILE,
        "transport": endpoint.get("transport") or TransportMode.OPENAI_CHAT.value,
        "errors": [],
    }
    try:
        infos = refresh_custom_endpoint_models(endpoint_or_provider_id)
        result["models_ok"] = True
    except Exception as exc:
        infos = []
        result["errors"].append(f"models: {exc}")

    target_model = str(model_id or "")
    if not target_model and infos:
        target_model = infos[0].model_id
    if not target_model:
        result["errors"].append("chat: no model available to probe")
        _classify_probe_result(result)
        _store_custom_endpoint_probe(endpoint["id"], result)
        logger.warning(
            "Custom endpoint probe failed: id=%s base_url=%s profile=%s errors=%s",
            endpoint["id"],
            endpoint.get("base_url"),
            endpoint.get("profile"),
            "; ".join(str(error) for error in result.get("errors", [])) or "unknown error",
        )
        return result

    matched = next((info for info in infos if info.model_id == target_model), None)
    result["vision_model"] = target_model
    if matched and matched.context_window:
        result["context_window"] = int(matched.context_window)

    chat_url = f"{str(endpoint['base_url']).rstrip('/')}/chat/completions"
    body = {
        "model": target_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        response = httpx.post(chat_url, headers=headers, json=body, timeout=20)
        response.raise_for_status()
        result["chat_ok"] = True
    except Exception as exc:
        result["errors"].append(f"chat: {exc}")

    tool_body = {
        "model": target_model,
        "messages": [{
            "role": "user",
            "content": "Call the row_bot_probe_echo tool with value set to ok. Do not answer in text.",
        }],
        "tools": [_probe_tool_schema()],
        "tool_choice": "auto",
        "max_tokens": TOOL_PROBE_MAX_TOKENS,
        "stream": False,
    }
    tool_call_payload: dict[str, Any] | None = None
    try:
        response = httpx.post(chat_url, headers=headers, json=tool_body, timeout=30)
        response.raise_for_status()
        payload = response.json()
        tool_call_payload = _extract_structured_tool_call(payload, "row_bot_probe_echo")
        result["tool_calling"] = tool_call_payload is not None
        if tool_call_payload is None:
            result["errors"].append("tools: no structured tool call returned")
    except Exception as exc:
        result["tool_calling"] = False
        result["errors"].append(f"tools: {exc}")

    if tool_call_payload is not None:
        call_id = str(tool_call_payload.get("id") or "call_row_bot_probe_echo")
        round_trip_body = {
            "model": target_model,
            "messages": [
                tool_body["messages"][0],
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [tool_call_payload],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": "{\"value\":\"ok\"}",
                },
            ],
            "max_tokens": TOOL_ROUND_TRIP_PROBE_MAX_TOKENS,
            "stream": False,
        }
        try:
            response = httpx.post(chat_url, headers=headers, json=round_trip_body, timeout=30)
            response.raise_for_status()
            result["tool_round_trip"] = True
        except Exception as exc:
            result["tool_round_trip"] = False
            result["errors"].append(f"tool_round_trip: {exc}")
    stream_body = dict(body)
    stream_body["stream"] = True
    stream_body["max_tokens"] = STREAMING_PROBE_MAX_TOKENS
    try:
        saw_stream_event = False
        with httpx.stream("POST", chat_url, headers=headers, json=stream_body, timeout=20) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line or "")
                if _stream_line_has_usable_delta(text):
                    saw_stream_event = True
                    break
        result["streaming_ok"] = saw_stream_event
        if not saw_stream_event:
            result["errors"].append("streaming: no usable stream delta returned")
    except Exception as exc:
        result["streaming_ok"] = False
        result["errors"].append(f"streaming: {exc}")

    stream_tool_body = dict(tool_body)
    stream_tool_body["stream"] = True
    stream_tool_body["max_tokens"] = TOOL_PROBE_MAX_TOKENS
    try:
        with httpx.stream("POST", chat_url, headers=headers, json=stream_tool_body, timeout=30) as response:
            response.raise_for_status()
            tool_call_payload = _extract_streamed_structured_tool_call(response.iter_lines(), "row_bot_probe_echo")
        result["streaming_tool_calling"] = tool_call_payload is not None
        if tool_call_payload is None:
            result["streaming_tool_error"] = "no streamed structured tool call returned"
            result["errors"].append("streaming_tools: no streamed structured tool call returned")
    except Exception as exc:
        result["streaming_tool_calling"] = False
        result["streaming_tool_error"] = str(exc)
        result["errors"].append(f"streaming_tools: {exc}")

    should_probe_vision, vision_skip_reason = _vision_probe_decision(endpoint, matched, target_model)
    if should_probe_vision:
        result["vision_probed"] = True
        result["vision_content_format"] = "openai_image_url"
        vision_body = _vision_probe_body(target_model)
        try:
            response = httpx.post(chat_url, headers=headers, json=vision_body, timeout=30)
            response.raise_for_status()
            text = _extract_probe_text(response.json()).strip().lower()
            result["vision_probe_response"] = text[:200]
            if _vision_probe_text_indicates_success(text):
                result["vision_ok"] = True
            elif _vision_probe_text_indicates_failure(text):
                result["vision_ok"] = False
                result["vision_error"] = f"unexpected response: {text or '<empty>'}"
                result["errors"].append(f"vision: {result['vision_error']}")
            else:
                result["vision_ok"] = None
                result["vision_error"] = f"probe inconclusive: unexpected response: {text or '<empty>'}"
        except Exception as exc:
            result["vision_ok"] = False
            result["vision_error"] = str(exc)
            result["errors"].append(f"vision: {exc}")
    else:
        result["vision_probe_skip_reason"] = vision_skip_reason

    _classify_probe_result(result)
    _store_custom_endpoint_probe(endpoint["id"], result)
    summary = custom_probe_summary(result)
    if result["ok"]:
        logger.info(
            "Custom endpoint probe ok: id=%s base_url=%s classification=%s models_ok=%s chat_ok=%s "
            "streaming_ok=%s tool_calling=%s tool_round_trip=%s streaming_tool_calling=%s "
            "vision_probed=%s vision_ok=%s vision_model=%s vision_content_format=%s vision_error=%s vision_skip=%s vision_response=%s",
            endpoint["id"],
            endpoint.get("base_url"),
            result.get("classification"),
            result.get("models_ok"),
            result.get("chat_ok"),
            result.get("streaming_ok"),
            result.get("tool_calling"),
            result.get("tool_round_trip"),
            result.get("streaming_tool_calling"),
            result.get("vision_probed"),
            result.get("vision_ok"),
            result.get("vision_model"),
            result.get("vision_content_format"),
            result.get("vision_error"),
            result.get("vision_probe_skip_reason"),
            result.get("vision_probe_response"),
        )
    else:
        logger.warning(
            "Custom endpoint probe failed: id=%s base_url=%s profile=%s classification=%s details=%s errors=%s",
            endpoint["id"],
            endpoint.get("base_url"),
            endpoint.get("profile"),
            result.get("classification"),
            summary.get("text"),
            "; ".join(str(error) for error in result.get("errors", [])) or "unknown error",
        )
    return result


def _component_status(value: Any, *, probed: bool = True, missing: str = "unknown") -> str:
    if not probed:
        return "not_probed"
    if value is True:
        return "ok"
    if value is False:
        return "failed"
    return missing


def _component_label(name: str, status: str) -> str:
    label = {
        "ok": "ok",
        "failed": "failed",
        "inconclusive": "inconclusive",
        "not_probed": "not probed",
        "unknown": "unknown",
    }.get(status, status)
    return f"{name}: {label}"


def custom_probe_summary(last_probe: dict[str, Any] | None) -> dict[str, Any]:
    probe = dict(last_probe or {}) if isinstance(last_probe, dict) else {}
    if not probe:
        return {"classification": "unknown", "components": [], "text": "No probe details recorded yet."}
    vision_probed = bool(probe.get("vision_probed"))
    if "vision_probed" not in probe:
        vision_probed = bool(probe.get("vision_content_format") or probe.get("vision_error") or probe.get("vision_ok") is not None)
    vision_status = _component_status(
        probe.get("vision_ok"),
        probed=vision_probed,
        missing="inconclusive" if vision_probed else "not_probed",
    )
    components = [
        {"id": "models", "name": "models", "status": _component_status(probe.get("models_ok"), missing="not_probed")},
        {"id": "chat", "name": "chat", "status": _component_status(probe.get("chat_ok"), missing="not_probed")},
        {"id": "streaming", "name": "streaming", "status": _component_status(probe.get("streaming_ok"), missing="not_probed")},
        {"id": "tools", "name": "tools", "status": _component_status(probe.get("tool_calling"), missing="not_probed")},
        {"id": "tool_round_trip", "name": "tool round-trip", "status": _component_status(probe.get("tool_round_trip"), missing="not_probed")},
        {"id": "streaming_tools", "name": "streaming tools", "status": _component_status(probe.get("streaming_tool_calling"), missing="not_probed")},
        {
            "id": "vision",
            "name": "vision",
            "status": vision_status,
            "detail": probe.get("vision_error") or probe.get("vision_probe_skip_reason") or (
                f"response: {probe.get('vision_probe_response')}" if probe.get("vision_probe_response") else ""
            ),
        },
    ]
    errors = probe.get("errors") if isinstance(probe.get("errors"), list) else []
    lines = [_component_label(str(item["name"]), str(item["status"])) for item in components]
    if probe.get("classification"):
        lines.insert(0, f"classification: {probe.get('classification')}")
    if probe.get("vision_model"):
        lines.append(f"vision model: {probe.get('vision_model')}")
    if probe.get("vision_content_format"):
        lines.append(f"vision format: {probe.get('vision_content_format')}")
    if probe.get("vision_probe_response"):
        lines.append(f"vision response: {probe.get('vision_probe_response')}")
    if errors:
        lines.append("errors: " + "; ".join(str(error) for error in errors[:4]))
    return {
        "classification": str(probe.get("classification") or "unknown"),
        "components": components,
        "text": "\n".join(lines),
    }


def _classify_probe_result(result: dict[str, Any]) -> None:
    agent_ok = bool(
        result.get("chat_ok") is True
        and result.get("tool_calling") is True
        and result.get("tool_round_trip") is True
    )
    chat_only_ok = bool(result.get("chat_ok") is True and not agent_ok)
    result["agent_ok"] = agent_ok
    result["chat_only_ok"] = chat_only_ok
    result["classification"] = "agent_ready" if agent_ok else "chat_only" if chat_only_ok else "unavailable"
    # Preserve the existing meaning of ok: fully Agent-ready.
    result["ok"] = agent_ok


def _probe_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "row_bot_probe_echo",
            "description": "Echo a probe value to verify structured tool calling.",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                },
                "required": ["value"],
            },
        },
    }


def _vision_probe_decision(endpoint: dict[str, Any], model_info: ModelInfo | None, target_model: str) -> tuple[bool, str]:
    if not str(target_model or "").strip():
        return False, "no target model"
    manual = endpoint.get("manual_capabilities")
    if isinstance(manual, dict) and isinstance(manual.get("vision"), bool):
        return bool(manual.get("vision")), "manual vision capability enabled" if manual.get("vision") else "manual vision capability disabled"
    if model_info is not None:
        try:
            from row_bot.providers.capabilities import model_supports_surface

            if model_supports_surface(model_info, "vision"):
                return True, "model metadata advertises vision"
        except Exception:
            pass
    try:
        info = model_info_from_metadata(
            str(endpoint.get("provider_id") or custom_provider_id(str(endpoint.get("id") or ""))),
            target_model,
            {},
            transport=TransportMode(str(endpoint.get("transport") or TransportMode.OPENAI_CHAT.value)),
            source="custom_openai_probe",
        )
        from row_bot.providers.capabilities import model_supports_surface

        if model_supports_surface(info, "vision"):
            return True, "inferred model metadata advertises vision"
    except Exception:
        pass
    return False, "model metadata does not advertise vision"


def _should_probe_vision(endpoint: dict[str, Any], model_info: ModelInfo | None, target_model: str) -> bool:
    return _vision_probe_decision(endpoint, model_info, target_model)[0]


def _vision_probe_body(target_model: str) -> dict[str, Any]:
    return {
        "model": target_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at the image. What is the dominant color? Answer with one color word."},
                {"type": "image_url", "image_url": {"url": VISION_PROBE_IMAGE_DATA_URL}},
            ],
        }],
        "max_tokens": VISION_PROBE_MAX_TOKENS,
        "stream": False,
    }


def _extract_probe_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") if isinstance(payload, dict) else []
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if isinstance(content, str):
            if content.strip():
                return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            joined = "".join(parts)
            if joined.strip():
                return joined
        reasoning = message.get("reasoning_content") or message.get("reasoning") or first.get("reasoning_content")
        if isinstance(reasoning, str):
            return reasoning
        text = first.get("text")
        if isinstance(text, str):
            return text
    return ""


def _vision_probe_text_indicates_success(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    if lowered == "vision-ok":
        return True
    if _vision_probe_text_indicates_failure(lowered):
        return False
    return bool(re.search(r"\b(red|reddish|crimson|scarlet)\b", lowered))


def _vision_probe_text_indicates_failure(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    failure_markers = (
        "cannot see",
        "can't see",
        "cannot access",
        "can't access",
        "no image",
        "image input unsupported",
        "unsupported image",
        "does not support image",
        "text only",
    )
    return any(marker in lowered for marker in failure_markers)


def _extract_structured_tool_call(payload: dict[str, Any], expected_name: str) -> dict[str, Any] | None:
    choices = payload.get("choices") if isinstance(payload, dict) else []
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        if str(function.get("name") or "") != expected_name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return None
            if not isinstance(parsed, dict):
                return None
        elif isinstance(arguments, dict):
            function = dict(function)
            function["arguments"] = json.dumps(arguments)
            call = dict(call)
            call["function"] = function
        else:
            return None
        normalized = dict(call)
        normalized.setdefault("id", f"call_row_bot_probe_{index}")
        normalized.setdefault("type", "function")
        return normalized
    return None


def _extract_streamed_structured_tool_call(lines: Any, expected_name: str) -> dict[str, Any] | None:
    states: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for line in lines:
        payload = _decode_sse_payload(line)
        if not payload:
            continue
        choices = payload.get("choices") if isinstance(payload, dict) else []
        if isinstance(choices, list) and choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            tool_calls = delta.get("tool_calls")
        else:
            message = payload.get("message") if isinstance(payload, dict) and isinstance(payload.get("message"), dict) else {}
            tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for fallback_index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                continue
            if call.get("index") is None and len(order) == 1:
                index = order[0]
            else:
                try:
                    index = int(call.get("index") if call.get("index") is not None else fallback_index)
                except (TypeError, ValueError):
                    index = fallback_index
            if index not in states:
                states[index] = {"id": "", "type": "", "name": "", "arguments": []}
                order.append(index)
            state = states[index]
            if call.get("id") and not state["id"]:
                state["id"] = str(call.get("id"))
            if call.get("type") and not state["type"]:
                state["type"] = str(call.get("type"))
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            if function.get("name") and not state["name"]:
                state["name"] = str(function.get("name")).strip()
            if "arguments" in function:
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments if arguments is not None else {})
                state["arguments"].append(arguments)
    for index in order:
        state = states[index]
        if str(state.get("name") or "") != expected_name:
            continue
        arguments = "".join(state.get("arguments") or [])
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict) or parsed.get("value") != "ok":
            return None
        return {
            "id": str(state.get("id") or f"call_row_bot_probe_stream_{index}"),
            "type": str(state.get("type") or "function"),
            "function": {
                "name": expected_name,
                "arguments": json.dumps(parsed),
            },
        }
    return None


def _decode_sse_payload(line: Any) -> dict[str, Any] | None:
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line or "")
    text = text.strip()
    if not text:
        return None
    if text.startswith("data:"):
        text = text[5:].strip()
    if not text or text == "[DONE]":
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _stream_line_has_usable_delta(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    if text.startswith("data:"):
        text = text[5:].strip()
    if not text or text == "[DONE]":
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    choices = payload.get("choices") if isinstance(payload, dict) else []
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning") or delta.get("tool_calls"):
            return True
    message = payload.get("message") if isinstance(payload, dict) and isinstance(payload.get("message"), dict) else {}
    return bool(message.get("content") or message.get("thinking") or message.get("tool_calls"))


def _custom_endpoint_headers(endpoint: dict[str, Any]) -> dict[str, str]:
    headers = dict(endpoint.get("headers") or {})
    secret = custom_endpoint_secret(str(endpoint["provider_id"])) if endpoint.get("auth_required") else ""
    if endpoint.get("auth_required") and not secret:
        raise ValueError(custom_endpoint_auth_missing_message(endpoint))
    if not secret:
        return headers
    header_name = str(endpoint.get("api_key_header") or "Authorization")
    headers[header_name] = f"Bearer {secret}" if header_name.lower() == "authorization" else secret
    return headers


def _store_custom_endpoint_models(endpoint_or_provider_id: str, infos: list[ModelInfo]) -> None:
    endpoint_id = endpoint_id_from_provider_id(endpoint_or_provider_id)
    cfg = load_provider_config()
    endpoints = [item for item in cfg.get("custom_endpoints", []) if isinstance(item, dict)]
    stored_models = []
    for info in infos:
        entry = model_info_to_cache_entry(info)
        entry.update({
            "id": info.model_id,
            "model_id": info.model_id,
            "display_name": info.display_name,
            "context_window": info.context_window,
        })
        stored_models.append(entry)
    for item in endpoints:
        if normalize_custom_endpoint(item).get("id") == endpoint_id:
            item["models"] = stored_models
            break
    cfg["custom_endpoints"] = endpoints
    save_provider_config(cfg)


def _store_custom_endpoint_probe(endpoint_or_provider_id: str, probe: dict[str, Any]) -> None:
    endpoint_id = endpoint_id_from_provider_id(endpoint_or_provider_id)
    cfg = load_provider_config()
    endpoints = [item for item in cfg.get("custom_endpoints", []) if isinstance(item, dict)]
    for item in endpoints:
        if normalize_custom_endpoint(item).get("id") == endpoint_id:
            item["last_probe"] = dict(probe)
            break
    cfg["custom_endpoints"] = endpoints
    save_provider_config(cfg)


def model_infos_from_openai_compatible_catalog(
    endpoint: dict[str, Any],
    payload: dict[str, Any],
    *,
    native_metadata_by_model: dict[str, dict[str, Any]] | None = None,
) -> list[ModelInfo]:
    normalized = normalize_custom_endpoint(endpoint)
    try:
        transport = TransportMode(str(normalized.get("transport") or TransportMode.OPENAI_CHAT.value))
    except ValueError:
        transport = TransportMode.OPENAI_CHAT
    data = _catalog_model_items(payload)
    if not isinstance(data, list):
        return []
    results: list[ModelInfo] = []
    native_metadata_by_model = native_metadata_by_model or {}
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "")
        if not model_id:
            continue
        metadata = dict(item)
        metadata.update(native_metadata_by_model.get(model_id) or {})
        _apply_native_capability_fields(metadata)
        manual = normalized.get("manual_capabilities")
        if isinstance(manual, dict):
            metadata.update(manual)
        context_window = _metadata_context_window(metadata, fallback=_positive_int(normalized.get("unknown_context_fallback")))
        if context_window:
            metadata["context_window"] = context_window
        results.append(model_info_from_metadata(
            str(normalized["provider_id"]),
            model_id,
            metadata,
            display_name=str(item.get("name") or item.get("id") or model_id),
            context_window=context_window,
            transport=transport,
            risk_label=str(normalized.get("risk_label") or "custom_endpoint"),
            source="custom_openai_catalog",
        ))
    return results


def _catalog_model_items(payload: dict[str, Any]) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    models = payload.get("models")
    if isinstance(models, list):
        return models
    return []


def _catalog_model_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in _catalog_model_items(payload):
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
        if model_id:
            ids.append(model_id)
    return ids


_CONTEXT_METADATA_KEYS = (
    "context_length",
    "context_window",
    "max_model_len",
    "max_context_length",
    "max_input_tokens",
    "max_sequence_length",
    "context_size",
    "n_ctx",
    "num_ctx",
)
_NESTED_CONTEXT_METADATA_KEYS = ("meta", "metadata", "details", "parameters", "model_info", "config")


def _metadata_context_window(metadata: dict[str, Any], *, fallback: int = 0) -> int:
    for key in _CONTEXT_METADATA_KEYS:
        value = _positive_int(metadata.get(key))
        if value > 0:
            return value
    for key in _NESTED_CONTEXT_METADATA_KEYS:
        nested = metadata.get(key)
        if isinstance(nested, dict):
            value = _metadata_context_window(nested, fallback=0)
            if value > 0:
                return value
    return _positive_int(fallback)


def _apply_native_capability_fields(metadata: dict[str, Any]) -> None:
    max_context = _metadata_context_window(metadata)
    if max_context > 0:
        metadata.setdefault("context_window", max_context)
    for container in _capability_containers(metadata):
        if _bool_field(container, "supports_vision", "vision", "image_input", "supports_image_input", "multimodal", "has_image_understanding"):
            metadata["vision"] = True
        if _bool_field(container, "supports_audio", "audio", "has_audio_understanding"):
            metadata["audio"] = True
        if _bool_field(container, "supports_function_calling", "supports_tool_calling", "supports_tools", "trained_for_tool_use", "function_calling", "tool_calling", "tools"):
            metadata["tool_calling"] = True
        if _bool_field(container, "supports_reasoning", "reasoning", "reasoning_content", "thinking", "supports_thinking"):
            metadata["reasoning"] = True
        for key in ("input_modalities", "input", "modalities"):
            modalities = _normalize_modalities(container.get(key))
            if modalities.intersection({"image", "vision", "multimodal"}):
                metadata["vision"] = True
                _merge_input_modalities(metadata, {"image", "text"})
            if "audio" in modalities:
                metadata["audio"] = True
                _merge_input_modalities(metadata, {"audio", "text"})
        model_type = str(container.get("type") or container.get("backend") or container.get("mode") or "").strip().lower()
        if model_type in {"vlm", "mlx-vlm"} or any(marker in model_type for marker in ("vision", "llava", "vlm")):
            metadata["vision"] = True
            _merge_input_modalities(metadata, {"image", "text"})
    capabilities = metadata.get("capabilities")
    if isinstance(capabilities, dict):
        if _bool_field(capabilities, "trained_for_tool_use", "supports_function_calling", "supports_tool_calling", "tools", "tool_calling"):
            metadata["tool_calling"] = True
        if _bool_field(capabilities, "vision", "supports_vision", "multimodal", "image_input"):
            metadata["vision"] = True
            _merge_input_modalities(metadata, {"image", "text"})
        if _bool_field(capabilities, "reasoning", "supports_reasoning", "thinking"):
            metadata["reasoning"] = True
    elif isinstance(capabilities, list):
        normalized = {str(capability).lower() for capability in capabilities}
        if normalized.intersection({"tool_use", "tools", "tool_calling", "function_calling"}):
            metadata["tool_calling"] = True
        if normalized.intersection({"vision", "image", "images", "multimodal"}):
            metadata["vision"] = True
            _merge_input_modalities(metadata, {"image", "text"})
        if normalized.intersection({"reasoning", "thinking"}):
            metadata["reasoning"] = True


def _capability_containers(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [metadata]
    for key in ("model_info", "metadata", "details", "config"):
        nested = metadata.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    return containers


def _bool_field(container: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = container.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "yes", "1", "enabled"}:
            return True
    return False


def _normalize_modalities(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.strip().lower()} if value.strip() else set()
    if isinstance(value, dict):
        return {str(key).strip().lower() for key, enabled in value.items() if enabled is True}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    return set()


def _merge_input_modalities(metadata: dict[str, Any], modalities: set[str]) -> None:
    current = _normalize_modalities(metadata.get("input_modalities"))
    merged = current | {str(item).strip().lower() for item in modalities if str(item).strip()}
    if merged:
        metadata["input_modalities"] = sorted(merged)


def _normalize_tool_history_mode(value: Any) -> str:
    mode = str(value or "").strip()
    if mode in {"flatten_for_non_tool_models", "native_or_drop", "native_when_supported"}:
        return "native_required"
    return mode or "native_required"


def _fetch_custom_endpoint_native_model_metadata(
    endpoint: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    model_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    profile = str(endpoint.get("profile") or "").lower()
    import httpx

    root_url = _native_root_url(str(endpoint.get("base_url") or ""))
    if not root_url:
        return {}
    if profile == "llama_cpp":
        try:
            response = httpx.get(f"{root_url}/props", headers=headers or {}, timeout=10)
            response.raise_for_status()
            metadata = _llamacpp_props_metadata_by_id(response.json())
            if metadata:
                return metadata
        except Exception:
            logger.debug("llama.cpp props metadata fetch failed for %s/props", root_url, exc_info=True)
        return {}
    if profile == "litellm":
        for root in _metadata_roots(root_url, str(endpoint.get("base_url") or "")):
            for path in ("/model_group/info", "/model/info"):
                try:
                    response = httpx.get(f"{root}{path}", headers=headers or {}, timeout=10)
                    response.raise_for_status()
                    metadata = _litellm_native_models_by_id(response.json())
                    if metadata:
                        return metadata
                except Exception:
                    logger.debug("LiteLLM native metadata fetch failed for %s%s", root, path, exc_info=True)
        return {}
    if profile == "sglang":
        try:
            response = httpx.get(f"{root_url}/get_model_info", headers=headers or {}, timeout=10)
            response.raise_for_status()
            metadata = _sglang_model_info_metadata_by_id(response.json(), model_ids=model_ids or [])
            if metadata:
                return metadata
        except Exception:
            logger.debug("SGLang model metadata fetch failed for %s/get_model_info", root_url, exc_info=True)
        return {}
    if profile == "localai":
        metadata: dict[str, dict[str, Any]] = {}
        for model_id in model_ids or []:
            try:
                encoded = quote(model_id, safe="")
                response = httpx.get(f"{root_url}/api/models/config-json/{encoded}", headers=headers or {}, timeout=10)
                response.raise_for_status()
                item = response.json()
                if isinstance(item, dict):
                    model_metadata = dict(item)
                    _apply_native_capability_fields(model_metadata)
                    metadata[model_id] = model_metadata
            except Exception:
                logger.debug("LocalAI model config metadata fetch failed for %s", model_id, exc_info=True)
        return metadata
    if profile != "lmstudio":
        return {}
    for path in ("/api/v1/models", "/api/v0/models"):
        try:
            response = httpx.get(f"{root_url}{path}", headers=headers or {}, timeout=10)
            response.raise_for_status()
            metadata = _lmstudio_native_models_by_id(response.json())
            if metadata:
                return metadata
        except Exception:
            logger.debug("LM Studio native metadata fetch failed for %s%s", root_url, path, exc_info=True)
    return {}


def _metadata_roots(root_url: str, base_url: str) -> list[str]:
    roots: list[str] = []
    for value in (root_url, str(base_url or "").rstrip("/")):
        if value and value not in roots:
            roots.append(value)
    return roots


def _native_root_url(base_url: str) -> str:
    value = str(base_url or "").rstrip("/")
    for suffix in ("/v1", "/api/v1", "/api/v0"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _llamacpp_props_metadata_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    model_id = str(payload.get("model_alias") or "").strip()
    if not model_id:
        return {}
    metadata: dict[str, Any] = {}
    modalities = payload.get("modalities")
    if isinstance(modalities, dict):
        metadata["modalities"] = dict(modalities)
        if modalities.get("vision") is True:
            metadata["vision"] = True
            metadata["input_modalities"] = ["text", "image"]
        if modalities.get("audio") is True:
            metadata["audio"] = True
    settings = payload.get("default_generation_settings")
    if isinstance(settings, dict):
        metadata.update(settings)
        params = settings.get("params")
        if isinstance(params, dict):
            metadata.update({key: value for key, value in params.items() if key in _CONTEXT_METADATA_KEYS})
    for key in _CONTEXT_METADATA_KEYS:
        if key in payload:
            metadata[key] = payload[key]
    context_window = _metadata_context_window(metadata)
    if context_window:
        metadata["context_window"] = context_window
    _apply_native_capability_fields(metadata)
    return {model_id: metadata} if metadata else {}


def _lmstudio_native_models_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("key") or "").strip()
        if not model_id:
            continue
        metadata = dict(item)
        _apply_native_capability_fields(metadata)
        result[model_id] = metadata
    return result


def _litellm_native_models_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        metadata = dict(item)
        model_info = item.get("model_info")
        if isinstance(model_info, dict):
            metadata.update(model_info)
            metadata["model_info"] = dict(model_info)
        litellm_params = item.get("litellm_params")
        if isinstance(litellm_params, dict):
            metadata.setdefault("litellm_params", dict(litellm_params))
        _apply_native_capability_fields(metadata)
        aliases = {
            str(item.get("model_group") or "").strip(),
            str(item.get("model_name") or "").strip(),
            str(item.get("id") or "").strip(),
        }
        if isinstance(model_info, dict):
            aliases.add(str(model_info.get("key") or "").strip())
        if isinstance(litellm_params, dict):
            aliases.add(str(litellm_params.get("model") or "").strip())
        for alias in {alias for alias in aliases if alias}:
            result[alias] = dict(metadata)
    return result


def _sglang_model_info_metadata_by_id(
    payload: dict[str, Any],
    *,
    model_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    metadata = dict(payload)
    _apply_native_capability_fields(metadata)
    aliases = {
        str(payload.get("model_path") or "").strip(),
        str(payload.get("tokenizer_path") or "").strip(),
        str(payload.get("served_model_name") or "").strip(),
    }
    for model_id in model_ids:
        if len(model_ids) == 1:
            aliases.add(str(model_id).strip())
    return {alias: dict(metadata) for alias in aliases if alias and metadata}
