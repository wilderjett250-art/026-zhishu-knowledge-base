param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$QdrantUrl = 'http://127.0.0.1:6333',
    [string]$WeFlowRoot = '',
    [string]$WeFlowExportRoot = '',
    [switch]$LocalOnly,
    [switch]$LaunchOnly,
    [ValidateRange(0, 7200)][int]$WeFlowWaitSeconds = 7200
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
$pythonPath = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
if ($LocalOnly) {
    if ($LaunchOnly) { throw 'LaunchOnly is incompatible with LocalOnly.' }
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw 'PKAS scheduled-sync Python runtime is unavailable.'
    }
    $env:PKAS_PROJECT_ROOT = $resolvedRoot
    $env:PKAS_DATA_ROOT = [System.IO.Path]::GetFullPath($DataRoot)
    $env:PKAS_QDRANT_URL = $QdrantUrl
    & $pythonPath -m pkas.scheduled_sync_worker --local-only
    exit $LASTEXITCODE
}
if ([string]::IsNullOrWhiteSpace($WeFlowRoot)) {
    throw 'WeFlowRoot must be explicitly supplied for legacy WeFlow scheduled sync.'
}
$nodePath = 'C:\Program Files\nodejs\node.exe'
$npmCliPath = 'C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js'
$weflowExecutable = Join-Path $WeFlowRoot 'node_modules\electron\dist\electron.exe'
$configureScript = Join-Path $resolvedRoot 'scripts\configure_weflow_daily.mjs'

foreach ($requiredPath in @($pythonPath, $nodePath, $npmCliPath, $weflowExecutable, $configureScript, $WeFlowRoot)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required scheduled-sync path is unavailable: $requiredPath"
    }
}

# A registered task must not start WeFlow until the user has explicitly
# authorized the daily import. The check reads only the local authorization
# state and fails closed; a disabled import still permits the normal local
# knowledge refresh to run without opening WeFlow.
& $pythonPath -m pkas.scheduled_sync_worker --check-daily-authorization | Out-Null
$authorizationExitCode = $LASTEXITCODE
if ($authorizationExitCode -eq 1) {
    throw 'WeFlow daily-import authorization preflight failed'
}
$dailyImportEnabled = $authorizationExitCode -eq 0

function Resolve-WeFlowExportRoot {
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
$weflowProcesses = Get-WeFlowProcesses
$startedByTask = $dailyImportEnabled -and $weflowProcesses.Count -eq 0
if ($startedByTask) {
    # Configure only before launch so the app cannot overwrite a newer in-memory
    # task definition. The Node helper never reads or prints secret fields.
    $env:WEFLOW_ROOT = $WeFlowRoot
    & $nodePath $configureScript | Out-Null
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
        status = if ($startedByTask) { 'started_hidden' } else { 'already_running' }
        weflow_processes = (Get-WeFlowProcesses).Count
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
    }
    return [pscustomobject]@{ Status = 'pending'; TriggeredAt = 0; ErrorCode = '' }
}

$workerExitCode = 1
try {
    $runningWithOldTask = $dailyImportEnabled -and -not $startedByTask -and
        -not (Test-WeFlowManagedTaskConfigured)
    $exportStatus = if ($runningWithOldTask) { 'error' } else { 'ready' }
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
                break
            }
            Start-Sleep -Seconds 15
        } while ($true)
    }

    $env:PKAS_PROJECT_ROOT = $resolvedRoot
    $env:PKAS_DATA_ROOT = [System.IO.Path]::GetFullPath($DataRoot)
    $env:PKAS_QDRANT_URL = $QdrantUrl
    & $pythonPath -m pkas.scheduled_sync_worker --weflow-export-status $exportStatus
    $workerExitCode = $LASTEXITCODE
} finally {
    # The task owns only the hidden dev launcher it started itself. Stop that
    # process tree so Vite/Electron do not consume RAM between nightly runs;
    # never touch a WeFlow process that was already running for the user.
    if ($startedByTask -and $null -ne $weflowLauncher -and -not $weflowLauncher.HasExited) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $weflowLauncher.Id /T /F 2>$null | Out-Null
    }
}
exit $workerExitCode
