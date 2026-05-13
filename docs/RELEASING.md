# Release Process

This is the end-to-end release checklist for Thoth.

## Versioning

Thoth uses semantic versioning:

- Patch: `3.17.1` for bug fixes
- Minor: `3.21.0` for new backwards-compatible features
- Major: `4.0.0` for breaking changes
- Beta/RC: `3.21.0-beta.1`, `3.21.0-rc.1`

## Before release

1. Make sure all feature/fix PRs are merged to `main`.
2. Run the full suite locally:

   ```bash
   python tests/test_suite.py
   ```

3. Cut a release-prep branch:

   ```bash
   git checkout main
   git pull --ff-only
   git checkout -b chore/release-vX.Y.Z
   ```

4. Bump versions with:

   ```bash
   python scripts/cut_release.py X.Y.Z
   ```

   This updates `version.py`, `installer/thoth_setup.iss`,
   `.github/workflows/release.yml`, the macOS app `Info.plist`, and the bug
   report version placeholder. The Linux package script derives its version
   from `version.py` or the workflow `THOTH_VERSION` argument.

5. Update `RELEASE_NOTES.md` with human-readable notes.
6. Confirm new shipped runtime files are covered by platform packaging:
   Windows `installer/thoth_setup.iss`, macOS `installer/build_mac_app.sh`,
   Linux `installer/build_linux_app.sh`, the Linux bootstrapper
   `installer/install-linux.sh`, and the installer payload notes in
   `installer/README.md`.
7. Smoke-test first-run behavior against a clean data directory before building
   artifacts, especially setup wizard imports, provider config defaults, and
   Custom/Self-hosted endpoint setup.
8. Run focused startup and packaging hardening tests:

   ```bash
   python -m pytest tests/test_startup_hardening.py tests/test_app_port.py tests/test_linux_support.py
   ```

9. Open and merge the release-prep PR.

## Build artifacts

1. Tag the release commit:

   ```bash
   git checkout main
   git pull --ff-only
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

2. Run GitHub Actions -> `Release - Build & Sign Installers` manually. This
   produces Windows, macOS, and Linux workflow artifacts; final release assets
   are uploaded manually after signing and smoke testing.
3. Download the Windows setup exe from the workflow artifact.
4. Sign it locally with the Certum certificate. Windows signing is intentionally
   not done in CI:

   ```powershell
   $signtool = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
   $exe = "dist\ThothSetup_X.Y.Z.exe"
   & $signtool sign /sha1 2341B4B36A21DF948E538A88BB194FAE4D1CAE51 /fd SHA256 /tr http://time.certum.pl /td SHA256 /d "Thoth" /du "https://github.com/siddsachar/Thoth" $exe
   & $signtool verify /pa /v $exe
   ```

5. Upload the signed exe to the draft GitHub Release.
6. Run notarization workflows for macOS when needed and upload the stapled DMG.
7. Download the Linux `Thoth-X.Y.Z-Linux-x86_64.tar.gz` artifact, extract it on
   a clean Linux VM, run `./install.sh`, and confirm `~/.local/bin/thoth` opens
   the browser UI and `~/.local/bin/thoth --server --no-open --port 8092`
   answers `/api/launcher-ping`.
8. Smoke-test the final Windows, macOS, and Linux artifacts. For Windows, include
   repair/upgrade over an existing install and confirm the bundled `python\`
   directory is replaced while `%USERPROFILE%\.thoth` is preserved. If a broken
   optional package such as TorchCodec was present in the old embedded runtime,
   confirm it is removed or the startup log contains a clear recovery hint.
9. Publish the GitHub Release.
10. Confirm `.github/workflows/update-manifest.yml` patches SHA256 hashes into
   the release body.
11. Test the packaged updater from the previous stable version on each platform.

## Linux Release Notes

Linux is shipped as a one-line installer backed by a self-contained XDG
user-install tarball, not as a root package. The supported baseline launches
Thoth in the system browser and avoids requiring pywebview, GTK/Qt,
AppIndicator, or tray backends. Native window and tray mode can still be tested
manually with `thoth --native` or `thoth --tray` on desktops with the required
libraries.

The user-facing install command is:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/Thoth/main/installer/install-linux.sh | bash
```

The bootstrapper resolves the latest GitHub Release, downloads the matching
`Thoth-X.Y.Z-Linux-ARCH.tar.gz`, verifies its SHA256 from the release manifest,
and then runs the tarball's bundled `install.sh`.

For unreleased Linux hotfix validation from a checkout, use the build script,
not the one-line bootstrapper. The bootstrapper always resolves published
GitHub Release assets. From the repository root:

```bash
bash installer/build_linux_app.sh 3.22.0
tar -xzf dist/Thoth-3.22.0-Linux-*.tar.gz
cd Thoth-3.22.0-Linux-*
./install.sh
~/.local/bin/thoth
```

The root-level `build_linux_app.sh` wrapper delegates to
`installer/build_linux_app.sh` so support snippets run from the checkout root do
not fail with a missing-script error.

If packaged Linux startup fails after printing `Thoth server started`, collect:

```bash
tail -200 ~/.thoth/thoth_app.log
tail -200 ~/.thoth/thoth_app.log.prev
uname -a
cat /etc/os-release
~/.local/bin/thoth --server --no-open --port 8092 --no-ollama
```

The launcher prints the selected port, child-process exit code when available,
and the tail of `~/.thoth/thoth_app.log` on readiness failure. For slow machines
or first-run package initialization, increase the wait with
`THOTH_STARTUP_TIMEOUT=180 ~/.local/bin/thoth`.

The tarball installs under `~/.local/share/thoth/releases/<version>`, updates
`~/.local/share/thoth/current`, creates `~/.local/bin/thoth`, and installs a
freedesktop `.desktop` file plus icon into user XDG locations. In-app updates
download the next Linux tarball, verify SHA256 through the release manifest,
install the new release under the same user-owned tree, flip the `current`
symlink, and restart through `~/.local/bin/thoth`.

Manual Linux smoke matrix before publishing:

- Ubuntu 22.04 or 24.04 GNOME Wayland
- Debian 12
- Fedora current
- Headless Ubuntu server mode

Minimum smoke checks:

- Fresh tarball install and desktop launcher
- Default installed command: `~/.local/bin/thoth`
- One-line installer after the GitHub Release is published
- `~/.local/bin/thoth --server --no-open --port 8092` plus `/api/launcher-ping`
- First-run setup with Providers and Custom/Self-hosted paths
- Ollama local model when `ollama` is installed and in `PATH`
- Browser tool after Playwright browser/dependency install
- Designer export and vault/open-folder actions
- Update from the previous Linux tarball to the new tarball

Camera/screenshot capture is optional on Linux. Missing OpenCV/MSS native
dependencies should disable those capture paths without preventing the app from
serving `/api/launcher-ping`.

## Post-release

- Post release notes and announcement.
- Open a tracking issue for the next patch/minor release.
- Label any follow-up bugs with the released version.
