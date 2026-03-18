# =============================================================================
# build_installer.ps1
# Downloads embedded Python + get-pip.py, bundles tkinter, then compiles
# the Inno Setup installer.
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

Write-Host "============================================" -ForegroundColor Cyan
Write-Host " Thoth v3.4.0 Installer Builder"              -ForegroundColor Cyan
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
    Write-Host " Output: dist\ThothSetup_3.4.0.exe"           -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "ERROR: Inno Setup compilation failed." -ForegroundColor Red
    exit 1
}
