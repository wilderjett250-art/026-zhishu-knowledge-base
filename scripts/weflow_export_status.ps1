# Read-only observation and bounded wait decisions. Return fixed codes only.
function Get-PkasWeFlowExportState {
    param(
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$ExportRoot,
        [Parameter(Mandatory)][long]$DayStartMs,
        [Parameter(Mandatory)][long]$NowMs
    )
    try {
        $raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
        if (-not $raw.TrimStart().StartsWith('{')) { throw 'Invalid config.' }
        $config = $raw | ConvertFrom-Json
        if ($config -isnot [pscustomobject]) { throw 'Invalid config.' }
        $scope = if ($config.dbPath -or $config.myWxid) {
            [string]$config.dbPath + '::' + [string]$config.myWxid
        } else { 'default' }
        foreach ($entry in $config.exportAutomationTaskMap.PSObject.Properties) {
            if ($entry.Name -cne $scope) { continue }
            foreach ($task in @($entry.Value.tasks)) {
                if ($task.id -ne 'pkas-weflow-daily-v1') { continue }
                if ($task.enabled -isnot [bool] -or -not $task.enabled -or
                    -not ([IO.Path]::GetFullPath([string]$task.outputDir).TrimEnd('\')).Equals(
                        [IO.Path]::GetFullPath($ExportRoot).TrimEnd('\'),
                        [StringComparison]::OrdinalIgnoreCase)) {
                    return [pscustomobject]@{Status='pending'; TriggeredAt=0; NextTriggerAt=0; ErrorCode='weflow_task_mismatch'}
                }
                $last = [long]$task.runState.lastTriggeredAt
                $state = [string]$task.runState.lastRunStatus
                if ($last -lt 0 -or $last -gt $NowMs + 300000) { throw 'Invalid timestamp.' }
                $safeError = if ([string]$task.runState.lastError -in @(
                    'managed_export_drive_low_space', 'managed_export_quota_exceeded',
                    'managed_export_directory_required', 'managed_export_directory_unavailable',
                    'managed_export_directory_unsafe', 'current_sessions_unavailable',
                    'some_sessions_failed')) { [string]$task.runState.lastError } else { 'export_failed' }
                if ($last -ge $DayStartMs -and $state -in @('success','skipped','error')) {
                    if ([long]$task.runState.lastFailedSessionCount -gt 0) {
                        $state = 'error'; $safeError = 'some_sessions_failed'
                    }
                    if ($state -ne 'error') { $safeError = '' }
                    return [pscustomobject]@{Status=$state; TriggeredAt=$last; NextTriggerAt=0; ErrorCode=$safeError}
                }
                if ($task.schedule.type -ne 'interval') { throw 'Unsupported schedule.' }
                $days = [int]$task.schedule.intervalDays
                $hours = [int]$task.schedule.intervalHours
                if ($days -lt 0 -or $hours -lt 0 -or $hours -gt 23 -or
                    ($days -eq 0 -and $hours -eq 0)) { throw 'Invalid interval.' }
                $interval = ([long]$days * 24 + $hours) * 3600000
                $next = if ($last -gt 0) {
                    if ($state -eq 'error') { $last + 1800000 }
                    elseif ($state -eq 'running') { $last + 7200000 }
                    else { $last + $interval }
                } elseif ([long]$task.schedule.firstTriggerAt -gt 0) {
                    [long]$task.schedule.firstTriggerAt
                } else { [long]$task.createdAt + $interval }
                $kind = if ($state -eq 'running') { 'running' }
                    elseif ($state -eq 'error') { 'retry' } else { 'schedule' }
                return [pscustomobject]@{Status='pending'; TriggeredAt=$last; NextTriggerAt=$next; PendingKind=$kind; ErrorCode=''}
            }
        }
        return [pscustomobject]@{Status='pending'; TriggeredAt=0; NextTriggerAt=0; ErrorCode='weflow_task_mismatch'}
    } catch {
        # Do not expose paths, account identifiers, raw errors or config values.
        return [pscustomobject]@{Status='pending'; TriggeredAt=0; NextTriggerAt=0; ErrorCode='weflow_config_unreadable'}
    }
}

function Resolve-PkasWeFlowPendingWait {
    param(
        [Parameter(Mandatory)]$Observed,
        [Parameter(Mandatory)][long]$NowMs,
        [Parameter(Mandatory)][long]$DeadlineMs,
        [Parameter(Mandatory)][long]$WaitStartedMs,
        [long]$ConfigUnreadableSinceMs = 0,
        [bool]$LauncherExited = $false
    )
    $code = ''
    if ($Observed.Status -eq 'pending') {
        if ($Observed.ErrorCode -eq 'weflow_task_mismatch') { $code = 'weflow_task_mismatch' }
        elseif ($LauncherExited) { $code = 'weflow_process_exited' }
        elseif ($Observed.ErrorCode -eq 'weflow_config_unreadable' -and
            $ConfigUnreadableSinceMs -gt 0 -and $NowMs - $ConfigUnreadableSinceMs -ge 120000) {
            $code = 'weflow_config_unreadable'
        } elseif ($Observed.PendingKind -eq 'schedule' -and
            $NowMs - $WaitStartedMs -ge 60000 -and
            [long]$Observed.NextTriggerAt -gt $DeadlineMs) {
            # Give a calendar-aware producer two 30-second ticks before
            # diagnosing an older, still interval-based producer's clock.
            $code = 'weflow_schedule_not_due'
        }
    }
    [pscustomobject]@{Stop=[bool]$code; ErrorCode=$code}
}
