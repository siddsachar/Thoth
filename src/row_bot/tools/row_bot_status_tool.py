"""Row-Bot status / introspection tool — lets the agent query its own configuration.

Provides read operations (always allowed) for settings, channel status,
memory stats, model info, provider status, and API key validity. Write operations
(changing settings, toggling channels) require user confirmation via
LangGraph interrupt().
"""

from __future__ import annotations

import json
import logging
import os
import re

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.data_paths import get_row_bot_data_dir
from row_bot.tools.base import BaseTool
from row_bot.tools import registry
from row_bot.tools.approval_gate import gate_action

logger = logging.getLogger(__name__)

_DATA_DIR = get_row_bot_data_dir()


def _approval_gate_bool(payload: dict, *, blocked_message: str) -> bool:
    return gate_action(
        payload,
        blocked_message=blocked_message,
        cancelled_message="",
    ) is None


# ═════════════════════════════════════════════════════════════════════════════
# INPUT SCHEMAS
# ═════════════════════════════════════════════════════════════════════════════

class _StatusQueryInput(BaseModel):
    category: str = Field(
        description=(
            "What to query. One of: 'overview' (full status summary), "
            "'version' (Row-Bot version number), "
            "'model' (current Brain model, provider info, and pinned Brain model choices), "
            "'channels' (messaging channel status), "
            "'memory' (knowledge graph stats), "
            "'skills' (Skill Library availability and pinned defaults), "
            "'tools' (effective active profile tools plus global enabled/disabled tools), "
            "'plugins' (installed plugins, stale legacy plugins, and plugin runtime tools), "
            "'mcp' (external MCP server/tool status), "
            "'providers' (provider connections, credential sources, and Quick Choices), "
            "'insights' (active dream-cycle insights and last analysis), "
            "'evolution' (controlled self-evolution proposals, action runs, rejections, and curator dry-runs), "
            "'api_keys' (legacy/API key storage status — never returns key values), "
            "'identity' (configured name and personality), "
            "'tasks' (active scheduled tasks summary), "
            "'agents' (durable Agent Runs, subagents, workflow mirrors, writer locks, and V1 defaults), "
            "'agent_profiles' (Agent Profile Library counts, sources, active profile, and selected tools), "
            "'goals' (current Goal Mode status, turn budgets, progress, and blockers), "
            "'vision' (vision/camera model, settings, and pinned Vision choices), "
            "'image_gen' (image generation model and pinned Image choices), "
            "'video_gen' (video generation model and pinned Video choices), "
            "'voice' (TTS and STT settings), "
            "'config' (context window, dream cycle, wiki vault, memory extraction), "
            "'designer' (Designer project count and recent projects), "
            "'logs' (recent warnings and errors from the log file), "
            "'errors' (recent errors with tracebacks — use to diagnose failures), "
            "'updates' (auto-update channel + last check + available version)."
        )
    )


class _SettingUpdateInput(BaseModel):
    setting: str = Field(
        description=(
            "The setting to change. One of: "
            "'thread_model' (set the current conversation's Brain model override; value must be an active pinned Brain Quick Choice canonical ref or exact label; use 'default' to clear), "
            "'default_model' (switch the global Brain default for new conversations and workflows; value must be an active pinned Brain Quick Choice canonical ref or exact label), "
            "'model' (legacy alias for default_model outside threaded chat; do not use for conversation model changes), "
            "'vision_model' (switch Vision model; value may be an installed local vision model, provider vision model, or Vision Quick Choice), "
            "'name' (change assistant name), "
            "'personality' (change personality text), "
            "'context_size' (local model context window — value is token count e.g. '65536'), "
            "'cloud_context_size' (provider model context cap — value is token count), "
            "'dream_cycle' (enable/disable — value is 'on' or 'off'), "
            "'dream_window' (dream cycle hours — value is 'START-END' e.g. '1-5'), "
            "'skill_toggle' (make a Skill Library item Available/Off — value is 'skill_name:on' or 'skill_name:off'), "
            "'skill_pin' (pin/unpin a Skill Library item as default active — value is 'skill_name:on' or 'skill_name:off'), "
            "'tool_toggle' (enable/disable a tool — value is 'tool_name:on' or 'tool_name:off'; use 'mcp:on/off' for the global MCP client), "
            "'image_gen_model' (set image generation model — value may be provider/model-id, bare model id, or model label), "
            "'video_gen_model' (set video generation model — value may be provider/model-id, bare model id, or model label), "
            "'run_dream_cycle' (manually trigger the dream cycle — value is 'now'), "
            "'self_improvement' (enable/disable self-improvement — value is 'on' or 'off')."
        )
    )
    value: str = Field(description="The new value for the setting.")


def _normalize_provider_model_value(setting: str, value: str) -> str:
    """Return a canonical provider/model value when the media model is unique."""
    normalized = (value or "").strip()
    if not normalized:
        return normalized

    if setting == "image_gen_model":
        from row_bot.tools.image_gen_tool import get_available_image_models
        options = get_available_image_models()
    elif setting == "video_gen_model":
        from row_bot.tools.video_gen_tool import get_available_video_models
        options = get_available_video_models()
    else:
        return normalized

    if normalized in options:
        return normalized

    def _media_lookup_tokens(text: str) -> set[str]:
        raw = re.sub(r"^[^A-Za-z0-9]+", "", str(text or "")).strip()
        tokens = {_normalize_lookup_token(raw)}
        raw_without_icon = re.sub(r"^[A-Za-z]{1,3}\s{2,}", "", raw).strip()
        if raw_without_icon and raw_without_icon != raw:
            tokens.add(_normalize_lookup_token(raw_without_icon))
        stripped = re.sub(r"\s*\([^)]*\)\s*$", "", raw).strip()
        tokens.add(_normalize_lookup_token(stripped))
        without_icon_prefix = re.sub(r"^[A-Za-z]{1,3}\s{2,}", "", stripped).strip()
        if without_icon_prefix and without_icon_prefix != stripped:
            tokens.add(_normalize_lookup_token(without_icon_prefix))
        if "/" in stripped:
            tokens.add(_normalize_lookup_token(stripped.split("/", 1)[1]))
        return {token for token in tokens if token}

    desired = _normalize_lookup_token(normalized)
    desired_tokens = _media_lookup_tokens(normalized)
    exact_matches: list[str] = []
    fuzzy_matches: list[str] = []
    for config_value, label in options.items():
        provider, _, model_id = config_value.partition("/")
        candidates = {
            config_value,
            model_id or config_value,
            label,
            f"{provider} {model_id}",
            f"{provider} {label}",
        }
        tokens = set().union(*(_media_lookup_tokens(candidate) for candidate in candidates))
        if desired in tokens:
            exact_matches.append(config_value)
        elif desired_tokens & tokens:
            fuzzy_matches.append(config_value)

    if len(exact_matches) == 1:
        return exact_matches[0]
    if not exact_matches and len(fuzzy_matches) == 1:
        return fuzzy_matches[0]
    return normalized


def _normalize_lookup_token(value: str) -> str:
    """Normalize a human/tool label into a stable lookup token."""
    token = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
    token = re.sub(r"_+", "_", token).strip("_")
    if token.endswith("_tool"):
        token = token[:-5]
    return token


def _model_setting_supported_message() -> str:
    return (
        "model, vision_model, name, personality, context_size, cloud_context_size, "
        "dream_cycle, dream_window, skill_toggle, skill_pin, tool_toggle, image_gen_model, "
        "video_gen_model, run_dream_cycle, self_improvement"
    )


def _surface_label(surface: str) -> str:
    return {"chat": "Brain", "vision": "Vision"}.get(surface, surface.replace("_", " ").title())


def _pinned_choice_status_lines(
    surface: str,
    heading: str,
    *,
    value_label: str = "Canonical ref",
    value_key: str = "canonical_ref",
    limit: int | None = 8,
) -> list[str]:
    try:
        from row_bot.providers.selection import pinned_model_choice_summaries

        choices = pinned_model_choice_summaries(surface, include_inactive=True)
    except Exception as exc:
        return ["", f"**{heading}**", f"- Unavailable: {exc}"]

    lines = ["", f"**{heading}**"]
    if not choices:
        lines.append("- None pinned. Pin choices in Settings -> Models.")
        return lines

    active = [choice for choice in choices if choice.get("active") is not False]
    inactive = [choice for choice in choices if choice.get("active") is False]
    if surface == "chat":
        lines.append(
            "- For natural Brain model requests, choose from this list and call tools with the canonical ref."
        )
    shown_active = active if not limit or limit < 0 else active[:limit]
    for choice in shown_active:
        value = str(choice.get(value_key) or choice.get("canonical_ref") or "")
        lines.append(
            f"- {choice.get('display_name')} - {choice.get('provider')}: "
            f"{value_label}: {value} (provider_id: {choice.get('provider_id')}, model_id: {choice.get('model_id')})"
        )
    hidden_count = len(active) - len(shown_active)
    if hidden_count > 0:
        lines.append(f"- Plus {hidden_count} more active pinned choice(s).")
    if inactive:
        lines.append(f"- Not usable now: {len(inactive)} pinned choice(s)")
        for choice in inactive[:3]:
            value = str(choice.get(value_key) or choice.get("canonical_ref") or "")
            reason = str(choice.get("reason") or "inactive")
            lines.append(
                f"  - {choice.get('display_name')} - {choice.get('provider')}: "
                f"{value_label}: {value} ({reason})"
            )
    return lines


def _resolve_model_update_value(value: str, *, surface: str) -> tuple[str | None, str | None]:
    """Resolve a status-tool model update to a runnable model id or an error."""
    if surface == "chat":
        try:
            from row_bot.providers.selection import resolve_catalog_model_selection

            resolved = resolve_catalog_model_selection(
                value,
                surface="chat",
                require_agent_ready=True,
                require_pinned=True,
            )
            return resolved.ref, None
        except Exception as exc:
            return None, str(exc)

    from row_bot.models import is_model_local, list_cloud_models, list_cloud_vision_models
    from row_bot.providers.capabilities import snapshot_supports_surface
    from row_bot.providers.selection import list_quick_choices, model_ref, resolve_selection

    raw = (value or "").strip()
    if not raw:
        return None, f"No {_surface_label(surface)} model was provided."

    resolved = resolve_selection(raw)
    if not resolved:
        return None, f"Model '{raw}' not found. Pin it in Settings → Models first."
    if resolved.kind == "route":
        return None, f"Route '{raw}' is configured but runtime routing is not enabled yet. Choose a direct model Quick Choice."
    if resolved.active is False:
        reason = f" {resolved.reason}" if resolved.reason else ""
        return None, f"Model '{raw}' is inactive.{reason}"

    provider_id = resolved.provider_id or "local"
    model_id = resolved.model_id
    quick_choices = {
        str(choice.get("id")): choice
        for choice in list_quick_choices(surface, include_inactive=True)
        if isinstance(choice, dict)
    }
    quick_choice = quick_choices.get(resolved.ref)
    if quick_choice:
        if quick_choice.get("active") is False:
            reason = str(quick_choice.get("inactive_reason") or "This Quick Choice is inactive.")
            return None, f"Model '{raw}' is not active for {_surface_label(surface)}. {reason}"
        snapshot = quick_choice.get("capabilities_snapshot") if isinstance(quick_choice.get("capabilities_snapshot"), dict) else {}
        if snapshot and not snapshot_supports_surface(snapshot, surface):
            return None, f"Model '{raw}' is not compatible with {_surface_label(surface)}."
        return model_id, None

    if provider_id in {"local", "ollama"}:
        if not is_model_local(model_id):
            return None, f"Local model '{raw}' is not installed. Install it with Ollama or pin an installed model in Settings → Models."
        if surface == "vision":
            from row_bot.providers.capability_resolution import resolve_capability_snapshot

            snapshot = resolve_capability_snapshot("ollama", model_id)
            if not snapshot or not snapshot_supports_surface(snapshot, "vision"):
                return None, f"Local model '{raw}' is installed, but Row-Bot does not have Vision capability metadata for it. Choose a Vision model from Settings → Models."
        return model_id, None

    known_provider_models = set(list_cloud_models(provider_id))
    if model_id not in known_provider_models:
        return None, f"Provider model '{raw}' is not in the current catalog. Refresh Providers or pin it from Settings → Models first."
    if surface == "vision" and model_id not in set(list_cloud_vision_models()):
        provider_ref = model_ref(provider_id, model_id)
        from row_bot.models import is_cloud_vision_model
        if not is_cloud_vision_model(provider_ref):
            return None, f"Provider model '{raw}' is not marked as Vision-capable in the catalog."
    return model_id, None


