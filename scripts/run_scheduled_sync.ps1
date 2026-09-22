param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$QdrantUrl = 'http://127.0.0.1:6333',
    [string]$WeFlowRoot = '',
    [switch]$LaunchOnly
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
if ([string]::IsNullOrWhiteSpace($WeFlowRoot)) {
    throw 'WeFlowRoot must be explicitly supplied for legacy WeFlow scheduled sync.'
}
$pythonPath = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
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

function Get-WeFlowProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'electron.exe' -and
        -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
        $_.CommandLine.IndexOf($weflowExecutable, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
    })
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
    $weflowLauncher = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $weflowLauncher) {
        throw 'WeFlow background launcher did not start'
    }

    # WeFlow is a desktop app. Hide only windows belonging to the instance this
    # task just started; never alter a window the user opened themselves.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class PkasWindowControl {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr extraData);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
}
'@
    for ($attempt = 0; $attempt -lt 45; $attempt += 1) {
        $targetIds = @((Get-WeFlowProcesses).ProcessId)
        if ($targetIds.Count -gt 0) {
            [PkasWindowControl]::EnumWindows({
                param([IntPtr]$windowHandle, [IntPtr]$extraData)
                [uint32]$processId = 0
                [void][PkasWindowControl]::GetWindowThreadProcessId($windowHandle, [ref]$processId)
                if ($targetIds -contains [int]$processId) {
                    [void][PkasWindowControl]::ShowWindow($windowHandle, 0)
                }
                return $true
            }, [IntPtr]::Zero) | Out-Null
        }
        Start-Sleep -Seconds 1
    }
}

if ($LaunchOnly) {
    [pscustomobject]@{
        status = if ($startedByTask) { 'started_hidden' } else { 'already_running' }
        weflow_processes = (Get-WeFlowProcesses).Count
        console_mode = 'create_no_window'
    } | ConvertTo-Json -Compress
    exit 0
}

$env:PKAS_PROJECT_ROOT = $resolvedRoot
$env:PKAS_DATA_ROOT = [System.IO.Path]::GetFullPath($DataRoot)
$env:PKAS_QDRANT_URL = $QdrantUrl
& $pythonPath -m pkas.scheduled_sync_worker
exit $LASTEXITCODE
