# Build the Better Agent Windows desktop app:
#   frontend build -> PyInstaller (onedir) -> [Authenticode sign] -> installer
#
# UNVERIFIED ON WINDOWS YET. Structure mirrors build_macos.sh; the exact
# signtool / Inno Setup invocations need a real Windows host with a code-
# signing certificate and Inno Setup (ISCC) installed. See task A3.
#
# Run from a Developer PowerShell on Windows:
#   powershell -ExecutionPolicy Bypass -File desktop\build_windows.ps1
#
# Optional env:
#   BA_SIGN_THUMBPRINT  Authenticode cert thumbprint; skips signing if unset.
#   ISCC                Path to Inno Setup's ISCC.exe (default: on PATH).

$ErrorActionPreference = "Stop"

$Dir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo  = Split-Path -Parent $Dir
$Venv  = Join-Path $Repo "backend\.venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Pip    = Join-Path $Venv "Scripts\pip.exe"

function Assert-NativeCommandSucceeded([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

Write-Host "==> Building the frontend (npm run build)"
Push-Location (Join-Path $Repo "frontend")
npm run build
Assert-NativeCommandSucceeded "frontend build"
Pop-Location

Write-Host "==> Installing build dependencies into the backend venv"
& $Pip install -q pyinstaller -r (Join-Path $Dir "requirements.txt")
Assert-NativeCommandSucceeded "build dependency installation"

$Version = (& $Python -c "import sys; sys.path.insert(0, 'desktop'); from _version import __version__; print(__version__)").Trim()
Assert-NativeCommandSucceeded "version resolution"
$BcHome = (& $Python -c "import sys; sys.path.insert(0, 'backend'); from paths import ba_home; print(ba_home())").Trim()
Assert-NativeCommandSucceeded "state root resolution"
if ($env:BA_DESKTOP_UPDATE_URL) {
    $PrimaryUpdateUrl = $env:BA_DESKTOP_UPDATE_URL.TrimEnd("/")
} else {
    $PrimaryUpdateUrl = (& $Python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8', 80)); print(f'http://{s.getsockname()[0]}:8000/api/desktop/updates'); s.close()").Trim()
    Assert-NativeCommandSucceeded "update URL resolution"
}
$UpdateRoot = Join-Path $BcHome "desktop\updates"
$UpdateRepo = Join-Path $UpdateRoot "repository"
$UpdateKeys = Join-Path $UpdateRoot "keystore"
$Downloads = Join-Path $BcHome "desktop\downloads"

if (-not (Test-Path (Join-Path $UpdateRepo "metadata\root.json"))) {
    Write-Host "==> Initializing desktop update repository"
    & $Python (Join-Path $Dir "release.py") init $UpdateRepo $UpdateKeys
    Assert-NativeCommandSucceeded "update repository initialization"
}

Write-Host "==> Exporting updater trust root"
& $Python (Join-Path $Dir "release.py") export-root $UpdateRepo $UpdateKeys (Join-Path $Dir "tufup_root.json")
Assert-NativeCommandSucceeded "updater trust root export"
Set-Content -NoNewline -Path (Join-Path $Dir "update_url.txt") -Value $PrimaryUpdateUrl

Write-Host "==> Running PyInstaller"
Push-Location $Dir
& $Venv\Scripts\pyinstaller.exe --noconfirm BetterAgent.spec
Assert-NativeCommandSucceeded "PyInstaller build"
Pop-Location

# PyInstaller COLLECT name is "Better Agent" -> dist\Better Agent\
$AppDir = Join-Path $Dir "dist\Better Agent"
$Exe    = Join-Path $AppDir "Better Agent.exe"
if (-not (Test-Path $Exe)) {
    throw "build failed: '$Exe' was not produced"
}

Write-Host "==> Verifying the immutable frozen execution artifact"
$SmokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) (
    "better-agent-artifact-smoke-" + [guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $SmokeRoot | Out-Null
$SmokeSucceeded = $false
try {
    & (Join-Path $Dir "run_windows_artifact_smoke.ps1") `
        -Exe $Exe `
        -SmokeRoot $SmokeRoot `
        -Python $Python
    $SmokeSucceeded = $true
} finally {
    if ($SmokeSucceeded) {
        Remove-Item -Recurse -Force $SmokeRoot
    } else {
        Write-Host "Artifact smoke diagnostics retained at $SmokeRoot"
    }
}

if ($env:BA_SIGN_THUMBPRINT) {
    Write-Host "==> Authenticode-signing the executable"
    # Sign the launcher exe (and ideally every bundled .exe/.dll). Requires
    # the Windows SDK signtool on PATH and a timestamp server.
    signtool sign /sha1 $env:BA_SIGN_THUMBPRINT `
        /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $Exe
    Assert-NativeCommandSucceeded "Authenticode signing"
} else {
    Write-Host "==> Skipping signing (BA_SIGN_THUMBPRINT not set)"
}

Write-Host "==> Building the installer (Inno Setup)"
$Iscc = if ($env:ISCC) { $env:ISCC } else { "ISCC.exe" }
& $Iscc "/DAppVersion=$Version" (Join-Path $Dir "installer.iss")
Assert-NativeCommandSucceeded "installer build"

Write-Host "==> Publishing desktop update + primary-host download"
& $Python (Join-Path $Dir "release.py") publish $UpdateRepo $UpdateKeys $AppDir $Version
Assert-NativeCommandSucceeded "desktop update publication"
New-Item -ItemType Directory -Force -Path $Downloads | Out-Null
Copy-Item (Join-Path $Dir "dist\BetterAgentSetup.exe") (Join-Path $Downloads "BetterAgentSetup.exe") -Force

Write-Host "==> Done — see desktop\dist\ for the installer"
