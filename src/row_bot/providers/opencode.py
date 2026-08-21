from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from row_bot.providers.models import ModelInfo, ModelModality, ModelTask, TransportMode
from row_bot.providers.selection import model_ref

OPENCODE_ZEN_PROVIDER_ID = "opencode_zen"
OPENCODE_GO_PROVIDER_ID = "opencode_go"
OPENCODE_PROVIDER_IDS = frozenset({OPENCODE_ZEN_PROVIDER_ID, OPENCODE_GO_PROVIDER_ID})

OPENCODE_ZEN_BASE_URL = "https://opencode.ai/zen/v1"
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
OPENCODE_MODELS_DEV_URL = "https://models.dev/api.json"

_MODELS_DEV_PROVIDER_IDS = {
    OPENCODE_ZEN_PROVIDER_ID: "opencode",
    OPENCODE_GO_PROVIDER_ID: "opencode-go",
}
_NATIVE_TRANSPORTS = {
    "@ai-sdk/openai-compatible": TransportMode.OPENAI_CHAT,
    "@ai-sdk/openai": TransportMode.OPENAI_RESPONSES,
    "@ai-sdk/anthropic": TransportMode.ANTHROPIC_MESSAGES,
    "@ai-sdk/google": TransportMode.GOOGLE_GENAI,
}
_SUPPORTED_OPENCODE_TRANSPORTS = frozenset(_NATIVE_TRANSPORTS.values())


class OpenCodeUnsupportedRouteError(ValueError):
    """Raised when an OpenCode model is known but intentionally unsupported."""


@dataclass(frozen=True)
class OpenCodeRegistryProvider:
    provider_id: str
    registry_provider_id: str
    default_package: str
    default_transport: TransportMode | None
    models: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class OpenCodeModelRoute:
    provider_id: str
    model_id: str
    display_name: str
    transport: TransportMode
    context_window: int
    tool_calling: bool | None = True
    streaming: bool | None = True
    unsupported_reason: str = ""
    image_input: bool | None = None
    input_modalities: frozenset[str] | None = None
    output_modalities: frozenset[str] | None = None
    reasoning: bool | None = None

    @property
    def selection_ref(self) -> str:
        return model_ref(self.provider_id, self.model_id)


_STALE_MODELS: dict[tuple[str, str], str] = {
    (OPENCODE_ZEN_PROVIDER_ID, "deepseek-v3.2"): "OpenCode Zen no longer lists deepseek-v3.2; use deepseek-v4-flash-free.",
    (OPENCODE_GO_PROVIDER_ID, "deepseek-v3.2"): "OpenCode Go no longer lists deepseek-v3.2; use deepseek-v4-pro or deepseek-v4-flash.",
    (OPENCODE_ZEN_PROVIDER_ID, "mimo-v2.5-pro"): "OpenCode Zen no longer lists mimo-v2.5-pro; this model is available through OpenCode Go.",
    (OPENCODE_ZEN_PROVIDER_ID, "codex-mini-latest"): "OpenCode Zen no longer lists codex-mini-latest; use a current gpt-*-codex model.",
}

_ZEN_STATIC_FALLBACK_IDS: tuple[str, ...] = (
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-opus-4-1",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4",
    "claude-haiku-4-5",
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.4",
    "gpt-5.4-pro",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.3-codex-spark",
    "gpt-5.3-codex",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-5.1",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "gpt-5",
    "gpt-5-codex",
    "gpt-5-nano",
    "grok-build-0.1",
    "glm-5.1",
    "glm-5",
    "minimax-m2.7",
    "minimax-m2.5",
    "kimi-k2.6",
    "kimi-k2.5",
    "qwen3.6-plus",
    "qwen3.5-plus",
    "big-pickle",
    "deepseek-v4-flash-free",
    "mimo-v2.5-free",
    "nemotron-3-super-free",
)

_GO_STATIC_FALLBACK_IDS: tuple[str, ...] = (
    "minimax-m2.7",
    "minimax-m2.5",
    "kimi-k2.6",
    "kimi-k2.5",
    "glm-5.1",
    "glm-5",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "qwen3.7-max",
    "qwen3.6-plus",
    "qwen3.5-plus",
    "mimo-v2-pro",
    "mimo-v2-omni",
    "mimo-v2.5-pro",
    "mimo-v2.5",
)

