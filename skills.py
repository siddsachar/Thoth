"""Skills engine — load, cache, and build prompt text for user-configured skills.

Skills are SKILL.md files with YAML frontmatter that teach the agent
step-by-step workflows using existing tools.  They are injected into the
system prompt via the pre-model hook and carry zero runtime cost beyond
the additional tokens.

Storage layout
--------------
Bundled (read-only):  <app_root>/bundled_skills/<name>/SKILL.md
User (read-write):    ~/.thoth/skills/<name>/SKILL.md

User skills with the same ``name`` override bundled skills.
"""

import json
import logging
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
DATA_DIR.mkdir(parents=True, exist_ok=True)

USER_SKILLS_DIR = DATA_DIR / "skills"
USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

BUNDLED_SKILLS_DIR = pathlib.Path(__file__).resolve().parent / "bundled_skills"
TOOL_GUIDES_DIR = pathlib.Path(__file__).resolve().parent / "tool_guides"

CONFIG_PATH = DATA_DIR / "skills_config.json"

# ── Data Model ───────────────────────────────────────────────────────────────


@dataclass
class Skill:
    """Parsed representation of a single SKILL.md file."""

    name: str  # unique identifier (snake_case)
    display_name: str  # shown in UI
    icon: str  # emoji
    description: str  # one-line
    instructions: str  # body text injected into prompt
    tools: list[str] = field(default_factory=list)
    version: str = "1.0"
    tags: list[str] = field(default_factory=list)
    author: str = "User"
    enabled_by_default: bool = False
    source: str = "user"  # "bundled" or "user"
    path: Optional[pathlib.Path] = None  # folder containing SKILL.md


# ── Parser ───────────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_skill_md(filepath: pathlib.Path, source: str = "user") -> Optional[Skill]:
    """Parse a SKILL.md file into a Skill dataclass.  Returns None on error."""
    try:
        text = filepath.read_text(encoding="utf-8")
    except OSError:
        logger.warning("Cannot read skill file %s", filepath, exc_info=True)
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        logger.warning("No YAML frontmatter found in %s", filepath)
        return None

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        logger.warning("Invalid YAML frontmatter in %s", filepath, exc_info=True)
        return None

    if not isinstance(meta, dict):
        logger.warning("Frontmatter is not a mapping in %s", filepath)
        return None

    name = meta.get("name")
    if not name:
        logger.warning("Skill missing 'name' in %s", filepath)
        return None

    instructions = text[match.end():].strip()
    if not instructions:
        logger.warning("Skill '%s' has empty instructions body in %s", name, filepath)
        return None

    # Parse tools — accept YAML list or comma-separated string
    raw_tools = meta.get("tools", [])
    if isinstance(raw_tools, str):
        raw_tools = [t.strip() for t in raw_tools.split(",") if t.strip()]

    # Parse tags — accept YAML list or comma-separated string
    raw_tags = meta.get("tags", [])
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    return Skill(
        name=str(name),
        display_name=str(meta.get("display_name", name.replace("_", " ").title())),
        icon=str(meta.get("icon", "✨")),
        description=str(meta.get("description", "")),
        instructions=instructions,
        tools=raw_tools,
        version=str(meta.get("version", "1.0")),
        tags=raw_tags,
        author=str(meta.get("author", "Thoth" if source == "bundled" else "User")),
        enabled_by_default=bool(meta.get("enabled_by_default", False)),
        source=source,
        path=filepath.parent,
    )


# ── Config (enable/disable state) ───────────────────────────────────────────

_enabled: dict[str, bool] = {}  # name → enabled


