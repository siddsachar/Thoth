from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from row_bot.providers.catalog import model_info_from_metadata
from row_bot.providers.models import TransportMode


@dataclass(frozen=True)
class ResolvedCapabilitySnapshot:
    snapshot: dict[str, Any]
    source: str = ""


def resolve_capability_snapshot(
    provider_id: str,
    model_id: str,
    *,
    transport: TransportMode | str | None = None,
    include_static_fallback: bool = True,
) -> dict[str, Any]:
    return resolve_capability_metadata(
        provider_id,
        model_id,
        transport=transport,
        include_static_fallback=include_static_fallback,
    ).snapshot


def resolve_capability_metadata(
    provider_id: str,
    model_id: str,
    *,
    transport: TransportMode | str | None = None,
    include_static_fallback: bool = True,
) -> ResolvedCapabilitySnapshot:
    provider = "ollama" if str(provider_id or "") == "local" else str(provider_id or "")
    model = str(model_id or "")
    if not provider or not model:
        return ResolvedCapabilitySnapshot({}, "")

    if provider.startswith("custom_openai_"):
        resolved = _custom_endpoint_snapshot(provider, model, transport=transport)
        if resolved.snapshot or not include_static_fallback:
            return resolved
        return ResolvedCapabilitySnapshot({}, "")

    if provider == "ollama":
        cached = cached_ollama_capability_snapshot(model)
        if cached:
            return ResolvedCapabilitySnapshot(cached, "ollama_catalog_cache")
        if not include_static_fallback:
            return ResolvedCapabilitySnapshot({}, "")
        try:
            from row_bot.providers.ollama import ollama_model_info

            return ResolvedCapabilitySnapshot(
                ollama_model_info(model).capability_snapshot(),
                "ollama_static_fallback",
            )
        except Exception:
            return ResolvedCapabilitySnapshot({}, "")

    cached = cached_provider_capability_snapshot(provider, model)
    if cached:
        return ResolvedCapabilitySnapshot(cached, "provider_cache")

    if provider == "codex":
        try:
            from row_bot.providers.codex import list_codex_model_infos_for_status

            for model_info in list_codex_model_infos_for_status():
                if model_info.model_id == model:
                    return ResolvedCapabilitySnapshot(model_info.capability_snapshot(), "codex_status_catalog")
        except Exception:
            pass

    if provider == "claude_subscription":
        try:
            from row_bot.providers.claude_subscription import list_claude_subscription_model_infos_for_status

            for model_info in list_claude_subscription_model_infos_for_status():
                if model_info.model_id == model:
                    return ResolvedCapabilitySnapshot(model_info.capability_snapshot(), "claude_subscription_status_catalog")
        except Exception:
            pass

    if provider == "xai_oauth":
        try:
            from row_bot.providers.xai_oauth import list_xai_oauth_model_infos_for_status

            for model_info in list_xai_oauth_model_infos_for_status():
                if model_info.model_id == model:
                    return ResolvedCapabilitySnapshot(model_info.capability_snapshot(), "xai_oauth_status_catalog")
        except Exception:
            pass

    if provider in {"opencode_zen", "opencode_go"}:
        try:
            from row_bot.providers.opencode import opencode_known_route, opencode_model_info

            route = opencode_known_route(provider, model)
            if route:
                return ResolvedCapabilitySnapshot(opencode_model_info(route).capability_snapshot(), "opencode_catalog")
        except Exception:
            pass

    if not include_static_fallback:
        return ResolvedCapabilitySnapshot({}, "")
    try:
        return ResolvedCapabilitySnapshot(
            model_info_from_metadata(
                provider,
                model,
                {},
                transport=_coerce_transport(transport),
            ).capability_snapshot(),
            "provider_static_fallback",
        )
    except Exception:
        return ResolvedCapabilitySnapshot({}, "")


def cached_ollama_capability_snapshot(
    model_id: str,
    *,
    snapshot: Any | None = None,
) -> dict[str, Any]:
    snap = snapshot
    if snap is None:
        try:
            from row_bot.providers.model_catalog_cache import read_model_catalog_cache

            snap = read_model_catalog_cache()
        except Exception:
            return {}
    rows = getattr(snap, "ollama_rows", []) or []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("model_id") or row.get("id") or "") != str(model_id or ""):
            continue
        capabilities = row.get("capabilities_snapshot")
        if isinstance(capabilities, Mapping) and capabilities:
            return dict(capabilities)
        return _snapshot_from_row_fields(row)
    return {}


