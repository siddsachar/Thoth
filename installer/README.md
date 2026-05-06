# Building Thoth Installers

This guide explains how to build distributable Thoth installers and packages.

For version bumps, CI release workflow expectations, signing, tagging, and publish
order, use the canonical [release process](../docs/RELEASING.md). This file only
covers the installer payload and local build flow.

## Linux Tarball

Linux users should normally install with the one-line bootstrapper:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/Thoth/main/installer/install-linux.sh | bash
```

The bootstrapper resolves the latest GitHub Release, downloads the matching
Linux tarball for the current architecture, verifies its SHA256 from the release
manifest, and then runs the tarball's bundled `install.sh`. For a pinned
version, pass it as an argument:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/Thoth/main/installer/install-linux.sh | bash -s -- 3.20.0
```

The bootstrapper installs published GitHub Release assets. It is not a way to
test an unreleased checkout from `main` or a hotfix branch before a release is
published.

Linux release tarballs are built with `installer/build_linux_app.sh`. The script mirrors
the macOS python-build-standalone approach, but emits a user-installable XDG
tarball instead of a native app bundle:

```bash
./installer/build_linux_app.sh
./installer/build_linux_app.sh 3.20.0
```

From a source checkout, this root-level wrapper is also supported for support
snippets and maintainer hotfixes:

```bash
bash build_linux_app.sh 3.20.0
```

To test an unreleased Linux fix locally, build the tarball from the checkout and
install the tarball it produced:

```bash
bash installer/build_linux_app.sh 3.20.0
tar -xzf dist/Thoth-3.20.0-Linux-*.tar.gz
cd Thoth-3.20.0-Linux-*
./install.sh
~/.local/bin/thoth
```

Also smoke the server path explicitly:

```bash
~/.local/bin/thoth --server --no-open --port 8092 --no-ollama
curl -fsS http://127.0.0.1:8092/api/launcher-ping
```

If the launcher starts a process but the app never becomes ready, inspect
`~/.thoth/thoth_app.log` and `~/.thoth/thoth_app.log.prev`. The launcher prints
the app log tail and targeted recovery hints for common native dependency
failures, and `THOTH_STARTUP_TIMEOUT=180 ~/.local/bin/thoth` can be used on
slow first-run systems.

The output is `dist/Thoth-X.Y.Z-Linux-x86_64.tar.gz` on x86_64 runners. It
contains bundled Python, installed Python packages, app source, `bin/thoth`, an
`install.sh`, an `uninstall.sh`, a freedesktop `.desktop` file, icon files, and
`install_info.json` for updater/dev-install detection.

Manual tarball install flow:

```bash
tar -xzf Thoth-X.Y.Z-Linux-x86_64.tar.gz
cd Thoth-X.Y.Z-Linux-x86_64
./install.sh
thoth
```

Linux installs to `~/.local/share/thoth/releases/<version>`, updates
`~/.local/share/thoth/current`, creates `~/.local/bin/thoth`, and installs the
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
to missing Linux native libraries, Thoth should still start; those capture tools
report unavailable until the platform libraries are installed.

## Windows Installer

## Architecture

The installer bundles the embedded Python runtime, pre-installed Python packages, and app source code. Repair and upgrade installs replace the embedded Python directory before copying the new payload so manually installed or corrupted packages cannot linger inside Thoth's bundled runtime. Kokoro TTS model files are auto-downloaded on first use. Ollama and Playwright Chromium are handled by the build/runtime flow, and Ollama is optional because Thoth can run entirely with provider models.

| Bundled in .exe | Downloaded or created outside install |
|----------------|--------------------------------------|
| Python 3.13 embeddable runtime | Ollama installer is optional for local models |
| App source code, tools, providers, plugins, MCP client, migration wizard, UI, Designer, static assets, and sounds | Kokoro TTS model + voices auto-download on first TTS use |
| Python packages from `requirements.txt` | Playwright Chromium is bundled during build when available, otherwise installed on first browser use |

## Prerequisites

