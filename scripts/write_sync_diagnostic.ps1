# Dependency-free fallback for failures before/after the Python worker.
# Fixed codes only: no paths, account/session identifiers or exception text.
function Write-PkasLauncherDiagnostic {
    param(
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[a-f0-9]{32}$')][string]$RunId,
        [Parameter(Mandatory = $true)][string]$Code,
        [ValidateSet('failed', 'warning')][string]$Status = 'failed'
    )
    $ErrorActionPreference = 'Stop'
    $actions = @{
        worker_config = 'Check the scheduled task configuration.'
        worker_runtime_missing = 'Repair the PKAS Python runtime.'
        worker_failed = 'Check the local worker and its bounded diagnostics.'
        low_disk_space = 'Keep at least 2 GiB free on the knowledge data drive; no import was started.'
        sync_storage_unavailable = 'Restore access to the configured knowledge data drive.'
        sync_schedule_failed = 'Check daily-sync progress and the user-scoped catch-up task.'
        sync_receipt_failed = 'Check log directory permissions and free space.'
        sync_history_reset = 'Corrupt diagnostic history was reset; the current failure is preserved.'
        weflow_authorization_failed = 'Repair the explicit daily-import authorization.'
        weflow_export_environment = 'Repair the configured WeFlow/Node/Electron environment.'
        weflow_export_directory = 'Check the managed export directory and data drive.'
        weflow_export_low_disk = 'Keep at least 2 GiB free on the export drive.'
        weflow_configuration_failed = 'Check WeFlow automation configuration permissions.'
        weflow_launch_failed = 'Check the configured background launcher.'
        weflow_export_failed = 'Check the configured WeFlow export task.'
        weflow_cleanup_failed = 'Check the task-owned background process; preserve user processes.'
    }
    if (-not $actions.ContainsKey($Code)) { throw 'Unknown sync diagnostic code.' }
    $diagnosticRoot = Join-Path ([IO.Path]::GetFullPath($DataRoot)) 'runs\diagnostics\scheduled-sync'
    [IO.Directory]::CreateDirectory($diagnosticRoot) | Out-Null
    $receipt = [pscustomobject]@{
        version = 1
        kind = 'scheduled-sync'
        run_id = $RunId
        recorded_at = [DateTime]::UtcNow.ToString('o')
        status = $Status
        counts = @{}
        issues = @(@{ code = $Code; count = 1; action = $actions[$Code] })
    }
    $latest = Join-Path $diagnosticRoot 'launcher-latest.json'
    $historyPath = Join-Path $diagnosticRoot 'launcher-recent-issues.json'
    $history = @()
    $historyReset = $false
    if (Test-Path -LiteralPath $historyPath -PathType Leaf) {
        try {
            if ((Get-Item -LiteralPath $historyPath).Length -le 1MB) {
                $old = [IO.File]::ReadAllText($historyPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
                foreach ($row in @($old)) {
                    if ($history.Count -ge 19) { break }
                    if ([string]$row.run_id -notmatch '^[a-f0-9]{32}$' -or
                        [string]$row.status -notin @('failed', 'warning')) { continue }
                    $safeIssues = @()
                    foreach ($item in @($row.issues) | Select-Object -First 10) {
                        $oldCode = [string]$item.code
                        if ($actions.ContainsKey($oldCode)) {
                            $safeIssues += @{ code = $oldCode; count = 1; action = $actions[$oldCode] }
                        }
                    }
                    $parsedTime = [DateTimeOffset]::MinValue
                    if ($row.recorded_at -is [DateTime]) {
                        $parsedTime = [DateTimeOffset]$row.recorded_at.ToUniversalTime()
                    } elseif (-not [DateTimeOffset]::TryParse([string]$row.recorded_at, [ref]$parsedTime)) {
                        continue
                    }
                    if (-not $safeIssues.Count) { continue }
                    if ($row.run_id -eq $RunId) {
                        # A cleanup warning must not erase an earlier launcher
                        # failure from this same invocation.
                        if ($row.status -eq 'failed') { $receipt.status = 'failed' }
                        $currentCodes = @($receipt.issues | ForEach-Object { $_.code })
                        foreach ($oldIssue in $safeIssues) {
                            if ($oldIssue.code -notin $currentCodes -and $receipt.issues.Count -lt 10) {
                                $receipt.issues += $oldIssue
                                $currentCodes += $oldIssue.code
                            }
                        }
                        continue
                    }
                    $history += [pscustomobject]@{
                        version = 1; kind = 'scheduled-sync'; run_id = [string]$row.run_id
                        recorded_at = $parsedTime.UtcDateTime.ToString('o')
                        status = [string]$row.status; counts = @{}; issues = $safeIssues
                    }
                }
            } else { $historyReset = $true }
        } catch {
            # Corrupt history must not suppress the current failure receipt.
            $history = @()
            $historyReset = $true
        }
    }
    if ($historyReset) {
        $receipt.issues += @{ code = 'sync_history_reset'; count = 1; action = $actions.sync_history_reset }
    }
    $newHistory = @($receipt) + $history
    foreach ($target in @(
        @{ Path = $latest; Payload = $receipt },
        @{ Path = $historyPath; Payload = $newHistory }
    )) {
        $temporary = $target.Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
        try {
            $json = ConvertTo-Json -InputObject $target.Payload -Depth 8 -Compress
            [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
            if ([IO.File]::Exists($target.Path)) {
                [IO.File]::Replace($temporary, $target.Path, [NullString]::Value)
            } else {
                [IO.File]::Move($temporary, $target.Path)
            }
        } finally {
            if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
        }
    }
    return $receipt.status
}
