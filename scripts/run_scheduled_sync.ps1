param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$RuntimeRoot = '',
    [string]$QdrantUrl = 'http://127.0.0.1:6333',
    [string]$WeFlowRoot = '',
    [string]$WeFlowExportRoot = '',
    [switch]$LocalOnly,
    [switch]$LaunchOnly,
    [switch]$Scheduled,
    [ValidatePattern('^[A-Za-z0-9_-]{1,100}$')][string]$ScheduledTaskName = 'PKAS-Knowledge-Sync',
    [string]$DailyAt = '00:00',
    [ValidateRange(1, 240)][int]$BootDelayMinutes = 60,
    [ValidateRange(0, 7200)][int]$WeFlowWaitSeconds = 7200
)

$ErrorActionPreference = 'Stop'
$runId = [Guid]::NewGuid().ToString('N')
$workerExitCode = 1
$weflowLauncher = $null
$startedByTask = $false
$script:failureCode = 'worker_config'
$syncMutex = $null
$mutexOwned = $false
$script:diagnosticDataRootKnown = -not [string]::IsNullOrWhiteSpace($DataRoot)

function Write-LauncherIssue {
    param([string]$Code, [string]$Status = 'failed')
    try {
        if (-not $script:diagnosticDataRootKnown) { throw 'No verified data scope for a receipt.' }
        $receiptStatus = Write-PkasLauncherDiagnostic -DataRoot $DataRoot -RunId $runId -Code $Code -Status $Status
        [pscustomobject]@{ status = $receiptStatus; run_id = $runId; failure_code = $Code } |
            ConvertTo-Json -Compress
    } catch {
        # Logging cannot guarantee disk writes when the drive is missing/full.
        # Leave a safe signal and a nonzero task exit code, never exception text.
        [Console]::Error.WriteLine('[PKAS_SYNC] ' + $Code + '; sync_receipt_failed')
    }
}

function Test-CurrentWorkerReceipt {
    try {
        $path = Join-Path $DataRoot 'runs\diagnostics\scheduled-sync\latest.json'
        $receipt = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8) | ConvertFrom-Json
        return $receipt.run_id -eq $runId -and
            $receipt.status -in @('completed', 'deferred', 'failed', 'warning')
    } catch { return $false }
}

