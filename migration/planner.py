"""Dry-run migration planners for supported source providers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from migration.core import (
    ConflictPolicy,
    MigrationAction,
    MigrationCategory,
    MigrationItem,
    MigrationPlan,
    MigrationProvider,
    MigrationSensitivity,
    MigrationSource,
    MigrationStatus,
    make_item_id,
    normalize_provider,
)
from migration.detection import MigrationScan, detect_hermes_source, detect_openclaw_source, detect_source

try:
    import yaml
except Exception:  # pragma: no cover - optional parser, planner falls back to file-only items
    yaml = None


DEFAULT_TARGET_ROOT = Path.home() / ".thoth" / "migration-preview"
_SAFE_NAME_RE = re.compile(r"[^a-z0-9_.-]+")


def build_migration_plan(
    provider: str | MigrationProvider,
    root: str | Path | None = None,
    *,
    target_root: str | Path | None = None,
    include_secrets: bool = False,
) -> MigrationPlan:
    """Build a preview-only migration plan for *provider* without mutating anything."""
    normalized = normalize_provider(provider)
    if normalized == MigrationProvider.HERMES:
        return build_hermes_plan(root, target_root=target_root, include_secrets=include_secrets)
    if normalized == MigrationProvider.OPENCLAW:
        return build_openclaw_plan(root, target_root=target_root, include_secrets=include_secrets)

    scan = detect_source(normalized, root)
    return MigrationPlan.empty(scan.source, "Unsupported migration provider; nothing was planned.")


def build_hermes_plan(
    root: str | Path | None = None,
    *,
    target_root: str | Path | None = None,
    include_secrets: bool = False,
) -> MigrationPlan:
    scan = detect_hermes_source(root)
    target = _target_root(target_root)
    if not scan.source.found:
        return _empty_for_missing(scan.source, "Hermes state was not found; nothing was planned.")

    warnings: list[str] = list(scan.source.warnings)
    config_path = scan.source.root / "config.yaml"
    config = _parse_yaml_object(config_path, warnings, "Hermes config.yaml")
    items: list[MigrationItem] = []

    model_ref = _resolve_hermes_model_ref(config)
    if model_ref:
        items.append(
            _planned_item(
                id="model:default",
                category=MigrationCategory.MODEL,
                action=MigrationAction.UPDATE,
                source=config_path,
                target=target / "config" / "models.json#default",
                label="Default model",
                details={"model": model_ref, "source_key": "config.yaml:model"},
            )
        )

    provider_ids = _hermes_provider_ids(config, model_ref)
    if provider_ids:
        items.append(
            _planned_item(
                id="settings:model-providers",
                category=MigrationCategory.SETTINGS,
                action=MigrationAction.UPDATE,
                source=config_path,
                target=target / "config" / "providers.json",
                label="Model providers",
                details={"providers": provider_ids},
                sensitivity=MigrationSensitivity.SENSITIVE,
            )
        )

    for server_name, server_config in _record_items(_child_record(config, "mcp_servers")):
        items.append(
            _disabled_mcp_item(
                provider=MigrationProvider.HERMES,
                name=server_name,
                source=config_path,
                target_root=target,
                details={"server": _redaction_safe_mcp(server_config), "enabled": False},
            )
        )

    items.extend(
        _file_items(
            scan.source.root,
            target,
            provider=MigrationProvider.HERMES,
            mappings=(
                ("SOUL.md", MigrationCategory.IDENTITY, MigrationAction.COPY, target / "identity" / "SOUL.md", "Persona"),
                ("AGENTS.md", MigrationCategory.IDENTITY, MigrationAction.COPY, target / "identity" / "AGENTS.md", "Agent instructions"),
                ("memories/MEMORY.md", MigrationCategory.MEMORIES, MigrationAction.APPEND, target / "memory" / "MEMORY.md", "Memory"),
                ("memories/USER.md", MigrationCategory.MEMORIES, MigrationAction.APPEND, target / "memory" / "USER.md", "User profile"),
            ),
        )
    )
    items.extend(_skill_items(scan.source.root / "skills", target / "skills", MigrationProvider.HERMES))
    items.extend(_secret_items(scan.source.root / ".env", target, include_secrets=include_secrets))
    items.extend(_archive_items(scan, target / "archive" / MigrationProvider.HERMES.value))

    if not include_secrets and any(item.category == MigrationCategory.API_KEYS for item in items):
        warnings.append("Secrets were detected but skipped. Re-run with include_secrets=True after explicit user consent.")

    return _plan_from_items(scan, items, warnings, target, include_secrets)


def build_openclaw_plan(
    root: str | Path | None = None,
    *,
    target_root: str | Path | None = None,
    include_secrets: bool = False,
) -> MigrationPlan:
    scan = detect_openclaw_source(root)
    target = _target_root(target_root)
    if not scan.source.found:
        return _empty_for_missing(scan.source, "OpenClaw state was not found; nothing was planned.")

    warnings: list[str] = list(scan.source.warnings)
    config_path = scan.source.root / "openclaw.json"
    config = _parse_json_object(config_path, warnings, "OpenClaw openclaw.json")
    items: list[MigrationItem] = []

    model_ref = _resolve_openclaw_model_ref(config)
    if model_ref:
        items.append(
            _planned_item(
                id="model:default",
                category=MigrationCategory.MODEL,
                action=MigrationAction.UPDATE,
                source=config_path,
                target=target / "config" / "models.json#default",
                label="Default model",
                details={"model": model_ref, "fallbacks": _openclaw_model_fallbacks(config)},
            )
        )

    provider_ids = sorted(_child_record(_child_record(config, "models"), "providers"))
    if provider_ids:
        items.append(
            _planned_item(
                id="settings:model-providers",
                category=MigrationCategory.SETTINGS,
                action=MigrationAction.UPDATE,
                source=config_path,
                target=target / "config" / "providers.json",
                label="Model providers",
                details={"providers": provider_ids},
                sensitivity=MigrationSensitivity.SENSITIVE,
            )
        )

    for server_name, server_config in _record_items(_child_record(_child_record(config, "mcp"), "servers")):
        items.append(
            _disabled_mcp_item(
                provider=MigrationProvider.OPENCLAW,
                name=server_name,
                source=config_path,
                target_root=target,
                details={"server": _redaction_safe_mcp(server_config), "enabled": False},
            )
        )

    for channel_name, channel_config in _record_items(_child_record(config, "channels")):
        items.append(
            _manual_review_item(
                id=make_item_id(MigrationCategory.CHANNELS, channel_name),
                category=MigrationCategory.CHANNELS,
                source=config_path,
                target=target / f"config/channels.json#{channel_name}",
                label=f"Channel: {channel_name}",
                reason="Channel settings require manual review before activation.",
                details={"channel": channel_name, "keys": sorted(str(key) for key in _as_record(channel_config))},
                sensitivity=MigrationSensitivity.SENSITIVE,
            )
        )

    items.extend(
        _file_items(
            scan.source.root,
            target,
            provider=MigrationProvider.OPENCLAW,
            mappings=(
                ("workspace/SOUL.md", MigrationCategory.IDENTITY, MigrationAction.COPY, target / "identity" / "SOUL.md", "Persona"),
                ("workspace/AGENTS.md", MigrationCategory.IDENTITY, MigrationAction.COPY, target / "identity" / "AGENTS.md", "Agent instructions"),
                ("workspace/MEMORY.md", MigrationCategory.MEMORIES, MigrationAction.APPEND, target / "memory" / "MEMORY.md", "Memory"),
                ("workspace/USER.md", MigrationCategory.MEMORIES, MigrationAction.APPEND, target / "memory" / "USER.md", "User profile"),
            ),
        )
    )
    items.extend(_daily_memory_item(scan.source.root / "workspace" / "memory", target / "memory" / "daily-memory.md"))
    items.extend(_skill_items(scan.source.root / "workspace" / "skills", target / "skills", MigrationProvider.OPENCLAW))
    items.extend(_skill_items(scan.source.root / "skills", target / "skills", MigrationProvider.OPENCLAW, prefix="shared-skill"))
    items.extend(_secret_items(scan.source.root / ".env", target, include_secrets=include_secrets))

    for section in ("approvals", "browser", "tools", "cron", "hooks", "memory"):
        if section in config:
            items.append(
                _manual_review_item(
                    id=make_item_id(MigrationCategory.SETTINGS, section),
                    category=MigrationCategory.SETTINGS,
                    source=config_path,
                    target=target / f"config/{section}.json",
                    label=f"OpenClaw {section}",
                    reason=f"OpenClaw {section} settings require manual review before activation.",
                    details={"section": section},
                    sensitivity=MigrationSensitivity.RISKY if section in {"approvals", "tools", "hooks"} else MigrationSensitivity.SENSITIVE,
                )
            )

    items.extend(_archive_items(scan, target / "archive" / MigrationProvider.OPENCLAW.value))

    if not include_secrets and any(item.category == MigrationCategory.API_KEYS for item in items):
        warnings.append("Secrets were detected but skipped. Re-run with include_secrets=True after explicit user consent.")

    return _plan_from_items(scan, items, warnings, target, include_secrets)


def _target_root(value: str | Path | None) -> Path:
    return Path(value).expanduser() if value is not None else DEFAULT_TARGET_ROOT


def _empty_for_missing(source: MigrationSource, warning: str) -> MigrationPlan:
    warnings = tuple(dict.fromkeys((*source.warnings, warning)))
    return MigrationPlan(source=source, warnings=warnings, metadata={"mode": "dry_run", "read_only": True})


def _plan_from_items(
    scan: MigrationScan,
    items: list[MigrationItem],
    warnings: list[str],
    target_root: Path,
    include_secrets: bool,
) -> MigrationPlan:
    if any(item.is_archive_only for item in items):
        warnings.append("Archive-only state will be copied to a migration report later, not imported live.")
    if any(item.status == MigrationStatus.CONFLICT for item in items):
        warnings.append("Conflicting target files were found. Resolve or choose an overwrite policy before apply.")
    return MigrationPlan(
        source=scan.source,
        items=tuple(items),
        warnings=tuple(dict.fromkeys(warnings)),
        metadata={
            "mode": "dry_run",
            "read_only": True,
            "target_root": str(target_root),
            "include_secrets": include_secrets,
            "scan_summary": scan.summary.to_dict(),
        },
    )


def _planned_item(
    *,
    id: str,
    category: MigrationCategory,
    action: MigrationAction,
    source: str | Path | None,
    target: str | Path | None,
    label: str,
    details: dict[str, Any] | None = None,
    sensitivity: MigrationSensitivity = MigrationSensitivity.NORMAL,
    selected: bool = True,
) -> MigrationItem:
    status = _target_status(action, target)
    return MigrationItem(
        id=id,
        category=category,
        action=action,
        status=status,
        source=source,
        target=target,
        label=label,
        reason="target already exists" if status == MigrationStatus.CONFLICT else "",
        details=details or {},
        sensitivity=sensitivity,
        conflict_policy=ConflictPolicy.REFUSE,
        selected=selected and status != MigrationStatus.CONFLICT,
    )


def _manual_review_item(
    *,
    id: str,
    category: MigrationCategory,
    source: str | Path | None,
    target: str | Path | None,
    label: str,
    reason: str,
    details: dict[str, Any],
    sensitivity: MigrationSensitivity,
) -> MigrationItem:
    return MigrationItem(
        id=id,
        category=category,
        action=MigrationAction.MANUAL_REVIEW,
        status=MigrationStatus.SKIPPED,
        source=source,
        target=target,
        label=label,
        reason=reason,
        details=details,
        sensitivity=sensitivity,
        selected=False,
        requires_confirmation=True,
    )


def _disabled_mcp_item(
    *,
    provider: MigrationProvider,
    name: str,
    source: Path,
    target_root: Path,
    details: dict[str, Any],
) -> MigrationItem:
    return MigrationItem(
        id=make_item_id(MigrationCategory.MCP, name),
        category=MigrationCategory.MCP,
        action=MigrationAction.CREATE,
        status=_target_status(MigrationAction.CREATE, target_root / f"config/mcp_servers.json#{name}"),
        source=source,
        target=target_root / f"config/mcp_servers.json#{name}",
        label=f"MCP server: {name}",
        reason=f"Imported {provider.value} MCP servers stay disabled until reviewed.",
        details=details,
        sensitivity=MigrationSensitivity.SENSITIVE,
        selected=False,
        requires_confirmation=True,
    )


def _file_items(
    source_root: Path,
    target_root: Path,
    *,
    provider: MigrationProvider,
    mappings: tuple[tuple[str, MigrationCategory, MigrationAction, Path, str], ...],
) -> list[MigrationItem]:
    items: list[MigrationItem] = []
    for relative_path, category, action, target, label in mappings:
        source = source_root / relative_path
        if not source.is_file():
            continue
        items.append(
            _planned_item(
                id=make_item_id(category, relative_path),
                category=category,
                action=action,
                source=source,
                target=target,
                label=f"{provider.value.title()} {label}",
                details={"relative_path": relative_path.replace("\\", "/")},
            )
        )
    return items


def _daily_memory_item(source_dir: Path, target_dir: Path) -> list[MigrationItem]:
    if not source_dir.is_dir():
        return []
    files = sorted(path.name for path in source_dir.glob("*.md") if path.is_file())
    if not files:
        return []
    return [
        MigrationItem(
            id="memories:daily-memory",
            category=MigrationCategory.MEMORIES,
            action=MigrationAction.APPEND,
            source=source_dir,
            target=target_dir,
            label="Daily memory files",
            details={"files": files, "count": len(files)},
        )
    ]


def _skill_items(source_dir: Path, target_dir: Path, provider: MigrationProvider, *, prefix: str = "skill") -> list[MigrationItem]:
    if not source_dir.is_dir():
        return []
    items: list[MigrationItem] = []
    for skill_dir in sorted(path for path in source_dir.iterdir() if path.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue
        skill_name = _safe_name(skill_dir.name)
        items.append(
            _planned_item(
                id=make_item_id(MigrationCategory.SKILLS, f"{prefix}:{skill_name}"),
                category=MigrationCategory.SKILLS,
                action=MigrationAction.COPY,
                source=skill_file,
                target=target_dir / skill_name / "SKILL.md",
                label=f"{provider.value.title()} skill: {skill_dir.name}",
                details={"skill": skill_dir.name},
            )
        )
    return items


def _secret_items(env_path: Path, target_root: Path, *, include_secrets: bool) -> list[MigrationItem]:
    env_keys = _env_keys(env_path)
    items: list[MigrationItem] = []
    for env_key in env_keys:
        items.append(
            MigrationItem(
                id=make_item_id(MigrationCategory.API_KEYS, env_key),
                category=MigrationCategory.API_KEYS,
                action=MigrationAction.UPDATE,
                status=MigrationStatus.SENSITIVE if include_secrets else MigrationStatus.SKIPPED,
                source=env_path,
                target=target_root / f"api_keys.json#{env_key}",
                label=f"API key: {env_key}",
                reason="secret import disabled by default" if not include_secrets else "requires explicit confirmation before apply",
                details={"env_var": env_key},
                sensitivity=MigrationSensitivity.SECRET,
                selected=include_secrets,
                requires_confirmation=True,
            )
        )
    return items


def _archive_items(scan: MigrationScan, archive_root: Path) -> list[MigrationItem]:
    items: list[MigrationItem] = []
    for entry in scan.entries:
        if not entry.archive_only:
            continue
        items.append(
            MigrationItem(
                id=make_item_id(MigrationCategory.ARCHIVE, entry.relative_path),
                category=MigrationCategory.ARCHIVE,
                action=MigrationAction.ARCHIVE,
                status=MigrationStatus.ARCHIVE_ONLY,
                source=scan.source.root / entry.relative_path,
                target=archive_root / entry.relative_path,
                label=f"Archive: {entry.relative_path}",
                reason=entry.reason,
                details={"relative_path": entry.relative_path, "size_bytes": entry.size_bytes},
                sensitivity=MigrationSensitivity.RISKY,
                selected=False,
            )
        )
    return items


def _target_status(action: MigrationAction, target: str | Path | None) -> MigrationStatus:
    if action == MigrationAction.APPEND:
        return MigrationStatus.PLANNED
    if target is None:
        return MigrationStatus.PLANNED
    target_path = Path(str(target).split("#", 1)[0])
    return MigrationStatus.CONFLICT if target_path.exists() else MigrationStatus.PLANNED


def _parse_json_object(path: Path, warnings: list[str], label: str) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"Could not parse {label}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _parse_yaml_object(path: Path, warnings: list[str], label: str) -> dict[str, Any]:
    if not path.is_file() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append(f"Could not parse {label}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_hermes_model_ref(config: Mapping[str, Any]) -> str | None:
    model = config.get("model")
    if isinstance(model, str) and model.strip():
        provider = _string(config.get("provider"))
        return _join_provider_model(provider, model.strip())
    model_record = _as_record(model)
    raw_model = _string(model_record.get("default")) or _string(model_record.get("model"))
    provider = _string(model_record.get("provider"))
    if raw_model:
        return _join_provider_model(provider, raw_model)
    root_model = _string(config.get("default_model")) or _string(config.get("model_name"))
    root_provider = _string(config.get("provider"))
    return _join_provider_model(root_provider, root_model) if root_model else None


def _resolve_openclaw_model_ref(config: Mapping[str, Any]) -> str | None:
    defaults = _child_record(_child_record(config, "agents"), "defaults")
    model = defaults.get("model") or config.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()
    model_record = _as_record(model)
    return _string(model_record.get("primary")) or _string(model_record.get("default")) or _string(model_record.get("model"))


def _openclaw_model_fallbacks(config: Mapping[str, Any]) -> list[str]:
    defaults = _child_record(_child_record(config, "agents"), "defaults")
    model_record = _as_record(defaults.get("model") or config.get("model"))
    fallbacks = model_record.get("fallbacks")
    if not isinstance(fallbacks, list):
        return []
    return [entry for entry in fallbacks if isinstance(entry, str) and entry.strip()]


def _hermes_provider_ids(config: Mapping[str, Any], model_ref: str | None) -> list[str]:
    ids = set(_child_record(config, "providers"))
    custom_providers = config.get("custom_providers")
    if isinstance(custom_providers, list):
        for entry in custom_providers:
            record = _as_record(entry)
            provider_id = _string(record.get("name")) or _string(record.get("id"))
            if provider_id:
                ids.add(provider_id)
    if model_ref and "/" in model_ref:
        ids.add(model_ref.split("/", 1)[0])
    return sorted(ids)


def _record_items(value: Mapping[str, Any]) -> list[tuple[str, Any]]:
    return sorted(value.items(), key=lambda item: item[0])


def _child_record(root: Mapping[str, Any], key: str) -> dict[str, Any]:
    return _as_record(root.get(key))


def _as_record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _join_provider_model(provider: str | None, model: str) -> str:
    return f"{provider}/{model}" if provider and "/" not in model else model


def _redaction_safe_mcp(value: Any) -> dict[str, Any]:
    record = _as_record(value)
    safe: dict[str, Any] = {}
    for key, raw_value in record.items():
        if key in {"env", "headers"} and isinstance(raw_value, Mapping):
            safe[key] = sorted(str(entry_key) for entry_key in raw_value)
        else:
            safe[key] = raw_value
    return safe


def _env_keys(path: Path) -> list[str]:
    if not path.is_file():
        return []
    keys: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key:
            keys.append(key)
    return sorted(dict.fromkeys(keys))


def _safe_name(name: str) -> str:
    safe = _SAFE_NAME_RE.sub("-", name.strip().lower()).strip("-_.")
    return safe or "skill"
