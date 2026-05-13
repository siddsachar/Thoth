"""Thoth status / introspection tool — lets the agent query its own configuration.

Provides read operations (always allowed) for settings, channel status,
memory stats, model info, provider status, and API key validity. Write operations
(changing settings, toggling channels) require user confirmation via
LangGraph interrupt().
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from datetime import datetime

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

logger = logging.getLogger(__name__)

_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_SKILL_VERSIONS_DIR = _DATA_DIR / "skill_versions"


# ═════════════════════════════════════════════════════════════════════════════
# INPUT SCHEMAS
# ═════════════════════════════════════════════════════════════════════════════

class _StatusQueryInput(BaseModel):
    category: str = Field(
        description=(
            "What to query. One of: 'overview' (full status summary), "
            "'version' (Thoth version number), "
            "'model' (current model and provider info), "
            "'channels' (messaging channel status), "
            "'memory' (knowledge graph stats), "
            "'skills' (enabled/disabled skills), "
            "'tools' (enabled/disabled tools), "
            "'mcp' (external MCP server/tool status), "
            "'providers' (provider connections, credential sources, and Quick Choices), "
            "'insights' (active dream-cycle insights and last analysis), "
            "'api_keys' (legacy/API key storage status — never returns key values), "
            "'identity' (configured name and personality), "
            "'tasks' (active scheduled tasks summary), "
            "'vision' (vision/camera model and settings), "
            "'image_gen' (image generation model), "
            "'video_gen' (video generation model), "
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
            "'model' (switch active model; value may be a local model, provider model, or Quick Choice), "
            "'vision_model' (switch Vision model; value may be an installed local vision model, provider vision model, or Vision Quick Choice), "
            "'name' (change assistant name), "
            "'personality' (change personality text), "
            "'context_size' (local model context window — value is token count e.g. '65536'), "
            "'cloud_context_size' (provider model context cap — value is token count), "
            "'dream_cycle' (enable/disable — value is 'on' or 'off'), "
            "'dream_window' (dream cycle hours — value is 'START-END' e.g. '1-5'), "
            "'skill_toggle' (enable/disable a skill — value is 'skill_name:on' or 'skill_name:off'), "
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
        from tools.image_gen_tool import get_available_image_models
        options = get_available_image_models()
    elif setting == "video_gen_model":
        from tools.video_gen_tool import get_available_video_models
        options = get_available_video_models()
    else:
        return normalized

    if normalized in options:
        return normalized

    def _media_lookup_tokens(text: str) -> set[str]:
        stripped = re.sub(r"^[^A-Za-z0-9]+", "", str(text or "")).strip()
        stripped = re.sub(r"\s*\([^)]*\)\s*$", "", stripped).strip()
        tokens = {_normalize_lookup_token(stripped)}
        if "/" in stripped:
            tokens.add(_normalize_lookup_token(stripped.split("/", 1)[1]))
        return {token for token in tokens if token}

    desired = _normalize_lookup_token(normalized)
    matches: list[str] = []
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
            matches.append(config_value)

    return matches[0] if len(matches) == 1 else normalized


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
        "dream_cycle, dream_window, skill_toggle, tool_toggle, image_gen_model, "
        "video_gen_model, run_dream_cycle, self_improvement"
    )


def _surface_label(surface: str) -> str:
    return {"chat": "Brain", "vision": "Vision"}.get(surface, surface.replace("_", " ").title())


def _resolve_model_update_value(value: str, *, surface: str) -> tuple[str | None, str | None]:
    """Resolve a status-tool model update to a runnable model id or an error."""
    from models import is_model_local, list_cloud_models, list_cloud_vision_models
    from providers.capabilities import snapshot_supports_surface
    from providers.selection import list_quick_choices, model_ref, resolve_selection

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
            from providers.ollama import is_ollama_vision_capable
            if not is_ollama_vision_capable(model_id):
                return None, f"Local model '{raw}' is installed, but Thoth does not have Vision capability metadata for it. Choose a Vision model from Settings → Models."
        return model_id, None

    known_provider_models = set(list_cloud_models(provider_id))
    if model_id not in known_provider_models:
        return None, f"Provider model '{raw}' is not in the current catalog. Refresh Providers or pin it from Settings → Models first."
    if surface == "vision" and model_id not in set(list_cloud_vision_models()):
        provider_ref = model_ref(provider_id, model_id)
        from models import is_cloud_vision_model
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
    from tools import registry as tool_registry

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

def _query_overview() -> str:
    """Full status summary across all categories."""
    from version import __version__
    parts = [f"**Thoth v{__version__}**"]
    for cat in ("model", "providers", "vision", "image_gen", "video_gen", "voice", "api_keys", "memory",
                "channels", "skills", "tools", "mcp", "identity", "tasks", "insights", "config", "designer", "updates"):
        try:
            parts.append(_QUERY_HANDLERS[cat]())
        except Exception as exc:
            parts.append(f"[{cat}] Error: {exc}")
    return "\n\n".join(parts)


def _query_model() -> str:
    try:
        from models import (get_current_model, is_model_local, get_context_size,
                            get_cloud_provider, get_provider_emoji,
                            _active_model_override,
                            get_user_context_size, get_cloud_context_size)
        from providers.selection import provider_display_label
        default_model = get_current_model()
        override = _active_model_override.get("")
        model = override if override else default_model
        local = is_model_local(model)
        ctx = get_context_size(model)
        provider = "local" if local else (get_cloud_provider(model) or "provider")
        provider_label = provider_display_label(provider)
        emoji = get_provider_emoji(model)
        lines = [
            "**Current Model**",
            f"- Model: {emoji} {model}",
            f"- Type: {'Local (Ollama)' if local else f'Provider ({provider_label})'}",
            f"- Effective context: {ctx:,} tokens",
        ]
        if local:
            lines.append(f"- Local context cap: {get_user_context_size():,} tokens")
        else:
            lines.append(f"- Provider context cap: {get_cloud_context_size():,} tokens")
        if override and override != default_model:
            lines.append(f"- ⚠️ Override active (global default: {default_model})")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Current Model**\nError retrieving model info: {exc}"


def _query_channels() -> str:
    try:
        from channels.registry import all_channels
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
        from knowledge_graph import count_entities, count_relations
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
        from skills import get_manual_skill_statuses
        skill_statuses = get_manual_skill_statuses()
        if not skill_statuses:
            return "**Skills**\nNo skills found."
        lines = ["**Skills**"]
        for skill, is_enabled in skill_statuses:
            status = "enabled" if is_enabled else "disabled"
            lines.append(f"- {skill.display_name}: {status}")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Skills**\nError: {exc}"


def _query_tools() -> str:
    try:
        from tools.registry import get_all_tools, is_enabled
        tools = get_all_tools()
        if not tools:
            return "**Tools**\nNo tools registered."
        active_developer = False
        try:
            from developer.tool_context import get_thread_id, get_workspace_id, infer_workspace_id_from_thread
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
        lines = [f"**Tools** ({len(enabled)} enabled{suffix}, {len(disabled)} disabled)"]
        for t in enabled:
            lines.append(f"- ✅ {t.display_name}")
        for t in contextual:
            lines.append(f"- contextual: {t.display_name} (active for the current Developer workspace)")
        for t in disabled:
            lines.append(f"- ❌ {t.display_name}")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Tools**\nError: {exc}"


def _query_mcp() -> str:
    try:
        from mcp_client.runtime import get_status_summary
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
        from api_keys import get_key
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
        from providers.status import summarize_providers
        return summarize_providers()
    except Exception as exc:
        return f"**Providers**\nError: {exc}"


def _query_insights() -> str:
    try:
        from insights import get_active_insights, get_insights_meta

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
        return "\n".join(lines)
    except Exception as exc:
        return f"**Insights**\nError: {exc}"


def _query_identity() -> str:
    try:
        from identity import get_identity_config, is_self_improvement_enabled
        cfg = get_identity_config()
        name = cfg.get("name", "Thoth")
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
        from tasks import list_tasks
        tasks = list_tasks()
        if not tasks:
            return "**Scheduled Tasks**\nNo tasks configured."
        lines = [f"**Scheduled Tasks** ({len(tasks)} total)"]
        for t in tasks[:10]:  # Show at most 10
            status = "enabled" if t.get("enabled", True) else "disabled"
            lines.append(f"- {t.get('name', 'Unnamed')}: {t.get('schedule', 'no schedule')} ({status})")
        if len(tasks) > 10:
            lines.append(f"  ... and {len(tasks) - 10} more")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Scheduled Tasks**\nError: {exc}"


def _query_logs() -> str:
    """Recent warnings and errors from the log file."""
    try:
        from logging_config import read_recent_logs
        entries = read_recent_logs(n=50)
        # Filter to WARNING+ and exclude thoth_status calls to avoid recursion
        filtered = [
            e for e in entries
            if e.get("level", "") in ("WARNING", "ERROR", "CRITICAL")
            and "thoth_status" not in e.get("msg", "")
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
        from logging_config import read_recent_logs
        entries = read_recent_logs(n=100)
        errors = [
            e for e in entries
            if e.get("level", "") in ("ERROR", "CRITICAL")
            and "thoth_status" not in e.get("msg", "")
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
        from vision import _load_settings, DEFAULT_VISION_MODEL
        settings = _load_settings()
        model = settings.get("model", DEFAULT_VISION_MODEL)
        enabled = settings.get("enabled", True)
        camera = settings.get("camera_index", 0)
        lines = [
            "**Vision**",
            f"- Model: {model}",
            f"- Enabled: {'yes' if enabled else 'no'}",
            f"- Camera index: {camera}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"**Vision**\nError: {exc}"


def _query_image_gen() -> str:
    """Image generation model."""
    try:
        from tools.image_gen_tool import _get_configured_selection, DEFAULT_MODEL
        from tools.registry import is_enabled
        selection = _get_configured_selection()
        lines = [
            "**Image Generation**",
            f"- Tool: {'enabled' if is_enabled('image_gen') else 'disabled'}",
            f"- Model: {selection}",
        ]
        if selection == DEFAULT_MODEL:
            lines.append(f"- (default — change in Settings → Models)")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Image Generation**\nError: {exc}"


def _query_video_gen() -> str:
    """Video generation model."""
    try:
        from tools.video_gen_tool import _get_configured_selection, DEFAULT_MODEL
        from tools.registry import is_enabled
        selection = _get_configured_selection()
        lines = [
            "**Video Generation**",
            f"- Tool: {'enabled' if is_enabled('video_gen') else 'disabled'}",
            f"- Model: {selection}",
        ]
        if selection == DEFAULT_MODEL:
            lines.append("- (default — change in Settings → Models)")
        return "\n".join(lines)
    except Exception as exc:
        return f"**Video Generation**\nError: {exc}"


def _query_voice() -> str:
    """TTS and STT settings."""
    try:
        lines = ["**Voice & Speech**"]
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
        return "\n".join(lines)
    except Exception as exc:
        return f"**Voice & Speech**\nError: {exc}"


def _query_config() -> str:
    """Miscellaneous configuration: context caps, dream cycle, wiki vault, memory extraction."""
    lines = ["**Configuration**"]
    try:
        # Context size caps
        from models import get_user_context_size, get_cloud_context_size
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
                lines.append(f"  Path: {wv.get('vault_path', '~/.thoth/vault')}")
        else:
            lines.append("- Wiki vault: disabled (default)")
    except Exception:
        pass
    return "\n".join(lines)


def _query_version() -> str:
    from version import __version__
    return f"**Thoth Version**: v{__version__}"


def _query_designer() -> str:
    try:
        from designer.storage import list_projects
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
    "mcp": _query_mcp,
    "providers": _query_providers,
    "insights": _query_insights,
    "api_keys": _query_api_keys,
    "identity": _query_identity,
    "tasks": _query_tasks,
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
        import updater
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


def _thoth_status(category: str) -> str:
    """Query Thoth's current status and configuration."""
    category = category.strip().lower()
    handler = _QUERY_HANDLERS.get(category)
    if handler is None:
        available = ", ".join(sorted(_QUERY_HANDLERS.keys()))
        return f"Unknown category '{category}'. Available: {available}"
    try:
        return handler()
    except Exception as exc:
        logger.error("thoth_status query error for '%s': %s", category, exc, exc_info=True)
        return f"Error querying {category}: {exc}"


