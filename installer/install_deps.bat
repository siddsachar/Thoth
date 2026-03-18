@echo off
:: ============================================================================
:: Thoth v3.4.0 – Post-install dependency setup
:: Called by Inno Setup after file extraction.
::
:: This script:
::   1. Patches embedded Python to enable pip/site-packages
::   2. Installs pip via get-pip.py
::   3. Installs setuptools + wheel
::   4. Downloads and installs Ollama (silent)
::   5. Installs Python packages from requirements.txt
:: ============================================================================
title Thoth - Setting up dependencies...
set "INSTALL_DIR=%~1"
set "PYTHON_DIR=%INSTALL_DIR%\python"
set "PYTHON=%PYTHON_DIR%\python.exe"
set "APP_DIR=%INSTALL_DIR%\app"
set "LOG=%INSTALL_DIR%\install_log.txt"

:: Prevent embedded Python from importing packages from a system-wide Python
set "PYTHONNOUSERSITE=1"

echo ==========================================
echo  Thoth v3.4.0 - Installing dependencies
echo  This may take 5-25 minutes depending
echo  on your system and internet connection
echo  and whether Ollama needs to be installed.
echo  Please do not close this window.
echo ==========================================
echo.

echo ========================================= >> "%LOG%" 2>&1
echo  Thoth v3.4.0 - Install log              >> "%LOG%" 2>&1
echo  Install dir: %INSTALL_DIR%               >> "%LOG%" 2>&1
echo  Date: %DATE% %TIME%                      >> "%LOG%" 2>&1
echo ========================================= >> "%LOG%" 2>&1

:: ── 1. Patch embedded Python for pip support ────────────────────────────────
echo [1/5] Patching Python configuration...
echo Patching ._pth files... >> "%LOG%" 2>&1
for %%f in ("%PYTHON_DIR%\python*._pth") do (
    echo Patching %%f >> "%LOG%" 2>&1
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$f='%%~f'; (Get-Content $f) -replace '^#import site','import site' | Set-Content $f" >> "%LOG%" 2>&1
)

:: Add Lib\site-packages to the path file so imports resolve
for %%f in ("%PYTHON_DIR%\python*._pth") do (
    findstr /C:"Lib\site-packages" "%%~f" >NUL 2>&1
    if errorlevel 1 (
        echo Lib\site-packages>> "%%~f"
    )
)

:: Add app directory so local imports (channels, tools, etc.) resolve
for %%f in ("%PYTHON_DIR%\python*._pth") do (
    findstr /C:"..\app" "%%~f" >NUL 2>&1
    if errorlevel 1 (
        echo ..\app>> "%%~f"
    )
)

:: Add Lib so tkinter (bundled from full Python) is importable
for %%f in ("%PYTHON_DIR%\python*._pth") do (
    findstr /X /C:"Lib" "%%~f" >NUL 2>&1
    if errorlevel 1 (
        echo Lib>> "%%~f"
    )
)

:: ── 2. Install pip ──────────────────────────────────────────────────────────
echo [2/5] Installing pip...
echo Installing pip... >> "%LOG%" 2>&1
"%PYTHON%" "%INSTALL_DIR%\get-pip.py" --no-warn-script-location >> "%LOG%" 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install pip. >> "%LOG%" 2>&1
    echo ERROR: Failed to install pip. See install_log.txt for details.
    pause
    exit /b 1
)

:: Add Scripts dir to PATH so pip-installed commands are found
set "PATH=%PYTHON_DIR%\Scripts;%PYTHON_DIR%;%PATH%"

:: ── 3. Install build tools ──────────────────────────────────────────────────
echo [3/5] Installing build tools...
echo Installing setuptools and wheel... >> "%LOG%" 2>&1
"%PYTHON%" -m pip install --no-warn-script-location setuptools wheel >> "%LOG%" 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Failed to install setuptools/wheel. Continuing... >> "%LOG%" 2>&1
    echo WARNING: Build tools failed, continuing anyway...
)

:: ── 4. Download and install Ollama ──────────────────────────────────────────
echo [4/5] Downloading and installing Ollama...

:: Check if Ollama is already installed
where ollama >NUL 2>&1
if %ERRORLEVEL% EQU 0 (
    echo       Ollama already installed, skipping.
    echo Ollama already installed, skipping download. >> "%LOG%" 2>&1
    goto :skip_ollama
)

set "OLLAMA_EXE=%TEMP%\OllamaSetup.exe"
echo Downloading Ollama installer... >> "%LOG%" 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { Invoke-WebRequest -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile '%OLLAMA_EXE%' -UseBasicParsing; Write-Host 'OK' } catch { Write-Host 'FAIL'; exit 1 }" >> "%LOG%" 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Failed to download Ollama. >> "%LOG%" 2>&1
    echo WARNING: Could not download Ollama. You can install it manually from https://ollama.com
    goto :skip_ollama
)

echo Installing Ollama silently... >> "%LOG%" 2>&1
"%OLLAMA_EXE%" /VERYSILENT /NORESTART >> "%LOG%" 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: Ollama installation may have failed. >> "%LOG%" 2>&1
    echo WARNING: Ollama install returned an error. You may need to install it manually.
)

:: Kill the Ollama UI that auto-launches after install
taskkill /F /IM "ollama app.exe" >NUL 2>&1

:: Clean up installer
del "%OLLAMA_EXE%" >NUL 2>&1

:skip_ollama

:: ── 5. Install Python packages ──────────────────────────────────────────────
echo [5/5] Installing Python packages (this may take several minutes)...
echo Installing Python packages from requirements.txt... >> "%LOG%" 2>&1
"%PYTHON%" -m pip install --no-warn-script-location -r "%APP_DIR%\requirements.txt" >> "%LOG%" 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install some packages. >> "%LOG%" 2>&1
    echo ERROR: Package installation failed. See install_log.txt for details.
    pause
    exit /b 1
)

echo.
echo =========================================
echo  Setup complete!
echo =========================================
echo.

echo Setup complete at %DATE% %TIME% >> "%LOG%" 2>&1
