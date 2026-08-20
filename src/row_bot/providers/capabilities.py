from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from row_bot.providers.models import ModelInfo, ModelModality, ModelTask, TransportMode

CHAT_TASKS = {ModelTask.CHAT.value, ModelTask.RESPONSES.value}

SURFACE_REQUIREMENTS: dict[str, dict[str, set[str]]] = {
    "chat": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "workflow": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "channels": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "designer": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "status_tool": {"tasks_any": CHAT_TASKS, "output_any": {ModelModality.TEXT.value}},
    "vision": {"input_any": {ModelModality.IMAGE.value}, "output_any": {ModelModality.TEXT.value}},
    "image": {"tasks_any": {ModelTask.IMAGE_GENERATION.value, ModelTask.IMAGE_EDIT.value}, "output_any": {ModelModality.IMAGE.value}},
    "video": {"tasks_any": {ModelTask.VIDEO_GENERATION.value}, "output_any": {ModelModality.VIDEO.value}},
    "embeddings": {"tasks_any": {ModelTask.EMBEDDING.value}},
    "audio": {"tasks_any": {ModelTask.TRANSCRIPTION.value, ModelTask.TTS.value, ModelTask.REALTIME.value}},
    "voice": {"tasks_any": {ModelTask.TRANSCRIPTION.value, ModelTask.TTS.value, ModelTask.REALTIME.value}},
}


def normalize_str_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item) for item in value if str(item)}
    return set()


def normalize_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        return {}
    normalized = {
        "capabilities": normalize_str_set(snapshot.get("capabilities")),
        "input_modalities": normalize_str_set(snapshot.get("input_modalities")),
        "output_modalities": normalize_str_set(snapshot.get("output_modalities")),
        "tasks": normalize_str_set(snapshot.get("tasks")),
        "endpoint_compatibility": normalize_str_set(snapshot.get("endpoint_compatibility")),
        "tool_calling": snapshot.get("tool_calling"),
        "streaming": snapshot.get("streaming"),
        "transport": str(snapshot.get("transport") or ""),
        "source_confidence": str(snapshot.get("source_confidence") or ""),
    }
    if isinstance(snapshot.get("reasoning"), Mapping):
        normalized["reasoning"] = dict(snapshot["reasoning"])
    return normalized


def snapshot_for_model(model_info: ModelInfo) -> dict[str, Any]:
    return model_info.capability_snapshot()


def snapshot_supports_surface(snapshot: Mapping[str, Any] | None, surface: str) -> bool:
    requirements = SURFACE_REQUIREMENTS.get(surface)
    normalized = normalize_snapshot(snapshot)
    if not requirements or not normalized:
        return True

    if not (
        normalized["tasks"]
        or normalized["input_modalities"]
        or normalized["output_modalities"]
        or normalized["capabilities"]
    ):
        return True

    tasks = normalized["tasks"]
    input_modalities = normalized["input_modalities"]
    output_modalities = normalized["output_modalities"]

    if "tasks_any" in requirements and not tasks.intersection(requirements["tasks_any"]):
        return False
    if "input_any" in requirements and not input_modalities.intersection(requirements["input_any"]):
        return False
    if "output_any" in requirements and not output_modalities.intersection(requirements["output_any"]):
        return False
    return True


def model_supports_surface(model_info: ModelInfo, surface: str) -> bool:
    return snapshot_supports_surface(snapshot_for_model(model_info), surface)


def visible_surfaces_for_model(model_info: ModelInfo) -> list[str]:
    surfaces = [surface for surface in SURFACE_REQUIREMENTS if model_supports_surface(model_info, surface)]
    if "chat" in surfaces:
        for default_surface in ("workflow", "channels", "designer", "status_tool"):
            if default_surface not in surfaces:
                surfaces.append(default_surface)
    return surfaces


def endpoint_values(modes: set[TransportMode] | frozenset[TransportMode] | tuple[TransportMode, ...]) -> frozenset[TransportMode]:
    return frozenset(modes)
