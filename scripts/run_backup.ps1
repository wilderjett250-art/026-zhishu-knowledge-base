param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [int]$Retention = 3
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
$pythonPath = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'PKAS backup Python runtime is unavailable.'
}

$env:PKAS_PROJECT_ROOT = $resolvedRoot
$env:PKAS_DATA_ROOT = [System.IO.Path]::GetFullPath($DataRoot)
& $pythonPath -m pkas.backup_worker --label scheduled --retention $Retention
exit $LASTEXITCODE
