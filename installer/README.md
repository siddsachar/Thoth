# Building Row-Bot Installers

This guide explains how to build distributable Row-Bot installers and packages.

For version bumps, CI release workflow expectations, signing, tagging, and publish
order, use the canonical [release process](../docs/RELEASING.md). This file only
covers the installer payload and local build flow.

## Release Verification

Fast installer and release contracts run in the normal test matrix:

```bash
uv run python scripts/run_test_matrix.py installer-contracts
uv run python scripts/run_test_matrix.py release
```

Actual package builds and installed-app smoke checks are manual release work.
Use the GitHub `Release - Build & Sign Installers` workflow for release
candidates and `Installer Verify` when you want to smoke platform packages
without publishing. Those workflows build the Windows, Linux, and macOS
artifacts, launch the installed app with isolated data dirs, and upload
checksums/manifests for review.

Windows code signing remains local-only unless the project signing policy
changes. macOS notarization remains an explicit manual workflow after the
signed artifact has been reviewed.

## Linux Tarball

Linux users should normally install with the one-line bootstrapper:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/row-bot/main/installer/install-linux.sh | bash
```

The bootstrapper resolves the latest GitHub Release, downloads the matching
Linux tarball for the current architecture, verifies its SHA256 from the release
manifest, and then runs the tarball's bundled `install.sh`. For a pinned
version, pass it as an argument:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/row-bot/main/installer/install-linux.sh | bash -s -- 4.5.0
```

The bootstrapper installs published GitHub Release assets. It is not a way to
test an unreleased checkout from `main` or a hotfix branch before a release is
published.

Linux release tarballs are built with `installer/build_linux_app.sh`. The script mirrors
the macOS python-build-standalone approach, but emits a user-installable XDG
tarball instead of a native app bundle:

```bash
./installer/build_linux_app.sh
./installer/build_linux_app.sh 4.5.0
```

From a source checkout, this root-level wrapper is also supported for support
snippets and maintainer hotfixes:

```bash
bash build_linux_app.sh 4.5.0
```

To test an unreleased Linux fix locally, build the tarball from the checkout and
install the tarball it produced:

```bash
bash installer/build_linux_app.sh 4.5.0
tar -xzf dist/Row-Bot-4.5.0-Linux-*.tar.gz
cd Row-Bot-4.5.0-Linux-*
./install.sh
~/.local/bin/row-bot
```

Also smoke the server path explicitly. Start it in one terminal:

```bash
ROW_BOT_DATA_DIR="$(mktemp -d)" ~/.local/bin/row-bot serve --port 8092
```

Probe it from another terminal:

```bash
curl -fsS http://127.0.0.1:8092/healthz
curl -fsS http://127.0.0.1:8092/readyz
```

Use an isolated data directory for package smoke tests and remove it after the
process exits. A fresh server does not create an owner session automatically;
bootstrap a real acceptance browser only with an explicit
`row-bot access invite --profile computer --origin ...` command. Do not record
that command's one-time link in CI logs or package artifacts.

If the launcher starts a process but the app never becomes ready, inspect
`~/.row-bot/row-bot_app.log` and `~/.row-bot/row-bot_app.log.prev`. The launcher prints
the app log tail and targeted recovery hints for common native dependency
failures, and `ROW_BOT_STARTUP_TIMEOUT=180 ~/.local/bin/row-bot` can be used on
slow first-run systems.

The output is `dist/Row-Bot-X.Y.Z-Linux-x86_64.tar.gz` on x86_64 runners. It
contains bundled Python, installed Python packages, app source, `bin/row-bot`, an
`install.sh`, an `uninstall.sh`, a freedesktop `.desktop` file, icon files, and
`install_info.json` for updater/dev-install detection.

Manual tarball install flow:

```bash
tar -xzf Row-Bot-X.Y.Z-Linux-x86_64.tar.gz
cd Row-Bot-X.Y.Z-Linux-x86_64
./install.sh
row-bot
```

