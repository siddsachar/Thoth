"""Plugin loader — discovery, validation, and lifecycle.

Scans ``~/.thoth/installed_plugins/`` for plugin directories, validates
their manifests, and loads enabled plugins safely (try/except + timeout).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import pathlib
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from plugins.manifest import PluginManifest, ManifestError, parse_manifest
from plugins.api import PluginAPI, PluginTool
from plugins import registry as plugin_registry
from plugins import state as plugin_state

logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
PLUGINS_DIR = DATA_DIR / "installed_plugins"

# Timeout for plugin register() calls (seconds)
REGISTER_TIMEOUT = 5.0

# Characters not allowed in plugin code (security scan)
_DANGEROUS_PATTERNS = [
    re.compile(r'\beval\s*\('),
    re.compile(r'\bexec\s*\('),
    re.compile(r'\bos\.system\s*\('),
    re.compile(r'\bsubprocess\b'),
    re.compile(r'\b__import__\s*\('),
]

# Core modules plugins must not import
_FORBIDDEN_IMPORTS = {
    "tools", "tools.base", "tools.registry",
    "agent", "app", "models", "prompts",
    "knowledge_graph", "memory_extraction", "dream_cycle",
    "threads", "tasks", "documents",
    "ui", "ui.settings", "ui.streaming", "ui.chat",
    "ui.render", "ui.helpers", "ui.home",
}


@dataclass
class LoadResult:
    """Summary of a plugin load attempt."""
    plugin_id: str
    success: bool
    manifest: PluginManifest | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)


# ── Module-level state ───────────────────────────────────────────────────────
_load_results: list[LoadResult] = []


# ── Public API ───────────────────────────────────────────────────────────────
def load_plugins() -> list[LoadResult]:
    """Discover and load all installed plugins. Safe to call multiple times.

    Returns a list of LoadResult objects summarising each plugin attempt.
    """
    global _load_results
    _load_results = []

    # Re-read state from disk so enable/disable changes are picked up
    plugin_state.reload()

    if not PLUGINS_DIR.exists():
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Created plugins directory: %s", PLUGINS_DIR)

    for entry in sorted(PLUGINS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith((".", "_")):
            continue

        result = _load_single_plugin(entry)
        _load_results.append(result)

        if result.success:
            logger.info("✅ Plugin '%s' v%s loaded (%d tools, %d skills)",
                         result.plugin_id,
                         result.manifest.version if result.manifest else "?",
                         result.manifest.tool_count if result.manifest else 0,
                         result.manifest.skill_count if result.manifest else 0)
        else:
            logger.warning("❌ Plugin '%s' failed to load: %s",
                            result.plugin_id, result.error)

        for w in result.warnings:
            logger.warning("⚠️  %s", w)

    try:
        from developer.tool_capsules import (
            list_promoted_capsules,
            register_promoted_capsules_with_plugins,
        )

        capsule_warnings = register_promoted_capsules_with_plugins()
        for capsule in list_promoted_capsules():
            _load_results.append(
                LoadResult(
                    plugin_id=capsule.promoted_plugin_id,
                    success=True,
                    manifest=plugin_registry.get_manifest(capsule.promoted_plugin_id),
                    warnings=[
                        "Custom Tool plugin wrapper; source files remain in the tool folder.",
                    ],
                )
            )
        for warning in capsule_warnings:
            logger.warning("Custom Tool plugin warning: %s", warning)
    except Exception as exc:
        logger.debug("Custom Tool plugin registration skipped: %s", exc)

    loaded = sum(1 for r in _load_results if r.success)
    failed = sum(1 for r in _load_results if not r.success)
    logger.info("Plugin loading complete: %d loaded, %d failed", loaded, failed)

    return _load_results


def get_load_summary() -> dict:
    """Return a summary dict for the status bar / UI."""
    return {
        "total": len(_load_results),
        "loaded": sum(1 for r in _load_results if r.success),
        "failed": sum(1 for r in _load_results if not r.success),
        "results": _load_results,
    }


def get_load_results() -> list[LoadResult]:
    return list(_load_results)


# ── Single Plugin Loader ─────────────────────────────────────────────────────
def _load_single_plugin(plugin_dir: pathlib.Path) -> LoadResult:
    """Load a single plugin from its directory. Never raises."""
    plugin_id = plugin_dir.name

    # Step 1: Parse manifest
    try:
        manifest = parse_manifest(plugin_dir)
    except ManifestError as exc:
        return LoadResult(plugin_id=plugin_id, success=False, error=str(exc))
    except Exception as exc:
        return LoadResult(plugin_id=plugin_id, success=False,
                          error=f"Unexpected error parsing manifest: {exc}")

    # Step 2: Check Thoth version compatibility
    compat_err = _check_version_compat(manifest)
    if compat_err:
        return LoadResult(plugin_id=plugin_id, success=False,
                          manifest=manifest, error=compat_err)

    # Step 3: Check if enabled
    if not plugin_state.is_plugin_enabled(plugin_id):
        # Still register the manifest so the plugin appears in the UI
        # (users need to see the card to re-enable the plugin).
        plugin_registry.register_plugin(
            manifest=manifest, tools=[], skills=[],
        )
        return LoadResult(
            plugin_id=plugin_id, success=True, manifest=manifest,
            warnings=["Plugin is disabled — skipping tool/skill registration"]
        )

    # Step 4: Security scan
    sec_err = _security_scan(plugin_dir)
    if sec_err:
        return LoadResult(plugin_id=plugin_id, success=False,
                          manifest=manifest, error=sec_err)

    # Step 5: Import and call register()
    try:
        api = PluginAPI(
            plugin_id=plugin_id,
            plugin_dir=plugin_dir,
            state_backend=plugin_state,
        )
        _call_register_with_timeout(plugin_dir, api)
    except TimeoutError:
        return LoadResult(
            plugin_id=plugin_id, success=False, manifest=manifest,
            error=f"Plugin register() timed out after {REGISTER_TIMEOUT}s"
        )
    except Exception as exc:
        return LoadResult(
            plugin_id=plugin_id, success=False, manifest=manifest,
            error=f"Plugin register() crashed: {exc}"
        )

    # Step 6: Discover skills from skills/ directory
    # Step 7: Register with plugin registry
    try:
        skills = _discover_plugin_skills(plugin_dir)
        for skill in skills:
            api.register_skill(skill)

        warnings = plugin_registry.register_plugin(
            manifest=manifest,
            tools=api._registered_tools,
            skills=api._registered_skills,
        )
    except Exception as exc:
        return LoadResult(
            plugin_id=plugin_id, success=False, manifest=manifest,
            error=f"Skill discovery / registry failed: {exc}"
        )

    return LoadResult(
        plugin_id=plugin_id, success=True, manifest=manifest,
        warnings=warnings,
    )


# ── Version Check ────────────────────────────────────────────────────────────
def _check_version_compat(manifest: PluginManifest) -> str | None:
    """Check min_thoth_version. Returns error string or None."""
    try:
        from importlib.metadata import version as pkg_version
        # Try to get Thoth's version from various sources
        thoth_version = _get_thoth_version()
        if thoth_version and manifest.min_thoth_version:
            if _version_tuple(thoth_version) < _version_tuple(manifest.min_thoth_version):
                return (
                    f"Requires Thoth >= {manifest.min_thoth_version}, "
                    f"but current version is {thoth_version}"
                )
    except Exception:
        pass  # Skip version check if we can't determine Thoth version
    return None


def _get_thoth_version() -> str | None:
    """Try to get Thoth's current version."""
    # Check if there's a VERSION file or version in app.py
    version_file = pathlib.Path(__file__).parent.parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    # Try to extract from RELEASE_NOTES.md
    rn = pathlib.Path(__file__).parent.parent / "RELEASE_NOTES.md"
    if rn.exists():
        try:
            with open(rn, "r", encoding="utf-8") as f:
                for line in f:
                    m = re.search(r"##\s+v(\d+\.\d+\.\d+)", line)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    return None