# ═════════════════════════════════════════════════════════════════════════════
# WRITE HANDLERS (require user confirmation)
# ═════════════════════════════════════════════════════════════════════════════

def _update_setting(setting: str, value: str) -> str:
    """Update a Thoth setting (with interrupt-based confirmation)."""
    from langgraph.types import interrupt

    setting = setting.strip().lower()
    value = value.strip()

    if setting == "model":
        approval = interrupt({
            "tool": "thoth_update_setting",
            "label": "Change active model",
            "description": f"Switch the active model to: {value}",
            "args": {"setting": "model", "value": value},
        })
        if not approval:
            return "Model change cancelled."
        try:
            from models import set_model
            from agent import clear_agent_cache
            model_value, error = _resolve_model_update_value(value, surface="chat")
            if error:
                return error
            if not model_value:
                return f"Model '{value}' not found. Pin it in Settings → Models first."
            set_model(model_value)
            clear_agent_cache()
            return f"Active model changed to: {model_value}"
        except Exception as exc:
            return f"Failed to change model: {exc}"

    elif setting == "vision_model":
        approval = interrupt({
            "tool": "thoth_update_setting",
            "label": "Change Vision model",
            "description": f"Switch the Vision model to: {value}",
            "args": {"setting": "vision_model", "value": value},
        })
        if not approval:
            return "Vision model change cancelled."
        try:
            from agent import clear_agent_cache
            from tools.vision_tool import _get_vision_service

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
            "tool": "thoth_update_setting",
            "label": "Change assistant name",
            "description": f"Change the assistant name to: {value}",
            "args": {"setting": "name", "value": value},
        })
        if not approval:
            return "Name change cancelled."
        try:
            from identity import get_identity_config, save_identity_config
            from agent import clear_agent_cache
            cfg = get_identity_config()
            cfg["name"] = value
            save_identity_config(cfg)
            clear_agent_cache()
            return f"Assistant name changed to: {value}"
        except Exception as exc:
            return f"Failed to change name: {exc}"

    elif setting == "personality":
        approval = interrupt({
            "tool": "thoth_update_setting",
            "label": "Change personality",
            "description": f"Set personality to: {value}",
            "args": {"setting": "personality", "value": value},
        })
        if not approval:
            return "Personality change cancelled."
        try:
            from identity import get_identity_config, save_identity_config, sanitize_personality
            from agent import clear_agent_cache
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
            "tool": "thoth_update_setting",
            "label": "Change local context size",
            "description": f"Set local model context window to {size:,} tokens",
            "args": {"setting": "context_size", "value": value},
        })
        if not approval:
            return "Context size change cancelled."
        try:
            from models import set_context_size
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
            "tool": "thoth_update_setting",
            "label": "Change provider context cap",
            "description": f"Set provider context cap to {size:,} tokens",
            "args": {"setting": "cloud_context_size", "value": value},
        })
        if not approval:
            return "Provider context size change cancelled."
        try:
            from models import set_cloud_context_size
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
            "tool": "thoth_update_setting",
            "label": f"{'Enable' if enabled else 'Disable'} dream cycle",
            "description": f"Set dream cycle to {'enabled' if enabled else 'disabled'}",
            "args": {"setting": "dream_cycle", "value": value},
        })
        if not approval:
            return "Dream cycle change cancelled."
        try:
            from dream_cycle import set_enabled as dc_set_enabled
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
            "tool": "thoth_update_setting",
            "label": "Change dream cycle window",
            "description": f"Set dream cycle window to {start}:00–{end}:00",
            "args": {"setting": "dream_window", "value": value},
        })
        if not approval:
            return "Dream window change cancelled."
        try:
            from dream_cycle import set_window
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
        approval = interrupt({
            "tool": "thoth_update_setting",
            "label": f"{'Enable' if on else 'Disable'} skill '{name_part}'",
            "description": f"Set skill '{name_part}' to {'enabled' if on else 'disabled'}",
            "args": {"setting": "skill_toggle", "value": value},
        })
        if not approval:
            return "Skill toggle cancelled."
        try:
            from skills import set_enabled as skill_set_enabled
            skill_set_enabled(name_part, on)
            return f"Skill '{name_part}' {'enabled' if on else 'disabled'}."
        except Exception as exc:
            return f"Failed to toggle skill: {exc}"

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
        from tools import registry as tool_registry
        resolved_name, resolved_label, suggestions = _resolve_tool_name(name_part)
        if not resolved_name:
            suggestion_text = f" Try one of: {', '.join(suggestions)}." if suggestions else ""
            return f"Unknown tool '{name_part}'.{suggestion_text}"
        if resolved_name == "mcp":
            try:
                from mcp_client import config as mcp_config
                current_state = mcp_config.is_globally_enabled()
            except Exception:
                current_state = tool_registry.is_enabled(resolved_name)
        else:
            current_state = tool_registry.is_enabled(resolved_name)
        if current_state == on:
            return f"Tool '{resolved_label}' is already {'enabled' if on else 'disabled'}."
        canonical_value = f"{resolved_name}:{'on' if on else 'off'}"
        approval = interrupt({
            "tool": "thoth_update_setting",
            "label": f"{'Enable' if on else 'Disable'} tool '{resolved_label}'",
            "description": f"Set tool '{resolved_label}' to {'enabled' if on else 'disabled'}",
            "args": {"setting": "tool_toggle", "value": canonical_value},
        })
        if not approval:
            return "Tool toggle cancelled."
        try:
            if resolved_name == "mcp":
                from mcp_client import config as mcp_config
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
            "tool": "thoth_update_setting",
            "label": "Change image generation model",
            "description": f"Set image gen model to: {model_value}",
            "args": {"setting": "image_gen_model", "value": model_value},
        })
        if not approval:
            return "Image gen model change cancelled."
        try:
            from tools import registry as tool_registry
            from providers.selection import seed_configured_media_quick_choices
            tool_registry.set_tool_config("image_gen", "model", model_value)
            seed_configured_media_quick_choices()
            return f"Image generation model set to: {model_value}"
        except Exception as exc:
            return f"Failed to change image gen model: {exc}"

    elif setting == "video_gen_model":
        model_value = _normalize_provider_model_value(setting, value)
        approval = interrupt({
            "tool": "thoth_update_setting",
            "label": "Change video generation model",
            "description": f"Set video gen model to: {model_value}",
            "args": {"setting": "video_gen_model", "value": model_value},
        })
        if not approval:
            return "Video gen model change cancelled."
        try:
            from tools import registry as tool_registry
            from providers.selection import seed_configured_media_quick_choices
            tool_registry.set_tool_config("video_gen", "model", model_value)
            seed_configured_media_quick_choices()
            return f"Video generation model set to: {model_value}"
        except Exception as exc:
            return f"Failed to change video gen model: {exc}"

    elif setting == "run_dream_cycle":
        approval = interrupt({
            "tool": "thoth_update_setting",
            "label": "Run dream cycle now",
            "description": "Manually trigger the dream cycle immediately (bypasses time window check)",
            "args": {"setting": "run_dream_cycle", "value": "now"},
        })
        if not approval:
            return "Dream cycle run cancelled."
        try:
            import threading
            from dream_cycle import run_dream_cycle
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
            "tool": "thoth_update_setting",
            "label": f"{'Enable' if enabled else 'Disable'} self-improvement",
            "description": f"Set self-improvement to {'enabled' if enabled else 'disabled'}",
            "args": {"setting": "self_improvement", "value": value},
        })
        if not approval:
            return "Self-improvement change cancelled."
        try:
            from identity import set_self_improvement_enabled
            from agent import clear_agent_cache
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
    """Create a new user skill (requires confirmation, additive only)."""
    from langgraph.types import interrupt

    # Validate name format
    name = name.strip().lower().replace(" ", "_")
    if not name:
        return "Skill name cannot be empty."

    # Check for existing skill
    try:
        from skills import get_all_skills
        existing = {s.name for s in get_all_skills()}
        if name in existing:
            return f"A skill named '{name}' already exists. Skill creation is additive only — cannot overwrite."
    except Exception:
        pass

    # Require user confirmation
    approval = interrupt({
        "tool": "thoth_create_skill",
        "label": "Create new skill",
        "description": f"Create skill '{display_name}' ({name}): {description}",
        "args": {"name": name, "display_name": display_name},
    })
    if not approval:
        return "Skill creation cancelled."

    try:
        from skills import create_skill
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        skill = create_skill(
            name=name,
            display_name=display_name,
            icon=icon,
            description=description,
            instructions=instructions,
            tags=tag_list,
            enabled=True,
        )
        if skill:
            return f"Skill '{display_name}' created and enabled. View it in Settings → Skills."
        return "Skill creation failed — unknown error."
    except Exception as exc:
        return f"Failed to create skill: {exc}"


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


