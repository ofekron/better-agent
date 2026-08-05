param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$SmokeRoot,
    [Parameter(Mandatory = $true)][string]$Python
)

$ErrorActionPreference = "Stop"

New-Item -ItemType Directory -Force -Path (Join-Path $SmokeRoot "state") | Out-Null
$Result = Join-Path $SmokeRoot "result.json"
$FailureLog = Join-Path $SmokeRoot "state\faulthandler.log"

function Show-SmokeDiagnostics {
    if (Test-Path $Result) {
        Get-Content $Result
    }
    if (Test-Path $FailureLog) {
        Get-Content $FailureLog
    }
}

$PreviousBetterAgentHome = $env:BETTER_AGENT_HOME
$Smoke = $null
try {
    $env:BETTER_AGENT_HOME = Join-Path $SmokeRoot "state"
    $ResultArgument = '"' + $Result + '"'
    $Smoke = Start-Process `
        -FilePath $Exe `
        -ArgumentList "--frozen-artifact-smoke", "--output", $ResultArgument `
        -PassThru
    $Deadline = [DateTime]::UtcNow.AddMinutes(5)
    $SeenProgress = @{}
    do {
        $Exited = $Smoke.WaitForExit(1000)
        $ProgressFiles = @(
            Get-ChildItem `
                -LiteralPath $SmokeRoot `
                -Filter "result.progress.*.json" |
                Sort-Object -Property Name
        )
        foreach ($ProgressFile in $ProgressFiles) {
            if ($SeenProgress.ContainsKey($ProgressFile.Name)) {
                continue
            }
            try {
                $ProgressLine = Get-Content `
                    -LiteralPath $ProgressFile.FullName `
                    -Raw
                $null = $ProgressLine | ConvertFrom-Json
            } catch {
                continue
            }
            Write-Host "artifact-smoke-progress $ProgressLine"
            $SeenProgress[$ProgressFile.Name] = $true
        }
        if (-not $Exited -and [DateTime]::UtcNow -ge $Deadline) {
            Write-Host "artifact smoke exceeded its five-minute safety bound"
            & taskkill.exe /PID $($Smoke.Id) /T /F
            if (-not $Smoke.WaitForExit(5000)) {
                Write-Host "artifact smoke process tree did not terminate"
            }
            Show-SmokeDiagnostics
            throw "frozen artifact smoke did not exit"
        }
    } while (-not $Exited)
    if ($Smoke.ExitCode -ne 0) {
        Show-SmokeDiagnostics
        throw "frozen artifact smoke failed"
    }
    & $Python -c "import json,sys; value=json.load(open(sys.argv[1], encoding='utf-8')); assert set(value['families']) == {'claude', 'agy'}; assert value['windows_wrapper'] == 'rejected'" $Result
    if (-not $?) {
        throw "frozen artifact smoke result is invalid"
    }
} finally {
    if ($null -ne $Smoke -and -not $Smoke.HasExited) {
        & taskkill.exe /PID $($Smoke.Id) /T /F
        if (-not $Smoke.WaitForExit(5000)) {
            Write-Host "artifact smoke process tree did not terminate"
        }
    }
    if ($null -eq $PreviousBetterAgentHome) {
        Remove-Item Env:BETTER_AGENT_HOME -ErrorAction SilentlyContinue
    } else {
        $env:BETTER_AGENT_HOME = $PreviousBetterAgentHome
    }
}
