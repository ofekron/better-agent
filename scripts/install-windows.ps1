param(
    [ValidateSet("desktop-ui-only", "mobile-desktop-ui-only", "default")][string]$Mode,
    [string]$Provider,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "install-windows.ps1 only supports Windows."
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "winget is required. Install App Installer from Microsoft Store, then re-run this script."
}

function Update-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$TestArgs = @("--version")
    )
    $commandReady = $false
    if (Get-Command $Command -ErrorAction SilentlyContinue) {
        & $Command @TestArgs *> $null
        $commandReady = $LASTEXITCODE -eq 0
    }
    if ($commandReady) {
        Write-Host "$Command already installed."
        return
    }
    Write-Host "Installing $Id..."
    winget install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements
    Update-ProcessPath
    & $Command @TestArgs *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "$Command was not found on PATH after installing $Id. Open a new PowerShell and re-run this script."
    }
}

Install-WingetPackage -Id "Git.Git" -Command "git"
Install-WingetPackage -Id "Python.Python.3.13" -Command "python"
Install-WingetPackage -Id "astral-sh.uv" -Command "uv"
Install-WingetPackage -Id "OpenJS.NodeJS.LTS" -Command "node"

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Update-ProcessPath
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    throw "npm was not found on PATH after installing Node.js. Open a new PowerShell and re-run this script."
}

$installerArgs = @("$PSScriptRoot\install.py")
if ($Mode) { $installerArgs += @("--mode", $Mode) }
if ($Provider) { $installerArgs += @("--provider", $Provider) }
if ($Yes) { $installerArgs += "--yes" }
& python @installerArgs
if ($LASTEXITCODE -ne 0) {
    throw "Better Agent installation configuration failed."
}

$Repo = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Repo "backend"
$ActiveEnv = (& python (Join-Path $Backend "dependency_plan.py") activate --uv (Get-Command uv).Source).Trim()
if ($LASTEXITCODE -ne 0 -or -not $ActiveEnv) {
    throw "Better Agent backend dependency activation failed."
}
$BinDir = Join-Path $env:LOCALAPPDATA "BetterAgent\bin"
New-Item -ItemType Directory -Force -Path $BinDir *> $null
$BagentPath = Join-Path $BinDir "bagent.cmd"
$BackendPython = Join-Path $ActiveEnv "Scripts\python.exe"
$CliPath = Join-Path $Backend "cli.py"
Set-Content -Path $BagentPath -Encoding Ascii -Value "@echo off`r`n`"$BackendPython`" `"$CliPath`" %*"
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$UserEntries = @($UserPath -split ";" | Where-Object { $_ })
if ($BinDir -notin $UserEntries) {
    [Environment]::SetEnvironmentVariable(
        "Path",
        (($UserEntries + $BinDir) -join ";"),
        "User"
    )
    Update-ProcessPath
}

git --version
python --version
uv --version
node --version
npm --version

Write-Host "Installation complete. Run run_windows.bat next."
