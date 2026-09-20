param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$venvRoot = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot '.venv')).TrimEnd('\') + '\'
$allowedModules = @(
    'pkas.cli',
    'pkas.core_worker',
    'pkas.scheduled_sync_worker',
    'pkas.sync_worker',
    'pkas.agent_worker',
    'pkas.vector_worker'
)
$targets = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('python.exe', 'pythonw.exe') -and
        -not [string]::IsNullOrWhiteSpace($_.ExecutablePath) -and
        [System.IO.Path]::GetFullPath($_.ExecutablePath).StartsWith(
            $venvRoot,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
        -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
        $_.CommandLine -match '(?:^|\s)-m\s+([^\s\"]+)' -and
        $allowedModules -contains $Matches[1]
    }
)
$stopped = [System.Collections.Generic.List[object]]::new()
foreach ($process in $targets) {
    $module = 'unknown'
    if ($process.CommandLine -match '(?:^|\s)-m\s+([^\s\"]+)') {
        $module = $Matches[1]
    }
    Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    $stopped.Add([pscustomobject]@{ pid = $process.ProcessId; module = $module })
}
[pscustomobject]@{
    status = 'completed'
    stopped_count = $stopped.Count
    stopped = $stopped
    scope = 'this_installation_venv_only'
} | ConvertTo-Json -Depth 4 -Compress
