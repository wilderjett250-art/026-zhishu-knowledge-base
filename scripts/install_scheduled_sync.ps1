param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$QdrantUrl = 'http://127.0.0.1:6333',
    [string]$WeFlowRoot = '',
    [string]$WeFlowExportRoot = '',
    [switch]$LocalOnly,
    [string]$TaskName = 'PKAS-Knowledge-Sync',
    [string]$DailyAt = '00:00'
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
& (Join-Path $resolvedRoot 'scripts\assert_windows_autostart_allowed.ps1') `
    -ProjectRoot $resolvedRoot -DataRoot $DataRoot
if ($LocalOnly -and -not [string]::IsNullOrWhiteSpace($WeFlowRoot)) {
    throw 'Choose either LocalOnly or an explicit WeFlowRoot, not both.'
}
if (-not $LocalOnly -and (
    [string]::IsNullOrWhiteSpace($WeFlowRoot) -or
    -not (Test-Path -LiteralPath $WeFlowRoot)
)) {
    throw 'WeFlowRoot must be an existing, explicitly supplied directory.'
}
$runnerPath = Join-Path $resolvedRoot 'scripts\run_scheduled_sync.ps1'
$pythonPath = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runnerPath) -or -not (Test-Path -LiteralPath $pythonPath)) {
    throw 'PKAS scheduled-sync runtime is incomplete'
}
try {
    $dailyTime = [datetime]::ParseExact(
        $DailyAt,
        'HH:mm',
        [System.Globalization.CultureInfo]::InvariantCulture
    )
} catch {
    throw "DailyAt must use 24-hour HH:mm format, for example 00:00. Received: $DailyAt"
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`" -ProjectRoot `"$resolvedRoot`" -DataRoot `"$DataRoot`" -QdrantUrl `"$QdrantUrl`""
if ($LocalOnly) {
    $arguments += ' -LocalOnly'
} else {
    $arguments += " -WeFlowRoot `"$WeFlowRoot`""
    if (-not [string]::IsNullOrWhiteSpace($WeFlowExportRoot)) {
        $arguments += " -WeFlowExportRoot `"$WeFlowExportRoot`""
    }
}
$action = New-ScheduledTaskAction -Execute $powershellPath -Argument $arguments -WorkingDirectory $resolvedRoot

# The task is intentionally once per day. The Python coordinator remains
# idempotent and uses export watermarks, so a missed night is safely caught up
# by the next available run without turning the machine into a poller.
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $dailyTime

$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
$task = New-ScheduledTask -Action $action -Trigger @($dailyTrigger) `
    -Principal $principal -Settings $settings `
    -Description "PKAS: daily authorized local-root refresh$(if ($LocalOnly) { '' } else { ' and enabled WeFlow import' }) at $DailyAt; hidden."
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

$registered = Get-ScheduledTask -TaskName $TaskName
[pscustomobject]@{
    status = 'configured'
    task_name = $registered.TaskName
    state = [string]$registered.State
    schedule = 'daily'
    daily_at = $DailyAt
    trigger_count = @($registered.Triggers).Count
    action = 'PKAS background scheduled-sync runner'
} | ConvertTo-Json -Compress
