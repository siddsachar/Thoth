import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import launcher
import pytest
import updater


def _linux_launcher_template() -> str:
    script = Path("installer/build_linux_app.sh").read_text(encoding="utf-8")
    start = script.index("cat > \"$PACKAGE_ROOT/bin/thoth\" <<'LAUNCHER'")
    launcher_start = script.index("#!/usr/bin/env bash", start)
    launcher_end = script.index("\nLAUNCHER", launcher_start)
    return script[launcher_start:launcher_end]


def test_linux_asset_selection(monkeypatch):
    monkeypatch.setattr(updater.platform, "system", lambda: "Linux")
    monkeypatch.setattr(updater.platform, "machine", lambda: "x86_64")
    body = (
        "Notes\n\n<!-- thoth-update-manifest -->\n"
        "```manifest\nschema: 1\nfiles:\n"
        "  Thoth-3.20.0-Linux-x86_64.tar.gz: sha256=" + "a" * 64 + "\n"
        "```\n"
    )
    release = {
        "tag_name": "v3.20.0",
        "prerelease": False,
        "published_at": "2026-05-04T12:00:00Z",
        "html_url": "https://github.com/siddsachar/Thoth/releases/tag/v3.20.0",
        "body": body,
        "assets": [{
            "name": "Thoth-3.20.0-Linux-x86_64.tar.gz",
            "size": 123,
            "browser_download_url": "https://github.com/siddsachar/Thoth/releases/download/v3.20.0/Thoth-3.20.0-Linux-x86_64.tar.gz",
        }],
    }

    info = updater._parse_release(release, "stable")

    assert info is not None
    assert info.asset_name == "Thoth-3.20.0-Linux-x86_64.tar.gz"
    assert info.sha256 == "a" * 64


