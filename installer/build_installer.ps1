# =============================================================================
# build_installer.ps1
# Downloads embedded Python + get-pip.py, bundles tkinter, pre-installs all
# pip packages, then compiles a self-contained Inno Setup installer.
#
# The resulting .exe contains everything needed — no internet downloads at
# install time.  Ollama and Playwright Chromium are handled at runtime.
#
# TTS: Kokoro TTS is a pip package — no binary to bundle.  The model
#      downloads automatically on first use (~170 MB).
#
# Usage:  .\installer\build_installer.ps1
# =============================================================================

param(
    [string]$PythonVersion = "3.13.2",
    [switch]$SkipDownloads
)

$ErrorActionPreference = "Stop"
$BuildDir = Join-Path $PSScriptRoot "build"
$ProjectRoot = Split-Path $PSScriptRoot
$VersionFile = Join-Path $ProjectRoot "version.py"
$ThothVersion = if (Test-Path $VersionFile) {
    $versionLine = Select-String -Path $VersionFile -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
    if ($versionLine -and $versionLine.Matches.Count -gt 0) { $versionLine.Matches[0].Groups[1].Value } else { "unknown" }
} else { "unknown" }

Write-Host "============================================" -ForegroundColor Cyan
Write-Host " Thoth v$ThothVersion Installer Builder"       -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ── Create build directory ───────────────────────────────────────────────────
if (!(Test-Path $BuildDir)) {
    New-Item -ItemType Directory -Path $BuildDir -Force | Out-Null
}

if (!$SkipDownloads) {
    # ── 1. Download Python Embeddable Package ────────────────────────────────
    $PythonZip = "python-$PythonVersion-embed-amd64.zip"
    $PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/$PythonZip"
    $PythonZipPath = Join-Path $BuildDir $PythonZip
    $PythonDir = Join-Path $BuildDir "python"

    if (!(Test-Path $PythonZipPath)) {
        Write-Host "[1/2] Downloading Python $PythonVersion embeddable package..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonZipPath -UseBasicParsing
        Write-Host "      Downloaded: $PythonZip" -ForegroundColor Green
    } else {
        Write-Host "[1/2] Python zip already exists, skipping download." -ForegroundColor DarkGray
    }

    # Extract Python
    if (Test-Path $PythonDir) {
        Remove-Item -Recurse -Force $PythonDir
    }
    Write-Host "      Extracting Python..." -ForegroundColor Yellow
    Expand-Archive -Path $PythonZipPath -DestinationPath $PythonDir -Force
    Write-Host "      Extracted to: $PythonDir" -ForegroundColor Green

    # ── 2. Download get-pip.py ───────────────────────────────────────────────
    $GetPipPath = Join-Path $BuildDir "get-pip.py"
    if (!(Test-Path $GetPipPath)) {
        Write-Host "[2/2] Downloading get-pip.py..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPipPath -UseBasicParsing
        Write-Host "      Downloaded: get-pip.py" -ForegroundColor Green
    } else {
        Write-Host "[2/2] get-pip.py already exists, skipping download." -ForegroundColor DarkGray
    }
} else {
    Write-Host "Skipping downloads (using existing build/ contents)." -ForegroundColor DarkGray
}

# ── Bundle tkinter into embedded Python (not included in embeddable zip) ─────
Write-Host ""
Write-Host "Bundling tkinter into embedded Python..." -ForegroundColor Yellow

# Locate the system Python that has tkinter
$SysPyRoot = & python -c "import sys; print(sys.base_prefix)" 2>$null
if (!$SysPyRoot -or !(Test-Path "$SysPyRoot\Lib\tkinter")) {
    Write-Host "WARNING: Could not find system Python with tkinter. Splash screen will be unavailable." -ForegroundColor DarkYellow
} else {
    $PythonDir = Join-Path $BuildDir "python"

    # Copy _tkinter.pyd and Tcl/Tk DLLs
    foreach ($dll in @("_tkinter.pyd", "tcl86t.dll", "tk86t.dll")) {
        $src = Join-Path "$SysPyRoot\DLLs" $dll
        if (Test-Path $src) {
            Copy-Item $src -Destination $PythonDir -Force
            Write-Host "      Copied $dll" -ForegroundColor Green
        } else {
            Write-Host "      WARNING: $dll not found at $src" -ForegroundColor DarkYellow
        }
    }

    # Copy tkinter Python package
    $TkPkgSrc = Join-Path "$SysPyRoot\Lib" "tkinter"
    $TkPkgDst = Join-Path $PythonDir "Lib\tkinter"
    if (Test-Path $TkPkgSrc) {
        if (Test-Path $TkPkgDst) { Remove-Item -Recurse -Force $TkPkgDst }
        Copy-Item $TkPkgSrc -Destination $TkPkgDst -Recurse -Force
        Write-Host "      Copied Lib\tkinter\" -ForegroundColor Green
    }

    # Copy Tcl/Tk runtime data (tcl8.6, tk8.6, tcl8)
    $TclDst = Join-Path $PythonDir "tcl"
    if (!(Test-Path $TclDst)) { New-Item -ItemType Directory -Path $TclDst -Force | Out-Null }
    foreach ($subdir in @("tcl8.6", "tk8.6", "tcl8")) {
        $src = Join-Path "$SysPyRoot\tcl" $subdir
        $dst = Join-Path $TclDst $subdir
        if (Test-Path $src) {
            if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
            Copy-Item $src -Destination $dst -Recurse -Force
            Write-Host "      Copied tcl\$subdir\" -ForegroundColor Green
        }
    }

    Write-Host "      tkinter bundling complete." -ForegroundColor Green
}

