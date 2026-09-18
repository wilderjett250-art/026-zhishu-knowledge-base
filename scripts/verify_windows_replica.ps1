param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$QdrantUrl = 'http://127.0.0.1:6333'
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$pythonPath = Join-Path $resolvedRoot '.venv\Scripts\python.exe'
$verifyScript = Join-Path $resolvedRoot 'scripts\verify_replica_runtime.py'
foreach ($requiredPath in @($pythonPath, $verifyScript)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "PKAS replica verification runtime is incomplete: $requiredPath"
    }
}

$previous = @{
    PKAS_PROJECT_ROOT = $env:PKAS_PROJECT_ROOT
    PKAS_DATA_ROOT = $env:PKAS_DATA_ROOT
    PKAS_EMBEDDING_ENABLED = $env:PKAS_EMBEDDING_ENABLED
    PKAS_RERANK_ENABLED = $env:PKAS_RERANK_ENABLED
    PKAS_DEEPSEEK_API_KEY = $env:PKAS_DEEPSEEK_API_KEY
    PKAS_AGENT_DAILY_CLOSEOUT_ENABLED = $env:PKAS_AGENT_DAILY_CLOSEOUT_ENABLED
}
try {
    $env:PKAS_PROJECT_ROOT = $resolvedRoot
    $env:PKAS_DATA_ROOT = $resolvedDataRoot
    $env:PKAS_EMBEDDING_ENABLED = 'false'
    $env:PKAS_RERANK_ENABLED = 'false'
    $env:PKAS_DEEPSEEK_API_KEY = ''
    $env:PKAS_AGENT_DAILY_CLOSEOUT_ENABLED = 'false'
    & $pythonPath $verifyScript --project-root $resolvedRoot `
        --data-root $resolvedDataRoot --qdrant-url $QdrantUrl
    exit $LASTEXITCODE
} finally {
    foreach ($name in $previous.Keys) {
        $value = $previous[$name]
        if ($null -eq $value) {
            Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}
