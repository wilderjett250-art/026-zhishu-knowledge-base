param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [int]$QdrantHttpPort = 6333,
    [int]$QdrantGrpcPort = 6334,
    [int]$DashboardPort = 8765,
    [string]$TaskPrefix = 'PKAS',
    [string]$WeFlowRoot = '',
    [string]$DailyAt = '00:00',
    [switch]$SkipWebBuild,
    [switch]$SkipQdrant,
    [switch]$StartServices,
    [switch]$RegisterTasks
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$qdrantUrl = "http://127.0.0.1:$QdrantHttpPort"
$steps = [System.Collections.Generic.List[object]]::new()

function Add-Step {
    param([string]$Name, [string]$Status)
    $steps.Add([pscustomobject]@{ name = $Name; status = $Status })
}

$preflightArgs = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    (Join-Path $resolvedRoot 'scripts\preflight_windows.ps1'),
    '-ProjectRoot', $resolvedRoot, '-PortList',
    "$QdrantHttpPort,$QdrantGrpcPort,$DashboardPort"
)
if ($SkipQdrant) {
    $preflightArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
        (Join-Path $resolvedRoot 'scripts\preflight_windows.ps1'),
        '-ProjectRoot', $resolvedRoot, '-PortList', [string]$DashboardPort
    )
}
& powershell.exe @preflightArgs | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Windows preflight failed. Run preflight_windows.ps1 directly for details.'
}
Add-Step -Name 'preflight' -Status 'passed'

$uv = (Get-Command uv -ErrorAction Stop).Source
Push-Location -LiteralPath $resolvedRoot
try {
    # A non-editable wheel avoids Python 3.11 .pth decoding failures when the
    # installation path contains Chinese characters. Copy mode is predictable
    # across drives and filesystems on customer PCs.
    & $uv sync --frozen --python 3.11 --no-editable --link-mode copy
    if ($LASTEXITCODE -ne 0) { throw 'uv sync failed.' }
} finally {
    Pop-Location
}
Add-Step -Name 'python_runtime' -Status 'installed'

if (-not $SkipWebBuild) {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if ($null -eq $npm) { $npm = Get-Command npm -ErrorAction Stop }
    Push-Location -LiteralPath (Join-Path $resolvedRoot 'web')
    try {
        & $npm.Source ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
        & $npm.Source run build
        if ($LASTEXITCODE -ne 0) { throw 'Web production build failed.' }
    } finally {
        Pop-Location
    }
    Add-Step -Name 'web_build' -Status 'built'
} else {
    Add-Step -Name 'web_build' -Status 'skipped'
}

if (-not $SkipQdrant) {
    & (Join-Path $resolvedRoot 'scripts\install_qdrant_windows.ps1') `
        -ProjectRoot $resolvedRoot | Out-Null
    Add-Step -Name 'qdrant_runtime' -Status 'installed'
} else {
    Add-Step -Name 'qdrant_runtime' -Status 'skipped'
}

New-Item -ItemType Directory -Path $resolvedDataRoot -Force | Out-Null
& (Join-Path $resolvedRoot 'scripts\verify_windows_replica.ps1') `
    -ProjectRoot $resolvedRoot -DataRoot $resolvedDataRoot -QdrantUrl $qdrantUrl | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Isolated replica verification failed.' }
Add-Step -Name 'isolated_fts_verification' -Status 'passed'

if ($StartServices -and -not $SkipQdrant) {
    & (Join-Path $resolvedRoot 'scripts\start_qdrant.ps1') `
        -ProjectRoot $resolvedRoot -DataRoot $resolvedDataRoot `
        -HttpPort $QdrantHttpPort -GrpcPort $QdrantGrpcPort | Out-Null
    Add-Step -Name 'qdrant_health' -Status 'passed'
}

if ($RegisterTasks) {
    & (Join-Path $resolvedRoot 'scripts\assert_windows_autostart_allowed.ps1') `
        -ProjectRoot $resolvedRoot -DataRoot $resolvedDataRoot
    if ($SkipQdrant) {
        throw 'Task registration requires the local Qdrant runtime.'
    }
    & (Join-Path $resolvedRoot 'scripts\install_qdrant_task.ps1') `
        -ProjectRoot $resolvedRoot -DataRoot $resolvedDataRoot `
        -HttpPort $QdrantHttpPort -GrpcPort $QdrantGrpcPort `
        -TaskName "$TaskPrefix-Qdrant" | Out-Null
    & (Join-Path $resolvedRoot 'scripts\install_core_worker.ps1') `
        -ProjectRoot $resolvedRoot -DataRoot $resolvedDataRoot -QdrantUrl $qdrantUrl `
        -TaskName "$TaskPrefix-Core-Worker" | Out-Null
    & (Join-Path $resolvedRoot 'scripts\install_dashboard.ps1') `
        -ProjectRoot $resolvedRoot -DataRoot $resolvedDataRoot -QdrantUrl $qdrantUrl `
        -Port $DashboardPort -TaskName "$TaskPrefix-Dashboard" | Out-Null
    & (Join-Path $resolvedRoot 'scripts\install_backup_task.ps1') `
        -ProjectRoot $resolvedRoot -DataRoot $resolvedDataRoot `
        -TaskName "$TaskPrefix-Daily-Backup" | Out-Null
    if (-not [string]::IsNullOrWhiteSpace($WeFlowRoot)) {
        & (Join-Path $resolvedRoot 'scripts\install_scheduled_sync.ps1') `
            -ProjectRoot $resolvedRoot -DataRoot $resolvedDataRoot -QdrantUrl $qdrantUrl `
            -WeFlowRoot $WeFlowRoot -TaskName "$TaskPrefix-Knowledge-Sync" -DailyAt $DailyAt | Out-Null
    }
    Add-Step -Name 'windows_tasks' -Status 'registered'
} else {
    Add-Step -Name 'windows_tasks' -Status 'not_requested'
}

$report = [pscustomobject]@{
    status = 'completed'
    protocol = 'windows-bootstrap-v1'
    project_root = $resolvedRoot
    data_root = $resolvedDataRoot
    qdrant_url = $qdrantUrl
    dashboard = "http://127.0.0.1:$DashboardPort/"
    secrets_copied = $false
    mcp_configuration_changed = $false
    steps = $steps
}
$reportPath = Join-Path $resolvedDataRoot 'runs\windows-bootstrap.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $reportPath) -Force | Out-Null
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $reportPath -Encoding UTF8
$report | ConvertTo-Json -Depth 5 -Compress