def _version_backup(skill_name: str, content: str, reason: str) -> int:
    """Save a version backup and return the new version number."""
    version_dir = _SKILL_VERSIONS_DIR / skill_name
    version_dir.mkdir(parents=True, exist_ok=True)

    changelog_path = version_dir / "changelog.json"
    changelog: list[dict] = []
    if changelog_path.exists():
        try:
            changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    version_num = len(changelog) + 1
    backup_path = version_dir / f"v{version_num}.md"
    backup_path.write_text(content, encoding="utf-8")

    changelog.append({
        "version": version_num,
        "timestamp": datetime.now().isoformat(),
        "reason": reason,
    })
    changelog_path.write_text(
        json.dumps(changelog, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return version_num


def _patch_skill(name: str, updated_instructions: str, reason: str) -> str:
    """Patch an existing skill with updated instructions (requires confirmation)."""
    from langgraph.types import interrupt

    name = name.strip()
    if not name:
        return "Skill name cannot be empty."

    try:
        from skills import get_all_skills, is_tool_guide
    except ImportError:
        return "Skills module not available."

    # Find the skill
    all_skills = {s.name: s for s in get_all_skills()}
    skill = all_skills.get(name)
    if not skill:
        return f"Skill '{name}' not found."

    # Tool guides are report-only — cannot be patched
    if is_tool_guide(skill):
        return (
            f"'{name}' is a tool guide and cannot be patched directly. "
            f"If you noticed a discrepancy, save it as a self_knowledge "
            f"memory for the developer to review."
        )

    # Require user confirmation
    approval = interrupt({
        "tool": "thoth_patch_skill",
        "label": f"Patch skill: {skill.display_name}",
        "description": (
            f"Update instructions for '{skill.display_name}'.\n"
            f"Reason: {reason}\n\n"
            f"The original will be backed up before any changes."
        ),
        "args": {"name": name, "reason": reason},
    })
    if not approval:
        return "Skill patch cancelled."

    try:
        # Backup current version
        current_content = ""
        if skill.path:
            md_path = skill.path / "SKILL.md"
            if md_path.exists():
                current_content = md_path.read_text(encoding="utf-8")
        if current_content:
            ver = _version_backup(name, current_content, reason)
            logger.info("Backed up skill '%s' as v%d", name, ver)

        if skill.source == "bundled":
            # Bundled skill — create user-space override (original untouched)
            from skills import create_skill
            new_skill = create_skill(
                name=skill.name,
                display_name=skill.display_name,
                icon=skill.icon,
                description=skill.description,
                instructions=updated_instructions,
                tools=skill.tools if skill.tools else None,
                tags=skill.tags if skill.tags else None,
                enabled=True,
                version=skill.version,
            )
            if new_skill:
                return (
                    f"Skill '{skill.display_name}' patched via user-space override. "
                    f"Original bundled version preserved. "
                    f"Backup saved. View in Settings → Skills."
                )
            return "Failed to create user-space override."
        else:
            # User skill — update in place
            from skills import update_skill
            updated = update_skill(
                name=name,
                instructions=updated_instructions,
            )
            if updated:
                return (
                    f"Skill '{skill.display_name}' patched successfully. "
                    f"Backup saved as v{ver}."
                )
            return "Skill update failed."

    except Exception as exc:
        logger.error("Skill patch failed for '%s': %s", name, exc, exc_info=True)
        return f"Failed to patch skill: {exc}"


# ═════════════════════════════════════════════════════════════════════════════
# TOOL CLASS
# ═════════════════════════════════════════════════════════════════════════════

class ThothStatusTool(BaseTool):

    @property
    def name(self) -> str:
        return "thoth_status"

    @property
    def display_name(self) -> str:
        return "🪞 Thoth Status"

    @property
    def description(self) -> str:
        return (
            "Query or change Thoth's own configuration: current model, "
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
                func=_thoth_status,
                name="thoth_status",
                description=(
                    "Query Thoth's current status and configuration. "
                    "Categories: overview, version, model, channels, memory, skills, "
                    "tools, mcp, providers, insights, api_keys, identity, tasks, vision, "
                    "image_gen, video_gen, voice, config, designer, updates, logs, errors."
                ),
                args_schema=_StatusQueryInput,
            ),
            StructuredTool.from_function(
                func=_update_setting,
                name="thoth_update_setting",
                description=(
                    "Change a Thoth setting. Requires user confirmation. "
                    "Settings: model, name, personality, context_size, "
                    "cloud_context_size, dream_cycle (on/off), "
                    "dream_window (e.g. '1-5'), "
                    "skill_toggle (e.g. 'deep_research:off'), "
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
            from identity import is_self_improvement_enabled
            self_improve = is_self_improvement_enabled()
        except Exception:
            self_improve = True  # safe fallback

        if self_improve:
            tools.append(StructuredTool.from_function(
                func=_create_skill,
                name="thoth_create_skill",
                description=(
                    "Create a new user skill (reusable instruction pack). "
                    "Requires user confirmation. Additive only — cannot "
                    "overwrite existing skills."
                ),
                args_schema=_CreateSkillInput,
            ))
            tools.append(StructuredTool.from_function(
                func=_patch_skill,
                name="thoth_patch_skill",
                description=(
                    "Patch an existing skill with improved instructions. "
                    "Requires user confirmation. Backs up the original. "
                    "Bundled skills are patched via user-space override "
                    "(originals preserved). Tool guides cannot be patched."
                ),
                args_schema=_PatchSkillInput,
            ))

        return tools

    def execute(self, query: str) -> str:
        return _thoth_status(query)


registry.register(ThothStatusTool())
