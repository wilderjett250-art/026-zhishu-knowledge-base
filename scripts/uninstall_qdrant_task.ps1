param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$TaskName = 'PKAS-Qdrant',
    [switch]$StopRuntime
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    $workingDirectory = [string]$task.Actions[0].WorkingDirectory
    if (-not $workingDirectory.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Task name exists but belongs to another installation; refusing to remove it.'
    }
    if ([string]$task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
if ($StopRuntime) {
    & (Join-Path $resolvedRoot 'scripts\stop_qdrant.ps1') `
        -ProjectRoot $resolvedRoot -DataRoot $DataRoot | Out-Null
}
[pscustomobject]@{
    status = $(if ($null -eq $task) { 'not_found' } else { 'removed' })
    task_name = $TaskName
    data_preserved = $true
} | ConvertTo-Json -Compress
