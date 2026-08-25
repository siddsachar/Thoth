"""Version-matched, user-triggered managed Chromium runtime lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata, resources
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from typing import Any, Callable, Mapping
from uuid import uuid4


RUNTIME_ID = "playwright-chrome"
MANIFEST_SCHEMA = 1


@dataclass(frozen=True)
class PlaywrightContract:
    package_version: str
    chromium_revision: str
    chromium_version: str


@dataclass(frozen=True)
class BrowserRuntimeReadiness:
    ready: bool
    code: str
    message: str
    executable_path: str = ""
    browsers_dir: str = ""
    package_version: str = ""
    chromium_revision: str = ""
    chromium_version: str = ""


@dataclass(frozen=True)
class BrowserRuntimeInstallResult:
    ok: bool
    message: str
    executable_path: str = ""
    browsers_dir: str = ""
    package_version: str = ""
    chromium_revision: str = ""


def installed_playwright_contract() -> PlaywrightContract:
    """Return the installed Python package's reviewed browser contract."""

    version = metadata.version("playwright")
    manifest_path = resources.files("playwright").joinpath("driver", "package", "browsers.json")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    chromium = next(
        item for item in data.get("browsers", [])
        if item.get("name") == "chromium" and bool(item.get("installByDefault", True))
    )
    return PlaywrightContract(
        package_version=version,
        chromium_revision=str(chromium.get("revision") or ""),
        chromium_version=str(chromium.get("browserVersion") or ""),
    )


def runtime_root() -> Path:
    from row_bot.mcp_client.requirements import RUNTIMES_DIR

    return Path(RUNTIMES_DIR) / RUNTIME_ID


def manifest_path(root: Path | None = None) -> Path:
    return (root or runtime_root()) / "manifest.json"


def _platform_id() -> str:
    return platform.system().casefold()


