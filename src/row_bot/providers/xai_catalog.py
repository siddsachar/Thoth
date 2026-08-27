from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from row_bot.providers.models import ModelInfo, ModelModality, ModelTask, TransportMode

XAI_COMPOSER_MODEL_ID = "grok-composer-2.5-fast"
XAI_COMPOSER_DISPLAY_NAME = "Grok Composer 2.5 Fast"


def xai_model_id_from_item(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("model") or item.get("name") or "").strip()


def xai_generation_parameters_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    pricing = item.get("pricing")
    if not isinstance(pricing, list) or not pricing:
        return None

    qualities: list[str] = []
    resolutions: list[str] = []
    combinations: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in pricing:
        if not isinstance(row, dict):
            continue
        quality = str(row.get("quality") or "").strip().lower()
        resolution = str(row.get("resolution") or "").strip().lower()
        pair = (quality, resolution)
        if not quality or not resolution or pair in seen:
            continue
        seen.add(pair)
        combinations.append({"quality": quality, "resolution": resolution})
        if quality not in qualities:
            qualities.append(quality)
        if resolution not in resolutions:
            resolutions.append(resolution)
    if not combinations:
        return None
    return {
        "options": {
            "quality": qualities,
            "resolution": resolutions,
        },
        "defaults": {
            "quality": "medium",
            "resolution": "1k",
        },
        "valid_combinations": combinations,
    }


def merged_xai_model_entries(pages: Iterable[Iterable[Any]]) -> list[dict[str, Any]]:
    by_model_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for page in pages:
        for item in page:
            if not isinstance(item, dict):
                continue
            model_id = xai_model_id_from_item(item)
            if not model_id:
                continue
            if model_id not in by_model_id:
                ordered_ids.append(model_id)
                by_model_id[model_id] = dict(item)
                continue
            merged = dict(by_model_id[model_id])
            merged.update(item)
            by_model_id[model_id] = merged
    return [by_model_id[model_id] for model_id in ordered_ids]


def is_hidden_xai_model(item: dict[str, Any], model_id: str) -> bool:
    if item.get("hidden") is True or item.get("is_hidden") is True:
        return True
    status = str(item.get("status") or item.get("visibility") or "").strip().lower()
    return status in {"hidden", "disabled", "internal", "unavailable"}


def xai_curated_chat_extra_model_infos(
    provider_id: str,
    *,
    transport: TransportMode,
    source: str,
    risk_label: str,
    billing_label: str = "",
    verified_at: str = "",
) -> list[ModelInfo]:
    task = ModelTask.RESPONSES.value if transport == TransportMode.OPENAI_RESPONSES else ModelTask.CHAT.value
    return [
        ModelInfo(
            provider_id=provider_id,
            model_id=XAI_COMPOSER_MODEL_ID,
            display_name=XAI_COMPOSER_DISPLAY_NAME,
            context_window=131_072,
            transport=transport,
            capabilities=frozenset({"text", "chat", "streaming", "tool_calling"}),
            input_modalities=frozenset({ModelModality.TEXT.value}),
            output_modalities=frozenset({ModelModality.TEXT.value}),
            tasks=frozenset({task}),
            tool_calling=True,
            streaming=True,
            endpoint_compatibility=frozenset({transport}),
            billing_label=billing_label,
            source_confidence="row_bot_curated_xai_catalog",
            last_verified_at=verified_at,
            risk_label=risk_label,
            source=source,
        )
    ]


def merge_xai_curated_chat_extras(
    model_infos: Iterable[ModelInfo],
    provider_id: str,
    *,
    transport: TransportMode,
    source: str,
    risk_label: str,
    billing_label: str = "",
    verified_at: str = "",
) -> list[ModelInfo]:
    merged = list(model_infos)
    seen = {info.model_id for info in merged}
    for info in xai_curated_chat_extra_model_infos(
        provider_id,
        transport=transport,
        source=source,
        risk_label=risk_label,
        billing_label=billing_label,
        verified_at=verified_at,
    ):
        if info.model_id not in seen:
            merged.append(info)
            seen.add(info.model_id)
    return merged
