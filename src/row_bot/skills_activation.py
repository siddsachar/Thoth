"""Chat-level Smart Skills activation state and deterministic suggestions.

This module intentionally does not know how to inject tool guides. Tool guides
remain owned by :mod:`skills` and are activated from enabled tools there.
"""

from __future__ import annotations

import json
import logging
import math
import os
import pathlib
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from row_bot.data_paths import get_row_bot_data_dir

logger = logging.getLogger(__name__)

DATA_DIR = get_row_bot_data_dir()
STATE_PATH = DATA_DIR / "skills_activation.json"
MAX_TRACES = 200
INSTRUCTION_SEARCH_CHAR_LIMIT = 3000
HEADING_SEARCH_LIMIT = 30
TERM_MAP_LIMIT = 240
SPECIFICITY_CAP = 2.8
SUGGESTION_MIN_SCORE = 6.0
CHOICE_MIN_SCORE = 3.0
MAX_AUTO_LOADED_SKILLS = 5
_state_lock = threading.RLock()
ADDITIONAL_SUGGESTION_MIN_RATIO = 0.85
_STOPWORDS = {
    "about", "actually", "after", "all", "and", "are", "around", "but",
    "can", "decent", "every", "find", "for", "from", "give", "going", "has",
    "have", "into", "less", "look", "looking", "make", "more", "need", "not",
    "now", "obvious", "one", "page", "please", "real", "right", "should",
    "that", "the", "then", "these", "this", "those", "under", "understand",
    "use", "using", "want", "wants", "what", "when", "whether", "will",
    "with", "you", "your",
}
_GENERIC_QUERY_TOKENS = {
    "anything", "concept", "explain", "hello", "help", "question", "thing",
    "stuff",
}
_TERM_SOURCE_PRIORITY = {
    "activation": 6,
    "name": 5,
    "tag": 4,
    "description": 3,
    "heading": 2,
    "instructions": 1,
}


@dataclass(frozen=True)
class SkillCommand:
    action: str
    name: str = ""


@dataclass(frozen=True)
class SuggestedSkill:
    name: str
    display_name: str
    icon: str
    description: str
    reason: str
    score: float


@dataclass(frozen=True)
class SkillSearchProfile:
    skill_name: str
    weighted_terms: dict[str, float]
    term_sources: dict[str, str]
    phrases: tuple[tuple[str, str, float, str], ...]
    negative_phrases: tuple[str, ...]
    name_terms: tuple[str, ...]


@dataclass(frozen=True)
class SkillSearchCorpus:
    profiles_by_name: dict[str, SkillSearchProfile]
    specificity: dict[str, float]


@dataclass(frozen=True)
class SkillChoice:
    name: str
    display_name: str
    slug: str
    description: str
    active: bool = False
    disabled_here: bool = False


@dataclass(frozen=True)
class SkillCommandResult:
    kind: str
    text: str
    choices: tuple[SkillChoice, ...] = ()
    selected_skill: str = ""
    query: str = ""


@dataclass(frozen=True)
class SkillActivationSnapshot:
    thread_id: str
    active: list[str]
    pinned: list[str]
    disabled: list[str]
    dismissed: list[str]
    smart_off: bool
    suggestions: list[SuggestedSkill]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_store() -> dict:
    return {
        "version": 1,
        "threads": {},
        "telemetry": {"skills": {}, "traces": []},
    }


def _load_store() -> dict:
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("threads", {})
                data.setdefault("telemetry", {})
                data["telemetry"].setdefault("skills", {})
                data["telemetry"].setdefault("traces", [])
                return data
    except Exception:
        logger.debug("Failed to load Smart Skills activation state", exc_info=True)
    return _default_store()


def _save_store(store: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".skills_activation.", suffix=".json", dir=str(DATA_DIR)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_name, STATE_PATH)
    except Exception:
        logger.debug("Failed to save Smart Skills activation state", exc_info=True)


def _thread_state(store: dict, thread_id: str) -> dict:
    threads = store.setdefault("threads", {})
    state = threads.setdefault(str(thread_id or "default"), {})
    state.setdefault("pinned", [])
    state.setdefault("disabled", [])
    state.setdefault("dismissed", [])
    state.setdefault("auto_loaded", [])
    state.setdefault("smart_off", False)
    return state


def _ordered_unique(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in names:
        name = str(raw or "").strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def _normalize(text: str) -> str:
    split = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", split)
    return re.sub(r"[^a-z0-9_./ -]+", " ", split.lower()).strip()


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower())
    return re.sub(r"-+", "-", value).strip("-")


