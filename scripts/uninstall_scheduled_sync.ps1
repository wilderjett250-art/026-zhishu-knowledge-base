param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = 'PKAS-Knowledge-Sync'
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$tasks = @(@($TaskName, ($TaskName + '-CatchUp')) | ForEach-Object {
    Get-ScheduledTask -TaskName $_ -ErrorAction SilentlyContinue
})
if ($tasks.Count -eq 0) {
    [pscustomobject]@{ status = 'not_found'; task_name = $TaskName } |
        ConvertTo-Json -Compress
    exit 0
}
foreach ($task in $tasks) {
    $workingDirectory = [string]$task.Actions[0].WorkingDirectory
    $runnerPath = Join-Path $resolvedRoot 'scripts\run_scheduled_sync.ps1'
    if (@($task.Actions).Count -ne 1 -or
        -not $workingDirectory.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
        ([string]$task.Actions[0].Arguments).IndexOf(
            '"' + $runnerPath + '"', [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Task name exists but belongs to another installation; refusing to remove it.'
    }
}
foreach ($task in $tasks) {
    if ([string]$task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $task.TaskName
    }
    Unregister-ScheduledTask -TaskName $task.TaskName -Confirm:$false
}
[pscustomobject]@{
    status = 'removed'
    task_name = $TaskName
    removed_tasks = @($tasks | ForEach-Object { $_.TaskName })
    data_preserved = $true
} | ConvertTo-Json -Compress
