from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, field
from hashlib import sha1
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from developer.runtime import CommandResult, classify_command_action, run_workspace_command, split_command
from developer.sandbox import ApprovalDecision, decide_action
from developer.sandbox_runtime import detect_container_runtime, run_docker_sandbox_command
from developer.state import ApprovalMode
from developer.state import DEFAULT_SANDBOX_IMAGE, DeveloperWorkspace
from developer.storage import DEVELOPER_DIR, _write_json_atomic, remember_clone_parent_folder, suggested_clone_name


logger = logging.getLogger(__name__)

CAPSULES_PATH = DEVELOPER_DIR / "tool_capsules.json"
CUSTOM_TOOL_DRAFTS_PATH = DEVELOPER_DIR / "custom_tool_drafts.json"
CAPSULE_INSTALL_ROOT = DEVELOPER_DIR / "tool-capsules"
DEFAULT_CUSTOM_TOOL_TEST_QUERY = "python"


@dataclass
class ToolCapsule:
    id: str
    name: str
    source_url: str
    installed_path: str
    version: str = ""
    enabled: bool = False
    community: bool = True
    commands: list[dict] = field(default_factory=list)
    promoted_plugin_id: str = ""
    promoted_at: str = ""


@dataclass(frozen=True)
class CapsuleManifestProposal:
    name: str
    version: str
    source_url: str
    installed_path: str
    commands: list[dict]
    warnings: list[str] = field(default_factory=list)
    existing_manifest: bool = False

    def to_manifest(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "commands": list(self.commands),
        }


