from __future__ import annotations

from typing import Any

from row_bot.providers.capabilities import endpoint_values
from row_bot.providers.models import AuthMethod, ModelInfo, ModelModality, ModelTask, ProviderDefinition, TransportMode

PROVIDER_DEFINITIONS: dict[str, ProviderDefinition] = {
    "ollama": ProviderDefinition(
        id="ollama",
        display_name="Ollama Local",
        auth_methods=(AuthMethod.NONE,),
        default_transport=TransportMode.OLLAMA_CHAT,
        base_url="http://127.0.0.1:11434",
        risk_label="local_private",
        icon="🖥️",
    ),
    "ollama_cloud": ProviderDefinition(
        id="ollama_cloud",
        display_name="Ollama Cloud",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.OLLAMA_CLOUD_CHAT,
        base_url="https://ollama.com",
        risk_label="cloud_provider",
        icon="☁️",
    ),
    "openai": ProviderDefinition(
        id="openai",
        display_name="OpenAI API",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.OPENAI_CHAT,
        base_url="https://api.openai.com/v1",
        icon="⬡",
    ),
    "codex": ProviderDefinition(
        id="codex",
        display_name="ChatGPT / Codex",
        auth_methods=(AuthMethod.EXTERNAL_CLI, AuthMethod.OAUTH_DEVICE),
        default_transport=TransportMode.OPENAI_RESPONSES,
        risk_label="subscription",
        experimental=True,
        icon="C",
    ),
    "claude_subscription": ProviderDefinition(
        id="claude_subscription",
        display_name="Claude Subscription",
        auth_methods=(AuthMethod.EXTERNAL_CLI, AuthMethod.OAUTH_PKCE),
        default_transport=TransportMode.ANTHROPIC_MESSAGES,
        risk_label="subscription",
        experimental=True,
        icon="C",
    ),
    "openrouter": ProviderDefinition(
        id="openrouter",
        display_name="OpenRouter",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.OPENAI_CHAT,
        base_url="https://openrouter.ai/api/v1",
        risk_label="third_party_router",
        icon="🌐",
    ),
    "requesty": ProviderDefinition(
        id="requesty",
        display_name="Requesty",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.OPENAI_CHAT,
        base_url="https://router.requesty.ai/v1",
        risk_label="third_party_router",
        icon="🛰️",
    ),
    "opencode_zen": ProviderDefinition(
        id="opencode_zen",
        display_name="OpenCode Zen",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.OPENAI_CHAT,
        base_url="https://opencode.ai/zen/v1",
        risk_label="cloud_provider",
        icon="OZ",
    ),
    "opencode_go": ProviderDefinition(
        id="opencode_go",
        display_name="OpenCode Go",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.OPENAI_CHAT,
        base_url="https://opencode.ai/zen/go/v1",
        risk_label="cloud_provider",
        icon="OG",
    ),
    "atlascloud": ProviderDefinition(
        id="atlascloud",
        display_name="Atlas Cloud",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.OPENAI_CHAT,
        base_url="https://api.atlascloud.ai/v1",
        risk_label="cloud_provider",
        icon="AC",
    ),
    "anthropic": ProviderDefinition(
        id="anthropic",
        display_name="Anthropic API",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.ANTHROPIC_MESSAGES,
        base_url="https://api.anthropic.com/v1",
        icon="🔶",
    ),
    "google": ProviderDefinition(
        id="google",
        display_name="Google AI API",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.GOOGLE_GENAI,
        base_url="https://generativelanguage.googleapis.com/v1beta",
        icon="💎",
    ),
    "xai": ProviderDefinition(
        id="xai",
        display_name="xAI API",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.OPENAI_CHAT,
        base_url="https://api.x.ai/v1",
        icon="X",
    ),
    "xai_oauth": ProviderDefinition(
        id="xai_oauth",
        display_name="xAI Grok",
        auth_methods=(AuthMethod.OAUTH_PKCE,),
        default_transport=TransportMode.OPENAI_RESPONSES,
        base_url="https://api.x.ai/v1",
        risk_label="subscription",
        experimental=True,
        icon="X",
    ),
    "minimax": ProviderDefinition(
        id="minimax",
        display_name="MiniMax API",
        auth_methods=(AuthMethod.API_KEY,),
        default_transport=TransportMode.ANTHROPIC_MESSAGES,
        base_url="https://api.minimax.io/anthropic",
        icon="M",
    ),
}