_ZEN_CHAT_EXACT = {
    "big-pickle",
    "deepseek-v4-flash-free",
    "grok-build-0.1",
    "nemotron-3-super-free",
}
_ZEN_CHAT_PREFIXES = ("glm-", "kimi-", "minimax-", "mimo-")
_GO_CHAT_PREFIXES = ("glm-", "kimi-", "deepseek-", "mimo-")
_GO_MESSAGES_PREFIXES = ("minimax-", "qwen3.")

_CONTEXT_BY_PREFIX: tuple[tuple[str, int], ...] = (
    ("gpt-5.5", 400_000),
    ("gpt-5.4", 400_000),
    ("gpt-5.3", 400_000),
    ("gpt-5.2", 400_000),
    ("gpt-5.1", 400_000),
    ("gpt-5", 400_000),
    ("claude-", 200_000),
    ("minimax-", 204_800),
    ("gemini-", 1_048_576),
)


_MODELS_DEV_IMAGE_INPUT_IDS: dict[str, frozenset[str]] = {
    OPENCODE_ZEN_PROVIDER_ID: frozenset({
        "claude-3-5-haiku",
        "claude-haiku-4-5",
        "claude-opus-4-1",
        "claude-opus-4-5",
        "claude-opus-4-6",
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-sonnet-4",
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "gpt-5",
        "gpt-5-codex",
        "gpt-5-nano",
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5.1-codex-max",
        "gpt-5.1-codex-mini",
        "gpt-5.2",
        "gpt-5.2-codex",
        "gpt-5.3-codex",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.4-pro",
        "gpt-5.5",
        "gpt-5.5-pro",
        "grok-build-0.1",
        "kimi-k2.5",
        "kimi-k2.5-free",
        "kimi-k2.6",
        "mimo-v2-omni-free",
        "mimo-v2.5-free",
        "qwen3.5-plus",
        "qwen3.6-plus",
        "qwen3.6-plus-free",
    }),
    OPENCODE_GO_PROVIDER_ID: frozenset({
        "kimi-k2.5",
        "kimi-k2.6",
        "mimo-v2-omni",
        "mimo-v2.5",
        "qwen3.5-plus",
        "qwen3.6-plus",
    }),
}


def _route_supports_image_input(route: OpenCodeModelRoute) -> bool:
    if route.unsupported_reason:
        return False
    if route.input_modalities is not None:
        return ModelModality.IMAGE.value in route.input_modalities
    if route.image_input is not None:
        return route.image_input
    lower = route.model_id.lower()
    return lower in _MODELS_DEV_IMAGE_INPUT_IDS.get(route.provider_id, frozenset())


def _with_image_input_metadata(
    route: OpenCodeModelRoute,
    image_input_model_ids: set[str] | None,
) -> OpenCodeModelRoute:
    if image_input_model_ids is None or route.unsupported_reason:
        return route
    return replace(route, image_input=route.model_id.lower() in image_input_model_ids)


def is_opencode_provider(provider_id: str | None) -> bool:
    return str(provider_id or "") in OPENCODE_PROVIDER_IDS


def native_opencode_transport(package: str | None) -> TransportMode | None:
    """Map an explicit models.dev SDK package to a supported Row-Bot route."""
    return _NATIVE_TRANSPORTS.get(str(package or "").strip())


def parse_opencode_registry_provider(
    payload: Mapping[str, Any],
    provider_id: str,
) -> OpenCodeRegistryProvider:
    """Extract the small provider-scoped subset Row-Bot uses from models.dev."""
    provider = str(provider_id or "").strip()
    registry_provider_id = _MODELS_DEV_PROVIDER_IDS.get(provider)
    if not registry_provider_id:
        raise ValueError(f"Unknown OpenCode provider: {provider_id}")
    raw_provider = payload.get(registry_provider_id) if isinstance(payload, Mapping) else None
    if not isinstance(raw_provider, Mapping):
        raise ValueError(f"models.dev has no provider metadata for '{registry_provider_id}'.")
    raw_models = raw_provider.get("models")
    models = {
        str(model_id): dict(model)
        for model_id, model in raw_models.items()
        if str(model_id) and isinstance(model, Mapping)
    } if isinstance(raw_models, Mapping) else {}
    default_package = str(raw_provider.get("npm") or "").strip()
    return OpenCodeRegistryProvider(
        provider_id=provider,
        registry_provider_id=registry_provider_id,
        default_package=default_package,
        default_transport=native_opencode_transport(default_package),
        models=models,
    )


