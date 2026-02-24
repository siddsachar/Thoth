@echo off
:: ============================================================================
:: Post-install script – sets up embedded Python environment
:: Called by Inno Setup after file extraction
:: ============================================================================
title Thoth - Installing dependencies...
set "INSTALL_DIR=%~1"
set "PYTHON_DIR=%INSTALL_DIR%\python"
set "PYTHON=%PYTHON_DIR%\python.exe"
set "APP_DIR=%INSTALL_DIR%\app"
set "LOG=%INSTALL_DIR%\install_log.txt"

echo =========================================
echo  Thoth - Installing Python packages...
echo  This may take 5-10 minutes.
echo  Please do not close this window.
echo =========================================
echo.

echo ========================================= >> "%LOG%" 2>&1
echo  Thoth – Installing Python packages...   >> "%LOG%" 2>&1
echo  Install dir: %INSTALL_DIR%               >> "%LOG%" 2>&1
echo ========================================= >> "%LOG%" 2>&1

:: ── Enable pip in embedded Python ───────────────────────────────────────────
:: The embedded distribution ships with a ._pth file that restricts imports.
:: We need to uncomment "import site" so pip/setuptools work.
echo [1/4] Patching Python configuration...
echo Patching ._pth files... >> "%LOG%" 2>&1
for %%f in ("%PYTHON_DIR%\python*._pth") do (
    echo Patching %%f >> "%LOG%" 2>&1
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$f='%%~f'; (Get-Content $f) -replace '^#import site','import site' | Set-Content $f" >> "%LOG%" 2>&1
)

:: Also add Lib\site-packages to the path file so imports resolve
for %%f in ("%PYTHON_DIR%\python*._pth") do (
    echo Ensuring site-packages in path file... >> "%LOG%" 2>&1
    findstr /C:"Lib\site-packages" "%%~f" >NUL 2>&1
    if errorlevel 1 (
        echo Lib\site-packages>> "%%~f"
    )
)

:: ── Install pip ─────────────────────────────────────────────────────────────
echo [2/4] Installing pip...
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

:: ── Install setuptools and wheel (needed to build some packages) ────────────
echo [3/4] Installing build tools...
echo Installing setuptools and wheel... >> "%LOG%" 2>&1
"%PYTHON%" -m pip install --no-warn-script-location setuptools wheel >> "%LOG%" 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install setuptools/wheel. >> "%LOG%" 2>&1
    echo ERROR: Failed to install build tools. See install_log.txt for details.
    pause
    exit /b 1
)

:: ── Install requirements ────────────────────────────────────────────────────
echo [4/4] Installing Python packages (this may take several minutes)...
echo Installing Python packages... >> "%LOG%" 2>&1
"%PYTHON%" -m pip install --no-warn-script-location -r "%APP_DIR%\requirements.txt" >> "%LOG%" 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install some packages. >> "%LOG%" 2>&1
    echo ERROR: Package installation failed. See install_log.txt for details.
    pause
    exit /b 1
)

echo.
echo =========================================
echo  All packages installed successfully!
echo =========================================
exit /b 0
