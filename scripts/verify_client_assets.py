"""Verify a local frontend build or staged installer payload without modifying it."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


# Source/install payloads use the same src layout; this script is build-only.
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from row_bot.client_assets import AssetValidationError, _read_regular, load_client_assets  # noqa: E402


_PRIVATE_MANIFESTS = ("asset-manifest.json", ".vite/manifest.json")


class PayloadValidationError(ValueError):
    """A payload contains unverified content or differs from its source build."""


@dataclass(frozen=True, slots=True)
class AssetPayloadReport:
    asset_count: int
    total_bytes: int
    asset_set_sha256: str
    strict: bool
    compared: bool


def _check_inventory_only(root: Path, expected: set[str]) -> None:
    """Reject every extra file and linked directory in a staged payload."""
    visited = 0
    found: set[str] = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            visited += 1
            if visited > 1024:
                raise PayloadValidationError("too_many_payload_entries")
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PayloadValidationError("linked_payload_path")
            relative = path.relative_to(root).as_posix()
            if name in directories and not any(value.startswith(relative + "/") for value in expected):
                raise PayloadValidationError("unexpected_payload_directory")
            if name in files:
                if not stat.S_ISREG(info.st_mode):
                    raise PayloadValidationError("invalid_payload_file")
                found.add(relative)
    if found != expected:
        raise PayloadValidationError("unexpected_payload_file")


def verify_client_asset_payload(
    root: Path, *, compare_to: Path | None = None, strict: bool = False,
) -> AssetPayloadReport:
    """Reuse the runtime validator and optionally prove an exact staged asset set.

    No build, package copy, network, application startup or user-data access is
    performed. Comparison validates both roots and compares verified asset bytes
    and the exact bytes of both private manifests, including JSON formatting.
    """
    assets = load_client_assets(root)
    if strict:
        _check_inventory_only(root, set(assets) | {"asset-manifest.json", ".vite/manifest.json"})
    if compare_to is not None:
        source = load_client_assets(compare_to)
        if assets != source:
            raise PayloadValidationError("payload_mismatch")
        for name in _PRIVATE_MANIFESTS:
            # Both manifests passed the runtime parser. Reuse its bounded,
            # regular-file/reparse-safe reader instead of an unbounded read.
            if _read_regular(root, name, 256 * 1024) != _read_regular(compare_to, name, 256 * 1024):
                raise PayloadValidationError("payload_manifest_mismatch")
    inventory = [(name, asset.sha256, len(asset.content)) for name, asset in sorted(assets.items())]
    digest = hashlib.sha256(json.dumps(inventory, separators=(",", ":")).encode()).hexdigest()
    return AssetPayloadReport(len(assets), sum(len(asset.content) for asset in assets.values()),
                              digest, strict, compare_to is not None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Local build or staged client-v2 directory")
    parser.add_argument("--compare", type=Path, help="Validate and compare the original build assets")
    parser.add_argument("--strict", action="store_true", help="Reject files absent from the generated inventory")
    args = parser.parse_args(argv)
    try:
        report = verify_client_asset_payload(args.root, compare_to=args.compare, strict=args.strict)
    except (AssetValidationError, PayloadValidationError, OSError):
        # Diagnostics never disclose paths, payload bytes or exception details.
        print("Client asset verification failed: missing, invalid or mismatched local payload", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
