# =============================================================================
# build_installer.ps1
# Downloads embedded Python + get-pip.py + Piper TTS engine + default voice,
# then compiles the Inno Setup installer.
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
Write-Host " Thoth v3.0.0 Installer Builder"              -ForegroundColor Cyan
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
        Write-Host "[1/4] Downloading Python $PythonVersion embeddable package..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonZipPath -UseBasicParsing
        Write-Host "      Downloaded: $PythonZip" -ForegroundColor Green
    } else {
        Write-Host "[1/4] Python zip already exists, skipping download." -ForegroundColor DarkGray
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
        Write-Host "[2/4] Downloading get-pip.py..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $GetPipPath -UseBasicParsing
        Write-Host "      Downloaded: get-pip.py" -ForegroundColor Green
    } else {
        Write-Host "[2/4] get-pip.py already exists, skipping download." -ForegroundColor DarkGray
    }

    # ── 3. Download Piper TTS engine ─────────────────────────────────────────
    $PiperRelease = "2023.11.14-2"
    $PiperZipName = "piper_windows_amd64.zip"
    $PiperUrl = "https://github.com/rhasspy/piper/releases/download/$PiperRelease/$PiperZipName"
    $PiperZipPath = Join-Path $BuildDir $PiperZipName
    $PiperDir = Join-Path $BuildDir "piper"

    if (!(Test-Path $PiperZipPath)) {
        Write-Host "[3/4] Downloading Piper TTS engine ($PiperRelease)..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri $PiperUrl -OutFile $PiperZipPath -UseBasicParsing
        Write-Host "      Downloaded: $PiperZipName" -ForegroundColor Green
    } else {
        Write-Host "[3/4] Piper zip already exists, skipping download." -ForegroundColor DarkGray
    }

    # Extract Piper (creates build/piper/piper/ with piper.exe + libs)
    if (Test-Path $PiperDir) {
        Remove-Item -Recurse -Force $PiperDir
    }
    Write-Host "      Extracting Piper..." -ForegroundColor Yellow
    Expand-Archive -Path $PiperZipPath -DestinationPath $PiperDir -Force
    Write-Host "      Extracted to: $PiperDir" -ForegroundColor Green

    # ── 4. Download default voice (en_US-lessac-medium) ──────────────────────
    $VoicesDir = Join-Path $PiperDir "voices"
    if (!(Test-Path $VoicesDir)) {
        New-Item -ItemType Directory -Path $VoicesDir -Force | Out-Null
    }

    $VoiceId = "en_US-lessac-medium"
    $VoiceBase = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium"
    $OnnxPath = Join-Path $VoicesDir "$VoiceId.onnx"
    $JsonPath = Join-Path $VoicesDir "$VoiceId.onnx.json"

    if (!(Test-Path $OnnxPath)) {
        Write-Host "[4/4] Downloading default voice ($VoiceId)..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri "$VoiceBase/$VoiceId.onnx" -OutFile $OnnxPath -UseBasicParsing
        Write-Host "      Downloaded: $VoiceId.onnx" -ForegroundColor Green
    } else {
        Write-Host "[4/4] Default voice already exists, skipping download." -ForegroundColor DarkGray
    }
    if (!(Test-Path $JsonPath)) {
        Invoke-WebRequest -Uri "$VoiceBase/$VoiceId.onnx.json" -OutFile $JsonPath -UseBasicParsing
        Write-Host "      Downloaded: $VoiceId.onnx.json" -ForegroundColor Green
    }
} else {
    Write-Host "Skipping downloads (using existing build/ contents)." -ForegroundColor DarkGray
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
    Write-Host " Output: dist\ThothSetup_3.0.0.exe"           -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "ERROR: Inno Setup compilation failed." -ForegroundColor Red
    exit 1
}
