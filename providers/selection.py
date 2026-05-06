from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from providers.capabilities import snapshot_supports_surface
from providers.catalog import infer_provider_id
from providers.config import load_provider_config, save_provider_config
from providers.errors import NormalizedProviderError, normalize_provider_error

CHAT_VISIBILITY = ["chat", "workflow", "channels", "designer", "status_tool"]
DEFAULT_VISIBILITY = list(CHAT_VISIBILITY)
SURFACE_VISIBILITY: dict[str, list[str]] = {
    "chat": list(CHAT_VISIBILITY),
    "vision": ["vision"],
    "image": ["image"],
    "video": ["video"],
}
QUICK_CHOICE_SURFACE_GROUPS = [
    {"id": "chat", "display_name": "Chat"},
    {"id": "vision", "display_name": "Vision"},
    {"id": "image", "display_name": "Image"},
    {"id": "video", "display_name": "Video"},
]


@dataclass(frozen=True)
class ResolvedSelection:
    ref: str
    kind: str
    model_id: str = ""
    provider_id: str = ""
    route_id: str = ""
    display_name: str = ""
    legacy_value: str = ""
    active: bool = True
    reason: str = ""


def model_ref(provider_id: str, model_id: str) -> str:
    return f"model:{provider_id}:{model_id}"