Linux installs to `~/.local/share/row-bot/releases/<version>`, updates
`~/.local/share/row-bot/current`, creates `~/.local/bin/row-bot`, and installs the
desktop entry/icon into user XDG locations. It launches in browser/no-tray mode
by default. Native window and system tray support remain optional because Linux
desktop environments require distro-specific GTK/Qt/AppIndicator dependencies.

Provider secrets use the system keyring when Linux Secret Service/KWallet is
available. Headless Linux and WSL installs without a keyring still start cleanly;
new secrets fall back to session-only storage rather than plaintext files.

Browser automation uses Playwright's normal Linux dependency flow. The tarball
does not install system packages; users should follow Playwright's printed
dependency command if Chromium reports missing libraries.

Camera and screenshot capture are optional. If OpenCV or MSS cannot import due
to missing Linux native libraries, Row-Bot should still start; those capture tools
report unavailable until the platform libraries are installed.

## Windows Installer

## Architecture

The installer bundles the embedded Python runtime, pre-installed Python packages, and app source code. Python packages are installed from `requirements.txt`, which is a generated locked export from `pyproject.toml` and `uv.lock`. Repair and upgrade installs replace the embedded Python directory before copying the new payload so manually installed or corrupted packages cannot linger inside Row-Bot's bundled runtime. Kokoro TTS model files are auto-downloaded on first use. Ollama and Playwright Chromium are handled by the build/runtime flow, and Ollama is optional because Row-Bot can run entirely with provider models. The optional Cua Driver used by Computer Use is not bundled: Row-Bot downloads the pinned platform asset only after the user reads its telemetry disclosure and explicitly chooses Install or Repair, verifies SHA-256, and keeps it in the private Row-Bot data directory.

| Bundled in .exe | Downloaded or created outside install |
|----------------|--------------------------------------|
| Python 3.13 embeddable runtime | Ollama installer is optional for local models |
| App source code, authenticated access/server package, Remote Access UI, Agent Profiles, Goal Mode, checkpointed Agent budgets and settings, child-agent runner, generation cancellation, Computer Use integration and pinned manifest, cache-only embedding fallback, mobile companion, channel streaming, tools, providers, plugins, MCP client, migration wizard, UI, Designer, Developer Studio, bundled skills/tool guides, static assets, and sounds | Kokoro TTS model + voices auto-download on first TTS use |
| Python packages from locked `requirements.txt` export | Playwright Chromium is bundled during build when available, otherwise installed on first browser use |
| Computer Use policy, private client, installer metadata, and platform checks | Pinned Cua Driver 0.7.1 is downloaded only after disclosure and explicit user consent |

## Prerequisites