def opencode_native_package(registry: OpenCodeRegistryProvider, model_id: str) -> str:
    """Return the explicit model package or the supported provider rollout default."""
    metadata = registry.models.get(str(model_id or ""))
    if isinstance(metadata, Mapping):
        model_provider = metadata.get("provider")
        if isinstance(model_provider, Mapping):
            package = str(model_provider.get("npm") or "").strip()
            if package:
                return package
        package = str(metadata.get("npm") or "").strip()
        if package:
            return package
    return registry.default_package


def _string_set(value: object) -> frozenset[str] | None:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        return None
    return frozenset(
        str(item).strip().lower()
        for item in values
        if str(item).strip()
    )


def _modalities(metadata: Mapping[str, Any], direction: str) -> frozenset[str] | None:
    nested = metadata.get("modalities")
    if isinstance(nested, Mapping):
        values = _string_set(nested.get(direction))
        if values is not None:
            return values
    keys = (
        ("input_modalities", "inputModalities")
        if direction == "input"
        else ("output_modalities", "outputModalities")
    )
    for key in keys:
        values = _string_set(metadata.get(key))
        if values is not None:
            return values
    architecture = metadata.get("architecture")
    if isinstance(architecture, Mapping):
        values = _string_set(architecture.get(f"{direction}_modalities"))
        if values is not None:
            return values
    return None


def _first_bool(*values: object) -> bool | None:
    return next((value for value in values if isinstance(value, bool)), None)


def _positive_int(*values: object) -> int:
    for value in values:
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0


def opencode_native_model_route(
    provider_id: str,
    live_model: Mapping[str, Any],
    registry: OpenCodeRegistryProvider,
) -> OpenCodeModelRoute | None:
    """Resolve one live gateway row using native registry routing metadata."""
    provider = str(provider_id or "").strip()
    model_id = str(live_model.get("id") or "").strip()
    if provider != registry.provider_id or not model_id:
        return None
    registry_model = registry.models.get(model_id)
    metadata = registry_model if isinstance(registry_model, Mapping) else {}
    package = opencode_native_package(registry, model_id)
    transport = native_opencode_transport(package)
    if transport is None:
        return None

    limit = metadata.get("limit") if isinstance(metadata.get("limit"), Mapping) else {}
    context_window = _positive_int(
        limit.get("context"),
        metadata.get("context_length"),
        metadata.get("context_window"),
        live_model.get("context_length"),
        live_model.get("context_window"),
    ) or _context_window(model_id, transport)
    tool_calling = _first_bool(
        metadata.get("tool_call"),
        metadata.get("tool_calling"),
        live_model.get("tool_call"),
        live_model.get("tool_calling"),
    )
    streaming = _first_bool(metadata.get("streaming"), live_model.get("streaming"))
    input_modalities = _modalities(metadata, "input")
    if input_modalities is None:
        input_modalities = _modalities(live_model, "input")
    output_modalities = _modalities(metadata, "output")
    if output_modalities is None:
        output_modalities = _modalities(live_model, "output")
    return OpenCodeModelRoute(
        provider_id=provider,
        model_id=model_id,
        display_name=str(
            metadata.get("name")
            or live_model.get("display_name")
            or live_model.get("name")
            or _display_name(model_id)
        ),
        transport=transport,
        context_window=context_window,
        tool_calling=True if tool_calling is None else tool_calling,
        streaming=True if streaming is None else streaming,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        reasoning=_first_bool(metadata.get("reasoning"), live_model.get("reasoning")),
    )


def opencode_base_url(provider_id: str) -> str:
    if provider_id == OPENCODE_ZEN_PROVIDER_ID:
        return OPENCODE_ZEN_BASE_URL
    if provider_id == OPENCODE_GO_PROVIDER_ID:
        return OPENCODE_GO_BASE_URL
    raise ValueError(f"Unknown OpenCode provider: {provider_id}")


