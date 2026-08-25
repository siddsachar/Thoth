"""Cua disclosure, pinned runtime installation, and readiness normalization."""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import tempfile
import shutil
from dataclasses import dataclass
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from row_bot.data_paths import get_row_bot_data_dir

NOTICE_VERSION = 2
RUNTIME_ID = "cua-driver"
DISCLOSURE_TEXT = (
    "Computer Use is powered by the open-source Cua Driver 0.19.3. Cua telemetry "
    "is enabled upstream by default and sends pseudonymous product analytics to "
    "Cua's EU PostHog endpoint. Events can include random installation and "
    "process-session identifiers; Cua/platform/architecture/transport versions; "
    "bounded client, provider, model, and agent categories; tool/operation, "
    "success, refusal/error class, duration bucket, output type/size bucket; "
    "aggregate session counts and capture-scope/browser/cursor/recording/config "
    "usage flags; permission-gate state; and install/update lifecycle events. "
    "Row-Bot disables Cua update checks and does not expose Cua recording, browser, "
    "desktop-wide, updater, autostart, clipboard, or arbitrary execution tools. "
    "Based on the reviewed tagged source, telemetry does not receive prompts, "
    "tool arguments or results, typed text, screenshots, accessibility trees, "
    "app/window names, URLs, filenames/paths, raw cursor/config values, or raw "
    "errors. This is Cua telemetry, not Row-Bot telemetry."
)


class CuaDisclosureRequired(PermissionError):
    pass


class ReadinessCode(str, Enum):
    DISABLED = "disabled"
    DISCLOSURE_REQUIRED = "disclosure_required"
    UNSUPPORTED = "unsupported"
    NOT_INSTALLED = "not_installed"
    HASH_MISMATCH = "hash_mismatch"
    VERSION_MISMATCH = "version_mismatch"
    PERMISSION_MISSING = "permission_missing"
    DEGRADED = "degraded"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class CuaReadiness:
    code: ReadinessCode
    message: str
    version: str = ""
    executable: str = ""
    hash_status: str = ""
    remediation: str = ""
    details: dict[str, Any] | None = None


def _settings_path() -> Path:
    return get_row_bot_data_dir() / "computer_use_settings.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_settings(payload: dict[str, Any]) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        Path(name).replace(path)
    finally:
        Path(name).unlink(missing_ok=True)


def load_cua_manifest() -> dict[str, Any]:
    text = resources.files("row_bot.computer_use").joinpath("cua_runtime_manifest.json").read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise RuntimeError("Unsupported Cua runtime manifest schema")
    return data


def disclosure_acknowledged() -> bool:
    return int(_read_json(_settings_path()).get("acknowledged_notice_version") or 0) == NOTICE_VERSION


def acknowledge_disclosure() -> None:
    settings = _read_json(_settings_path())
    settings["acknowledged_notice_version"] = NOTICE_VERSION
    _write_settings(settings)


def cancel_disclosure() -> None:
    settings = _read_json(_settings_path())
    settings.pop("acknowledged_notice_version", None)
    _write_settings(settings)
    try:
        from row_bot.tools import registry

        if registry.get_tool("computer_use") is not None:
            registry.set_enabled("computer_use", False)
    except Exception:
        pass


def require_cua_disclosure() -> None:
    if not disclosure_acknowledged():
        raise CuaDisclosureRequired("Cua telemetry disclosure must be accepted before any Cua executable invocation.")


def _platform_key() -> str | None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        if machine in {"amd64", "x86_64"}:
            return "windows-x86_64"
        if machine in {"arm64", "aarch64"}:
            return "windows-arm64"
    if system == "darwin" and machine in {"arm64", "aarch64", "x86_64", "amd64"}:
        return "macos-universal"
    return None


def selected_asset() -> dict[str, Any] | None:
    key = _platform_key()
    asset = (load_cua_manifest().get("assets") or {}).get(key) if key else None
    if not isinstance(asset, dict):
        return None
    return {"platform_key": key, **asset}


