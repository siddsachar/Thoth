from __future__ import annotations

from providers.catalog import list_provider_definitions
from providers.runtime import provider_status
from providers.selection import list_quick_choices


def _provider_group(provider_id: str, risk_label: str) -> str:
    if provider_id == "ollama":
        return "Local"
    if risk_label == "subscription":
        return "Subscription Accounts"
    if provider_id.startswith("custom_openai_") or risk_label == "custom_endpoint":
        return "Custom Endpoints"
    return "API Providers"


def _cached_model_stats(provider_id: str) -> dict[str, int]:
    stats = {"model_count": 0, "chat_count": 0, "media_count": 0}
    try:
        from models import _cloud_model_cache, _sync_custom_model_cache
        _sync_custom_model_cache()
        for info in _cloud_model_cache.values():
            if not isinstance(info, dict) or info.get("provider") != provider_id:
                continue
            stats["model_count"] += 1
            snapshot = info.get("capabilities_snapshot") if isinstance(info.get("capabilities_snapshot"), dict) else {}
            tasks = set(snapshot.get("tasks") or []) if isinstance(snapshot.get("tasks"), list) else set()
            if tasks.intersection({"chat", "responses"}) or not tasks:
                stats["chat_count"] += 1
            if tasks.intersection({"image_generation", "image_edit", "video_generation"}):
                stats["media_count"] += 1
    except Exception:
        pass
    return stats


def provider_status_cards() -> list[dict]:
    cards: list[dict] = []
    for definition in list_provider_definitions():
        status = provider_status(definition.id)
        cache_stats = _cached_model_stats(definition.id)
        model_count = status.get("model_count")
        if model_count is None:
            model_count = cache_stats["model_count"] or None
        cards.append({
            "provider_id": definition.id,
            "display_name": definition.display_name,
            "configured": bool(status.get("configured")),
            "source": status.get("source") or "",
            "fingerprint": status.get("fingerprint") or "",
            "auth_method": status.get("auth_method") or "",
            "expires_at": status.get("expires_at") or "",
            "account_id_hash": status.get("account_id_hash") or "",
            "plan_type": status.get("plan_type") or "",
            "external_reference_label": status.get("external_reference_label") or "",
            "external_reference_path_hash": status.get("external_reference_path_hash") or "",
            "external_reference_exists": bool(status.get("external_reference_exists")),
            "cli_installed": bool(status.get("cli_installed")),
            "runtime_enabled": bool(status.get("runtime_enabled")),
            "last_error": status.get("last_error") or "",
            "model_count": model_count,
            "chat_count": cache_stats["chat_count"],
            "media_count": cache_stats["media_count"],
            "risk_label": definition.risk_label,
            "icon": definition.icon,
            "group": _provider_group(definition.id, definition.risk_label),
        })
    return cards


def summarize_providers() -> str:
    lines = ["**Providers**"]
    for card in provider_status_cards():
        state = "configured" if card["configured"] else "not set"
        source = f" ({card['source']})" if card.get("source") else ""
        count_label = "local model(s)" if card.get("provider_id") == "ollama" else "catalog model(s)"
        count = f", {card['model_count']} {count_label}" if card.get("model_count") is not None else ""
        lines.append(f"- {card['display_name']}: {state}{source}{count}")
    quick_count = len([c for c in list_quick_choices("status_tool") if c.get("kind") == "model"])
    lines.append(f"- Quick Choices: {quick_count} model(s)")
    return "\n".join(lines)