def opencode_anthropic_base_url(provider_id: str) -> str:
    return opencode_base_url(provider_id).removesuffix("/v1")


def opencode_models_url(provider_id: str) -> str:
    return f"{opencode_base_url(provider_id)}/models"


def _display_name(model_id: str) -> str:
    special = {
        "glm": "GLM",
        "gpt": "GPT",
        "mimo": "MiMo",
        "qwen3.5": "Qwen3.5",
        "qwen3.6": "Qwen3.6",
        "qwen3.7": "Qwen3.7",
    }
    parts = str(model_id or "").replace("_", "-").split("-")
    labels: list[str] = []
    for index, part in enumerate(parts):
        joined = ".".join(parts[: index + 1])
        if joined in special:
            labels = [special[joined]]
            continue
        labels.append(special.get(part, part.upper() if len(part) <= 3 else part.capitalize()))
    return " ".join(labels).replace("V ", "V").replace("K ", "K")


def _context_window(model_id: str, transport: TransportMode) -> int:
    lower = str(model_id or "").lower()
    for prefix, context_window in _CONTEXT_BY_PREFIX:
        if lower.startswith(prefix):
            return context_window
    if transport == TransportMode.OPENAI_RESPONSES:
        return 400_000
    return 131_072


def _unsupported_route(provider_id: str, model_id: str, reason: str, *, transport: TransportMode = TransportMode.OPENAI_CHAT) -> OpenCodeModelRoute:
    return OpenCodeModelRoute(
        provider_id,
        model_id,
        _display_name(model_id),
        transport,
        _context_window(model_id, transport),
        tool_calling=False,
        unsupported_reason=reason,
    )


def classify_opencode_model_route(provider_id: str, model_id: str) -> OpenCodeModelRoute | None:
    provider_id = str(provider_id or "")
    model_id = str(model_id or "").strip()
    lower = model_id.lower()
    if not is_opencode_provider(provider_id) or not lower:
        return None

    stale_reason = _STALE_MODELS.get((provider_id, lower))
    if stale_reason:
        return _unsupported_route(provider_id, model_id, stale_reason)

    if lower.startswith("gemini-"):
        return _unsupported_route(
            provider_id,
            model_id,
            "OpenCode Gemini routes are deferred and not supported yet.",
            transport=TransportMode.GOOGLE_GENAI,
        )

    transport: TransportMode | None = None
    if provider_id == OPENCODE_ZEN_PROVIDER_ID:
        if lower.startswith("gpt-"):
            transport = TransportMode.OPENAI_RESPONSES
        elif lower.startswith("claude-") or lower in {"qwen3.6-plus", "qwen3.5-plus"}:
            transport = TransportMode.ANTHROPIC_MESSAGES
        elif lower in _ZEN_CHAT_EXACT or lower.startswith(_ZEN_CHAT_PREFIXES):
            transport = TransportMode.OPENAI_CHAT
    elif provider_id == OPENCODE_GO_PROVIDER_ID:
        if lower.startswith(_GO_MESSAGES_PREFIXES):
            transport = TransportMode.ANTHROPIC_MESSAGES
        elif lower.startswith(_GO_CHAT_PREFIXES):
            transport = TransportMode.OPENAI_CHAT

    if transport is None:
        return None
    return OpenCodeModelRoute(
        provider_id,
        model_id,
        _display_name(model_id),
        transport,
        _context_window(model_id, transport),
    )


def _cached_opencode_entry(provider_id: str, model_id: str) -> Mapping[str, Any] | None:
    try:
        from row_bot.models import _cloud_model_cache
    except Exception:
        return None
    qualified = _cloud_model_cache.get(model_ref(provider_id, model_id))
    if isinstance(qualified, Mapping):
        cached_provider = str(qualified.get("provider") or "")
        if not cached_provider or cached_provider == provider_id:
            return qualified
    legacy = _cloud_model_cache.get(model_id)
    if isinstance(legacy, Mapping) and str(legacy.get("provider") or "") == provider_id:
        return legacy
    return None


