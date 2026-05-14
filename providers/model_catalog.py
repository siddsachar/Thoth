from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Iterable

from providers.capabilities import SURFACE_REQUIREMENTS, normalize_snapshot, snapshot_supports_surface
from providers.catalog import get_provider_definition, model_info_from_legacy, model_info_from_metadata
from providers.models import ModelInfo
from providers.selection import model_ref

CATALOG_SURFACES = ("chat", "vision", "image", "video")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogModelRow:
    provider_id: str
    model_id: str
    display_name: str
    categories: tuple[str, ...]
    capabilities_snapshot: dict[str, Any] = field(default_factory=dict)
    selection_ref: str = ""
    provider_display_name: str = ""
    provider_icon: str = ""
    context_window: int = 0
    runtime_ready: bool = True
    configured: bool = True
    installed: bool = True
    downloadable: bool = False
    pinned_surfaces: tuple[str, ...] = ()
    default_surfaces: tuple[str, ...] = ()
    source: str = "catalog"
    risk_label: str = "api_key"
    status_reason: str = ""
    availability: str = ""

    def supports(self, surface: str) -> bool:
        return surface in self.categories


def categories_for_snapshot(snapshot: dict[str, Any] | None) -> tuple[str, ...]:
    normalized = normalize_snapshot(snapshot)
    has_structured_metadata = bool(
        normalized.get("tasks")
        or normalized.get("input_modalities")
        or normalized.get("output_modalities")
        or normalized.get("capabilities")
    )
    if not has_structured_metadata:
        return ()
    return tuple(surface for surface in CATALOG_SURFACES if snapshot_supports_surface(snapshot, surface))


def rows_for_surface(rows: Iterable[CatalogModelRow], surface: str) -> list[CatalogModelRow]:
    return [row for row in rows if row.supports(surface)]


def group_rows_by_provider(rows: Iterable[CatalogModelRow]) -> dict[str, list[CatalogModelRow]]:
    grouped: dict[str, list[CatalogModelRow]] = {}
    for row in rows:
        grouped.setdefault(row.provider_id, []).append(row)
    return {
        provider_id: sorted(provider_rows, key=lambda row: row.display_name.lower())
        for provider_id, provider_rows in sorted(grouped.items(), key=lambda item: _provider_sort_label(item[0], item[1]))
    }


def load_ollama_catalog_rows() -> list[dict[str, Any]]:
    from models import POPULAR_MODELS, fetch_trending_ollama_models, list_local_models
    from providers.ollama import (
        DEFAULT_OLLAMA_LIBRARY_FAMILIES,
        fetch_ollama_cloud_library_models,
        fetch_ollama_library_family_models,
        fetch_ollama_library_models,
        is_ollama_cloud_offload_model,
        ollama_catalog_rows,
        ollama_provider_catalog_model_ids,
    )

    started = time.perf_counter()
    local = list_local_models()
    library_families = fetch_ollama_library_models()
    cloud_families = fetch_ollama_cloud_library_models()
    family_tag_models: list[str] = []
    families_to_expand = _unique_ordered([
        *DEFAULT_OLLAMA_LIBRARY_FAMILIES,
        *cloud_families[:24],
    ])
    for family in families_to_expand:
        family_tag_models.extend(fetch_ollama_library_family_models(family))
    cloud_candidates = [
        model_id for model_id in family_tag_models
        if is_ollama_cloud_offload_model(model_id) and model_id not in local
    ]
    cloud_show_metadata = _probe_ollama_show_metadata(cloud_candidates[:40])
    recommended_models = ollama_provider_catalog_model_ids(
        [],
        [*POPULAR_MODELS, *fetch_trending_ollama_models()],
        _unique_ordered([*library_families, *cloud_families]),
        family_tag_models,
    )
    rows = ollama_catalog_rows(
        local,
        recommended_models,
        ready_cloud_offload=set(cloud_show_metadata),
        metadata_by_model=cloud_show_metadata,
    )
    logger.info(
        "perf: ollama catalog rows loaded in %.3fs (local=%d families=%d tags=%d cloud_candidates=%d show_ready=%d rows=%d)",
        time.perf_counter() - started,
        len(local),
        len(families_to_expand),
        len(family_tag_models),
        len(cloud_candidates),
        len(cloud_show_metadata),
        len(rows),
    )
    return rows


