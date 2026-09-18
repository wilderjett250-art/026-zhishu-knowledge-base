param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$KeepDashboard
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$runRoot = Join-Path $resolvedRoot 'data\runs'
$statePath = Join-Path $runRoot 'desktop-host.json'
$qdrantPidPath = Join-Path $runRoot 'qdrant.pid.json'
$stopped = New-Object System.Collections.Generic.List[string]

function Stop-VerifiedProcess {
    param([int]$ProcessId, [string[]]$ExpectedFragments, [string]$Name)
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" `
        -ErrorAction SilentlyContinue
    if ($null -eq $candidate) { return }
    $matchesExpected = @($ExpectedFragments | Where-Object {
        $candidate.CommandLine -like "*$_*"
    }).Count -gt 0
    if ($candidate.CommandLine -notlike "*$resolvedRoot*" -or -not $matchesExpected) {
        throw "Refused to stop PID $ProcessId because it is outside the verified PKAS process boundary."
    }
    Stop-Process -Id $ProcessId -Force
    $stopped.Add($Name)
}

if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    foreach ($entry in @($state.managed)) {
        if ($KeepDashboard -and $entry.name -eq 'dashboard') { continue }
        $fragment = if ($entry.name -eq 'dashboard') { 'pkas.cli' } else { 'pkas.core_worker' }
        Stop-VerifiedProcess -ProcessId ([int]$entry.pid) -ExpectedFragments @($fragment) `
            -Name ([string]$entry.name)
    }
    Stop-VerifiedProcess -ProcessId ([int]$state.host_pid) `
        -ExpectedFragments @('pkas_desktop_host.ps1', 'launch_desktop_hidden.ps1') `
        -Name 'tray-host'
    Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
}

if (-not $KeepDashboard -and (Test-Path -LiteralPath $qdrantPidPath)) {
    $qdrantState = Get-Content -LiteralPath $qdrantPidPath -Raw | ConvertFrom-Json
    Stop-VerifiedProcess -ProcessId ([int]$qdrantState.pid) `
        -ExpectedFragments @('qdrant.exe') -Name 'qdrant'
}

[pscustomobject]@{
    status = 'stopped'
    stopped = @($stopped)
    state_removed = -not (Test-Path -LiteralPath $statePath)
} | ConvertTo-Json -Compress