_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
_OPENAI_VISION_PREFIXES = ("gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4")
_ANTHROPIC_VISION_PREFIXES = ("claude-3", "claude-sonnet-4", "claude-opus-4")
_LOCAL_VISION_MARKERS = (
    "bakllava",
    "gemma-3",
    "gemma3",
    "llama3.2-vision",
    "llava",
    "minicpm-v",
    "moondream",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "qwen3.5-vl",
)
_VOICE_MODEL_MARKERS = (
    "audio",
    "audio-preview",
    "gpt-realtime",
    "realtime",
    "speech",
    "transcribe",
    "transcri",
    "transcription",
    "tts",
    "whisper",
)


def _name_suggests_vision_model(model_id: str) -> bool:
    normalized = str(model_id or "").split(":", 1)[0].split("/", 1)[-1].lower().replace("_", "-")
    return any(marker in normalized for marker in _LOCAL_VISION_MARKERS)


def _name_suggests_voice_model(model_id: str) -> bool:
    normalized = str(model_id or "").split(":", 1)[0].split("/", 1)[-1].lower().replace("_", "-")
    return any(marker in normalized for marker in _VOICE_MODEL_MARKERS)


def _metadata_suggests_image_input(metadata: dict[str, Any]) -> bool:
    capabilities = metadata.get("capabilities")
    if isinstance(capabilities, list) and any(str(item).lower() in {"vision", "image"} for item in capabilities):
        return True
    for key in ("input_modalities", "input", "modalities"):
        modalities = metadata.get(key)
        if isinstance(modalities, list) and any(str(item).lower() == "image" for item in modalities):
            return True
    architecture = metadata.get("architecture")
    if isinstance(architecture, dict):
        for key in ("input_modalities", "input", "modalities"):
            modalities = architecture.get(key)
            if isinstance(modalities, list) and any(str(item).lower() == "image" for item in modalities):
                return True
    return False


def list_provider_definitions() -> list[ProviderDefinition]:
    definitions = list(PROVIDER_DEFINITIONS.values())
    try:
        from row_bot.providers.custom import list_custom_provider_definitions
        definitions.extend(list_custom_provider_definitions())
    except Exception:
        pass
    return definitions


def get_provider_definition(provider_id: str) -> ProviderDefinition | None:
    return PROVIDER_DEFINITIONS.get(provider_id)


def infer_provider_id(model_id: str, cached_provider: str | None = None) -> str | None:
    if cached_provider:
        return cached_provider
    model_id = str(model_id or "")
    if "/" in model_id:
        return "openrouter"
    bare = model_id.split("/")[-1]
    if _name_suggests_voice_model(bare):
        return "openai"
    if any(bare.startswith(prefix) for prefix in _OPENAI_CHAT_PREFIXES):
        return "openai"
    if bare.startswith("claude"):
        return "anthropic"
    if bare.startswith("gemini"):
        return "google"
    if bare.startswith("grok"):
        return "xai"
    if bare.lower().startswith("minimax"):
        return "minimax"
    return None


def split_model_cache_key(model_id: str) -> tuple[str | None, str]:
    """Return ``(provider_id, runtime_model_id)`` for legacy or provider-ref cache keys."""
    raw = str(model_id or "").strip()
    parts = raw.split(":", 2)
    if len(parts) == 3 and parts[0] == "model" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None, raw


def _str_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value if str(item)}
    return set()


def _transport_from_value(value: Any, fallback: TransportMode) -> TransportMode:
    if isinstance(value, TransportMode):
        return value
    try:
        return TransportMode(str(value))
    except Exception:
        return fallback


def _transport_set_from_values(value: Any, fallback: frozenset[TransportMode]) -> frozenset[TransportMode]:
    modes: set[TransportMode] = set()
    for item in _str_set(value):
        try:
            modes.add(TransportMode(item))
        except Exception:
            continue
    return frozenset(modes) or fallback