def test_linux_install_marker_is_not_dev_install(monkeypatch, tmp_path):
    app_root = tmp_path / "current" / "app"
    app_root.mkdir(parents=True)
    marker = tmp_path / "current" / "install_info.json"
    marker.write_text(json.dumps({
        "platform": "linux",
        "install_kind": "xdg-user-tarball",
        "version": "3.20.0",
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

    package_root = tmp_path / "Thoth-3.20.0-Linux-x86_64"
    (package_root / "bin").mkdir(parents=True)
    (package_root / "app").mkdir()
    (package_root / "python" / "bin").mkdir(parents=True)
    (package_root / "share" / "applications").mkdir(parents=True)
    (package_root / "share" / "icons" / "hicolor" / "256x256" / "apps").mkdir(parents=True)
    (package_root / "bin" / "thoth").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (package_root / "python" / "bin" / "python3").write_text("", encoding="utf-8")
    (package_root / "share" / "applications" / "com.thoth.Thoth.desktop").write_text(
        "[Desktop Entry]\nExec=thoth\n", encoding="utf-8"
    )
    (package_root / "share" / "icons" / "hicolor" / "256x256" / "apps" / "thoth.png").write_bytes(b"png")
    (package_root / "install_info.json").write_text(json.dumps({
        "platform": "linux",
        "install_kind": "xdg-user-tarball",
        "version": "3.20.0",
    }), encoding="utf-8")
    archive = tmp_path / "Thoth-3.20.0-Linux-x86_64.tar.gz"
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

    assert launcher_path == home / ".local" / "bin" / "thoth"
    assert launcher_path.is_symlink()
    assert (xdg / "thoth" / "current").is_symlink()
    assert (xdg / "thoth" / "releases" / "3.20.0" / "install_info.json").exists()
    desktop_text = (xdg / "applications" / "com.thoth.Thoth.desktop").read_text(encoding="utf-8")
    assert f"Exec={launcher_path}" in desktop_text


def test_linux_build_script_declares_expected_package_contract():
    script = Path("installer/build_linux_app.sh").read_text(encoding="utf-8")

    assert "unknown-linux-gnu-install_only" in script
    assert 'PACKAGE_NAME="Thoth-${VERSION}-Linux-${PACKAGE_ARCH}"' in script
    assert 'TARBALL="$DIST_DIR/${PACKAGE_NAME}.tar.gz"' in script
    assert 'while [ -L "$SOURCE" ]; do' in script
    assert 'TARGET="$(readlink "$SOURCE")"' in script
    assert 'ROOT="$(cd -P "$(dirname "$SOURCE")/.." && pwd)"' in script
    assert "--browser --no-tray" in script
    assert "share/applications/com.thoth.Thoth.desktop" in script
    assert "install_kind\": \"xdg-user-tarball" in script
    for package in ("tools", "channels", "bundled_skills", "providers", "mcp_client", "migration"):
        assert package in script


def test_linux_launcher_resolves_installed_symlink_chain(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX symlink execution is covered by Linux CI")
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required to execute the generated launcher")

    home = tmp_path / "home"
    release_root = home / ".local" / "share" / "thoth" / "releases" / "3.20.0"
    bin_home = home / ".local" / "bin"
    app_dir = release_root / "app"
    python_dir = release_root / "python" / "bin"
    (release_root / "bin").mkdir(parents=True)
    app_dir.mkdir(parents=True)
    python_dir.mkdir(parents=True)
    bin_home.mkdir(parents=True)

    launcher = release_root / "bin" / "thoth"
    launcher.write_text(_linux_launcher_template(), encoding="utf-8")
    launcher.chmod(0o755)
    fake_python = python_dir / "python3"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'cwd=%s\\n' \"$PWD\"\n"
        "printf 'install_root=%s\\n' \"${THOTH_INSTALL_ROOT:-}\"\n"
        "printf 'args=%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    (app_dir / "launcher.py").write_text("# fake launcher\n", encoding="utf-8")

    current = home / ".local" / "share" / "thoth" / "current"
    current.symlink_to(Path("releases") / "3.20.0", target_is_directory=True)
    user_launcher = bin_home / "thoth"
    user_launcher.symlink_to(current / "bin" / "thoth")

    result = subprocess.run(
        [str(user_launcher), "--server", "--no-open"],
        env={**os.environ, "HOME": str(home), "THOTH_DATA_DIR": str(home / ".thoth")},
        text=True,
        capture_output=True,
        check=True,
        timeout=20,
    )

    assert f"cwd={app_dir}" in result.stdout
    assert f"install_root={release_root}" in result.stdout
    assert "args=launcher.py --server --no-open" in result.stdout


def test_linux_one_line_installer_declares_verified_release_contract():
    script = Path("installer/install-linux.sh").read_text(encoding="utf-8")

    assert "api.github.com/repos/${REPO}" in script
    assert "releases/latest" in script
    assert "releases/tags/v${REQUESTED_VERSION#v}" in script
    assert "Thoth-{re.escape(tag)}-Linux-{re.escape(arch)}" in script
    assert "thoth-update-manifest" in script
    assert "sha256sum -c" in script
    assert "bash \"$PACKAGE_ROOT/install.sh\"" in script
    assert "META_FILE" in script
    assert "x86_64" in script
    assert "aarch64" in script


def test_thread_list_initializes_missing_thread_meta(monkeypatch, tmp_path):
    import sqlite3
    import threads

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
    release = Path(".github/workflows/release.yml").read_text(encoding="utf-8")
    manifest = Path(".github/workflows/update-manifest.yml").read_text(encoding="utf-8")
    ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    installer_docs = Path("installer/README.md").read_text(encoding="utf-8")

    assert "build-linux" in release
    assert "installer/build_linux_app.sh" in release
    assert "installer/install-linux.sh" in release
    assert "installer/install-linux.sh" in ci
    assert "Thoth-*-Linux-*.tar.gz" in release
    linux_smoke = release[release.index("Smoke Linux package"):]
    assert "--no-root-check" not in linux_smoke
    assert "HOME=\"$RUNNER_TEMP/thoth-linux-home\"" in linux_smoke
    assert "bash \"$PACKAGE_ROOT/install.sh\"" in linux_smoke
    assert "\"$HOME/.local/bin/thoth\" --server --no-open --port 8091 --no-ollama" in linux_smoke
    assert "Thoth-*-Linux-*.tar.gz" in manifest
    assert "curl -fsSL https://raw.githubusercontent.com/siddsachar/Thoth/main/installer/install-linux.sh | bash" in installer_docs
