import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

import row_bot.launcher as launcher
import pytest
import row_bot.updater as updater
from scripts import app_payload_manifest
from scripts import check_linux_native_baseline

REQUIRED_RUNTIME_PACKAGES = (
    "voice",
    "buddy",
    "migration",
    "mobile",
    "providers",
    "mcp_client",
    "plugins",
    "skills_hub",
    "tools",
    "channels",
    "ui",
    "designer",
    "developer",
    "utils",
)


def _linux_launcher_template() -> str:
    script = Path("installer/build_linux_app.sh").read_text(encoding="utf-8")
    start = script.index("cat > \"$PACKAGE_ROOT/bin/row-bot\" <<'LAUNCHER'")
    launcher_start = script.index("#!/usr/bin/env bash", start)
    launcher_end = script.index("\nLAUNCHER", launcher_start)
    return script[launcher_start:launcher_end]


def _win_source_path(relative_path: str) -> str:
    return "..\\" + relative_path.replace("/", "\\")


def _windows_installer_sources() -> set[str]:
    iss = Path("installer/row_bot_setup.iss").read_text(encoding="utf-8")
    return set(re.findall(r'Source:\s+"([^"]+)"', iss))


def _windows_source_covers_dir(sources: set[str], directory: str) -> bool:
    prefix = _win_source_path(directory + "/")
    return any(source.startswith(prefix) for source in sources)


def _read_root_wrapper_source(relative_path: str) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; sys.stdout.write(Path(sys.argv[1]).read_text(encoding='utf-8'))",
            relative_path,
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    return result.stdout


def test_linux_asset_selection(monkeypatch):
    monkeypatch.setattr(updater.platform, "system", lambda: "Linux")
    monkeypatch.setattr(updater.platform, "machine", lambda: "x86_64")
    body = (
        "Notes\n\n<!-- row-bot-update-manifest -->\n"
        "```manifest\nschema: 1\nfiles:\n"
        "  Row-Bot-3.21.0-Linux-x86_64.tar.gz: sha256=" + "a" * 64 + "\n"
        "```\n"
    )
    release = {
        "tag_name": "v3.21.0",
        "prerelease": False,
        "published_at": "2026-05-04T12:00:00Z",
        "html_url": "https://github.com/siddsachar/row-bot/releases/tag/v3.21.0",
        "body": body,
        "assets": [{
            "name": "Row-Bot-3.21.0-Linux-x86_64.tar.gz",
            "size": 123,
            "browser_download_url": "https://github.com/siddsachar/row-bot/releases/download/v3.21.0/Row-Bot-3.21.0-Linux-x86_64.tar.gz",
        }],
    }

    info = updater._parse_release(release, "stable")

    assert info is not None
    assert info.asset_name == "Row-Bot-3.21.0-Linux-x86_64.tar.gz"
    assert info.sha256 == "a" * 64


def test_windows_asset_selection_accepts_hyphenated_installer(monkeypatch):
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    body = (
        "Notes\n\n<!-- row-bot-update-manifest -->\n"
        "```manifest\nschema: 1\nfiles:\n"
        "  Row-Bot-3.21.0-Windows-x64.exe: sha256=" + "c" * 64 + "\n"
        "```\n"
    )
    release = {
        "tag_name": "v3.21.0",
        "prerelease": False,
        "published_at": "2026-05-04T12:00:00Z",
        "html_url": "https://github.com/siddsachar/row-bot/releases/tag/v3.21.0",
        "body": body,
        "assets": [{
            "name": "Row-Bot-3.21.0-Windows-x64.exe",
            "size": 123,
            "browser_download_url": "https://github.com/siddsachar/row-bot/releases/download/v3.21.0/Row-Bot-3.21.0-Windows-x64.exe",
        }],
    }

    info = updater._parse_release(release, "stable")

    assert info is not None
    assert info.asset_name == "Row-Bot-3.21.0-Windows-x64.exe"
    assert info.sha256 == "c" * 64