def _cached_opencode_route(provider_id: str, model_id: str) -> OpenCodeModelRoute | None:
    cached = _cached_opencode_entry(provider_id, model_id)
    if cached is None:
        return None
    snapshot = cached.get("capabilities_snapshot")
    capabilities = snapshot.get("capabilities") if isinstance(snapshot, Mapping) else None
    raw_transport = (
        snapshot.get("transport") if isinstance(snapshot, Mapping) else None
    ) or cached.get("transport")
    if not raw_transport:
        return None
    try:
        transport = TransportMode(str(raw_transport))
    except ValueError:
        safe_transport = str(raw_transport)[:80].replace("\r", "").replace("\n", "")
        return _unsupported_route(
            provider_id,
            model_id,
            f"OpenCode model '{model_id}' has unsupported cached transport '{safe_transport}'. Refresh the catalog.",
        )
    if transport not in _SUPPORTED_OPENCODE_TRANSPORTS:
        return _unsupported_route(
            provider_id,
            model_id,
            f"OpenCode model '{model_id}' has unsupported cached transport '{transport.value}'. Refresh the catalog.",
        )
    input_modalities = (
        _string_set(snapshot.get("input_modalities"))
        if isinstance(snapshot, Mapping)
        else None
    )
    output_modalities = (
        _string_set(snapshot.get("output_modalities"))
        if isinstance(snapshot, Mapping)
        else None
    )
    tool_calling = (
        snapshot.get("tool_calling")
        if isinstance(snapshot, Mapping) and isinstance(snapshot.get("tool_calling"), bool)
        else None
    )
    streaming = (
        snapshot.get("streaming")
        if isinstance(snapshot, Mapping) and isinstance(snapshot.get("streaming"), bool)
        else None
    )
    return OpenCodeModelRoute(
        provider_id=provider_id,
        model_id=model_id,
        display_name=str(cached.get("label") or _display_name(model_id)),
        transport=transport,
        context_window=_positive_int(cached.get("ctx")) or _context_window(model_id, transport),
        tool_calling=True if tool_calling is None else tool_calling,
        streaming=True if streaming is None else streaming,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        reasoning=(
            "reasoning" in _string_set(capabilities)
            if _string_set(capabilities) is not None
            else None
        ),
    )


def opencode_model_route(provider_id: str, model_id: str) -> OpenCodeModelRoute:
    route = opencode_known_route(provider_id, str(model_id or ""))
    if not route:
        raise OpenCodeUnsupportedRouteError(
            f"OpenCode model '{model_id}' has no supported route mapping for provider '{provider_id}'."
        )
    if route.unsupported_reason:
        raise OpenCodeUnsupportedRouteError(route.unsupported_reason)
    return route


def opencode_model_transport(provider_id: str, model_id: str) -> TransportMode:
    return opencode_model_route(provider_id, model_id).transport


def opencode_known_route(provider_id: str, model_id: str) -> OpenCodeModelRoute | None:
    provider = str(provider_id or "")
    model = str(model_id or "")
    cached = _cached_opencode_route(provider, model)
    return cached or classify_opencode_model_route(provider, model)


def opencode_static_fallback_model_ids(provider_id: str) -> tuple[str, ...]:
    if provider_id == OPENCODE_ZEN_PROVIDER_ID:
        return _ZEN_STATIC_FALLBACK_IDS
    if provider_id == OPENCODE_GO_PROVIDER_ID:
        return _GO_STATIC_FALLBACK_IDS
    return ()


def list_opencode_model_routes(
    provider_id: str | None = None,
    *,
    include_unsupported: bool = True,
    model_ids: Iterable[str] | None = None,
    image_input_model_ids: Iterable[str] | None = None,
) -> list[OpenCodeModelRoute]:
    provider_ids: Iterable[str] = [provider_id] if provider_id else sorted(OPENCODE_PROVIDER_IDS)
    routes: list[OpenCodeModelRoute] = []
    image_input_lookup = (
        {str(model_id or "").strip().lower() for model_id in image_input_model_ids}
        if image_input_model_ids is not None
        else None
    )
    for item in provider_ids:
        ids = list(model_ids) if model_ids is not None else list(opencode_static_fallback_model_ids(str(item)))
        if include_unsupported:
            ids.extend(["gemini-3.5-flash", "gemini-3.1-pro", "gemini-3-flash"])
        for model_id in dict.fromkeys(str(model_id or "") for model_id in ids):
            route = opencode_known_route(str(item), model_id)
            if not route:
                continue
            route = _with_image_input_metadata(route, image_input_lookup)
            if include_unsupported or not route.unsupported_reason:
                routes.append(route)
    return routes