def _singularize_token(token: str) -> str:
    tok = str(token or "")
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 4 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def _token_list(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.split(r"[\s_\-/.]+", _normalize(text)):
        tok = _singularize_token(raw.strip())
        if len(tok) >= 3 and tok not in _STOPWORDS:
            tokens.append(tok)
    return tokens


def _tokens(text: str) -> set[str]:
    return set(_token_list(text))


def _is_generic_query(query_tokens: set[str]) -> bool:
    return bool(query_tokens) and query_tokens.issubset(_GENERIC_QUERY_TOKENS)


def _phrase_matches(query_norm: str, phrase: str) -> bool:
    phrase_norm = _normalize(phrase)
    return bool(phrase_norm and phrase_norm in query_norm)


def _markdown_headings(text: str) -> list[str]:
    headings: list[str] = []
    for match in re.finditer(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", str(text or ""), re.MULTILINE):
        heading = re.sub(r"[`*_]+", "", match.group(1)).strip()
        if heading:
            headings.append(heading)
        if len(headings) >= HEADING_SEARCH_LIMIT:
            break
    return headings


def _all_manual_skills() -> list:
    import row_bot.skills as skills

    if not skills.skills_loaded():
        skills.load_skills()
    return [skill for skill in skills.get_manual_skills() if not skills.is_tool_guide(skill)]


def _available_manual_skills() -> list:
    import row_bot.skills as skills

    return [skill for skill in _all_manual_skills() if skills.is_enabled(skill.name)]


def _add_weighted_terms(
    weighted_terms: dict[str, float],
    term_sources: dict[str, str],
    text: str,
    *,
    weight: float,
    source: str,
) -> None:
    for term in _token_list(text):
        if term not in weighted_terms and len(weighted_terms) >= TERM_MAP_LIMIT:
            continue
        weighted_terms[term] = min(10.0, weighted_terms.get(term, 0.0) + weight)
        old_source = term_sources.get(term, "")
        if _TERM_SOURCE_PRIORITY.get(source, 0) >= _TERM_SOURCE_PRIORITY.get(old_source, 0):
            term_sources[term] = source


def _add_phrase(
    phrases: list[tuple[str, str, float, str]],
    text: str,
    *,
    weight: float,
    reason: str,
) -> None:
    raw = str(text or "").strip()
    norm = _normalize(raw)
    if norm:
        phrases.append((raw, norm, weight, reason))


def _build_skill_search_profile(skill) -> SkillSearchProfile:
    activation = getattr(skill, "activation", {}) or {}
    weighted_terms: dict[str, float] = {}
    term_sources: dict[str, str] = {}
    phrases: list[tuple[str, str, float, str]] = []

    name_text = " ".join([
        getattr(skill, "name", "") or "",
        getattr(skill, "display_name", "") or "",
        _slug(getattr(skill, "name", "") or "").replace("-", " "),
        _slug(getattr(skill, "display_name", "") or "").replace("-", " "),
    ])
    _add_weighted_terms(weighted_terms, term_sources, name_text, weight=4.5, source="name")
    _add_phrase(phrases, getattr(skill, "name", ""), weight=6.0, reason="name")
    _add_phrase(phrases, getattr(skill, "display_name", ""), weight=6.0, reason="name")

    description = getattr(skill, "description", "") or ""
    _add_weighted_terms(weighted_terms, term_sources, description, weight=2.4, source="description")
    if 3 <= len(_token_list(description)) <= 12:
        _add_phrase(phrases, description, weight=4.0, reason="description")

    _add_weighted_terms(
        weighted_terms,
        term_sources,
        " ".join(getattr(skill, "tags", []) or []),
        weight=3.2,
        source="tag",
    )

    for phrase in activation.get("phrases", []):
        _add_weighted_terms(weighted_terms, term_sources, phrase, weight=5.5, source="activation")
        _add_phrase(phrases, phrase, weight=10.0, reason="activation phrase")
    for keyword in activation.get("keywords", []):
        _add_weighted_terms(weighted_terms, term_sources, keyword, weight=5.0, source="activation")
        if len(_token_list(keyword)) > 1:
            _add_phrase(phrases, keyword, weight=7.0, reason="activation keyword")
    for example in activation.get("examples", []):
        _add_weighted_terms(weighted_terms, term_sources, example, weight=3.5, source="activation")
        _add_phrase(phrases, example, weight=7.0, reason="activation example")

    instructions = getattr(skill, "instructions", "") or ""
    for heading in _markdown_headings(instructions):
        _add_weighted_terms(weighted_terms, term_sources, heading, weight=1.8, source="heading")
        _add_phrase(phrases, heading, weight=3.0, reason="instruction heading")
    _add_weighted_terms(
        weighted_terms,
        term_sources,
        instructions[:INSTRUCTION_SEARCH_CHAR_LIMIT],
        weight=0.55,
        source="instructions",
    )

    return SkillSearchProfile(
        skill_name=getattr(skill, "name", ""),
        weighted_terms=weighted_terms,
        term_sources=term_sources,
        phrases=tuple(phrases),
        negative_phrases=tuple(activation.get("negative_phrases", []) or ()),
        name_terms=tuple(_tokens(name_text)),
    )


def _build_skill_search_corpus(skill_list: Iterable) -> SkillSearchCorpus:
    profiles: dict[str, SkillSearchProfile] = {}
    document_frequency: dict[str, int] = {}
    for skill in skill_list:
        profile = _build_skill_search_profile(skill)
        profiles[profile.skill_name] = profile
        for term in profile.weighted_terms:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    skill_count = max(1, len(profiles))
    specificity = {
        term: min(SPECIFICITY_CAP, 1.0 + math.log((skill_count + 1) / (df + 1)))
        for term, df in document_frequency.items()
    }
    return SkillSearchCorpus(profiles_by_name=profiles, specificity=specificity)


def _choice_match_fields(skill) -> list[str]:
    values = [
        skill.name,
        getattr(skill, "display_name", ""),
        _slug(skill.name),
        _slug(getattr(skill, "display_name", "")),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = _normalize(value).replace("-", " ")
        compact = normalized.replace(" ", "").replace("_", "")
        for item in (normalized, compact):
            if item and item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _skill_choice(skill, *, active: set[str] | None = None, disabled: set[str] | None = None) -> SkillChoice:
    return SkillChoice(
        name=skill.name,
        display_name=getattr(skill, "display_name", skill.name),
        slug=_slug(getattr(skill, "display_name", "") or skill.name) or _slug(skill.name),
        description=getattr(skill, "description", "") or "",
        active=skill.name in (active or set()),
        disabled_here=skill.name in (disabled or set()),
    )


def list_skill_choices(thread_id: str = "", *, query: str = "", limit: int | None = None) -> list[SkillChoice]:
    """Return enabled manual runtime skills for channel/app command pickers."""
    query_norm = _normalize(query).replace("-", " ")
    query_compact = query_norm.replace(" ", "").replace("_", "")
    query_tokens = _tokens(query)
    store = _load_store()
    state = _thread_state(store, thread_id or "default")
    active = set(resolve_active_skill_names(thread_id or "default"))
    disabled = set(state.get("disabled", []))
    available_skills = _available_manual_skills()
    corpus = _build_skill_search_corpus(available_skills)
    ranked: list[tuple[int, float, str, SkillChoice]] = []
    for skill in available_skills:
        fields = _choice_match_fields(skill)
        if not query_norm:
            rank = 0
            score = 0.0
        elif any(query_norm == field or query_compact == field for field in fields):
            rank = 0
            score = 100.0
        elif any(field.startswith(query_norm) or field.startswith(query_compact) for field in fields):
            rank = 1
            score = 80.0
        else:
            score, _reason = _skill_score(skill, query_norm, query_tokens, store.get("telemetry", {}), corpus)
            if score >= CHOICE_MIN_SCORE:
                rank = 2
            elif any(query_norm in field or query_compact in field for field in fields):
                rank = 3
                score = 20.0
            else:
                continue
        choice = _skill_choice(skill, active=active, disabled=disabled)
        ranked.append((rank, -score, choice.display_name.lower(), choice))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    if query_norm and ranked and ranked[0][0] <= 1:
        ranked = [item for item in ranked if item[0] == ranked[0][0]]
    choices = [item[3] for item in ranked]
    return choices if limit is None else choices[:limit]


def match_skill_choices(raw_name: str, *, thread_id: str = "", limit: int | None = None) -> list[SkillChoice]:
    """Return deterministic skill matches for a user-provided skill query."""
    return list_skill_choices(thread_id, query=raw_name, limit=limit)


def _format_choice_line(index: int, choice: SkillChoice) -> str:
    markers: list[str] = []
    if choice.active:
        markers.append("active")
    if choice.disabled_here:
        markers.append("disabled here")
    suffix = f" ({', '.join(markers)})" if markers else ""
    description = f" - {choice.description}" if choice.description else ""
    return f"{index}. {choice.display_name}{suffix}{description} - /skill {choice.slug}"


def _format_choices(
    heading: str,
    choices: Iterable[SkillChoice],
    *,
    empty: str = "No matching skills.",
) -> str:
    choice_list = list(choices)
    if not choice_list:
        return empty
    lines = [heading]
    lines.extend(_format_choice_line(index, choice) for index, choice in enumerate(choice_list, 1))
    return "\n".join(lines)


def apply_channel_skill_command(
    thread_id: str,
    text: str,
    *,
    current_text: str = "",
    enabled_tool_names: Iterable[str] | None = None,
    choice_limit: int = 10,
) -> SkillCommandResult | None:
    """Apply a Smart Skills command and return a channel-renderable result."""
    command = parse_skill_command(text)
    if command is None:
        return None
    if command.action == "list":
        snap = get_activation_snapshot(
            thread_id,
            current_text=current_text,
            enabled_tool_names=enabled_tool_names,
        )
        choices = tuple(list_skill_choices(thread_id, query=command.name, limit=choice_limit))
        lines = ["Skills for this chat:"]
        lines.append("Suggestions: off" if snap.smart_off else "Suggestions: on")
        lines.append("Active: " + (", ".join(snap.active) if snap.active else "none"))
        if snap.disabled:
            lines.append("Disabled here: " + ", ".join(snap.disabled))
        if snap.suggestions and not command.name:
            lines.append(
                "Suggested: "
                + ", ".join(f"{s.display_name} ({s.reason})" for s in snap.suggestions)
            )
        if choices:
            title = "Matching skills:" if command.name else "Available skills:"
            lines.append("")
            lines.append(title)
            lines.extend(_format_choice_line(index, choice) for index, choice in enumerate(choices, 1))
            if len(choices) >= choice_limit:
                lines.append(f"Showing first {choice_limit}. Use /skills <query> to filter.")
        elif command.name:
            lines.append("")
            lines.append(f"No skills match: {command.name}")
        return SkillCommandResult(
            "list" if choices else "text",
            "\n".join(lines),
            choices=choices,
            query=command.name,
        )
    if command.action == "off":
        set_smart_off(thread_id, True)
        return SkillCommandResult(
            "text",
            "Skill suggestions are off for this chat. Active skills stay active until removed.",
        )
    if command.action == "reset":
        reset_thread(thread_id)
        return SkillCommandResult("reset", "Skills reset for this chat.")
    if command.action == "unsupported_once":
        return SkillCommandResult(
            "error",
            "Temporary skill activation is not supported. Use /skill <name> to activate a skill for this chat, then remove it when done.",
        )
    if command.action == "disable" and not command.name:
        active = resolve_active_skill_names(thread_id)
        if len(active) == 1:
            disable_skill(thread_id, active[0])
            return SkillCommandResult(
                "disabled",
                f"Skill disabled for this chat: {active[0]}",
                selected_skill=active[0],
            )
        if len(active) > 1:
            active_set = set(active)
            active_choices = [
                choice for choice in list_skill_choices(thread_id, limit=choice_limit)
                if choice.name in active_set
            ]
            text_out = _format_choices(
                "Multiple skills are active. Choose one to disable:",
                active_choices,
                empty="Multiple skills are active. Use /noskill <skill> to disable one.",
            )
            return SkillCommandResult(
                "choices",
                text_out,
                choices=tuple(active_choices),
            )
        return SkillCommandResult("text", "No active skill is set for this chat.")

    if command.action == "disable" and command.name:
        try:
            from row_bot.skill_discovery import collect_enabled_skill_records, resolve_skill_record_name

            record, _error = resolve_skill_record_name(
                collect_enabled_skill_records(),
                command.name,
            )
            if record and record.canonical_id.startswith("plugin:"):
                if remove_auto_loaded_skill(thread_id, record.canonical_id):
                    return SkillCommandResult(
                        "disabled",
                        f"Skill disabled for this chat: {record.display_name}",
                        selected_skill=record.canonical_id,
                        query=command.name,
                    )
        except Exception:
            logger.debug("Implicit plugin skill removal skipped", exc_info=True)

    _resolved_name, _resolved_error = resolve_skill_name(command.name)
    if _resolved_error and _resolved_error.startswith("Skill is off in the Skills library:"):
        return SkillCommandResult("error", _resolved_error, query=command.name)
    matches = match_skill_choices(command.name, thread_id=thread_id, limit=choice_limit)
    if not matches:
        _name, error = resolve_skill_name(command.name)
        return SkillCommandResult("error", error or f"Skill not found: {command.name}", query=command.name)
    if len(matches) > 1:
        action = "disable" if command.action == "disable" else "activate"
        text_out = _format_choices(
            f"Multiple skills match. Reply with /skill <name> to {action}:",
            matches,
        )
        return SkillCommandResult(
            "choices",
            text_out,
            choices=tuple(matches),
            query=command.name,
        )
    name = matches[0].name
    if command.action == "pin":
        pin_skill(thread_id, name)
        record_accept(thread_id, name, source="slash")
        return SkillCommandResult(
            "activated",
            f"Skill active for this chat: {name}",
            selected_skill=name,
            query=command.name,
        )
    if command.action == "disable":
        disable_skill(thread_id, name)
        return SkillCommandResult(
            "disabled",
            f"Skill disabled for this chat: {name}",
            selected_skill=name,
            query=command.name,
        )
    return None


def resolve_skill_name(raw_name: str) -> tuple[str | None, str | None]:
    """Resolve a command argument to a manual skill name.

    Returns ``(name, error)``. Matching is exact first, then case-insensitive
    name/display name, then a single substring match.
    """
    query = _normalize(raw_name)
    if not query:
        return None, "Skill name is required."
    manual = _all_manual_skills()
    available = {skill.name for skill in _available_manual_skills()}
    by_exact = {skill.name: skill.name for skill in manual}
    if raw_name in by_exact:
        if raw_name in available:
            return raw_name, None
        return None, f"Skill is off in the Skills library: {raw_name}"
    folded: dict[str, str] = {}
    for skill in manual:
        folded[_normalize(skill.name)] = skill.name
        folded[_normalize(skill.display_name)] = skill.name
        folded[_normalize(_slug(skill.name)).replace("-", " ")] = skill.name
        folded[_normalize(_slug(skill.display_name)).replace("-", " ")] = skill.name
    if query in folded:
        name = folded[query]
        if name in available:
            return name, None
        return None, f"Skill is off in the Skills library: {name}"
    choices = match_skill_choices(raw_name)
    matches = [choice.name for choice in choices]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, "Multiple skills match: " + ", ".join(matches[:6])
    unavailable_matches = [
        skill.name
        for skill in manual
        if query in _normalize(skill.name)
        or query in _normalize(skill.display_name)
        or query in _normalize(_slug(skill.name)).replace("-", " ")
        or query in _normalize(_slug(skill.display_name)).replace("-", " ")
    ]
    if unavailable_matches:
        return None, "Skill is off in the Skills library: " + ", ".join(unavailable_matches[:6])
    return None, f"Skill not found: {raw_name}"


def parse_skill_command(text: str) -> SkillCommand | None:
    text = str(text or "").strip()
    if not text.startswith("/"):
        return None
    parts = text.split(maxsplit=2)
    cmd = parts[0].lower()
    if cmd == "/skills":
        return SkillCommand("list", text.split(maxsplit=1)[1].strip() if len(parts) > 1 else "")
    if cmd == "/noskill":
        return SkillCommand("disable", text.split(maxsplit=1)[1].strip() if len(parts) > 1 else "")
    if cmd in {"/skill-reset", "/skillreset", "/skill_reset"}:
        return SkillCommand("reset")
    if cmd != "/skill":
        return None
    if len(parts) == 1:
        return SkillCommand("list")
    arg = parts[1].strip()
    lowered = arg.lower()
    if lowered == "off":
        return SkillCommand("off")
    if lowered == "reset":
        return SkillCommand("reset")
    if lowered == "once":
        return SkillCommand("unsupported_once")
    name = text.split(maxsplit=1)[1].strip()
    return SkillCommand("pin", name)


def apply_skill_command(
    thread_id: str,
    text: str,
    *,
    current_text: str = "",
    enabled_tool_names: Iterable[str] | None = None,
) -> str | None:
    result = apply_channel_skill_command(
        thread_id,
        text,
        current_text=current_text,
        enabled_tool_names=enabled_tool_names,
    )
    return result.text if result is not None else None


def pin_skill(thread_id: str, skill_name: str) -> None:
    store = _load_store()
    state = _thread_state(store, thread_id)
    state["pinned"] = _ordered_unique([*state.get("pinned", []), skill_name])
    state["disabled"] = [n for n in state.get("disabled", []) if n != skill_name]
    state["dismissed"] = [n for n in state.get("dismissed", []) if n != skill_name]
    _save_store(store)


def disable_skill(thread_id: str, skill_name: str) -> None:
    store = _load_store()
    state = _thread_state(store, thread_id)
    state["disabled"] = _ordered_unique([*state.get("disabled", []), skill_name])
    state["pinned"] = [n for n in state.get("pinned", []) if n != skill_name]
    state["auto_loaded"] = [n for n in state.get("auto_loaded", []) if n != skill_name]
    _save_store(store)


def dismiss_suggestion(thread_id: str, skill_name: str) -> None:
    store = _load_store()
    state = _thread_state(store, thread_id)
    state["dismissed"] = _ordered_unique([*state.get("dismissed", []), skill_name])
    _save_store(store)
    record_dismiss(thread_id, skill_name, source="ui")


def set_smart_off(thread_id: str, value: bool) -> None:
    store = _load_store()
    _thread_state(store, thread_id)["smart_off"] = bool(value)
    _save_store(store)


def is_smart_off(thread_id: str) -> bool:
    store = _load_store()
    return bool(_thread_state(store, thread_id).get("smart_off"))


def get_auto_loaded_skill_ids(thread_id: str) -> list[str]:
    """Return this task's implicit skills in oldest-to-newest order."""

    with _state_lock:
        store = _load_store()
        return list(_thread_state(store, thread_id).get("auto_loaded", []))


def load_auto_skill(
    thread_id: str,
    skill_id: str,
    *,
    available_ids: Iterable[str] | None = None,
    limit: int = MAX_AUTO_LOADED_SKILLS,
) -> tuple[bool, str | None]:
    """Load or touch one implicit skill and evict the oldest beyond the limit."""

    canonical_id = str(skill_id or "").strip()
    if not canonical_id:
        raise ValueError("skill id is required")
    with _state_lock:
        store = _load_store()
        state = _thread_state(store, thread_id)
        current = _ordered_unique(state.get("auto_loaded", []))
        if available_ids is not None:
            available = {str(name) for name in available_ids}
            current = [name for name in current if name in available]
            if canonical_id not in available:
                raise ValueError("skill is no longer available")
        newly_active = canonical_id not in current
        current = [name for name in current if name != canonical_id]
        current.append(canonical_id)
        evicted = None
        bounded_limit = max(1, int(limit or MAX_AUTO_LOADED_SKILLS))
        while len(current) > bounded_limit:
            evicted = current.pop(0)
        state["auto_loaded"] = current
        _save_store(store)
        return newly_active, evicted


def remove_auto_loaded_skill(thread_id: str, skill_id: str) -> bool:
    with _state_lock:
        store = _load_store()
        state = _thread_state(store, thread_id)
        before = list(state.get("auto_loaded", []))
        state["auto_loaded"] = [name for name in before if name != skill_id]
        changed = before != state["auto_loaded"]
        if changed:
            _save_store(store)
        return changed


def clear_auto_loaded_skills(thread_id: str) -> None:
    with _state_lock:
        store = _load_store()
        state = _thread_state(store, thread_id)
        if state.get("auto_loaded"):
            state["auto_loaded"] = []
            _save_store(store)


def delete_thread_activation_state(thread_id: str) -> None:
    """Remove durable skill activation data when its owning thread is deleted."""

    with _state_lock:
        store = _load_store()
        removed = store.setdefault("threads", {}).pop(str(thread_id or "default"), None)
        if removed is not None:
            _save_store(store)


def seed_thread_default_skills(
    thread_id: str,
    *,
    surface: str = "chat",
    replace: bool = False,
) -> list[str]:
    """Snapshot the current default manual skills onto a thread.

    Existing non-empty activation state is left alone unless ``replace`` is set.
    """
    import row_bot.skills as skills

    defaults = skills.get_default_active_skill_names(surface)
    store = _load_store()
    state = _thread_state(store, thread_id)
    has_state = any(
        state.get(key)
        for key in ("pinned", "disabled", "dismissed", "auto_loaded")
    ) or bool(state.get("smart_off"))
    if has_state and not replace:
        return resolve_active_skill_names(thread_id)
    state["pinned"] = _ordered_unique(defaults)
    state["disabled"] = []
    state["dismissed"] = []
    state["smart_off"] = False
    _save_store(store)
    return list(state["pinned"])


def reset_thread(thread_id: str, *, surface: str = "chat") -> None:
    store = _load_store()
    store.setdefault("threads", {}).pop(str(thread_id or "default"), None)
    _save_store(store)
    seed_thread_default_skills(thread_id, surface=surface, replace=True)


def resolve_active_skill_names(
    thread_id: str,
    *,
    explicit_override: list[str] | None = None,
    is_background: bool = False,
) -> list[str]:
    """Return manual skill names to inject for this turn."""
    manual_names = {skill.name for skill in _available_manual_skills()}
    if is_background:
        return [n for n in (explicit_override or []) if n in manual_names]
    store = _load_store()
    state = _thread_state(store, thread_id)
    disabled = set(state.get("disabled", []))
    active = _ordered_unique([
        *(explicit_override or []),
        *state.get("pinned", []),
        *state.get("auto_loaded", []),
    ])
    return [name for name in active if name in manual_names and name not in disabled]


def _skill_score(
    skill,
    query_norm: str,
    query_tokens: set[str],
    telemetry: dict,
    corpus: SkillSearchCorpus | None = None,
) -> tuple[float, str]:
    if corpus is None:
        corpus = _build_skill_search_corpus([skill])
    profile = corpus.profiles_by_name.get(skill.name) or _build_skill_search_profile(skill)
    negative_matches = [
        phrase for phrase in profile.negative_phrases
        if _phrase_matches(query_norm, phrase)
    ]
    if negative_matches:
        return -10.0, "suppressed by " + ", ".join(negative_matches[:2])

    score = 0.0
    reasons: list[str] = []
    source_hits: dict[str, list[str]] = {}
    source_specificity: dict[str, float] = {}
    source_scores: dict[str, float] = {}

    phrase_hits: list[tuple[str, str]] = []
    for raw, normalized, weight, reason in profile.phrases:
        if normalized and normalized in query_norm:
            score += weight
            phrase_hits.append((reason, raw))
    if phrase_hits:
        reason, raw = phrase_hits[0]
        reasons.append(f"matched {reason} {raw}")

    overlap = query_tokens & set(profile.weighted_terms)
    for term in overlap:
        specificity = corpus.specificity.get(term, 1.0)
        term_score = profile.weighted_terms.get(term, 0.0) * specificity
        score += term_score
        source = profile.term_sources.get(term, "instructions")
        source_hits.setdefault(source, []).append(term)
        source_specificity[source] = source_specificity.get(source, 0.0) + specificity
        source_scores[source] = source_scores.get(source, 0.0) + term_score

    prefix_term_hits: set[str] = set()
    weighted_terms = set(profile.weighted_terms)
    for query_token in query_tokens - overlap:
        if len(query_token) < 5:
            continue
        best: tuple[float, str, str, float] | None = None
        for term in weighted_terms:
            if len(term) < 5:
                continue
            if not (term.startswith(query_token) or query_token.startswith(term)):
                continue
            specificity = corpus.specificity.get(term, 1.0)
            term_score = profile.weighted_terms.get(term, 0.0) * specificity * 0.65
            if best is None or term_score > best[0]:
                best = (term_score, term, profile.term_sources.get(term, "instructions"), specificity)
        if best is None:
            continue
        term_score, term, source, specificity = best
        score += term_score
        prefix_term_hits.add(term)
        source_hits.setdefault(source, []).append(term)
        source_specificity[source] = source_specificity.get(source, 0.0) + specificity * 0.65
        source_scores[source] = source_scores.get(source, 0.0) + term_score

    prefix_hits: list[str] = []
    for query_token in query_tokens:
        if any(
            name_term.startswith(query_token)
            or (len(name_term) >= 4 and query_token.startswith(name_term))
            for name_term in profile.name_terms
        ):
            prefix_hits.append(query_token)
    if prefix_hits:
        score += min(20.0, 10.0 * len(set(prefix_hits)))
        source_hits.setdefault("name", []).extend(prefix_hits)
        source_specificity["name"] = source_specificity.get("name", 0.0) + 1.2 * len(set(prefix_hits))
        source_scores["name"] = source_scores.get("name", 0.0) + 10.0 * len(set(prefix_hits))

    if len(overlap) >= 2:
        score += min(3.0, len(overlap) * 0.45)
    if query_tokens and query_tokens.issubset(overlap):
        score += 1.0

    strong_phrase = any(
        reason in {"activation phrase", "activation keyword", "activation example", "name", "description"}
        for reason, _raw in phrase_hits
    )
    source_names = set(source_hits)
    matched_terms = overlap | prefix_term_hits
    specific_terms = {
        term for term in matched_terms
        if corpus.specificity.get(term, 1.0) >= 1.35
    }
    total_specificity = sum(corpus.specificity.get(term, 1.0) for term in matched_terms)
    desc_tag_terms = set(source_hits.get("description", [])) | set(source_hits.get("tag", []))
    heading_terms = set(source_hits.get("heading", []))
    body_terms = set(source_hits.get("instructions", []))

    name_evidence = source_specificity.get("name", 0.0) >= 1.4
    activation_terms = set(source_hits.get("activation", []))
    activation_evidence = (
        source_specificity.get("activation", 0.0) >= 2.2
        or (len(activation_terms & specific_terms) >= 2 and source_specificity.get("activation", 0.0) >= 1.4)
    )
    desc_tag_evidence = (
        len(desc_tag_terms) >= 2 and source_specificity.get("description", 0.0) + source_specificity.get("tag", 0.0) >= 2.6
    )
    heading_body_evidence = (
        bool(heading_terms)
        and len((heading_terms | body_terms | desc_tag_terms) & specific_terms) >= 2
        and total_specificity >= 3.4
    )
    body_only_evidence = (
        source_names
        and source_names.issubset({"instructions"})
        and len(body_terms & specific_terms) >= 2
        and total_specificity >= 2.75
    )
    multi_specific_evidence = len(specific_terms) >= 3 and total_specificity >= 4.2
    evidence_ok = (
        strong_phrase
        or name_evidence
        or activation_evidence
        or desc_tag_evidence
        or heading_body_evidence
        or body_only_evidence
        or multi_specific_evidence
    )
    if not evidence_ok:
        return -5.0, "insufficient specific evidence"
    if body_only_evidence:
        score += 2.2

    if not reasons and source_hits:
        best_source = max(
            source_hits,
            key=lambda source: (
                _TERM_SOURCE_PRIORITY.get(source, 0),
                len(set(source_hits[source])),
            ),
        )
        terms = ", ".join(sorted(set(source_hits[best_source]))[:3])
        reasons.append(f"matched {best_source} terms {terms}")

    skill_meta = telemetry.get("skills", {}).get(skill.name, {})
    accepted = int(skill_meta.get("accepted", 0) or 0)
    dismissed = int(skill_meta.get("dismissed", 0) or 0)
    usage_count = int(skill_meta.get("usage_count", 0) or 0)
    score += min(2.0, accepted * 0.35) + min(1.0, usage_count * 0.1)
    score -= min(3.0, dismissed * 0.5)
    if accepted or usage_count:
        reasons.append("used before")
    return score, "; ".join(reasons) if reasons else "related skill"


def suggest_skills(
    thread_id: str,
    current_text: str = "",
    *,
    enabled_tool_names: Iterable[str] | None = None,
    extra_excluded: Iterable[str] | None = None,
    limit: int = 3,
    trace: bool = True,
) -> list[SuggestedSkill]:
    store = _load_store()
    state = _thread_state(store, thread_id)
    if state.get("smart_off"):
        return []
    query_norm = _normalize(current_text)
    query_tokens = _tokens(current_text)
    if not query_tokens:
        return []
    if _is_generic_query(query_tokens):
        return []
    active = set(resolve_active_skill_names(thread_id))
    excluded = (
        active
        | set(state.get("disabled", []))
        | set(state.get("dismissed", []))
        | set(extra_excluded or [])
    )
    available_skills = _available_manual_skills()
    corpus = _build_skill_search_corpus(available_skills)
    ranked: list[SuggestedSkill] = []
    for skill in available_skills:
        if skill.name in excluded:
            continue
        score, reason = _skill_score(skill, query_norm, query_tokens, store.get("telemetry", {}), corpus)
        if score < SUGGESTION_MIN_SCORE:
            continue
        ranked.append(SuggestedSkill(
            name=skill.name,
            display_name=skill.display_name,
            icon=skill.icon,
            description=skill.description,
            reason=reason,
            score=round(score, 2),
        ))
    ranked.sort(key=lambda item: (-item.score, item.display_name.lower()))
    suggestions: list[SuggestedSkill] = []
    if ranked:
        top_score = ranked[0].score
        min_additional = max(SUGGESTION_MIN_SCORE, round(top_score * ADDITIONAL_SUGGESTION_MIN_RATIO, 2))
        for item in ranked:
            if not suggestions or item.score >= min_additional:
                suggestions.append(item)
            if len(suggestions) >= limit:
                break
    if suggestions and trace:
        record_trace(
            thread_id,
            "suggest",
            suggestions=[s.name for s in suggestions],
            query_tokens=sorted(query_tokens)[:12],
        )
    return suggestions


def get_activation_snapshot(
    thread_id: str,
    *,
    current_text: str = "",
    enabled_tool_names: Iterable[str] | None = None,
    explicit_override: list[str] | None = None,
    is_background: bool = False,
) -> SkillActivationSnapshot:
    store = _load_store()
    state = _thread_state(store, thread_id)
    return SkillActivationSnapshot(
        thread_id=str(thread_id or "default"),
        active=resolve_active_skill_names(
            thread_id,
            explicit_override=explicit_override,
            is_background=is_background,
        ),
        pinned=list(state.get("pinned", [])),
        disabled=list(state.get("disabled", [])),
        dismissed=list(state.get("dismissed", [])),
        smart_off=bool(state.get("smart_off")),
        suggestions=suggest_skills(
            thread_id,
            current_text,
            enabled_tool_names=enabled_tool_names,
            extra_excluded=explicit_override or [],
        ) if not is_background else [],
    )


def _bump_skill_metric(skill_name: str, key: str, *, last_used: bool = False) -> None:
    store = _load_store()
    skills_meta = store.setdefault("telemetry", {}).setdefault("skills", {})
    meta = skills_meta.setdefault(skill_name, {})
    meta[key] = int(meta.get(key, 0) or 0) + 1
    if last_used:
        meta["last_used"] = _now_iso()
    _save_store(store)


def record_usage(thread_id: str, skill_names: Iterable[str], *, source: str = "agent") -> None:
    names = _ordered_unique(skill_names)
    if not names:
        return
    for name in names:
        _bump_skill_metric(name, "usage_count", last_used=True)
    record_trace(thread_id, "activate", skills=names, source=source)


def record_accept(thread_id: str, skill_name: str, *, source: str = "ui") -> None:
    _bump_skill_metric(skill_name, "accepted", last_used=False)
    record_trace(thread_id, "accept", skills=[skill_name], source=source)


def record_dismiss(thread_id: str, skill_name: str, *, source: str = "ui") -> None:
    _bump_skill_metric(skill_name, "dismissed", last_used=False)
    record_trace(thread_id, "dismiss", skills=[skill_name], source=source)


def record_trace(thread_id: str, event: str, **payload) -> None:
    try:
        store = _load_store()
        traces = store.setdefault("telemetry", {}).setdefault("traces", [])
        traces.append({
            "at": _now_iso(),
            "thread_id": str(thread_id or ""),
            "event": event,
            **payload,
        })
        if len(traces) > MAX_TRACES:
            del traces[:-MAX_TRACES]
        _save_store(store)
    except Exception:
        logger.debug("Failed to record Smart Skills trace", exc_info=True)


def get_skill_telemetry() -> dict[str, dict]:
    return dict(_load_store().get("telemetry", {}).get("skills", {}))


def get_thread_activation_state(thread_id: str) -> dict:
    store = _load_store()
    return dict(_thread_state(store, thread_id))
