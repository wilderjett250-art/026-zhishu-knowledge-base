param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$TaskPrefix = 'PKAS',
    [switch]$StopQdrant
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
$results = [System.Collections.Generic.List[object]]::new()

$operations = @(
    @{ script = 'uninstall_backup_task.ps1'; name = "$TaskPrefix-Daily-Backup" },
    @{ script = 'uninstall_scheduled_sync.ps1'; name = "$TaskPrefix-Knowledge-Sync" },
    @{ script = 'uninstall_dashboard.ps1'; name = "$TaskPrefix-Dashboard" },
    @{ script = 'uninstall_core_worker.ps1'; name = "$TaskPrefix-Core-Worker" },
    @{ script = 'uninstall_qdrant_task.ps1'; name = "$TaskPrefix-Qdrant" }
)
foreach ($operation in $operations) {
    $script = Join-Path $resolvedRoot ("scripts\" + $operation.script)
    if ($operation.script -eq 'uninstall_qdrant_task.ps1') {
        $raw = & $script -ProjectRoot $resolvedRoot -DataRoot $DataRoot `
            -TaskName $operation.name
    } else {
        $raw = & $script -ProjectRoot $resolvedRoot -TaskName $operation.name
    }
    $results.Add(($raw | ConvertFrom-Json))
}
$raw = & (Join-Path $resolvedRoot 'scripts\stop_pkas_runtime.ps1') `
    -ProjectRoot $resolvedRoot
$results.Add(($raw | ConvertFrom-Json))
if ($StopQdrant) {
    $raw = & (Join-Path $resolvedRoot 'scripts\stop_qdrant.ps1') `
        -ProjectRoot $resolvedRoot -DataRoot $DataRoot
    $results.Add(($raw | ConvertFrom-Json))
}

[pscustomobject]@{
    status = 'completed'
    project_root = $resolvedRoot
    data_root = [System.IO.Path]::GetFullPath($DataRoot)
    data_preserved = $true
    runtime_files_preserved = $true
    operations = $results
} | ConvertTo-Json -Depth 5 -Compress