try {
. (Join-Path $PSScriptRoot 'write_sync_diagnostic.ps1')
# Set the requested data scope before the authorization subprocess. A task
# using a non-default database must never consult another installation's scope.
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path ([IO.Path]::GetFullPath($ProjectRoot)) 'data'
}
$DataRoot = [IO.Path]::GetFullPath($DataRoot)
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$script:failureCode = 'worker_runtime_missing'
. (Join-Path $PSScriptRoot 'resolve_sync_runtime.ps1')
# Leave an omitted packaged DataRoot for the resolver to read from the storage
# pointer; do not turn it into <install>/data, a different empty knowledge base.
$requestedDataRoot = $DataRoot
if (-not $PSBoundParameters.ContainsKey('DataRoot')) { $requestedDataRoot = '' }
$context = Resolve-PkasSyncRuntime -ProjectRoot $resolvedRoot -DataRoot $requestedDataRoot -RuntimeRoot $RuntimeRoot
$DataRoot = $context.DataRoot
$script:diagnosticDataRootKnown = $true
$RuntimeRoot = $context.RuntimeRoot
$env:PKAS_PROJECT_ROOT = $resolvedRoot
$env:PKAS_DATA_ROOT = $DataRoot
$env:PKAS_RUNTIME_ROOT = $RuntimeRoot
$env:PYTHONPATH = Join-Path $resolvedRoot 'src'
if ($context.Packaged) { $env:PKAS_ENV_FILE = Join-Path $DataRoot 'config\.env' }
$env:PKAS_QDRANT_URL = $QdrantUrl
$env:PKAS_SYNC_RUN_ID = $runId
$pythonPath = $context.Python
$script:failureCode = 'worker_runtime_missing'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Python runtime unavailable.'
}
# Main and one-shot tasks share a non-blocking per-installation mutex. Their
# individual IgnoreNew settings alone cannot prevent cross-task concurrency.
$mutexKey = $resolvedRoot.ToLowerInvariant() + '|' + $DataRoot.ToLowerInvariant()
$sha = [Security.Cryptography.SHA256]::Create()
try { $mutexHash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($mutexKey))).Replace('-', '') }
finally { $sha.Dispose() }
$syncMutex = [Threading.Mutex]::new($false, ('Local\PKAS-Sync-' + $mutexHash))
try { $mutexOwned = $syncMutex.WaitOne(0) }
catch [Threading.AbandonedMutexException] { $mutexOwned = $true }
if (-not $mutexOwned) {
    [pscustomobject]@{status = 'already_running'; run_id = $runId} | ConvertTo-Json -Compress
    exit 0
}
if ($Scheduled) {
    $script:failureCode = 'sync_schedule_failed'
    if ($LaunchOnly) { throw 'Scheduled is incompatible with LaunchOnly.' }
    & (Join-Path $resolvedRoot 'scripts\assert_windows_autostart_allowed.ps1') `
        -ProjectRoot $resolvedRoot -DataRoot $DataRoot
    $checkArgs = @('-m', 'pkas.sync_schedule', '--daily-at', $DailyAt, '--boot-delay-minutes', $BootDelayMinutes)
    # Local-only and explicitly disabled WeFlow schedules use a different
    # completion scope; enabling WeFlow later must not inherit their success.
    if ($LocalOnly) {
        $checkArgs += '--local-only'
    } else {
        & $pythonPath -m pkas.scheduled_sync_worker --check-daily-authorization 2>$null | Out-Null
        if ($LASTEXITCODE -notin @(0, 2)) { throw 'Schedule authorization failed.' }
        if ($LASTEXITCODE -eq 2) { $checkArgs += '--local-only' }
    }
    $decisionText = & $pythonPath @checkArgs 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Daily schedule state unavailable.' }
    $decision = ($decisionText -join '') | ConvertFrom-Json
    if ($decision.status -eq 'not-due') {
        [pscustomobject]@{status = 'not-due'; run_id = $runId} | ConvertTo-Json -Compress
        exit 0
    }
    if ($decision.status -eq 'boot-delay') {
        . (Join-Path $PSScriptRoot 'arm_sync_catchup.ps1')
        Register-PkasSyncCatchUp -ProjectRoot $resolvedRoot -DataRoot $DataRoot `
            -TaskName $ScheduledTaskName -DelaySeconds $decision.delay_seconds
        [pscustomobject]@{status = 'boot-delay'; delay_seconds = $decision.delay_seconds; run_id = $runId} | ConvertTo-Json -Compress
        exit 0
    }
    if ($decision.status -ne 'due') { throw 'Unknown schedule decision.' }
}
# Reject low-capacity imports before opening SQLite or starting/exporting
# WeFlow. The guard must not create a directory merely to measure its drive.
$script:failureCode = 'sync_storage_unavailable'
$dataDriveRoot = [IO.Path]::GetPathRoot($DataRoot)
$dataDrive = [IO.DriveInfo]::new($dataDriveRoot)
if (-not $dataDrive.IsReady) { throw 'Data drive unavailable.' }
$script:failureCode = 'low_disk_space'
if ($dataDrive.AvailableFreeSpace -lt 2GB) { throw 'Data drive has insufficient free space.' }
if ($LocalOnly) {
    $script:failureCode = 'worker_config'
    if ($LaunchOnly) { throw 'LaunchOnly is incompatible with LocalOnly.' }
    $script:failureCode = 'worker_failed'
    $localArguments = @('-m', 'pkas.scheduled_sync_worker', '--local-only')
    if ($Scheduled) { $localArguments += '--record-daily-check' }
    & $pythonPath @localArguments 2>$null
    $workerExitCode = $LASTEXITCODE
    if (-not (Test-CurrentWorkerReceipt)) {
        Write-LauncherIssue -Code 'sync_receipt_failed'
        $workerExitCode = 1
    }
    exit $workerExitCode
}
$nodePath = $context.Node
$npmCliPath = $context.NpmCli
$configureScript = Join-Path $resolvedRoot 'scripts\configure_weflow_daily.mjs'