def _load_config() -> dict:
    """Load persisted skills config from disk."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Failed to load skills config from %s", CONFIG_PATH, exc_info=True
            )
            return {}
    return {}


def _save_config():
    """Persist the current enabled state to disk."""
    data = _load_config()
    data["skills"] = _enabled
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── In-Memory Cache ─────────────────────────────────────────────────────────

_skills_cache: dict[str, Skill] = {}  # name → Skill


def _discover_skills() -> dict[str, Skill]:
    """Scan bundled + tool-guide + user skill folders.  User skills override bundled."""
    found: dict[str, Skill] = {}

    # 1. Bundled skills (lowest precedence)
    for base_dir in (BUNDLED_SKILLS_DIR, TOOL_GUIDES_DIR):
        if base_dir.is_dir():
            for child in sorted(base_dir.iterdir()):
                md = child / "SKILL.md" if child.is_dir() else None
                if md and md.exists():
                    skill = _parse_skill_md(md, source="bundled")
                    if skill:
                        found[skill.name] = skill

    # 2. User skills (highest precedence — override bundled by name)
    if USER_SKILLS_DIR.is_dir():
        for child in sorted(USER_SKILLS_DIR.iterdir()):
            md = child / "SKILL.md" if child.is_dir() else None
            if md and md.exists():
                skill = _parse_skill_md(md, source="user")
                if skill:
                    found[skill.name] = skill

    return found


def load_skills():
    """Discover all skills, apply persisted enable/disable state, populate cache."""
    global _skills_cache, _enabled

    _skills_cache = _discover_skills()

    saved = _load_config().get("skills", {})

    # Merge saved state with discovered skills
    new_enabled: dict[str, bool] = {}
    for name, skill in _skills_cache.items():
        if name in saved:
            new_enabled[name] = saved[name]
        else:
            new_enabled[name] = skill.enabled_by_default

    _enabled = new_enabled
    _save_config()

    logger.info(
        "Loaded %d skills (%d enabled)",
        len(_skills_cache),
        sum(1 for v in _enabled.values() if v),
    )


# ── Public API ───────────────────────────────────────────────────────────────


def get_all_skills() -> list[Skill]:
    """Return all discovered skills sorted by display_name."""
    return sorted(_skills_cache.values(), key=lambda s: s.display_name)


def get_skill(name: str) -> Optional[Skill]:
    """Return a skill by name, or None."""
    return _skills_cache.get(name)


def is_tool_guide(skill: Skill) -> bool:
    """True if the skill is a tool guide (has non-empty tools list)."""
    return bool(skill.tools)


def _is_tool_guide_active(skill: Skill) -> bool:
    """True if a tool guide's linked tools are enabled in the tool registry."""
    if not skill.tools:
        return False
    try:
        from tools import registry
        return any(registry.is_enabled(t) for t in skill.tools)
    except Exception:
        return False


def is_enabled(name: str) -> bool:
    """Check if a skill is enabled."""
    return _enabled.get(name, False)


def set_enabled(name: str, value: bool):
    """Enable or disable a skill and persist."""
    _enabled[name] = value
    _save_config()
    logger.info("Skill '%s' %s", name, "enabled" if value else "disabled")


def get_enabled_skills() -> list[Skill]:
    """Return manually-enabled skills + auto-activated tool guides."""
    result = []
    for s in get_all_skills():
        if is_tool_guide(s):
            if _is_tool_guide_active(s):
                result.append(s)
        else:
            if _enabled.get(s.name, False):
                result.append(s)
    return result


def get_manual_skills() -> list[Skill]:
    """Return only non-tool-guide skills (for the UI Skills tab)."""
    return [s for s in get_all_skills() if not is_tool_guide(s)]


def _ensure_skills_loaded():
    """Populate the in-memory cache on first read for status/reporting paths."""
    if not _skills_cache:
        load_skills()


def get_manual_skill_statuses() -> list[tuple[Skill, bool]]:
    """Return non-tool-guide skills with their persisted enabled state."""
    _ensure_skills_loaded()
    return [(skill, _enabled.get(skill.name, False)) for skill in get_manual_skills()]