def _version_tuple(v: str) -> tuple[int, ...]:
    """Convert '3.12.0' to (3, 12, 0)."""
    return tuple(int(x) for x in v.split(".") if x.isdigit())


# ── Security Scan ────────────────────────────────────────────────────────────
def _security_scan(plugin_dir: pathlib.Path) -> str | None:
    """Scan plugin Python files for dangerous patterns.

    Returns error string or None.
    """
    for py_file in plugin_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue

        # Check for dangerous function calls
        for pattern in _DANGEROUS_PATTERNS:
            match = pattern.search(content)
            if match:
                rel_path = py_file.relative_to(plugin_dir)
                return (
                    f"Security violation in {rel_path}: "
                    f"forbidden pattern '{match.group()}' detected. "
                    f"Plugins must not use eval(), exec(), os.system(), "
                    f"subprocess, or __import__()."
                )

        # Check for forbidden core imports
        for line_no, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for forbidden in _FORBIDDEN_IMPORTS:
                # Match: import agent, from agent import ..., import tools.registry
                if (re.search(rf'\bimport\s+{re.escape(forbidden)}\b', stripped)
                        or re.search(rf'\bfrom\s+{re.escape(forbidden)}\b', stripped)):
                    # Allow "from plugins.api import" (that's the expected import)
                    if forbidden.startswith("plugins"):
                        continue
                    rel_path = py_file.relative_to(plugin_dir)
                    return (
                        f"Security violation in {rel_path}:{line_no}: "
                        f"plugins must not import core module '{forbidden}'. "
                        f"Use the PluginAPI object instead."
                    )

    return None