# A registered task must not start WeFlow until the user has explicitly
# authorized the daily import. The check reads only the local authorization
# state and fails closed; a disabled import still permits the normal local
# knowledge refresh to run without opening WeFlow.
$script:failureCode = 'weflow_authorization_failed'
& $pythonPath -m pkas.scheduled_sync_worker --check-daily-authorization 2>$null | Out-Null
$authorizationExitCode = $LASTEXITCODE
if ($authorizationExitCode -notin @(0, 2)) {
    throw 'WeFlow daily-import authorization preflight failed'
}
$dailyImportEnabled = $authorizationExitCode -eq 0
if ($dailyImportEnabled) {
    $script:failureCode = 'weflow_export_environment'
    if ([string]::IsNullOrWhiteSpace($WeFlowRoot)) {
        throw 'WeFlowRoot must be explicitly supplied.'
    }
    $weflowExecutable = Join-Path $WeFlowRoot 'node_modules\electron\dist\electron.exe'
    foreach ($requiredPath in @($nodePath, $npmCliPath, $weflowExecutable, $configureScript, $WeFlowRoot)) {
        if (-not (Test-Path -LiteralPath $requiredPath)) { throw 'Export runtime unavailable.' }
    }
}

function Resolve-WeFlowExportRoot {
    $script:failureCode = 'weflow_export_directory'
    if (-not [string]::IsNullOrWhiteSpace($WeFlowExportRoot)) {
        $candidate = [IO.Path]::GetFullPath($WeFlowExportRoot)
    } else {
        $candidate = ''
        foreach ($driveLetter in @('I', 'G')) {
            $drive = Get-PSDrive -Name $driveLetter -PSProvider FileSystem -ErrorAction SilentlyContinue
            if ($null -ne $drive -and $drive.Free -ge 2GB) {
                $candidate = Join-Path $drive.Root 'PKAS-WeFlow-Exports'
                break
            }
        }
        if (-not $candidate) {
            $dataDrive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($DataRoot))
            if (-not $dataDrive -or $dataDrive.TrimEnd('\').Equals(
                [string]$env:SystemDrive, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'No non-system data drive is available for managed WeFlow exports.'
            }
            $candidate = Join-Path $dataDrive 'PKAS-WeFlow-Exports'
        }
    }
    if ([IO.Path]::GetFileName($candidate.TrimEnd('\')) -ine 'PKAS-WeFlow-Exports') {
        throw 'Managed WeFlow export root must end in PKAS-WeFlow-Exports.'
    }
    $driveRoot = [IO.Path]::GetPathRoot($candidate)
    if (-not $driveRoot -or -not (Test-Path -LiteralPath $driveRoot -PathType Container)) {
        throw 'Managed WeFlow export drive is unavailable.'
    }
    $driveName = $driveRoot.Substring(0, 1)
    $drive = Get-PSDrive -Name $driveName -PSProvider FileSystem -ErrorAction Stop
    if ($drive.Free -lt 2GB) {
        $script:failureCode = 'weflow_export_low_disk'
        throw 'Managed WeFlow export drive has less than 2 GiB free.'
    }
    if (Test-Path -LiteralPath $candidate) {
        $item = Get-Item -LiteralPath $candidate -Force
        if (-not $item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Managed WeFlow export root must be an ordinary directory.'
        }
    }
    return $candidate
}

function Get-WeFlowProcesses {
    $processIds = @(Get-CimInstance Win32_Process -Filter "Name = 'electron.exe'" | Where-Object {
        $_.Name -eq 'electron.exe' -and
        -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
        $_.CommandLine.IndexOf($weflowExecutable, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    } | Select-Object -ExpandProperty ProcessId)
    # An elevated desktop WeFlow can hide CommandLine/ExecutablePath from an
    # unelevated scheduled task. tasklist still exposes its visible window
    # title, so include that exact PID rather than starting a second instance.
    try {
        $rows = @(& "$env:SystemRoot\System32\tasklist.exe" /FI 'IMAGENAME eq electron.exe' /V /FO CSV 2>$null | ConvertFrom-Csv)
        foreach ($row in $rows) {
            $fields = @($row.PSObject.Properties | ForEach-Object Value)
            if ($fields.Count -lt 9 -or [string]$fields[-1] -cne 'WeFlow') { continue }
            $processIdValue = 0
            if ([int]::TryParse([string]$fields[1], [ref]$processIdValue)) {
                $processIds += $processIdValue
            }
        }
    } catch {
        # Keep the command-line matches; an unavailable window list must not
        # make a known WeFlow process disappear from the result.
    }
    @($processIds | Sort-Object -Unique)
}

if ($dailyImportEnabled) {
    $env:PKAS_WEFLOW_EXPORT_ROOT = Resolve-WeFlowExportRoot
}
$script:failureCode = 'weflow_export_environment'
$weflowProcesses = if ($dailyImportEnabled) { @(Get-WeFlowProcesses) } else { @() }
$startedByTask = $dailyImportEnabled -and $weflowProcesses.Count -eq 0
if ($startedByTask) {
    # Configure only before launch so the app cannot overwrite a newer in-memory
    # task definition. The Node helper never reads or prints secret fields.
    $env:WEFLOW_ROOT = $WeFlowRoot
    $script:failureCode = 'weflow_configuration_failed'
    & $nodePath $configureScript 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'WeFlow automation configuration failed'
    }
    # Do not launch npm.cmd here. A .cmd launcher creates a child console even
    # when the outer scheduled PowerShell process is hidden. Invoke npm-cli.js
    # with node.exe and CreateNoWindow so the complete npm/vite/electron tree
    # inherits a background console state without flashing a terminal window.
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $nodePath
    $startInfo.Arguments = '"' + $npmCliPath + '" run electron:dev'
    $startInfo.WorkingDirectory = $WeFlowRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    # WeFlow's automation scheduler lives on /export. Background mode opens
    # that route without the splash, tray, or a user-visible window.
    $startInfo.EnvironmentVariables['WEFLOW_BACKGROUND_EXPORT'] = '1'
    $script:failureCode = 'weflow_launch_failed'
    $weflowLauncher = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $weflowLauncher) {
        throw 'WeFlow background launcher did not start'
    }
}

function Test-WeFlowManagedTaskConfigured {
    $configPath = Join-Path $env:APPDATA 'weflow\WeFlow-config.json'
    try {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $dbPath = [string]$config.dbPath
        $myWxid = [string]$config.myWxid
        $scope = if ($dbPath -or $myWxid) { "${dbPath}::${myWxid}" } else { 'default' }
        foreach ($entry in $config.exportAutomationTaskMap.PSObject.Properties) {
            if ($entry.Name -cne $scope) { continue }
            foreach ($task in @($entry.Value.tasks)) {
                if ($task.id -ne 'pkas-weflow-daily-v1') { continue }
                if (-not $task.enabled) { return $false }
                $configured = [IO.Path]::GetFullPath([string]$task.outputDir).TrimEnd('\')
                return $configured.Equals(
                    $env:PKAS_WEFLOW_EXPORT_ROOT.TrimEnd('\'),
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
        }
    } catch {
        return $false
    }
    return $false
}

if ($LaunchOnly) {
    [pscustomobject]@{
        status = if (-not $dailyImportEnabled) { 'disabled' } elseif ($startedByTask) {
            'started_hidden'
        } else { 'already_running' }
        weflow_processes = if ($dailyImportEnabled) { (Get-WeFlowProcesses).Count } else { 0 }
        console_mode = 'create_no_window'
    } | ConvertTo-Json -Compress
    exit 0
}

# Starting the Electron app does not mean its asynchronous export has finished.
# Wait for today's automation run before importing exports, but never print the
# account key, session names, messages, or paths from its local config file.
function Get-WeFlowDailyExportStatus {
    $configPath = Join-Path $env:APPDATA 'weflow\WeFlow-config.json'
    try {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $dbPath = [string]$config.dbPath
        $myWxid = [string]$config.myWxid
        $scope = if ($dbPath -or $myWxid) { "${dbPath}::${myWxid}" } else { 'default' }
        $dayStart = [DateTimeOffset]::new((Get-Date).Date).ToUnixTimeMilliseconds()
        foreach ($entry in $config.exportAutomationTaskMap.PSObject.Properties) {
            if ($entry.Name -cne $scope) { continue }
            foreach ($task in @($entry.Value.tasks)) {
                if ($task.id -ne 'pkas-weflow-daily-v1') { continue }
                if ([long]$task.runState.lastTriggeredAt -lt $dayStart) { continue }
                $state = [string]$task.runState.lastRunStatus
                if ($state -in @('success', 'skipped', 'error')) {
                    return [pscustomobject]@{
                        Status = $state
                        TriggeredAt = [long]$task.runState.lastTriggeredAt
                        ErrorCode = [string]$task.runState.lastError
                    }
                }
            }
        }
    } catch {
        # A concurrent electron-store write can briefly expose incomplete JSON.
        # Retry until the bounded deadline rather than reading any chat content.
        return [pscustomobject]@{
            Status = 'pending'; TriggeredAt = 0; ErrorCode = 'weflow_config_unreadable'
        }
    }
    return [pscustomobject]@{ Status = 'pending'; TriggeredAt = 0; ErrorCode = '' }
}

$script:failureCode = 'weflow_export_failed'
    $runningWithOldTask = $dailyImportEnabled -and -not $startedByTask -and
        -not (Test-WeFlowManagedTaskConfigured)
    $exportStatus = if ($runningWithOldTask) { 'error' } else { 'ready' }
    $exportFailureCode = if ($runningWithOldTask) { 'weflow_task_mismatch' } else { '' }
    if ($dailyImportEnabled -and -not $runningWithOldTask) {
        $deadline = (Get-Date).AddSeconds($WeFlowWaitSeconds)
        $firstErrorTrigger = $null
        do {
            $observed = Get-WeFlowDailyExportStatus
            if ($observed.Status -in @('success', 'skipped')) {
                $exportStatus = if ($observed.Status -eq 'success') { 'ready' } else { 'skipped' }
                break
            }
            if ($observed.Status -eq 'error') {
                $exportFailureCode = switch ($observed.ErrorCode) {
                    'managed_export_drive_low_space' { 'weflow_export_low_disk' }
                    'managed_export_quota_exceeded' { 'weflow_export_quota' }
                    'managed_export_directory_required' { 'weflow_export_directory' }
                    'managed_export_directory_unavailable' { 'weflow_export_directory' }
                    'managed_export_directory_unsafe' { 'weflow_export_directory' }
                    'current_sessions_unavailable' { 'weflow_export_sessions' }
                    'some_sessions_failed' { 'weflow_export_partial' }
                    default { 'weflow_export_failed' }
                }
                if ($observed.ErrorCode -like 'managed_export_*') {
                    # Retrying cannot fix an invalid/full managed export root
                    # while this invocation owns the same process and path.
                    $exportStatus = 'error'
                    break
                }
                if ($null -eq $firstErrorTrigger) {
                    $firstErrorTrigger = $observed.TriggeredAt
                } elseif ($observed.TriggeredAt -gt $firstErrorTrigger) {
                    # The main-process scheduler retried once and failed again.
                    $exportStatus = 'error'
                    break
                }
            }
            if ((Get-Date) -ge $deadline) {
                $exportStatus = if ($observed.Status -eq 'error') { 'error' } else { 'timeout' }
                if ($exportStatus -eq 'timeout') {
                    $exportFailureCode = if ($observed.ErrorCode -eq 'weflow_config_unreadable') {
                        'weflow_config_unreadable'
                    } else { 'weflow_export_timeout' }
                }
                break
            }
            Start-Sleep -Seconds 15
        } while ($true)
    }

    $script:failureCode = 'worker_failed'
    $workerArguments = @('-m', 'pkas.scheduled_sync_worker', '--weflow-export-status', $exportStatus)
    if ($exportFailureCode) { $workerArguments += @('--weflow-export-code', $exportFailureCode) }
    if ($Scheduled) { $workerArguments += '--record-daily-check' }
    & $pythonPath @workerArguments 2>$null
    $workerExitCode = $LASTEXITCODE
    if (-not (Test-CurrentWorkerReceipt)) {
        Write-LauncherIssue -Code 'sync_receipt_failed'
        $workerExitCode = 1
    }
} catch {
    Write-LauncherIssue -Code $script:failureCode
    $workerExitCode = 1
} finally {
    # The task owns only the hidden dev launcher it started itself. Stop that
    # process tree so Vite/Electron do not consume RAM between nightly runs;
    # never touch a WeFlow process that was already running for the user.
    try {
        if (-not $LaunchOnly -and $startedByTask -and $null -ne $weflowLauncher -and
            -not $weflowLauncher.HasExited) {
            & "$env:SystemRoot\System32\taskkill.exe" /PID $weflowLauncher.Id /T /F 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0 -and -not $weflowLauncher.HasExited) {
                throw 'Background cleanup failed.'
            }
        }
    } catch {
        Write-LauncherIssue -Code 'weflow_cleanup_failed' -Status 'warning'
        if ($workerExitCode -eq 0) { $workerExitCode = 2 }
    }
    if ($mutexOwned -and $null -ne $syncMutex) { $syncMutex.ReleaseMutex() }
    if ($null -ne $syncMutex) { $syncMutex.Dispose() }
}
exit $workerExitCode