def _tool_display_label(tool) -> str:
    """Return a friendly display label without leading emoji."""
    label = getattr(tool, "display_name", "") or getattr(tool, "name", "")
    label = re.sub(r"^[^A-Za-z0-9]+", "", str(label)).strip()
    return label or getattr(tool, "name", "")


def _resolve_tool_name(value: str) -> tuple[str | None, str | None, list[str]]:
    """Resolve a tool alias like ``video_generation`` to a registered tool."""
    from row_bot.tools import registry as tool_registry

    raw = (value or "").strip()
    if not raw:
        return None, None, []

    direct = tool_registry.get_tool(raw)
    if direct is not None:
        return direct.name, _tool_display_label(direct), []

    normalized = _normalize_lookup_token(raw)
    alias_to_name: dict[str, str] = {}
    for tool in tool_registry.get_all_tools():
        aliases = {
            tool.name,
            _normalize_lookup_token(tool.name),
            _normalize_lookup_token(_tool_display_label(tool)),
        }
        for alias in aliases:
            if alias:
                alias_to_name.setdefault(alias, tool.name)

    resolved_name = alias_to_name.get(normalized)
    if resolved_name:
        resolved_tool = tool_registry.get_tool(resolved_name)
        return resolved_name, _tool_display_label(resolved_tool), []

    suggestions: list[str] = []
    for tool in tool_registry.get_all_tools():
        label = _tool_display_label(tool)
        if normalized and (normalized in _normalize_lookup_token(label)
                           or normalized in _normalize_lookup_token(tool.name)):
            suggestions.append(label)
    return None, None, suggestions[:3]


# ═════════════════════════════════════════════════════════════════════════════
# QUERY HANDLERS (read-only, always allowed)
# ═════════════════════════════════════════════════════════════════════════════

def _resolve_manual_skill_name(value: str) -> tuple[str | None, str | None, list[str]]:
    """Resolve a Skill Library name/display label to a manual skill id."""
    from row_bot.skills import get_manual_skill_statuses

    raw = (value or "").strip()
    if not raw:
        return None, None, []

    statuses = get_manual_skill_statuses()
    for skill, _is_available in statuses:
        if raw == skill.name:
            return skill.name, skill.display_name, []

    normalized = _normalize_lookup_token(raw)
    alias_to_name: dict[str, str] = {}
    labels: dict[str, str] = {}
    for skill, _is_available in statuses:
        labels[skill.name] = skill.display_name
        for alias in {
            skill.name,
            _normalize_lookup_token(skill.name),
            _normalize_lookup_token(skill.display_name),
        }:
            if alias:
                alias_to_name.setdefault(alias, skill.name)

    resolved_name = alias_to_name.get(normalized)
    if resolved_name:
        return resolved_name, labels.get(resolved_name, resolved_name), []

    suggestions: list[str] = []
    for skill, _is_available in statuses:
        label = skill.display_name
        if normalized and (
            normalized in _normalize_lookup_token(label)
            or normalized in _normalize_lookup_token(skill.name)
        ):
            suggestions.append(label)
    return None, None, suggestions[:3]


def _query_overview() -> str:
    """Full status summary across all categories."""
    from row_bot.version import __version__
    parts = [f"**Row-Bot v{__version__}**"]
    for cat in ("model", "providers", "vision", "image_gen", "video_gen", "voice", "api_keys", "memory",
                "channels", "skills", "tools", "plugins", "mcp", "identity", "tasks", "agents", "agent_profiles", "goals",
                "insights", "evolution", "config", "designer", "updates"):
        try:
            if cat == "model":
                parts.append(_query_model(compact_pinned=True))
            else:
                parts.append(_QUERY_HANDLERS[cat]())
        except Exception as exc:
            parts.append(f"[{cat}] Error: {exc}")
    return "\n\n".join(parts)


def _query_model(*, compact_pinned: bool = False) -> str:
    try:
        from row_bot.models import (get_current_model, get_context_size, get_provider_emoji,
                            _active_model_override,
                            get_user_context_size, get_cloud_context_size)
        from row_bot.providers.readiness import evaluate_runtime_readiness
        from row_bot.providers.resolution import resolve_provider_config
        from row_bot.providers.selection import provider_display_label
        default_model = get_current_model()
        override = _active_model_override.get("")
        model = override if override else default_model
        ctx = get_context_size(model)
        resolved = resolve_provider_config(model, allow_legacy_local=True)
        local = resolved.execution_location == "local" or resolved.risk_label == "local_private"
        provider_label = resolved.provider_display_name or provider_display_label(resolved.provider_id)
        if resolved.provider_id == "ollama":
            type_label = "Local (Ollama)"
        elif str(resolved.provider_id).startswith("custom_openai_"):
            type_label = "Local custom endpoint" if local else "Custom endpoint"
        else:
            type_label = f"Provider ({provider_label})"
        emoji = get_provider_emoji(model)
        active_runtime = {}
        try:
            from row_bot.agent import get_active_runtime_context

            active_runtime = get_active_runtime_context()
        except Exception:
            active_runtime = {}
        runtime = evaluate_runtime_readiness(model, probe_ollama_tools=False)
        mode_labels = {
            "agent": "Agent Mode",
            "chat_only": "Chat Only - tools and actions are off",
            "blocked": "Unavailable",
        }
        readiness_label = mode_labels.get(runtime.selected_mode, runtime.selected_mode)
        lines = [
            "**Current Model**",
            f"- Model: {emoji} {model}",
            f"- Runtime model: {resolved.runtime_model}",
            f"- Provider: {provider_label}",
            f"- Type: {type_label}",
            f"- Effective context: {ctx:,} tokens",
            f"- Readiness: {readiness_label} ({runtime.selection_reason})",
        ]
        selected_runtime = str(active_runtime.get("selected_runtime_mode") or "").strip()
        requested_runtime = str(active_runtime.get("requested_runtime_mode") or "").strip()
        runtime_surface = str(active_runtime.get("runtime_surface") or "").strip()
        if selected_runtime:
            selected_label = mode_labels.get(selected_runtime, selected_runtime)
            requested = f", requested {requested_runtime}" if requested_runtime else ""
            surface = f" on {runtime_surface}" if runtime_surface else ""
            lines.append(f"- Active turn runtime: {selected_label}{requested}{surface}")
        if local:
            lines.append(f"- Local context cap: {get_user_context_size():,} tokens")
        else:
            lines.append(f"- Provider context cap: {get_cloud_context_size():,} tokens")
        if override and override != default_model:
            lines.append(f"- ⚠️ Override active (global default: {default_model})")
        lines.extend(_pinned_choice_status_lines(
            "chat",
            "Pinned Brain Model Choices",
            value_label="Canonical ref",
            value_key="canonical_ref",
            limit=8 if compact_pinned else 0,
        ))
        return "\n".join(lines)
    except Exception as exc:
        return f"**Current Model**\nError retrieving model info: {exc}"


def _query_channels() -> str:
    try:
        from row_bot.channels.registry import all_channels
        channels = all_channels()
        if not channels:
            return "**Channels**\nNo channels registered."
        lines = ["**Channels**"]
        for ch in channels:
            status = "running" if ch.is_running() else ("configured" if ch.is_configured() else "not configured")
            lines.append(f"- {ch.name}: {status}")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Channels**\nError: {exc}"


def _query_memory() -> str:
    try:
        from row_bot.knowledge_graph import count_entities, count_relations
        entities = count_entities()
        relations = count_relations()
        return (
            f"**Knowledge Graph**\n"
            f"- Entities: {entities}\n"
            f"- Relations: {relations}"
        )
    except Exception as exc:
        return f"**Knowledge Graph**\nError: {exc}"


def _query_skills() -> str:
    try:
        from row_bot.skills import get_default_active_skill_names, get_manual_skill_statuses, is_pinned
        skill_statuses = get_manual_skill_statuses()
        if not skill_statuses:
            return "**Skills**\nNo skills found."
        available_count = sum(1 for _skill, is_available in skill_statuses if is_available)
        off_count = len(skill_statuses) - available_count
        pinned_count = sum(
            1
            for skill, is_available in skill_statuses
            if is_available and is_pinned(skill.name)
        )
        skill_labels = {skill.name: skill.display_name for skill, _is_available in skill_statuses}

        def _default_labels(surface: str) -> str:
            names = get_default_active_skill_names(surface)
            if not names:
                return "None"
            return ", ".join(skill_labels.get(name, name) for name in names)

        lines = [f"**Skill Library** ({available_count} available, {off_count} off, {pinned_count} pinned)"]
        lines.append("- Available means a skill can be selected in chat and suggested when relevant.")
        lines.append("- Pinned skills start active in new chats, tasks, designer threads, and developer threads; users can remove them per workflow.")
        lines.append("- Designer threads also start with Design Creator when it is available.")
        lines.append("- Tool guides are separate tool instructions and are not listed here.")
        lines.append(f"- Default chat skills: {_default_labels('chat')}")
        lines.append(f"- Default task skills: {_default_labels('task')}")
        lines.append(f"- Default designer skills: {_default_labels('designer')}")
        lines.append(f"- Default developer skills: {_default_labels('developer')}")
        for skill, is_available in skill_statuses:
            status = "Available" if is_available else "Off"
            markers: list[str] = []
            if is_available and is_pinned(skill.name):
                markers.append("Pinned default")
            if skill.name in get_default_active_skill_names("designer") and not is_pinned(skill.name):
                markers.append("Designer default")
            marker_text = f" ({', '.join(markers)})" if markers else ""
            lines.append(f"- {skill.display_name}: {status}{marker_text}")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Skills**\nError: {exc}"


def _query_tools() -> str:
    try:
        from row_bot.tools.registry import get_all_tools, is_enabled
        tools = get_all_tools()
        if not tools:
            return "**Tools**\nNo tools registered."
        active_developer = False
        try:
            from row_bot.developer.tool_context import get_thread_id, get_workspace_id, infer_workspace_id_from_thread
            active_developer = bool(get_workspace_id() or infer_workspace_id_from_thread(get_thread_id()))
        except Exception:
            active_developer = False

        enabled = [t for t in tools if is_enabled(t.name)]
        contextual = [t for t in tools if t.name == "developer" and active_developer and not is_enabled(t.name)]
        disabled = [
            t for t in tools
            if not is_enabled(t.name) and not (t.name == "developer" and active_developer)
        ]
        suffix = f", {len(contextual)} contextual" if contextual else ""
        lines = [
            "**Tools**",
            f"- Global catalog: {len(enabled)} enabled{suffix}, {len(disabled)} disabled",
        ]
        profile_scope = _active_thread_profile_scope()
        profile = profile_scope.get("profile") if isinstance(profile_scope, dict) else None
        allow_tools = profile_scope.get("allow_tools") if isinstance(profile_scope, dict) else []
        runtime_surface = (
            str(profile_scope.get("runtime_surface") or "").strip()
            if isinstance(profile_scope, dict)
            else ""
        )
        if isinstance(profile, dict) and allow_tools:
            active_label = profile.get("display_name") or profile.get("slug") or "active profile"
            state = "enabled" if profile.get("enabled", True) else "disabled"
            lines.extend([
                "",
                "**Effective Thread Tool Scope**",
                f"- Active profile: {active_label} ({profile.get('slug')}, {state})",
                (
                    "- Runtime enforcement: selected tools are runtime-bound for this thread; "
                    "other global tools are not bound while this profile is active."
                ),
                f"- Profile-selected tool groups: {_format_selected_tool_ids(allow_tools)}",
            ])
            runtime_allowlist = profile_scope.get("runtime_allowlist") or []
            if runtime_allowlist and list(runtime_allowlist) != list(allow_tools):
                lines.append(
                    f"- Runtime allow-list: {_format_selected_tool_ids(runtime_allowlist)}"
                )
            if runtime_surface.startswith("agent_child"):
                lines.append(
                    "- skill_search and skill_load, when present, are separate from the tool-group "
                    "allowlist; loaded skills can guide the child but cannot add tools or relax its "
                    "profile, approval, or workspace boundaries."
                )
            lines.append("")
            lines.append("Global tools:")
        elif isinstance(profile, dict):
            active_label = profile.get("display_name") or profile.get("slug") or "active profile"
            state = "enabled" if profile.get("enabled", True) else "disabled"
            lines.extend([
                "",
                "**Effective Thread Tool Scope**",
                f"- Active profile: {active_label} ({profile.get('slug')}, {state})",
                (
                    "- Runtime enforcement: no profile allow-list is active; "
                    "this profile inherits all globally enabled tools."
                ),
            ])
            if runtime_surface.startswith("agent_child"):
                lines.append(
                    "- skill_search and skill_load, when present, are separate from the tool-group "
                    "allowlist; loaded skills can guide the child but cannot add tools or relax its "
                    "profile, approval, or workspace boundaries."
                )
            lines.extend(["", "Global tools:"])
        for t in enabled:
            lines.append(f"- ✅ {t.display_name}")
        for t in contextual:
            lines.append(f"- contextual: {t.display_name} (active for the current Developer workspace)")
        for t in disabled:
            lines.append(f"- ❌ {t.display_name}")
        try:
            from row_bot.plugins import registry as plugin_registry

            plugin_records = plugin_registry.get_enabled_plugin_tool_records()
            if plugin_records:
                lines.append("")
                lines.append("Plugin tools:")
                for item in plugin_records:
                    runtime_name = str(item.get("runtime_name") or "").strip()
                    if not runtime_name:
                        continue
                    plugin_name = str(item.get("plugin_name") or item.get("plugin_id") or "Plugin")
                    label = str(item.get("label") or runtime_name)
                    lines.append(f"- ✅ {plugin_name}: {label} ({runtime_name})")
            else:
                lines.append("")
                lines.append("Plugin tools: none currently registered.")
        except Exception as exc:
            lines.append("")
            lines.append(f"Plugin tools: unavailable ({exc})")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Tools**\nError: {exc}"


