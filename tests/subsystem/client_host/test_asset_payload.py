from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

import pytest

from scripts.verify_client_assets import PayloadValidationError, main, verify_client_asset_payload
from tests.subsystem.client_host.test_assets import build  # noqa: F401 - shared deterministic build fixture


def test_valid_payload_output_matches_build_without_writes(build: Path, tmp_path: Path, capsys) -> None:
    staged = tmp_path / "staged"
    shutil.copytree(build, staged)
    for name in ("asset-manifest.json", ".vite/manifest.json"):
        assert (staged / name).read_bytes() == (build / name).read_bytes()
    before = {path.relative_to(staged).as_posix(): path.read_bytes() for path in staged.rglob("*") if path.is_file()}
    assert main(["--root", str(staged), "--compare", str(build), "--strict"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["asset_count"] == 3
    assert output["total_bytes"] > 0
    assert len(output["asset_set_sha256"]) == 64
    assert output["strict"] and output["compared"]
    assert before == {path.relative_to(staged).as_posix(): path.read_bytes() for path in staged.rglob("*") if path.is_file()}
    assert verify_client_asset_payload(build).asset_set_sha256 == output["asset_set_sha256"]


@pytest.mark.parametrize("change", ["missing_asset", "missing_vite", "bad_digest", "bad_json", "traversal"])
def test_cli_rejects_invalid_build_without_disclosing_contents(build: Path, capsys, change: str) -> None:
    inventory = build / "asset-manifest.json"
    if change == "missing_asset":
        (build / "assets/index-abcdef12.js").unlink()
    elif change == "missing_vite":
        (build / ".vite/manifest.json").unlink()
    elif change == "bad_json":
        inventory.write_text("private sentinel not JSON")
    else:
        metadata = json.loads(inventory.read_text())
        if change == "bad_digest":
            metadata["files"]["index.html"]["sha256"] = "a" * 64
        else:
            metadata["files"]["../../private-sentinel"] = metadata["files"].pop("index.html")
        inventory.write_text(json.dumps(metadata))
    assert main(["--root", str(build)]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "private" not in output.err and str(build) not in output.err


def test_cli_rejects_missing_root_without_creating_it(tmp_path: Path, capsys) -> None:
    root = tmp_path / "absent"
    assert main(["--root", str(root)]) == 1
    assert not root.exists()
    assert "verification failed" in capsys.readouterr().err


def test_comparison_rejects_a_valid_but_different_build(build: Path, tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    shutil.copytree(build, staged)
    data = b"export const changed = true;"
    (staged / "assets/index-abcdef12.js").write_bytes(data)
    inventory = staged / "asset-manifest.json"
    metadata = json.loads(inventory.read_text())
    metadata["files"]["assets/index-abcdef12.js"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    inventory.write_text(json.dumps(metadata))
    verify_client_asset_payload(staged)
    with pytest.raises(PayloadValidationError, match="payload_mismatch"):
        verify_client_asset_payload(staged, compare_to=build)


@pytest.mark.parametrize("name", ["asset-manifest.json", ".vite/manifest.json"])
def test_same_assets_with_different_private_manifest_bytes_fail_comparison(build: Path, tmp_path: Path,
                                                                         name: str) -> None:
    staged = tmp_path / "staged"
    shutil.copytree(build, staged)
    manifest = staged / name
    metadata = json.loads(manifest.read_text())
    if name == ".vite/manifest.json":
        metadata["index.html"]["fixture_metadata"] = "changed but still valid"
    # Inventory whitespace alone is a byte difference; copied metadata must be exact.
    manifest.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    assert verify_client_asset_payload(staged).asset_set_sha256 == verify_client_asset_payload(build).asset_set_sha256
    with pytest.raises(PayloadValidationError, match="payload_manifest_mismatch"):
        verify_client_asset_payload(staged, compare_to=build, strict=True)


@pytest.mark.parametrize("name", ["assets/old-abcdef99.js", "assets/index.js.map", "node_modules", "private.txt"])
def test_strict_staging_rejects_unlisted_assets_maps_and_tooling(build: Path, name: str) -> None:
    path = build / name
    if name == "node_modules":
        path.mkdir()
    else:
        path.write_bytes(b"unlisted fixture")
    # Runtime development may retain old assets, but never serves those entries.
    verify_client_asset_payload(build)
    with pytest.raises(PayloadValidationError, match="unexpected_payload"):
        verify_client_asset_payload(build, strict=True)


def test_strict_staging_rejects_extra_reparse_without_native_symlinks(build: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = build / "extra"
    extra.mkdir()
    original = Path.lstat
    class LinkedStat:
        st_mode = 0o120777
        st_file_attributes = 0x400
    monkeypatch.setattr(Path, "lstat", lambda path, **kwargs: LinkedStat() if path == extra else original(path, **kwargs))
    with pytest.raises(PayloadValidationError, match="linked_payload_path"):
        verify_client_asset_payload(build, strict=True)