def get_enabled_manual_skills() -> list[Skill]:
    """Return only enabled non-tool-guide skills."""
    return [skill for skill, enabled in get_manual_skill_statuses() if enabled]


def get_enabled_skill_names() -> list[str]:
    """Return names of all active skills (manual + auto tool guides)."""
    return [s.name for s in get_enabled_skills()]


def get_skills_prompt(skill_names: Optional[list[str]] = None) -> str:
    """Build the skills SystemMessage text for injection.

    Parameters
    ----------
    skill_names
        Specific skill names to include.  ``None`` means all globally-enabled
        skills.  An empty list means no skills.

    Returns an empty string when there are no skills to inject.
    """
    # Tool guides are ALWAYS injected based on which tools are enabled —
    # they cannot be toggled off via skill overrides.
    guides = [s for s in get_all_skills() if is_tool_guide(s) and _is_tool_guide_active(s)]

    if skill_names is not None:
        manual = [_skills_cache[n] for n in skill_names
                  if n in _skills_cache and not is_tool_guide(_skills_cache[n])]
    else:
        manual = [s for s in get_enabled_skills() if not is_tool_guide(s)]

    if not guides and not manual:
        return ""

    parts: list[str] = []

    # Tool guides are injected as plain tool guidance (no "Skills" header)
    if guides:
        for skill in guides:
            parts.append(f"{skill.instructions}\n")

    # Manual skills get the existing Skills header
    if manual:
        parts.append(
            "## Skills\n\n"
            "The following skills are user-configured workflows. When a user's "
            "request closely matches a skill's trigger, follow the skill's "
            "step-by-step instructions. For all other requests, use your standard "
            "judgment and the guidelines above.\n"
        )
        for skill in manual:
            parts.append(f"\n### {skill.icon} {skill.display_name}\n{skill.instructions}\n")

    return "\n".join(parts)


def estimate_tokens(skill_names: Optional[list[str]] = None) -> int:
    """Rough token estimate for the skills prompt (~4 chars per token)."""
    text = get_skills_prompt(skill_names)
    return len(text) // 4 if text else 0


def estimate_text_tokens(text: str) -> int:
    """Rough token estimate for arbitrary skill text (~4 chars per token)."""
    return len(text or "") // 4


def estimate_skill_tokens(name: str) -> int:
    """Rough token estimate for one skill's own instructions.

    This intentionally excludes auto-active tool guides and shared prompt
    wrapper text. Use ``estimate_tokens`` when estimating the complete injected
    skills prompt for an enabled skill set.
    """
    _ensure_skills_loaded()
    skill = _skills_cache.get(name)
    if not skill:
        return 0
    return estimate_text_tokens(skill.instructions)


# ── Skill CRUD ───────────────────────────────────────────────────────────────


def _build_ordered_frontmatter(meta: dict) -> str:
    """Serialize skill metadata as YAML with canonical field order."""
    _FIELD_ORDER = [
        "name", "display_name", "icon", "description",
        "enabled_by_default", "version", "tools", "tags", "author",
    ]
    lines: list[str] = []
    for key in _FIELD_ORDER:
        if key not in meta:
            continue
        val = meta[key]
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        elif isinstance(val, bool):
            lines.append(f"{key}: {'true' if val else 'false'}")
        elif isinstance(val, str) and ('\n' in val or ':' in val or '"' in val):
            escaped = val.replace('"', '\\"')
            lines.append(f'{key}: "{escaped}"')
        else:
            lines.append(f"{key}: {val}")
    # Include any extra keys not in _FIELD_ORDER
    for key in meta:
        if key not in _FIELD_ORDER:
            val = meta[key]
            lines.append(f"{key}: {val}")
    return "\n".join(lines) + "\n"


