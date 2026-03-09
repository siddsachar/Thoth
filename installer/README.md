# Building the Thoth Windows Installer

This guide explains how to build a distributable Windows installer for Thoth v2.2.

## Architecture

The installer is **lightweight** (~20 MB) — it bundles only the embedded Python runtime and app source code. Ollama and all Python packages are downloaded at install time.

| Bundled in .exe | Downloaded at install time |
|----------------|---------------------------|
| Python 3.13 embeddable (~15 MB) | Ollama (~120 MB) |
| App source code + tools (~200 KB) | Python packages via pip (~2 GB) |
| get-pip.py (~2.5 MB) | |

## Prerequisites

1. **Inno Setup 6** — free installer compiler  
   Download: https://jrsoftware.org/isdl.php  
   Ensure `ISCC.exe` is installed (default: `C:\Program Files (x86)\Inno Setup 6\`)

2. **Internet connection** — the build script downloads Python embeddable + get-pip.py

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
3. Compile everything into `dist\ThothSetup_2.2.0.exe`

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
    ├── app.py                      # Streamlit frontend
    ├── agent.py                    # ReAct agent
    ├── memory.py                   # Long-term memory DB + FAISS vector search
    ├── memory_extraction.py        # Background memory extraction from conversations
    ├── models.py                   # Ollama model management
    ├── documents.py                # Document ingestion
    ├── threads.py                  # Thread/conversation persistence
    ├── api_keys.py                 # API key management
    ├── voice.py                    # Speech-to-text (toggle-based, CPU Whisper)
    ├── tts.py                      # Text-to-speech (Piper TTS)
    ├── vision.py                   # Camera/screen capture
    ├── workflows.py                # Workflow engine + scheduler
    ├── notifications.py             # Unified notification system
    ├── launcher.py                 # System tray launcher
    ├── sounds/                     # Notification sound effects
    │   ├── workflow.wav
    │   └── timer.wav
    ├── .streamlit/                 # Streamlit config
    │   └── config.toml
    ├── requirements.txt
    ├── thoth.ico
    ├── tools/                      # 19 tool modules
    │   ├── __init__.py
    │   ├── base.py
    │   ├── registry.py
    │   ├── web_search_tool.py
    │   ├── ...
    │   └── youtube_tool.py

%USERPROFILE%\.thoth\               # User data directory (auto-created at runtime)
├── threads.db                      # Conversation history & checkpoints
├── memory.db                       # Long-term memories
├── memory_vectors/                 # FAISS index for semantic memory search
├── memory_extraction_state.json    # Tracks last extraction run
├── api_keys.json                   # API keys
├── tools_config.json               # Tool enable/disable state
├── model_settings.json             # Selected model & context size
├── processed_files.json            # Tracked indexed documents
├── status.json                     # Voice state for system tray
├── workflows.db                    # Workflow definitions, schedules & run history
├── timers.sqlite                   # Timer jobs
├── gmail/                          # Gmail OAuth tokens
├── calendar/                       # Calendar OAuth tokens
└── piper/                          # Piper TTS engine & voices
```

Ollama is installed system-wide via its official installer.

> **Note:** User data is stored outside `Program Files` in `~/.thoth/` to avoid write-permission issues. Override the location by setting the `THOTH_DATA_DIR` environment variable.

## Install Flow

The Inno Setup installer runs these steps:

1. **Extract files** — embedded Python, app source, scripts
2. **Run `install_deps.bat`** which:
   - Patches the Python `._pth` file to enable pip and site-packages
   - Installs pip via `get-pip.py`
   - Installs setuptools + wheel
   - Downloads and silently installs Ollama (skipped if already installed)
   - Runs `pip install -r requirements.txt`
3. **Create shortcuts** — Start Menu and optionally Desktop
4. **Optionally launch Thoth**

## End-User Experience

1. Run `ThothSetup_2.2.0.exe`
2. Follow the wizard — dependencies download and install automatically (5-15 min)
3. Launch Thoth from Start Menu or Desktop shortcut
4. The system tray icon appears; the app opens at `http://localhost:8501`
5. First launch downloads the default brain model (`qwen3:14b`, ~9 GB one-time)

## Notes

- **CPU-only PyTorch**: `requirements.txt` uses CPU-only torch. Users with NVIDIA GPUs can upgrade to CUDA torch after install.
- **Ollama detection**: `install_deps.bat` checks if Ollama is already on PATH and skips the download if so.
- **Launcher**: Uses `launcher.py` (system tray icon) instead of running Streamlit directly. The tray icon shows voice state and provides graceful shutdown.
- **Uninstall**: Registered with Windows Add/Remove Programs. The uninstaller removes the installation directory but does **not** delete user data in `~/.thoth/` — users can remove it manually if desired.
