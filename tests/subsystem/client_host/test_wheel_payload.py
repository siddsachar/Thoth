from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from setuptools import Distribution
from setuptools.errors import SetupError

from scripts.client_build import ClientBuildPy, select_client_payload


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    package = tmp_path / "src/row_bot"
    root = package / "static/client-v2"
    (root / "assets").mkdir(parents=True)
    (root / ".vite").mkdir()
    package.joinpath("__init__.py").write_text("")
    package.joinpath("example.py").write_text("value = 42\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/client_build.py").write_text("# isolated build-helper fixture\n")
    files = {"index.html": b'<script src="/app-v2/assets/index-abcdef12.js"></script>',
             "assets/index-abcdef12.js": b"export const fixture = true;"}
    for name, data in files.items():
        (root / name).write_bytes(data)
    (root / "asset-manifest.json").write_text(json.dumps({"version": 1, "files": {
        name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()}}))
    (root / ".vite/manifest.json").write_text(json.dumps({"index.html": {
        "isEntry": True, "file": "assets/index-abcdef12.js"}}))
    return tmp_path


def command(project: Path) -> ClientBuildPy:
    distribution = Distribution({"name": "fixture", "version": "1.0", "package_dir": {"": "src"},
                                 "packages": ["row_bot", "row_bot.static", "row_bot.static.client-v2",
                                              "row_bot.static.client-v2.assets"],
                                 "package_data": {"row_bot": ["static/client-v2/assets/*"]},
                                 "include_package_data": False})
    distribution.script_name = "setup.py"
    build = distribution.get_command_obj("build")
    build.build_base = str(project / "build")
    result = ClientBuildPy(distribution)
    distribution.command_obj["build_py"] = result
    result.ensure_finalized()
    result.compile = False
    return result


def test_build_copies_only_manifest_files_and_retains_both_private_manifests(project: Path) -> None:
    root = project / "src/row_bot/static/client-v2"
    for name in ("assets/old-abcdefgh.js", "assets/debug.js.map", "assets/secret.txt", "assets/unsafe.py"):
        (root / name).write_bytes(b"unlisted fixture")
    expected = select_client_payload(root)
    original = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    build = command(project)
    build.run()
    destination = Path(build.build_lib) / "row_bot/static/client-v2"
    actual = {path.relative_to(destination).as_posix(): path.read_bytes()
              for path in destination.rglob("*") if path.is_file()}
    assert actual == expected
    assert (Path(build.build_lib) / "row_bot/example.py").read_text() == "value = 42\n"
    assert original == {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert "scripts/client_build.py" in build.get_source_files()


def test_reused_build_and_failed_wheel_stages_are_never_merged_or_removed(project: Path) -> None:
    first = command(project)
    stale_wheel = project / "build/old-wheel"
    stale_wheel.mkdir(parents=True)
    marker = stale_wheel / "stale-sentinel.txt"
    marker.write_bytes(b"keep previous output")
    wheel = SimpleNamespace(bdist_dir=str(stale_wheel))
    first.distribution.command_obj["bdist_wheel"] = wheel
    first.run()
    first_output = Path(first.build_lib)
    (first_output / "row_bot/static/client-v2/assets/old-abcdefgh.js").write_bytes(b"old output")
    second = command(project)
    second.distribution.command_obj["bdist_wheel"] = wheel
    second.run()
    assert Path(second.build_lib) != first_output
    assert Path(wheel.bdist_dir).parent == Path(second.build_lib).parent
    assert not (Path(second.build_lib) / "row_bot/static/client-v2/assets/old-abcdefgh.js").exists()
    assert (first_output / "row_bot/static/client-v2/assets/old-abcdefgh.js").read_bytes() == b"old output"
    assert marker.read_bytes() == b"keep previous output"
    assert second.get_finalized_command("build").build_lib == second.build_lib


@pytest.mark.parametrize("change", ["missing_root", "missing_asset", "missing_vite", "bad_json", "bad_hash",
                                    "wrong_size", "traversal", "unhashed", "too_large", "missing_reference"])
def test_invalid_payload_fails_before_any_build_output(project: Path, change: str) -> None:
    root = project / "src/row_bot/static/client-v2"
    inventory = root / "asset-manifest.json"
    metadata = json.loads(inventory.read_text())
    if change == "missing_root":
        root.rename(root.with_name("retained-fixture"))
    elif change == "missing_asset":
        (root / "assets/index-abcdef12.js").unlink()
    elif change == "missing_vite":
        (root / ".vite/manifest.json").unlink()
    elif change == "bad_json":
        inventory.write_text("not-json fixture")
    elif change == "missing_reference":
        vite = root / ".vite/manifest.json"
        value = json.loads(vite.read_text())
        value["index.html"]["dynamicImports"] = ["missing"]
        vite.write_text(json.dumps(value))
    else:
        entry = metadata["files"]["assets/index-abcdef12.js"]
        if change == "bad_hash":
            entry["sha256"] = "0" * 64
        elif change == "wrong_size":
            entry["size"] += 1
        elif change == "too_large":
            entry["size"] = 9 * 1024 * 1024
        else:
            metadata["files"]["../private.txt" if change == "traversal" else "assets/index.js"] = entry
        inventory.write_text(json.dumps(metadata))
    build = command(project)
    with pytest.raises(SetupError, match="missing or invalid"):
        build.run()
    assert not (project / "build").exists()


def test_payload_rejects_windows_reparse_paths_without_native_links(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = project / "src/row_bot/static/client-v2"
    original = Path.lstat
    class LinkedStat:
        st_mode = 0o100644
        st_file_attributes = 0x400
    monkeypatch.setattr(Path, "lstat", lambda path, **kwargs: LinkedStat() if path == root / "assets" else original(path, **kwargs))
    with pytest.raises(SetupError, match="missing or invalid"):
        select_client_payload(root)


def test_editable_mode_does_not_create_a_wheel_stage(project: Path) -> None:
    build = command(project)
    build.editable_mode = True
    root = project / "src/row_bot/static/client-v2"
    root.rename(root.with_name("retained-fixture"))
    build.manifest_files = {}
    assert build.find_data_files("row_bot", "src/row_bot") == []
    build.run()
    assert not (project / "build").exists()
