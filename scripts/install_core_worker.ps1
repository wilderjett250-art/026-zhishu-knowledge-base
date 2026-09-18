param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$QdrantUrl = 'http://127.0.0.1:6333',
    [string]$TaskName = 'PKAS-Core-Worker'
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
& (Join-Path $resolvedRoot 'scripts\assert_windows_autostart_allowed.ps1') `
    -ProjectRoot $resolvedRoot -DataRoot $DataRoot
$runnerPath = Join-Path $resolvedRoot 'scripts\run_core_worker.ps1'
$pythonPath = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
foreach ($requiredPath in @($runnerPath, $pythonPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "PKAS Core runtime is incomplete: $requiredPath"
    }
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`" -ProjectRoot `"$resolvedRoot`" -DataRoot `"$DataRoot`" -QdrantUrl `"$QdrantUrl`""
$action = New-ScheduledTaskAction -Execute $powershellPath -Argument $arguments `
    -WorkingDirectory $resolvedRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$trigger.Delay = 'PT1M'
$principal = New-ScheduledTaskPrincipal -UserId $currentUser `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'PKAS single-owner Outbox and Agent core worker.'
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

$registered = Get-ScheduledTask -TaskName $TaskName
[pscustomobject]@{
    status = 'configured'
    task_name = $registered.TaskName
    state = [string]$registered.State
    login_delay = $registered.Triggers[0].Delay
    action = 'PKAS single core worker'
    data_root = [System.IO.Path]::GetFullPath($DataRoot)
} | ConvertTo-Json -Compress
