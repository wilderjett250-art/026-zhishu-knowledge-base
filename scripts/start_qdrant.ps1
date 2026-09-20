param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [int]$HttpPort = 6333,
    [int]$GrpcPort = 6334
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$executable = Join-Path $resolvedRoot 'runtime\qdrant\qdrant.exe'
$storage = Join-Path $resolvedDataRoot 'qdrant\storage'
$snapshots = Join-Path $resolvedDataRoot 'qdrant\snapshots'
$runLogs = Join-Path $resolvedDataRoot 'runs'
$config = Join-Path $runLogs 'qdrant.generated.yaml'
$pidPath = Join-Path $runLogs 'qdrant.pid.json'
$stdoutLog = Join-Path $runLogs 'qdrant-stdout.log'
$stderrLog = Join-Path $runLogs 'qdrant-stderr.log'

if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Qdrant is not installed at $executable"
}

$listening = @(Get-NetTCPConnection -LocalPort $HttpPort -State Listen -ErrorAction SilentlyContinue)
if ($listening.Count -gt 0) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$HttpPort/" -TimeoutSec 2
        [pscustomobject]@{
            status = 'already_running'
            version = $health.version
            http = "127.0.0.1:$HttpPort"
        } | ConvertTo-Json -Compress
        exit 0
    } catch {
        throw "Port $HttpPort is occupied by a service that is not a healthy Qdrant instance."
    }
}

New-Item -ItemType Directory -Path $storage -Force | Out-Null
New-Item -ItemType Directory -Path $snapshots -Force | Out-Null
New-Item -ItemType Directory -Path $runLogs -Force | Out-Null
$storageYaml = $storage.Replace('\', '/')
$snapshotsYaml = $snapshots.Replace('\', '/')
@"
log_level: INFO

storage:
  storage_path: $storageYaml
  snapshots_path: $snapshotsYaml
  on_disk_payload: true

service:
  host: 127.0.0.1
  http_port: $HttpPort
  grpc_port: $GrpcPort
  enable_cors: false
  enable_tls: false

telemetry_disabled: true
"@ | Set-Content -LiteralPath $config -Encoding UTF8

$quotedConfig = '"' + $config + '"'
$process = Start-Process -FilePath $executable `
    -ArgumentList @('--config-path', $quotedConfig) `
    -WorkingDirectory (Split-Path -Parent $executable) `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden -PassThru
[pscustomobject]@{
    pid = $process.Id
    executable = $executable
    data_root = $resolvedDataRoot
    http_port = $HttpPort
    grpc_port = $GrpcPort
} | ConvertTo-Json -Compress | Set-Content -LiteralPath $pidPath -Encoding UTF8

$deadline = (Get-Date).AddSeconds(30)
$health = $null
do {
    Start-Sleep -Milliseconds 500
    $listening = Get-NetTCPConnection -LocalPort $HttpPort -State Listen -ErrorAction SilentlyContinue
    if ($null -ne $listening) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$HttpPort/" -TimeoutSec 2
        } catch {
            $health = $null
        }
    }
} while ($null -eq $health -and (Get-Date) -lt $deadline)

if ($null -eq $health) {
    if (Test-Path -LiteralPath $pidPath) {
        Remove-Item -LiteralPath $pidPath -Force
    }
    throw "Qdrant did not pass HTTP health check within 30 seconds. Log: $stderrLog"
}

[pscustomobject]@{
    status = 'started'
    version = $health.version
    pid = $process.Id
    http = "127.0.0.1:$HttpPort"
    data_root = $resolvedDataRoot
} | ConvertTo-Json -Compress
