# Better Agent one-command installer entry point (Windows).
#
# Usage:
#   irm https://raw.githubusercontent.com/ofekron/better-agent/main/scripts/bootstrap.ps1 | iex
#   .\scripts\bootstrap.ps1 -Mode default -Provider claude -Yes
#   $env:BETTER_AGENT_FROM = "readme"; irm .../bootstrap.ps1 | iex
#
# Environment:
#   BETTER_AGENT_FROM  Optional acquisition channel, one of: readme, landing,
#     hn, ph, devto, yt, gh-trending, dmg, brew, unknown. Passed through
#     untouched; scripts/install_channel.py owns the allowlist and validates
#     it. Unset or unrecognized is recorded as "unknown". Only the channel
#     string is recorded — no host, user, or environment data.
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
} else {
    # New-Item -ItemType Directory (without -Force) is atomic: it throws if
    # the lock dir already exists. That makes it a correct mutex for two
    # installers racing on a fresh $installDir — without it, both can see
    # $installDir absent, both clone, and the loser's Move-Item lands INSIDE
    # the winner's already-published directory (Move-Item moves a source
    # INTO an existing destination directory rather than failing), leaving
    # an orphaned nested checkout.installing.<pid> nothing cleans up.
    $lockDir = "$installDir.lock"
    $parent = Split-Path -Parent $installDir
    New-Item -ItemType Directory -Force -Path $parent | Out-Null

    $holdsLock = $false
    try {
        New-Item -ItemType Directory -Path $lockDir -ErrorAction Stop | Out-Null
        $holdsLock = $true
    } catch {
        Write-Host "bootstrap: another installer is publishing $installDir — waiting..."
        $waited = 0
        while (Test-Path $lockDir) {
            if (Test-Path (Join-Path $installDir ".git")) {
                Write-Host "bootstrap: $installDir was published by the other installer"
                break
            }
            $waited++
            if ($waited -ge 120) {
                throw "timed out waiting for a concurrent installer to finish publishing $installDir"
            }
            Start-Sleep -Seconds 1
        }
    }

    if ($holdsLock) {
        try {
            if (Test-Path $installDir) {
                throw "install destination exists but is not a Git checkout: $installDir. Move it aside or set BETTER_AGENT_INSTALL_DIR, then retry. No files were changed."
            }

            $staging = "$installDir.installing.$PID"
            Write-Host "bootstrap: cloning $repoUrl to $installDir"
            git clone $repoUrl $staging
            if ($LASTEXITCODE -ne 0) {
                if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
                throw "clone failed; no partial installation was published"
            }
            Move-Item $staging $installDir
        } finally {
            Remove-Item -Recurse -Force $lockDir -ErrorAction SilentlyContinue
        }
    } elseif (-not (Test-Path (Join-Path $installDir ".git"))) {
        throw "the other installer publishing $installDir failed; re-run this installer"
    }
}

Write-Host "bootstrap: handing off to install-windows.ps1"
Set-Location $installDir

$forwardArgs = @{}
if ($Mode) { $forwardArgs["Mode"] = $Mode }
if ($Provider) { $forwardArgs["Provider"] = $Provider }
if ($Yes) { $forwardArgs["Yes"] = $true }

& (Join-Path $installDir "scripts\install-windows.ps1") @forwardArgs
exit $LASTEXITCODE
