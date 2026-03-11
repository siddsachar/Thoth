@echo off
:: ============================================================================
:: Thoth Launcher – starts Ollama (if needed) and the system tray app
:: ============================================================================
title Thoth - Starting...

set "APP_DIR=%~dp0app"
set "PYTHON_DIR=%~dp0python"
set "PYTHON=%PYTHON_DIR%\python.exe"
set "PATH=%PYTHON_DIR%\Scripts;%PYTHON_DIR%;%PATH%"

:: ── Disable user site-packages to avoid conflicts with system Python ────────
set "PYTHONNOUSERSITE=1"

:: ── Force UTF-8 for Python I/O so emoji in print() never crash on cp1252 ────
set "PYTHONIOENCODING=utf-8"

:: ── Point tkinter at its bundled Tcl/Tk runtime data ────────────────────────
set "TCL_LIBRARY=%PYTHON_DIR%\tcl\tcl8.6"
set "TK_LIBRARY=%PYTHON_DIR%\tcl\tk8.6"

:: ── Find Ollama ─────────────────────────────────────────────────────────────
set "OLLAMA_APP="
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" (
    set "OLLAMA_APP=%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
)

:: ── Ensure Ollama is running ────────────────────────────────────────────────
if not defined OLLAMA_APP (
    echo WARNING: Ollama not found. Install from https://ollama.com
    echo          Thoth requires Ollama to run language models.
    goto :launch_app
)

echo Checking Ollama service...
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I "ollama.exe" >NUL
if %ERRORLEVEL% NEQ 0 (
    echo Starting Ollama...
    echo NOTE: The Ollama window may appear briefly — you can safely close it.
    start "" "%OLLAMA_APP%"
    :: Give Ollama a few seconds to start up
    timeout /t 5 /nobreak >NUL
) else (
    echo Ollama is already running.
)

:: ── Launch Thoth via system tray launcher ───────────────────────────────────
:launch_app
cd /d "%APP_DIR%"
"%PYTHON%" launcher.py
