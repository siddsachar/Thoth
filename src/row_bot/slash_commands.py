"""Slash command registry and composer token helpers.

This module is intentionally UI-light. It owns command metadata, generated
manual-skill commands, collision rules, text filtering, and send-path command
dispatch that can run without NiceGUI widgets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable, Literal


ArgumentBehavior = Literal["none", "optional", "required", "prefix"]


@dataclass(frozen=True)
class SlashCommandSpec:
    id: str
    slash: str
    aliases: tuple[str, ...]
    title: str
    description: str
    icon: str
    category: str
    argument_behavior: ArgumentBehavior
    handler_key: str
    skill_name: str = ""

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.slash, *self.aliases)


BUILTIN_COMMANDS: tuple[SlashCommandSpec, ...] = (
    SlashCommandSpec(
        "skills", "/skills", ("/skill",), "Skills",
        "Open the Skills picker for this chat.", "auto_fix_high",
        "Skills", "none", "open_skills",
    ),
    SlashCommandSpec(
        "skill-reset", "/skill-reset", ("/skill reset",), "Reset Skills",
        "Reset Smart Skills for this chat.", "restart_alt",
        "Skills", "none", "skill_reset",
    ),
    SlashCommandSpec(
        "noskill", "/noskill", (), "Remove Skill",
        "Remove or prepare to disable a Smart Skill in this chat.", "remove_circle",
        "Skills", "prefix", "noskill",
    ),
    SlashCommandSpec(
        "new", "/new", (), "New Chat",
        "Start a new conversation thread.", "add_comment",
        "Chat", "none", "new_thread",
    ),
    SlashCommandSpec(
        "stop", "/stop", (), "Stop",
        "Stop the current generation if one is running.", "stop_circle",
        "Chat", "none", "stop_generation",
    ),
    SlashCommandSpec(
        "reasoning", "/reasoning", (), "Reasoning",
        "Show or set reasoning for the active model in this chat.", "psychology",
        "Chat", "prefix", "reasoning",
    ),
    SlashCommandSpec(
        "profiles", "/profiles", ("/agent-profiles",), "Agent Profiles",
        "List Agent Profiles available for this chat.", "badge",
        "Agents", "optional", "profiles",
    ),
    SlashCommandSpec(
        "profile", "/profile", ("/agent-profile",), "Agent Profile",
        "Show or set the Agent Profile for this chat.", "person_pin",
        "Agents", "prefix", "profile",
    ),
    SlashCommandSpec(
        "agents", "/agents", (), "Agents",
        "Show Agent Runs for this chat.", "hub",
        "Agents", "optional", "agents",
    ),
    SlashCommandSpec(
        "agent", "/agent", ("/subagent",), "Start Agent",
        "Start a child Agent with an optional profile or --worktree isolation.", "hub",
        "Agents", "prefix", "agent",
    ),
    SlashCommandSpec(
        "goal", "/goal", (), "Goal",
        "Start or control a durable thread goal.", "flag",
        "Agents", "prefix", "goal",
    ),
    SlashCommandSpec(
        "status", "/status", (), "Status",
        "Show a lightweight local status summary.", "monitor_heart",
        "Info", "none", "status",
    ),
    SlashCommandSpec(
        "tools", "/tools", (), "Tools",
        "Show enabled tools read-only.", "construction",
        "Info", "none", "tools",
    ),
    SlashCommandSpec(
        "export", "/export", (), "Export",
        "Open export for the current thread.", "download",
        "App", "none", "export",
    ),
    SlashCommandSpec(
        "help", "/help", (), "Help",
        "Show available slash commands.", "help",
        "Info", "none", "help",
    ),
)


def normalize_command_name(value: str) -> str:
    """Normalize a command or skill label to a slash-token suffix."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text