def _runtime_manifest() -> dict[str, Any]:
    from row_bot.mcp_client.requirements import RUNTIMES_DIR

    return _read_json(RUNTIMES_DIR / RUNTIME_ID / "manifest.json")


def resolve_cua_executable() -> str:
    settings = _read_json(_settings_path())
    if settings.get("allow_system_cua") and settings.get("system_cua_path"):
        require_cua_disclosure()
        path = Path(str(settings["system_cua_path"]))
        return str(path) if path.is_file() else ""
    manifest = _runtime_manifest()
    path = Path(str(manifest.get("executable_path") or ""))
    return str(path) if path.is_file() else ""


def _is_macos_app_bundle_executable(executable: str) -> bool:
    path = Path(executable)
    return any(parent.suffix.casefold() == ".app" for parent in path.parents)


def configure_system_cua(path: str, *, enabled: bool) -> None:
    """Store an explicit advanced override without executing it."""

    settings = _read_json(_settings_path())
    settings["system_cua_path"] = str(Path(path).expanduser()) if path else ""
    settings["allow_system_cua"] = bool(enabled)
    settings.pop("system_cua_version", None)
    settings.pop("system_cua_doctor_ok", None)
    settings.pop("system_cua_observation_ok", None)
    _write_settings(settings)


def verify_system_cua() -> CuaReadiness:
    """Invoke only the user-selected binary, after disclosure, to pin its version."""

    require_cua_disclosure()
    settings = _read_json(_settings_path())
    path = Path(str(settings.get("system_cua_path") or ""))
    if not settings.get("allow_system_cua") or not path.is_file():
        return CuaReadiness(ReadinessCode.NOT_INSTALLED, "No valid system Cua override is selected.")
    completed = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=15, check=False)
    output = " ".join(part.strip() for part in (completed.stdout, completed.stderr) if part).strip()
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", output)
    expected = str(load_cua_manifest()["version"])
    if completed.returncode != 0 or not match:
        return CuaReadiness(ReadinessCode.FAILED, "System Cua version check failed.", executable=str(path), remediation=f"Select the reviewed {expected} binary or use the managed install.")
    version = match.group(1)
    if version != expected:
        return CuaReadiness(ReadinessCode.VERSION_MISMATCH, "System Cua is outside the reviewed version pin.", version, str(path), remediation=f"Use Cua Driver {expected}.")
    settings["system_cua_version"] = version
    _write_settings(settings)
    return CuaReadiness(ReadinessCode.READY, "System Cua version matches the reviewed pin; diagnostics are still required.", version, str(path), "system-override")