def create_skill(
    name: str,
    display_name: str,
    icon: str,
    description: str,
    instructions: str,
    tools: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    enabled: bool = True,
    version: str = "1.0",
) -> Skill:
    """Create a new user skill on disk and register it in the cache."""
    skill_dir = USER_SKILLS_DIR / name.replace(" ", "-").lower()
    md_path = skill_dir / "SKILL.md"

    if get_skill(name) is not None:
        raise ValueError(f"Skill already exists: {name}")
    if md_path.exists():
        raise ValueError(f"Skill directory already contains SKILL.md: {skill_dir.name}")

    skill_dir.mkdir(parents=True, exist_ok=True)

    # Build SKILL.md content
    meta = {
        "name": name,
        "display_name": display_name,
        "icon": icon,
        "description": description,
        "version": version,
        "author": "User",
        "enabled_by_default": enabled,
    }
    if tools:
        meta["tools"] = tools
    if tags:
        meta["tags"] = tags

    frontmatter = _build_ordered_frontmatter(meta)
    content = f"---\n{frontmatter}---\n\n{instructions}\n"

    md_path.write_text(content, encoding="utf-8")

    # Re-parse to get a clean Skill object
    skill = _parse_skill_md(md_path, source="user")
    if skill:
        _skills_cache[skill.name] = skill
        _enabled[skill.name] = enabled
        _save_config()
        logger.info("Created skill '%s' at %s", name, skill_dir)

    return skill


def update_skill(
    name: str,
    display_name: Optional[str] = None,
    icon: Optional[str] = None,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    tools: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
) -> Optional[Skill]:
    """Update an existing user skill.  Returns the updated Skill or None."""
    skill = _skills_cache.get(name)
    if not skill or not skill.path:
        logger.warning("Cannot update skill '%s': not found", name)
        return None

    # Build updated metadata
    meta = {
        "name": skill.name,
        "display_name": display_name if display_name is not None else skill.display_name,
        "icon": icon if icon is not None else skill.icon,
        "description": description if description is not None else skill.description,
        "version": skill.version,
        "author": skill.author,
        "enabled_by_default": skill.enabled_by_default,
    }
    new_tools = tools if tools is not None else skill.tools
    if new_tools:
        meta["tools"] = new_tools
    new_tags = tags if tags is not None else skill.tags
    if new_tags:
        meta["tags"] = new_tags

    new_instructions = instructions if instructions is not None else skill.instructions

    frontmatter = _build_ordered_frontmatter(meta)
    content = f"---\n{frontmatter}---\n\n{new_instructions}\n"

    md_path = skill.path / "SKILL.md"
    md_path.write_text(content, encoding="utf-8")

    # Re-parse to refresh cache
    updated = _parse_skill_md(md_path, source=skill.source)
    if updated:
        _skills_cache[updated.name] = updated
        logger.info("Updated skill '%s'", name)
    return updated


def delete_skill(name: str) -> bool:
    """Delete a user skill from disk and cache.  Returns True on success."""
    skill = _skills_cache.get(name)
    if not skill or skill.source != "user" or not skill.path:
        logger.warning("Cannot delete skill '%s': not a user skill", name)
        return False

    import shutil

    try:
        shutil.rmtree(skill.path)
    except OSError:
        logger.warning("Failed to delete skill folder %s", skill.path, exc_info=True)
        return False

    _skills_cache.pop(name, None)
    _enabled.pop(name, None)
    _save_config()
    logger.info("Deleted skill '%s'", name)
    return True


def duplicate_skill(name: str, new_name: Optional[str] = None) -> Optional[Skill]:
    """Duplicate a skill (typically bundled) into the user skills folder."""
    original = _skills_cache.get(name)
    if not original:
        return None

    dup_name = new_name or f"{original.name}_custom"
    dup_display = f"{original.display_name} (Custom)"

    return create_skill(
        name=dup_name,
        display_name=dup_display,
        icon=original.icon,
        description=original.description,
        instructions=original.instructions,
        tools=list(original.tools) if original.tools else None,
        tags=list(original.tags),
        enabled=True,
    )
