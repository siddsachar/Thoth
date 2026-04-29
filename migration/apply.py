"""Backup, report, and guarded apply engine for migration plans."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migration.core import (
    MigrationAction,
    MigrationCategory,
    MigrationItem,
    MigrationPlan,
    MigrationStatus,
    PlanSummary,
)
from migration.redaction import REDACTED, redact_mapping, redact_value


@dataclass(frozen=True)
class MigrationApplyOptions:
    backup_root: Path | str | None = None
    report_root: Path | str | None = None
    require_backup: bool = True
    allow_without_backup: bool = False
    overwrite: bool = False


@dataclass(frozen=True)
class MigrationApplyResult:
    source_plan: MigrationPlan
    items: tuple[MigrationItem, ...]
    backup_dir: Path | None = None
    report_dir: Path | None = None
    warnings: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = ""
    backup_manifest: tuple[dict[str, str], ...] = field(default_factory=tuple)

    @property
    def summary(self) -> PlanSummary:
        return MigrationPlan(source=self.source_plan.source, items=self.items).summary

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        backup_manifest = [dict(entry) for entry in self.backup_manifest]
        if redact:
            backup_manifest = [redact_mapping(entry) for entry in backup_manifest]
        return {
            "source": self.source_plan.source.to_dict(),
            "summary": self.summary.to_dict(),
            "warnings": list(self.warnings),
            "backup_dir": str(self.backup_dir) if self.backup_dir else "",
            "report_dir": str(self.report_dir) if self.report_dir else "",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "backup_manifest": backup_manifest,
            "items": [item.to_dict(redact=redact) for item in self.items],
        }


def apply_migration_plan(
    plan: MigrationPlan,
    options: MigrationApplyOptions | None = None,
) -> MigrationApplyResult:
    """Apply selected items from a migration plan with backups and redacted reports."""
    opts = options or MigrationApplyOptions()
    if not opts.require_backup and not opts.allow_without_backup:
        raise ValueError("Disabling migration backups requires allow_without_backup=True.")

    started_at = _timestamp()
    target_root = _target_root(plan)
    report_dir = _unique_dir(_root_or_default(opts.report_root, target_root / "migration-reports") / started_at)
    backup_dir = None
    if opts.require_backup:
        backup_dir = _unique_dir(_root_or_default(opts.backup_root, target_root / "migration-backups") / started_at)

    report_dir.mkdir(parents=True, exist_ok=True)
    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)

    result_items: list[MigrationItem] = []
    backup_manifest: list[dict[str, str]] = []
    backed_up_targets: set[Path] = set()
    warnings = list(plan.warnings)

    _write_json(report_dir / "plan.json", plan.to_dict(redact=True))
    for item in plan.items:
        if item.is_archive_only:
            result_items.append(_archive_to_report(item, report_dir))
            continue
        if not _should_apply(item, overwrite=opts.overwrite):
            result_items.append(item.with_status(MigrationStatus.SKIPPED, item.reason or "not selected for apply"))
            continue
        if item.status == MigrationStatus.CONFLICT and not opts.overwrite:
            result_items.append(item.with_status(MigrationStatus.BLOCKED, "target conflict requires overwrite"))
            continue
        try:
            backups = _apply_item(
                item,
                target_root,
                backup_dir,
                overwrite=opts.overwrite,
                backed_up_targets=backed_up_targets,
            )
            backup_manifest.extend(backups)
            details = dict(item.details)
            if backups:
                details["backup_count"] = len(backups)
            result_items.append(_replace_item(item, status=MigrationStatus.MIGRATED, reason="applied", details=details))
        except Exception as exc:
            result_items.append(item.with_status(MigrationStatus.ERROR, str(exc)))

    finished_at = _timestamp()
    result = MigrationApplyResult(
        source_plan=plan,
        items=tuple(result_items),
        backup_dir=backup_dir,
        report_dir=report_dir,
        warnings=tuple(dict.fromkeys(warnings)),
        started_at=started_at,
        finished_at=finished_at,
        backup_manifest=tuple(backup_manifest),
    )
    _write_json(report_dir / "result.json", result.to_dict(redact=True))
    _write_json(report_dir / "backup_manifest.json", {"entries": backup_manifest})
    _write_summary(report_dir / "summary.md", result)
    return result


def _apply_item(
    item: MigrationItem,
    target_root: Path,
    backup_dir: Path | None,
    *,
    overwrite: bool,
    backed_up_targets: set[Path],
) -> list[dict[str, str]]:
    if item.category == MigrationCategory.API_KEYS:
        return _apply_secret_item(item, target_root, backup_dir, overwrite=overwrite, backed_up_targets=backed_up_targets)
    if item.action == MigrationAction.COPY:
        return _copy_file_item(item, target_root, backup_dir, overwrite=overwrite, backed_up_targets=backed_up_targets)
    if item.action == MigrationAction.APPEND:
        return _append_item(item, target_root, backup_dir, backed_up_targets=backed_up_targets)
    if item.action in {MigrationAction.CREATE, MigrationAction.UPDATE}:
        return _write_json_item(item, target_root, backup_dir, overwrite=overwrite, backed_up_targets=backed_up_targets)
    raise ValueError(f"unsupported migration action for apply: {item.action.value}")


def _copy_file_item(
    item: MigrationItem,
    target_root: Path,
    backup_dir: Path | None,
    *,
    overwrite: bool,
    backed_up_targets: set[Path],
) -> list[dict[str, str]]:
    source = _require_source(item)
    target, _fragment = _target_file_and_fragment(item)
    if target.exists() and not overwrite and item.status == MigrationStatus.CONFLICT:
        raise FileExistsError(str(target))
    backups = _backup_existing(target, backup_dir, target_root, backed_up_targets)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return backups


def _append_item(
    item: MigrationItem,
    target_root: Path,
    backup_dir: Path | None,
    *,
    backed_up_targets: set[Path],
) -> list[dict[str, str]]:
    source = _require_source(item)
    target, _fragment = _target_file_and_fragment(item)
    backups = _backup_existing(target, backup_dir, target_root, backed_up_targets)
    target.parent.mkdir(parents=True, exist_ok=True)
    chunks = _append_chunks(source)
    with target.open("a", encoding="utf-8") as handle:
        for label, content in chunks:
            handle.write(f"\n\n<!-- Imported from {item.id}: {label} -->\n\n")
            handle.write(content.rstrip())
            handle.write("\n")
    return backups


def _write_json_item(
    item: MigrationItem,
    target_root: Path,
    backup_dir: Path | None,
    *,
    overwrite: bool,
    backed_up_targets: set[Path],
) -> list[dict[str, str]]:
    target, fragment = _target_file_and_fragment(item)
    if target.exists() and item.status == MigrationStatus.CONFLICT and not overwrite:
        raise FileExistsError(str(target))
    backups = _backup_existing(target, backup_dir, target_root, backed_up_targets)
    data = _read_json_object(target)
    value = _json_value_for_item(item)
    if fragment:
        data[fragment] = value
    elif isinstance(value, dict):
        data.update(value)
    else:
        data[item.id] = value
    _write_json(target, data)
    return backups


def _apply_secret_item(
    item: MigrationItem,
    target_root: Path,
    backup_dir: Path | None,
    *,
    overwrite: bool,
    backed_up_targets: set[Path],
) -> list[dict[str, str]]:
    env_path = _require_source(item)
    env_var = str(item.details.get("env_var") or _target_file_and_fragment(item)[1] or "").strip()
    if not env_var:
        raise ValueError("secret item is missing env_var metadata")
    env = _parse_env(env_path)
    if env_var not in env:
        raise KeyError(f"{env_var} was not found in source env file")
    target, fragment = _target_file_and_fragment(item)
    if target.exists() and item.status == MigrationStatus.CONFLICT and not overwrite:
        raise FileExistsError(str(target))
    backups = _backup_existing(target, backup_dir, target_root, backed_up_targets)
    from api_keys import set_key_for_data_dir
    set_key_for_data_dir(target_root, fragment or env_var, env[env_var])
    return backups


def _archive_to_report(item: MigrationItem, report_dir: Path) -> MigrationItem:
    try:
        source = _require_source(item)
        archive_relative = str(item.details.get("relative_path") or source.name).replace("\\", "/")
        target = report_dir / "archive" / archive_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_archive_path(source, target)
        details = dict(item.details)
        details["archived_to"] = str(target)
        return _replace_item(item, status=MigrationStatus.MIGRATED, reason="archived to report", details=details, target=target)
    except Exception as exc:
        return item.with_status(MigrationStatus.ERROR, str(exc))


def _should_apply(item: MigrationItem, *, overwrite: bool) -> bool:
    if not item.selected:
        return False
    if item.status in {MigrationStatus.PLANNED, MigrationStatus.SENSITIVE}:
        return True
    return overwrite and item.status == MigrationStatus.CONFLICT


def _json_value_for_item(item: MigrationItem) -> Any:
    if item.category == MigrationCategory.MODEL:
        return {"model": item.details.get("model"), "fallbacks": item.details.get("fallbacks", [])}
    if item.category == MigrationCategory.MCP:
        return {"enabled": False, **dict(item.details.get("server") or {})}
    return dict(item.details)


def _append_chunks(source: Path) -> list[tuple[str, str]]:
    if source.is_file():
        return [(source.name, source.read_text(encoding="utf-8"))]
    if source.is_dir():
        chunks = []
        for child in sorted(source.glob("*.md")):
            if child.is_file():
                chunks.append((child.name, child.read_text(encoding="utf-8")))
        if chunks:
            return chunks
    raise FileNotFoundError(str(source))


def _copy_archive_path(source: Path, target: Path) -> None:
    if source.is_dir():
        for child in source.rglob("*"):
            if child.is_file():
                _copy_archive_file(child, target / child.relative_to(source))
        return
    _copy_archive_file(source, target)


def _copy_archive_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    redacted = _redacted_archive_text(source)
    if redacted is not None:
        target.write_text(redacted, encoding="utf-8")
        return
    target.write_text(
        "[Binary or unsupported archive file omitted from migration report. Review the original source file manually.]\n",
        encoding="utf-8",
    )


def _redacted_archive_text(source: Path) -> str | None:
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    except OSError:
        return None
    suffix = source.suffix.lower()
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return _redact_key_value_lines(text)
        redacted = redact_value(data)
        return json.dumps(redacted, indent=2, sort_keys=True) + "\n"
    return _redact_key_value_lines(text)


def _redact_key_value_lines(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        separator = "=" if "=" in stripped else ":" if ":" in stripped else ""
        if not stripped or stripped.startswith("#") or not separator:
            lines.append(line)
            continue
        prefix, value = line.split(separator, 1)
        redacted = redact_value(value.strip().strip('"').strip("'"), key=prefix.strip())
        if redacted == REDACTED:
            lines.append(f"{prefix}{separator} {REDACTED}")
        else:
            lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _backup_existing(
    target: Path,
    backup_dir: Path | None,
    target_root: Path,
    backed_up_targets: set[Path],
) -> list[dict[str, str]]:
    target_key = target.resolve()
    if target_key in backed_up_targets:
        return []
    if not target.exists():
        backed_up_targets.add(target_key)
        return []
    if backup_dir is None:
        raise RuntimeError("backup is required before overwriting an existing migration target")
    relative = _relative_to_root(target, target_root)
    destination = backup_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        shutil.copytree(target, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(target, destination)
    backed_up_targets.add(target_key)
    return [{"source": str(target), "backup": str(destination)}]


def _require_source(item: MigrationItem) -> Path:
    if item.source is None:
        raise FileNotFoundError(f"{item.id} has no source path")
    source = Path(item.source)
    if not source.exists():
        raise FileNotFoundError(str(source))
    return source


def _target_file_and_fragment(item: MigrationItem) -> tuple[Path, str]:
    if item.target is None:
        raise ValueError(f"{item.id} has no target path")
    raw = str(item.target)
    file_part, fragment = raw.split("#", 1) if "#" in raw else (raw, "")
    return Path(file_part), fragment


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _replace_item(
    item: MigrationItem,
    *,
    status: MigrationStatus,
    reason: str,
    details: dict[str, Any] | None = None,
    target: Path | str | None = None,
) -> MigrationItem:
    return MigrationItem(
        id=item.id,
        category=item.category,
        action=item.action,
        status=status,
        source=item.source,
        target=item.target if target is None else target,
        label=item.label,
        reason=reason,
        details=dict(item.details if details is None else details),
        sensitivity=item.sensitivity,
        conflict_policy=item.conflict_policy,
        selected=item.selected,
        requires_confirmation=item.requires_confirmation,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_summary(path: Path, result: MigrationApplyResult) -> None:
    summary = result.summary
    lines = [
        "# Migration Apply Report",
        "",
        f"Provider: {result.source_plan.source.provider.value}",
        f"Source: {result.source_plan.source.root}",
        f"Started: {result.started_at}",
        f"Finished: {result.finished_at}",
        f"Migrated: {summary.migrated}",
        f"Skipped: {summary.skipped}",
        f"Blocked: {summary.blocked}",
        f"Errors: {summary.errors}",
        "",
        "## Items",
    ]
    for item in result.items:
        lines.append(f"- {item.status.value}: {item.id} - {item.label or item.reason}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _root_or_default(value: Path | str | None, default: Path) -> Path:
    return Path(value).expanduser() if value is not None else default


def _target_root(plan: MigrationPlan) -> Path:
    raw = plan.metadata.get("target_root")
    return Path(str(raw)).expanduser() if raw else Path.cwd() / "migration-target"


def _unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not allocate unique migration directory below {path.parent}")


def _relative_to_root(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        safe = str(path).replace(":", "").replace("\\", "/").strip("/")
        return Path("external") / safe


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
