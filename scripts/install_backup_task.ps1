param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$TaskName = 'PKAS-Daily-Backup',
    [string]$DailyAt = '03:00',
    [int]$Retention = 3
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
& (Join-Path $resolvedRoot 'scripts\assert_windows_autostart_allowed.ps1') `
    -ProjectRoot $resolvedRoot -DataRoot $DataRoot
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$runnerPath = Join-Path $resolvedRoot 'scripts\run_backup.ps1'
$pythonPath = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runnerPath) -or -not (Test-Path -LiteralPath $pythonPath)) {
    throw 'PKAS backup runtime is incomplete.'
}
if ($Retention -lt 1 -or $Retention -gt 30) {
    throw 'Retention must be between 1 and 30.'
}
try {
    $at = [datetime]::ParseExact($DailyAt, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
} catch {
    throw 'DailyAt must use 24-hour HH:mm format.'
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`" -ProjectRoot `"$resolvedRoot`" -DataRoot `"$resolvedDataRoot`" -Retention $Retention"
$action = New-ScheduledTaskAction -Execute $powershellPath -Argument $arguments `
    -WorkingDirectory $resolvedRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $at
$principal = New-ScheduledTaskPrincipal -UserId $currentUser `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'PKAS verified daily recovery bundle; secrets excluded.'
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

$registered = Get-ScheduledTask -TaskName $TaskName
[pscustomobject]@{
    status = 'configured'
    task_name = $registered.TaskName
    state = [string]$registered.State
    daily_at = $DailyAt
    retention = $Retention
    secrets_included = $false
    action = 'PKAS verified recovery-bundle runner'
} | ConvertTo-Json -Compress
