param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = 'PKAS-Knowledge-Sync'
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    [pscustomobject]@{ status = 'not_found'; task_name = $TaskName } |
        ConvertTo-Json -Compress
    exit 0
}
$workingDirectory = [string]$task.Actions[0].WorkingDirectory
if (-not $workingDirectory.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Task name exists but belongs to another installation; refusing to remove it.'
}
if ([string]$task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
[pscustomobject]@{
    status = 'removed'
    task_name = $TaskName
    data_preserved = $true
} | ConvertTo-Json -Compress