def model_info_from_legacy(model_id: str, info: dict[str, Any]) -> ModelInfo | None:
    key_provider_id, runtime_model_id = split_model_cache_key(model_id)
    provider_id = infer_provider_id(runtime_model_id, info.get("provider") or key_provider_id)
    if not provider_id:
        return None
    definition = get_provider_definition(provider_id)
    transport = definition.default_transport if definition else TransportMode.OPENAI_CHAT
    classified = classify_model_capabilities(provider_id, model_id, info, transport=transport)
    snapshot = info.get("capabilities_snapshot") if isinstance(info.get("capabilities_snapshot"), dict) else {}

    capabilities = _str_set(snapshot.get("capabilities")) or set(classified["capabilities"])
    if info.get("vision"):
        capabilities.add("vision")
    input_modalities = _str_set(snapshot.get("input_modalities")) or set(classified["input_modalities"])
    output_modalities = _str_set(snapshot.get("output_modalities")) or set(classified["output_modalities"])
    tasks = _str_set(snapshot.get("tasks")) or set(classified["tasks"])
    if "tool_calling" in snapshot:
        tool_calling = snapshot.get("tool_calling")
    else:
        tool_calling = classified["tool_calling"]
    if tool_calling is True:
        capabilities.add("tool_calling")
    elif tool_calling is False:
        capabilities.discard("tool_calling")
    if "streaming" in snapshot:
        streaming = snapshot.get("streaming")
    else:
        streaming = classified["streaming"]
    if streaming is True:
        capabilities.add("streaming")
    elif streaming is False:
        capabilities.discard("streaming")
    resolved_transport = _transport_from_value(snapshot.get("transport") or info.get("transport"), classified["transport"])
    endpoint_compatibility = _transport_set_from_values(
        snapshot.get("endpoint_compatibility") or info.get("endpoint_compatibility"),
        frozenset(classified["endpoint_compatibility"]),
    )
    return ModelInfo(
        provider_id=provider_id,
        model_id=runtime_model_id,
        display_name=str(info.get("label") or runtime_model_id),
        context_window=int(info.get("ctx") or 0),
        transport=resolved_transport,
        capabilities=frozenset(capabilities),
        input_modalities=frozenset(input_modalities),
        output_modalities=frozenset(output_modalities),
        tasks=frozenset(tasks),
        tool_calling=tool_calling if tool_calling in (True, False) else None,
        streaming=streaming if streaming in (True, False) else None,
        endpoint_compatibility=endpoint_compatibility,
        billing_label=str(snapshot.get("billing_label") or info.get("billing_label") or ""),
        source_confidence=str(snapshot.get("source_confidence") or info.get("source_confidence") or "inferred"),
        last_verified_at=str(snapshot.get("last_verified_at") or info.get("last_verified_at") or ""),
        risk_label=str(info.get("risk_label") or (definition.risk_label if definition else "api_key")),
        source=str(info.get("source") or "legacy_cloud_cache"),
        reasoning=snapshot.get("reasoning") if isinstance(snapshot.get("reasoning"), dict) else None,
    )


def model_info_from_metadata(
    provider_id: str,
    model_id: str,
    metadata: dict[str, Any] | None = None,
    *,
    display_name: str | None = None,
    context_window: int = 0,
    transport: TransportMode | None = None,
    risk_label: str | None = None,
    source: str = "provider_catalog",
) -> ModelInfo:
    definition = get_provider_definition(provider_id)
    classified = classify_model_capabilities(provider_id, model_id, metadata, transport=transport)
    from row_bot.providers.reasoning import reasoning_metadata_for_catalog

    reasoning = reasoning_metadata_for_catalog(provider_id, model_id, metadata)
    return ModelInfo(
        provider_id=provider_id,
        model_id=model_id,
        display_name=display_name or model_id,
        context_window=int(context_window or 0),
        transport=classified["transport"],
        capabilities=frozenset(classified["capabilities"]),
        input_modalities=frozenset(classified["input_modalities"]),
        output_modalities=frozenset(classified["output_modalities"]),
        tasks=frozenset(classified["tasks"]),
        tool_calling=classified["tool_calling"],
        streaming=classified["streaming"],
        endpoint_compatibility=frozenset(classified["endpoint_compatibility"]),
        risk_label=risk_label or (definition.risk_label if definition else "api_key"),
        source=source,
        reasoning=reasoning,
    )


def model_info_to_cache_entry(model_info: ModelInfo) -> dict[str, Any]:
    snapshot = model_info.capability_snapshot()
    return {
        "label": model_info.display_name,
        "ctx": model_info.context_window,
        "provider": model_info.provider_id,
        "vision": "image" in model_info.input_modalities,
        "capabilities_snapshot": snapshot,
        "transport": model_info.transport.value,
        "risk_label": model_info.risk_label,
        "source": model_info.source,
    }