def _query_plugins() -> str:
    try:
        from row_bot.channels import registry as channel_registry
        from row_bot.plugins import loader as plugin_loader
        from row_bot.plugins import registry as plugin_registry
        from row_bot.plugins import state as plugin_state

        summary = plugin_loader.get_load_summary()
        manifests = sorted(plugin_registry.get_loaded_manifests(), key=lambda item: item.name.lower())
        enabled_manifests = [manifest for manifest in manifests if plugin_state.is_plugin_enabled(manifest.id)]
        plugin_records = plugin_registry.get_enabled_plugin_tool_records()
        stale_results = [result for result in summary.get("results", []) if getattr(result, "stale", False)]
        failed_results = [result for result in summary.get("results", []) if not getattr(result, "success", False)]

        lines = [
            "**Plugins**",
            (
                f"- Installed/loaded: {len(manifests)}; enabled: {len(enabled_manifests)}; "
                f"failed: {len(failed_results)}; stale legacy: {len(stale_results)}"
            ),
        ]
        if manifests:
            lines.append("- Plugin states:")
            for manifest in manifests:
                state_label = "enabled" if plugin_state.is_plugin_enabled(manifest.id) else "disabled"
                lines.append(f"  - {manifest.name} ({manifest.id}) v{manifest.version}: {state_label}")
        if plugin_records:
            lines.append("- Enabled plugin tools:")
            for item in plugin_records:
                runtime_name = str(item.get("runtime_name") or "").strip()
                if not runtime_name:
                    continue
                plugin_name = str(item.get("plugin_name") or item.get("plugin_id") or "Plugin")
                label = str(item.get("label") or runtime_name)
                lines.append(f"  - {plugin_name}: {label} ({runtime_name})")
        else:
            lines.append("- Enabled plugin tools: none")

        plugin_channels: list[str] = []
        for channel in channel_registry.all_channels():
            source = channel_registry.get_source(channel.name)
            if source.kind == "plugin":
                plugin_channels.append(f"{source.label or source.plugin_id}: {channel.display_name} ({channel.name})")
        if plugin_channels:
            lines.append("- Plugin channels:")
            lines.extend(f"  - {item}" for item in sorted(plugin_channels))

        if stale_results:
            lines.append("- Stale legacy plugins moved aside:")
            for result in stale_results:
                detail = result.stale_path or "stale_plugins"
                lines.append(f"  - {result.plugin_id}: {detail}")
        if failed_results:
            lines.append("- Failed plugins:")
            for result in failed_results:
                lines.append(f"  - {result.plugin_id}: {result.error or 'unknown error'}")
        if not manifests and not stale_results and not failed_results:
            lines.append("- No plugins installed.")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Plugins**\nError: {exc}"


def _query_mcp() -> str:
    try:
        from row_bot.mcp_client.runtime import get_status_summary
        summary = get_status_summary()
        lines = [
            "**MCP Client**",
            f"- Global enable: {'on' if summary.get('enabled') else 'off'}",
            f"- MCP SDK: {'available' if summary.get('sdk_available') else 'missing'}",
            f"- Servers: {summary.get('enabled_server_count', 0)} enabled / {summary.get('server_count', 0)} configured / {summary.get('connected_server_count', 0)} connected",
            f"- Tools: {summary.get('enabled_tool_count', 0)} enabled / {summary.get('tool_count', 0)} discovered",
            f"- Approval-gated tools: {summary.get('destructive_tool_count', 0)}",
        ]
        servers = summary.get("servers", {})
        if servers:
            lines.append("- Server details:")
            for name, status in sorted(servers.items()):
                detail = f"  - {name}: {status.get('status', 'unknown')} ({status.get('transport', 'stdio')})"
                if status.get("last_error"):
                    detail += f" — last error: {status['last_error']}"
                lines.append(detail)
        return "\n".join(lines)
    except Exception as exc:
        return f"**MCP Client**\nError: {exc}"


def _query_api_keys() -> str:
    try:
        from row_bot.api_keys import get_key
        providers = {
            "OpenRouter": "OPENROUTER_API_KEY",
            "OpenAI": "OPENAI_API_KEY",
            "Anthropic": "ANTHROPIC_API_KEY",
            "Google AI": "GOOGLE_API_KEY",
            "xAI": "XAI_API_KEY",
            "Tavily": "TAVILY_API_KEY",
        }
        lines = ["**API Keys** (never shows actual key values)"]
        for label, env_var in providers.items():
            configured = bool(get_key(env_var))
            lines.append(f"- {label}: {'configured' if configured else 'not set'}")
        return "\n".join(lines)
    except Exception as exc:
        return f"**API Keys**\nError: {exc}"


def _query_providers() -> str:
    try:
        from row_bot.providers.status import summarize_providers
        return summarize_providers()
    except Exception as exc:
        return f"**Providers**\nError: {exc}"


def _query_insights() -> str:
    try:
        from row_bot.insights import get_active_insights, get_insights_meta

        active = get_active_insights()
        meta = get_insights_meta()
        lines = ["**Insights**"]
        lines.append(f"- Active: {len(active)}")
        lines.append(f"- Last analysis: {meta.get('last_analysis') or 'never'}")
        total = meta.get("total_generated")
        if total is not None:
            lines.append(f"- Total generated: {total}")
        if active:
            lines.append("- Active insights:")
            for item in active[:8]:
                category = item.get("category", "unknown")
                severity = item.get("severity", "info")
                status = item.get("status", "new")
                title = item.get("title", "Untitled insight")
                lines.append(f"  - [{severity}/{category}/{status}] {title}")
            if len(active) > 8:
                lines.append(f"  - … and {len(active) - 8} more")
        else:
            lines.append("- No active insights")
        try:
            from row_bot.evolution import TERMINAL_PROPOSAL_STATUSES, list_action_runs, list_proposals

            proposals = list_proposals()
            active_proposals = [
                proposal
                for proposal in proposals
                if proposal.get("status") not in TERMINAL_PROPOSAL_STATUSES
            ]
            lines.append(f"- Active proposals: {len(active_proposals)}")
            for proposal in active_proposals[:6]:
                lines.append(
                    "  - "
                    f"[{proposal.get('proposal_type')}/{proposal.get('risk')}/{proposal.get('status')}] "
                    f"{proposal.get('title')} ({proposal.get('id')})"
                )
            runs = list_action_runs(limit=3)
            if runs:
                lines.append("- Recent action runs:")
                for run in runs:
                    lines.append(
                        f"  - [{run.get('action_type')}/{run.get('result')}] {run.get('proposal_id')}"
                    )
        except Exception as exc:
            lines.append(f"- Proposal data unavailable: {exc}")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Insights**\nError: {exc}"


def _query_evolution() -> str:
    try:
        from row_bot.evolution import evolution_summary

        return evolution_summary()
    except Exception as exc:
        return f"**Controlled Self-Evolution**\nError: {exc}"


