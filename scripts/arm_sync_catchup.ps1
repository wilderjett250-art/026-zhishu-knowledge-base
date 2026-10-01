# A single fixed-name, one-shot task; no resident process or periodic poller.
function Register-PkasSyncCatchUp {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][ValidateRange(1, 14400)][int]$DelaySeconds
    )
    $ErrorActionPreference = 'Stop'
    & (Join-Path $PSScriptRoot 'assert_windows_autostart_allowed.ps1') `
        -ProjectRoot $ProjectRoot -DataRoot $DataRoot
    $main = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $resolvedRoot = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
    $runnerPath = Join-Path $resolvedRoot 'scripts\run_scheduled_sync.ps1'
    $catchUpName = $TaskName + '-CatchUp'
    foreach ($task in @($main, (Get-ScheduledTask -TaskName $catchUpName -ErrorAction SilentlyContinue))) {
        if ($null -eq $task) { continue }
        if (@($task.Actions).Count -ne 1 -or
            -not ([string]$task.Actions[0].WorkingDirectory).TrimEnd('\').Equals(
                $resolvedRoot, [StringComparison]::OrdinalIgnoreCase) -or
            ([string]$task.Actions[0].Arguments).IndexOf(
                '"' + $runnerPath + '"', [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw 'Catch-up task belongs to another installation.'
        }
    }
    $when = (Get-Date).AddSeconds($DelaySeconds + 2)
    $trigger = New-ScheduledTaskTrigger -Once -At $when
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
    $task = New-ScheduledTask -Action $main.Actions -Trigger $trigger `
        -Principal $main.Principal -Settings $settings `
        -Description 'PKAS: one-shot missed daily sync after the boot cooldown; no polling.'
    Register-ScheduledTask -TaskName $catchUpName -InputObject $task -Force | Out-Null
}