def test_windows_asset_selection_accepts_legacy_setup_name(monkeypatch):
    monkeypatch.setattr(updater.platform, "system", lambda: "Windows")
    body = (
        "Notes\n\n<!-- row-bot-update-manifest -->\n"
        "```manifest\nschema: 1\nfiles:\n"
        "  RowBotSetup_3.21.0.exe: sha256=" + "d" * 64 + "\n"
        "```\n"
    )
    release = {
        "tag_name": "v3.21.0",
        "prerelease": False,
        "published_at": "2026-05-04T12:00:00Z",
        "html_url": "https://github.com/siddsachar/row-bot/releases/tag/v3.21.0",
        "body": body,
        "assets": [{
            "name": "RowBotSetup_3.21.0.exe",
            "size": 123,
            "browser_download_url": "https://github.com/siddsachar/row-bot/releases/download/v3.21.0/RowBotSetup_3.21.0.exe",
        }],
    }

    info = updater._parse_release(release, "stable")

    assert info is not None
    assert info.asset_name == "RowBotSetup_3.21.0.exe"
    assert info.sha256 == "d" * 64


def test_root_launch_entrypoints_remain_source_compatible():
    manifest = app_payload_manifest.build_manifest(Path("."))
    app_src = _read_root_wrapper_source("app.py")
    launcher_src = _read_root_wrapper_source("launcher.py")
    app_impl_src = Path("src/row_bot/app.py").read_text(encoding="utf-8")

    assert "app.py" in manifest["root_python_files"]
    assert "launcher.py" in manifest["root_python_files"]
    assert 'runpy.run_module("row_bot.app", run_name="__main__")' in app_src
    assert 'if __name__ in {"__main__", "__mp_main__"}:' in app_impl_src
    assert "ui.run(**_run_kwargs)" in app_impl_src
    assert "from row_bot.launcher import main" in launcher_src
    assert 'if __name__ == "__main__":\n    main()' in launcher_src


def test_app_payload_manifest_declares_required_runtime_payload():
    manifest = app_payload_manifest.build_manifest(Path("."))
    payload_dirs = set(manifest["payload_dirs"])
    asset_dirs = set(manifest["asset_dirs"])

    assert "src/row_bot" in payload_dirs
    for package in REQUIRED_RUNTIME_PACKAGES:
        assert Path("src/row_bot", package).is_dir(), f"runtime package missing: {package}"
    assert {"static", "sounds", "tool_guides", "bundled_skills"} <= asset_dirs
    assert "pyproject.toml" in manifest["root_files"]
    assert "uv.lock" in manifest["root_files"]
    assert "requirements.txt" in manifest["root_files"]
    assert "row-bot.ico" in manifest["root_files"]
    assert "scripts/verify_runtime_dependencies.py" in manifest["runtime_script_files"]
    assert "docs/row_bot_glyph_256.png" in manifest["linux_icon_candidates"]
    assert "docs/row_bot_glyph.png" in manifest["mac_icon_source_candidates"]
    assert "debug_tools.py" not in manifest["root_python_files"]

    for relative_path in app_payload_manifest.app_payload_paths(Path(".")):
        assert Path(relative_path).exists(), f"manifest path missing: {relative_path}"


def test_mac_and_linux_builders_copy_from_app_payload_manifest():
    linux_builder = Path("installer/build_linux_app.sh").read_text(encoding="utf-8")
    mac_builder = Path("installer/build_mac_app.sh").read_text(encoding="utf-8")

    for builder in (linux_builder, mac_builder):
        assert "scripts/app_payload_manifest.py" in builder
        assert 'mkdir -p "$(dirname "$APP_SRC/$pkg")"' in builder
        assert 'mkdir -p "$(dirname "$APP_SRC/$dir")"' in builder
        for category in (
            "root_python_files",
            "root_files",
            "runtime_script_files",
            "payload_dirs",
            "asset_dirs",
        ):
            assert f"--category {category}" in builder

    assert "--category linux_icon_candidates" in linux_builder
    assert "--category mac_icon_source_candidates" in mac_builder


def test_windows_installer_payload_matches_app_manifest_contract():
    manifest = app_payload_manifest.build_manifest(Path("."))
    sources = _windows_installer_sources()

    expected_files = (
        manifest["root_python_files"]
        + manifest["root_files"]
        + manifest["runtime_script_files"]
    )
    missing_files = [path for path in expected_files if _win_source_path(path) not in sources]
    assert not missing_files, f"Windows installer is missing manifest files: {missing_files}"

    expected_dirs = manifest["payload_dirs"] + manifest["asset_dirs"]
    missing_dirs = [path for path in expected_dirs if not _windows_source_covers_dir(sources, path)]
    assert not missing_dirs, f"Windows installer is missing manifest directories: {missing_dirs}"