def _query_identity() -> str:
    try:
        from row_bot.identity import get_identity_config, is_self_improvement_enabled
        cfg = get_identity_config()
        name = cfg.get("name", "Row-Bot")
        personality = cfg.get("personality", "")
        self_improve = is_self_improvement_enabled()
        lines = [
            "**Identity**",
            f"- Name: {name}",
            f"- Personality: {personality or '(not set)'}",
            f"- Self-improvement: {'enabled' if self_improve else 'disabled'}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"**Identity**\nError: {exc}"


def _query_tasks() -> str:
    try:
        from row_bot.tasks import diagnose_task_schema, list_tasks
        diag = diagnose_task_schema()
        tasks = list_tasks()
        schema_lines = [
            "**Task DB Schema**",
            f"- Path: {diag.get('db_path')}",
            f"- OK: {diag.get('ok')}",
            f"- Size: {diag.get('size_bytes', 0)} bytes",
            f"- user_version: {diag.get('user_version', '(unknown)')}",
            f"- WAL/SHM: {diag.get('wal_exists')} / {diag.get('shm_exists')}",
            f"- Missing tables: {diag.get('missing_tables', [])}",
            f"- Missing columns: {diag.get('missing_columns', {})}",
        ]
        if diag.get("last_repair"):
            schema_lines.append(f"- Last repair: {diag.get('last_repair')}")
        if diag.get("error"):
            schema_lines.append(f"- Error: {diag.get('error')}")
        if not tasks:
            return "\n".join(schema_lines + ["", "**Scheduled Tasks**", "No tasks configured."])
        lines = [f"**Scheduled Tasks** ({len(tasks)} total)"]
        for t in tasks[:10]:  # Show at most 10
            status = "enabled" if t.get("enabled", True) else "disabled"
            lines.append(f"- {t.get('name', 'Unnamed')}: {t.get('schedule', 'no schedule')} ({status})")
        if len(tasks) > 10:
            lines.append(f"  ... and {len(tasks) - 10} more")
        return "\n".join(schema_lines + [""] + lines)
    except Exception as exc:
        return f"**Scheduled Tasks**\nError: {exc}"


def _short_status_text(value: object, limit: int = 96) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _counts_by(items: list[dict], key: str) -> str:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unset").strip() or "unset"
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return "none"
    return ", ".join(
        f"{name} {count}"
        for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    )


def _clean_tool_ids(items: object) -> list[str]:
    if isinstance(items, str):
        raw_items = [items]
    elif isinstance(items, dict):
        raw_items = []
    else:
        try:
            raw_items = list(items or [])
        except TypeError:
            raw_items = [items]
    return [
        str(item).strip()
        for item in raw_items
        if str(item or "").strip()
    ]


def _profile_allow_tools(profile: dict | None) -> list[str]:
    if not isinstance(profile, dict):
        return []
    tool_policy = profile.get("tool_policy_json") if isinstance(profile.get("tool_policy_json"), dict) else {}
    return _clean_tool_ids(tool_policy.get("allow_tools") or [])


def _active_runtime_context() -> dict:
    try:
        from row_bot.agent import get_active_runtime_context

        context = get_active_runtime_context()
    except Exception:
        return {}
    return dict(context) if isinstance(context, dict) else {}


def _active_runtime_thread_id() -> str:
    context = _active_runtime_context()
    return str((context or {}).get("thread_id") or "").strip()


def _tool_catalog_lookup() -> dict[str, dict]:
    try:
        from row_bot.agent_tool_catalog import list_agent_tool_catalog

        catalog = list_agent_tool_catalog(include_unavailable=True)
    except Exception:
        catalog = []
    lookup: dict[str, dict] = {}
    for record in catalog:
        if not isinstance(record, dict):
            continue
        for key in ("id", "runtime_name"):
            value = str(record.get(key) or "").strip()
            if value:
                lookup.setdefault(value, record)
    return lookup


def _tool_label_for_id(tool_id: str, catalog_lookup: dict[str, dict] | None = None) -> str:
    clean_id = str(tool_id or "").strip()
    if not clean_id:
        return ""
    lookup = catalog_lookup if isinstance(catalog_lookup, dict) else _tool_catalog_lookup()
    record = lookup.get(clean_id)
    if isinstance(record, dict):
        label = str(record.get("label") or record.get("runtime_name") or clean_id).strip()
        label = re.sub(r"^[^A-Za-z0-9]+", "", label).strip()
        if label:
            return label
    try:
        tool = registry.get_tool(clean_id)
    except Exception:
        tool = None
    if tool is not None:
        return _tool_display_label(tool)
    return clean_id


def _format_selected_tool_ids(tool_ids: object, *, limit: int = 12) -> str:
    clean_ids = _clean_tool_ids(tool_ids)
    if not clean_ids:
        return "none"
    lookup = _tool_catalog_lookup()
    labels: list[str] = []
    for tool_id in clean_ids[:limit]:
        label = _tool_label_for_id(tool_id, lookup)
        labels.append(f"{label or tool_id} ({tool_id})")
    if len(clean_ids) > limit:
        labels.append(f"... and {len(clean_ids) - limit} more")
    return ", ".join(labels)


def _active_thread_profile_scope() -> dict:
    context = _active_runtime_context()
    thread_id = str(context.get("thread_id") or "").strip()
    runtime_surface = str(context.get("runtime_surface") or "").strip()
    runtime_profile_ref = str(context.get("agent_profile_id") or "").strip()
    runtime_allowlist = _clean_tool_ids(context.get("tool_allowlist") or [])
    thread_profile_ref = ""
    if thread_id:
        try:
            from row_bot.threads import _get_thread_agent_profile

            pointer = _get_thread_agent_profile(thread_id)
        except Exception:
            pointer = {"id": "", "slug": ""}
        if isinstance(pointer, dict):
            thread_profile_ref = str(pointer.get("id") or pointer.get("slug") or "").strip()

    profile_ref = runtime_profile_ref or thread_profile_ref
    profile = None
    if profile_ref:
        try:
            from row_bot.agent_profiles import get_agent_profile

            profile = get_agent_profile(profile_ref, enabled_only=False)
        except Exception:
            profile = None

    allow_tools = _profile_allow_tools(profile)
    if not allow_tools and profile is None and runtime_allowlist:
        allow_tools = list(runtime_allowlist)
    return {
        "thread_id": thread_id,
        "runtime_surface": runtime_surface,
        "profile_ref": profile_ref,
        "profile": profile,
        "allow_tools": allow_tools,
        "runtime_allowlist": runtime_allowlist,
    }


def _query_agents() -> str:
    try:
        from row_bot.agent_runs import (
            TERMINAL_STATUSES,
            list_agent_runs,
            list_agent_write_locks,
        )

        sampled_runs = list_agent_runs(limit=200)
        from row_bot.agent_runner import child_dispatch_state
        from row_bot.agent_settings import load_agent_runtime_settings

        runtime_settings = load_agent_runtime_settings()
        dispatch = child_dispatch_state()
        current_runs = [
            run for run in sampled_runs
            if str(run.get("status") or "") not in TERMINAL_STATUSES
        ]
        writer_locks = list_agent_write_locks()
        lines = [
            "**Agents**",
            f"- Current runs: {len(current_runs)} active/attention from {len(sampled_runs)} sampled",
            f"- By kind: {_counts_by(sampled_runs, 'kind')}",
            f"- By status: {_counts_by(sampled_runs, 'status')}",
            (
                "- Defaults: "
                f"{runtime_settings.max_iterations} model rounds; "
                f"depth {runtime_settings.max_spawn_depth}; "
                f"{runtime_settings.max_concurrent_children} active per parent; "
                f"{runtime_settings.max_active_children_global} active globally; "
                f"child timeout {runtime_settings.child_timeout_seconds or 'off'}"
            ),
            f"- Dispatcher: {dispatch['active']} active, {dispatch['queued']} queued",
            f"- Writer locks: {len(writer_locks)} active",
        ]
        if not sampled_runs:
            lines.append("No durable Agent Runs recorded.")
            return "\n".join(lines)

        if not current_runs:
            lines.append("No current Agent Runs.")
        else:
            lines.append("Current runs:")
        for run in current_runs[:8]:
            label = _short_status_text(
                run.get("display_name") or run.get("prompt") or run.get("id"),
                72,
            )
            status = str(run.get("status") or "queued")
            kind = str(run.get("kind") or "subagent")
            profile = str(run.get("profile_display_name") or run.get("profile_slug") or "").strip()
            profile_label = f", profile {profile}" if profile else ""
            progress = ""
            if int(run.get("model_iterations_max") or 0):
                progress = (
                    f", rounds {run.get('model_iterations_used', 0)}/"
                    f"{run.get('model_iterations_max', 0)}, depth {run.get('depth', 0)}"
                )
            detail = _short_status_text(
                run.get("status_message") or run.get("summary") or run.get("error"),
                90,
            )
            suffix = f" - {detail}" if detail else ""
            lines.append(f"- {label} [{kind}/{status}{profile_label}{progress}]{suffix}")
        if len(current_runs) > 8:
            lines.append(f"- ... and {len(current_runs) - 8} more current runs")

        if writer_locks:
            lines.append("Writer locks:")
            for lock in writer_locks[:5]:
                key = _short_status_text(lock.get("lock_key"), 64)
                run_id = str(lock.get("run_id") or "")
                workspace = _short_status_text(lock.get("workspace_path") or lock.get("workspace_id"), 72)
                workspace_label = f" ({workspace})" if workspace else ""
                lines.append(f"- {key}: run {run_id}{workspace_label}")
            if len(writer_locks) > 5:
                lines.append(f"- ... and {len(writer_locks) - 5} more locks")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Agents**\nError: {exc}"


def _query_agent_profiles() -> str:
    try:
        from row_bot.agent_profiles import list_agent_profiles
        from row_bot.agent_tool_catalog import (
            count_tool_ids_by_source,
            format_tool_source_counts,
        )

        profiles = list_agent_profiles(enabled_only=False)
        enabled = [profile for profile in profiles if profile.get("enabled", True)]
        selected_profiles = []
        aggregate_tool_counts: dict[str, int] = {}
        for profile in profiles:
            tool_policy = profile.get("tool_policy_json") if isinstance(profile.get("tool_policy_json"), dict) else {}
            allow_tools = [
                str(name)
                for name in tool_policy.get("allow_tools") or []
                if str(name or "").strip()
            ]
            if not allow_tools:
                continue
            selected_profiles.append(profile)
            for source, count in count_tool_ids_by_source(allow_tools).items():
                aggregate_tool_counts[source] = aggregate_tool_counts.get(source, 0) + count
        lines = [
            "**Agent Profiles**",
            f"- Profiles: {len(enabled)} enabled / {len(profiles)} total",
            f"- Sources: {_counts_by(profiles, 'source')}",
            f"- Scopes: {_counts_by(profiles, 'scope')}",
            f"- Tool modes: {len(profiles) - len(selected_profiles)} inherited enabled tools / "
            f"{len(selected_profiles)} selected tools",
        ]
        aggregate_text = format_tool_source_counts(aggregate_tool_counts)
        if aggregate_text:
            lines.append(f"- Selected tool sources: {aggregate_text}")
        profile_scope = _active_thread_profile_scope()
        thread_id = str(profile_scope.get("thread_id") or "").strip()
        active = profile_scope.get("profile")
        active_ref = str(profile_scope.get("profile_ref") or "").strip()
        if isinstance(active, dict):
            state = "enabled" if active.get("enabled", True) else "disabled"
            lines.append(
                "- Active thread profile: "
                f"{active.get('display_name') or active.get('slug')} "
                f"({active.get('slug')}, {state})"
            )
            active_allow_tools = _clean_tool_ids(profile_scope.get("allow_tools") or [])
            if active_allow_tools:
                source_counts = format_tool_source_counts(count_tool_ids_by_source(active_allow_tools))
                source_suffix = f"; {source_counts}" if source_counts else ""
                lines.append(
                    "- Active profile tool mode: "
                    f"selected tools ({len(active_allow_tools)} selected{source_suffix})"
                )
                lines.append(
                    f"- Active profile selected tools: {_format_selected_tool_ids(active_allow_tools)}"
                )
                lines.append(
                    "- Active profile enforcement: selected tools are runtime-bound; "
                    "other global tools are not bound for this profile."
                )
                runtime_allowlist = _clean_tool_ids(profile_scope.get("runtime_allowlist") or [])
                if runtime_allowlist and runtime_allowlist != active_allow_tools:
                    lines.append(
                        f"- Active runtime allow-list: {_format_selected_tool_ids(runtime_allowlist)}"
                    )
            else:
                lines.append(
                    "- Active profile tool mode: inherited enabled tools "
                    "(broad/default; no profile allow-list)"
                )
        elif active_ref:
            lines.append(f"- Active thread profile: {active_ref} (not found)")
        elif thread_id:
            lines.append("- Active thread profile: implicit default")
        else:
            lines.append("- Active thread profile: no active runtime thread")

        if not profiles:
            lines.append("No Agent Profiles are available.")
            return "\n".join(lines)

        lines.append("Available profiles:")
        for profile in profiles[:10]:
            state = "enabled" if profile.get("enabled", True) else "disabled"
            tool_policy = profile.get("tool_policy_json") if isinstance(profile.get("tool_policy_json"), dict) else {}
            skill_policy = profile.get("skill_policy_json") if isinstance(profile.get("skill_policy_json"), dict) else {}
            allow_tools = [
                str(name)
                for name in tool_policy.get("allow_tools") or []
                if str(name or "").strip()
            ]
            tool_mode = "selected tools" if allow_tools else "inherits enabled tools"
            policy_bits = [
                str(tool_policy.get("capability") or "read_only"),
                f"tools={tool_mode}",
                f"selected={len(allow_tools)}",
                f"skills={len(skill_policy.get('skills_override') or [])}",
            ]
            source_counts = format_tool_source_counts(count_tool_ids_by_source(allow_tools)) if allow_tools else ""
            if source_counts:
                policy_bits.append(f"sources {source_counts}")
            description = _short_status_text(
                profile.get("when_to_use") or profile.get("description"),
                90,
            )
            suffix = f" - {description}" if description else ""
            lines.append(
                f"- {profile.get('display_name') or profile.get('slug')} "
                f"[{profile.get('slug')}, {profile.get('source')}, {state}, "
                f"{', '.join(policy_bits)}]{suffix}"
            )
        if len(profiles) > 10:
            lines.append(f"- ... and {len(profiles) - 10} more profiles")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Agent Profiles**\nError: {exc}"


def _query_goals() -> str:
    try:
        from row_bot.goals import (
            DEFAULT_GOAL_MAX_TURNS,
            GOAL_TERMINAL_STATUSES,
            get_current_goal,
            list_goals,
        )

        sampled_goals = list_goals(limit=200)
        current_goals = [
            goal for goal in sampled_goals
            if str(goal.get("status") or "") not in GOAL_TERMINAL_STATUSES
        ]
        lines = [
            "**Goals**",
            f"- Current goals: {len(current_goals)} active/attention from {len(sampled_goals)} sampled",
            f"- By status: {_counts_by(sampled_goals, 'status')}",
            f"- Default turn budget: {DEFAULT_GOAL_MAX_TURNS}",
        ]
        thread_id = _active_runtime_thread_id()
        if thread_id:
            current = get_current_goal(thread_id, include_terminal=True)
            if current:
                objective = _short_status_text(current.get("objective"), 90)
                lines.append(
                    "- Current thread: "
                    f"{current.get('status')} - {objective} "
                    f"({current.get('turns_used', 0)}/{current.get('max_turns', DEFAULT_GOAL_MAX_TURNS)} turns)"
                )
            else:
                lines.append("- Current thread: no visible goal")
        else:
            lines.append("- Current thread: no active runtime thread")

        if not sampled_goals:
            lines.append("No Goal Mode records yet.")
            return "\n".join(lines)

        if not current_goals:
            lines.append("No current goals.")
        else:
            lines.append("Current goals:")
        for goal in current_goals[:8]:
            objective = _short_status_text(goal.get("objective"), 76)
            status = str(goal.get("status") or "active")
            progress = _short_status_text(
                goal.get("last_progress") or goal.get("last_blocker") or goal.get("status_reason"),
                90,
            )
            verifier = ""
            if int(goal.get("verifier_failures") or 0):
                verifier = f", verifier failures {goal.get('verifier_failures')}"
            suffix = f" - {progress}" if progress else ""
            lines.append(
                f"- {objective} [{status}, turns {goal.get('turns_used', 0)}/"
                f"{goal.get('max_turns', DEFAULT_GOAL_MAX_TURNS)}{verifier}]{suffix}"
            )
        if len(current_goals) > 8:
            lines.append(f"- ... and {len(current_goals) - 8} more current goals")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Goals**\nError: {exc}"


def _query_logs() -> str:
    """Recent warnings and errors from the log file."""
    try:
        from row_bot.logging_config import read_recent_logs
        entries = read_recent_logs(n=50)
        # Filter to WARNING+ and exclude row_bot_status calls to avoid recursion
        filtered = [
            e for e in entries
            if e.get("level", "") in ("WARNING", "ERROR", "CRITICAL")
            and "row_bot_status" not in e.get("msg", "")
        ][:15]
        if not filtered:
            return "**Recent Logs**\nNo warnings or errors in the recent log."
        lines = [f"**Recent Logs** (WARNING+ level, newest first)"]
        for e in filtered:
            ts = e.get("ts", "?")[-8:]  # HH:MM:SS
            lvl = e.get("level", "?")[:4]
            msg = e.get("msg", "")[:200]
            lines.append(f"- [{ts}] {lvl}: {msg}")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Recent Logs**\nError reading logs: {exc}"


def _query_errors() -> str:
    """Recent errors with tracebacks for diagnosis."""
    try:
        from row_bot.logging_config import read_recent_logs
        entries = read_recent_logs(n=100)
        errors = [
            e for e in entries
            if e.get("level", "") in ("ERROR", "CRITICAL")
            and "row_bot_status" not in e.get("msg", "")
        ][:10]
        if not errors:
            return "**Recent Errors**\nNo errors in the recent log."
        lines = [f"**Recent Errors** (newest first)"]
        for e in errors:
            ts = e.get("ts", "?")
            msg = e.get("msg", "")[:300]
            lines.append(f"- [{ts}] {msg}")
            exc = e.get("exc", "")
            if exc:
                # Show last 3 lines of traceback to keep tokens low
                tb_lines = exc.strip().splitlines()[-3:]
                for tb in tb_lines:
                    lines.append(f"  {tb.strip()[:150]}")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Recent Errors**\nError reading logs: {exc}"


def _query_vision() -> str:
    """Vision / camera model and settings."""
    try:
        from row_bot.vision import _load_settings, DEFAULT_VISION_MODEL
        settings = _load_settings()
        model = settings.get("model", DEFAULT_VISION_MODEL)
        enabled = settings.get("enabled", True)
        camera = settings.get("camera_index", 0)
        provider_id = ""
        runtime_model = str(model or "")
        provider_label = "Ollama"
        probe: dict = {}
        try:
            from row_bot.providers.selection import parse_model_ref, provider_display_label

            parsed = parse_model_ref(str(model or ""))
            if parsed:
                provider_id, runtime_model = parsed
            else:
                try:
                    from row_bot.models import get_cloud_provider

                    provider_id = str(get_cloud_provider(str(model or "")) or "ollama")
                except Exception:
                    provider_id = "ollama"
            if provider_id == "local":
                provider_id = "ollama"
            if provider_id.startswith("custom_openai_"):
                try:
                    from row_bot.providers.custom import get_custom_endpoint

                    endpoint = get_custom_endpoint(provider_id) or {}
                    provider_label = str(endpoint.get("display_name") or endpoint.get("name") or provider_display_label(provider_id))
                    if isinstance(endpoint.get("last_probe"), dict):
                        probe = dict(endpoint["last_probe"])
                except Exception:
                    provider_label = provider_display_label(provider_id)
            else:
                provider_label = provider_display_label(provider_id)
        except Exception:
            provider_id = "ollama"
            provider_label = "Ollama"
        vision_ok = probe.get("vision_ok") if probe else None
        vision_error = str(probe.get("vision_error") or "") if probe else ""
        vision_skip = str(probe.get("vision_probe_skip_reason") or "") if probe else ""
        compatibility: dict = {}
        try:
            from row_bot.vision import vision_model_compatibility

            compatibility = vision_model_compatibility(str(model or ""))
        except Exception:
            compatibility = {}
        incompat_reason = ""
        if compatibility.get("explicit") and not compatibility.get("usable"):
            incompat_reason = str(compatibility.get("reason") or "capability metadata says this model is not compatible with vision")
        if vision_ok is False and _vision_probe_error_is_inconclusive(vision_error):
            vision_ok = None
        if incompat_reason:
            readiness = "vision disabled for endpoint" if "manual" in incompat_reason.lower() else "vision incompatible"
        elif vision_ok is True:
            readiness = "vision verified"
        elif vision_ok is False:
            readiness = "vision failed"
        elif provider_id.startswith("custom_openai_"):
            readiness = "vision unverified"
        else:
            readiness = "vision inferred"
        lines = [
            "**Vision**",
            f"- Model: {model}",
            f"- Runtime model: {runtime_model}",
            f"- Provider: {provider_label}",
            f"- Enabled: {'yes' if enabled else 'no'}",
            f"- Camera index: {camera}",
            f"- Vision readiness: {readiness}",
        ]
        if probe:
            if probe.get("vision_model"):
                lines.append(f"- Vision probe model: {probe.get('vision_model')}")
            if probe.get("vision_content_format"):
                lines.append(f"- Vision content format: {probe.get('vision_content_format')}")
            if vision_skip:
                lines.append(f"- Vision probe skipped: {vision_skip}")
            if vision_error and (vision_ok is False or _vision_probe_error_is_inconclusive(vision_error)):
                label = "Vision probe error" if vision_ok is False else "Vision probe note"
                lines.append(f"- {label}: {vision_error}")
        if incompat_reason:
            lines.append(f"- Vision compatibility: {incompat_reason}")
        lines.extend(_pinned_choice_status_lines(
            "vision",
            "Pinned Vision Model Choices",
            value_label="Selection value",
            value_key="canonical_ref",
        ))
        return "\n".join(lines)
    except Exception as exc:
        return f"**Vision**\nError: {exc}"


def _vision_probe_error_is_inconclusive(error: str) -> bool:
    text = str(error or "").strip().lower()
    return text.startswith("probe inconclusive") or text in {
        "unexpected response: <empty>",
        "unexpected response: ",
    }


def _query_image_gen() -> str:
    """Image generation model."""
    try:
        from row_bot.tools.image_gen_tool import _get_configured_selection, DEFAULT_MODEL
        from row_bot.tools.registry import is_enabled
        selection = _get_configured_selection()
        lines = [
            "**Image Generation**",
            f"- Tool: {'enabled' if is_enabled('image_gen') else 'disabled'}",
            f"- Model: {selection}",
        ]
        if selection == DEFAULT_MODEL:
            lines.append(f"- (default — change in Settings → Models)")
        lines.extend(_pinned_choice_status_lines(
            "image",
            "Pinned Image Model Choices",
            value_label="Config value",
            value_key="config_value",
        ))
        return "\n".join(lines)
    except Exception as exc:
        return f"**Image Generation**\nError: {exc}"


def _query_video_gen() -> str:
    """Video generation model."""
    try:
        from row_bot.tools.video_gen_tool import _get_configured_selection, DEFAULT_MODEL
        from row_bot.tools.registry import is_enabled
        selection = _get_configured_selection()
        lines = [
            "**Video Generation**",
            f"- Tool: {'enabled' if is_enabled('video_gen') else 'disabled'}",
            f"- Model: {selection}",
        ]
        if selection == DEFAULT_MODEL:
            lines.append("- (default — change in Settings → Models)")
        lines.extend(_pinned_choice_status_lines(
            "video",
            "Pinned Video Model Choices",
            value_label="Config value",
            value_key="config_value",
        ))
        return "\n".join(lines)
    except Exception as exc:
        return f"**Video Generation**\nError: {exc}"


def _query_voice() -> str:
    """Voice runtime, speech settings, and realtime diagnostics."""
    try:
        lines = ["**Voice & Speech**"]
        lines.append("- User-facing modes: Talk, Dictate")
        lines.append("- Dictate policy: STT-only; never sends to the LLM until the user presses Send")
        lines.append("- Realtime role: voice transport/backchannel for normal Row-Bot work")
        # TTS
        try:
            tts_path = _DATA_DIR / "tts_settings.json"
            if tts_path.is_file():
                tts = json.loads(tts_path.read_text())
                lines.append(f"- TTS enabled: {'yes' if tts.get('enabled', False) else 'no'}")
                lines.append(f"- TTS voice: {tts.get('voice', 'af_heart')}")
                lines.append(f"- TTS speed: {tts.get('speed', 1.0)}")
            else:
                lines.append("- TTS: not configured")
        except Exception:
            lines.append("- TTS: error reading settings")
        # STT (Whisper)
        try:
            voice_path = _DATA_DIR / "voice_settings.json"
            if voice_path.is_file():
                voice = json.loads(voice_path.read_text())
                lines.append(f"- Whisper model: {voice.get('whisper_model', 'small')}")
            else:
                lines.append("- Whisper model: small (default)")
        except Exception:
            lines.append("- Whisper: error reading settings")
        try:
            from row_bot.voice.runtime import load_voice_runtime_settings

            runtime = load_voice_runtime_settings()
            lines.append(f"- Talk provider: {runtime.talk_provider}")
            lines.append(f"- Talk model: {runtime.talk_model}")
            lines.append(f"- Dictation provider: {runtime.dictation_provider}")
            lines.append(f"- Dictation model: {runtime.dictation_model}")
            lines.append(f"- Speech output provider: {runtime.speech_output_provider}")
            lines.append(f"- Speech output model: {runtime.speech_output_model}")
            lines.append(f"- Speech output voice: {runtime.speech_output_voice}")
            lines.append(f"- Captions: {'on' if runtime.captions_enabled else 'off'}")
            lines.append(f"- Realtime fallback: {'on' if runtime.realtime_fallback_to_local else 'off'}")
        except Exception:
            lines.append("- Voice runtime: error reading settings")
        try:
            from row_bot.voice.openai_realtime import OpenAIRealtimeProvider
            from row_bot.voice.agent_bridge import (
                REALTIME_ALLOWED_BRIDGE_TOOLS,
                REALTIME_DIRECT_TOOL_POLICY,
                REALTIME_WAIT_TOOL,
                VOICE_BRAIN_STRATEGY,
            )

            realtime = OpenAIRealtimeProvider().status()
            lines.append(f"- OpenAI Realtime: {'ready' if realtime.ready else 'not configured'}")
            lines.append(f"- Realtime brain strategy: {VOICE_BRAIN_STRATEGY}")
            lines.append(f"- Realtime direct normal-tool access: {REALTIME_DIRECT_TOOL_POLICY}")
            lines.append(f"- Realtime bridge tools: {', '.join(REALTIME_ALLOWED_BRIDGE_TOOLS)}")
            lines.append(f"- Realtime quiet idle tool: {REALTIME_WAIT_TOOL}")
            lines.append("- Realtime credential safety: browser receives ephemeral client secrets only; long-lived provider keys stay server-side")
        except Exception:
            lines.append("- OpenAI Realtime: unavailable")
        try:
            from row_bot.ui.state import _active_generations

            active = list(_active_generations.items())
            if not active:
                lines.append("- Active Row-Bot run: none")
            else:
                lines.append(f"- Active Row-Bot runs: {len(active)}")
                for thread_id, gen in active[:3]:
                    pending_tools = getattr(gen, "pending_tools", {}) or {}
                    tool_names = [
                        str(tool.get("name") or "")
                        for tool in pending_tools.values()
                        if isinstance(tool, dict)
                    ]
                    queued = list(getattr(gen, "voice_control_queue", []) or [])
                    lines.append(
                        f"  - {thread_id}: {getattr(gen, 'status', 'streaming')}; "
                        f"tools={', '.join(tool_names) if tool_names else 'none'}; "
                        f"approval={'yes' if getattr(gen, 'interrupt_data', None) else 'no'}; "
                        f"cancel={'yes' if getattr(gen, 'stop_event', None) else 'no'}; "
                        f"follow-up/steer={'yes'}; queued_controls={len(queued)}"
                    )
        except Exception:
            lines.append("- Active Row-Bot run: unavailable")
        try:
            from row_bot.logging_config import read_recent_logs

            realtime_logs = [
                entry
                for entry in read_recent_logs(80)
                if "voice.realtime.pipeline" in str(entry.get("msg") or "")
                   or "OpenAI Realtime" in str(entry.get("msg") or "")
            ][:5]
            if realtime_logs:
                lines.append("- Recent realtime diagnostics:")
                for entry in realtime_logs:
                    msg = str(entry.get("msg") or "").replace("\n", " ")
                    msg = re.sub(r"(sk|ek)_[A-Za-z0-9_\-]+", r"\1_***", msg)
                    lines.append(f"  - {entry.get('ts', '')} {entry.get('level', '')}: {msg[:300]}")
            else:
                lines.append("- Recent realtime diagnostics: none")
        except Exception:
            lines.append("- Recent realtime diagnostics: unavailable")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Voice & Speech**\nError: {exc}"


def _query_config() -> str:
    """Miscellaneous configuration: context caps, dream cycle, wiki vault, memory extraction."""
    lines = ["**Configuration**"]
    try:
        # Context size caps
        from row_bot.models import get_user_context_size, get_cloud_context_size
        lines.append(f"- Local context cap: {get_user_context_size():,} tokens")
        lines.append(f"- Provider context cap: {get_cloud_context_size():,} tokens")
    except Exception:
        pass
    try:
        # Dream cycle
        dc_path = _DATA_DIR / "dream_config.json"
        if dc_path.is_file():
            dc = json.loads(dc_path.read_text())
            lines.append(f"- Dream cycle: {'enabled' if dc.get('enabled', True) else 'disabled'}"
                         f" (window {dc.get('window_start', 1)}:00–{dc.get('window_end', 5)}:00)")
        else:
            lines.append("- Dream cycle: enabled (default 1:00–5:00)")
    except Exception:
        pass
    try:
        # Memory extraction
        me_path = _DATA_DIR / "memory_extraction_state.json"
        if me_path.is_file():
            me = json.loads(me_path.read_text())
            last = me.get("last_extraction", "never")
            entities = me.get("entities_saved", 0)
            lines.append(f"- Memory extraction: last run {last}, {entities} entities saved")
        else:
            lines.append("- Memory extraction: no runs yet")
    except Exception:
        pass
    try:
        # Wiki vault
        wv_path = _DATA_DIR / "wiki_config.json"
        if wv_path.is_file():
            wv = json.loads(wv_path.read_text())
            lines.append(f"- Wiki vault: {'enabled' if wv.get('enabled', False) else 'disabled'}")
            if wv.get("enabled"):
                lines.append(f"  Path: {wv.get('vault_path', '~/.row-bot/vault')}")
        else:
            lines.append("- Wiki vault: disabled (default)")
    except Exception:
        pass
    return "\n".join(lines)


def _query_version() -> str:
    from row_bot.version import __version__
    return f"**Row-Bot Version**: v{__version__}"


def _query_designer() -> str:
    try:
        from row_bot.designer.storage import list_projects
        projects = list_projects()
        if not projects:
            return "**Designer**\nNo projects yet."
        lines = ["**Designer**", f"- {len(projects)} project(s)"]
        for proj in projects[:5]:
            pages = proj.get("page_count", "?")
            ratio = proj.get("aspect_ratio", "?")
            lines.append(f"  - {proj.get('name', 'Untitled')} ({pages} pages, {ratio})")
        if len(projects) > 5:
            lines.append(f"  - … and {len(projects) - 5} more")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Designer**\nError: {exc}"


_QUERY_HANDLERS = {
    "overview": _query_overview,
    "version": _query_version,
    "model": _query_model,
    "channels": _query_channels,
    "memory": _query_memory,
    "skills": _query_skills,
    "tools": _query_tools,
    "plugins": _query_plugins,
    "mcp": _query_mcp,
    "providers": _query_providers,
    "insights": _query_insights,
    "evolution": _query_evolution,
    "api_keys": _query_api_keys,
    "identity": _query_identity,
    "tasks": _query_tasks,
    "agents": _query_agents,
    "agent_profiles": _query_agent_profiles,
    "goals": _query_goals,
    "vision": _query_vision,
    "image_gen": _query_image_gen,
    "video_gen": _query_video_gen,
    "voice": _query_voice,
    "config": _query_config,
    "logs": _query_logs,
    "errors": _query_errors,
    "designer": _query_designer,
    "updates": lambda: _query_updates(),
}


def _query_updates() -> str:
    """Summary of auto-update state for the agent."""
    try:
        import row_bot.updater as updater
        s = updater.summary_for_status()
    except Exception as exc:  # pragma: no cover - defensive
        return f"**Updates**\nError: {exc}"
    lines = [
        "**Updates**",
        f"- Current version: v{s['current_version']}",
        f"- Channel: {s['channel']}",
        f"- Auto-check: {'on' if s['auto_check'] else 'off'}",
        f"- Last check: {s.get('last_check') or 'never'}",
    ]
    if s.get("dev_install"):
        lines.append("- Dev install — updater disabled")
    if s["update_available"]:
        lines.append(f"- ⬆ Update available: v{s['available_version']}")
        if s.get("available_notes_summary"):
            lines.append(f"  Summary: {s['available_notes_summary']}")
    else:
        lines.append("- No update available")
    if s.get("skipped_versions"):
        lines.append(f"- Skipped: {', '.join(s['skipped_versions'])}")
    return "\n".join(lines)


def _row_bot_status(category: str) -> str:
    """Query Row-Bot's current status and configuration."""
    category = category.strip().lower()
    handler = _QUERY_HANDLERS.get(category)
    if handler is None:
        available = ", ".join(sorted(_QUERY_HANDLERS.keys()))
        return f"Unknown category '{category}'. Available: {available}"
    try:
        return handler()
    except Exception as exc:
        logger.error("row_bot_status query error for '%s': %s", category, exc, exc_info=True)
        return f"Error querying {category}: {exc}"


# ═════════════════════════════════════════════════════════════════════════════
# WRITE HANDLERS (require user confirmation)
# ═════════════════════════════════════════════════════════════════════════════

def _update_setting(setting: str, value: str) -> str:
    """Update a Row-Bot setting (with interrupt-based confirmation)."""
    def interrupt(payload: dict) -> bool:
        return _approval_gate_bool(
            payload,
            blocked_message="BLOCKED: Changing Row-Bot settings is disabled in Block approval mode.",
        )

    setting = setting.strip().lower()
    value = value.strip()

    if setting in {"thread_model", "default_model", "model"}:
        runtime_context = _active_runtime_context()
        runtime_surface = str(runtime_context.get("runtime_surface") or "").strip()
        thread_id = str(runtime_context.get("thread_id") or "").strip()
        if runtime_surface in {"agent_child", "agent_child_resume"}:
            return (
                "Child Agents cannot switch their own runtime model. "
                "Ask the parent to spawn the child with delegate_work(model=...) "
                "or use /agent --model=model:provider:model-id for explicit command spawns."
            )
        if setting == "model" and thread_id:
            return (
                "Model scope is ambiguous in this conversation, so I did not change anything. "
                "Use setting='thread_model' to change this conversation, or "
                "setting='default_model' to change the global default for new conversations and workflows."
            )
        if setting == "thread_model":
            if not thread_id:
                return (
                    "Cannot change the conversation model because no active thread is in context. "
                    "Use setting='default_model' only if you want to change the global default."
                )
            if value.lower() in {"", "default", "inherit", "global"}:
                approval = interrupt({
                    "tool": "row_bot_update_setting",
                    "label": "Clear conversation model override",
                    "description": "Use the global default model for this conversation.",
                    "args": {"setting": "thread_model", "value": value or "default"},
                })
                if not approval:
                    return "Thread model change cancelled."
                try:
                    from row_bot.agent import clear_agent_cache
                    from row_bot.models import get_current_model
                    from row_bot.threads import _set_thread_model_override

                    _set_thread_model_override(thread_id, "")
                    clear_agent_cache()
                    return f"Thread model override cleared; using global default: {get_current_model()}"
                except Exception as exc:
                    return f"Failed to change thread model: {exc}"
            try:
                model_value, error = _resolve_model_update_value(value, surface="chat")
                if error:
                    return error
                if not model_value:
                    return f"Model '{value}' not found. Pin it in Settings -> Models first."
            except Exception as exc:
                return f"Failed to resolve thread model: {exc}"
            approval = interrupt({
                "tool": "row_bot_update_setting",
                "label": "Change conversation model",
                "description": f"Switch this conversation to: {model_value}",
                "args": {"setting": "thread_model", "value": model_value},
            })
            if not approval:
                return "Thread model change cancelled."
            try:
                from row_bot.agent import clear_agent_cache
                from row_bot.threads import _set_thread_model_override

                _set_thread_model_override(thread_id, model_value)
                clear_agent_cache()
                return f"Thread model override changed to: {model_value}"
            except Exception as exc:
                return f"Failed to change thread model: {exc}"
        result_prefix = (
            "Active model changed to:" if setting == "model"
            else "Global default model changed to:"
        )
        cancel_text = "Model change cancelled." if setting == "model" else "Default model change cancelled."
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Change global default model",
            "description": f"Switch the global default model to: {value}",
            "args": {"setting": setting, "value": value},
        })
        if not approval:
            return cancel_text
        try:
            from row_bot.models import set_model
            from row_bot.agent import clear_agent_cache
            model_value, error = _resolve_model_update_value(value, surface="chat")
            if error:
                return error
            if not model_value:
                return f"Model '{value}' not found. Pin it in Settings → Models first."
            set_model(model_value)
            clear_agent_cache()
            return f"{result_prefix} {model_value}"
        except Exception as exc:
            return f"Failed to change model: {exc}"

    elif setting == "vision_model":
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Change Vision model",
            "description": f"Switch the Vision model to: {value}",
            "args": {"setting": "vision_model", "value": value},
        })
        if not approval:
            return "Vision model change cancelled."
        try:
            from row_bot.agent import clear_agent_cache
            from row_bot.tools.vision_tool import _get_vision_service

            model_value, error = _resolve_model_update_value(value, surface="vision")
            if error:
                return error
            if not model_value:
                return f"Vision model '{value}' not found. Pin a Vision model in Settings → Models first."
            _get_vision_service().model = model_value
            clear_agent_cache()
            return f"Vision model changed to: {model_value}"
        except Exception as exc:
            return f"Failed to change Vision model: {exc}"

    elif setting == "name":
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Change assistant name",
            "description": f"Change the assistant name to: {value}",
            "args": {"setting": "name", "value": value},
        })
        if not approval:
            return "Name change cancelled."
        try:
            from row_bot.identity import get_identity_config, save_identity_config
            from row_bot.agent import clear_agent_cache
            cfg = get_identity_config()
            cfg["name"] = value
            save_identity_config(cfg)
            clear_agent_cache()
            return f"Assistant name changed to: {value}"
        except Exception as exc:
            return f"Failed to change name: {exc}"

    elif setting == "personality":
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Change personality",
            "description": f"Set personality to: {value}",
            "args": {"setting": "personality", "value": value},
        })
        if not approval:
            return "Personality change cancelled."
        try:
            from row_bot.identity import get_identity_config, save_identity_config, sanitize_personality
            from row_bot.agent import clear_agent_cache
            sanitized = sanitize_personality(value)
            cfg = get_identity_config()
            cfg["personality"] = sanitized
            save_identity_config(cfg)
            clear_agent_cache()
            result = f"Personality updated."
            if sanitized != value:
                result += " (Some text was removed due to disallowed patterns.)"
            return result
        except Exception as exc:
            return f"Failed to change personality: {exc}"

    elif setting == "context_size":
        try:
            size = int(value)
        except ValueError:
            return f"Invalid context size '{value}' — must be an integer (e.g. 65536)."
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Change local context size",
            "description": f"Set local model context window to {size:,} tokens",
            "args": {"setting": "context_size", "value": value},
        })
        if not approval:
            return "Context size change cancelled."
        try:
            from row_bot.models import set_context_size
            set_context_size(size)
            return f"Local context size set to {size:,} tokens."
        except Exception as exc:
            return f"Failed to change context size: {exc}"

    elif setting == "cloud_context_size":
        try:
            size = int(value)
        except ValueError:
            return f"Invalid context size '{value}' — must be an integer (e.g. 131072)."
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Change provider context cap",
            "description": f"Set provider context cap to {size:,} tokens",
            "args": {"setting": "cloud_context_size", "value": value},
        })
        if not approval:
            return "Provider context size change cancelled."
        try:
            from row_bot.models import set_cloud_context_size
            set_cloud_context_size(size)
            return f"Provider context cap set to {size:,} tokens."
        except Exception as exc:
            return f"Failed to change cloud context size: {exc}"

    elif setting == "dream_cycle":
        enabled = value.strip().lower() in ("on", "true", "yes", "enable", "enabled", "1")
        disabled = value.strip().lower() in ("off", "false", "no", "disable", "disabled", "0")
        if not enabled and not disabled:
            return f"Invalid value '{value}'. Use 'on' or 'off'."
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": f"{'Enable' if enabled else 'Disable'} dream cycle",
            "description": f"Set dream cycle to {'enabled' if enabled else 'disabled'}",
            "args": {"setting": "dream_cycle", "value": value},
        })
        if not approval:
            return "Dream cycle change cancelled."
        try:
            from row_bot.dream_cycle import set_enabled as dc_set_enabled
            dc_set_enabled(enabled)
            return f"Dream cycle {'enabled' if enabled else 'disabled'}."
        except Exception as exc:
            return f"Failed to change dream cycle: {exc}"

    elif setting == "dream_window":
        try:
            parts = value.replace(" ", "").split("-")
            start, end = int(parts[0]), int(parts[1])
            assert 0 <= start <= 23 and 0 <= end <= 23
        except Exception:
            return f"Invalid dream window '{value}'. Use 'START-END' (e.g. '1-5', hours 0–23)."
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Change dream cycle window",
            "description": f"Set dream cycle window to {start}:00–{end}:00",
            "args": {"setting": "dream_window", "value": value},
        })
        if not approval:
            return "Dream window change cancelled."
        try:
            from row_bot.dream_cycle import set_window
            set_window(start, end)
            return f"Dream cycle window set to {start}:00–{end}:00."
        except Exception as exc:
            return f"Failed to change dream window: {exc}"

    elif setting == "skill_toggle":
        try:
            name_part, toggle = value.rsplit(":", 1)
            name_part = name_part.strip()
            on = toggle.strip().lower() in ("on", "true", "yes", "enable", "enabled", "1")
            off = toggle.strip().lower() in ("off", "false", "no", "disable", "disabled", "0")
            if not on and not off:
                raise ValueError
        except Exception:
            return f"Invalid value '{value}'. Use 'skill_name:on' or 'skill_name:off'."
        try:
            resolved_name, resolved_label, suggestions = _resolve_manual_skill_name(name_part)
        except Exception as exc:
            return f"Failed to inspect Skill Library: {exc}"
        if not resolved_name:
            suggestion_text = f" Try one of: {', '.join(suggestions)}." if suggestions else ""
            return f"Unknown Skill Library item '{name_part}'.{suggestion_text}"
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": f"Turn {'on' if on else 'off'} skill '{resolved_label}'",
            "description": f"Set Skill Library item '{resolved_label}' to {'Available' if on else 'Off'}",
            "args": {"setting": "skill_toggle", "value": f"{resolved_name}:{'on' if on else 'off'}"},
        })
        if not approval:
            return "Skill toggle cancelled."
        try:
            from row_bot.skills import is_pinned as skill_is_pinned
            from row_bot.skills import set_enabled as skill_set_enabled
            was_pinned = skill_is_pinned(resolved_name)
            skill_set_enabled(resolved_name, on)
            if not on and was_pinned:
                return f"Skill '{resolved_label}' is now Off and no longer pinned."
            return f"Skill '{resolved_label}' is now {'Available' if on else 'Off'}."
        except Exception as exc:
            return f"Failed to toggle skill: {exc}"

    elif setting == "skill_pin":
        try:
            name_part, toggle = value.rsplit(":", 1)
            name_part = name_part.strip()
            on = toggle.strip().lower() in ("on", "true", "yes", "enable", "enabled", "1")
            off = toggle.strip().lower() in ("off", "false", "no", "disable", "disabled", "0")
            if not on and not off:
                raise ValueError
        except Exception:
            return f"Invalid value '{value}'. Use 'skill_name:on' or 'skill_name:off'."
        try:
            resolved_name, resolved_label, suggestions = _resolve_manual_skill_name(name_part)
        except Exception as exc:
            return f"Failed to inspect Skill Library: {exc}"
        if not resolved_name:
            suggestion_text = f" Try one of: {', '.join(suggestions)}." if suggestions else ""
            return f"Unknown Skill Library item '{name_part}'.{suggestion_text}"
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": f"{'Pin' if on else 'Unpin'} skill '{resolved_label}'",
            "description": (
                f"{'Pin' if on else 'Unpin'} Skill Library item '{resolved_label}' "
                f"for new chats, tasks, designer threads, and developer threads"
            ),
            "args": {"setting": "skill_pin", "value": f"{resolved_name}:{'on' if on else 'off'}"},
        })
        if not approval:
            return "Skill pin change cancelled."
        try:
            from row_bot.skills import is_enabled as skill_is_enabled
            from row_bot.skills import set_pinned as skill_set_pinned
            skill_set_pinned(resolved_name, on)
            if on:
                return (
                    f"Skill '{resolved_label}' is now pinned and Available. "
                    "It will start active in new chats, tasks, designer threads, and developer threads."
                )
            if skill_is_enabled(resolved_name):
                return (
                    f"Skill '{resolved_label}' is no longer pinned. "
                    "It remains Available unless you turn it off separately."
                )
            return (
                f"Skill '{resolved_label}' is no longer pinned. "
                "It is Off and will stay Off unless you make it Available."
            )
        except Exception as exc:
            return f"Failed to change skill pin: {exc}"

    elif setting == "tool_toggle":
        try:
            name_part, toggle = value.rsplit(":", 1)
            name_part = name_part.strip()
            on = toggle.strip().lower() in ("on", "true", "yes", "enable", "enabled", "1")
            off = toggle.strip().lower() in ("off", "false", "no", "disable", "disabled", "0")
            if not on and not off:
                raise ValueError
        except Exception:
            return f"Invalid value '{value}'. Use 'tool_name:on' or 'tool_name:off'."
        from row_bot.tools import registry as tool_registry
        resolved_name, resolved_label, suggestions = _resolve_tool_name(name_part)
        if not resolved_name:
            suggestion_text = f" Try one of: {', '.join(suggestions)}." if suggestions else ""
            return f"Unknown tool '{name_part}'.{suggestion_text}"
        if resolved_name == "mcp":
            try:
                from row_bot.mcp_client import config as mcp_config
                current_state = mcp_config.is_globally_enabled()
            except Exception:
                current_state = tool_registry.is_enabled(resolved_name)
        else:
            current_state = tool_registry.is_enabled(resolved_name)
        if current_state == on:
            return f"Tool '{resolved_label}' is already {'enabled' if on else 'disabled'}."
        canonical_value = f"{resolved_name}:{'on' if on else 'off'}"
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": f"{'Enable' if on else 'Disable'} tool '{resolved_label}'",
            "description": f"Set tool '{resolved_label}' to {'enabled' if on else 'disabled'}",
            "args": {"setting": "tool_toggle", "value": canonical_value},
        })
        if not approval:
            return "Tool toggle cancelled."
        try:
            if resolved_name == "mcp":
                from row_bot.mcp_client import config as mcp_config
                mcp_config.set_global_enabled(on)
                actual_state = mcp_config.is_globally_enabled() and tool_registry.is_enabled(resolved_name)
            else:
                tool_registry.set_enabled(resolved_name, on)
                actual_state = tool_registry.is_enabled(resolved_name)
            if actual_state != on:
                return (
                    f"Failed to {'enable' if on else 'disable'} tool '{resolved_label}'. "
                    f"The setting did not take effect."
                )
            if resolved_name == "mcp":
                return f"MCP client and tool '{resolved_label}' {'enabled' if on else 'disabled'}."
            return f"Tool '{resolved_label}' {'enabled' if on else 'disabled'}."
        except Exception as exc:
            return f"Failed to toggle tool: {exc}"

    elif setting == "image_gen_model":
        model_value = _normalize_provider_model_value(setting, value)
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Change image generation model",
            "description": f"Set image gen model to: {model_value}",
            "args": {"setting": "image_gen_model", "value": model_value},
        })
        if not approval:
            return "Image gen model change cancelled."
        try:
            from row_bot.tools import registry as tool_registry
            from row_bot.providers.selection import seed_configured_media_quick_choices
            tool_registry.set_tool_config("image_gen", "model", model_value)
            seed_configured_media_quick_choices()
            return f"Image generation model set to: {model_value}"
        except Exception as exc:
            return f"Failed to change image gen model: {exc}"

    elif setting == "video_gen_model":
        model_value = _normalize_provider_model_value(setting, value)
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Change video generation model",
            "description": f"Set video gen model to: {model_value}",
            "args": {"setting": "video_gen_model", "value": model_value},
        })
        if not approval:
            return "Video gen model change cancelled."
        try:
            from row_bot.tools import registry as tool_registry
            from row_bot.providers.selection import seed_configured_media_quick_choices
            tool_registry.set_tool_config("video_gen", "model", model_value)
            seed_configured_media_quick_choices()
            return f"Video generation model set to: {model_value}"
        except Exception as exc:
            return f"Failed to change video gen model: {exc}"

    elif setting == "run_dream_cycle":
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": "Run dream cycle now",
            "description": "Manually trigger the dream cycle immediately (bypasses time window check)",
            "args": {"setting": "run_dream_cycle", "value": "now"},
        })
        if not approval:
            return "Dream cycle run cancelled."
        try:
            import threading
            from row_bot.dream_cycle import run_dream_cycle
            def _run():
                try:
                    run_dream_cycle()
                except Exception as exc:
                    logger.error("Manual dream cycle failed: %s", exc, exc_info=True)
            threading.Thread(target=_run, name="manual-dream-cycle", daemon=True).start()
            return "Dream cycle started in background. It may take a few minutes to complete."
        except Exception as exc:
            return f"Failed to start dream cycle: {exc}"

    elif setting == "self_improvement":
        enabled = value.strip().lower() in ("on", "true", "yes", "enable", "enabled", "1")
        disabled = value.strip().lower() in ("off", "false", "no", "disable", "disabled", "0")
        if not enabled and not disabled:
            return f"Invalid value '{value}'. Use 'on' or 'off'."
        approval = interrupt({
            "tool": "row_bot_update_setting",
            "label": f"{'Enable' if enabled else 'Disable'} self-improvement",
            "description": f"Set self-improvement to {'enabled' if enabled else 'disabled'}",
            "args": {"setting": "self_improvement", "value": value},
        })
        if not approval:
            return "Self-improvement change cancelled."
        try:
            from row_bot.identity import set_self_improvement_enabled
            from row_bot.agent import clear_agent_cache
            set_self_improvement_enabled(enabled)
            clear_agent_cache()
            return f"Self-improvement {'enabled' if enabled else 'disabled'}."
        except Exception as exc:
            return f"Failed to change self-improvement: {exc}"

    else:
        return (
            f"Unknown setting '{setting}'. Supported: {_model_setting_supported_message()}."
        )