@dataclass
class CustomToolDraft:
    id: str
    source_url: str
    installed_path: str
    name: str
    version: str = "1.0.0"
    commands: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    test_results: dict[str, dict] = field(default_factory=dict)
    status: str = "draft"
    created_tool_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_proposal(self) -> CapsuleManifestProposal:
        return CapsuleManifestProposal(
            name=self.name,
            version=self.version,
            source_url=self.source_url,
            installed_path=self.installed_path,
            commands=list(self.commands),
            warnings=list(self.warnings),
            existing_manifest=False,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> dict:
    return {"capsules": []}


def _load_state() -> dict:
    if not CAPSULES_PATH.exists():
        return _default_state()
    try:
        data = json.loads(CAPSULES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _default_state()
    if not isinstance(data, dict):
        return _default_state()
    data.setdefault("capsules", [])
    return data


def _save_state(data: dict) -> None:
    _write_json_atomic(CAPSULES_PATH, data)


def _default_draft_state() -> dict:
    return {"drafts": []}


def _load_draft_state() -> dict:
    if not CUSTOM_TOOL_DRAFTS_PATH.exists():
        return _default_draft_state()
    try:
        data = json.loads(CUSTOM_TOOL_DRAFTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _default_draft_state()
    if not isinstance(data, dict):
        return _default_draft_state()
    data.setdefault("drafts", [])
    return data


def _save_draft_state(data: dict) -> None:
    _write_json_atomic(CUSTOM_TOOL_DRAFTS_PATH, data)


def _draft_id(source_url: str, installed_path: str) -> str:
    key = f"{source_url.strip()}|{Path(installed_path).expanduser().resolve()}"
    return f"draft-{sha1(key.encode('utf-8', errors='ignore')).hexdigest()[:12]}"


def _draft_from_item(item: dict[str, Any]) -> CustomToolDraft | None:
    try:
        return CustomToolDraft(**item)
    except TypeError:
        return None


def _save_draft(draft: CustomToolDraft) -> CustomToolDraft:
    draft.updated_at = _now_iso()
    data = _load_draft_state()
    rows = [item for item in data.get("drafts", []) if item.get("id") != draft.id]
    rows.append(asdict(draft))
    data["drafts"] = rows
    _save_draft_state(data)
    return draft


def list_custom_tool_drafts() -> list[CustomToolDraft]:
    drafts: list[CustomToolDraft] = []
    for item in _load_draft_state().get("drafts", []):
        if isinstance(item, dict):
            draft = _draft_from_item(item)
            if draft is not None:
                drafts.append(draft)
    return drafts


def get_custom_tool_draft(draft_id: str) -> CustomToolDraft:
    for draft in list_custom_tool_drafts():
        if draft.id == draft_id:
            return draft
    raise KeyError(f"Custom Tool draft not found: {draft_id}")


def delete_custom_tool_draft(draft_id: str) -> None:
    data = _load_draft_state()
    data["drafts"] = [item for item in data.get("drafts", []) if item.get("id") != draft_id]
    _save_draft_state(data)


def _capsule_id(name_or_url: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name_or_url.strip().lower()).strip("-")
    return slug[:80] or "custom-tool"


def list_capsules() -> list[ToolCapsule]:
    capsules = []
    for item in _load_state().get("capsules", []):
        if isinstance(item, dict):
            try:
                capsules.append(ToolCapsule(**item))
            except TypeError:
                continue
    return capsules


def _plugin_slug(value: str, *, sep: str = "-") -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", sep, value.strip().lower()).strip(sep)
    return slug or "custom-tool"


def _is_public_source(source_url: str) -> bool:
    return source_url.strip().startswith(("http://", "https://", "git@"))


def _looks_like_custom_tool_source(path: Path) -> bool:
    """Return True when a folder looks like a repo/tool source, not a clone parent."""
    if not path.exists() or not path.is_dir():
        return False
    indicators = (
        ".git",
        "thoth-custom-tool.json",
        ".thoth-custom-tool.json",
        "custom-tool.json",
        "thoth-capsule.json",
        "README.md",
        "README.rst",
        "README.txt",
        "readme.md",
        "package.json",
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "Cargo.toml",
        "go.mod",
        "Makefile",
    )
    return any((path / name).exists() for name in indicators)


def promoted_plugin_id(capsule_id: str) -> str:
    return f"custom-tool-{_plugin_slug(capsule_id)}"[:64].rstrip("-")


def _command_tool_name(capsule: ToolCapsule, command: dict[str, Any]) -> str:
    capsule_slug = _plugin_slug(capsule.id, sep="_")
    command_slug = _plugin_slug(str(command.get("name", "run")), sep="_")
    return f"custom_tool_{capsule_slug}_{command_slug}"[:64].rstrip("_")


def parse_capsule_manifest(installed_path: str) -> dict:
    """Read and validate an optional Custom Tool manifest.

    The manifest is intentionally small for the first public shape. A Custom Tool
    can declare display metadata and command entries, but execution still flows
    through Developer runtime approval policy.
    """
    root = Path(installed_path).expanduser().resolve()
    for filename in (
        "thoth-custom-tool.json",
        ".thoth-custom-tool.json",
        "custom-tool.json",
        "thoth-capsule.json",
        ".thoth-capsule.json",
        "capsule.json",
    ):
        path = root / filename
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Custom Tool config must be a JSON object: {path}")
        commands: list[dict] = []
        for item in data.get("commands", []):
            if not isinstance(item, dict):
                raise ValueError("Custom Tool commands must be objects.")
            name = str(item.get("name", "")).strip()
            command = str(item.get("command", "")).strip()
            if not name or not command:
                raise ValueError("Custom Tool commands require name and command.")
            commands.append(
                {
                    "name": name,
                    "command": command,
                    "description": str(item.get("description", "")).strip(),
                }
            )
        return {
            "name": str(data.get("name", "")).strip(),
            "version": str(data.get("version", "")).strip(),
            "commands": commands,
        }
    return {}


def clone_capsule_repository(repo_url: str, destination_parent: str) -> Path:
    """Clone a public Custom Tool source into a user-chosen folder."""
    source = str(repo_url or "").strip()
    if not source:
        raise ValueError("Repository URL is required.")
    parent = Path(destination_parent).expanduser().resolve()
    if not parent.exists() or not parent.is_dir():
        raise ValueError(f"Clone destination folder does not exist: {destination_parent}")
    remember_clone_parent_folder(str(parent))
    target = parent / suggested_clone_name(source)
    if target.exists():
        if target.is_dir():
            return target
        raise FileExistsError(f"Clone target already exists and is not a folder: {target}")
    subprocess.run(
        ["git", "clone", source, str(target)],
        cwd=str(parent),
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return target


def propose_capsule_manifest(installed_path: str, *, source_url: str = "", use_ai: bool = False) -> CapsuleManifestProposal:
    """Infer a safe first Custom Tool manifest from a repo/folder.

    The preferred product path is a simple AI pass over a compact repo brief.
    The deterministic proposal stays as a conservative fallback for offline
    setups, tests, or model failures.
    """
    root = Path(installed_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Custom Tool folder does not exist: {installed_path}")
    existing = parse_capsule_manifest(str(root))
    if existing.get("commands"):
        return CapsuleManifestProposal(
            name=existing.get("name") or _friendly_capsule_name(root, source_url),
            version=existing.get("version") or "1.0.0",
            source_url=str(source_url or "").strip() or root.as_uri(),
            installed_path=str(root),
            commands=existing.get("commands", []),
            warnings=["Existing Custom Tool config found; review before replacing."],
            existing_manifest=True,
        )

    if use_ai:
        try:
            return generate_custom_tool_proposal_with_llm(str(root), source_url=source_url)
        except Exception as exc:
            logger.warning("AI Custom Tool proposal failed; using deterministic fallback: %s", exc)
            fallback = _deterministic_capsule_manifest(root, source_url=source_url)
            detail = str(exc).strip()
            if "Skipped " in detail or "no safe commands" in detail.lower():
                fallback.warnings.insert(0, "AI proposal was rejected by safety validation, so Thoth used the basic scanner.")
            else:
                fallback.warnings.insert(0, "AI analysis was unavailable, so Thoth used the basic scanner.")
            if detail:
                fallback.warnings.insert(1, f"AI detail: {detail[:240]}")
            return fallback

    return _deterministic_capsule_manifest(root, source_url=source_url)


def _deterministic_capsule_manifest(root: Path, *, source_url: str = "") -> CapsuleManifestProposal:
    commands: list[dict] = []
    warnings: list[str] = []

    def add(name: str, command: str, description: str) -> None:
        if any(item.get("name") == name or item.get("command") == command for item in commands):
            return
        commands.append({"name": name, "command": command, "description": description})

    add(
        "List files",
        "python -c \"from pathlib import Path; print('\\n'.join(str(p) for p in sorted(Path('.').iterdir())[:80]))\"",
        "List top-level files in this tool repository.",
    )

    readme = _first_existing(root, ["README.md", "README.rst", "README.txt", "readme.md"])
    if readme:
        add(
            "Show README",
            f"python -c \"from pathlib import Path; print(Path({readme.name!r}).read_text(encoding='utf-8', errors='replace')[:6000])\"",
            "Print the first part of the repository README.",
        )
    else:
        warnings.append("No README file found; generated commands may need extra review.")

    tldr_page_dirs = sorted(
        item.name
        for item in root.iterdir()
        if item.is_dir() and (item.name == "pages" or item.name.startswith("pages."))
    )
    if (root / "pages").is_dir() and tldr_page_dirs:
        add(
            "List TLDR languages",
            "python -c \"from pathlib import Path; dirs=sorted(d.name for d in Path('.').iterdir() if d.is_dir() and (d.name == 'pages' or d.name.startswith('pages.'))); print('\\n'.join(dirs[:160]))\"",
            "List available TLDR page language/platform folders.",
        )
        add(
            "Count TLDR pages",
            "python -c \"from pathlib import Path; pages=[p for d in Path('.').iterdir() if d.is_dir() and (d.name == 'pages' or d.name.startswith('pages.')) for p in d.rglob('*.md')]; print(f'{len(pages)} TLDR pages across {len(set(p.parts[0] for p in pages))} folders')\"",
            "Count local TLDR command pages.",
        )
        add(
            "Search TLDR pages",
            "python -c \"from pathlib import Path; q='{query}'.strip().lower(); q='' if (q.startswith('{') and q.endswith('}')) else q; q=q or 'tar'; matches=[p for p in Path('pages').rglob('*.md') if q in p.stem.lower()][:30]; print('\\n'.join(str(p) for p in matches) or f'No TLDR pages found for {q}')\"",
            "Search English TLDR page filenames; defaults to tar when no query is provided.",
        )
        add(
            "Show tar TLDR page",
            "python -c \"from pathlib import Path; matches=list(Path('pages').rglob('tar.md')); p=matches[0] if matches else None; print(p.read_text(encoding='utf-8', errors='replace')[:6000] if p else 'tar.md not found')\"",
            "Show a representative TLDR command page.",
        )

    root_gitignores = sorted(path for path in root.glob("*.gitignore") if path.name != ".gitignore")
    if len(root_gitignores) >= 2 or (root / "Python.gitignore").exists():
        add(
            "List gitignore templates",
            "python -c \"from pathlib import Path; print('\\n'.join(sorted(p.stem for p in Path('.').glob('*.gitignore'))[:120]))\"",
            "List root .gitignore template names.",
        )
        preferred = "Python.gitignore" if (root / "Python.gitignore").exists() else root_gitignores[0].name
        add(
            f"Show {Path(preferred).stem} template",
            f"python -c \"from pathlib import Path; p=Path({preferred!r}); print(p.read_text(encoding='utf-8', errors='replace')[:6000] if p.exists() else '{preferred} not found')\"",
            f"Print the {Path(preferred).stem} .gitignore template.",
        )
        add(
            "Count gitignore templates",
            "python -c \"from pathlib import Path; files=list(Path('.').rglob('*.gitignore')); print(f'{len(files)} gitignore templates found')\"",
            "Count .gitignore templates in the repository.",
        )

    package_json = root / "package.json"
    if package_json.exists():
        add(
            "Show package scripts",
            "python -c \"import json; from pathlib import Path; data=json.loads(Path('package.json').read_text(encoding='utf-8')); print('\\n'.join(f'{k}: {v}' for k,v in sorted(data.get('scripts', {}).items())) or 'No package scripts')\"",
            "List package.json scripts without installing dependencies.",
        )

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        add(
            "Show Python project metadata",
            "python -c \"from pathlib import Path; print(Path('pyproject.toml').read_text(encoding='utf-8', errors='replace')[:5000])\"",
            "Print pyproject.toml metadata.",
        )

    if (root / "Cargo.toml").exists():
        add(
            "Show Rust project metadata",
            "python -c \"from pathlib import Path; print(Path('Cargo.toml').read_text(encoding='utf-8', errors='replace')[:5000])\"",
            "Print Cargo.toml metadata.",
        )

    if (root / "go.mod").exists():
        add(
            "Show Go module metadata",
            "python -c \"from pathlib import Path; print(Path('go.mod').read_text(encoding='utf-8', errors='replace')[:5000])\"",
            "Print go.mod metadata.",
        )

    if len(commands) <= 2:
        warnings.append("Only generic read-only commands were inferred. Review and add domain-specific commands before promotion.")

    return CapsuleManifestProposal(
        name=_friendly_capsule_name(root, source_url),
        version="1.0.0",
        source_url=str(source_url or "").strip() or root.as_uri(),
        installed_path=str(root),
        commands=commands[:8],
        warnings=warnings,
        existing_manifest=False,
    )


def _read_text_excerpt(path: Path, limit: int = 5000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _build_custom_tool_repo_brief(root: Path, source_url: str = "") -> str:
    top_level: list[str] = []
    for item in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:120]:
        suffix = "/" if item.is_dir() else ""
        top_level.append(f"- {item.name}{suffix}")

    manifest_names = [
        "package.json",
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "Cargo.toml",
        "go.mod",
        "Makefile",
        "README.md",
        "README.rst",
        "README.txt",
    ]
    excerpts: list[str] = []
    for name in manifest_names:
        path = root / name
        if path.exists() and path.is_file():
            excerpts.append(f"## {name}\n{_read_text_excerpt(path, 3500)}")

    sample_files: list[str] = []
    for pattern in ("docs/**/*.md", "examples/**/*", "data/**/*", "*.md", "*.json", "*.csv"):
        for path in sorted(root.glob(pattern))[:12]:
            if path.is_file():
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    continue
                sample_files.append(str(rel))
        if len(sample_files) >= 24:
            break

    return "\n\n".join(
        [
            f"Source: {source_url or root.as_uri()}",
            f"Path name: {root.name}",
            "Top-level tree:\n" + "\n".join(top_level),
            "Sample files:\n" + ("\n".join(f"- {item}" for item in sample_files[:30]) or "- none"),
            "\n\n".join(excerpts)[:14000],
        ]
    )[:22000]


_BLOCKED_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"\brm\s+(-|/)", "delete command"),
        (r"\bdel\s+", "delete command"),
        (r"\brmdir\s+", "directory removal command"),
        (r"\bremove-item\b", "file removal command"),
        (r"\bshutdown\b", "shutdown command"),
        (r"\bmkfs\b", "disk format command"),
        (r"\bgit\s+(push|commit|clone|reset|checkout)\b", "git mutation command"),
        (r"\b(pip|uv|poetry|npm|pnpm|yarn|cargo|go)\s+install\b", "dependency install command"),
        (r"\bcurl\b", "network command"),
        (r"\bwget\b", "network command"),
        (r"\binvoke-webrequest\b", "network command"),
        (r"\binvoke-restmethod\b", "network command"),
        (r"https?://", "network URL"),
        (r"\|\s*(sh|bash|powershell|pwsh|cmd)\b", "shell pipe command"),
    )
)


def _has_unquoted_redirection(command: str) -> bool:
    in_single = False
    in_double = False
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if char == "\\" and in_double:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == ">" and not in_single and not in_double:
            return True
    return False


def _first_command_name(command: str) -> str:
    try:
        parts = split_command(command)
    except Exception:
        return ""
    if not parts:
        return ""
    executable = re.split(r"[\\/]", parts[0])[-1].strip().lower()
    for suffix in (".exe", ".com", ".bat", ".cmd"):
        if executable.endswith(suffix):
            executable = executable[: -len(suffix)]
    return executable


def _blocked_custom_tool_command_reason(command: str) -> str:
    # `format` is only dangerous as the executable, not as Python string formatting.
    if _first_command_name(command) == "format":
        return "disk format command"
    for pattern, reason in _BLOCKED_COMMAND_PATTERNS:
        if pattern.search(command):
            return reason
    if _has_unquoted_redirection(command):
        return "shell output redirection"
    return ""


def _validate_ai_commands(raw_commands: Any) -> tuple[list[dict], list[str]]:
    commands: list[dict] = []
    warnings: list[str] = []
    if not isinstance(raw_commands, list):
        return [], ["AI proposal did not include a commands list."]
    for idx, item in enumerate(raw_commands[:10], start=1):
        if not isinstance(item, dict):
            warnings.append(f"Skipped command {idx}: command entry was not an object.")
            continue
        name = str(item.get("name", "")).strip()[:80]
        command = str(item.get("command", "")).strip()
        description = str(item.get("description", "")).strip()[:240]
        if not name or not command:
            warnings.append(f"Skipped command {idx}: name and command are required.")
            continue
        if len(command) > 1200:
            warnings.append(f"Skipped {name}: command was too long.")
            continue
        blocked = _blocked_custom_tool_command_reason(command)
        if blocked:
            warnings.append(f"Skipped {name}: command needs manual review ({blocked}).")
            continue
        if any(existing["name"].lower() == name.lower() or existing["command"] == command for existing in commands):
            continue
        commands.append({"name": name, "command": command, "description": description})
    if not commands:
        warnings.append("AI proposal had no safe commands after validation.")
    return commands[:8], warnings


def _extract_json_object(text: str) -> dict:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1)
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("AI proposal JSON must be an object.")
    return data


def generate_custom_tool_proposal_with_llm(installed_path: str, *, source_url: str = "", model: str = "") -> CapsuleManifestProposal:
    root = Path(installed_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Custom Tool folder does not exist: {installed_path}")

    brief = _build_custom_tool_repo_brief(root, source_url=source_url)
    prompt = f"""You are designing a Thoth Custom Tool from a local repository.

Return ONLY valid JSON. Do not wrap in markdown.

Goal: infer useful, safe commands that make this repository valuable as a reusable tool.

Rules:
- Prefer 3 to 8 commands.
- Commands must be read-only by default.
- Commands must run from the repository root.
- Prefer cross-platform Python one-liners using `python -c "..."`.
- Use `{{query}}` when a command should accept user input.
- Do not install dependencies.
- Do not call network URLs.
- Do not delete, overwrite, commit, push, clone, or modify files.
- Avoid generic commands unless they are genuinely useful.

JSON schema:
{{
  "name": "Short Custom Tool name",
  "version": "1.0.0",
  "purpose": "One sentence",
  "commands": [
    {{"name": "Search items", "description": "What this returns", "command": "python -c \\"...\\""}}
  ],
  "warnings": ["optional short warnings"]
}}

Repository brief:
{brief}
"""
    from langchain_core.messages import HumanMessage
    from models import get_current_model, get_llm_for

    llm = get_llm_for(model or get_current_model())
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = response.content or ""
    if isinstance(raw, list):
        raw = " ".join(block.get("text", "") if isinstance(block, dict) else str(block) for block in raw)
    data = _extract_json_object(str(raw))
    commands, validation_warnings = _validate_ai_commands(data.get("commands"))
    if not commands:
        raise ValueError("; ".join(validation_warnings) or "No safe commands generated.")
    warnings = [str(item).strip() for item in data.get("warnings", []) if str(item).strip()] if isinstance(data.get("warnings"), list) else []
    warnings.extend(validation_warnings)
    return CapsuleManifestProposal(
        name=str(data.get("name") or _friendly_capsule_name(root, source_url)).strip()[:80],
        version=str(data.get("version") or "1.0.0").strip()[:32],
        source_url=str(source_url or "").strip() or root.as_uri(),
        installed_path=str(root),
        commands=commands,
        warnings=warnings,
        existing_manifest=False,
    )


def write_capsule_manifest(
    installed_path: str,
    proposal: CapsuleManifestProposal | dict,
    *,
    overwrite: bool = False,
) -> Path:
    root = Path(installed_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Custom Tool folder does not exist: {installed_path}")
    legacy_manifest_path = root / "thoth-capsule.json"
    manifest_path = legacy_manifest_path if legacy_manifest_path.exists() else root / "thoth-custom-tool.json"
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(f"Custom Tool config already exists: {manifest_path}")
    payload = proposal.to_manifest() if isinstance(proposal, CapsuleManifestProposal) else dict(proposal)
    commands = payload.get("commands", [])
    if not isinstance(commands, list) or not commands:
        raise ValueError("Custom Tool config requires at least one command.")
    _write_json_atomic(manifest_path, payload)
    return manifest_path


def generate_and_register_capsule(
    installed_path: str,
    *,
    source_url: str = "",
    overwrite: bool = False,
    community: bool = True,
) -> ToolCapsule:
    proposal = propose_capsule_manifest(installed_path, source_url=source_url)
    write_capsule_manifest(installed_path, proposal, overwrite=overwrite or proposal.existing_manifest)
    return register_capsule(
        proposal.source_url,
        installed_path=proposal.installed_path,
        community=community,
    )


# Public product-language aliases. The persisted implementation still uses
# "capsule" internally for compatibility, but UI/agent surfaces call these
# Custom Tools.
CustomTool = ToolCapsule
CustomToolProposal = CapsuleManifestProposal


def list_custom_tools() -> list[ToolCapsule]:
    return list_capsules()


def inspect_custom_tool_source(path: str, *, source_url: str = "", use_ai: bool = True) -> CapsuleManifestProposal:
    return propose_capsule_manifest(path, source_url=source_url, use_ai=use_ai)


def create_custom_tool_from_source(
    path: str,
    *,
    source_url: str = "",
    overwrite: bool = False,
    community: bool = True,
    use_ai: bool = True,
) -> ToolCapsule:
    proposal = propose_capsule_manifest(path, source_url=source_url, use_ai=use_ai)
    write_capsule_manifest(path, proposal, overwrite=overwrite or proposal.existing_manifest)
    return register_capsule(proposal.source_url, installed_path=proposal.installed_path, community=community)


def create_custom_tool_draft(path: str, *, source_url: str = "", use_ai: bool = True) -> CustomToolDraft:
    proposal = propose_capsule_manifest(path, source_url=source_url, use_ai=use_ai)
    now = _now_iso()
    draft_id = _draft_id(proposal.source_url, proposal.installed_path)
    existing = next((item for item in list_custom_tool_drafts() if item.id == draft_id), None)
    draft = CustomToolDraft(
        id=draft_id,
        source_url=proposal.source_url,
        installed_path=proposal.installed_path,
        name=proposal.name,
        version=proposal.version or "1.0.0",
        commands=list(proposal.commands),
        warnings=list(proposal.warnings),
        test_results=existing.test_results if existing else {},
        status=existing.status if existing and existing.status != "deleted" else "draft",
        created_tool_id=existing.created_tool_id if existing else "",
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    return _save_draft(draft)


def update_custom_tool_draft(draft_id: str, fields: dict[str, Any]) -> CustomToolDraft:
    draft = get_custom_tool_draft(draft_id)
    allowed = {"name", "version", "source_url", "installed_path", "warnings", "commands", "status"}
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "commands":
            commands, warnings = _validate_ai_commands(value)
            if not commands:
                raise ValueError("; ".join(warnings) or "No safe Custom Tool commands supplied.")
            draft.commands = commands
            draft.warnings.extend(warning for warning in warnings if warning not in draft.warnings)
            continue
        if key == "warnings":
            draft.warnings = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
            continue
        setattr(draft, key, str(value).strip())
    return _save_draft(draft)


def refine_custom_tool_draft_with_llm(draft_id: str, instruction: str, *, model: str = "") -> CustomToolDraft:
    draft = get_custom_tool_draft(draft_id)
    root = Path(draft.installed_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Custom Tool folder does not exist: {draft.installed_path}")
    brief = _build_custom_tool_repo_brief(root, source_url=draft.source_url)
    current = {
        "name": draft.name,
        "version": draft.version,
        "commands": draft.commands,
        "warnings": draft.warnings,
    }
    prompt = f"""Refine this Thoth Custom Tool draft.

Return ONLY valid JSON. Do not wrap in markdown.

User instruction:
{instruction}

Current draft:
{json.dumps(current, indent=2)}

Rules:
- Keep 1 to 8 useful commands.
- Commands must be read-only by default and run from the tool repository root.
- Prefer cross-platform Python one-liners using `python -c "..."`.
- Use `{{query}}` when a command should accept user input.
- Do not install dependencies, call network URLs, delete/overwrite files, commit, push, clone, or modify files.

JSON schema:
{{
  "name": "Short Custom Tool name",
  "version": "1.0.0",
  "commands": [
    {{"name": "Search items", "description": "What this returns", "command": "python -c \\"...\\""}}
  ],
  "warnings": ["optional short warnings"]
}}

Repository brief:
{brief}
"""
    from langchain_core.messages import HumanMessage
    from models import get_current_model, get_llm_for

    llm = get_llm_for(model or get_current_model())
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = response.content or ""
    if isinstance(raw, list):
        raw = " ".join(block.get("text", "") if isinstance(block, dict) else str(block) for block in raw)
    data = _extract_json_object(str(raw))
    commands, validation_warnings = _validate_ai_commands(data.get("commands"))
    if not commands:
        raise ValueError("; ".join(validation_warnings) or "No safe commands generated.")
    draft.name = str(data.get("name") or draft.name).strip()[:80]
    draft.version = str(data.get("version") or draft.version or "1.0.0").strip()[:32]
    draft.commands = commands
    warnings = [str(item).strip() for item in data.get("warnings", []) if str(item).strip()] if isinstance(data.get("warnings"), list) else []
    warnings.extend(validation_warnings)
    draft.warnings = warnings
    draft.status = "draft"
    return _save_draft(draft)


def _command_from_draft(draft: CustomToolDraft, command_name: str = "") -> dict:
    if not draft.commands:
        raise ValueError("Custom Tool draft has no commands to test.")
    requested = command_name.strip().lower()
    if requested:
        for command in draft.commands:
            if str(command.get("name", "")).strip().lower() == requested:
                return command
        raise KeyError(f"Custom Tool draft command not found: {command_name}")
    return draft.commands[0]


def custom_tool_command_needs_query(command: str) -> bool:
    return "{query}" in str(command or "")


def substitute_custom_tool_query(command: str, query: str = "", *, default_query: str = "") -> str:
    text = str(command or "")
    if not custom_tool_command_needs_query(text):
        return text
    value = str(query or default_query or "")
    double_value = value.replace("\\", "\\\\").replace('"', '\\"')
    single_value = value.replace("\\", "\\\\").replace("'", "\\'")
    text = text.replace('"{query}"', f'"{double_value}"')
    text = text.replace("'{query}'", f"'{single_value}'")
    return text.replace("{query}", repr(value))


def test_custom_tool_draft_command(
    draft_id: str,
    *,
    command_name: str = "",
    approval_mode: ApprovalMode = "agent_run",
    query: str = "",
) -> CommandResult:
    draft = get_custom_tool_draft(draft_id)
    command = _command_from_draft(draft, command_name)
    command_text = substitute_custom_tool_query(
        str(command.get("command", "")),
        query,
        default_query=DEFAULT_CUSTOM_TOOL_TEST_QUERY,
    )
    result = run_workspace_command(draft.installed_path, command_text, approval_mode)
    draft.test_results[str(command.get("name", "Command"))] = {
        "command": result.command,
        "cwd": result.cwd,
        "ran": result.ran,
        "ok": result.ok,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    draft.status = "tested" if result.ok else "test_failed"
    _save_draft(draft)
    return result


def create_tool_from_draft(draft_id: str, *, overwrite: bool = False, community: bool = True) -> ToolCapsule:
    draft = get_custom_tool_draft(draft_id)
    proposal = draft.to_proposal()
    root = Path(draft.installed_path).expanduser().resolve()
    manifest_exists = any(
        (root / filename).exists()
        for filename in ("thoth-custom-tool.json", ".thoth-custom-tool.json", "custom-tool.json", "thoth-capsule.json")
    )
    if not manifest_exists or overwrite:
        write_capsule_manifest(draft.installed_path, proposal, overwrite=overwrite)
    tool = register_capsule(draft.source_url, installed_path=draft.installed_path, community=community)
    draft.created_tool_id = tool.id
    draft.status = "created"
    _save_draft(draft)
    return tool


def enable_created_custom_tool_from_draft(draft_id: str, enabled: bool = True) -> ToolCapsule:
    draft = get_custom_tool_draft(draft_id)
    if not draft.created_tool_id:
        raise ValueError("Create the Custom Tool before enabling it.")
    tool = set_capsule_enabled(draft.created_tool_id, enabled)
    draft.status = "enabled" if enabled else "created"
    _save_draft(draft)
    return tool


def promote_created_custom_tool_from_draft(draft_id: str, *, enabled: bool = True) -> ToolCapsule:
    draft = get_custom_tool_draft(draft_id)
    if not draft.created_tool_id:
        raise ValueError("Create the Custom Tool before making it available in chat.")
    tool = promote_capsule(draft.created_tool_id, enabled=enabled)
    draft.status = "promoted"
    _save_draft(draft)
    return tool


def _draft_summary(draft: CustomToolDraft) -> dict:
    return asdict(draft)


def _tool_summary(tool: ToolCapsule) -> dict:
    return asdict(tool)


def _missing_draft_response(action: str, draft_id: str = "") -> dict:
    return {
        "blocked": True,
        "needs_input": "draft",
        "action": action,
        "draft_id": draft_id,
        "message": "No Custom Tool draft is active yet. Start by inspecting a repo URL or local folder.",
    }


def _get_builder_draft_or_response(action: str, draft_id: str) -> tuple[CustomToolDraft | None, dict | None]:
    did = str(draft_id or "").strip()
    if not did:
        return None, _missing_draft_response(action, did)
    try:
        return get_custom_tool_draft(did), None
    except KeyError:
        return None, _missing_draft_response(action, did)


def custom_tool_builder(
    action: str,
    *,
    source_path: str = "",
    source_url: str = "",
    draft_id: str = "",
    instruction: str = "",
    command_name: str = "",
    fields: dict[str, Any] | None = None,
    enable: bool = True,
    overwrite: bool = False,
    delete_files: bool = False,
) -> dict:
    """Single agent-facing Custom Tool builder.

    This keeps Custom Tool creation conversational without exposing several
    near-duplicate lifecycle tools to the model.
    """
    normalized = action.strip().lower()
    fields = fields or {}
    if normalized == "list":
        return {
            "drafts": [_draft_summary(draft) for draft in list_custom_tool_drafts()],
            "tools": [_tool_summary(tool) for tool in list_capsules()],
        }
    if normalized == "start":
        path = source_path.strip()
        source = source_url.strip()
        if source and _is_public_source(source) and path:
            maybe_parent = Path(path).expanduser()
            target = maybe_parent / suggested_clone_name(source)
            if not fields.get("clone_parent") and not fields.get("clone_parent_folder"):
                if not maybe_parent.exists() or target.exists() or not _looks_like_custom_tool_source(maybe_parent):
                    fields["clone_parent"] = path
                    path = ""
        if not path and source.startswith(("http://", "https://", "git@")):
            clone_parent = str(fields.get("clone_parent", "") or fields.get("clone_parent_folder", "")).strip()
            if not clone_parent:
                return {
                    "needs_input": "clone_parent",
                    "message": "Choose a local parent folder where Thoth should clone this Custom Tool source.",
                    "source_url": source,
                }
            parent = Path(clone_parent).expanduser().resolve()
            if not parent.exists():
                if not bool(fields.get("create_clone_parent")):
                    return {
                        "needs_input": "create_clone_parent",
                        "message": f"Clone parent folder does not exist: {clone_parent}. Ask whether to create it before cloning.",
                        "source_url": source,
                        "clone_parent": str(parent),
                    }
                parent.mkdir(parents=True, exist_ok=True)
            elif not parent.is_dir():
                return {
                    "blocked": True,
                    "message": f"Clone parent exists but is not a folder: {clone_parent}",
                    "source_url": source,
                    "clone_parent": str(parent),
                }
            path = clone_capsule_repository(source, clone_parent)
        if not path:
            raise ValueError("source_path is required for action=start, or provide source_url with fields.clone_parent.")
        draft = create_custom_tool_draft(path, source_url=source_url, use_ai=True)
        return {"draft": _draft_summary(draft)}
    if normalized == "show":
        draft, response = _get_builder_draft_or_response(normalized, draft_id)
        if response:
            return response
        return {"draft": _draft_summary(draft)}
    if normalized == "refine":
        draft, response = _get_builder_draft_or_response(normalized, draft_id)
        if response:
            return response
        if not instruction.strip():
            raise ValueError("instruction is required for action=refine.")
        draft = refine_custom_tool_draft_with_llm(draft_id, instruction)
        return {"draft": _draft_summary(draft)}
    if normalized == "update":
        draft, response = _get_builder_draft_or_response(normalized, draft_id)
        if response:
            return response
        draft = update_custom_tool_draft(draft_id, fields)
        return {"draft": _draft_summary(draft)}
    if normalized == "test":
        draft, response = _get_builder_draft_or_response(normalized, draft_id)
        if response:
            return response
        result = test_custom_tool_draft_command(
            draft_id,
            command_name=command_name,
            query=str(fields.get("query", "") or fields.get("test_query", "")),
        )
        return {"draft": _draft_summary(get_custom_tool_draft(draft_id)), "result": result.__dict__}
    if normalized == "create":
        draft, response = _get_builder_draft_or_response(normalized, draft_id)
        if response:
            return response
        tool = create_tool_from_draft(draft_id, overwrite=overwrite, community=True)
        return {"draft": _draft_summary(get_custom_tool_draft(draft_id)), "tool": _tool_summary(tool)}
    if normalized == "enable":
        draft, response = _get_builder_draft_or_response(normalized, draft_id)
        if response:
            return response
        tool = enable_created_custom_tool_from_draft(draft_id, enabled=enable)
        return {"draft": _draft_summary(get_custom_tool_draft(draft_id)), "tool": _tool_summary(tool)}
    if normalized == "promote":
        draft, response = _get_builder_draft_or_response(normalized, draft_id)
        if response:
            return response
        tool = promote_created_custom_tool_from_draft(draft_id, enabled=True)
        return {"draft": _draft_summary(get_custom_tool_draft(draft_id)), "tool": _tool_summary(tool)}
    if normalized == "delete":
        if draft_id:
            draft = get_custom_tool_draft(draft_id)
            if draft.created_tool_id:
                remove_capsule(draft.created_tool_id, delete_files=delete_files)
            delete_custom_tool_draft(draft_id)
            return {"deleted_draft_id": draft_id, "deleted_files": bool(delete_files)}
        tool_id = str(fields.get("tool_id", "")).strip()
        if not tool_id:
            raise ValueError("draft_id or fields.tool_id is required for action=delete.")
        remove_capsule(tool_id, delete_files=delete_files)
        return {"deleted_tool_id": tool_id, "deleted_files": bool(delete_files)}
    raise ValueError(f"Unknown Custom Tool builder action: {action}")


def set_custom_tool_enabled(tool_id: str, enabled: bool) -> ToolCapsule:
    return set_capsule_enabled(tool_id, enabled)


def promote_custom_tool(tool_id: str, *, enabled: bool = True) -> ToolCapsule:
    return promote_capsule(tool_id, enabled=enabled)


def remove_custom_tool(tool_id: str, *, delete_files: bool = False) -> None:
    remove_capsule(tool_id, delete_files=delete_files)


def run_custom_tool_command(
    tool_id: str,
    command: str,
    approval_mode: ApprovalMode,
    *,
    require_enabled: bool = True,
) -> CommandResult:
    return run_capsule_command(tool_id, command, approval_mode, require_enabled=require_enabled)


def classify_custom_tool_command(command: str) -> dict:
    action = classify_command_action(command)
    decision = decide_action("agent_run", action)  # type: ignore[arg-type]
    label_by_action = {
        "run_safe_command": "Local",
        "run_network": "Network",
        "run_install": "Install",
        "start_server": "Server",
    }
    return {
        "action": action,
        "label": label_by_action.get(action, "Review"),
        "requires_approval": decision.requires_approval,
        "blocked": decision.decision == "block",
        "reason": decision.reason,
    }


def _custom_tool_workspace(tool: ToolCapsule, *, network: bool = False) -> DeveloperWorkspace:
    return DeveloperWorkspace(
        id=f"custom-tool-{tool.id}",
        name=tool.name,
        path=tool.installed_path,
        repo_url=tool.source_url,
        approval_mode="agent_run",
        execution_mode="docker",
        sandbox_network="on" if network else "off",
        sandbox_image=DEFAULT_SANDBOX_IMAGE,
        trusted=False,
    )


def _run_custom_tool_local_direct(tool: ToolCapsule, command: str, decision) -> CommandResult:
    root = Path(tool.installed_path).expanduser().resolve()
    completed = subprocess.run(
        split_command(command),
        cwd=str(root),
        shell=False,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return CommandResult(
        command=command,
        cwd=str(root),
        returncode=completed.returncode,
        stdout=(completed.stdout or "")[-20_000:],
        stderr=(completed.stderr or "")[-20_000:],
        decision=decision,
        execution_mode="local",
    )


def run_custom_tool_test_command(
    tool_id: str,
    command: str,
    *,
    query: str = "",
    approved_once: bool = False,
    require_enabled: bool = False,
) -> CommandResult:
    tool = next((item for item in list_capsules() if item.id == tool_id), None)
    if tool is None:
        raise KeyError(f"Custom Tool not found: {tool_id}")
    if require_enabled and not tool.enabled:
        raise PermissionError(f"Custom Tool is disabled: {tool.name}")

    command = substitute_custom_tool_query(command, query, default_query=DEFAULT_CUSTOM_TOOL_TEST_QUERY)
    action = classify_command_action(command)
    decision = decide_action("agent_run", action)  # type: ignore[arg-type]
    if decision.requires_approval and not approved_once:
        return CommandResult(
            command=command,
            cwd=tool.installed_path,
            returncode=None,
            stderr=decision.reason,
            decision=decision,
        )

    if approved_once and action in {"run_network", "run_install", "start_server"}:
        probe = detect_container_runtime()
        if probe.available:
            workspace = _custom_tool_workspace(tool, network=action in {"run_network", "run_install"})
            outcome = run_docker_sandbox_command(
                workspace,
                command,
                thread_id=f"custom-tool-test-{tool.id}",
                timeout=120,
            )
            if outcome.ran:
                return CommandResult(
                    command=command,
                    cwd=outcome.cwd,
                    returncode=outcome.returncode,
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    decision=decision,
                    changed_files=outcome.changed_files,
                    execution_mode="docker",
                    sandbox_backend=outcome.sandbox_backend,
                    sandbox_pending_change_id=outcome.pending_change_id,
                )

    if approved_once and decision.requires_approval:
        decision = ApprovalDecision("allow", "User approved this Custom Tool test run once.")
        return _run_custom_tool_local_direct(tool, command, decision)
    return run_workspace_command(tool.installed_path, command, "agent_run")


def _friendly_capsule_name(root: Path, source_url: str = "") -> str:
    base = suggested_clone_name(source_url) if source_url else root.name
    words = re.split(r"[-_.\s]+", base)
    title = " ".join(word.capitalize() for word in words if word)
    return f"{title or 'Tool'} Custom Tool"


def _first_existing(root: Path, names: list[str]) -> Path | None:
    for name in names:
        path = root / name
        if path.exists() and path.is_file():
            return path
    return None


def register_capsule(
    source_url: str,
    *,
    name: str = "",
    version: str = "",
    installed_path: str = "",
    community: bool = True,
) -> ToolCapsule:
    source = source_url.strip()
    if not source:
        raise ValueError("Custom Tool source URL is required.")
    capsule_name = name.strip() or Path(source.rstrip("/")).stem or "Custom Tool"
    capsule_id = _capsule_id(capsule_name)
    root = CAPSULE_INSTALL_ROOT / capsule_id
    path = Path(installed_path).expanduser().resolve() if installed_path else root
    if installed_path and not path.exists():
        raise ValueError(f"Installed Custom Tool path does not exist: {path}")
    manifest = parse_capsule_manifest(str(path)) if path.exists() else {}
    capsule_name = name.strip() or manifest.get("name") or Path(source.rstrip("/")).stem or "Custom Tool"
    capsule_id = _capsule_id(capsule_name)

    capsule = ToolCapsule(
        id=capsule_id,
        name=capsule_name,
        source_url=source,
        installed_path=str(path),
        version=version or manifest.get("version", ""),
        enabled=False,
        community=community,
        commands=manifest.get("commands", []),
    )
    data = _load_state()
    capsules = [item for item in data.get("capsules", []) if item.get("id") != capsule.id]
    capsules.append(asdict(capsule))
    data["capsules"] = capsules
    _save_state(data)
    return capsule


def set_capsule_enabled(capsule_id: str, enabled: bool) -> ToolCapsule:
    data = _load_state()
    updated: ToolCapsule | None = None
    capsules = []
    for item in data.get("capsules", []):
        if item.get("id") == capsule_id:
            item = dict(item)
            item["enabled"] = bool(enabled)
            updated = ToolCapsule(**item)
        capsules.append(item)
    if updated is None:
        raise KeyError(f"Custom Tool not found: {capsule_id}")
    data["capsules"] = capsules
    _save_state(data)
    return updated


def promote_capsule(capsule_id: str, *, enabled: bool = True) -> ToolCapsule:
    """Promote a tested Custom Tool into the normal plugin/tool surface."""
    from datetime import datetime, timezone
    from plugins import registry as plugin_registry
    from plugins import state as plugin_state

    data = _load_state()
    updated: ToolCapsule | None = None
    capsules = []
    for item in data.get("capsules", []):
        if item.get("id") == capsule_id:
            item = dict(item)
            capsule = ToolCapsule(**item)
            if not capsule.commands:
                raise ValueError("Custom Tool has no commands to promote.")
            if not Path(capsule.installed_path).expanduser().exists():
                raise ValueError(f"Custom Tool path does not exist: {capsule.installed_path}")
            item["enabled"] = True
            item["promoted_plugin_id"] = promoted_plugin_id(capsule.id)
            item["promoted_at"] = datetime.now(timezone.utc).isoformat()
            updated = ToolCapsule(**item)
            plugin_state.set_plugin_enabled(updated.promoted_plugin_id, enabled)
            plugin_registry.unregister_plugin(updated.promoted_plugin_id)
        capsules.append(item)
    if updated is None:
        raise KeyError(f"Custom Tool not found: {capsule_id}")
    data["capsules"] = capsules
    _save_state(data)
    register_promoted_capsules_with_plugins()
    return updated


def remove_promoted_capsule_tool(capsule_id: str) -> None:
    """Remove the promoted plugin wrapper while preserving capsule files."""
    from plugins import registry as plugin_registry
    from plugins import state as plugin_state

    data = _load_state()
    capsules = []
    removed_plugin_id = ""
    for item in data.get("capsules", []):
        if item.get("id") == capsule_id:
            item = dict(item)
            removed_plugin_id = str(item.get("promoted_plugin_id") or promoted_plugin_id(capsule_id))
            item["promoted_plugin_id"] = ""
            item["promoted_at"] = ""
        capsules.append(item)
    data["capsules"] = capsules
    _save_state(data)
    if removed_plugin_id:
        plugin_registry.unregister_plugin(removed_plugin_id)
        plugin_state.remove_plugin_state(removed_plugin_id)


def list_promoted_capsules() -> list[ToolCapsule]:
    return [capsule for capsule in list_capsules() if capsule.promoted_plugin_id]


def remove_capsule(capsule_id: str, *, delete_files: bool = False) -> None:
    data = _load_state()
    removed: dict | None = None
    kept = []
    for item in data.get("capsules", []):
        if item.get("id") == capsule_id:
            removed = item
            continue
        kept.append(item)
    data["capsules"] = kept
    _save_state(data)
    removed_plugin_id = str((removed or {}).get("promoted_plugin_id", ""))
    if removed_plugin_id:
        try:
            from plugins import registry as plugin_registry
            from plugins import state as plugin_state

            plugin_registry.unregister_plugin(removed_plugin_id)
            plugin_state.remove_plugin_state(removed_plugin_id)
        except Exception:
            pass
    if delete_files and removed:
        path = Path(str(removed.get("installed_path", ""))).expanduser().resolve()
        root = CAPSULE_INSTALL_ROOT.resolve()
        if root in path.parents or path == root:
            shutil.rmtree(path, ignore_errors=True)


def run_capsule_command(
    capsule_id: str,
    command: str,
    approval_mode: ApprovalMode,
    *,
    require_enabled: bool = True,
) -> CommandResult:
    capsule = next((item for item in list_capsules() if item.id == capsule_id), None)
    if capsule is None:
        raise KeyError(f"Custom Tool not found: {capsule_id}")
    if require_enabled and not capsule.enabled:
        raise PermissionError(f"Custom Tool is disabled: {capsule.name}")
    return run_workspace_command(capsule.installed_path, command, approval_mode)


class _CapsuleCommandInput(BaseModel):
    query: str = Field(default="", description="Optional user input for the capsule command.")


def _result_to_text(result: CommandResult) -> str:
    payload = {
        "command": result.command,
        "cwd": result.cwd,
        "ran": result.ran,
        "ok": result.ok,
        "returncode": result.returncode,
        "stdout": result.stdout[-12000:],
        "stderr": result.stderr[-12000:],
        "changed_files": result.changed_files,
    }
    if result.decision:
        payload["decision"] = {
            "decision": result.decision.decision,
            "reason": result.decision.reason,
        }
    return json.dumps(payload, indent=2)


class CapsulePluginTool:
    def __init__(self, capsule: ToolCapsule):
        self.capsule = capsule

    @property
    def name(self) -> str:
        return self.capsule.promoted_plugin_id or promoted_plugin_id(self.capsule.id)

    @property
    def display_name(self) -> str:
        return self.capsule.name

    @property
    def description(self) -> str:
        return f"Promoted Custom Tool from {self.capsule.source_url}"

    @property
    def destructive_tool_names(self) -> set[str]:
        return {
            _command_tool_name(self.capsule, command)
            for command in self.capsule.commands
        }

    @property
    def background_allowed_tool_names(self) -> set[str]:
        return set()

    def execute(self, query: str) -> str:
        if not self.capsule.commands:
            return "Custom Tool has no commands."
        command = self.capsule.commands[0]
        return _run_promoted_capsule_command(self.capsule, command, query)

    def as_langchain_tools(self) -> list:
        tools = []
        for command in self.capsule.commands:
            tool_name = _command_tool_name(self.capsule, command)
            command_label = str(command.get("name", tool_name))
            command_text = str(command.get("command", ""))
            description = str(command.get("description", "")).strip()

            def _run(query: str = "", *, _command=command) -> str:
                return _run_promoted_capsule_command(self.capsule, _command, query)

            tools.append(
                StructuredTool.from_function(
                    func=_run,
                    name=tool_name,
                    description=(
                        f"{self.capsule.name} / {command_label}: "
                        f"{description or command_text}. Runs in the capsule folder."
                    ),
                    args_schema=_CapsuleCommandInput,
                )
            )
        return tools


def _run_promoted_capsule_command(capsule: ToolCapsule, command: dict[str, Any], query: str = "") -> str:
    command_text = str(command.get("command", "")).strip()
    if not command_text:
        return "Custom Tool command is empty."
    command_text = substitute_custom_tool_query(command_text, query)
    result = run_capsule_command(
        capsule.id,
        command_text,
        "agent_run",
        require_enabled=False,
    )
    return _result_to_text(result)


def _build_manifest(capsule: ToolCapsule):
    from plugins.manifest import PluginAuthor, PluginManifest, PluginProvides

    provides = PluginProvides(
        tools=[
            {
                "name": _command_tool_name(capsule, command),
                "description": str(command.get("description") or command.get("command") or ""),
            }
            for command in capsule.commands
        ],
        skills=[],
    )
    return PluginManifest(
        id=capsule.promoted_plugin_id or promoted_plugin_id(capsule.id),
        name=capsule.name,
        version="1.0.0",
        min_thoth_version="0.1.0",
        author=PluginAuthor(name="Custom Tool"),
        description=f"Promoted Custom Tool from {capsule.source_url}",
        icon="extension",
        tags=["custom-tool"],
        repository=capsule.source_url,
        provides=provides,
        path=Path(capsule.installed_path),
    )


def register_promoted_capsules_with_plugins() -> list[str]:
    """Register promoted Custom Tools as synthetic plugin tools."""
    from plugins import registry as plugin_registry
    from plugins import state as plugin_state

    warnings: list[str] = []
    for capsule in list_promoted_capsules():
        plugin_id = capsule.promoted_plugin_id
        if not plugin_id:
            continue
        manifest = _build_manifest(capsule)
        plugin_registry.unregister_plugin(plugin_id)
        tools = [CapsulePluginTool(capsule)] if plugin_state.is_plugin_enabled(plugin_id) else []
        warnings.extend(plugin_registry.register_plugin(manifest, tools=tools, skills=[]))
    return warnings