1. **Inno Setup 6** â€” free installer compiler
   Download: https://jrsoftware.org/isdl.php
   Ensure `ISCC.exe` is installed (default: `C:\Program Files (x86)\Inno Setup 6\`)

2. **Internet connection** â€” the build script downloads Python embeddable and get-pip.py

3. **Icon file** â€” `row-bot.ico` in the project root
   If you don't have one, remove the `SetupIconFile` and `IconFilename` lines in `row_bot_setup.iss`.

## Build Steps

```powershell
# From the project root:
.\installer\build_installer.ps1
```

This will:
1. Download Python 3.13 embeddable package (~15 MB)
2. Download `get-pip.py` (~2.5 MB)
3. Compile everything into `dist\Row-Bot-4.5.0-Windows-x64.exe`

### Options

```powershell
# Use a different Python version:
.\installer\build_installer.ps1 -PythonVersion "3.12.8"

# Skip downloads if build/ already has the files:
.\installer\build_installer.ps1 -SkipDownloads
```

## What Gets Installed

On the end user's machine:

```
C:\Program Files\Row-Bot\            # Installation directory
â”œâ”€â”€ launch_row_bot.bat                # Main launcher (starts Ollama + Row-Bot)
â”œâ”€â”€ launch_row_bot.vbs                # Hidden-console wrapper (shortcuts point here)
â”œâ”€â”€ python\                         # Embedded Python runtime
â”‚   â”œâ”€â”€ python.exe
â”‚   â”œâ”€â”€ python313.dll
â”‚   â”œâ”€â”€ Lib\site-packages\          # All pip packages installed here
â”‚   â””â”€â”€ ...
â””â”€â”€ app\                            # Application source code
    â”œâ”€â”€ app.py                       # NiceGUI frontend
    â”œâ”€â”€ agent.py                    # ReAct agent
    â”œâ”€â”€ memory.py                   # Long-term memory DB + FAISS vector search
    â”œâ”€â”€ memory_extraction.py        # Background memory extraction from conversations
    â”œâ”€â”€ knowledge_graph.py          # Knowledge graph (triple store + NetworkX + FAISS)
    â”œâ”€â”€ wiki_vault.py               # Obsidian-compatible markdown vault export
    â”œâ”€â”€ dream_cycle.py              # Nightly knowledge refinement daemon
    â”œâ”€â”€ document_extraction.py      # Document knowledge extraction (map-reduce LLM pipeline)
    â”œâ”€â”€ models.py                   # Compatibility facade for local + provider model management
    â”œâ”€â”€ documents.py                # Document ingestion
    â”œâ”€â”€ threads.py                  # Thread/conversation persistence
    â”œâ”€â”€ api_keys.py                 # API key management
    â”œâ”€â”€ secret_store.py             # OS credential-store wrapper
    â”œâ”€â”€ voice.py                    # Speech-to-text (toggle-based, CPU Whisper)
    â”œâ”€â”€ tts.py                      # Text-to-speech (Kokoro TTS)
    â”œâ”€â”€ startup_diagnostics.py      # Optional native dependency startup probes
    â”œâ”€â”€ vision.py                   # Camera/screen capture
    â”œâ”€â”€ data_reader.py              # Pandas-based structured data reader
    â”œâ”€â”€ tasks.py                    # Task engine + APScheduler
    â”œâ”€â”€ prompts.py                  # Centralized LLM prompts
    â”œâ”€â”€ notifications.py             # Unified notification system
    â”œâ”€â”€ launcher.py                 # System tray + native window + splash screen
    â”œâ”€â”€ ui/                         # UI package (status bar, settings, sidebar, etc.)
    â”œâ”€â”€ sounds/                     # Notification sound effects
    â”‚   â”œâ”€â”€ workflow.wav
    â”‚   â””â”€â”€ timer.wav
    â”œâ”€â”€ channels/                   # Multi-channel messaging framework
    â”‚   â”œâ”€â”€ __init__.py
    â”‚   â”œâ”€â”€ base.py
    â”‚   â”œâ”€â”€ config.py
    â”‚   â”œâ”€â”€ media.py
    â”‚   â”œâ”€â”€ registry.py
    â”‚   â”œâ”€â”€ telegram.py
    â”‚   â””â”€â”€ tool_factory.py
    â”œâ”€â”€ requirements.txt
    â”œâ”€â”€ row-bot.ico
    â”œâ”€â”€ static/                     # Vendored JS libraries, fonts, and Designer runtime assets
    â”œâ”€â”€ tools/                      # Core tool modules, Developer tool, and Custom Tool builder
    â”‚   â”œâ”€â”€ __init__.py
    â”‚   â”œâ”€â”€ base.py
    â”‚   â”œâ”€â”€ registry.py
    â”‚   â”œâ”€â”€ web_search_tool.py
    â”‚   â”œâ”€â”€ ...
    â”‚   â””â”€â”€ youtube_tool.py
    â”œâ”€â”€ providers/                  # Provider config, auth metadata, catalog cache, runtime, Quick Choices
    â”œâ”€â”€ mobile/                     # Mobile pairing, access gate, PWA routes, auth storage, route discovery
    â”œâ”€â”€ mcp_client/                 # External MCP server client/runtime
    â”œâ”€â”€ migration/                  # Hermes/OpenClaw migration wizard backend
    â”œâ”€â”€ developer/                  # Developer Studio, Git helpers, Docker sandbox, Custom Tools
    â”œâ”€â”€ designer/                   # Designer Studio projects, editor, exports, and publishing
    â”œâ”€â”€ bundled_skills/             # Built-in manual skills
    â”œâ”€â”€ tool_guides/                # Auto-activation tool guides
    â””â”€â”€ plugins/                    # Plugin system & marketplace
        â”œâ”€â”€ __init__.py
        â”œâ”€â”€ api.py
        â”œâ”€â”€ installer.py
        â”œâ”€â”€ loader.py
        â”œâ”€â”€ manifest.py
        â”œâ”€â”€ marketplace.py
        â”œâ”€â”€ registry.py
        â”œâ”€â”€ sandbox.py
        â”œâ”€â”€ state.py
        â”œâ”€â”€ ui_marketplace.py
        â”œâ”€â”€ ui_plugin_dialog.py
        â””â”€â”€ ui_settings.py

%USERPROFILE%\.row-bot\               # User data directory (auto-created at runtime)
â”œâ”€â”€ threads.db                      # Conversation history & checkpoints
â”œâ”€â”€ memory.db                       # Long-term memories (knowledge graph entities & relations)
â”œâ”€â”€ memory_vectors/                 # FAISS index for semantic memory search
â”œâ”€â”€ memory_extraction_state.json    # Tracks last extraction run
â”œâ”€â”€ dream_journal.json              # Dream Cycle operation log
â”œâ”€â”€ api_keys.json                   # API key metadata only; raw keys use the OS credential store when available
â”œâ”€â”€ plugin_secrets.json             # Plugin API-key metadata only; raw keys use the OS credential store when available
â”œâ”€â”€ providers.json                  # Provider metadata, status, Quick Choices, and masked fingerprints
â”œâ”€â”€ model_catalog_cache.json         # Cached provider/Ollama model catalog rows
â”œâ”€â”€ embedding_config.json            # Selected local/cloud embedding provider
â”œâ”€â”€ cloud_config.json               # Legacy cloud model favorites/settings compatibility
â”œâ”€â”€ app_config.json                 # Onboarding / first-run state
â”œâ”€â”€ tools_config.json               # Tool enable/disable state
â”œâ”€â”€ model_settings.json             # Selected model & context size
â”œâ”€â”€ tts_settings.json               # Selected TTS voice
â”œâ”€â”€ vision_settings.json            # Vision model & camera selection
â”œâ”€â”€ voice_settings.json             # Whisper model size preference
â”œâ”€â”€ processed_files.json            # Tracked indexed documents
â”œâ”€â”€ tasks.db                        # Task definitions, schedules, run history & delivery config
â”œâ”€â”€ channels_config.json            # Channel settings
â”œâ”€â”€ mobile.db                       # Hashed mobile pairing/device credentials, scopes, revocation, access events
â”œâ”€â”€ channel_secrets.json             # Channel credential metadata only; raw secrets use OS keyring when available
â”œâ”€â”€ plugin_state.json               # Installed plugin state & settings
â”œâ”€â”€ shell_history.json              # Shell command history per thread
â”œâ”€â”€ skills_config.json              # Skill enable/disable state
â”œâ”€â”€ user_config.json                # Avatar emoji & ring color preferences
â”œâ”€â”€ row-bot_app.log                   # Application log
â”œâ”€â”€ developer/                       # Developer workspace links, Custom Tools, drafts, sandboxes
â”œâ”€â”€ vector_store/                   # FAISS index for uploaded documents
â”‚   â””â”€â”€ embedding_metadata.json      # Vector-index embedding provider metadata
â”œâ”€â”€ gmail/                          # Gmail OAuth tokens
â”œâ”€â”€ calendar/                       # Calendar OAuth tokens
â”œâ”€â”€ browser_profile/                # Playwright persistent browser profile
â”œâ”€â”€ wiki/                           # Obsidian-compatible markdown vault export
â””â”€â”€ kokoro/                         # Kokoro TTS model & voice data (auto-downloaded)
```

Ollama is installed system-wide via its official installer.

> **Note:** User data is stored outside `Program Files` in `~/.row-bot/` to avoid write-permission issues. Override the location by setting the `ROW_BOT_DATA_DIR` environment variable.

## Install Flow

The Inno Setup installer runs these steps:

1. **Extract files** â€” embedded Python, pre-installed packages, app source, assets, and launch scripts
2. **Create shortcuts** â€” Start Menu and optionally Desktop
3. **Optionally launch Row-Bot**

On repair/upgrade, Inno Setup deletes `{app}\python` before extraction. User data in `%USERPROFILE%\.row-bot` is not touched.

The app payload includes `pyproject.toml`, `uv.lock`, and generated `requirements.txt` so repair helpers and support diagnostics can identify the exact dependency set that produced the bundled runtime.

## End-User Experience

1. Run `Row-Bot-4.5.0-Windows-x64.exe`
2. Follow the wizard â€” the app payload is already bundled; optional model/runtime assets download only when a feature needs them
3. Launch Row-Bot from Start Menu or Desktop shortcut
4. The system tray icon appears; the app opens on the first available local port, normally `http://localhost:8080`
5. First launch shows a setup wizard â€” choose **Local** (download an Ollama model), **Providers** (enter an API key and pick a provider model), or **Custom/Self-hosted** (enter an OpenAI-compatible endpoint such as LM Studio, fetch models, and pick a default)

## Notes

- **CPU-only PyTorch**: `pyproject.toml` maps `torch` to the PyTorch CPU index and the generated `requirements.txt` preserves that installer policy. Users with NVIDIA GPUs can upgrade to CUDA torch after install.
- **Ollama is optional**: Row-Bot works with API-key provider models (OpenAI, Anthropic, Google AI, xAI, MiniMax, OpenRouter, Atlas Cloud, Requesty, and Ollama Cloud), ChatGPT / Codex subscription models after in-app ChatGPT sign-in, xAI Grok OAuth after in-app Grok sign-in, and Claude Subscription models after Row-Bot-owned Claude OAuth or setup-token import. Installed local Ollama chat models appear in Settings -> Models even when their family is newer than Row-Bot's curated capability lists; Vision stays conservative and requires known Vision metadata/families.
- **Agent orchestration**: the packaged app includes Agent Profiles, Goal Mode, child-agent delegation, profile/tool allowlists, profile-first workflow agents, Agent-run workflow promotion, checkpointed work-round budgets, repeat-stall protection, and application-wide nesting, concurrency, and timeout settings. These records live in Row-Bot's local data directory alongside workflow state.
- **Computer Use beta**: Windows and macOS packages include Row-Bot's provider-neutral native-app tool, target-window safety policy, private Cua client, and pinned runtime manifest, but not the third-party executable. Computer Use is off by default, interactive-desktop only, and requires a separate telemetry acknowledgement and verified Cua Driver install. Linux, server, schedule, channel, workflow, and child-agent callers cannot acquire it.
- **Local embeddings**: packaged local embedding paths are cache-only during normal startup. Missing or corrupted models stay repairable through explicit download actions, while memory and graph search fall back quickly instead of triggering a surprise model download.
- **Remote Access and server mode**: recursive `src/row_bot` packaging includes
  the versioned access store, migration bridge for the existing `mobile.db`,
  request/origin/proxy policy, owner and companion capabilities, invitation
  routes, Remote Access settings, `row-bot serve`, and
  `row-bot access invite/list/revoke/revoke-all/doctor`. Desktop mode remains
  local-first. Server mode requires an authenticated session even over
  loopback and runs with one worker, no tray/splash/browser, and no automatic
  Ollama startup by default.
- **Mobile companion**: the phone shell, PWA routes, workflow/activity
  surfaces, and phone-safe settings are a restricted profile of the shared
  access system. Existing companion records migrate in place; packaging must
  not introduce a second mobile-only credential or policy store.
- **Tailscale boundary**: Tailscale remains an optional host dependency.
  Packaged Row-Bot does not bundle or install Tailscale, sign the user in,
  enable Funnel, reset Serve, alter firewall rules, or overwrite an unrelated
  Serve route. Reachability still requires a Row-Bot invitation and revocable
  session. After Row-Bot verifies a route it created, the launcher restarts the
  child so startup can trust only the local loopback proxy for that exact app
  port and allow only the verified owned `.ts.net` origin. A missing launcher
  produces an explicit manual-restart instruction.
- **Deployment artifacts**: source checkouts include hardened Docker/Compose,
  Caddy, and systemd examples under `deploy/`. The Docker image contains no
  invitations, sessions, or provider credentials and publishes only on host
  loopback by default. The normal image includes all canonical server extras,
  matching headless Chromium, native media libraries, `uv`/`uvx`, and pinned
  Node.js tooling; voice and embedding models remain explicit persistent
  downloads. Browser-local voice uses the remote browser's microphone and
  speaker and requires HTTPS away from localhost. Review
  [`../deploy/docker/README.md`](../deploy/docker/README.md) before building or
  exposing a server image.
- **Cancellation and channel streaming**: provider cancellation transports, subprocess cancellation, shared channel streaming/finalization, interactive approvals, and durable channel notification modules are part of the recursive runtime payload.
- **Developer Studio**: the packaged app includes the Developer workspace UI, repo-scoped tools, Git helpers, durable worktree allocation, optional Docker shadow sandbox, and Custom Tool builder. Docker and GitHub CLI are optional external tools; when missing, the UI reports clear setup guidance instead of blocking normal chat.
- **Plugin System v2**: the packaged app includes Plugin Center, marketplace install/update flows, manifest validation, native tools, plugin-packaged MCP tools, bundled plugin skills, plugin-owned channels, and plugin templates. Plugins install disabled by default and must pass review/configuration before contributing runtime tools or channels.
- **Model picker behavior**: Settings -> Models pickers show pinned catalog Quick Choices plus the current default. Pin Brain or Vision catalog rows before expecting them in the everyday pickers; ChatGPT / Codex, Claude Subscription, xAI Grok OAuth, and Atlas Cloud Vision pins keep their provider-specific image-input capability metadata during refresh. Atlas Cloud image-generation and video-generation catalog rows are intentionally not exposed as chat, agent, or Vision models in this phase, and Grok Imagine rows stay scoped to Image and Video surfaces.
- **Custom/self-hosted endpoints**: first-run setup can connect to OpenAI-compatible endpoints such as LM Studio, vLLM, LocalAI, or private gateways. LM Studio's local server commonly uses `http://127.0.0.1:1234/v1`; load the selected model with a larger context window, such as `32768`, so Row-Bot's agent prompt and enabled tools fit.
- **Codex credential boundary**: external Codex CLI auth files are metadata/reference only. Direct ChatGPT / Codex runtime in the packaged app requires the in-app ChatGPT sign-in and stores Row-Bot-owned tokens in the OS credential store.
- **Optional native package recovery**: built-in TTS uses Kokoro ONNX and does not require TorchCodec. If a user-approved shell command installs a broken optional native package into the embedded Python runtime, startup diagnostics and the launcher log emit recovery hints, and repair/upgrade replaces the embedded runtime.
- **Task DB recovery**: `launcher.py --reset-tasks-db` backs up `tasks.db`, `tasks.db-wal`, and `tasks.db-shm` under the resolved Row-Bot data directory, recreates a clean task schema, and prints the exact paths. `launcher.py --reset-db` backs up known local SQLite stores (`tasks.db`, `memory.db`, `threads.db` families). `launcher.py --restore-data [backup-dir]` restores known SQLite files from a recovery backup or from the latest backup when no directory is supplied.
- **Launcher**: Uses `launcher.py` (system tray icon + native window + splash screen) instead of running NiceGUI directly. The tray icon shows app status (running/stopped) and provides graceful shutdown.
- **Uninstall**: Registered with Windows Add/Remove Programs. The uninstaller removes the installation directory but does **not** delete user data in `~/.row-bot/` â€” users can remove it manually if desired.
