param(
    [switch]$Yes,
    [switch]$WithClaude,
    [switch]$WithCodex
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Confirm-Bootstrap {
    if ($Yes) {
        return
    }
    Write-Host "This installs with winget: Git, Python, uv, Node.js."
    if ($WithClaude) {
        Write-Host "It also installs Claude Code CLI globally with npm."
    }
    if ($WithCodex) {
        Write-Host "It also installs Codex CLI globally with npm."
    }
    $answer = Read-Host "Continue? [y/N]"
    if ($answer -notin @("y", "Y", "yes", "YES")) {
        Write-Host "Aborted."
        exit 1
    }
}

function Require-Windows {
    if ($env:OS -ne "Windows_NT") {
        throw "bootstrap-windows.ps1 only supports Windows."
    }
}

function Require-Winget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is required. Install App Installer from Microsoft Store, then re-run this script."
    }
}

function Update-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Command
    )

    if (Get-Command $Command -ErrorAction SilentlyContinue) {
        Write-Host "$Command already installed."
        return
    }

    Write-Host "Installing $Id..."
    winget install --id $Id --exact --silent --accept-package-agreements --accept-source-agreements
    Update-ProcessPath
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command was not found on PATH after installing $Id. Open a new PowerShell and re-run this script."
    }
}

Require-Windows
Require-Winget
Confirm-Bootstrap

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

if ($WithClaude) {
    npm install -g "@anthropic-ai/claude-code"
}
if ($WithCodex) {
    npm install -g "@openai/codex"
}

git --version
python --version
uv --version
node --version
npm --version

Write-Host "Base Windows prerequisites installed. Run ./run.sh from Git Bash next."