1. **Inno Setup 6** — free installer compiler  
   Download: https://jrsoftware.org/isdl.php  
   Ensure `ISCC.exe` is installed (default: `C:\Program Files (x86)\Inno Setup 6\`)

2. **Internet connection** — the build script downloads Python embeddable and get-pip.py

3. **Icon file** — `thoth.ico` in the project root  
   If you don't have one, remove the `SetupIconFile` and `IconFilename` lines in `thoth_setup.iss`.

## Build Steps

```powershell
# From the project root:
.\installer\build_installer.ps1
```

This will:
1. Download Python 3.13 embeddable package (~15 MB)
2. Download `get-pip.py` (~2.5 MB)
3. Compile everything into `dist\ThothSetup_3.20.0.exe`

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
C:\Program Files\Thoth\            # Installation directory
├── launch_thoth.bat                # Main launcher (starts Ollama + Thoth)
├── launch_thoth.vbs                # Hidden-console wrapper (shortcuts point here)
├── python\                         # Embedded Python runtime
│   ├── python.exe
│   ├── python313.dll
│   ├── Lib\site-packages\          # All pip packages installed here
│   └── ...
└── app\                            # Application source code
    ├── app.py                       # NiceGUI frontend
    ├── agent.py                    # ReAct agent
    ├── memory.py                   # Long-term memory DB + FAISS vector search
    ├── memory_extraction.py        # Background memory extraction from conversations
    ├── knowledge_graph.py          # Knowledge graph (triple store + NetworkX + FAISS)
    ├── wiki_vault.py               # Obsidian-compatible markdown vault export
    ├── dream_cycle.py              # Nightly knowledge refinement daemon
    ├── document_extraction.py      # Document knowledge extraction (map-reduce LLM pipeline)
    ├── models.py                   # Compatibility facade for local + provider model management
    ├── documents.py                # Document ingestion
    ├── threads.py                  # Thread/conversation persistence
    ├── api_keys.py                 # API key management
    ├── secret_store.py             # OS credential-store wrapper
    ├── voice.py                    # Speech-to-text (toggle-based, CPU Whisper)
    ├── tts.py                      # Text-to-speech (Kokoro TTS)
    ├── startup_diagnostics.py      # Optional native dependency startup probes
    ├── vision.py                   # Camera/screen capture
    ├── data_reader.py              # Pandas-based structured data reader
    ├── tasks.py                    # Task engine + APScheduler
    ├── prompts.py                  # Centralized LLM prompts
    ├── notifications.py             # Unified notification system
    ├── launcher.py                 # System tray + native window + splash screen
    ├── ui/                         # UI package (status bar, settings, sidebar, etc.)
    ├── sounds/                     # Notification sound effects
    │   ├── workflow.wav
    │   └── timer.wav
    ├── channels/                   # Multi-channel messaging framework
    │   ├── __init__.py
    │   ├── base.py
    │   ├── config.py
    │   ├── media.py
    │   ├── registry.py
    │   ├── telegram.py
    │   └── tool_factory.py
    ├── requirements.txt
    ├── thoth.ico
    ├── static/                     # Vendored JS libraries, fonts, and Designer runtime assets
    ├── tools/                      # 30 core tool modules
    │   ├── __init__.py
    │   ├── base.py
    │   ├── registry.py
    │   ├── web_search_tool.py
    │   ├── ...
    │   └── youtube_tool.py
    ├── providers/                  # Provider config, auth metadata, catalog, runtime, Quick Choices
    ├── mcp_client/                 # External MCP server client/runtime
    ├── migration/                  # Hermes/OpenClaw migration wizard backend
    └── plugins/                    # Plugin system & marketplace
        ├── __init__.py
        ├── api.py
        ├── installer.py
        ├── loader.py
        ├── manifest.py
        ├── marketplace.py
        ├── registry.py
        ├── sandbox.py
        ├── state.py
        ├── ui_marketplace.py
        ├── ui_plugin_dialog.py
        └── ui_settings.py

%USERPROFILE%\.thoth\               # User data directory (auto-created at runtime)
├── threads.db                      # Conversation history & checkpoints
├── memory.db                       # Long-term memories (knowledge graph entities & relations)
├── memory_vectors/                 # FAISS index for semantic memory search
├── memory_extraction_state.json    # Tracks last extraction run
├── dream_journal.json              # Dream Cycle operation log
├── api_keys.json                   # API key metadata only; raw keys use the OS credential store when available
├── plugin_secrets.json             # Plugin API-key metadata only; raw keys use the OS credential store when available
├── providers.json                  # Provider metadata, status, Quick Choices, and masked fingerprints
├── cloud_config.json               # Legacy cloud model favorites/settings compatibility
├── app_config.json                 # Onboarding / first-run state
├── tools_config.json               # Tool enable/disable state
├── model_settings.json             # Selected model & context size
├── tts_settings.json               # Selected TTS voice
├── vision_settings.json            # Vision model & camera selection
├── voice_settings.json             # Whisper model size preference
├── processed_files.json            # Tracked indexed documents
├── tasks.db                        # Task definitions, schedules, run history & delivery config
├── channels_config.json            # Channel settings
├── plugin_state.json               # Installed plugin state & settings
├── shell_history.json              # Shell command history per thread
├── skills_config.json              # Skill enable/disable state
├── user_config.json                # Avatar emoji & ring color preferences
├── thoth_app.log                   # Application log
├── vector_store/                   # FAISS index for uploaded documents
├── gmail/                          # Gmail OAuth tokens
├── calendar/                       # Calendar OAuth tokens
├── browser_profile/                # Playwright persistent browser profile
├── wiki/                           # Obsidian-compatible markdown vault export
└── kokoro/                         # Kokoro TTS model & voice data (auto-downloaded)
```

Ollama is installed system-wide via its official installer.

> **Note:** User data is stored outside `Program Files` in `~/.thoth/` to avoid write-permission issues. Override the location by setting the `THOTH_DATA_DIR` environment variable.

## Install Flow

The Inno Setup installer runs these steps:

1. **Extract files** — embedded Python, pre-installed packages, app source, assets, and launch scripts
2. **Create shortcuts** — Start Menu and optionally Desktop
3. **Optionally launch Thoth**

On repair/upgrade, Inno Setup deletes `{app}\python` before extraction. User data in `%USERPROFILE%\.thoth` is not touched.

## End-User Experience

1. Run `ThothSetup_3.20.0.exe`
2. Follow the wizard — dependencies download and install automatically (5-15 min)
3. Launch Thoth from Start Menu or Desktop shortcut
4. The system tray icon appears; the app opens on the first available local port, normally `http://localhost:8080`
5. First launch shows a setup wizard — choose **Local** (download an Ollama model), **Providers** (enter an API key and pick a provider model), or **Custom/Self-hosted** (enter an OpenAI-compatible endpoint such as LM Studio, fetch models, and pick a default)

## Notes

- **CPU-only PyTorch**: `requirements.txt` uses CPU-only torch. Users with NVIDIA GPUs can upgrade to CUDA torch after install.
- **Ollama is optional**: `install_deps.bat` offers to install Ollama, but it can be skipped for provider-only setups. Thoth works with API-key provider models (OpenAI, Anthropic, Google AI, xAI, MiniMax, OpenRouter) and ChatGPT / Codex subscription models after in-app ChatGPT sign-in. Installed local Ollama chat models appear in Settings -> Models even when their family is newer than Thoth's curated capability lists; Vision stays conservative and requires known Vision metadata/families.
- **Model picker behavior**: Settings -> Models pickers show pinned catalog Quick Choices plus the current default. Pin Brain or Vision catalog rows before expecting them in the everyday pickers; ChatGPT / Codex Vision pins keep their provider-specific image-input capability metadata during refresh.
- **Custom/self-hosted endpoints**: first-run setup can connect to OpenAI-compatible endpoints such as LM Studio, vLLM, LocalAI, or private gateways. LM Studio's local server commonly uses `http://127.0.0.1:1234/v1`; load the selected model with a larger context window, such as `32768`, so Thoth's agent prompt and enabled tools fit.
- **Codex credential boundary**: external Codex CLI auth files are metadata/reference only. Direct ChatGPT / Codex runtime in the packaged app requires the in-app ChatGPT sign-in and stores Thoth-owned tokens in the OS credential store.
- **Optional native package recovery**: built-in TTS uses Kokoro ONNX and does not require TorchCodec. If a user-approved shell command installs a broken optional native package into the embedded Python runtime, startup diagnostics and the launcher log emit recovery hints, and repair/upgrade replaces the embedded runtime.
- **Launcher**: Uses `launcher.py` (system tray icon + native window + splash screen) instead of running NiceGUI directly. The tray icon shows app status (running/stopped) and provides graceful shutdown.
- **Uninstall**: Registered with Windows Add/Remove Programs. The uninstaller removes the installation directory but does **not** delete user data in `~/.thoth/` — users can remove it manually if desired.
