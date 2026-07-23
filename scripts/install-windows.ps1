param(
    [ValidateSet("default", "ui-only")][string]$Mode,
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

git --version
python --version
uv --version
node --version
npm --version

Write-Host "Installation configured. Run ./run.sh from Git Bash next."