# ═════════════════════════════════════════════════════════════════════════════
# SKILL CREATION (additive only — never overwrites existing)
# ═════════════════════════════════════════════════════════════════════════════

class _CreateSkillInput(BaseModel):
    name: str = Field(description="Unique snake_case identifier for the skill (e.g. 'weekly_report').")
    display_name: str = Field(description="Human-readable name (e.g. 'Weekly Report').")
    icon: str = Field(description="Single emoji icon (e.g. '📊').")
    description: str = Field(description="One-line description of what the skill does.")
    instructions: str = Field(description="The full skill instructions — step-by-step guidance for the agent.")
    tags: str = Field(default="", description="Comma-separated tags (e.g. 'productivity, writing').")


def _create_skill(
    name: str,
    display_name: str,
    icon: str,
    description: str,
    instructions: str,
    tags: str = "",
) -> str:
    """Create a skill proposal. Applying the proposal performs the mutation."""
    try:
        from row_bot.evolution import build_create_skill_proposal

        proposal = build_create_skill_proposal(
            {
                "name": name,
                "display_name": display_name,
                "icon": icon,
                "description": description,
                "instructions": instructions,
                "tags": tags,
                "enabled": True,
            },
            rationale="Requested through row_bot_create_skill. This records a previewable proposal before any skill file is created.",
        )
        validation = proposal.get("preview", {}).get("validation", {})
        return (
            f"Skill creation proposal created: {proposal['id']}\n"
            f"Status: {proposal.get('status')}\n"
            f"Validation: {json.dumps(validation, ensure_ascii=False)}\n"
            "Preview this proposal, then call row_bot_apply_proposal with the proposal id after user approval."
        )
    except Exception as exc:
        return f"Failed to create skill proposal: {exc}"