# ── Pre-install Python packages (self-contained installer) ──────────────────
Write-Host ""
Write-Host "Pre-installing Python packages into embedded Python..." -ForegroundColor Yellow

$PythonDir = Join-Path $BuildDir "python"
$PythonExe = Join-Path $PythonDir "python.exe"

# Prevent embedded Python from picking up system-wide packages
$env:PYTHONNOUSERSITE = "1"

# Patch ._pth files to enable pip and site-packages
Write-Host "      Patching ._pth files..." -ForegroundColor Yellow
Get-ChildItem "$PythonDir\python*._pth" | ForEach-Object {
    $content = Get-Content $_.FullName
    $content = $content -replace '^#import site', 'import site'

    $lines = @($content)
    if ($lines -notcontains 'Lib\site-packages') { $lines += 'Lib\site-packages' }
    if ($lines -notcontains '..\app')             { $lines += '..\app' }
    if ($lines -notcontains 'Lib')                { $lines += 'Lib' }

    $lines | Set-Content $_.FullName
    Write-Host "      Patched $($_.Name)" -ForegroundColor Green
}

# Install pip
Write-Host "      Installing pip..." -ForegroundColor Yellow
$GetPipPath = Join-Path $BuildDir "get-pip.py"
& $PythonExe $GetPipPath --no-warn-script-location 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install pip." -ForegroundColor Red
    exit 1
}

# Add Scripts to PATH so pip-installed commands are found
$env:PATH = (Join-Path $PythonDir "Scripts") + ";" + $env:PATH
Write-Host "      pip installed" -ForegroundColor Green

# Install build tools
Write-Host "      Installing setuptools and wheel..." -ForegroundColor Yellow
& $PythonExe -m pip install --no-warn-script-location setuptools wheel --quiet 2>&1 | Out-Null

# Install all packages from requirements.txt
$RequirementsFile = Join-Path (Split-Path $PSScriptRoot) "requirements.txt"
Write-Host "      Installing packages from requirements.txt..." -ForegroundColor Yellow
Write-Host "      (this may take several minutes)" -ForegroundColor Yellow
& $PythonExe -m pip install --no-warn-script-location -r $RequirementsFile 2>&1 | ForEach-Object {
    $line = $_.ToString()
    if ($line -match 'Successfully installed|Installing collected') {
        Write-Host "      $line" -ForegroundColor Green
    } elseif ($line -match 'ERROR:|error:|Could not|Failed') {
        Write-Host "      $line" -ForegroundColor Red
    }
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to install Python packages." -ForegroundColor Red
    exit 1
}
Write-Host "      All packages pre-installed" -ForegroundColor Green

# Install Playwright Chromium into the embedded Python's cache
Write-Host "      Installing Playwright Chromium browser..." -ForegroundColor Yellow
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PythonDir "playwright-browsers"
& $PythonExe -m playwright install chromium 2>&1 | ForEach-Object {
    if ($_ -match 'downloading|Chromium') {
        Write-Host "      $_" -ForegroundColor Green
    }
}
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: Playwright Chromium install failed. Browser tool will auto-install on first use." -ForegroundColor DarkYellow
} else {
    Write-Host "      Playwright Chromium installed" -ForegroundColor Green
}

# ── 3. Create dist directory ────────────────────────────────────────────────
$DistDir = Join-Path (Join-Path $PSScriptRoot "..") "dist"
if (!(Test-Path $DistDir)) {
    New-Item -ItemType Directory -Path $DistDir -Force | Out-Null
}

# ── 4. Compile with Inno Setup ──────────────────────────────────────────────
Write-Host ""
Write-Host "Compiling installer with Inno Setup..." -ForegroundColor Yellow

$IssFile = Join-Path $PSScriptRoot "thoth_setup.iss"

# Try to find ISCC.exe
[string[]]$IsccPaths = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    (Get-Command "iscc.exe" -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ -and (Test-Path $_) }

if ($IsccPaths.Count -eq 0) {
    Write-Host ""
    Write-Host "ERROR: Inno Setup (ISCC.exe) not found!" -ForegroundColor Red
    Write-Host "Download from: https://jrsoftware.org/isdl.php" -ForegroundColor Red
    Write-Host ""
    Write-Host "Build directory is ready at: $BuildDir" -ForegroundColor Yellow
    Write-Host "After installing Inno Setup, run:" -ForegroundColor Yellow
    Write-Host "  iscc `"$IssFile`"" -ForegroundColor White
    exit 1
}

$Iscc = $IsccPaths[0]
Write-Host "Using ISCC: $Iscc" -ForegroundColor DarkGray

& $Iscc $IssFile

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host " Installer built successfully!"               -ForegroundColor Green
    Write-Host " Output: dist\ThothSetup_$ThothVersion.exe"    -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "ERROR: Inno Setup compilation failed." -ForegroundColor Red
    exit 1
}
