[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$StagingRoot = ''
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$runtimeConfigPath = Join-Path $resolvedRoot 'config\desktop_runtime.json'
$runtimeConfig = Get-Content -LiteralPath $runtimeConfigPath -Raw | ConvertFrom-Json
$replicationConfig = Get-Content -LiteralPath (Join-Path $resolvedRoot 'config\windows_replication.json') -Raw | ConvertFrom-Json

if ([string]::IsNullOrWhiteSpace($StagingRoot)) {
    if (Test-Path -LiteralPath 'I:\') {
        $StagingRoot = 'I:\PKAS-build-cache\zhishu-desktop'
    } elseif (Test-Path -LiteralPath 'G:\') {
        $StagingRoot = 'G:\PKAS-build-cache\zhishu-desktop'
    } else {
        throw 'Provide -StagingRoot on a non-system data drive; the installer build must not stage downloads on C:.'
    }
}
$resolvedStaging = [System.IO.Path]::GetFullPath($StagingRoot)
if ($resolvedStaging.StartsWith('C:\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'StagingRoot must not be on C:.'
}
New-Item -ItemType Directory -Path $resolvedStaging -Force | Out-Null

function Get-Sha256 {
    param([Parameter(Mandatory)][string]$Path)
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-Archive {
    param(
        [Parameter(Mandatory)]$Definition,
        [Parameter(Mandatory)][string]$StageDirectory
    )

    $archivePath = Join-Path $StageDirectory $Definition.asset
    if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
        $existingHash = Get-Sha256 -Path $archivePath
        if ($existingHash -ne $Definition.sha256) {
            throw "Pinned runtime archive exists with an unexpected SHA-256: $archivePath"
        }
        return $archivePath
    }

    $temporary = Join-Path $StageDirectory ($Definition.asset + '.partial-' + [guid]::NewGuid().ToString('N'))
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Definition.url -OutFile $temporary -UseBasicParsing
        $downloadHash = Get-Sha256 -Path $temporary
        if ($downloadHash -ne $Definition.sha256) {
            throw "Downloaded runtime archive failed SHA-256 verification: $($Definition.asset)"
        }
        Move-Item -LiteralPath $temporary -Destination $archivePath
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
    return $archivePath
}

function Get-ExtractedBinary {
    param(
        [Parameter(Mandatory)][string]$Archive,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$StageDirectory
    )
    $extractRoot = Join-Path $StageDirectory ('extract-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $extractRoot | Out-Null
    try {
        Expand-Archive -LiteralPath $Archive -DestinationPath $extractRoot
        $matches = @(Get-ChildItem -LiteralPath $extractRoot -Filter $Name -File -Recurse)
        if ($matches.Count -ne 1) {
            throw "Expected exactly one $Name in the pinned runtime archive; found $($matches.Count)."
        }
        $temporaryBinary = Join-Path $StageDirectory ('verified-' + [guid]::NewGuid().ToString('N') + '-' + $Name)
        Copy-Item -LiteralPath $matches[0].FullName -Destination $temporaryBinary
        return $temporaryBinary
    } finally {
        if (Test-Path -LiteralPath $extractRoot) {
            Remove-Item -LiteralPath $extractRoot -Recurse -Force
        }
    }
}

$runtimePayload = Join-Path $resolvedRoot 'runtime\tauri-payload'
New-Item -ItemType Directory -Path $runtimePayload -Force | Out-Null
$uvArchive = Get-Archive -Definition $runtimeConfig.uv -StageDirectory $resolvedStaging
$qdrantArchive = Get-Archive -Definition $runtimeConfig.qdrant -StageDirectory $resolvedStaging
$nodeArchive = Get-Archive -Definition $runtimeConfig.node -StageDirectory $resolvedStaging
$uvSource = Get-ExtractedBinary -Archive $uvArchive -Name 'uv.exe' -StageDirectory $resolvedStaging
$qdrantSource = Get-ExtractedBinary -Archive $qdrantArchive -Name 'qdrant.exe' -StageDirectory $resolvedStaging
$nodeExtractRoot = Join-Path $resolvedStaging ('node-extract-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $nodeExtractRoot | Out-Null

try {
    Expand-Archive -LiteralPath $nodeArchive -DestinationPath $nodeExtractRoot
    $nodeRoots = @(Get-ChildItem -LiteralPath $nodeExtractRoot -Directory | Where-Object { $_.Name -eq "node-v$($runtimeConfig.node.version)-win-x64" })
    if ($nodeRoots.Count -ne 1) {
        throw 'Expected the pinned x64 Node.js distribution folder in the official archive.'
    }
    $nodeSourceRoot = $nodeRoots[0].FullName
    $nodeSource = Join-Path $nodeSourceRoot 'node.exe'
    $npmSource = Join-Path $nodeSourceRoot 'node_modules\npm\bin\npm-cli.js'
    if (-not (Test-Path -LiteralPath $nodeSource -PathType Leaf) -or -not (Test-Path -LiteralPath $npmSource -PathType Leaf)) {
        throw 'Pinned Node.js distribution is missing node.exe or npm-cli.js.'
    }
    $nodeVersionOutput = (& $nodeSource --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $nodeVersionOutput -ne "v$($runtimeConfig.node.version)") {
        throw 'The extracted Node.js binary did not report the pinned version.'
    }

    $everythingExecutable = Join-Path $resolvedRoot 'tools\everything\everything.exe'
    if (-not (Test-Path -LiteralPath $everythingExecutable -PathType Leaf)) {
        $everythingExecutable = Join-Path $resolvedRoot 'tools\everything\Everything.exe'
    }
    if ((Get-Sha256 -Path $everythingExecutable) -ne $runtimeConfig.everything.executable_sha256) {
        throw 'The bundled Everything executable did not match the reviewed SHA-256.'
    }

    $uvVersionOutput = (& $uvSource --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $uvVersionOutput.StartsWith("uv $($runtimeConfig.uv.version) ")) {
        throw 'The extracted uv binary did not report the pinned version.'
    }
    $qdrantVersionOutput = (& $qdrantSource --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $qdrantVersionOutput -notmatch "\b$([regex]::Escape($runtimeConfig.qdrant.version))\b") {
        throw 'The extracted Qdrant binary did not report the pinned version.'
    }

    $uvDestination = Join-Path $runtimePayload 'uv.exe'
    $qdrantDirectory = Join-Path $runtimePayload 'qdrant'
    $qdrantDestination = Join-Path $qdrantDirectory 'qdrant.exe'
    $nodeDestination = Join-Path $runtimePayload 'node'
    New-Item -ItemType Directory -Path $qdrantDirectory,$nodeDestination -Force | Out-Null
    Copy-Item -LiteralPath $uvSource -Destination $uvDestination -Force
    Copy-Item -LiteralPath $qdrantSource -Destination $qdrantDestination -Force
    Get-ChildItem -LiteralPath $nodeSourceRoot -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $nodeDestination -Recurse -Force
    }
    if ((Get-Sha256 -Path $uvDestination) -ne (Get-Sha256 -Path $uvSource) -or
        (Get-Sha256 -Path $qdrantDestination) -ne (Get-Sha256 -Path $qdrantSource)) {
        throw 'A runtime executable changed while being copied into the application resource tree.'
    }

    $inputs = @(
        Get-Item -LiteralPath (Join-Path $resolvedRoot 'pyproject.toml')
        Get-Item -LiteralPath (Join-Path $resolvedRoot 'uv.lock')
        Get-Item -LiteralPath (Join-Path $resolvedRoot 'README.md')
    ) + @(Get-ChildItem -LiteralPath (Join-Path $resolvedRoot 'src\pkas') -File -Recurse)
    $sourceParts = @(
        $inputs | Sort-Object FullName | ForEach-Object {
            $relative = $_.FullName.Substring($resolvedRoot.Length).TrimStart('\')
            "$relative|$($_.Length)|$(Get-Sha256 -Path $_.FullName)"
        }
    )
    $sourceText = [string]::Join("`n", $sourceParts)
    $sourceBytes = [System.Text.UTF8Encoding]::new($false).GetBytes($sourceText)
    $sourceHasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        $sourceHash = [BitConverter]::ToString($sourceHasher.ComputeHash($sourceBytes)).Replace('-', '').ToLowerInvariant()
    } finally {
        $sourceHasher.Dispose()
    }

    $manifest = [ordered]@{
        schema_version = 1
        app_version = '0.1.0'
        python_version = [string]$runtimeConfig.python_version
        uv_version = [string]$runtimeConfig.uv.version
        uv_sha256 = Get-Sha256 -Path $uvDestination
        uv_lock_sha256 = Get-Sha256 -Path (Join-Path $resolvedRoot 'uv.lock')
        source_sha256 = $sourceHash
        qdrant_version = [string]$runtimeConfig.qdrant.version
        qdrant_archive_sha256 = [string]$runtimeConfig.qdrant.sha256
        qdrant_sha256 = Get-Sha256 -Path $qdrantDestination
        qdrant_config_sha256 = [string]$replicationConfig.qdrant.sha256
        node_version = [string]$runtimeConfig.node.version
        node_archive_sha256 = [string]$runtimeConfig.node.sha256
        node_sha256 = Get-Sha256 -Path (Join-Path $nodeDestination 'node.exe')
        npm_cli_sha256 = Get-Sha256 -Path (Join-Path $nodeDestination 'node_modules\npm\bin\npm-cli.js')
        everything_version = [string]$runtimeConfig.everything.version
        everything_sha256 = Get-Sha256 -Path $everythingExecutable
        manual_weflow_helper_sha256 = Get-Sha256 -Path (Join-Path $resolvedRoot 'scripts\configure_weflow_manual.mjs')
    }
    $manifestPath = Join-Path $runtimePayload 'desktop-runtime.json'
    $manifestText = ($manifest | ConvertTo-Json -Depth 4) + "`n"
    [System.IO.File]::WriteAllText($manifestPath, $manifestText, [System.Text.UTF8Encoding]::new($false))

    [pscustomobject]@{
        status = 'prepared'
        payload = $runtimePayload
        manifest = $manifestPath
        uv_version = $runtimeConfig.uv.version
        qdrant_version = $runtimeConfig.qdrant.version
        node_version = $runtimeConfig.node.version
        everything_version = $runtimeConfig.everything.version
        source_file_count = $inputs.Count
        uv_archive_sha256 = $runtimeConfig.uv.sha256
        qdrant_archive_sha256 = $runtimeConfig.qdrant.sha256
    } | ConvertTo-Json -Compress
} finally {
    if (Test-Path -LiteralPath $nodeExtractRoot) {
        Remove-Item -LiteralPath $nodeExtractRoot -Recurse -Force
    }
    foreach ($temporaryBinary in @($uvSource, $qdrantSource)) {
        if (Test-Path -LiteralPath $temporaryBinary -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryBinary -Force
        }
    }
}