def _unique_ordered(values: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _probe_ollama_show_metadata(model_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    try:
        from models import _ollama_client, _ollama_reachable
    except Exception:
        return {}
    if not _ollama_reachable():
        return {}
    client = _ollama_client()
    if client is None:
        return {}
    results: dict[str, dict[str, Any]] = {}
    for model_id in _unique_ordered(model_ids):
        try:
            raw = client.show(model_id)
        except Exception:
            continue
        metadata = _normalize_ollama_show_response(raw)
        if metadata:
            results[model_id] = metadata
    return results


def _normalize_ollama_show_response(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        data = dict(raw)
    elif hasattr(raw, "model_dump"):
        try:
            data = dict(raw.model_dump())
        except Exception:
            data = {}
    else:
        data = {
            key: getattr(raw, key)
            for key in ("details", "modelinfo", "capabilities", "parameters", "template")
            if hasattr(raw, key)
        }
    metadata: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            for subkey, subvalue in value.items():
                metadata[f"{key}_{subkey}"] = subvalue
        else:
            metadata[key] = value
    modelinfo = data.get("modelinfo") if isinstance(data.get("modelinfo"), dict) else {}
    context = modelinfo.get("general.context_length") or modelinfo.get("llama.context_length")
    if context:
        metadata["context_window"] = context
    return metadata


def build_model_catalog_rows(
    *,
    cloud_cache: dict[str, dict[str, Any]] | None = None,
    ollama_rows: Iterable[dict[str, Any]] | None = None,
    defaults: dict[str, str] | None = None,
    quick_choices: Iterable[dict[str, Any]] | None = None,
) -> list[CatalogModelRow]:
    cloud_cache = dict(_safe_cloud_cache() if cloud_cache is None else cloud_cache)
    defaults = dict(defaults or {})
    quick = [choice for choice in (quick_choices if quick_choices is not None else _safe_quick_choices()) if isinstance(choice, dict)]
    pinned_by_ref = _pinned_surfaces_by_ref(quick)
    default_refs = _default_refs(defaults)
    provider_status = _provider_status_by_id()

    rows: dict[str, CatalogModelRow] = {}
    for model_id, info in cloud_cache.items():
        provider_id = str(info.get("provider") or "")
        if provider_id.startswith("custom_openai_"):
            continue
        model_info = model_info_from_legacy(str(model_id), info)
        if model_info:
            _add_model_info_row(rows, model_info, provider_status, pinned_by_ref, default_refs, installed=True)

    if provider_status.get("minimax", {}).get("configured") and not any(
        row.provider_id == "minimax" for row in rows.values()
    ):
        for model_info in _minimax_static_model_infos():
            _add_model_info_row(rows, model_info, provider_status, pinned_by_ref, default_refs, installed=True)

    for model_info in _custom_model_infos():
        _add_model_info_row(rows, model_info, provider_status, pinned_by_ref, default_refs, installed=True)

    for surface in ("image", "video"):
        for config_value, info in _curated_media_entries(surface).items():
            provider_id, model_id = config_value.split("/", 1) if "/" in config_value else (str(info.get("provider") or ""), config_value)
            if not provider_id or not model_id:
                continue
            model_info = model_info_from_metadata(
                provider_id,
                model_id,
                info,
                display_name=str(info.get("label") or model_id),
                context_window=int(info.get("ctx") or 0),
                risk_label=str(info.get("risk_label") or "api_key"),
                source=str(info.get("source") or "curated_media_catalog"),
            )
            _add_model_info_row(rows, model_info, provider_status, pinned_by_ref, default_refs, installed=True)

    for row in ollama_rows or []:
        _add_ollama_row(rows, row, provider_status, pinned_by_ref, default_refs)

    for model_info in _codex_model_infos():
        _add_model_info_row(rows, model_info, provider_status, pinned_by_ref, default_refs, installed=True)

    for surface, ref in default_refs.items():
        if ref in rows:
            continue
        parsed = _parse_model_ref(ref)
        if not parsed:
            continue
        provider_id, model_id = parsed
        model_info = model_info_from_metadata(
            provider_id,
            model_id,
            {},
            display_name=model_id,
            source=f"default_{surface}",
        )
        _add_model_info_row(rows, model_info, provider_status, pinned_by_ref, default_refs, installed=True)

    return sorted(rows.values(), key=lambda row: (row.provider_display_name.lower(), row.display_name.lower()))


def _add_model_info_row(
    rows: dict[str, CatalogModelRow],
    model_info: ModelInfo,
    provider_status: dict[str, dict[str, Any]],
    pinned_by_ref: dict[str, set[str]],
    default_refs: dict[str, str],
    *,
    installed: bool,
    downloadable: bool = False,
) -> None:
    snapshot = model_info.capability_snapshot()
    categories = categories_for_snapshot(snapshot)
    if not categories:
        return
    rows[model_info.selection_ref] = _catalog_row(
        provider_id=model_info.provider_id,
        model_id=model_info.model_id,
        display_name=model_info.display_name,
        categories=categories,
        capabilities_snapshot=snapshot,
        provider_status=provider_status,
        pinned_by_ref=pinned_by_ref,
        default_refs=default_refs,
        context_window=model_info.context_window,
        installed=installed,
        downloadable=downloadable,
        source=model_info.source,
        risk_label=model_info.risk_label,
    )


def _add_ollama_row(
    rows: dict[str, CatalogModelRow],
    row: dict[str, Any],
    provider_status: dict[str, dict[str, Any]],
    pinned_by_ref: dict[str, set[str]],
    default_refs: dict[str, str],
) -> None:
    model_id = str(row.get("model_id") or "")
    if not model_id:
        return
    snapshot = row.get("capabilities_snapshot") if isinstance(row.get("capabilities_snapshot"), dict) else {}
    categories = categories_for_snapshot(snapshot)
    if not categories:
        return
    rows[model_ref("ollama", model_id)] = _catalog_row(
        provider_id="ollama",
        model_id=model_id,
        display_name=str(row.get("display_name") or model_id),
        categories=categories,
        capabilities_snapshot=snapshot,
        provider_status=provider_status,
        pinned_by_ref=pinned_by_ref,
        default_refs=default_refs,
        context_window=int(row.get("context_window") or 0),
        installed=bool(row.get("installed")),
        downloadable=bool(row.get("downloadable", not bool(row.get("installed")))),
        source=str(row.get("source") or "ollama_catalog"),
        risk_label=str(row.get("risk_label") or "local_private"),
        availability=str(row.get("availability") or ""),
    )


def _catalog_row(
    *,
    provider_id: str,
    model_id: str,
    display_name: str,
    categories: tuple[str, ...],
    capabilities_snapshot: dict[str, Any],
    provider_status: dict[str, dict[str, Any]],
    pinned_by_ref: dict[str, set[str]],
    default_refs: dict[str, str],
    context_window: int,
    installed: bool,
    downloadable: bool,
    source: str,
    risk_label: str,
    availability: str = "",
) -> CatalogModelRow:
    definition = get_provider_definition(provider_id)
    status = provider_status.get(provider_id, {})
    configured = bool(status.get("configured")) if status else provider_id == "ollama" or provider_id.startswith("custom_openai_")
    if provider_id == "ollama":
        configured = str(status.get("source") or "") != "not_running" if status else True
    runtime_ready = configured and installed
    status_reason = ""
    if not configured:
        status_reason = "Connect this provider before using this model."
    elif provider_id == "ollama" and availability == "cloud_offload_available":
        status_reason = "Sign in to Ollama and pull this cloud model before using it."
    elif not installed:
        status_reason = "Download this Ollama model before using it."
    if provider_id == "codex" and not bool(status.get("runtime_enabled")):
        runtime_ready = False
        status_reason = "Codex account is connected, but direct chat runtime is not enabled yet."
    ref = model_ref(provider_id, model_id)
    return CatalogModelRow(
        provider_id=provider_id,
        model_id=model_id,
        display_name=display_name,
        selection_ref=ref,
        provider_display_name=definition.display_name if definition else provider_id,
        provider_icon=definition.icon if definition else "",
        categories=categories,
        capabilities_snapshot=dict(capabilities_snapshot),
        context_window=int(context_window or 0),
        runtime_ready=runtime_ready,
        configured=configured,
        installed=installed,
        downloadable=downloadable,
        pinned_surfaces=tuple(sorted(pinned_by_ref.get(ref, set()))),
        default_surfaces=tuple(sorted(surface for surface, default_ref in default_refs.items() if default_ref == ref)),
        source=source,
        risk_label=risk_label,
        status_reason=status_reason,
        availability=availability,
    )


def _safe_cloud_cache() -> dict[str, dict[str, Any]]:
    try:
        from models import _cloud_model_cache, _sync_custom_model_cache

        _sync_custom_model_cache()
        return {str(model_id): dict(info) for model_id, info in _cloud_model_cache.items() if isinstance(info, dict)}
    except Exception:
        return {}


def _safe_quick_choices() -> list[dict[str, Any]]:
    try:
        from providers.selection import list_quick_choices

        return list_quick_choices("", include_inactive=True)
    except Exception:
        return []


def _pinned_surfaces_by_ref(quick: Iterable[dict[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for choice in quick:
        ref = str(choice.get("id") or "")
        if not ref:
            continue
        surfaces = set()
        snapshot = choice.get("capabilities_snapshot") if isinstance(choice.get("capabilities_snapshot"), dict) else {}
        for surface in CATALOG_SURFACES:
            if snapshot_supports_surface(snapshot, surface):
                surfaces.add(surface)
        visibility = choice.get("visibility")
        if isinstance(visibility, list):
            surfaces.update(str(item) for item in visibility if str(item) in CATALOG_SURFACES)
        if surfaces:
            result[ref] = surfaces
    return result


def _default_refs(defaults: dict[str, str]) -> dict[str, str]:
    refs: dict[str, str] = {}
    for surface, value in defaults.items():
        raw = str(value or "")
        if not raw:
            continue
        if raw.startswith("model:"):
            refs[surface] = raw
            continue
        if "/" in raw and surface in {"image", "video"}:
            provider_id, model_id = raw.split("/", 1)
            refs[surface] = model_ref(provider_id, model_id)
            continue
        try:
            from providers.catalog import infer_provider_id

            provider_id = infer_provider_id(raw) or ("ollama" if surface in {"chat", "vision"} else "")
        except Exception:
            provider_id = "ollama" if surface in {"chat", "vision"} else ""
        if provider_id:
            refs[surface] = model_ref(provider_id, raw)
    return refs


def _provider_status_by_id() -> dict[str, dict[str, Any]]:
    try:
        from providers.status import provider_status_cards

        return {str(card.get("provider_id")): dict(card) for card in provider_status_cards() if isinstance(card, dict)}
    except Exception:
        return {}


def _curated_media_entries(surface: str) -> dict[str, dict[str, Any]]:
    try:
        from providers.media import curated_media_cache_entries

        return curated_media_cache_entries(surface)
    except Exception:
        return {}


def _custom_model_infos() -> list[ModelInfo]:
    try:
        from providers.custom import custom_endpoint_models, list_custom_endpoints
    except Exception:
        return []
    infos: list[ModelInfo] = []
    for endpoint in list_custom_endpoints():
        provider_id = str(endpoint.get("provider_id") or "")
        for model in custom_endpoint_models(str(endpoint.get("id") or provider_id)):
            model_id = str(model.get("model_id") or model.get("id") or "")
            if not provider_id or not model_id:
                continue
            infos.append(model_info_from_metadata(
                provider_id,
                model_id,
                model,
                display_name=str(model.get("display_name") or model.get("label") or model_id),
                context_window=int(model.get("context_window") or model.get("ctx") or 0),
                risk_label=str(endpoint.get("risk_label") or model.get("risk_label") or "custom_endpoint"),
                source="custom_openai_catalog",
            ))
    return infos


def _minimax_static_model_infos() -> list[ModelInfo]:
    try:
        from models import _MINIMAX_SUPPORTED_MODELS
    except Exception:
        return []
    return [
        model_info_from_metadata(
            "minimax",
            str(model_id),
            {"max_input_tokens": int(context_window or 0)},
            display_name=str(model_id),
            context_window=int(context_window or 0),
            source="provider_static_catalog",
        )
        for model_id, context_window in _MINIMAX_SUPPORTED_MODELS
    ]


def _parse_model_ref(ref: str) -> tuple[str, str] | None:
    parts = str(ref or "").split(":", 2)
    if len(parts) == 3 and parts[0] == "model" and parts[1] and parts[2]:
        return parts[1], parts[2]
    return None


def _codex_model_infos() -> list[ModelInfo]:
    try:
        from providers.codex import list_codex_model_infos

        return list_codex_model_infos()
    except Exception:
        return []


def _provider_sort_label(provider_id: str, rows: list[CatalogModelRow]) -> tuple[str, str]:
    label = rows[0].provider_display_name if rows else provider_id
    return (label.lower(), provider_id)