def opencode_model_info(route: OpenCodeModelRoute) -> ModelInfo:
    tasks = {ModelTask.RESPONSES.value} if route.transport == TransportMode.OPENAI_RESPONSES else {ModelTask.CHAT.value}
    capabilities = {"text", "chat"}
    if route.streaming:
        capabilities.add("streaming")
    input_modalities = set(route.input_modalities or {ModelModality.TEXT.value})
    output_modalities = set(route.output_modalities or {ModelModality.TEXT.value})
    if route.transport == TransportMode.OPENAI_RESPONSES:
        capabilities.add("responses")
    if route.tool_calling:
        capabilities.add("tool_calling")
    if route.reasoning:
        capabilities.add("reasoning")
    if _route_supports_image_input(route):
        input_modalities.add(ModelModality.IMAGE.value)
        capabilities.add("vision")
    if route.unsupported_reason:
        tasks = set()
        capabilities = {"unsupported"}
        input_modalities = {ModelModality.TEXT.value}
        output_modalities = {ModelModality.TEXT.value}
    return ModelInfo(
        provider_id=route.provider_id,
        model_id=route.model_id,
        display_name=route.display_name,
        context_window=route.context_window,
        transport=route.transport,
        capabilities=frozenset(capabilities),
        input_modalities=frozenset(input_modalities),
        output_modalities=frozenset(output_modalities),
        tasks=frozenset(tasks),
        tool_calling=route.tool_calling if not route.unsupported_reason else False,
        streaming=route.streaming,
        endpoint_compatibility=frozenset({route.transport}),
        risk_label="cloud_provider",
        source="opencode_catalog",
    )


def list_opencode_model_infos(
    provider_id: str | None = None,
    *,
    model_ids: Iterable[str] | None = None,
    image_input_model_ids: Iterable[str] | None = None,
) -> list[ModelInfo]:
    return [
        opencode_model_info(route)
        for route in list_opencode_model_routes(
            provider_id,
            include_unsupported=False,
            model_ids=model_ids,
            image_input_model_ids=image_input_model_ids,
        )
    ]


def opencode_route_diagnostics(provider_id: str, model_id: str) -> dict[str, object]:
    route = opencode_known_route(provider_id, model_id)
    transport = route.transport if route else None
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "selection_ref": model_ref(provider_id, model_id),
        "base_url": opencode_base_url(provider_id) if is_opencode_provider(provider_id) else "",
        "anthropic_base_url": opencode_anthropic_base_url(provider_id) if is_opencode_provider(provider_id) else "",
        "transport": transport.value if transport else "",
        "unsupported_reason": route.unsupported_reason if route else "OpenCode model has no supported route mapping.",
    }


def opencode_failure_diagnostics(provider_id: str, model_id: str, exc: BaseException) -> dict[str, object]:
    diagnostics = opencode_route_diagnostics(provider_id, model_id)
    text = str(exc or "")
    lower = text.lower()
    hint = ""
    if "model" in lower and "not supported" in lower:
        hint = "OpenCode says this model is not supported for the configured account or current route. Refresh the catalog and choose a currently listed OpenCode model."
    elif "401" in lower or "unauthorized" in lower or "api key" in lower or "auth" in lower:
        label = "OpenCode Zen" if provider_id == OPENCODE_ZEN_PROVIDER_ID else "OpenCode Go"
        hint = f"{label} authentication failed. Check the {provider_id} API key."
    elif "404" in lower or "not found" in lower:
        hint = "OpenCode returned 404; the configured model route may be stale or mapped to the wrong transport."
    elif diagnostics.get("unsupported_reason"):
        hint = str(diagnostics["unsupported_reason"])
    diagnostics.update({
        "error": text,
        "hint": hint,
    })
    return diagnostics