def test_linux_install_marker_is_not_dev_install(monkeypatch, tmp_path):
    app_root = tmp_path / "current" / "app"
    app_root.mkdir(parents=True)
    marker = tmp_path / "current" / "install_info.json"
    marker.write_text(json.dumps({
        "platform": "linux",
        "install_kind": "xdg-user-tarball",
        "version": "3.21.0",
    }), encoding="utf-8")

    monkeypatch.setattr(updater.platform, "system", lambda: "Linux")

    assert updater._linux_install_root(app_root) == tmp_path / "current"


def test_linux_safe_tar_extraction_rejects_traversal(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    payload = tmp_path / "payload.txt"
    payload.write_text("bad", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(payload, arcname="../payload.txt")

    try:
        updater._safe_extract_tar(archive, tmp_path / "out")
    except updater.UpdateError as exc:
        assert "unsafe path" in str(exc)
    else:
        raise AssertionError("expected unsafe tar path to be rejected")


def test_linux_tarball_installs_into_xdg_tree(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("Linux tarball installer uses POSIX symlinks")

    package_root = tmp_path / "Row-Bot-3.21.0-Linux-x86_64"
    (package_root / "bin").mkdir(parents=True)
    (package_root / "app").mkdir()
    (package_root / "python" / "bin").mkdir(parents=True)
    (package_root / "share" / "applications").mkdir(parents=True)
    (package_root / "share" / "icons" / "hicolor" / "256x256" / "apps").mkdir(parents=True)
    (package_root / "bin" / "row-bot").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (package_root / "python" / "bin" / "python3").write_text("", encoding="utf-8")
    (package_root / "share" / "applications" / "ai.row-bot.RowBot.desktop").write_text(
        "[Desktop Entry]\nExec=row-bot\n", encoding="utf-8"
    )
    (package_root / "share" / "icons" / "hicolor" / "256x256" / "apps" / "row-bot.png").write_bytes(b"png")
    (package_root / "install_info.json").write_text(json.dumps({
        "platform": "linux",
        "install_kind": "xdg-user-tarball",
        "version": "3.21.0",
    }), encoding="utf-8")
    archive = tmp_path / "Row-Bot-3.21.0-Linux-x86_64.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(package_root, arcname=package_root.name)

    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(updater.platform, "system", lambda: "Linux")
    monkeypatch.setattr(updater.shutil, "which", lambda _cmd: None)

    launcher_path = updater._install_linux_tarball(archive)

    assert launcher_path == home / ".local" / "bin" / "row-bot"
    assert launcher_path.is_symlink()
    assert (xdg / "row-bot" / "current").is_symlink()
    assert (xdg / "row-bot" / "releases" / "3.21.0" / "install_info.json").exists()
    desktop_text = (xdg / "applications" / "ai.row-bot.RowBot.desktop").read_text(encoding="utf-8")
    assert f"Exec={launcher_path}" in desktop_text


def test_linux_build_script_declares_expected_package_contract():
    script = Path("installer/build_linux_app.sh").read_text(encoding="utf-8")
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    manifest = app_payload_manifest.build_manifest(Path("."))

    assert "unknown-linux-gnu-install_only" in script
    assert 'PACKAGE_NAME="Row-Bot-${VERSION}-Linux-${PACKAGE_ARCH}"' in script
    assert 'TARBALL="$DIST_DIR/${PACKAGE_NAME}.tar.gz"' in script
    assert 'while [ -L "$SOURCE" ]; do' in script
    assert 'TARGET="$(readlink "$SOURCE")"' in script
    assert 'ROOT="$(cd -P "$(dirname "$SOURCE")/.." && pwd)"' in script
    assert "--browser --no-tray" in script
    assert "share/applications/ai.row-bot.RowBot.desktop" in script
    assert "install_kind\": \"xdg-user-tarball" in script
    assert "LAUNCH_CMD=\"row-bot\"" in script
    assert "ROW_BOT_SUPPRESS_INSTALL_PATH_HINT" in script
    assert "export PATH=\"$HOME/.local/bin:$PATH\"" in script
    assert "Run: $LAUNCH_CMD" in script
    assert "# This file is generated from pyproject.toml and uv.lock." in requirements
    assert "numpy==" in requirements
    assert "numpy>=1.26,<2.3" in pyproject["project"]["dependencies"]
    assert "scripts/check_linux_native_baseline.py" in script
    assert "Checking native CPU baselines" in script
    assert "src/row_bot" in manifest["payload_dirs"]
    for package in REQUIRED_RUNTIME_PACKAGES:
        assert Path("src/row_bot", package).is_dir()
    for category in ("root_python_files", "root_files", "runtime_script_files", "payload_dirs", "asset_dirs"):
        assert f"--category {category}" in script


def test_linux_native_baseline_check_blocks_x86_v2_metadata():
    blocked = check_linux_native_baseline._blocked_x86_baselines(["SSE", "SSE2", "X86_V2"])

    assert blocked == ["X86_V2"]


def test_linux_native_baseline_check_allows_legacy_x86_metadata():
    blocked = check_linux_native_baseline._blocked_x86_baselines(["SSE", "SSE2"])

    assert blocked == []


def test_linux_native_baseline_check_blocks_readelf_x86_v3_output():
    output = "Properties: x86 ISA needed: x86-64-baseline, x86-64-v3"

    blocked = check_linux_native_baseline._blocked_readelf_baselines(output)

    assert blocked == ["X86_V3"]


def test_linux_native_baseline_check_allows_readelf_baseline_output():
    output = "Properties: x86 ISA needed: x86-64-baseline"

    blocked = check_linux_native_baseline._blocked_readelf_baselines(output)

    assert blocked == []


def test_linux_root_build_wrapper_delegates_to_installer_script():
    script = Path("build_linux_app.sh").read_text(encoding="utf-8")

    assert "installer/build_linux_app.sh" in script
    assert 'exec "$SCRIPT_DIR/installer/build_linux_app.sh" "$@"' in script


def test_linux_launcher_resolves_installed_symlink_chain(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX symlink execution is covered by Linux CI")
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required to execute the generated launcher")

    home = tmp_path / "home"
    release_root = home / ".local" / "share" / "row-bot" / "releases" / "3.21.0"
    bin_home = home / ".local" / "bin"
    app_dir = release_root / "app"
    python_dir = release_root / "python" / "bin"
    (release_root / "bin").mkdir(parents=True)
    app_dir.mkdir(parents=True)
    python_dir.mkdir(parents=True)
    bin_home.mkdir(parents=True)

    launcher = release_root / "bin" / "row-bot"
    launcher.write_text(_linux_launcher_template(), encoding="utf-8")
    launcher.chmod(0o755)
    fake_python = python_dir / "python3"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'cwd=%s\\n' \"$PWD\"\n"
        "printf 'install_root=%s\\n' \"${ROW_BOT_INSTALL_ROOT:-}\"\n"
        "printf 'args=%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    (app_dir / "launcher.py").write_text("# fake launcher\n", encoding="utf-8")

    current = home / ".local" / "share" / "row-bot" / "current"
    current.symlink_to(Path("releases") / "3.21.0", target_is_directory=True)
    user_launcher = bin_home / "row-bot"
    user_launcher.symlink_to(current / "bin" / "row-bot")

    result = subprocess.run(
        [str(user_launcher), "--server", "--no-open"],
        env={**os.environ, "HOME": str(home), "ROW_BOT_DATA_DIR": str(home / ".row-bot")},
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    assert f"cwd={app_dir}" in result.stdout
    assert f"install_root={release_root}" in result.stdout
    assert "args=launcher.py --server --no-open" in result.stdout

    default_result = subprocess.run(
        [str(user_launcher)],
        env={**os.environ, "HOME": str(home), "ROW_BOT_DATA_DIR": str(home / ".row-bot")},
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    assert "args=launcher.py --browser --no-tray" in default_result.stdout


def test_linux_one_line_installer_declares_verified_release_contract():
    script = Path("installer/install-linux.sh").read_text(encoding="utf-8")
    legacy_repo = "siddsachar/" + "Th" + "oth"

    assert "siddsachar/row-bot" in script
    assert legacy_repo not in script
    assert "api.github.com/repos/${REPO}" in script
    assert "releases/latest" in script
    assert "releases/tags/v${REQUESTED_VERSION#v}" in script
    assert "Row-Bot-{re.escape(tag)}-Linux-{re.escape(arch)}" in script
    assert "Row-Bot-[0-9A-Za-z][0-9A-Za-z.-]*-Linux-" in script
    assert "row-bot-update-manifest" in script
    assert "sha256sum -c" in script
    assert "bash \"$PACKAGE_ROOT/install.sh\"" in script
    assert "ROW_BOT_SUPPRESS_INSTALL_PATH_HINT=1 bash \"$PACKAGE_ROOT/install.sh\"" in script
    assert "META_FILE" in script
    assert "x86_64" in script
    assert "aarch64" in script
    assert "LAUNCH_CMD=\"row-bot\"" in script
    assert "Run: ${LAUNCH_CMD}" in script
    assert "export PATH=\"$HOME/.local/bin:$PATH\"" in script


def test_thread_list_initializes_missing_thread_meta(monkeypatch, tmp_path):
    import sqlite3
    import row_bot.threads as threads

    db_path = tmp_path / "threads.db"
    monkeypatch.setattr(threads, "DB_PATH", str(db_path))

    assert threads._list_threads() == []

    conn = sqlite3.connect(db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(thread_meta)").fetchall()}
    finally:
        conn.close()

    assert {"thread_id", "name", "updated_at", "model_override", "project_id"} <= cols


def test_launcher_linux_default_is_direct_browser(monkeypatch):
    called = {}

    def fake_run_direct(args):
        called["browser"] = args.browser
        called["no_tray"] = args.no_tray

    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher, "_run_direct", fake_run_direct)

    launcher.main([])

    assert called == {"browser": True, "no_tray": False}


def test_release_workflows_reference_linux_artifact():
    from row_bot.version import __version__

    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    manifest = Path(".github/workflows/update-manifest.yml").read_text(encoding="utf-8")
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    installer_docs = Path("installer/README.md").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "build-linux" in release
    assert "installer/build_linux_app.sh" in release
    assert "bash -n build_linux_app.sh installer/build_linux_app.sh installer/install-linux.sh" in release
    assert "bash -n build_linux_app.sh installer/build_linux_app.sh installer/install-linux.sh" in ci
    assert "libxcb-cursor0" in ci
    assert "libportaudio2" in ci
    assert "installer/install-linux.sh" in release
    assert "installer/install-linux.sh" in ci
    assert "Row-Bot-Windows" in release
    assert "Row-Bot-*-Windows-*.exe" in release
    assert "RowBotSetup-Windows" not in release
    assert "Row-Bot-*-Linux-*.tar.gz" in release
    assert "libxcb-cursor0" in release
    assert "libportaudio2" in release
    assert "binutils" in release
    linux_smoke = release[release.index("Smoke Linux package"):]
    linux_smoke = linux_smoke[:linux_smoke.index("Upload Linux package")]
    assert "--no-root-check" not in linux_smoke
    assert linux_smoke.count("--public-probes") == 2
    assert "HOME=\"$RUNNER_TEMP/row-bot-linux-home\"" in linux_smoke
    assert "bash \"$PACKAGE_ROOT/install.sh\"" in linux_smoke
    assert '"$HOME/.local/bin/row-bot"\n' in linux_smoke
    assert "\"$HOME/.local/bin/row-bot\" --no-ollama serve --port 8091" in linux_smoke
    assert "Row-Bot-*-Linux-*.tar.gz" in manifest
    assert "Row-Bot-*-Windows-*.exe" in manifest
    assert "RowBotSetup_*.exe" not in manifest
    assert "curl -fsSL https://raw.githubusercontent.com/siddsachar/row-bot/main/installer/install-linux.sh | bash" in installer_docs
    assert "curl -fsSL https://raw.githubusercontent.com/siddsachar/row-bot/main/installer/install-linux.sh | bash" in readme
    assert "https://github.com/siddsachar/row-bot/releases/latest" in readme
    legacy_repo = "siddsachar/" + "Th" + "oth"
    assert f"https://github.com/{legacy_repo}/releases/latest" not in readme
    assert "published GitHub Release assets" in installer_docs
    assert f"bash installer/build_linux_app.sh {__version__}" in installer_docs
    assert f"bash build_linux_app.sh {__version__}" in installer_docs


def test_release_scripts_use_source_layout_version_file():
    from row_bot.version import __version__

    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    cut_release = Path("scripts/cut_release.py").read_text(encoding="utf-8")
    windows_builder = Path("installer/build_installer.ps1").read_text(encoding="utf-8")
    linux_builder = Path("installer/build_linux_app.sh").read_text(encoding="utf-8")
    mac_builder = Path("installer/build_mac_app.sh").read_text(encoding="utf-8")
    mac_zip_builder = Path("installer/build_mac_release.sh").read_text(encoding="utf-8")
    mac_launcher = Path("Start Row-Bot.command").read_text(encoding="utf-8")

    assert "src/row_bot/version.py" in release
    assert 'VERSION="$ROW_BOT_VERSION"' in release
    assert "from version import __version__" not in release
    assert '"src" / "row_bot" / "version.py"' in cut_release
    assert '"installer" / "install_deps.bat"' in cut_release
    assert '"Start Row-Bot.command"' in cut_release
    assert '"tests" / "test_brand_constants.py"' in cut_release
    assert 'Join-Path $ProjectRoot "src\\row_bot\\version.py"' in windows_builder
    for script in (linux_builder, mac_builder, mac_zip_builder):
        assert '$PROJECT_DIR/src/row_bot/version.py' in script
        assert '$PROJECT_DIR/version.py' not in script
    assert '$PROJECT_DIR/src/row_bot/version.py' in mac_launcher
    assert '$PROJECT_DIR/version.py' not in mac_launcher
    assert f'ROW_BOT_VERSION="{__version__}"' in mac_launcher


def test_release_manifest_script_uses_brand_contract():
    from row_bot.brand import APP_REPOSITORY, UPDATE_MANIFEST_MARKER, UPDATER_USER_AGENT
    from scripts import append_sha_manifest

    block = append_sha_manifest.build_manifest_block({"Row-Bot-4.0.0-Windows-x64.exe": "e" * 64})

    assert f"<!-- {UPDATE_MANIFEST_MARKER} -->" in block
    assert "Row-Bot-4.0.0-Windows-x64.exe: sha256=" + "e" * 64 in block
    assert append_sha_manifest.APP_REPOSITORY == APP_REPOSITORY
    assert append_sha_manifest.UPDATER_USER_AGENT == UPDATER_USER_AGENT

    help_result = subprocess.run(
        [sys.executable, "scripts/append_sha_manifest.py", "--help"],
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )
    assert "--repo" in help_result.stdout


def test_v4_is_newer_than_latest_v3_for_update_checks():
    assert updater.compare_versions("3.23.1", "4.0.0") > 0


def test_packagers_exclude_tests_directory():
    windows_installer = Path("installer/row_bot_setup.iss").read_text(encoding="utf-8")
    linux_builder = Path("installer/build_linux_app.sh").read_text(encoding="utf-8")
    mac_builder = Path("installer/build_mac_app.sh").read_text(encoding="utf-8")
    manifest = app_payload_manifest.build_manifest(Path("."))

    assert 'Source: "..\\tests' not in windows_installer
    assert 'DestDir: "{app}\\app\\tests' not in windows_installer
    assert "OutputBaseFilename=Row-Bot-{#MyAppVersion}-Windows-x64" in windows_installer
    assert " tests" not in linux_builder
    assert " tests" not in mac_builder
    assert not any(name.startswith("test_") for name in manifest["root_python_files"])
    assert not any(name.endswith("_test.py") for name in manifest["root_python_files"])
    assert not any(name.endswith("_harness.py") for name in manifest["root_python_files"])
    assert set(manifest["payload_dirs"]) == {"src/row_bot"}
    for package in REQUIRED_RUNTIME_PACKAGES:
        assert Path("src/row_bot", package).is_dir()
    assert {"static", "sounds", "bundled_skills", "tool_guides"} <= set(manifest["asset_dirs"])
    for builder in (linux_builder, mac_builder):
        assert "scripts/app_payload_manifest.py" in builder
        assert "--category payload_dirs" in builder
        assert "--category asset_dirs" in builder
    assert "--exclude='node_modules'" in linux_builder
    assert "--exclude='node_modules'" in mac_builder
    assert "Linux package payload contains test or harness artifacts" in linux_builder
    assert "macOS app payload contains test or harness artifacts" in mac_builder
