#!/usr/bin/env python3
"""Run an explicit, isolated macOS probe against the reviewed Cua runtime."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


PERMISSION_CHECK_PREFIXES = ("tcc_", "ax_", "screen_capture")


def classify_diagnostics(
    code: str,
    details: Mapping[str, Any] | None,
) -> tuple[str, bool]:
    """Classify a real driver report without treating missing TCC as readiness."""

    report = details if isinstance(details, Mapping) else {}
    schema_version = str(report.get("schema_version") or "")
    overall = str(report.get("overall") or "").casefold()
    checks = report.get("checks")
    failed_names = (
        [
            str(item.get("name") or "")
            for item in checks
            if isinstance(item, Mapping) and item.get("status") == "fail"
        ]
        if isinstance(checks, list)
        else []
    )

    if schema_version != "1":
        return "failed", False
    if overall == "ok" and code in {"ready", "degraded"}:
        return "diagnostics_passed", True
    if (
        code == "permission_missing"
        and failed_names
        and all(name.startswith(PERMISSION_CHECK_PREFIXES) for name in failed_names)
    ):
        return "permission_pending", True
    return "failed", False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install and start the reviewed Cua runtime on an isolated macOS host. "
            "This probe does not grant privacy permissions or mark Calculator verification complete."
        )
    )
    parser.add_argument(
        "--accept-cua-notice",
        action="store_true",
        help="Confirm explicit acceptance of the reviewed Cua telemetry notice for this run.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="New, empty Row-Bot data directory dedicated to the probe.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path for the sanitized JSON probe report.",
    )
    return parser


def _prepare_data_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    forbidden = {Path.home().resolve(), Path(resolved.anchor).resolve()}
    if resolved in forbidden:
        raise ValueError(
            "Probe data directory must not be a home or filesystem root directory"
        )
    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError("Probe data directory must be empty")
    resolved.mkdir(parents=True, exist_ok=True)
    os.environ["ROW_BOT_DATA_DIR"] = str(resolved)
    return resolved


def _run_command(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _require_command(
    arguments: Sequence[str], label: str
) -> subprocess.CompletedProcess[str]:
    completed = _run_command(arguments)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"{label} failed: {detail or 'no diagnostic output'}")
    return completed


def _bundle_report(executable: Path) -> dict[str, Any]:
    bundle = next(
        (parent for parent in executable.parents if parent.suffix == ".app"), None
    )
    if bundle is None:
        raise RuntimeError("Managed Cua executable is not inside a macOS app bundle")
    plist = bundle / "Contents" / "Info.plist"
    if not plist.is_file():
        raise RuntimeError("Managed Cua app bundle does not contain Info.plist")

    file_result = _require_command(("file", str(executable)), "Mach-O inspection")
    file_description = file_result.stdout.strip().split(": ", 1)[-1]
    if "Mach-O" not in file_description:
        raise RuntimeError("Managed Cua executable is not Mach-O")

    lipo_result = _require_command(
        ("lipo", "-archs", str(executable)), "Architecture inspection"
    )
    architectures = lipo_result.stdout.strip().split()
    host_arch = (
        "x86_64" if platform.machine().casefold() in {"amd64", "x86_64"} else "arm64"
    )
    if host_arch not in architectures:
        raise RuntimeError(
            f"Managed Cua executable does not contain host architecture {host_arch}"
        )

    _require_command(("plutil", "-lint", str(plist)), "Info.plist validation")
    signature = _require_command(
        ("codesign", "--verify", "--deep", "--strict", str(bundle)),
        "Code-signature validation",
    )
    return {
        "architectures": architectures,
        "file_description": file_description,
        "plist_valid": True,
        "signature_valid": signature.returncode == 0,
    }


def _sanitize_health_report(details: Mapping[str, Any] | None) -> dict[str, Any]:
    report = details if isinstance(details, Mapping) else {}
    checks = report.get("checks")
    safe_checks = (
        [
            {
                "name": str(item.get("name") or ""),
                "status": str(item.get("status") or ""),
            }
            for item in checks
            if isinstance(item, Mapping)
        ]
        if isinstance(checks, list)
        else []
    )
    return {
        "schema_version": str(report.get("schema_version") or ""),
        "overall": str(report.get("overall") or ""),
        "checks": safe_checks,
    }


def run_probe(report: dict[str, Any]) -> bool:
    from row_bot.computer_use.readiness import (
        acknowledge_disclosure,
        disclosure_acknowledged,
        install_cua_runtime,
        load_cua_manifest,
        readiness,
        run_cua_diagnostics,
        selected_asset,
    )

    if platform.system() != "Darwin":
        raise RuntimeError("The real Cua probe must run on macOS")

    manifest = load_cua_manifest()
    asset = selected_asset()
    if asset is None or asset.get("platform_key") != "macos-universal":
        raise RuntimeError("The reviewed universal macOS Cua asset was not selected")

    report.update(
        {
            "schema_version": 1,
            "status": "running",
            "platform": {
                "system": platform.system(),
                "release": platform.mac_ver()[0],
                "machine": platform.machine(),
            },
            "runtime": {
                "version": str(manifest["version"]),
                "asset_name": str(asset["name"]),
                "platform_key": str(asset["platform_key"]),
                "expected_sha256": str(asset["sha256"]),
            },
        }
    )

    acknowledge_disclosure()
    if not disclosure_acknowledged():
        raise RuntimeError("Cua disclosure acknowledgement was not recorded")
    report["disclosure"] = {
        "accepted": True,
        "notice_version": int(manifest["telemetry_notice_version"]),
    }

    install_result = install_cua_runtime(
        progress=lambda message: print(message, flush=True)
    )
    if not install_result.ok:
        raise RuntimeError(install_result.message)

    installed = readiness(enabled=True)
    executable = Path(installed.executable)
    if installed.hash_status != "verified" or not executable.is_file():
        raise RuntimeError("Managed Cua runtime did not reach verified installed state")
    report["bundle"] = _bundle_report(executable)

    diagnostic = run_cua_diagnostics()
    diagnostic_details = _sanitize_health_report(diagnostic.details)
    status, accepted = classify_diagnostics(diagnostic.code.value, diagnostic_details)
    report["status"] = status
    report["diagnostics"] = {
        "readiness_code": diagnostic.code.value,
        "health": diagnostic_details,
    }
    report["calculator_observation_verified"] = False
    return accepted


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.accept_cua_notice:
        print("Explicit --accept-cua-notice is required.", file=sys.stderr)
        return 2

    report: dict[str, Any] = {"schema_version": 1, "status": "failed"}
    accepted = False
    try:
        _prepare_data_dir(args.data_dir)
        accepted = run_probe(report)
    except Exception as exc:
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        _write_report(args.output, report)
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
