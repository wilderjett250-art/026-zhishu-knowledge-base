param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$QdrantUrl = 'http://127.0.0.1:6333',
    [int]$IntervalSeconds = 300
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
$pythonPath = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw "PKAS Python runtime is unavailable: $pythonPath"
}

Set-Location -LiteralPath $resolvedRoot
$env:PKAS_PROJECT_ROOT = $resolvedRoot
$env:PKAS_DATA_ROOT = [System.IO.Path]::GetFullPath($DataRoot)
$env:PKAS_QDRANT_URL = $QdrantUrl
& $pythonPath -m pkas.core_worker --interval-seconds $IntervalSeconds
exit $LASTEXITCODE