def normalize_slash(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    head, *tail = text.split(maxsplit=1)
    head = "/" + normalize_command_name(head[1:])
    return f"{head} {tail[0].strip()}" if tail else head


def _skill_aliases(skill) -> tuple[str, ...]:
    aliases: list[str] = []
    for raw in (getattr(skill, "name", ""), getattr(skill, "display_name", "")):
        token = normalize_command_name(raw)
        if token:
            aliases.append("/" + token)
        if "_" in str(raw or ""):
            aliases.append("/" + str(raw).strip().lower())
    seen: set[str] = set()
    result: list[str] = []
    for alias in aliases:
        normalized = normalize_slash(alias)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _generated_skill_commands(reserved: set[str]) -> list[SlashCommandSpec]:
    try:
        import row_bot.skills as skills

        if not skills.skills_loaded():
            skills.load_skills()
        manual = [
            skill for skill in skills.get_manual_skills()
            if skills.is_enabled(skill.name) and not skills.is_tool_guide(skill)
        ]
    except Exception:
        return []

    specs: list[SlashCommandSpec] = []
    used = set(reserved)
    for skill in manual:
        aliases = _skill_aliases(skill)
        available_aliases = tuple(alias for alias in aliases if alias not in used)
        if not available_aliases:
            continue
        primary = available_aliases[0]
        used.update(available_aliases)
        specs.append(SlashCommandSpec(
            id=f"skill:{skill.name}",
            slash=primary,
            aliases=available_aliases[1:],
            title=getattr(skill, "display_name", skill.name),
            description=getattr(skill, "description", "") or "Activate this skill for the current chat.",
            icon=getattr(skill, "icon", "*") or "*",
            category="Skills",
            argument_behavior="none",
            handler_key="activate_skill",
            skill_name=skill.name,
        ))
    return specs


def get_builtin_commands() -> list[SlashCommandSpec]:
    return list(BUILTIN_COMMANDS)


def get_command_specs(*, include_skills: bool = True) -> list[SlashCommandSpec]:
    builtins = get_builtin_commands()
    reserved = {normalize_slash(name) for spec in builtins for name in spec.all_names}
    if not include_skills:
        return builtins
    return [*builtins, *_generated_skill_commands(reserved)]


def build_lookup(*, include_skills: bool = True) -> dict[str, SlashCommandSpec]:
    lookup: dict[str, SlashCommandSpec] = {}
    for spec in get_command_specs(include_skills=include_skills):
        for name in spec.all_names:
            lookup.setdefault(normalize_slash(name), spec)
    return lookup


def resolve_command_token(token: str, *, include_skills: bool = True) -> SlashCommandSpec | None:
    return build_lookup(include_skills=include_skills).get(normalize_slash(token))


def resolve_command_text(text: str, *, include_skills: bool = True) -> tuple[SlashCommandSpec, str] | None:
    stripped = str(text or "").strip()
    if not stripped.startswith("/"):
        return None
    lookup = build_lookup(include_skills=include_skills)
    normalized_text = normalize_slash(stripped)
    for alias, spec in sorted(lookup.items(), key=lambda item: len(item[0]), reverse=True):
        if normalized_text == alias:
            return spec, ""
        if " " in alias and normalized_text.startswith(alias + " "):
            return spec, stripped[len(alias):].strip()
    parts = stripped.split(maxsplit=1)
    spec = lookup.get(normalize_slash(parts[0]))
    if spec is None:
        return None
    return spec, parts[1].strip() if len(parts) > 1 else ""


def filter_command_specs(
    specs: Iterable[SlashCommandSpec],
    query: str,
    *,
    limit: int = 12,
) -> list[SlashCommandSpec]:
    q = normalize_command_name(query)
    if not q:
        return list(specs)[:limit]

    def _subsequence_score(needle: str, haystack: str) -> int | None:
        if not needle:
            return 0
        pos = -1
        gaps = 0
        first = -1
        for char in needle:
            next_pos = haystack.find(char, pos + 1)
            if next_pos < 0:
                return None
            if first < 0:
                first = next_pos
            if pos >= 0:
                gaps += max(0, next_pos - pos - 1)
            pos = next_pos
        return first + gaps + max(0, len(haystack) - len(needle)) // 8

    scored: list[tuple[int, int, SlashCommandSpec]] = []
    for index, spec in enumerate(specs):
        fields: list[tuple[int, str]] = []
        for name_index, name in enumerate(spec.all_names):
            fields.append((0 if name_index == 0 else 2, normalize_command_name(name.lstrip("/"))))
        fields.extend(
            [
                (6, normalize_command_name(spec.title)),
                (12, normalize_command_name(spec.category)),
                (20, normalize_command_name(spec.description)),
            ]
        )
        best: int | None = None
        for weight, haystack in fields:
            if not haystack:
                continue
            score: int | None
            if haystack == q:
                score = weight
            elif haystack.startswith(q):
                score = 10 + weight + len(haystack) - len(q)
            elif q in haystack:
                score = 30 + weight + haystack.index(q)
            else:
                fuzzy = _subsequence_score(q, haystack)
                score = None if fuzzy is None else 60 + weight + fuzzy + len(haystack)
            if score is not None:
                best = score if best is None else min(best, score)
        if best is not None:
            scored.append((best, index, spec))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored[:limit]]


def argument_hint(spec: SlashCommandSpec) -> str:
    return {
        "none": "",
        "optional": "Optional argument",
        "required": "Argument required",
        "prefix": "Type details after the command",
    }.get(spec.argument_behavior, "")


def find_current_slash_token(text: str, cursor: int | None = None) -> tuple[int, int, str] | None:
    value = str(text or "")
    if cursor is None:
        cursor = len(value)
    cursor = max(0, min(int(cursor), len(value)))
    start = cursor
    while start > 0 and not value[start - 1].isspace():
        start -= 1
    end = cursor
    while end < len(value) and not value[end].isspace():
        end += 1
    token = value[start:end]
    if not token.startswith("/"):
        return None
    return start, end, token[1:]


def replace_current_slash_token(
    text: str,
    cursor: int | None = None,
    replacement: str = "",
) -> tuple[str, int]:
    value = str(text or "")
    found = find_current_slash_token(value, cursor)
    if found is None:
        return value, len(value)
    start, end, _query = found
    before = value[:start]
    after = value[end:]
    inserted = str(replacement or "")
    new_value = before + inserted + after
    return new_value, len(before) + len(inserted)


