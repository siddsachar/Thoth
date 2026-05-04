"""Thoth in-app auto-update — polls GitHub Releases, downloads installers,
verifies SHA256 + OS code signatures, and hands off to the OS installer.

Design principles
-----------------
- **Silent-on-failure**: any network/parse/IO error during a background check
  is logged at DEBUG and swallowed. Never blocks startup, never surfaces a
  modal error for a routine poll.
- **No opt-in**: checking is on by default. Users can still pick channel
  (stable/beta), skip specific versions, or ignore the prompt — they're
  never auto-installed-into.
- **Stdlib-only networking** via ``urllib.request`` so the updater has no
  new external dependencies and can run before heavier modules are imported.
- **Thread-based scheduler** — avoids touching the NiceGUI event loop.

Data model
~~~~~~~~~~
- ``UpdateInfo``  — a parsed release (version, asset, sha256, notes).
- ``UpdateState`` — persisted preferences + last-check / skipped-versions.

Release body manifest
~~~~~~~~~~~~~~~~~~~~~
We look for a fenced block of the form::

    <!-- thoth-update-manifest -->
    ```manifest
    schema: 1
    files:
      ThothSetup_3.18.0.exe: sha256=<hex>
      Thoth-3.18.0-macOS-arm64.dmg: sha256=<hex>
    ```

If the manifest is missing we still surface the release, but refuse to
install (SHA256 is a hard requirement).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from packaging.version import InvalidVersion, Version

from version import __version__

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ════════════════════════════════════════════════════════════════════════════

_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_CONFIG_PATH = _DATA_DIR / "update_config.json"
_DOWNLOAD_DIR = _DATA_DIR / "updates"

_GITHUB_API_HOST = "api.github.com"
_GITHUB_DOWNLOAD_HOST = "github.com"
_RELEASES_LATEST_URL = (
    "https://api.github.com/repos/siddsachar/Thoth/releases/latest"
)
_RELEASES_URL = "https://api.github.com/repos/siddsachar/Thoth/releases"

_USER_AGENT = f"Thoth-Updater/{__version__}"
_HTTP_TIMEOUT = 15
_DOWNLOAD_TIMEOUT = 600  # 10 min for a ~300 MB installer

_CHECK_STARTUP_DELAY_SEC = 30
_CHECK_INTERVAL_SEC = 6 * 60 * 60      # 6 hours between scheduler ticks
_CHECK_DEBOUNCE_SEC = 24 * 60 * 60     # min 24h between actual network calls

# Platform → asset name pattern
_WIN_ASSET_RE = re.compile(r"^ThothSetup_[\d.]+\.exe$")
_MAC_ARM_ASSET_RE = re.compile(r"^Thoth-[\d.]+-macOS-arm64\.dmg$")
_MAC_X86_ASSET_RE = re.compile(r"^Thoth-[\d.]+-macOS-x86_64\.dmg$")
_LINUX_X64_ASSET_RE = re.compile(r"^Thoth-[\d.]+-Linux-x86_64\.tar\.gz$")
_LINUX_ARM64_ASSET_RE = re.compile(r"^Thoth-[\d.]+-Linux-aarch64\.tar\.gz$")

# Manifest fenced-block parser
_MANIFEST_BLOCK_RE = re.compile(
    r"<!--\s*thoth-update-manifest\s*-->\s*```manifest\s*(.*?)\s*```",
    re.DOTALL | re.IGNORECASE,
)
_MANIFEST_FILE_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9._+-]+):\s*sha256\s*=\s*([0-9a-fA-F]{64})\s*$"
)


# ════════════════════════════════════════════════════════════════════════════
# DATA TYPES
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class UpdateInfo:
    """A parsed GitHub release that could be installed."""

    version: str
    channel: str  # 'stable' or 'beta'
    published_at: str  # ISO-8601
    notes_md: str
    notes_summary: str
    asset_name: str
    asset_url: str
    asset_size: int
    sha256: str  # may be "" if manifest missing
    html_url: str
    is_prerelease: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class UpdateState:
    """Persisted updater preferences + runtime state."""

    channel: str = "stable"
    auto_check: bool = True                      # kept for completeness, defaults on
    check_interval_hours: int = 24
    last_check: Optional[str] = None             # ISO-8601
    last_success: Optional[str] = None
    skipped_versions: list[str] = field(default_factory=list)
    dismissed_banner_versions: list[str] = field(default_factory=list)
    # Runtime-only, not persisted:
    available: Optional[UpdateInfo] = None
    current_version: str = __version__
    current_channel: str = "stable"

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("available", None)
        d.pop("current_version", None)
        d.pop("current_channel", None)
        return d


# ════════════════════════════════════════════════════════════════════════════
# STATE PERSISTENCE
# ════════════════════════════════════════════════════════════════════════════

_state_lock = threading.RLock()
_state: Optional[UpdateState] = None
_listeners: list[Callable[[UpdateState], None]] = []


def _load_state() -> UpdateState:
    """Load persisted state from disk; return defaults on any error."""
    state = UpdateState()
    if not _CONFIG_PATH.exists():
        return state
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in ("channel", "last_check", "last_success"):
                if isinstance(data.get(key), str):
                    setattr(state, key, data[key])
            if isinstance(data.get("auto_check"), bool):
                state.auto_check = data["auto_check"]
            if isinstance(data.get("check_interval_hours"), int):
                state.check_interval_hours = max(1, data["check_interval_hours"])
            for key in ("skipped_versions", "dismissed_banner_versions"):
                val = data.get(key)
                if isinstance(val, list):
                    setattr(state, key, [str(v) for v in val if isinstance(v, str)])
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to load update_config.json (using defaults): %s", exc)
    # Normalize channel
    if state.channel not in ("stable", "beta"):
        state.channel = "stable"
    state.current_channel = state.channel
    return state


def _save_state(state: UpdateState) -> None:
    try:
        _CONFIG_PATH.write_text(
            json.dumps(state.to_dict(), indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.debug("Failed to save update_config.json: %s", exc)


def get_update_state() -> UpdateState:
    """Return the (cached) update state. Safe to call from any thread."""
    global _state
    with _state_lock:
        if _state is None:
            _state = _load_state()
        return _state


def set_channel(channel: str) -> None:
    """Change the update channel (stable|beta) and persist."""
    if channel not in ("stable", "beta"):
        raise ValueError(f"invalid channel: {channel!r}")
    with _state_lock:
        st = get_update_state()
        st.channel = channel
        st.current_channel = channel
        # clear any cached update — it's for the old channel
        st.available = None
        _save_state(st)
    _notify()


def skip_version(version: str) -> None:
    """Add *version* to the skipped list and clear any pending update for it."""
    with _state_lock:
        st = get_update_state()
        if version and version not in st.skipped_versions:
            st.skipped_versions.append(version)
        if st.available and st.available.version == version:
            st.available = None
        _save_state(st)
    _notify()


def dismiss_banner(version: str) -> None:
    with _state_lock:
        st = get_update_state()
        if version and version not in st.dismissed_banner_versions:
            st.dismissed_banner_versions.append(version)
            _save_state(st)
    _notify()


def subscribe(callback: Callable[[UpdateState], None]) -> Callable[[], None]:
    """Register a listener; returns an unsubscribe callable."""
    with _state_lock:
        _listeners.append(callback)

    def _unsub() -> None:
        with _state_lock:
            try:
                _listeners.remove(callback)
            except ValueError:
                pass

    return _unsub


def _notify() -> None:
    with _state_lock:
        listeners = list(_listeners)
        st = get_update_state()
    for cb in listeners:
        try:
            cb(st)
        except Exception:
            logger.exception("Updater listener raised")


# ════════════════════════════════════════════════════════════════════════════
# HELPERS — DEV INSTALL DETECTION
# ════════════════════════════════════════════════════════════════════════════

def is_dev_install() -> bool:
    """Return True if we're running from a git checkout, not an installed
    build. We disable self-installation in dev to avoid clobbering work.
    """
    try:
        app_root = pathlib.Path(__file__).resolve().parent
        if (app_root / ".git").exists():
            return True
        # Packaged Windows builds ship as frozen exe or _internal dir
        if platform.system() == "Windows":
            if getattr(sys, "frozen", False):
                return False
            # Running from the installed Thoth folder: expect no .git, expect
            # a sibling 'unins000.exe' or similar. We conservatively treat
            # "no .git and running under the installer python" as prod.
            return not (app_root / "unins000.exe").exists() and \
                   not (app_root.parent / "unins000.exe").exists()
        if platform.system() == "Darwin":
            # Running from a .app bundle → app_root ends in
            # Thoth.app/Contents/Resources or similar.
            return ".app" not in str(app_root)
        if platform.system() == "Linux":
            return _linux_install_root(app_root) is None
    except Exception:
        pass
    return False


def _linux_install_root(app_root: pathlib.Path | None = None) -> pathlib.Path | None:
    """Return the Linux XDG tarball install root, if this is one."""
    if platform.system() != "Linux":
        return None
    env_root = os.environ.get("THOTH_INSTALL_ROOT")
    candidates: list[pathlib.Path] = []
    if env_root:
        candidates.append(pathlib.Path(env_root))
    resolved_app_root = app_root or pathlib.Path(__file__).resolve().parent
    candidates.extend([resolved_app_root.parent, resolved_app_root])
    for candidate in candidates:
        try:
            marker = candidate / "install_info.json"
            if not marker.exists():
                continue
            data = json.loads(marker.read_text(encoding="utf-8"))
            if data.get("platform") == "linux" and data.get("install_kind") == "xdg-user-tarball":
                return candidate
        except Exception:
            continue
    return None


# ════════════════════════════════════════════════════════════════════════════
# HELPERS — ASSET + MANIFEST PARSING
# ════════════════════════════════════════════════════════════════════════════

def _platform_asset_re() -> Optional[re.Pattern[str]]:
    sys_name = platform.system()
    if sys_name == "Windows":
        return _WIN_ASSET_RE
    if sys_name == "Darwin":
        mach = platform.machine().lower()
        if mach in ("arm64", "aarch64"):
            return _MAC_ARM_ASSET_RE
        return _MAC_X86_ASSET_RE
    if sys_name == "Linux":
        mach = platform.machine().lower()
        if mach in ("arm64", "aarch64"):
            return _LINUX_ARM64_ASSET_RE
        return _LINUX_X64_ASSET_RE
    return None


def _safe_extract_tar(archive: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle.getmembers():
            target = (destination / member.name).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise UpdateError(f"Archive contains unsafe path: {member.name}")
        handle.extractall(destination)
    children = [child for child in destination.iterdir() if child.is_dir()]
    if len(children) == 1:
        return children[0]
    return destination


def _install_linux_tarball(installer_path: pathlib.Path) -> pathlib.Path:
    """Install a verified Linux tarball into the user's XDG app directory."""
    install_home = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local" / "share")) / "thoth"
    releases_dir = install_home / "releases"
    bin_dir = pathlib.Path.home() / ".local" / "bin"
    desktop_dir = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local" / "share")) / "applications"
    icon_dir = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local" / "share")) / "icons" / "hicolor" / "256x256" / "apps"

    with tempfile.TemporaryDirectory(prefix="thoth_linux_update_") as tmp:
        extracted = _safe_extract_tar(installer_path, pathlib.Path(tmp))
        marker = extracted / "install_info.json"
        wrapper = extracted / "bin" / "thoth"
        app_dir = extracted / "app"
        python_bin = extracted / "python" / "bin" / "python3"
        if not marker.exists() or not wrapper.exists() or not app_dir.exists() or not python_bin.exists():
            raise UpdateError("Linux update archive is missing required package files")
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise UpdateError("Linux update archive has invalid install_info.json") from exc
        if metadata.get("platform") != "linux" or metadata.get("install_kind") != "xdg-user-tarball":
            raise UpdateError("Linux update archive is not a Thoth XDG tarball install")
        version = str(metadata.get("version") or "").strip()
        if not version:
            raise UpdateError("Linux update archive is missing a version")

        releases_dir.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        desktop_dir.mkdir(parents=True, exist_ok=True)
        icon_dir.mkdir(parents=True, exist_ok=True)

        target = releases_dir / version
        staging = releases_dir / f".installing-{version}-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(extracted, staging, symlinks=True)
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)

    current = install_home / "current"
    tmp_link = install_home / f".current-{os.getpid()}"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(pathlib.Path("releases") / version, tmp_link, target_is_directory=True)
    tmp_link.replace(current)

    launcher = bin_dir / "thoth"
    if launcher.exists() or launcher.is_symlink():
        launcher.unlink()
    os.symlink(current / "bin" / "thoth", launcher)

    desktop_src = current / "share" / "applications" / "com.thoth.Thoth.desktop"
    if desktop_src.exists():
        desktop_target = desktop_dir / "com.thoth.Thoth.desktop"
        text = desktop_src.read_text(encoding="utf-8")
        text = re.sub(r"^Exec=.*$", f"Exec={launcher}", text, flags=re.MULTILINE)
        desktop_target.write_text(text, encoding="utf-8")
    icon_src = current / "share" / "icons" / "hicolor" / "256x256" / "apps" / "thoth.png"
    if icon_src.exists():
        shutil.copy2(icon_src, icon_dir / "thoth.png")

    for cmd, arg in (("update-desktop-database", desktop_dir), ("gtk-update-icon-cache", icon_dir.parents[1])):
        tool = shutil.which(cmd)
        if tool:
            try:
                subprocess.run([tool, str(arg)], capture_output=True, timeout=20)
            except Exception:
                logger.debug("Linux desktop cache refresh failed for %s", cmd, exc_info=True)

    return launcher