# ═════════════════════════════════════════════════════════════════════════════
# SKILL PATCHING (versioned, with user-space override for bundled skills)
# ═════════════════════════════════════════════════════════════════════════════

class _PatchSkillInput(BaseModel):
    name: str = Field(description="The name (identifier) of the skill to patch.")
    updated_instructions: str = Field(
        description="The complete updated instructions text for the skill."
    )
    reason: str = Field(
        description="Brief explanation of why this patch improves the skill."
    )


class _SendFeedbackInput(BaseModel):
    title: str = Field(description="Short feedback title.")
    summary: str = Field(description="Short feedback summary. Secrets and local user paths are redacted before saving.")
    insight_id: str = Field(default="", description="Optional insight id to attach to the feedback proposal.")
    include_logs: bool = Field(default=False, description="Include a small redacted recent warning/error log excerpt.")


class _ProposalApplyInput(BaseModel):
    proposal_id: str = Field(description="The controlled-evolution proposal id to apply.")


class _ProposalRejectInput(BaseModel):
    proposal_id: str = Field(description="The controlled-evolution proposal id to reject.")
    reason: str = Field(default="", description="Why this proposal should not be repeated.")


class _ProposalVerifyInput(BaseModel):
    proposal_id: str = Field(description="The proposal id to mark verified after validation or user confirmation.")
    note: str = Field(default="", description="Short verification note.")