def readiness(*, enabled: bool | None = None) -> CuaReadiness:
    if enabled is None:
        try:
            from row_bot.tools import registry

            enabled = registry.is_enabled("computer_use")
        except Exception:
            enabled = False
    manifest = load_cua_manifest()
    if not enabled:
        return CuaReadiness(ReadinessCode.DISABLED, "Computer Use is off.", version=str(manifest["version"]))
    if not disclosure_acknowledged():
        return CuaReadiness(ReadinessCode.DISCLOSURE_REQUIRED, "Review the Cua telemetry warning to continue.", version=str(manifest["version"]), remediation="Continue or Cancel the disclosure.")
    asset = selected_asset()
    if asset is None:
        return CuaReadiness(ReadinessCode.UNSUPPORTED, "Computer Use is unavailable on this platform.", version=str(manifest["version"]), remediation="Use Browser or a supported Windows/macOS host.")
    settings = _read_json(_settings_path())
    system_override = bool(settings.get("allow_system_cua") and settings.get("system_cua_path"))
    installed = _runtime_manifest()
    executable = resolve_cua_executable()
    if system_override:
        system_version = str(settings.get("system_cua_version") or "")
        if not executable:
            return CuaReadiness(ReadinessCode.NOT_INSTALLED, "Configured system Cua path does not exist.", version=str(manifest["version"]), remediation="Select an existing Cua executable or use the managed install.")
        if system_version != str(manifest["version"]):
            return CuaReadiness(ReadinessCode.VERSION_MISMATCH, "System Cua must be verified against the reviewed version.", version=system_version, executable=executable, remediation="Run Verify system Cua after reviewing the telemetry warning.")
        if not settings.get("system_cua_doctor_ok"):
            return CuaReadiness(ReadinessCode.DEGRADED, "System Cua version is verified; run diagnostics before use.", version=system_version, executable=executable, hash_status="system-override", remediation="Run diagnostics.")
        if not settings.get("system_cua_observation_ok"):
            return CuaReadiness(ReadinessCode.DEGRADED, "Diagnostics passed; complete Test with Calculator before agent use.", version=system_version, executable=executable, hash_status="system-override", remediation="Test with Calculator.")
        return CuaReadiness(ReadinessCode.READY, "Approved system Cua is ready.", version=system_version, executable=executable, hash_status="system-override")
    if not installed or not executable:
        return CuaReadiness(ReadinessCode.NOT_INSTALLED, "Cua Driver is not installed.", version=str(manifest["version"]), remediation="Select Install Cua Driver.")
    if str(installed.get("version")) != str(manifest["version"]):
        return CuaReadiness(ReadinessCode.VERSION_MISMATCH, "Installed Cua Driver version is outside the reviewed pin.", version=str(installed.get("version") or ""), executable=executable, remediation="Install the reviewed version.")
    if str(installed.get("archive_sha256") or "").lower() != str(asset["sha256"]).lower():
        return CuaReadiness(ReadinessCode.HASH_MISMATCH, "Cua Driver integrity metadata does not match the reviewed artifact.", version=str(installed.get("version") or ""), executable=executable, hash_status="mismatch", remediation="Reinstall the reviewed artifact.")
    if asset.get("platform_key") == "macos-universal" and not _is_macos_app_bundle_executable(executable):
        return CuaReadiness(
            ReadinessCode.DEGRADED,
            "The managed macOS helper needs repair before Computer Use can run.",
            version=str(manifest["version"]),
            executable=executable,
            hash_status="verified",
            remediation="Reinstall Computer Use to repair its macOS helper.",
            details={"repair_required": True},
        )
    if not installed.get("doctor_ok"):
        return CuaReadiness(ReadinessCode.DEGRADED, "Cua Driver integrity is verified; run diagnostics before use.", version=str(manifest["version"]), executable=executable, hash_status="verified", remediation="Run diagnostics.")
    if not installed.get("observation_ok"):
        return CuaReadiness(ReadinessCode.DEGRADED, "Diagnostics passed; complete Test with Calculator before agent use.", version=str(manifest["version"]), executable=executable, hash_status="verified", remediation="Test with Calculator.")
    return CuaReadiness(ReadinessCode.READY, "Cua Driver is ready for task-scoped Computer Use.", version=str(manifest["version"]), executable=executable, hash_status="verified")


def _set_readiness_marker(key: str, value: bool) -> None:
    settings = _read_json(_settings_path())
    if settings.get("allow_system_cua") and settings.get("system_cua_path"):
        settings[f"system_cua_{key}"] = bool(value)
        _write_settings(settings)
        return
    from row_bot.mcp_client.requirements import _read_manifest, _write_manifest

    manifest = _read_manifest(RUNTIME_ID)
    if manifest:
        manifest[key] = bool(value)
        _write_manifest(RUNTIME_ID, manifest)


def mark_cua_observation_verified() -> None:
    require_cua_disclosure()
    _set_readiness_marker("observation_ok", True)