# ── Plugin Registration ──────────────────────────────────────────────────────
def _call_register_with_timeout(plugin_dir: pathlib.Path, api: PluginAPI) -> None:
    """Import plugin_main.py and call register(api) with timeout."""
    main_path = plugin_dir / "plugin_main.py"
    if not main_path.exists():
        raise FileNotFoundError(f"Missing plugin_main.py in {plugin_dir}")

    # Add plugin dir to sys.path temporarily for local imports
    plugin_dir_str = str(plugin_dir)
    if plugin_dir_str not in sys.path:
        sys.path.insert(0, plugin_dir_str)

    error_holder: list[Exception] = []

    def _do_register():
        try:
            spec = importlib.util.spec_from_file_location(
                f"_thoth_plugin_{api.plugin_id}", main_path
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot create module spec for {main_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            register_fn = getattr(module, "register", None)
            if register_fn is None:
                raise AttributeError("plugin_main.py has no register() function")

            register_fn(api)
        except Exception as exc:
            error_holder.append(exc)

    thread = threading.Thread(target=_do_register, daemon=True)
    thread.start()
    thread.join(timeout=REGISTER_TIMEOUT)

    # Restore sys.path
    if plugin_dir_str in sys.path:
        sys.path.remove(plugin_dir_str)

    if thread.is_alive():
        raise TimeoutError(f"register() timed out after {REGISTER_TIMEOUT}s")

    if error_holder:
        raise error_holder[0]


# ── Skill Discovery ─────────────────────────────────────────────────────────
def _discover_plugin_skills(plugin_dir: pathlib.Path) -> list[dict]:
    """Discover SKILL.md files in plugin's skills/ directory."""
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        return []

    import yaml
    skills: list[dict] = []

    for skill_dir in sorted(skills_dir.iterdir()):
        skill_md = None
        if skill_dir.is_dir():
            skill_md = skill_dir / "SKILL.md"
        elif skill_dir.is_file() and skill_dir.name == "SKILL.md":
            skill_md = skill_dir

        if skill_md is None or not skill_md.exists():
            continue

        try:
            content = skill_md.read_text(encoding="utf-8")
            # Parse YAML frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                    instructions = parts[2].strip()
                else:
                    frontmatter = {}
                    instructions = content
            else:
                frontmatter = {}
                instructions = content

            skill_name = frontmatter.get("name", skill_dir.stem if skill_dir.is_dir() else "unnamed")
            skills.append({
                "name": skill_name,
                "display_name": frontmatter.get("display_name", skill_name),
                "icon": frontmatter.get("icon", "🔌"),
                "description": frontmatter.get("description", ""),
                "instructions": instructions,
            })
        except Exception as exc:
            logger.warning("Failed to parse skill %s: %s", skill_md, exc)

    return skills


# ── Reset (for testing) ─────────────────────────────────────────────────────
def _reset():
    """Reset load state. For testing only."""
    global _load_results
    _load_results = []
