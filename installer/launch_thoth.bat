@echo off
:: ============================================================================
:: Thoth Launcher – starts Ollama (if needed) and the Streamlit app
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

:: ── Launch Streamlit ────────────────────────────────────────────────────────
cd /d "%APP_DIR%"

:: Open browser after a short delay (Streamlit won't auto-open from a hidden window)
start "" cmd /c "timeout /t 4 /nobreak >NUL & start http://localhost:8501"

"%PYTHON%" -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
