# Release Process

This is the end-to-end release checklist for Row-Bot.

## Versioning

Row-Bot uses semantic versioning:

- Patch: `3.17.1` for bug fixes
- Minor: `3.21.0` for new backwards-compatible features
- Major: `4.0.0` for breaking changes
- Beta/RC: `3.21.0-beta.1`, `3.21.0-rc.1`

## Before release

1. Make sure all feature/fix PRs are merged to `main`.
2. Run the full suite locally:

   ```bash
   python -m pip install "uv>=0.7,<1.0"
   uv lock --check
   python scripts/export_locked_requirements.py --check
   uv sync --locked --all-extras --group test
   uv run python scripts/verify_runtime_dependencies.py all
   uv run python scripts/run_test_matrix.py release
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

   This updates `src/row_bot/version.py`, `installer/row_bot_setup.iss`,
   `installer/install_deps.bat`, the `Start Row-Bot.command` fallback,
   `.github/workflows/release.yml`, the macOS app `Info.plist`, the bug report
   version placeholder, and the brand/user-agent contract expectations. The
   Linux package script derives its version from `src/row_bot/version.py` or
   the workflow `ROW_BOT_VERSION` argument.

5. Update `RELEASE_NOTES.md` with human-readable notes.
6. Confirm new shipped runtime files are covered by platform packaging:
   Windows `installer/row_bot_setup.iss`, macOS `installer/build_mac_app.sh`,
   Linux `installer/build_linux_app.sh`, the Linux bootstrapper
   `installer/install-linux.sh`, and the installer payload notes in
   `installer/README.md`. The current source-layout and payload contract is
   summarized in [`docs/SOURCE_LAYOUT.md`](SOURCE_LAYOUT.md).
   For server/deployment changes, also review `deploy/docker/Dockerfile`,
   `deploy/docker/compose.yaml`, the reverse-proxy and systemd examples under
   `deploy/`, `.dockerignore`, and `.github/workflows/container.yml`.
   For Computer Use releases, also confirm the pinned Cua manifest is packaged
   while the third-party executable remains an explicit post-install download.
7. Smoke-test first-run behavior against a clean data directory before building
   artifacts, especially setup wizard imports, provider config defaults, and
   Custom/Self-hosted endpoint setup. Exercise automatic Agent delegation and a
   durable document upload through completion, cancellation, and restart
   recovery. Start `row-bot serve` with an isolated data directory and confirm
   loopback is still gated until an explicit owner invitation is redeemed; do
   not copy one-time invitation URLs into logs or artifacts. Confirm Computer
   Use remains off by default and does not download or invoke Cua before its
   disclosure and an explicit Install or Repair action. Confirm Browser
   readiness is read-only at startup, a missing or mismatched managed Chromium
   stays unavailable until an explicit Browser Install or Repair action, and a
   supported installed Chrome or Edge channel remains selectable. Exercise
   filter-aware conversation selection and deletion with active work, a linked
   Designer project, and a Developer worktree or sandbox containing unimported
   changes; retained recovery paths must be reported. On native Windows or
   macOS, tear Buddy off into its desktop overlay, send and stop a turn, review
   a simple approval, reopen the full thread, and recover the overlay from the
   tray.
8. Run focused startup and packaging hardening tests:

   ```bash
   uv run python scripts/verify_runtime_dependencies.py all
   uv run python -m pytest tests/test_dependency_metadata.py tests/test_optional_dependency_imports.py tests/test_startup_hardening.py tests/test_app_port.py tests/test_linux_support.py
   ```

   For documentation changes, also run `npm audit --omit=dev` in `docs-site/`
   and review the current build-only advisory note in
   [`docs-site/README.md`](../docs-site/README.md). Do not force a dependency
   rewrite to hide an advisory with no compatible published fix.

9. For dependency, payload, or installer changes, run GitHub Actions ->
   `Installer Verify` manually on the release-prep branch. Windows, Linux, and
   macOS should all pass unless a skipped platform is documented.
10. Open and merge the release-prep PR.

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
   $exe = "dist\Row-Bot-X.Y.Z-Windows-x64.exe"
   & $signtool sign /sha1 2341B4B36A21DF948E538A88BB194FAE4D1CAE51 /fd SHA256 /tr http://time.certum.pl /td SHA256 /d "Row-Bot" /du "https://row-bot.ai" $exe
   & $signtool verify /pa /v $exe
   ```

5. Upload the signed exe to the draft GitHub Release.
6. Run notarization workflows for macOS when needed and upload the stapled DMG.
7. Download the Linux `Row-Bot-X.Y.Z-Linux-x86_64.tar.gz` artifact, extract it on
   a clean Linux VM, run `./install.sh`, and confirm `~/.local/bin/row-bot` opens
   the browser UI and `~/.local/bin/row-bot serve --port 8092`
   answers `/api/launcher-ping`.
