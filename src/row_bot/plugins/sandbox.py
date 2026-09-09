"""Plugin dependency safety — core dependency protection.

Before installing plugin dependencies, we freeze the core dependency
versions and block any plugin that would downgrade or change them.
"""

from __future__ import annotations

import logging
import json
import pathlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from row_bot.runtime_paths import app_path

logger = logging.getLogger(__name__)


@dataclass
class DepCheckResult:
    """Result of a dependency compatibility check."""
    ok: bool
    conflicts: list[str]  # list of conflict descriptions
    warnings: list[str]   # plugin-to-plugin conflicts (non-blocking)


# ── Core Requirements ────────────────────────────────────────────────────────
_core_requirements: dict[str, str] | None = None  # package_name → installed_version


def _get_core_requirements() -> dict[str, str]:
    """Get the current frozen core dependencies.

    Reads from requirements.txt to identify core packages, then checks
    installed versions via importlib.metadata.
    """
    global _core_requirements
    # Installation is an authority boundary: an updater or repair may have
    # changed the environment since a previous Plugin Center read.

    # Read requirements.txt to get the list of core package names
    req_path = app_path("requirements.txt")
    core_names: set[str] = set()
    if req_path.exists():
        for line in req_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            # Extract package name (before any version specifier)
            match = re.match(r"([a-zA-Z0-9_\-\.]+(?:\[[^\]]+\])?)", line)
            if match:
                pkg = match.group(1).split("[")[0]  # strip extras like [gmail]
                core_names.add(pkg.lower().replace("-", "_").replace(".", "_"))

    # Get installed versions
    from importlib.metadata import distributions
    installed: dict[str, str] = {}
    for dist in distributions():
        name = dist.metadata["Name"]
        if name:
            normalised = name.lower().replace("-", "_").replace(".", "_")
            if normalised in core_names:
                installed[normalised] = dist.metadata["Version"]

    _core_requirements = installed
    return _core_requirements


def check_dependencies(plugin_deps: list[str]) -> DepCheckResult:
    """Check if plugin dependencies conflict with core Row-Bot packages.

    Uses pip's dependency resolver in dry-run mode to detect conflicts.

    Parameters
    ----------
    plugin_deps
        List of pip requirement strings (e.g. ["requests>=2.28", "boto3"]).

    Returns
    -------
    DepCheckResult with ok=False if any core dependency would change.
    """
    if not plugin_deps:
        return DepCheckResult(ok=True, conflicts=[], warnings=[])

    return _resolve_dependencies(plugin_deps)[0]


def _resolve_dependencies(plugin_deps: list[str]) -> tuple[DepCheckResult, dict[str, str]]:
    """Verify the complete resolver plan against immutable host constraints.

    Exit status alone is not evidence of compatibility. No unverified plan may
    reach installation; the installer uses these same constraints and pins.
    """
    core = {canonicalize_name(k): v for k, v in _get_core_requirements().items()}
    conflicts: list[str] = []
    warnings: list[str] = []
    pins = dict(core)
    try:
        if not core:
            raise ValueError("Host dependency constraints unavailable")
        for value in plugin_deps:
            req = Requirement(value)
            name = canonicalize_name(req.name)
            if req.url:
                raise ValueError("Direct dependency references require isolated installation")
            if name in core and (not req.marker or req.marker.evaluate()):
                if not req.specifier.contains(core[name], prereleases=True):
                    raise ValueError("Plugin requirement conflicts with host constraints")
        with tempfile.TemporaryDirectory(prefix="rb-deps-") as tmp:
            constraint = pathlib.Path(tmp) / "constraints.txt"
            report = pathlib.Path(tmp) / "report.json"
            constraint.write_text(_constraint_text(core), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--dry-run", "--no-input",
                 "--disable-pip-version-check", "--constraint", str(constraint),
                 "--report", str(report), *plugin_deps],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                raise ValueError("Dependency resolver did not verify compatibility")
            plan = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(plan, dict) or plan.get("version") != "1" or not isinstance(plan.get("install"), list):
            raise ValueError("Dependency resolver report is invalid")
        for item in plan["install"]:
            metadata = item["metadata"]
            name = canonicalize_name(metadata["name"])
            version = str(Version(metadata["version"]))
            if name in pins and Version(pins[name]) != Version(version):
                raise ValueError("Resolver plan changes a constrained distribution")
            pins[name] = version
            # Even an exit-zero report may carry prohibited transitive edges.
            for value in metadata.get("requires_dist", []):
                req = Requirement(value)
                dep = canonicalize_name(req.name)
                if req.url:
                    raise ValueError("Transitive direct references require isolated installation")
                if dep in core and (not req.marker or req.marker.evaluate()):
                    if req.url or not req.specifier.contains(core[dep], prereleases=True):
                        raise ValueError("Resolver plan conflicts with host dependencies")
    except Exception:
        conflicts.append("Dependency compatibility could not be verified; installation blocked")
    return DepCheckResult(not conflicts, conflicts, warnings), pins


def _constraint_text(pins: dict[str, str]) -> str:
    return "".join(f"{name}=={version}\n" for name, version in sorted(pins.items()))


def install_dependencies(plugin_deps: list[str]) -> tuple[bool, str]:
    """Install plugin dependencies into the main venv.

    Returns (success, message).
    """
    if not plugin_deps:
        return True, "No dependencies to install"

    # Safety check first
    check, pins = _resolve_dependencies(plugin_deps)
    if not check.ok:
        return False, (
            "Cannot install — conflicts with core Row-Bot dependencies:\n"
            + "\n".join(f"  - {c}" for c in check.conflicts)
        )

    try:
        with tempfile.TemporaryDirectory(prefix="rb-deps-") as tmp:
            constraint = pathlib.Path(tmp) / "constraints.txt"
            constraint.write_text(_constraint_text(pins), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-input", "--no-deps",
                 "--disable-pip-version-check", "--constraint", str(constraint),
                 *plugin_deps, *(f"{name}=={version}" for name, version in sorted(pins.items()))],
                capture_output=True, text=True, timeout=300,
            )
        if result.returncode == 0:
            return True, "Dependencies installed successfully"
        else:
            return False, f"pip install failed:\n{result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, "pip install timed out after 5 minutes"
    except Exception as exc:
        return False, f"pip install error: {exc}"


# ── Reset (for testing) ─────────────────────────────────────────────────────
def _reset():
    global _core_requirements
    _core_requirements = None