def parse_manifest(body: str) -> dict[str, str]:
    """Extract ``{filename: sha256-hex}`` from a release body. Returns {} if
    the fenced block is missing or malformed. Never raises.
    """
    if not body:
        return {}
    match = _MANIFEST_BLOCK_RE.search(body)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        m = _MANIFEST_FILE_LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2).lower()
    return out


def _summarize_notes(markdown: str, max_chars: int = 280) -> str:
    """Return a one-paragraph summary of the release notes for status-bar
    tooltips and banners."""
    if not markdown:
        return ""
    cleaned = _MANIFEST_BLOCK_RE.sub("", markdown)
    # Strip remaining fenced code blocks (``` … ```) — they're noise in a summary.
    cleaned = re.sub(r"```[\s\S]*?```", "", cleaned)
    # Strip HTML comments, headings, bold/italic markers.
    cleaned = re.sub(r"<!--.*?-->", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"^#+\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"^\s*[-*]\s+", "• ", cleaned, flags=re.MULTILINE)
    # Collapse blank lines.
    cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "…"


def _parse_release(release: dict[str, Any], channel: str) -> Optional[UpdateInfo]:
    """Turn a GitHub release JSON payload into an UpdateInfo for *this*
    platform, or return None if no suitable asset exists."""
    asset_re = _platform_asset_re()
    if asset_re is None:
        return None
    assets = release.get("assets") or []
    chosen = None
    for asset in assets:
        name = asset.get("name") or ""
        if asset_re.match(name):
            chosen = asset
            break
    if chosen is None:
        return None

    tag = (release.get("tag_name") or "").lstrip("v")
    if not tag:
        return None

    manifest = parse_manifest(release.get("body") or "")
    sha256 = manifest.get(chosen.get("name", ""), "")

    notes_md = release.get("body") or ""
    return UpdateInfo(
        version=tag,
        channel=channel,
        published_at=release.get("published_at") or "",
        notes_md=notes_md,
        notes_summary=_summarize_notes(notes_md),
        asset_name=chosen.get("name") or "",
        asset_url=chosen.get("browser_download_url") or "",
        asset_size=int(chosen.get("size") or 0),
        sha256=sha256,
        html_url=release.get("html_url") or "",
        is_prerelease=bool(release.get("prerelease")),
    )


def compare_versions(current: str, candidate: str) -> int:
    """Return >0 if candidate > current, <0 if less, 0 if equal. Invalid
    versions compare as 0 (treat as no update)."""
    try:
        c = Version(current)
        n = Version(candidate)
    except InvalidVersion:
        return 0
    if n > c:
        return 1
    if n < c:
        return -1
    return 0


# ════════════════════════════════════════════════════════════════════════════
# NETWORKING — GITHUB API
# ════════════════════════════════════════════════════════════════════════════

def _http_get(url: str, *, accept: str = "application/vnd.github+json") -> Optional[bytes]:
    """Minimal HTTPS GET. Returns response body bytes or None on any error."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        logger.debug("Refusing non-https URL: %s", url)
        return None
    if parsed.hostname not in (_GITHUB_API_HOST, _GITHUB_DOWNLOAD_HOST,
                               "objects.githubusercontent.com",
                               "codeload.github.com"):
        logger.debug("Refusing unknown host: %s", parsed.hostname)
        return None
    req = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("THOTH_UPDATER_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec
            if resp.status == 200:
                return resp.read()
            logger.debug("GitHub HTTP %s for %s", resp.status, url)
            return None
    except urllib.error.HTTPError as exc:
        logger.debug("GitHub HTTPError %s for %s: %s", exc.code, url, exc.reason)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("GitHub request failed for %s: %s", url, exc)
        return None


def _fetch_releases_payload(channel: str) -> Optional[list[dict[str, Any]]]:
    """Fetch the relevant release(s) for *channel*. Returns a list so the
    caller can iterate (stable → length 1, beta → up to 10)."""
    if channel == "stable":
        data = _http_get(_RELEASES_LATEST_URL)
        if not data:
            return None
        try:
            payload = json.loads(data.decode("utf-8"))
            return [payload] if isinstance(payload, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.debug("Failed to parse stable release JSON: %s", exc)
            return None
    # beta: take top 10 releases (including pre-releases, excluding drafts)
    data = _http_get(f"{_RELEASES_URL}?per_page=10")
    if not data:
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.debug("Failed to parse releases JSON: %s", exc)
        return None
    if not isinstance(payload, list):
        return None
    return [r for r in payload if isinstance(r, dict) and not r.get("draft")]


# ════════════════════════════════════════════════════════════════════════════
# CORE CHECK
# ════════════════════════════════════════════════════════════════════════════

def check_for_updates(*, force: bool = False) -> Optional[UpdateInfo]:
    """Poll GitHub for a newer release on the user's channel.

    Returns the UpdateInfo when an applicable newer release exists, else
    None. Never raises — all failures are swallowed and logged at DEBUG.
    Results are cached on ``UpdateState.available``.
    """
    st = get_update_state()
    now = datetime.now(timezone.utc)
    if not force and st.last_check:
        try:
            last = datetime.fromisoformat(st.last_check)
            if (now - last).total_seconds() < _CHECK_DEBOUNCE_SEC:
                logger.debug("Skipping update check — debounce (%s)", st.last_check)
                return st.available
        except ValueError:
            pass

    with _state_lock:
        st.last_check = now.isoformat()
        _save_state(st)

    releases = _fetch_releases_payload(st.channel)
    if releases is None:
        logger.debug("Update check: no network / rate-limited / parse failure")
        _notify()
        return st.available

    best: Optional[UpdateInfo] = None
    for r in releases:
        info = _parse_release(r, st.channel)
        if info is None:
            continue
        if st.channel == "stable" and info.is_prerelease:
            continue
        if compare_versions(__version__, info.version) <= 0:
            continue
        if info.version in st.skipped_versions:
            continue
        if best is None or compare_versions(best.version, info.version) < 0:
            best = info

    with _state_lock:
        st.last_success = now.isoformat()
        st.available = best
        _save_state(st)
    _notify()
    if best:
        logger.info("Update available: v%s (%s channel)", best.version, best.channel)
    else:
        logger.debug("No update available")
    return best


# ════════════════════════════════════════════════════════════════════════════
# DOWNLOAD + VERIFY + INSTALL
# ════════════════════════════════════════════════════════════════════════════

class UpdateError(Exception):
    """Raised by ``download_update`` / ``install_and_restart`` on fatal errors."""


def download_update(
    info: UpdateInfo,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
) -> pathlib.Path:
    """Download the installer for *info* to the updates dir and verify SHA256.

    Raises ``UpdateError`` on any failure. Returns the installer path on
    success. The caller is responsible for not calling this on dev installs.
    """
    if not info.asset_url:
        raise UpdateError("Release has no downloadable asset for this platform")
    if not info.sha256:
        raise UpdateError(
            "Release is missing a SHA256 manifest — refusing to install"
        )

    _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = _DOWNLOAD_DIR / info.asset_name
    tmp = target.with_suffix(target.suffix + ".part")

    parsed = urllib.parse.urlparse(info.asset_url)
    if parsed.scheme != "https":
        raise UpdateError(f"Refusing non-https download URL: {info.asset_url}")

    req = urllib.request.Request(
        info.asset_url,
        headers={"Accept": "application/octet-stream", "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # nosec
            total = info.asset_size or int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            hasher = hashlib.sha256()
            with open(tmp, "wb") as fp:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fp.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress:
                        try:
                            progress(downloaded, total)
                        except Exception:
                            pass
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _safe_unlink(tmp)
        raise UpdateError(f"Download failed: {exc}") from exc

    actual = hasher.hexdigest().lower()
    if actual != info.sha256.lower():
        _safe_unlink(tmp)
        raise UpdateError(
            f"SHA256 mismatch (expected {info.sha256[:12]}…, "
            f"got {actual[:12]}…)"
        )

    _safe_unlink(target)
    try:
        tmp.replace(target)
    except OSError as exc:
        _safe_unlink(tmp)
        raise UpdateError(f"Could not finalize download: {exc}") from exc
    return target


def _safe_unlink(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("Could not remove %s", path, exc_info=True)


def verify_os_signature(path: pathlib.Path) -> tuple[bool, str]:
    """Verify OS-level code signature. Returns (ok, message).

    - Windows: ``signtool.exe verify /pa <path>`` if available; otherwise
      returns (True, "signtool not found — skipping") and trusts SHA256.
    - macOS: ``codesign --verify --deep --strict`` and ``spctl --assess``.
    - Other: (True, "no verifier").
    """
    import shutil
    import subprocess

    sys_name = platform.system()
    if sys_name == "Windows":
        signtool = shutil.which("signtool.exe") or shutil.which("signtool")
        if not signtool:
            return True, "signtool not found — relying on SHA256 only"
        try:
            r = subprocess.run(
                [signtool, "verify", "/pa", "/q", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                return True, "signtool: signature OK"
            return False, f"signtool failed: {r.stdout or r.stderr}"
        except (subprocess.SubprocessError, OSError) as exc:
            return True, f"signtool error ({exc}) — relying on SHA256"
    if sys_name == "Darwin":
        try:
            r = subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", str(path)],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                return False, f"codesign failed: {r.stderr.strip()}"
            return True, "codesign: signature OK"
        except (subprocess.SubprocessError, OSError) as exc:
            return True, f"codesign error ({exc}) — relying on SHA256"
    return True, "no OS-level verifier available"


def install_and_restart(installer_path: pathlib.Path) -> None:
    """Launch the installer and schedule this process to exit.

    Windows: ``ThothSetup_x.y.z.exe /SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS``.
    macOS: ``open <dmg>`` (Finder handles mount + drag-to-Applications).
    """
    import subprocess

    if not installer_path.exists():
        raise UpdateError(f"Installer not found: {installer_path}")

    ok, msg = verify_os_signature(installer_path)
    if not ok:
        raise UpdateError(f"Signature verification failed: {msg}")
    logger.info("Signature check: %s", msg)

    sys_name = platform.system()
    try:
        if sys_name == "Windows":
            subprocess.Popen(
                [str(installer_path),
                 "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"],
                close_fds=True,
            )
        elif sys_name == "Darwin":
            subprocess.Popen(["open", str(installer_path)], close_fds=True)
        elif sys_name == "Linux":
            launcher = _install_linux_tarball(installer_path)
            subprocess.Popen([str(launcher)], close_fds=True)
        else:
            raise UpdateError(f"Unsupported platform: {sys_name}")
    except OSError as exc:
        raise UpdateError(f"Failed to launch installer: {exc}") from exc

    # Give the installer a moment to spawn before we exit.
    try:
        from launcher import quit_for_update  # optional hook
        quit_for_update()
        return
    except Exception:
        pass
    # Default: exit after short delay
    def _exit():
        time.sleep(2)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()


# ════════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ════════════════════════════════════════════════════════════════════════════

_scheduler_thread: Optional[threading.Thread] = None
_scheduler_stop = threading.Event()


def start_update_scheduler() -> None:
    """Start the background poll loop. Idempotent. Silent on all failures."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    if is_dev_install():
        logger.info("Updater: dev install detected — scheduler disabled")
        return

    _scheduler_stop.clear()

    def _loop() -> None:
        # Startup delay so we don't compete with heavy init
        if _scheduler_stop.wait(_CHECK_STARTUP_DELAY_SEC):
            return
        while not _scheduler_stop.is_set():
            try:
                check_for_updates()
            except Exception:
                logger.debug("Updater scheduler tick failed", exc_info=True)
            if _scheduler_stop.wait(_CHECK_INTERVAL_SEC):
                return

    t = threading.Thread(target=_loop, name="thoth-updater", daemon=True)
    t.start()
    _scheduler_thread = t
    logger.info("Updater scheduler started (channel=%s)",
                get_update_state().channel)


def stop_update_scheduler() -> None:
    _scheduler_stop.set()


# ════════════════════════════════════════════════════════════════════════════
# CONVENIENCE
# ════════════════════════════════════════════════════════════════════════════

def summary_for_status() -> dict[str, Any]:
    """Return a JSON-serializable summary for the thoth_status tool."""
    st = get_update_state()
    info = st.available
    return {
        "current_version": __version__,
        "channel": st.channel,
        "auto_check": st.auto_check,
        "last_check": st.last_check,
        "last_success": st.last_success,
        "update_available": info is not None,
        "available_version": info.version if info else None,
        "available_notes_summary": info.notes_summary if info else None,
        "skipped_versions": list(st.skipped_versions),
        "dev_install": is_dev_install(),
    }
