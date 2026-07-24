# Better Agent one-command installer entry point (Windows).
#
# Usage:
#   irm https://raw.githubusercontent.com/ofekron/better-agent/main/scripts/bootstrap.ps1 | iex
#   .\scripts\bootstrap.ps1 -Mode default -Provider claude -Yes
#
# Behaviour:
#   - Clones to %USERPROFILE%\.better-agent\checkout if absent, git pulls
#     (fast-forward only) if present.
#   - Verifies git is on PATH.
#   - Hands off to scripts\install-windows.ps1 in that checkout, forwarding
#     -Mode / -Provider / -Yes unchanged. That script bootstraps winget
#     packages (git, python, uv, node), then runs scripts\install.py.
#     See INSTALL.md Part 1.1.

param(
    [ValidateSet("desktop-ui-only", "mobile-desktop-ui-only", "default")][string]$Mode,
    [string]$Provider,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "bootstrap.ps1 only supports Windows. On macOS, use scripts/bootstrap.sh instead."
}

$repoUrl = if ($env:BETTER_AGENT_REPO_URL) { $env:BETTER_AGENT_REPO_URL } else { "https://github.com/ofekron/better-agent.git" }
$installDir = if ($env:BETTER_AGENT_INSTALL_DIR) { $env:BETTER_AGENT_INSTALL_DIR } else { Join-Path $env:USERPROFILE ".better-agent\checkout" }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git not found on PATH. Run 'winget install --id Git.Git --exact', open a new PowerShell, then re-run this installer."
}

if (Test-Path (Join-Path $installDir ".git")) {
    Write-Host "bootstrap: updating existing checkout at $installDir"
    git -C $installDir pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "git pull --ff-only failed in $installDir" }
} elseif (Test-Path $installDir) {
    throw "install destination exists but is not a Git checkout: $installDir. Move it aside or set BETTER_AGENT_INSTALL_DIR, then retry. No files were changed."
} else {
    $parent = Split-Path -Parent $installDir
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $staging = "$installDir.installing.$PID"

    Write-Host "bootstrap: cloning $repoUrl to $installDir"
    git clone $repoUrl $staging
    if ($LASTEXITCODE -ne 0) {
        if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
        throw "clone failed; no partial installation was published"
    }

    if (Test-Path $installDir) {
        Remove-Item -Recurse -Force $staging
        throw "another installer published $installDir while cloning; leaving it untouched"
    }
    Move-Item $staging $installDir
}

Write-Host "bootstrap: handing off to install-windows.ps1"
Set-Location $installDir

$forwardArgs = @{}
if ($Mode) { $forwardArgs["Mode"] = $Mode }
if ($Provider) { $forwardArgs["Provider"] = $Provider }
if ($Yes) { $forwardArgs["Yes"] = $true }

& (Join-Path $installDir "scripts\install-windows.ps1") @forwardArgs
exit $LASTEXITCODE
