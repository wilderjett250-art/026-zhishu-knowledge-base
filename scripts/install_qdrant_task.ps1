param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [int]$HttpPort = 6333,
    [int]$GrpcPort = 6334,
    [string]$TaskName = 'PKAS-Qdrant'
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
& (Join-Path $resolvedRoot 'scripts\assert_windows_autostart_allowed.ps1') `
    -ProjectRoot $resolvedRoot -DataRoot $DataRoot
$runnerPath = Join-Path $resolvedRoot 'scripts\run_qdrant.ps1'
$qdrantPath = Join-Path $resolvedRoot 'runtime\qdrant\qdrant.exe'
foreach ($requiredPath in @($runnerPath, $qdrantPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "PKAS Qdrant runtime is incomplete: $requiredPath"
    }
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runnerPath`" -ProjectRoot `"$resolvedRoot`" -DataRoot `"$DataRoot`" -HttpPort $HttpPort -GrpcPort $GrpcPort"
$action = New-ScheduledTaskAction -Execute $powershellPath -Argument $arguments `
    -WorkingDirectory $resolvedRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$trigger.Delay = 'PT30S'
$principal = New-ScheduledTaskPrincipal -UserId $currentUser `
    -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'PKAS localhost-only Qdrant vector service.'
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

[pscustomobject]@{
    status = 'configured'
    task_name = $TaskName
    login_delay = 'PT30S'
    listen = "127.0.0.1:$HttpPort"
} | ConvertTo-Json -Compress