class _CuratorDryRunInput(BaseModel):
    create_proposals: bool = Field(default=True, description="Whether the dry-run should create review proposals. It never mutates skills.")


def _patch_skill(name: str, updated_instructions: str, reason: str) -> str:
    """Create a bounded patch proposal. Applying the proposal performs the mutation."""
    try:
        from row_bot.evolution import build_patch_skill_proposal

        proposal = build_patch_skill_proposal(
            target_skill=name.strip(),
            updated_instructions=updated_instructions,
            reason=reason,
        )
        preview = proposal.get("preview", {})
        validation = preview.get("validation", {})
        return (
            f"Skill patch proposal created: {proposal['id']}\n"
            f"Status: {proposal.get('status')}\n"
            f"Changed lines: {preview.get('changed_lines', 0)}\n"
            f"Validation: {json.dumps(validation, ensure_ascii=False)}\n"
            "Preview the diff, then call row_bot_apply_proposal with the proposal id after user approval."
        )
    except Exception as exc:
        return f"Failed to create skill patch proposal: {exc}"


def _send_feedback(
    title: str,
    summary: str,
    insight_id: str = "",
    include_logs: bool = False,
) -> str:
    """Create a redacted send-feedback proposal without uploading anything."""

    try:
        from row_bot.evolution import build_send_feedback_proposal

        insight = None
        if insight_id:
            try:
                from row_bot.insights import get_insight_by_id

                insight = get_insight_by_id(insight_id)
            except Exception:
                insight = None
        proposal = build_send_feedback_proposal(
            insight,
            title=title,
            summary=summary,
            include_logs=include_logs,
            insight_ids=[insight_id] if insight_id else [],
        )
        return (
            f"Send feedback proposal created: {proposal['id']}\n"
            "Preview the redacted report, then copy it, save it locally, or submit it through the contact page."
        )
    except Exception as exc:
        return f"Failed to create send feedback proposal: {exc}"