def _arch_id() -> str:
    return platform.machine().casefold()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, indent=2, sort_keys=True)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _contained(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def read_runtime_manifest(root: Path | None = None) -> dict[str, Any]:
    return _read_manifest(manifest_path(root))


def check_managed_browser_runtime(
    root: Path | None = None,
    *,
    contract: PlaywrightContract | None = None,
) -> BrowserRuntimeReadiness:
    """Readiness is side-effect free: no launch, process probe, or download."""

    selected_root = root or runtime_root()
    selected_contract = contract or installed_playwright_contract()
    manifest = read_runtime_manifest(selected_root)
    if not manifest:
        return BrowserRuntimeReadiness(False, "missing", "Install the managed Chromium runtime explicitly.")
    expected = {
        "schema": MANIFEST_SCHEMA,
        "owner": "row-bot-python-playwright",
        "browser": "chromium",
        "package_version": selected_contract.package_version,
        "chromium_revision": selected_contract.chromium_revision,
        "chromium_version": selected_contract.chromium_version,
        "platform": _platform_id(),
        "arch": _arch_id(),
        "complete": True,
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            return BrowserRuntimeReadiness(
                False,
                "runtime_mismatch",
                "The managed Chromium runtime does not match the installed Python Playwright package.",
                package_version=selected_contract.package_version,
                chromium_revision=selected_contract.chromium_revision,
                chromium_version=selected_contract.chromium_version,
            )
    browsers_dir = Path(str(manifest.get("browsers_dir") or ""))
    executable = Path(str(manifest.get("executable_path") or ""))
    if not browsers_dir.is_dir() or not executable.is_file():
        return BrowserRuntimeReadiness(False, "missing", "The managed Chromium runtime is incomplete.")
    if not _contained(browsers_dir, selected_root) or not _contained(executable, browsers_dir):
        return BrowserRuntimeReadiness(False, "runtime_mismatch", "The managed Chromium manifest contains an unsafe path.")
    return BrowserRuntimeReadiness(
        True,
        "ready",
        "Managed Chromium is ready.",
        str(executable),
        str(browsers_dir),
        selected_contract.package_version,
        selected_contract.chromium_revision,
        selected_contract.chromium_version,
    )


def check_packaged_browser_runtime(
    env: Mapping[str, str] | None = None,
    *,
    contract: PlaywrightContract | None = None,
) -> BrowserRuntimeReadiness:
    """Validate an installer/server-image browser owned by this Python package."""

    environment = os.environ if env is None else env
    value = str(environment.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if not value:
        return BrowserRuntimeReadiness(False, "missing", "No packaged Chromium runtime is configured.")
    browsers_dir = Path(value)
    selected_contract = contract or installed_playwright_contract()
    executable = _find_chromium_executable(browsers_dir, selected_contract.chromium_revision)
    if executable is None or not _contained(executable, browsers_dir):
        return BrowserRuntimeReadiness(
            False,
            "runtime_mismatch",
            "The packaged Chromium runtime does not match the installed Python Playwright package.",
            package_version=selected_contract.package_version,
            chromium_revision=selected_contract.chromium_revision,
            chromium_version=selected_contract.chromium_version,
        )
    return BrowserRuntimeReadiness(
        True,
        "ready",
        "The packaged Python Playwright-matched Chromium runtime is ready.",
        str(executable),
        str(browsers_dir),
        selected_contract.package_version,
        selected_contract.chromium_revision,
        selected_contract.chromium_version,
    )


def _find_chromium_executable(browsers_dir: Path, revision: str) -> Path | None:
    base = browsers_dir / f"chromium-{revision}"
    patterns = {
        "windows": ("chrome-win/chrome.exe", "chrome-win64/chrome.exe", "chrome.exe"),
        "darwin": (
            "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
            "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
        ),
        "linux": ("chrome-linux/chrome", "chrome-linux64/chrome", "chrome"),
    }.get(_platform_id(), ())
    for relative in patterns:
        candidate = base / relative
        if candidate.is_file():
            return candidate
    return None


def _default_runner(command: list[str], *, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=env,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=900,
        check=False,
    )


def _default_smoke(executable: Path, browsers_dir: Path) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
        try:
            page = browser.new_page()
            page.goto("data:text/html,<title>Row-Bot runtime check</title>", wait_until="domcontentloaded")
            if page.title() != "Row-Bot runtime check":
                raise RuntimeError("managed browser smoke validation failed")
        finally:
            browser.close()


def install_managed_browser_runtime(
    root: Path | None = None,
    *,
    contract: PlaywrightContract | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    smoke_validator: Callable[[Path, Path], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> BrowserRuntimeInstallResult:
    """Install and validate a candidate before atomically selecting it.

    A failed candidate is deliberately retained for diagnostics and never
    replaces the previous known-good manifest.
    """

    selected_root = root or runtime_root()
    selected_contract = contract or installed_playwright_contract()
    candidate_root = selected_root / "candidates" / f"{selected_contract.chromium_revision}-{uuid4().hex}"
    browsers_dir = candidate_root / "browsers"
    browsers_dir.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    if progress:
        progress("Installing the Python Playwright-matched Chromium runtime")
    completed = (runner or _default_runner)(command, env=environment, cwd=selected_root)
    if completed.returncode:
        return BrowserRuntimeInstallResult(False, "The managed Chromium candidate failed to install.")
    executable = _find_chromium_executable(browsers_dir, selected_contract.chromium_revision)
    if executable is None:
        return BrowserRuntimeInstallResult(False, "The managed Chromium candidate was incomplete.")
    try:
        (smoke_validator or _default_smoke)(executable, browsers_dir)
    except Exception:
        return BrowserRuntimeInstallResult(False, "The managed Chromium candidate failed smoke validation.")
    previous = read_runtime_manifest(selected_root)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "owner": "row-bot-python-playwright",
        "browser": "chromium",
        "package_version": selected_contract.package_version,
        "chromium_revision": selected_contract.chromium_revision,
        "chromium_version": selected_contract.chromium_version,
        "platform": _platform_id(),
        "arch": _arch_id(),
        "browsers_dir": str(browsers_dir),
        "executable_path": str(executable),
        "source": "python -m playwright install chromium",
        "complete": True,
        "previous_manifest": previous or None,
    }
    _atomic_json(manifest_path(selected_root), manifest)
    return BrowserRuntimeInstallResult(
        True,
        "Installed the Python Playwright-matched Chromium runtime.",
        str(executable),
        str(browsers_dir),
        selected_contract.package_version,
        selected_contract.chromium_revision,
    )


def rollback_managed_browser_runtime(root: Path | None = None) -> bool:
    selected_root = root or runtime_root()
    current = read_runtime_manifest(selected_root)
    previous = current.get("previous_manifest") if isinstance(current, dict) else None
    if not isinstance(previous, dict):
        return False
    _atomic_json(manifest_path(selected_root), previous)
    return True


def ensure_profile_engine(profile_dir: Path, engine_family: str = "chromium") -> None:
    """Bind an existing owned profile to its browser engine without migrating it."""

    marker = profile_dir / ".row-bot-browser-engine.json"
    existing = _read_manifest(marker)
    if existing and existing.get("engine_family") != engine_family:
        raise RuntimeError("The owned browser profile belongs to a different browser engine.")
    profile_dir.mkdir(parents=True, exist_ok=True)
    if not existing:
        _atomic_json(marker, {"schema": 1, "engine_family": engine_family})
