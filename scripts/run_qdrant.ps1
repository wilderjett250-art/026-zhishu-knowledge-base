param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [int]$HttpPort = 6333,
    [int]$GrpcPort = 6334
)

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start_qdrant.ps1') -ProjectRoot $ProjectRoot `
    -DataRoot $DataRoot -HttpPort $HttpPort -GrpcPort $GrpcPort
exit $LASTEXITCODE