def _apply_controlled_proposal(proposal_id: str) -> str:
    """Apply a proposal through the controlled action-run path."""

    try:
        from row_bot.evolution import apply_proposal

        result = apply_proposal(proposal_id, require_approval=True)
        run = result.get("action_run") or {}
        refs = run.get("result_refs") or []
        rollback = run.get("rollback_ref") or ""
        details = [result.get("message", "Proposal applied.")]
        if refs:
            details.append("Result refs: " + ", ".join(str(ref) for ref in refs))
        if rollback:
            details.append(f"Rollback ref: {rollback}")
        return "\n".join(details)
    except Exception as exc:
        return f"Failed to apply proposal: {exc}"


def _reject_controlled_proposal(proposal_id: str, reason: str = "") -> str:
    try:
        from row_bot.evolution import reject_proposal

        proposal = reject_proposal(proposal_id, reason)
        return f"Proposal rejected: {proposal.get('title')} ({proposal_id}). Rejection memory recorded."
    except Exception as exc:
        return f"Failed to reject proposal: {exc}"


def _verify_controlled_proposal(proposal_id: str, note: str = "") -> str:
    try:
        from row_bot.evolution import mark_proposal_verified

        if mark_proposal_verified(proposal_id, note):
            return f"Proposal verified: {proposal_id}"
        return f"Proposal not found or could not be verified: {proposal_id}"
    except Exception as exc:
        return f"Failed to verify proposal: {exc}"


def _curator_dry_run(create_proposals: bool = True) -> str:
    try:
        from row_bot.evolution import review_skill_library_dry_run

        report = review_skill_library_dry_run(create_proposals=create_proposals)
        summary = report.get("summary", {})
        return (
            f"Curator dry-run complete: {report.get('id')}\n"
            f"Manual skills: {summary.get('manual_skill_count', 0)}\n"
            f"Findings: {summary.get('finding_count', 0)}\n"
            f"Proposals: {summary.get('proposal_count', 0)}\n"
            f"Mutated skills: {report.get('mutated_skills')}"
        )
    except Exception as exc:
        return f"Curator dry-run failed: {exc}"


# ═════════════════════════════════════════════════════════════════════════════
# TOOL CLASS
# ═════════════════════════════════════════════════════════════════════════════

class RowBotStatusTool(BaseTool):

    @property
    def name(self) -> str:
        return "row_bot_status"

    @property
    def display_name(self) -> str:
        return "🪞 Row-Bot Status"

    @property
    def description(self) -> str:
        return (
            "Query or change Row-Bot's own configuration: current model and pinned model choices, "
            "active channels, memory stats, skills, tools, API keys, "
            "identity settings, and scheduled tasks."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def destructive_tool_names(self) -> set[str]:
        # These sub-tools perform their own interrupt() calls with specific
        # labels and arguments. Listing them here would wrap them in a second,
        # generic approval gate before the real tool logic runs.
        return set()

    def as_langchain_tools(self) -> list:
        tools = [
            StructuredTool.from_function(
                func=_row_bot_status,
                name="row_bot_status",
                description=(
                    "Query Row-Bot's current status and configuration. "
                    "Categories: overview, version, model, channels, memory, skills, "
                    "tools, plugins, mcp, providers, insights, evolution, api_keys, identity, tasks, "
                    "agents, agent_profiles, goals, vision, "
                    "image_gen, video_gen, voice, config, designer, updates, logs, errors."
                ),
                args_schema=_StatusQueryInput,
            ),
            StructuredTool.from_function(
                func=_update_setting,
                name="row_bot_update_setting",
                description=(
                    "Change a Row-Bot setting. Requires user confirmation. "
                    "Settings: thread_model (current conversation Brain override; use 'default' to clear), "
                    "default_model (global Brain default for new conversations/workflows), "
                    "model (legacy global-default alias outside threaded chat), "
                    "vision_model, name, personality, context_size, "
                    "cloud_context_size, dream_cycle (on/off), "
                    "dream_window (e.g. '1-5'), "
                    "skill_toggle for Skill Library Available/Off (e.g. 'deep_research:off'), "
                    "skill_pin for pinned default active skills (e.g. 'deep_research:on'), "
                    "tool_toggle (e.g. 'arxiv:off' or 'mcp:off'), "
                    "image_gen_model, video_gen_model, "
                    "run_dream_cycle (trigger immediately), "
                    "self_improvement (on/off)."
                ),
                args_schema=_SettingUpdateInput,
            ),
        ]

        # Self-improvement tools: only available when enabled
        try:
            from row_bot.identity import is_self_improvement_enabled
            self_improve = is_self_improvement_enabled()
        except Exception:
            self_improve = True  # safe fallback

        if self_improve:
            tools.append(StructuredTool.from_function(
                func=_create_skill,
                name="row_bot_create_skill",
                description=(
                    "Create a proposal for a new user skill (reusable instruction pack). "
                    "Requires user confirmation. Additive only — cannot "
                    "apply the proposal separately after preview and approval."
                ),
                args_schema=_CreateSkillInput,
            ))
            tools.append(StructuredTool.from_function(
                func=_patch_skill,
                name="row_bot_patch_skill",
                description=(
                    "Create a bounded patch-skill proposal with a diff preview. "
                    "Does not mutate skills; apply the proposal separately after approval. "
                    "Tool guides cannot be patched through this path."
                ),
                args_schema=_PatchSkillInput,
            ))
            tools.append(StructuredTool.from_function(
                func=_send_feedback,
                name="row_bot_send_feedback",
                description=(
                    "Create a redacted send-feedback proposal. Applying it saves a local "
                    "markdown report; the user can copy it or submit it through the "
                    "Row-Bot contact page."
                ),
                args_schema=_SendFeedbackInput,
            ))
            tools.append(StructuredTool.from_function(
                func=_apply_controlled_proposal,
                name="row_bot_apply_proposal",
                description=(
                    "Apply a controlled self-evolution proposal. Mutating proposal types "
                    "are approval-gated, audited as ActionRun records, and skill patches "
                    "include rollback refs."
                ),
                args_schema=_ProposalApplyInput,
            ))
            tools.append(StructuredTool.from_function(
                func=_reject_controlled_proposal,
                name="row_bot_reject_proposal",
                description=(
                    "Reject a controlled self-evolution proposal and record feedback so "
                    "similar future proposals can account for the rejection."
                ),
                args_schema=_ProposalRejectInput,
            ))
            tools.append(StructuredTool.from_function(
                func=_verify_controlled_proposal,
                name="row_bot_verify_proposal",
                description=(
                    "Mark an applied proposal verified after explicit validation or user confirmation."
                ),
                args_schema=_ProposalVerifyInput,
            ))
            tools.append(StructuredTool.from_function(
                func=_curator_dry_run,
                name="row_bot_review_skill_library",
                description=(
                    "Run a manual dry-run review of the skill library. It reports findings "
                    "and may create proposals, but never mutates skill files."
                ),
                args_schema=_CuratorDryRunInput,
            ))

        return tools

    def execute(self, query: str) -> str:
        return _row_bot_status(query)


registry.register(RowBotStatusTool())
