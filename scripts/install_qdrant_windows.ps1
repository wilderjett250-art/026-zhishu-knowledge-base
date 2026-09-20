param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ManifestPath = '',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $ManifestPath = Join-Path $resolvedRoot 'config\windows_replication.json'
}
$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$version = [string]$manifest.qdrant.version
$downloadUrl = [string]$manifest.qdrant.url
$expectedHash = ([string]$manifest.qdrant.sha256).ToLowerInvariant()
$runtimeRoot = Join-Path $resolvedRoot 'runtime\qdrant'
$executable = Join-Path $runtimeRoot 'qdrant.exe'

if ((Test-Path -LiteralPath $executable -PathType Leaf) -and -not $Force) {
    $versionText = (& $executable --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $versionText -match [regex]::Escape($version)) {
        [pscustomobject]@{
            status = 'already_installed'
            version = $version
            executable = $executable
        } | ConvertTo-Json -Compress
        exit 0
    }
}

$downloadRoot = Join-Path $resolvedRoot 'tmp\qdrant-install'
$archivePath = Join-Path $downloadRoot ([string]$manifest.qdrant.asset)
$extractRoot = Join-Path $downloadRoot 'extract'
if (Test-Path -LiteralPath $downloadRoot) {
    Remove-Item -LiteralPath $downloadRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null

try {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $archivePath -UseBasicParsing
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw 'Qdrant archive SHA-256 verification failed.'
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
    $candidate = Get-ChildItem -LiteralPath $extractRoot -Filter 'qdrant.exe' -File -Recurse |
        Select-Object -First 1
    if ($null -eq $candidate) {
        throw 'Qdrant archive does not contain qdrant.exe.'
    }
    New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
    Copy-Item -LiteralPath $candidate.FullName -Destination $executable -Force
    $versionText = (& $executable --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $versionText -notmatch [regex]::Escape($version)) {
        throw 'Installed Qdrant executable did not report the pinned version.'
    }
} finally {
    if (Test-Path -LiteralPath $downloadRoot) {
        Remove-Item -LiteralPath $downloadRoot -Recurse -Force
    }
}

[pscustomobject]@{
    status = 'installed'
    version = $version
    archive_sha256 = $expectedHash
    executable = $executable
} | ConvertTo-Json -Compress