8. Smoke-test the final Windows, macOS, and Linux artifacts. For Windows, include
   repair/upgrade over an existing install and confirm the bundled `python\`
   directory is replaced while Row-Bot user data is preserved. If a broken
   optional package such as TorchCodec was present in the old embedded runtime,
   confirm it is removed or the startup log contains a clear recovery hint.
   Also run the packaged launcher recovery commands against a disposable data
   directory: `--reset-tasks-db`, `--reset-db`, and `--restore-data`. Confirm
   they print the resolved data paths and that task DB reset backs up
   `tasks.db`, `tasks.db-wal`, and `tasks.db-shm`.
   On Windows and macOS, also exercise Computer Use setup, telemetry consent,
   pinned-runtime verification, one target-window action, Stop, Take over, and
   permission recovery. Confirm screenshots and typed content do not appear in
   logs. On Linux, confirm Computer Use reports unsupported without attempting
   a driver download. With a disposable xAI API account, refresh the live image
   catalog and run one supported image generation; confirm an endpoint-rejected
   optional quality value is retried once without that field while timeouts and
   other failures do not submit a duplicate generation.
9. Publish the GitHub Release.
10. Confirm `.github/workflows/container.yml` builds and smokes native
    `linux/amd64` and `linux/arm64` images, publishes the versioned multi-arch
    manifest to `ghcr.io/siddsachar/row-bot:X.Y.Z`, and updates `latest` for a
    stable release. Inspect the workflow summary for the release, architecture,
    and manifest digests; pull and smoke both platforms from a logged-out GHCR
    client to confirm the package is public.
11. Confirm `.github/workflows/update-manifest.yml` patches SHA256 hashes into
    the release body.
12. Test the packaged updater from the previous stable version on each platform.

## v4 Rebrand Upgrade Note

Row-Bot v4 uses Row-Bot release asset names and the Row-Bot SHA256 manifest
marker. Pre-v4 in-app updaters recognize only the old 3.x artifact and manifest
contract, so do not upload duplicate legacy-named v4 assets to bridge that gap.
For the v4 jump, direct existing users to download and run a Row-Bot v4
installer manually. Current Row-Bot releases no longer run the old automatic
Thoth-to-Row-Bot startup migration; users still on Thoth or an early Row-Bot
build should first install and launch a previous migration-capable Row-Bot
release, then upgrade to the current release. Future Row-Bot releases are
discoverable by the Row-Bot updater using the v4 asset contract.

## Linux Release Notes

Linux is shipped as a one-line installer backed by a self-contained XDG
user-install tarball, not as a root package. The supported baseline launches
Row-Bot in the system browser and avoids requiring pywebview, GTK/Qt,
AppIndicator, or tray backends. Native window and tray mode can still be tested
manually with `row-bot --native` or `row-bot --tray` on desktops with the required
libraries.

The user-facing install command is:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/row-bot/main/installer/install-linux.sh | bash
```

The bootstrapper resolves the latest GitHub Release, downloads the matching
`Row-Bot-X.Y.Z-Linux-ARCH.tar.gz`, verifies its SHA256 from the release manifest,
and then runs the tarball's bundled `install.sh`.

For unreleased Linux hotfix validation from a checkout, use the build script,
not the one-line bootstrapper. The bootstrapper always resolves published
GitHub Release assets. From the repository root:

```bash
bash installer/build_linux_app.sh X.Y.Z
tar -xzf dist/Row-Bot-X.Y.Z-Linux-*.tar.gz
cd Row-Bot-X.Y.Z-Linux-*
./install.sh
~/.local/bin/row-bot
```

The root-level `build_linux_app.sh` wrapper delegates to
`installer/build_linux_app.sh` so support snippets run from the checkout root do
not fail with a missing-script error.

If packaged Linux startup fails after printing `Row-Bot server started`, collect:

```bash
tail -200 ~/.row-bot/row_bot_app.log
tail -200 ~/.row-bot/row_bot_app.log.prev
uname -a
cat /etc/os-release
~/.local/bin/row-bot serve --port 8092 --no-ollama
```

The launcher prints the selected port, child-process exit code when available,
and the tail of `~/.row-bot/row_bot_app.log` on readiness failure. For slow machines
or first-run package initialization, increase the wait with
`ROW_BOT_STARTUP_TIMEOUT=180 ~/.local/bin/row-bot`.

The tarball installs under `~/.local/share/row-bot/releases/<version>`, updates
`~/.local/share/row-bot/current`, creates `~/.local/bin/row-bot`, and installs a
freedesktop `.desktop` file plus icon into user XDG locations. In-app updates
download the next Linux tarball, verify SHA256 through the release manifest,
install the new release under the same user-owned tree, flip the `current`
symlink, and restart through `~/.local/bin/row-bot`.

Manual Linux smoke matrix before publishing:

- Ubuntu 22.04 or 24.04 GNOME Wayland
- Debian 12
- Fedora current
- Headless Ubuntu server mode

Minimum smoke checks:

- Fresh tarball install and desktop launcher
- Default installed command: `~/.local/bin/row-bot`
- One-line installer after the GitHub Release is published
- `~/.local/bin/row-bot serve --port 8092` plus `/healthz`, `/readyz`, and
  `/api/launcher-ping`
- Owner invitation redemption, authenticated refresh, revocation, and a second
  browser being rejected while the first owner session is active
- Docker Compose startup with persistent `/data`, loopback-only publishing,
  container health, restart persistence, and a logged-out pull of the published
  versioned GHCR image on both supported architectures
- First-run setup with Providers and Custom/Self-hosted paths
- Ollama local model when `ollama` is installed and in `PATH`
- Browser tool with a supported installed browser channel or after the explicit
  managed Playwright browser/dependency install; startup alone must not download
  it
- Computer Use remains unavailable without attempting a Cua download
- Designer export and vault/open-folder actions
- Update from the previous Linux tarball to the new tarball

Camera/screenshot capture is optional on Linux. Missing OpenCV/MSS native
dependencies should disable those capture paths without preventing the app from
serving `/api/launcher-ping`.

## Post-release

- Post release notes and announcement.
- Open a tracking issue for the next patch/minor release.
- Label any follow-up bugs with the released version.
