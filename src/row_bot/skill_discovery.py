"""Unified enabled-skill views and deterministic progressive skill bridges."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.capability_search import CapabilityDocument, search_capabilities
from row_bot.tools.discovery import normalize_catalog_text, render_capability_manifest


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillRecord:
    canonical_id: str
    alias: str | None
    display_name: str
    icon: str
    description: str
    tags: tuple[str, ...]
    activation: Mapping[str, Any]
    instructions: str
    source: str
    root: Path | None
    plugin_name: str = ""

    @property
    def name(self) -> str:
        return self.canonical_id

    def search_document(self) -> CapabilityDocument:
        activation_terms = tuple(
            str(item)
            for value in self.activation.values()
            for item in (value if isinstance(value, list) else [value])
            if str(item or "").strip()
        )
        aliases = (self.alias,) if self.alias else ()
        return CapabilityDocument(
            canonical_id=self.canonical_id,
            name=self.display_name,
            source=self.source,
            description=self.description,
            tags=tuple((*self.tags, *activation_terms)),
            aliases=aliases,
        )


def resolve_skill_aliases(records: Sequence[SkillRecord]) -> list[SkillRecord]:
    """Keep a plugin bare alias only when unique and not shadowed by manual id."""

    manual_ids = {
        record.canonical_id
        for record in records
        if not record.canonical_id.startswith("plugin:")
    }
    alias_counts = Counter(
        record.alias
        for record in records
        if record.canonical_id.startswith("plugin:") and record.alias
    )
    return [
        replace(
            record,
            alias=(
                record.alias
                if record.canonical_id.startswith("plugin:")
                and record.alias
                and alias_counts[record.alias] == 1
                and record.alias not in manual_ids
                else None
            ),
        )
        if record.canonical_id.startswith("plugin:")
        else record
        for record in records
    ]


def collect_enabled_skill_records(*, load_manual: bool = True) -> list[SkillRecord]:
    """Read enabled manual and enabled-plugin skills without merging stores.

    UI shells that already defer skill loading can request the current in-memory
    manual snapshot; agent/runtime callers keep the load-on-demand default.
    """

    import row_bot.skills as skills
    from row_bot.plugins import registry as plugin_registry

    records: list[SkillRecord] = []
    manual_skills = (
        skills.get_enabled_manual_skills()
        if load_manual
        else skills.get_enabled_manual_skills_snapshot()
    )
    for skill in manual_skills:
        if skills.is_tool_guide(skill):
            continue
        records.append(SkillRecord(
            canonical_id=str(skill.name),
            alias=None,
            display_name=normalize_catalog_text(str(skill.display_name)),
            icon=str(skill.icon or "✨"),
            description=normalize_catalog_text(str(skill.description or ""), description=True)[:180],
            tags=tuple(str(tag) for tag in (skill.tags or [])),
            activation=dict(skill.activation or {}),
            instructions=str(skill.instructions or ""),
            source="manual",
            root=Path(skill.path) if skill.path else None,
        ))

    for info in plugin_registry.get_enabled_plugin_skill_records():
        plugin_id = str(info.get("plugin_id") or "").strip()
        skill_name = str(info.get("name") or "").strip()
        if not plugin_id or not skill_name:
            continue
        root = info.get("root")
        records.append(SkillRecord(
            canonical_id=f"plugin:{plugin_id}:{skill_name}",
            alias=skill_name,
            display_name=normalize_catalog_text(str(info.get("display_name") or skill_name)),
            icon=str(info.get("icon") or "🔌"),
            description=normalize_catalog_text(str(info.get("description") or ""), description=True)[:180],
            tags=tuple(str(tag) for tag in (info.get("tags") or [])),
            activation=dict(info.get("activation") or {}),
            instructions=str(info.get("instructions") or ""),
            source=f"plugin:{plugin_id}",
            root=Path(root) if root else None,
            plugin_name=normalize_catalog_text(str(info.get("plugin_name") or plugin_id)),
        ))
    return resolve_skill_aliases(records)


def resolve_skill_record_name(
    records: Sequence[SkillRecord],
    name: str,
) -> tuple[SkillRecord | None, str | None]:
    """Resolve an exact canonical id or an already-vetted unique bare alias."""

    requested = str(name or "").strip()
    if not requested:
        return None, "skill name is required"
    by_id = {record.canonical_id: record for record in records}
    if requested in by_id:
        return by_id[requested], None
    aliases = [record for record in records if record.alias == requested]
    if len(aliases) == 1:
        return aliases[0], None
    if len(aliases) > 1:
        return None, "skill alias is ambiguous"
    return None, "skill is not available in this invocation"


def render_active_skills_prompt(records: Sequence[SkillRecord]) -> str:
    """Render manual and plugin records in the established ordered Skills section."""

    if not records:
        return ""
    parts = [
        "## Skills\n\n"
        "The following skills are user-configured workflows. When a user's request "
        "closely matches a skill's trigger, follow the skill's step-by-step instructions. "
        "For all other requests, use your standard judgment and the guidelines above.\n"
    ]
    for record in records:
        heading = f"{record.icon} {record.display_name}"
        if record.canonical_id.startswith("plugin:"):
            heading += f" (plugin: {record.plugin_name or record.source.removeprefix('plugin:')})"
        parts.append(f"\n### {heading}\n{record.instructions}\n")
    return "\n".join(parts)


def skill_snapshot_fingerprint(
    records: Sequence[SkillRecord],
    *,
    active_ids: Sequence[str] = (),
    policy: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "active": list(active_ids),
        "policy": dict(policy or {}),
        "records": [
            {
                "id": record.canonical_id,
                "alias": record.alias,
                "display_name": record.display_name,
                "icon": record.icon,
                "description": record.description,
                "tags": list(record.tags),
                "activation": dict(record.activation),
                "instructions": record.instructions,
                "source": record.source,
                "root": str(record.root) if record.root else "",
                "plugin_name": record.plugin_name,
            }
            for record in records
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_skill_reference(root: Path | None, relative_path: str) -> str:
    """Read one UTF-8 reference while preventing traversal and symlink escapes."""

    requested = str(relative_path or "")
    path = Path(requested)
    if not requested.strip() or path.is_absolute():
        raise ValueError("reference path must be a non-empty relative path")
    if root is None:
        raise ValueError("skill does not provide a reference root")
    try:
        resolved_root = Path(root).resolve(strict=True)
        resolved_path = (resolved_root / path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("reference was not found") from exc
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError("reference must stay inside the skill root")
    if not resolved_path.is_file():
        raise ValueError("reference must be a regular file")
    data = resolved_path.read_bytes()
    if any(byte < 9 or 13 < byte < 32 for byte in data):
        raise ValueError("binary references are not supported")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("reference must contain valid UTF-8 text") from exc


class _SkillSearchInput(BaseModel):
    query: str = Field(description="Workflow or expertise needed. Must not be empty.")
    limit: int = Field(default=3, description="Number of results, clamped from 1 through 5.")


class _SkillLoadInput(BaseModel):
    name: str = Field(description="Exact canonical id or unique alias returned by skill_search.")
    relative_path: str | None = Field(default=None, description="Optional reference file within the skill root.")


def _json_result(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def build_skill_discovery_tools(
    records: Sequence[SkillRecord],
    *,
    thread_id: str,
    context_tokens: int,
    active_skill_ids: Sequence[str] = (),
    child_boundaries: bool = False,
) -> tuple[StructuredTool, StructuredTool]:
    """Build search/load tools over one frozen authorized enabled-skill snapshot."""

    frozen_records = tuple(records)
    active = {str(skill_id) for skill_id in active_skill_ids}
    searchable_records = tuple(record for record in frozen_records if record.canonical_id not in active)
    documents = tuple(record.search_document() for record in searchable_records)
    records_by_id = {record.canonical_id: record for record in frozen_records}
    searchable_by_id = {record.canonical_id: record for record in searchable_records}
    manifest = render_capability_manifest(searchable_records, context_tokens=context_tokens)
    fallback_thread_id = str(thread_id or "").strip()[:512]

    def _search(query: str, limit: int = 3) -> str:
        query_text = str(query or "").strip()
        if not query_text:
            return _json_result({
                "ok": False,
                "error": {"code": "invalid_query", "message": "query must not be empty"},
            })
        matches = search_capabilities(documents, query_text, limit=limit)
        return _json_result({
            "ok": True,
            "query": query_text,
            "results": [
                {
                    "skill_id": record.canonical_id,
                    **({"alias": record.alias} if record.alias else {}),
                    "source": record.source,
                    "display_name": record.display_name,
                    "description": record.description,
                    "tags": list(record.tags),
                }
                for document in matches
                for record in (searchable_by_id[document.canonical_id],)
            ],
        })

    def _load(
        name: str,
        relative_path: str | None = None,
        config: RunnableConfig = None,
    ) -> str:
        configurable = config.get("configurable") if isinstance(config, Mapping) else None
        runtime_thread_id = (
            str(configurable.get("thread_id") or "").strip()[:512]
            if isinstance(configurable, Mapping)
            else ""
        )
        activation_thread_id = runtime_thread_id or fallback_thread_id
        record, error = resolve_skill_record_name(frozen_records, name)
        if record is None:
            return _json_result({
                "ok": False,
                "error": {"code": "unknown_skill", "message": error or "skill is unavailable"},
            })
        reference_text = None
        if relative_path is not None:
            try:
                reference_text = read_skill_reference(record.root, relative_path)
            except ValueError as exc:
                return _json_result({
                    "ok": False,
                    "skill_id": record.canonical_id,
                    "error": {"code": "invalid_reference", "message": str(exc)},
                })
        try:
            from row_bot.skills_activation import get_auto_loaded_skill_ids

            implicit_for_task = set(get_auto_loaded_skill_ids(activation_thread_id))
        except Exception:
            implicit_for_task = set()
        if record.canonical_id in active and record.canonical_id not in implicit_for_task:
            newly_active, evicted = False, None
        else:
            from row_bot.skills_activation import load_auto_skill

            try:
                newly_active, evicted = load_auto_skill(
                    activation_thread_id,
                    record.canonical_id,
                    available_ids=records_by_id,
                )
            except ValueError as exc:
                return _json_result({
                    "ok": False,
                    "skill_id": record.canonical_id,
                    "error": {"code": "skill_unavailable", "message": str(exc)},
                })
        result: dict[str, Any] = {
            "ok": True,
            "kind": "skill_loaded",
            "skill_id": record.canonical_id,
            "display_name": record.display_name[:180],
            "source": record.source[:180],
            "newly_active": newly_active,
        }
        if evicted:
            result["evicted_skill_id"] = evicted
        if reference_text is not None:
            result["reference_text"] = reference_text
        return _json_result(result)

    boundary = (
        " Skills teach workflows but cannot grant tools, change approvals, acquire a workspace, "
        "enable delegation, or bypass the child Agent's profile."
        if child_boundaries
        else " Skills teach workflows and never grant tools or relax runtime policy."
    )
    search_description = (
        "Search enabled manual and plugin skills. Results contain bounded metadata, not instructions."
        + boundary
    )
    if manifest:
        search_description += "\nEnabled skill catalog:\n" + manifest
    return (
        StructuredTool.from_function(
            func=_search,
            name="skill_search",
            description=search_description,
            args_schema=_SkillSearchInput,
        ),
        StructuredTool.from_function(
            func=_load,
            name="skill_load",
            description=(
                "Activate one exact skill for this task, optionally reading a UTF-8 reference inside its root. "
                "The full skill body is injected on the next model pass." + boundary
            ),
            args_schema=_SkillLoadInput,
        ),
    )