def legacy_cache_to_model_infos(cache: dict[str, dict[str, Any]]) -> list[ModelInfo]:
    infos: list[ModelInfo] = []
    for model_id, info in cache.items():
        model_info = model_info_from_legacy(model_id, info)
        if model_info:
            infos.append(model_info)
    return infos


def classify_model_capabilities(
    provider_id: str,
    model_id: str,
    metadata: dict[str, Any] | None = None,
    *,
    transport: TransportMode | None = None,
) -> dict[str, Any]:
    metadata = metadata or {}
    bare = str(model_id or "").split("/")[-1].lower()
    lower = bare.replace("_", "-")
    default_transport = transport or (get_provider_definition(provider_id).default_transport if get_provider_definition(provider_id) else TransportMode.OPENAI_CHAT)
    upstream = str(model_id or "").split("/", 1)[0].lower() if "/" in str(model_id or "") else provider_id
    tasks: set[str] = {ModelTask.CHAT.value}
    input_modalities: set[str] = {ModelModality.TEXT.value}
    output_modalities: set[str] = {ModelModality.TEXT.value}
    capabilities: set[str] = {"text", "chat"}
    tool_calling: bool | None = None if provider_id.startswith("custom_openai_") else True
    streaming: bool | None = True
    endpoint_compatibility = {default_transport}
    if isinstance(metadata.get("tool_calling"), bool):
        tool_calling = bool(metadata.get("tool_calling"))
    if isinstance(metadata.get("streaming"), bool):
        streaming = bool(metadata.get("streaming"))

    if provider_id == "atlascloud":
        from row_bot.providers.atlascloud import (
            atlascloud_input_modalities,
            atlascloud_is_media_or_non_chat_model,
            atlascloud_output_modalities,
            atlascloud_tool_calling,
        )

        default_transport = TransportMode.OPENAI_CHAT
        endpoint_compatibility = {TransportMode.OPENAI_CHAT}
        streaming = metadata.get("streaming") if isinstance(metadata.get("streaming"), bool) else True
        tool_calling = atlascloud_tool_calling(model_id, metadata)
        if atlascloud_is_media_or_non_chat_model(model_id, metadata):
            return {
                "capabilities": set(),
                "input_modalities": {ModelModality.TEXT.value},
                "output_modalities": set(),
                "tasks": set(),
                "tool_calling": False,
                "streaming": streaming,
                "endpoint_compatibility": endpoint_values(endpoint_compatibility),
                "transport": default_transport,
            }
        input_modalities = atlascloud_input_modalities(model_id, metadata)
        output_modalities = atlascloud_output_modalities(metadata)
        capabilities = {"text", "chat"}
        if ModelModality.IMAGE.value in input_modalities:
            capabilities.add("vision")
        if tool_calling is True:
            capabilities.add("tool_calling")
        if streaming is True:
            capabilities.add("streaming")
        return {
            "capabilities": capabilities,
            "input_modalities": input_modalities,
            "output_modalities": output_modalities,
            "tasks": tasks,
            "tool_calling": tool_calling,
            "streaming": streaming,
            "endpoint_compatibility": endpoint_values(endpoint_compatibility),
            "transport": default_transport,
        }

    if provider_id in {"ollama", "ollama_cloud"}:
        default_transport = TransportMode.OLLAMA_CLOUD_CHAT if provider_id == "ollama_cloud" else TransportMode.OLLAMA_CHAT
        endpoint_compatibility = {default_transport}
        family = bare.split(":", 1)[0]
        if metadata.get("vision") or _metadata_suggests_image_input(metadata) or _name_suggests_vision_model(family):
            input_modalities.add(ModelModality.IMAGE.value)
            capabilities.add("vision")
        if metadata.get("tool_calling") is False:
            tool_calling = False
        if metadata.get("embedding") or "embed" in lower:
            tasks = {ModelTask.EMBEDDING.value}
            capabilities = {"text", "embedding"}
            tool_calling = False

    if provider_id == "openai" and bare.startswith("gpt-5"):
        default_transport = TransportMode.OPENAI_RESPONSES
        tasks = {ModelTask.RESPONSES.value}
        endpoint_compatibility = {TransportMode.OPENAI_RESPONSES}
    if provider_id == "codex":
        default_transport = TransportMode.OPENAI_RESPONSES
        tasks = {ModelTask.RESPONSES.value}
        endpoint_compatibility = {TransportMode.OPENAI_RESPONSES}
    if provider_id == "xai_oauth":
        default_transport = TransportMode.OPENAI_RESPONSES
        tasks = {ModelTask.RESPONSES.value}
        endpoint_compatibility = {TransportMode.OPENAI_RESPONSES}
        tool_calling = metadata.get("tool_calling") if isinstance(metadata.get("tool_calling"), bool) else True
        streaming = metadata.get("streaming") if isinstance(metadata.get("streaming"), bool) else True
        for key in ("input_modalities", "input", "modalities"):
            modalities = metadata.get(key)
            if isinstance(modalities, str):
                modalities = [modalities]
            if isinstance(modalities, (list, tuple, set, frozenset)):
                normalized = {str(item).strip().lower() for item in modalities}
                if "image" in normalized:
                    input_modalities.add(ModelModality.IMAGE.value)
                    capabilities.add("vision")
        capabilities_payload = metadata.get("capabilities")
        if isinstance(capabilities_payload, dict):
            for key in ("image_input", "vision", "image"):
                value = capabilities_payload.get(key)
                if value is True or (isinstance(value, dict) and value.get("supported")):
                    input_modalities.add(ModelModality.IMAGE.value)
                    capabilities.add("vision")
        elif isinstance(capabilities_payload, list):
            if any(str(value).strip().lower() in {"image", "vision", "image_input"} for value in capabilities_payload):
                input_modalities.add(ModelModality.IMAGE.value)
                capabilities.add("vision")
        if "reasoning" in lower or lower.startswith("grok-4"):
            capabilities.add("reasoning")
    if provider_id == "claude_subscription":
        default_transport = TransportMode.ANTHROPIC_MESSAGES
        endpoint_compatibility = {TransportMode.ANTHROPIC_MESSAGES}
        tool_calling = metadata.get("tool_calling") if isinstance(metadata.get("tool_calling"), bool) else True
        streaming = metadata.get("streaming") if isinstance(metadata.get("streaming"), bool) else True
        for key in ("input_modalities", "input", "modalities"):
            modalities = metadata.get(key)
            if isinstance(modalities, str):
                modalities = [modalities]
            if isinstance(modalities, (list, tuple, set, frozenset)):
                normalized = {str(item).strip().lower() for item in modalities}
                if "image" in normalized:
                    input_modalities.add(ModelModality.IMAGE.value)
                    capabilities.add("vision")
    if provider_id == "minimax":
        default_transport = TransportMode.ANTHROPIC_MESSAGES
        endpoint_compatibility = {TransportMode.ANTHROPIC_MESSAGES}
        tool_calling = metadata.get("tool_calling") if isinstance(metadata.get("tool_calling"), bool) else True
        streaming = metadata.get("streaming") if isinstance(metadata.get("streaming"), bool) else True
        for key in ("input_modalities", "input", "modalities"):
            modalities = metadata.get(key)
            if isinstance(modalities, str):
                modalities = [modalities]
            if isinstance(modalities, (list, tuple, set, frozenset)):
                normalized = {str(item).strip().lower() for item in modalities}
                if "image" in normalized:
                    input_modalities.add(ModelModality.IMAGE.value)
                    capabilities.add("vision")
                if "video" in normalized:
                    input_modalities.add(ModelModality.VIDEO.value)
                    capabilities.add("video_input")
        if lower.startswith("minimax-m3"):
            input_modalities.add(ModelModality.IMAGE.value)
            input_modalities.add(ModelModality.VIDEO.value)
            capabilities.add("vision")
            capabilities.add("video_input")
        if lower.startswith("minimax-m"):
            capabilities.add("thinking")

    if isinstance(metadata.get("vision"), bool):
        inferred_vision = bool(metadata.get("vision"))
    else:
        inferred_vision = (
            (provider_id in {"openai", "codex"} and bare.startswith(_OPENAI_VISION_PREFIXES))
            or (provider_id == "google" and bare.startswith("gemini"))
            or (provider_id == "anthropic" and bare.startswith(_ANTHROPIC_VISION_PREFIXES))
            or (provider_id.startswith("custom_openai_") and _name_suggests_vision_model(model_id))
            or (
                provider_id == "openrouter"
                and (
                    (upstream == "google" and bare.startswith("gemini"))
                    or (upstream == "anthropic" and bare.startswith(_ANTHROPIC_VISION_PREFIXES))
                    or (upstream == "openai" and bare.startswith(_OPENAI_VISION_PREFIXES))
                )
            )
        )

    if inferred_vision:
        input_modalities.add(ModelModality.IMAGE.value)
        capabilities.add("vision")

    if any(part in lower for part in ("embed", "embedding")):
        tasks = {ModelTask.EMBEDDING.value}
        capabilities = {"text", "embedding"}
        tool_calling = False
    elif any(part in lower for part in ("dall-e", "gpt-image", "imagen", "image-generation")) or (
        provider_id == "google" and bare.startswith("gemini") and "image" in lower
    ) or (provider_id in {"xai", "xai_oauth"} and "grok-imagine" in lower and "image" in lower):
        tasks = {ModelTask.IMAGE_GENERATION.value}
        if provider_id == "google" and bare.startswith("gemini"):
            tasks.add(ModelTask.IMAGE_EDIT.value)
            input_modalities.add(ModelModality.IMAGE.value)
        output_modalities = {ModelModality.IMAGE.value}
        capabilities = {"image_generation"}
        if ModelTask.IMAGE_EDIT.value in tasks:
            capabilities.add("image_edit")
        tool_calling = False
        streaming = False
    elif any(part in lower for part in ("veo", "video")):
        tasks = {ModelTask.VIDEO_GENERATION.value}
        output_modalities = {ModelModality.VIDEO.value}
        capabilities = {"video_generation"}
        tool_calling = False
        streaming = False
    elif any(part in lower for part in ("whisper", "transcri", "transcribe", "transcription")):
        tasks = {ModelTask.TRANSCRIPTION.value}
        input_modalities = {ModelModality.AUDIO.value}
        output_modalities = {ModelModality.TEXT.value}
        capabilities = {"transcription"}
        tool_calling = False
    elif "tts" in lower:
        tasks = {ModelTask.TTS.value}
        input_modalities = {ModelModality.TEXT.value}
        output_modalities = {ModelModality.AUDIO.value}
        capabilities = {"tts"}
        tool_calling = False
    elif "moderation" in lower:
        tasks = {ModelTask.MODERATION.value}
        capabilities = {"moderation"}
        tool_calling = False
    elif "realtime" in lower:
        tasks = {ModelTask.REALTIME.value}
        input_modalities.add(ModelModality.AUDIO.value)
        output_modalities.add(ModelModality.AUDIO.value)
        capabilities.add("realtime")
        tool_calling = False
    elif "audio" in lower or "speech" in lower:
        tasks = {ModelTask.REALTIME.value}
        input_modalities.add(ModelModality.AUDIO.value)
        output_modalities.add(ModelModality.AUDIO.value)
        capabilities = {"audio", "realtime"}
        tool_calling = False

    if provider_id == "openai" and any(part in lower for part in ("davinci", "babbage", "curie", "text-ada", "instruct")):
        tasks = set()
        capabilities = {"legacy_completion"}
        tool_calling = False

    if provider_id == "google":
        methods = metadata.get("supportedGenerationMethods") or metadata.get("supported_generation_methods") or []
        if methods and "generateContent" not in methods:
            tasks.discard(ModelTask.CHAT.value)
    if provider_id == "openrouter":
        architecture = metadata.get("architecture") if isinstance(metadata.get("architecture"), dict) else {}
        modality = str(architecture.get("modality") or "")
        if "image" in modality or _metadata_suggests_image_input(metadata):
            input_modalities.add(ModelModality.IMAGE.value)
            capabilities.add("vision")
        tool_calling = None
        if "supported_parameters" in metadata:
            supported = metadata.get("supported_parameters") or []
            tool_calling = any(param in supported for param in ("tools", "tool_choice"))
    if provider_id == "requesty":
        from row_bot.providers.requesty import requesty_supports_vision, requesty_tool_calling

        # Requesty exposes the same provider/model IDs as OpenRouter but reports
        # capabilities via supports_tool_calling / supports_vision booleans.
        vision = requesty_supports_vision(metadata)
        if vision is True or (vision is None and _metadata_suggests_image_input(metadata)):
            input_modalities.add(ModelModality.IMAGE.value)
            capabilities.add("vision")
        tool_calling = requesty_tool_calling(metadata)

    if tool_calling:
        capabilities.add("tool_calling")
    if streaming:
        capabilities.add("streaming")

    return {
        "capabilities": capabilities,
        "input_modalities": input_modalities,
        "output_modalities": output_modalities,
        "tasks": tasks,
        "tool_calling": tool_calling,
        "streaming": streaming,
        "endpoint_compatibility": endpoint_values(endpoint_compatibility),
        "transport": default_transport,
    }
