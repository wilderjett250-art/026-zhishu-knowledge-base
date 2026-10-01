param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$RuntimeRoot = '',
    [string]$QdrantUrl = 'http://127.0.0.1:6333',
    [string]$WeFlowRoot = '',
    [string]$WeFlowExportRoot = '',
    [switch]$LocalOnly,
    [ValidatePattern('^[A-Za-z0-9_-]{1,100}$')][string]$TaskName = 'PKAS-Knowledge-Sync',
    [string]$DailyAt = '00:00',
    [ValidateRange(1, 240)][int]$BootDelayMinutes = 60
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'resolve_sync_runtime.ps1')
$context = Resolve-PkasSyncRuntime -ProjectRoot $ProjectRoot -DataRoot $DataRoot -RuntimeRoot $RuntimeRoot
$resolvedRoot = $context.ProjectRoot
$DataRoot = $context.DataRoot
$RuntimeRoot = $context.RuntimeRoot
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
$pythonPath = $context.Python
if (-not (Test-Path -LiteralPath $runnerPath) -or -not (Test-Path -LiteralPath $pythonPath)) {
    throw 'PKAS scheduled-sync runtime is incomplete'
}
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existingTask -and (
    @($existingTask.Actions).Count -ne 1 -or
    -not ([string]$existingTask.Actions[0].WorkingDirectory).TrimEnd('\').Equals(
        $resolvedRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) -or
    ([string]$existingTask.Actions[0].Arguments).IndexOf(
        '"' + $runnerPath + '"', [StringComparison]::OrdinalIgnoreCase) -lt 0
)) {
    throw 'Task name belongs to another installation; refusing to overwrite it.'
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
$arguments += " -RuntimeRoot `"$RuntimeRoot`""
$arguments += " -Scheduled -ScheduledTaskName `"$TaskName`" -DailyAt `"$DailyAt`" -BootDelayMinutes $BootDelayMinutes"
if ($LocalOnly) {
    $arguments += ' -LocalOnly'
} else {
    $arguments += " -WeFlowRoot `"$WeFlowRoot`""
    if (-not [string]::IsNullOrWhiteSpace($WeFlowExportRoot)) {
        $arguments += " -WeFlowExportRoot `"$WeFlowExportRoot`""
    }
}
$action = New-ScheduledTaskAction -Execute $powershellPath -Argument $arguments -WorkingDirectory $resolvedRoot

# A user-scoped logon trigger does not require storing a Windows password or
# administrative boot-trigger privileges. The runner uses actual boot uptime
# and arms one one-shot catch-up task for the remaining cooldown, then exits.
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $dailyTime
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$logonTrigger.Delay = 'PT1M'

$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
$task = New-ScheduledTask -Action $action -Trigger @($dailyTrigger, $logonTrigger) `
    -Principal $principal -Settings $settings `
    -Description "PKAS: daily sync at $DailyAt; missed sync after $BootDelayMinutes minutes of boot uptime; hidden."
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

$registered = Get-ScheduledTask -TaskName $TaskName
[pscustomobject]@{
    status = 'configured'
    task_name = $registered.TaskName
    state = [string]$registered.State
    schedule = 'daily-and-boot-catchup'
    daily_at = $DailyAt
    boot_delay_minutes = $BootDelayMinutes
    trigger_count = @($registered.Triggers).Count
    action = 'PKAS background scheduled-sync runner'
} | ConvertTo-Json -Compress