def install_cua_runtime(
    *,
    progress: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Any:
    """Explicit install action. Merely enabling Computer Use never calls this."""

    require_cua_disclosure()
    asset = selected_asset()
    if asset is None:
        raise RuntimeError("No reviewed Cua Driver artifact exists for this platform")
    manifest = load_cua_manifest()
    from row_bot.mcp_client.requirements import install_pinned_archive_runtime

    return install_pinned_archive_runtime(
        RUNTIME_ID,
        version=str(manifest["version"]),
        url=str(asset["url"]),
        sha256=str(asset["sha256"]),
        asset_name=str(asset["name"]),
        executable_candidates=tuple(str(value) for value in asset["executable_candidates"]),
        progress=progress,
        cancelled=cancelled,
        preserve_top_level_directory=asset.get("platform_key") == "macos-universal",
    )


def uninstall_cua_runtime() -> bool:
    """Remove only Row-Bot's managed Cua runtime after local UI confirmation."""

    from row_bot.mcp_client.requirements import RUNTIMES_DIR

    root = (RUNTIMES_DIR / RUNTIME_ID).resolve()
    runtimes = RUNTIMES_DIR.resolve()
    if root.parent != runtimes:
        raise RuntimeError("Refusing to remove a runtime outside Row-Bot's managed runtime directory")
    try:
        from row_bot.computer_use.service import get_computer_use_service

        get_computer_use_service().stop()
    except Exception:
        pass
    if not root.exists():
        return False
    shutil.rmtree(root)
    return True


def run_cua_diagnostics() -> CuaReadiness:
    """Start the reviewed binary only after disclosure and normalize health v1."""

    require_cua_disclosure()
    state = readiness(enabled=True)
    if state.code not in {ReadinessCode.READY, ReadinessCode.DEGRADED} or not state.executable:
        return state
    if state.details and state.details.get("repair_required"):
        return state
    from row_bot.computer_use.client import CuaClient
    from row_bot.mcp_client.requirements import finalize_pinned_archive_runtime, rollback_pinned_archive_runtime

    def _rollback_managed_candidate() -> None:
        if state.hash_status == "verified":
            rollback_pinned_archive_runtime(RUNTIME_ID)

    manifest = load_cua_manifest()
    client = CuaClient(
        state.executable,
        contract_version=str(manifest["version"]),
        capabilities=frozenset(
            str(value) for value in manifest.get("reviewed_service_capabilities") or []
        ),
    )
    try:
        client.start()
        response = client.call_internal("health_report")
    except Exception:
        _rollback_managed_candidate()
        raise
    finally:
        client.close()
    report = response.structured
    if str(report.get("schema_version")) != "1":
        _rollback_managed_candidate()
        return CuaReadiness(ReadinessCode.FAILED, "Unsupported Cua health-report schema.", state.version, state.executable, state.hash_status)
    overall = str(report.get("overall") or "failed")
    if overall == "ok":
        _set_readiness_marker("doctor_ok", True)
        if state.hash_status == "verified":
            finalize_pinned_archive_runtime(RUNTIME_ID)
        updated = readiness(enabled=True)
        return CuaReadiness(updated.code, "Cua Driver diagnostics passed." + (" Complete Test with Calculator before agent use." if updated.code is not ReadinessCode.READY else ""), state.version, state.executable, state.hash_status, updated.remediation, report)
    failed = [check for check in report.get("checks") or [] if isinstance(check, dict) and check.get("status") == "fail"]
    permission = any(str(item.get("name") or "").startswith(("tcc_", "ax_", "screen_capture")) for item in failed)
    code = ReadinessCode.PERMISSION_MISSING if permission else ReadinessCode.DEGRADED if overall == "degraded" else ReadinessCode.FAILED
    hints = "; ".join(str(item.get("hint") or item.get("message") or "") for item in failed)
    if permission:
        hints = (
            "Allow Row-Bot in macOS Privacy & Security, then return here and check again."
            if platform.system().lower() == "darwin"
            else "Grant the required screen and control permissions, then check again."
        )
    _rollback_managed_candidate()
    return CuaReadiness(code, "Cua Driver diagnostics need attention.", state.version, state.executable, state.hash_status, hints, report)