def remove_current_slash_token(text: str, cursor: int | None = None) -> tuple[str, int]:
    value, next_cursor = replace_current_slash_token(text, cursor, "")
    value = re.sub(r" {2,}", " ", value)
    return value.strip() if not value.strip() else value, min(next_cursor, len(value))


def help_text(*, include_skills: bool = True) -> str:
    grouped: dict[str, list[SlashCommandSpec]] = {}
    for spec in get_command_specs(include_skills=include_skills):
        grouped.setdefault(spec.category, []).append(spec)
    category_order = ["Chat", "Skills", "Agents", "Info", "App"]
    lines = ["Available slash commands"]
    for category in category_order:
        specs = grouped.pop(category, [])
        if not specs:
            continue
        lines.extend(["", f"**{category}**", ""])
        for spec in specs:
            label = spec.title if spec.skill_name else spec.description
            lines.append(f"- `{spec.slash}` - {label}")
    for category, specs in grouped.items():
        lines.extend(["", f"**{category}**", ""])
        for spec in specs:
            label = spec.title if spec.skill_name else spec.description
            lines.append(f"- `{spec.slash}` - {label}")
    return "\n".join(lines)


def dispatch_text_command(
    thread_id: str,
    text: str,
    *,
    enabled_tool_names: Iterable[str] | None = None,
) -> str | None:
    """Execute a slash command in a non-UI send path.

    UI-only commands return a short response instead of opening dialogs. The
    composer palette handles those commands with richer UI actions.
    """
    resolved = resolve_command_text(text, include_skills=True)
    if resolved is None:
        return None
    spec, arg = resolved
    if spec.handler_key == "activate_skill":
        from row_bot.skills_activation import pin_skill, record_accept, resolve_skill_name

        name, error = resolve_skill_name(spec.skill_name)
        if error:
            return error
        assert name is not None
        pin_skill(thread_id, name)
        record_accept(thread_id, name, source="slash")
        return f"Skill active for this chat: {name}"

    if spec.id in {"skills", "noskill"} or spec.slash in {"/skill", "/skills", "/noskill"}:
        from row_bot.skills_activation import apply_skill_command

        return apply_skill_command(
            thread_id,
            text,
            enabled_tool_names=enabled_tool_names,
        )
    if spec.id == "skill-reset":
        from row_bot.skills_activation import apply_skill_command

        return apply_skill_command(
            thread_id,
            "/skill reset",
            enabled_tool_names=enabled_tool_names,
        )
    if spec.id == "status":
        from row_bot.tools.row_bot_status_tool import _row_bot_status

        return _row_bot_status("overview")
    if spec.id == "tools":
        from row_bot.tools.row_bot_status_tool import _row_bot_status

        return _row_bot_status("tools")
    if spec.id == "profiles":
        from row_bot.agent_commands import format_agent_profiles

        return format_agent_profiles(arg)
    if spec.id == "profile":
        from row_bot.agent_commands import handle_thread_profile_command

        return handle_thread_profile_command(thread_id, arg)
    if spec.id == "agents":
        from row_bot.agent_commands import format_agents_status

        include_all = arg.strip().lower() in {"all", "global"}
        return format_agents_status(parent_thread_id=thread_id, include_all=include_all)
    if spec.id == "agent":
        from row_bot.agent_commands import (
            format_agent_spawn_started,
            format_agent_spawn_usage,
            parse_agent_spawn_text,
            spawn_agent_from_request,
        )

        request = parse_agent_spawn_text(text)
        if request is None:
            return format_agent_spawn_usage()
        try:
            run = spawn_agent_from_request(
                thread_id,
                request,
                enabled_tool_names=enabled_tool_names,
            )
        except Exception as exc:
            return f"Could not start Agent: {exc}"
        return format_agent_spawn_started(run, request)
    if spec.id == "goal":
        from row_bot.goals import handle_goal_command

        return handle_goal_command(thread_id, arg)
    if spec.id == "reasoning":
        from row_bot.models import get_current_model
        from row_bot.providers.reasoning import apply_reasoning_command
        from row_bot.providers.resolution import resolve_provider_config
        from row_bot.threads import _get_thread_model_override

        selected = _get_thread_model_override(thread_id) or get_current_model()
        try:
            resolved_model = resolve_provider_config(selected, allow_legacy_local=True)
        except Exception as exc:
            return f"Could not resolve the active model for reasoning: {exc}"
        return apply_reasoning_command(thread_id, resolved_model.selection_ref, arg)
    if spec.id == "help":
        return help_text(include_skills=True)
    if spec.id == "new":
        return "Use the command palette or New button to start a new chat in the app."
    if spec.id == "stop":
        return "Use the command palette or Stop button to stop the current generation in the app."
    if spec.id == "export":
        return "Use the command palette to export the current thread."
    return None


def with_skill_name(spec: SlashCommandSpec, skill_name: str) -> SlashCommandSpec:
    """Small test helper for collision scenarios."""
    return replace(spec, skill_name=skill_name)
