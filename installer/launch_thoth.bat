@echo off
:: ============================================================================
:: Thoth Launcher – starts Ollama (if needed) and the system tray app
:: ============================================================================
title Thoth - Starting...

set "APP_DIR=%~dp0app"
set "PYTHON_DIR=%~dp0python"
set "PYTHON=%PYTHON_DIR%\python.exe"
set "PATH=%PYTHON_DIR%\Scripts;%PYTHON_DIR%;%PATH%"

:: ── Ensure Ollama is running ────────────────────────────────────────────────
echo Checking Ollama service...
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I "ollama.exe" >NUL
if %ERRORLEVEL% NEQ 0 (
    echo Starting Ollama...
    start "" "ollama" serve
    :: Give Ollama a few seconds to start up
    timeout /t 5 /nobreak >NUL
) else (
    echo Ollama is already running.
)

:: ── Launch Thoth via system tray launcher ───────────────────────────────────
cd /d "%APP_DIR%"
"%PYTHON%" launcher.py