def parse_model_ref(value: str | None) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if not raw.startswith("model:"):
        return None
    parts = raw.split(":", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def provider_display_label(provider_id: str) -> str:
    provider_id = str(provider_id or "")
    try:
        from providers.catalog import list_provider_definitions

        for definition in list_provider_definitions():
            if definition.id == provider_id:
                return definition.display_name
    except Exception:
        pass
    if provider_id.startswith("custom_openai_"):
        return provider_id.replace("custom_openai_", "Custom ").replace("_", " ").title()
    if provider_id == "local":
        return "Local"
    return provider_id or "Provider"


def provider_icon_label(provider_id: str) -> str:
    provider_id = str(provider_id or "")
    try:
        from providers.catalog import list_provider_definitions

        for definition in list_provider_definitions():
            if definition.id == provider_id:
                return definition.icon or ""
    except Exception:
        pass
    if provider_id in {"local", "ollama"}:
        return "🖥️"
    return ""


def model_choice_value(value: str | None, *, provider_id: str | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = parse_model_ref(raw)
    if parsed:
        provider, model_id = parsed
    else:
        provider = provider_id or infer_provider_id(raw) or "local"
        model_id = raw
    if provider in {"local", "ollama"}:
        return model_id
    return model_ref(provider, model_id)


def model_id_from_choice_value(value: str | None) -> str:
    raw = str(value or "").strip()
    parsed = parse_model_ref(raw)
    return parsed[1] if parsed else raw


def provider_id_from_choice_value(value: str | None) -> str:
    raw = str(value or "").strip()
    parsed = parse_model_ref(raw)
    if parsed:
        return parsed[0]
    return infer_provider_id(raw) or "local"


def format_model_choice_label(
    provider_id: str,
    model_id: str,
    display_name: str | None = None,
    *,
    include_icon: bool = True,
) -> str:
    provider = str(provider_id or "local")
    name = str(display_name or model_id or "").strip() or str(model_id or "")
    icon = provider_icon_label(provider) if include_icon else ""
    prefix = f"{icon} " if icon else ""
    return f"{prefix}{name} — {provider_display_label(provider)}"


def _model_choice_option_for_value(value: str) -> dict[str, Any] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = parse_model_ref(raw)
    if parsed:
        provider_id, model_id = parsed
    else:
        provider_id = infer_provider_id(raw) or "local"
        model_id = raw
    value_ref = model_choice_value(model_id, provider_id=provider_id)
    return {
        "value": value_ref,
        "label": format_model_choice_label(provider_id, model_id),
        "provider_id": provider_id,
        "model_id": model_id,
        "display_name": model_id,
        "source": "included_value",
    }


def list_model_choice_options(
    surface: str = "chat",
    *,
    include_values: Iterable[str] | None = None,
    include_inactive: bool = False,
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_option(option: dict[str, Any] | None) -> None:
        if not option:
            return
        value = str(option.get("value") or "")
        if not value or value in seen:
            return
        seen.add(value)
        options.append(option)

    for choice in list_quick_choices(surface, include_inactive=include_inactive):
        if choice.get("kind") != "model" or not choice.get("model_id"):
            continue
        provider_id = str(choice.get("provider_id") or infer_provider_id(str(choice.get("model_id") or "")) or "local")
        model_id = str(choice.get("model_id") or "")
        value = model_choice_value(model_id, provider_id=provider_id)
        add_option({
            "value": value,
            "label": format_model_choice_label(provider_id, model_id, str(choice.get("display_name") or model_id)),
            "provider_id": provider_id,
            "model_id": model_id,
            "display_name": str(choice.get("display_name") or model_id),
            "source": str(choice.get("source") or "quick_choice"),
            "active": choice.get("active") is not False,
            "reason": str(choice.get("inactive_reason") or ""),
        })

    for value in include_values or []:
        add_option(_model_choice_option_for_value(str(value or "")))

    return options


def model_choice_options_map(
    surface: str = "chat",
    *,
    include_values: Iterable[str] | None = None,
    include_inactive: bool = False,
) -> dict[str, str]:
    return {
        str(option["value"]): str(option["label"])
        for option in list_model_choice_options(surface, include_values=include_values, include_inactive=include_inactive)
    }


def route_ref(route_id: str) -> str:
    return f"route:{route_id}"


def _quick_choice_for_model(
    model_id: str,
    *,
    provider_id: str | None = None,
    display_name: str | None = None,
    source: str = "manual",
    capabilities_snapshot: dict[str, Any] | None = None,
    visibility: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    provider_id = provider_id or infer_provider_id(model_id)
    if not provider_id:
        return None
    try:
        from providers.catalog import get_provider_definition
        definition = get_provider_definition(provider_id)
        risk_label = definition.risk_label if definition else "custom_endpoint" if provider_id.startswith("custom_openai_") else "api_key"
    except Exception:
        risk_label = "custom_endpoint" if provider_id.startswith("custom_openai_") else "api_key"
    return {
        "id": model_ref(provider_id, model_id),
        "kind": "model",
        "provider_id": provider_id,
        "model_id": model_id,
        "display_name": display_name or model_id,
        "visibility": list(visibility or DEFAULT_VISIBILITY),
        "pinned": True,
        "order": 1000,
        "recommended": source != "manual",
        "source": source,
        "capabilities_snapshot": dict(capabilities_snapshot or {}),
        "risk_label": risk_label,
        "active": True,
        "inactive_reason": "",
        "inactive_surfaces": {},
        "last_validated_at": "",
        "last_error": "",
    }


def _surface_inactive_reason(choice: dict[str, Any], surface: str) -> str:
    if choice.get("provider_id") == "codex":
        try:
            from providers.runtime import provider_status
            if not provider_status("codex").get("runtime_enabled"):
                return "Codex account is connected, but direct chat runtime is not enabled yet."
        except Exception:
            return "Codex direct chat runtime is not enabled yet."
    if choice.get("active") is False:
        return str(choice.get("inactive_reason") or choice.get("last_error") or "This Quick Choice is inactive.")
    inactive_surfaces = choice.get("inactive_surfaces")
    if surface and isinstance(inactive_surfaces, dict) and inactive_surfaces.get(surface):
        return str(inactive_surfaces[surface])
    return ""


def _annotated_choice(choice: dict[str, Any], surface: str) -> dict[str, Any]:
    annotated = dict(choice)
    reason = _surface_inactive_reason(choice, surface)
    annotated["active"] = not bool(reason)
    annotated["inactive_reason"] = reason
    return annotated


def _surface_unsupported_reason(surface: str) -> str:
    label = surface.replace("_", " ") if surface else "this surface"
    return f"Capability metadata says this model is not compatible with {label}."


def _is_auto_capability_reason(reason: Any) -> bool:
    return str(reason or "").startswith("Capability metadata says this model is not compatible with ")


def _inferred_capability_snapshot(choice: dict[str, Any]) -> dict[str, Any]:
    provider_id = str(choice.get("provider_id") or "")
    model_id = str(choice.get("model_id") or "")
    if not provider_id or not model_id or provider_id.startswith("custom_openai_"):
        return {}
    try:
        if provider_id == "ollama":
            from providers.ollama import ollama_model_info
            return ollama_model_info(model_id).capability_snapshot()
        if provider_id == "codex":
            from providers.codex import list_codex_model_infos
            for model_info in list_codex_model_infos():
                if model_info.model_id == model_id:
                    return model_info.capability_snapshot()
        from providers.catalog import model_info_from_metadata
        return model_info_from_metadata(provider_id, model_id).capability_snapshot()
    except Exception:
        return {}


def refresh_quick_choice_capability_snapshots() -> list[dict[str, Any]]:
    cfg = load_provider_config()
    quick = [choice for choice in cfg.get("quick_choices", []) if isinstance(choice, dict)]
    changed = False
    for choice in quick:
        if choice.get("kind") != "model":
            continue
        inferred = _inferred_capability_snapshot(choice)
        if not inferred:
            continue
        current = choice.get("capabilities_snapshot") if isinstance(choice.get("capabilities_snapshot"), dict) else {}
        if current != inferred:
            choice["capabilities_snapshot"] = inferred
            inactive_surfaces = choice.get("inactive_surfaces")
            if isinstance(inactive_surfaces, dict):
                choice["inactive_surfaces"] = {
                    surface: reason for surface, reason in inactive_surfaces.items()
                    if not _is_auto_capability_reason(reason)
                }
            if _is_auto_capability_reason(choice.get("last_error")):
                choice["last_error"] = ""
            changed = True
    if changed:
        cfg["quick_choices"] = quick
        return save_provider_config(cfg).get("quick_choices", [])
    return quick


def _media_tool_selection(tool_name: str, default_model: str) -> str:
    try:
        from tools import registry
        tool = registry.get_tool(tool_name)
        if tool:
            return str(tool.get_config("model", default_model) or default_model)
    except Exception:
        pass
    return default_model


def _quick_choice_for_media_selection(selection: str, surface: str) -> dict[str, Any] | None:
    if "/" not in selection:
        return None
    provider_id, model_id = selection.split("/", 1)
    if not provider_id or not model_id:
        return None
    try:
        from api_keys import get_key
        from providers.media import IMAGE_PROVIDER_META, VIDEO_PROVIDER_META
        provider_meta = IMAGE_PROVIDER_META if surface == "image" else VIDEO_PROVIDER_META
        meta = provider_meta.get(provider_id)
        if not meta or not get_key(meta["key"]):
            return None
    except Exception:
        return None
    try:
        from models import _cloud_model_cache
        cached = _cloud_model_cache.get(model_id)
        if isinstance(cached, dict) and cached.get("provider") == provider_id:
            snapshot = cached.get("capabilities_snapshot") if isinstance(cached.get("capabilities_snapshot"), dict) else {}
            display_name = str(cached.get("label") or model_id)
        else:
            from providers.catalog import model_info_from_metadata
            model_info = model_info_from_metadata(provider_id, model_id)
            snapshot = model_info.capability_snapshot()
            display_name = model_info.display_name
    except Exception:
        return None
    if not snapshot_supports_surface(snapshot, surface):
        return None
    choice = _quick_choice_for_model(
        model_id,
        provider_id=provider_id,
        display_name=display_name,
        source=f"{surface}_tool_default",
        capabilities_snapshot=snapshot,
    )
    if choice:
        choice["visibility"] = SURFACE_VISIBILITY.get(surface, [surface])
        choice["order"] = 50 if surface == "image" else 60
        choice["recommended"] = True
    return choice


def seed_configured_media_quick_choices() -> list[dict[str, Any]]:
    try:
        from tools.image_gen_tool import DEFAULT_MODEL as IMAGE_DEFAULT
        from tools.video_gen_tool import DEFAULT_MODEL as VIDEO_DEFAULT
    except Exception:
        return load_provider_config().get("quick_choices", [])

    candidates = [
        _quick_choice_for_media_selection(_media_tool_selection("image_gen", IMAGE_DEFAULT), "image"),
        _quick_choice_for_media_selection(_media_tool_selection("video_gen", VIDEO_DEFAULT), "video"),
    ]
    cfg = load_provider_config()
    quick = [choice for choice in cfg.get("quick_choices", []) if isinstance(choice, dict)]
    by_id = {choice.get("id"): choice for choice in quick}
    changed = False
    for choice in candidates:
        if not choice:
            continue
        existing = by_id.get(choice["id"])
        if existing:
            snapshot = choice.get("capabilities_snapshot") if isinstance(choice.get("capabilities_snapshot"), dict) else {}
            if existing.get("source") in {"image_tool_default", "video_tool_default"}:
                existing.update({
                    "display_name": choice["display_name"],
                    "visibility": choice["visibility"],
                    "capabilities_snapshot": snapshot,
                    "active": True,
                    "inactive_reason": "",
                    "inactive_surfaces": {},
                    "last_error": "",
                })
                changed = True
            continue
        quick.append(choice)
        by_id[choice["id"]] = choice
        changed = True
    if changed:
        cfg["quick_choices"] = quick
        return save_provider_config(cfg).get("quick_choices", [])
    return quick


def _choice_matches_surface(choice: dict[str, Any], surface: str, *, include_inactive: bool = False) -> bool:
    visibility = choice.get("visibility")
    supports_surface = snapshot_supports_surface(choice.get("capabilities_snapshot"), surface)
    inactive_reason = _surface_inactive_reason(choice, surface)
    if inactive_reason and not include_inactive:
        return False
    if surface and isinstance(visibility, list) and surface not in visibility:
        return supports_surface and bool(choice.get("capabilities_snapshot"))
    return supports_surface or (include_inactive and bool(inactive_reason))


def validate_quick_choices_for_surface(surface: str = "chat") -> list[dict[str, Any]]:
    if not surface:
        return migrate_legacy_starred_models()
    migrate_legacy_starred_models()
    quick = refresh_quick_choice_capability_snapshots()
    cfg = load_provider_config()
    changed = False
    for choice in quick:
        if choice.get("kind") != "model" or choice.get("active") is False:
            continue
        snapshot = choice.get("capabilities_snapshot")
        if not isinstance(snapshot, dict) or not snapshot:
            continue
        if snapshot_supports_surface(snapshot, surface):
            continue
        inactive_surfaces = choice.get("inactive_surfaces")
        if not isinstance(inactive_surfaces, dict):
            inactive_surfaces = {}
            choice["inactive_surfaces"] = inactive_surfaces
        reason = _surface_unsupported_reason(surface)
        if inactive_surfaces.get(surface):
            continue
        if inactive_surfaces.get(surface) != reason:
            inactive_surfaces[surface] = reason
            choice["last_error"] = reason
            changed = True
    if changed:
        cfg["quick_choices"] = quick
        return save_provider_config(cfg).get("quick_choices", [])
    return quick


def migrate_legacy_starred_models(*, cloud_models: Iterable[str] | None = None) -> list[dict[str, Any]]:
    try:
        from api_keys import get_cloud_config
        starred = list(get_cloud_config().get("starred_models", []))
    except Exception:
        starred = []
    if cloud_models is not None:
        available = set(cloud_models)
        starred = [model_id for model_id in starred if model_id in available]
    if not starred:
        return load_provider_config().get("quick_choices", [])
    cfg = load_provider_config()
    quick = list(cfg.get("quick_choices", []))
    existing = {choice.get("id") for choice in quick if isinstance(choice, dict)}
    changed = False
    for model_id in starred:
        choice = _quick_choice_for_model(model_id, source="legacy_starred_cloud")
        if choice and choice["id"] not in existing:
            choice["order"] = len(quick) + 100
            quick.append(choice)
            existing.add(choice["id"])
            changed = True
    if changed:
        cfg["quick_choices"] = quick
        save_provider_config(cfg)
    return quick


def add_quick_choice_for_model(
    model_id: str,
    *,
    provider_id: str | None = None,
    display_name: str | None = None,
    source: str = "manual",
    capabilities_snapshot: dict[str, Any] | None = None,
    visibility: Iterable[str] | None = None,
    surface: str | None = None,
) -> None:
    if surface and visibility is None:
        visibility = SURFACE_VISIBILITY.get(surface, [surface])
    choice = _quick_choice_for_model(
        model_id,
        provider_id=provider_id,
        display_name=display_name,
        source=source,
        capabilities_snapshot=capabilities_snapshot,
        visibility=visibility,
    )
    if not choice:
        return
    cfg = load_provider_config()
    quick = [c for c in cfg.get("quick_choices", []) if isinstance(c, dict)]
    for existing in quick:
        if existing.get("id") == choice["id"]:
            existing.update({
                "pinned": True,
                "display_name": existing.get("display_name") or choice["display_name"],
                "visibility": choice["visibility"],
                "active": True,
                "inactive_reason": "",
                "inactive_surfaces": {},
                "last_error": "",
            })
            if capabilities_snapshot:
                existing["capabilities_snapshot"] = dict(capabilities_snapshot)
            cfg["quick_choices"] = quick
            save_provider_config(cfg)
            return
    choice["order"] = len(quick) + 100
    quick.append(choice)
    cfg["quick_choices"] = quick
    save_provider_config(cfg)


def remove_quick_choice_for_model(model_id: str, *, provider_id: str | None = None) -> None:
    provider_id = provider_id or infer_provider_id(model_id)
    if not provider_id:
        return
    ref = model_ref(provider_id, model_id)
    cfg = load_provider_config()
    cfg["quick_choices"] = [
        c for c in cfg.get("quick_choices", [])
        if not isinstance(c, dict) or c.get("id") != ref
    ]
    save_provider_config(cfg)


def deactivate_quick_choice(
    ref: str | None = None,
    *,
    model_id: str | None = None,
    provider_id: str | None = None,
    surface: str | None = None,
    reason: str = "",
) -> bool:
    if not ref and model_id:
        provider = provider_id or infer_provider_id(model_id)
        ref = model_ref(provider, model_id) if provider else ""
    if not ref:
        return False
    cfg = load_provider_config()
    quick = [choice for choice in cfg.get("quick_choices", []) if isinstance(choice, dict)]
    changed = False
    inactive_reason = reason or "This Quick Choice is inactive."
    for choice in quick:
        if choice.get("id") != ref:
            continue
        if surface:
            inactive_surfaces = choice.get("inactive_surfaces")
            if not isinstance(inactive_surfaces, dict):
                inactive_surfaces = {}
                choice["inactive_surfaces"] = inactive_surfaces
            inactive_surfaces[surface] = inactive_reason
        else:
            choice["active"] = False
            choice["inactive_reason"] = inactive_reason
        choice["last_error"] = inactive_reason
        changed = True
        break
    if changed:
        cfg["quick_choices"] = quick
        save_provider_config(cfg)
    return changed


def deactivate_quick_choice_for_error(
    ref: str | None = None,
    *,
    model_id: str | None = None,
    provider_id: str | None = None,
    surface: str | None = None,
    error: BaseException | NormalizedProviderError,
) -> bool:
    normalized = error if isinstance(error, NormalizedProviderError) else normalize_provider_error(error)
    reason = normalized.next_action or normalized.message or "Choose a compatible provider model."
    return deactivate_quick_choice(ref, model_id=model_id, provider_id=provider_id, surface=surface, reason=reason)


def list_quick_choices(surface: str = "chat", *, include_routes: bool = False, include_inactive: bool = False) -> list[dict[str, Any]]:
    migrate_legacy_starred_models()
    quick = validate_quick_choices_for_surface(surface)
    choices = [
        _annotated_choice(choice, surface) for choice in quick
        if isinstance(choice, dict) and _choice_matches_surface(choice, surface, include_inactive=include_inactive)
    ]
    if include_routes:
        cfg = load_provider_config()
        for route in cfg.get("routes", []):
            if isinstance(route, dict) and route.get("enabled"):
                choices.append({
                    "id": route_ref(str(route.get("id"))),
                    "kind": "route",
                    "route_id": str(route.get("id")),
                    "display_name": f"Route: {route.get('display_name') or route.get('id')}",
                    "visibility": list(DEFAULT_VISIBILITY),
                    "order": 10,
                    "pinned": True,
                    "recommended": True,
                    "risk_label": route.get("data_policy", "route"),
                    "active": False,
                    "inactive_reason": "Routing execution is configured but not enabled until the routing phase.",
                })
    return sorted(choices, key=lambda c: (int(c.get("order", 1000)), str(c.get("display_name") or c.get("id"))))


def grouped_quick_choices(
    *,
    include_inactive: bool = True,
    include_routes: bool = False,
    include_media_defaults: bool = True,
) -> list[dict[str, Any]]:
    if include_media_defaults:
        seed_configured_media_quick_choices()
    groups: list[dict[str, Any]] = []
    group_defs = list(QUICK_CHOICE_SURFACE_GROUPS)
    if include_routes:
        group_defs.append({"id": "routes", "display_name": "Routes"})
    for group in group_defs:
        surface = group["id"]
        if surface == "routes":
            choices = [
                choice for choice in list_quick_choices("", include_routes=True, include_inactive=True)
                if choice.get("kind") == "route"
            ]
        else:
            choices = [
                choice for choice in list_quick_choices(surface, include_inactive=include_inactive)
                if choice.get("kind") == "model"
            ]
        groups.append({"id": surface, "display_name": group["display_name"], "choices": choices})
    return groups


def list_quick_model_ids(surface: str = "chat") -> list[str]:
    model_ids: list[str] = []
    for choice in list_quick_choices(surface):
        if choice.get("kind") == "model" and choice.get("model_id"):
            model_ids.append(str(choice["model_id"]))
    return model_ids


def resolve_selection(value: str) -> ResolvedSelection | None:
    raw = (value or "").strip()
    if not raw:
        return None
    choices = list_quick_choices("", include_routes=True, include_inactive=True)
    lower = raw.lower()
    for choice in choices:
        aliases = {
            str(choice.get("id", "")).lower(),
            str(choice.get("display_name", "")).lower(),
            str(choice.get("model_id", "")).lower(),
            str(choice.get("route_id", "")).lower(),
        }
        if lower in aliases:
            if choice.get("kind") == "route":
                return ResolvedSelection(
                    ref=str(choice.get("id")), kind="route", route_id=str(choice.get("route_id")),
                    display_name=str(choice.get("display_name") or choice.get("route_id")), legacy_value=raw,
                    active=False, reason="Routing execution is configured but not enabled until the routing phase.",
                )
            return ResolvedSelection(
                ref=str(choice.get("id")), kind="model", model_id=str(choice.get("model_id")),
                provider_id=str(choice.get("provider_id")), display_name=str(choice.get("display_name") or choice.get("model_id")),
                legacy_value=raw, active=choice.get("active") is not False, reason=str(choice.get("inactive_reason") or ""),
            )
    parsed = parse_model_ref(raw)
    if parsed:
        provider_id, model_id = parsed
        return ResolvedSelection(ref=raw, kind="model", provider_id=provider_id, model_id=model_id, display_name=model_id, legacy_value=raw)
    if raw.startswith("route:"):
        return ResolvedSelection(ref=raw, kind="route", route_id=raw.split(":", 1)[1], display_name=raw, legacy_value=raw, active=False, reason="Routing execution is not enabled yet.")
    provider_id = infer_provider_id(raw)
    if provider_id:
        return ResolvedSelection(ref=model_ref(provider_id, raw), kind="model", provider_id=provider_id, model_id=raw, display_name=raw, legacy_value=raw)
    return ResolvedSelection(ref=f"model:local:{raw}", kind="model", provider_id="local", model_id=raw, display_name=raw, legacy_value=raw)