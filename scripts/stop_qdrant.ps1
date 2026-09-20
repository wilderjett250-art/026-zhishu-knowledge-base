param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = ''
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$pidPath = Join-Path $resolvedDataRoot 'runs\qdrant.pid.json'
$expectedExecutable = [System.IO.Path]::GetFullPath(
    (Join-Path $resolvedRoot 'runtime\qdrant\qdrant.exe')
)

if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    [pscustomobject]@{ status = 'not_found'; data_root = $resolvedDataRoot } |
        ConvertTo-Json -Compress
    exit 0
}

$record = Get-Content -LiteralPath $pidPath -Raw | ConvertFrom-Json
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$record.pid)" `
    -ErrorAction SilentlyContinue
if ($null -eq $process) {
    Remove-Item -LiteralPath $pidPath -Force
    [pscustomobject]@{ status = 'already_stopped'; pid = [int]$record.pid } |
        ConvertTo-Json -Compress
    exit 0
}

$actualExecutable = [System.IO.Path]::GetFullPath([string]$process.ExecutablePath)
if (-not $actualExecutable.Equals($expectedExecutable, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'PID file points to a process outside this PKAS installation; refusing to stop it.'
}
Stop-Process -Id ([int]$record.pid) -Force
Remove-Item -LiteralPath $pidPath -Force
[pscustomobject]@{ status = 'stopped'; pid = [int]$record.pid } |
    ConvertTo-Json -Compress