def cached_provider_capability_snapshot(provider_id: str, model_id: str) -> dict[str, Any]:
    provider = str(provider_id or "")
    model = str(model_id or "")
    cached = _cloud_cache_capability_snapshot(provider, model)
    if cached:
        return cached
    cached = _catalog_cache_capability_snapshot(provider, model)
    if cached:
        return cached
    cached = _subscription_status_capability_snapshot(provider, model)
    if cached:
        return cached
    return {}


def _cloud_cache_capability_snapshot(provider: str, model: str) -> dict[str, Any]:
    try:
        from row_bot.models import _cloud_model_cache

        cached = _cloud_model_cache.get(f"model:{provider}:{model}") or _cloud_model_cache.get(model)
    except Exception:
        return {}
    if not isinstance(cached, Mapping):
        return {}
    cached_provider = str(cached.get("provider") or "")
    if cached_provider and cached_provider != provider:
        return {}
    snapshot = cached.get("capabilities_snapshot")
    if isinstance(snapshot, Mapping) and snapshot:
        return dict(snapshot)
    return {}


def _catalog_cache_capability_snapshot(provider: str, model: str) -> dict[str, Any]:
    try:
        from row_bot.providers.model_catalog_cache import read_model_catalog_cache

        snap = read_model_catalog_cache()
    except Exception:
        return {}
    if provider == "ollama":
        return cached_ollama_capability_snapshot(model, snapshot=snap)
    for key, cached in (getattr(snap, "cloud_cache", {}) or {}).items():
        if not isinstance(cached, Mapping):
            continue
        cached_provider = str(cached.get("provider") or "")
        cached_model = str(cached.get("model_id") or cached.get("id") or key or "")
        if str(key or "") == f"model:{provider}:{model}":
            cached_model = model
            cached_provider = cached_provider or provider
        if cached_provider and cached_provider != provider:
            continue
        if cached_model != model:
            continue
        snapshot = cached.get("capabilities_snapshot")
        if isinstance(snapshot, Mapping) and snapshot:
            return dict(snapshot)
    return {}


def _subscription_status_capability_snapshot(provider: str, model: str) -> dict[str, Any]:
    try:
        if provider == "codex":
            from row_bot.providers.codex import list_codex_model_infos_for_status

            infos = list_codex_model_infos_for_status()
        elif provider == "claude_subscription":
            from row_bot.providers.claude_subscription import list_claude_subscription_model_infos_for_status

            infos = list_claude_subscription_model_infos_for_status()
        elif provider == "xai_oauth":
            from row_bot.providers.xai_oauth import list_xai_oauth_model_infos_for_status

            infos = list_xai_oauth_model_infos_for_status()
        else:
            return {}
        for model_info in infos:
            if model_info.model_id == model:
                return model_info.capability_snapshot()
    except Exception:
        return {}
    return {}


def _custom_endpoint_snapshot(
    provider_id: str,
    model_id: str,
    *,
    transport: TransportMode | str | None = None,
) -> ResolvedCapabilitySnapshot:
    try:
        from row_bot.providers.custom import custom_endpoint_models

        for item in custom_endpoint_models(provider_id):
            if str(item.get("model_id") or item.get("id") or "") != model_id:
                continue
            snapshot = item.get("capabilities_snapshot")
            if isinstance(snapshot, Mapping) and snapshot:
                return ResolvedCapabilitySnapshot(dict(snapshot), "custom_endpoint_cache")
            return ResolvedCapabilitySnapshot(
                model_info_from_metadata(
                    provider_id,
                    model_id,
                    dict(item),
                    context_window=int(item.get("context_window") or item.get("ctx") or 0),
                    transport=_coerce_transport(transport),
                ).capability_snapshot(),
                "custom_endpoint_metadata",
            )
    except Exception:
        return ResolvedCapabilitySnapshot({}, "")
    return ResolvedCapabilitySnapshot({}, "")


def _snapshot_from_row_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key in (
        "capabilities",
        "input_modalities",
        "output_modalities",
        "tasks",
        "endpoint_compatibility",
        "tool_calling",
        "streaming",
        "transport",
        "source_confidence",
        "last_verified_at",
        "reasoning",
        "generation_parameters",
    ):
        if key in row:
            snapshot[key] = row[key]
    return snapshot


def _coerce_transport(value: TransportMode | str | None) -> TransportMode | None:
    if isinstance(value, TransportMode):
        return value
    if value is None:
        return None
    try:
        return TransportMode(str(value))
    except Exception:
        return None
