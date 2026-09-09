"""Plugin installer — download, validate, install, update, uninstall.

Downloads plugin archives from the monorepo, validates before install,
checks dependency conflicts against core, and manages the local
``~/.row-bot/installed_plugins/`` directory.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

from row_bot.data_paths import get_row_bot_data_dir

logger = logging.getLogger(__name__)

DATA_DIR = get_row_bot_data_dir()
PLUGINS_DIR = DATA_DIR / "installed_plugins"

# Base URL for downloading plugins from the monorepo
# Each plugin directory is downloaded as: {BASE_URL}/plugins/{plugin_id}/
DEFAULT_REPO_URL = os.environ.get(
    "ROW_BOT_PLUGIN_REPO_URL",
    "https://github.com/siddsachar/row-bot-plugins",
)


@dataclass
class InstallResult:
    """Result of an install/update/uninstall operation."""
    success: bool
    plugin_id: str
    message: str
    version: str = ""


# ── Public API ───────────────────────────────────────────────────────────────
def install_plugin(
    plugin_id: str,
    *,
    source_dir: pathlib.Path | None = None,
    source: str | None = None,
    source_ref: str = "",
    archive_url: str = "",
    expected_checksum: str | None = None,
) -> InstallResult:
    """Install a plugin.

    If *source_dir* is provided, copies from that directory (local install).
    Otherwise, downloads from the marketplace repo.
    """
    dest = PLUGINS_DIR / plugin_id
    if dest.exists():
        return InstallResult(
            success=False, plugin_id=plugin_id,
            message=f"Plugin '{plugin_id}' is already installed. Use update instead.",
        )

    try:
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

        if source_dir:
            # Local install (copy directory)
            if not source_dir.is_dir():
                return InstallResult(
                    success=False, plugin_id=plugin_id,
                    message=f"Source directory not found: {source_dir}",
                )
            shutil.copytree(source_dir, dest)
        elif archive_url:
            _download_plugin_archive(plugin_id, dest, archive_url)
        else:
            # Download from repo
            _download_plugin(plugin_id, dest)

        checksum_error = _verify_checksum(dest, expected_checksum)
        if checksum_error:
            shutil.rmtree(dest, ignore_errors=True)
            return InstallResult(
                success=False, plugin_id=plugin_id,
                message=checksum_error,
            )

        # Validate manifest exists and conforms to the v2 contract.
        try:
            from row_bot.plugins.manifest import parse_manifest

            manifest = parse_manifest(dest)
        except Exception as exc:
            shutil.rmtree(dest, ignore_errors=True)
            return InstallResult(
                success=False, plugin_id=plugin_id,
                message=f"Installed plugin manifest is invalid: {exc}",
            )
        if manifest.id != plugin_id:
            shutil.rmtree(dest, ignore_errors=True)
            return InstallResult(
                success=False, plugin_id=plugin_id,
                message=(
                    f"Manifest id '{manifest.id}' does not match requested "
                    f"plugin id '{plugin_id}'"
                ),
            )
        version = manifest.version

        # Security scan
        from row_bot.plugins.loader import _security_scan
        sec_err = _security_scan(dest)
        if sec_err:
            shutil.rmtree(dest, ignore_errors=True)
            return InstallResult(
                success=False, plugin_id=plugin_id,
                message=f"Security check failed: {sec_err}",
            )

        from row_bot.plugins import state as plugin_state

        install_source = source or ("local" if source_dir else "marketplace")
        install_ref = source_ref or (str(source_dir) if source_dir else archive_url or DEFAULT_REPO_URL)
        plugin_state.mark_plugin_installed(
            plugin_id,
            version=version,
            source=install_source,
            source_ref=install_ref,
        )

        logger.info("Plugin '%s' v%s installed to %s", plugin_id, version, dest)
        return InstallResult(
            success=True, plugin_id=plugin_id, version=version,
            message=(
                f"Installed '{plugin_id}' v{version} and kept it off. "
                "Configure, test, then enable it in Plugin Center."
            ),
        )

    except Exception as exc:
        # Cleanup on failure
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        logger.error("Install failed for '%s': %s", plugin_id, exc, exc_info=True)
        return InstallResult(
            success=False, plugin_id=plugin_id,
            message=f"Install failed: {exc}",
        )


def update_plugin(
    plugin_id: str,
    *,
    source_dir: pathlib.Path | None = None,
    source: str | None = None,
    source_ref: str = "",
    archive_url: str = "",
    expected_checksum: str | None = None,
) -> InstallResult:
    """Update an installed plugin.

    Backs up the current version, installs the new one, and rolls back
    on failure.
    """
    dest = PLUGINS_DIR / plugin_id
    if not dest.exists():
        return InstallResult(
            success=False, plugin_id=plugin_id,
            message=f"Plugin '{plugin_id}' is not installed",
        )

    backup = dest.with_suffix(".bak")
    try:
        # Backup current version
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(dest), str(backup))

        # Install new version
        result = install_plugin(
            plugin_id,
            source_dir=source_dir,
            source=source,
            source_ref=source_ref,
            archive_url=archive_url,
            expected_checksum=expected_checksum,
        )

        if result.success:
            # Remove backup
            shutil.rmtree(backup, ignore_errors=True)
            return result
        else:
            # Rollback
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(backup), str(dest))
            return InstallResult(
                success=False, plugin_id=plugin_id,
                message=f"Update failed, rolled back: {result.message}",
            )

    except Exception as exc:
        # Attempt rollback
        if backup.exists():
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(backup), str(dest))
        logger.error("Update failed for '%s': %s", plugin_id, exc, exc_info=True)
        return InstallResult(
            success=False, plugin_id=plugin_id,
            message=f"Update failed: {exc}",
        )


def uninstall_plugin(plugin_id: str) -> InstallResult:
    """Uninstall a plugin — remove files and clean state."""
    dest = PLUGINS_DIR / plugin_id
    if not dest.exists():
        return InstallResult(
            success=False, plugin_id=plugin_id,
            message=f"Plugin '{plugin_id}' is not installed",
        )

    try:
        # Unregister from runtime
        from row_bot.plugins import registry as reg
        reg.unregister_plugin(plugin_id)

        # Remove state and secrets
        from row_bot.plugins import state
        state.remove_plugin_state(plugin_id)

        # Remove files
        shutil.rmtree(dest)

        logger.info("Plugin '%s' uninstalled", plugin_id)
        return InstallResult(
            success=True, plugin_id=plugin_id,
            message=f"Plugin '{plugin_id}' uninstalled successfully",
        )

    except Exception as exc:
        logger.error("Uninstall error for '%s': %s", plugin_id, exc, exc_info=True)
        return InstallResult(
            success=False, plugin_id=plugin_id,
            message=f"Uninstall error: {exc}",
        )


def is_installed(plugin_id: str) -> bool:
    """Check if a plugin is installed."""
    return (PLUGINS_DIR / plugin_id).is_dir()


def get_installed_version(plugin_id: str) -> str | None:
    """Get the installed version of a plugin, or None."""
    manifest_path = PLUGINS_DIR / plugin_id / "plugin.json"
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("version")
    except Exception:
        return None


# ── Download ─────────────────────────────────────────────────────────────────
def _verify_checksum(plugin_dir: pathlib.Path, expected_checksum: str | None) -> str | None:
    expected = (expected_checksum or "").strip()
    if not expected:
        return None
    if not expected.lower().startswith("sha256:"):
        return f"Unsupported plugin checksum format: {expected}"
    from row_bot.plugins.devtools import compute_plugin_checksum

    actual = compute_plugin_checksum(plugin_dir)
    if actual.lower() != expected.lower():
        return f"Checksum mismatch: expected {expected}, got {actual}"
    return None


def _download_plugin(plugin_id: str, dest: pathlib.Path) -> None:
    """Download a plugin from the monorepo.

    Downloads the plugin directory as a zip from GitHub's archive API
    and extracts just the plugin's subdirectory.
    """
    # GitHub archive URL: downloads entire repo as zip
    archive_url = f"{DEFAULT_REPO_URL}/archive/refs/heads/main.zip"
    _download_plugin_archive(plugin_id, dest, archive_url)


def _download_plugin_archive(plugin_id: str, dest: pathlib.Path, archive_url: str) -> None:
    """Download or read a zip archive and extract one plugin directory."""

    logger.info("Downloading plugin '%s' from %s", plugin_id, archive_url)

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = pathlib.Path(tmp) / "repo.zip"
        _download_to_file(archive_url, zip_path)

        with zipfile.ZipFile(zip_path, "r") as zf:
            extract_dir = pathlib.Path(tmp) / "extracted"
            _safe_extract_zip(zf, extract_dir)

        extracted_plugin = _find_extracted_plugin_dir(extract_dir, plugin_id)
        shutil.copytree(extracted_plugin, dest)

    logger.info("Downloaded plugin '%s' to %s", plugin_id, dest)


def _download_to_file(ref: str, dest: pathlib.Path) -> None:
    local_path = _local_path_from_ref(ref)
    if local_path is not None:
        shutil.copyfile(local_path, dest)
        return

    import urllib.request

    req = urllib.request.Request(
        ref, headers={"User-Agent": "Row-Bot-Plugin-Installer"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        with open(dest, "wb") as f:
            f.write(resp.read())


def _safe_extract_zip(zf: zipfile.ZipFile, dest: pathlib.Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            raise ValueError(f"Unsafe zip member path: {member.filename}")
        zf.extract(member, dest)


def _find_extracted_plugin_dir(extract_dir: pathlib.Path, plugin_id: str) -> pathlib.Path:
    candidates = sorted({path.parent for path in extract_dir.rglob("plugin.json")})
    matching: list[pathlib.Path] = []
    for candidate in candidates:
        try:
            raw = json.loads((candidate / "plugin.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(raw, dict) and str(raw.get("id", "")) == plugin_id:
            matching.append(candidate)
    if len(matching) == 1:
        return matching[0]
    if len(matching) > 1:
        raise ValueError(f"Archive contains multiple plugin.json files for '{plugin_id}'")
    if len(candidates) == 1:
        return candidates[0]
    raise FileNotFoundError(f"Plugin '{plugin_id}' not found in archive")


def _local_path_from_ref(ref: str) -> pathlib.Path | None:
    if not ref:
        return None
    candidate = pathlib.Path(ref).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    parsed = urlparse(ref)
    if parsed.scheme != "file":
        return None
    raw_path = url2pathname(parsed.path)
    if parsed.netloc:
        raw_path = f"//{parsed.netloc}{raw_path}"
    path = pathlib.Path(raw_path).expanduser()
    return path.resolve() if path.is_file() else None


# ── Dependency Installation ──────────────────────────────────────────────────
def _install_plugin_deps(deps: list[str]) -> InstallResult:
    """Install plugin Python dependencies with core freeze protection."""
    from row_bot.plugins.sandbox import install_dependencies

    # Install
    try:
        success, message = install_dependencies(deps)
        if success:
            return InstallResult(success=True, plugin_id="", message="Dependencies installed")
        else:
            logger.warning("Plugin dependency install failed: %s", message)
            return InstallResult(
                success=False, plugin_id="",
                message=f"Install blocked: {message}",
            )
    except Exception as exc:
        logger.error("Plugin dependency install error: %s", exc, exc_info=True)
        return InstallResult(
            success=False, plugin_id="",
            message=f"Dependency install failed: {exc}",
        )
