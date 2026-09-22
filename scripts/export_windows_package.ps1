param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$AllowDirty
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$resolvedDestination = [System.IO.Path]::GetFullPath($Destination)
$rootWithSeparator = $resolvedRoot.TrimEnd('\') + '\'
$destinationWithSeparator = $resolvedDestination.TrimEnd('\') + '\'

if ($resolvedDestination.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Destination must not be the source project root.'
}
$destinationParent = Split-Path -Parent $resolvedDestination
if ([string]::IsNullOrWhiteSpace($destinationParent)) {
    throw 'Destination must be a specific child directory.'
}
if (Test-Path -LiteralPath $resolvedDestination) {
    $existing = @(Get-ChildItem -LiteralPath $resolvedDestination -Force)
    if ($existing.Count -gt 0) {
        throw 'Destination already exists and is not empty; refusing to overwrite it.'
    }
}

$dirtyStatus = @(& git -C $resolvedRoot status --porcelain 2>$null)
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to inspect the source Git worktree.'
}
if (-not $AllowDirty -and $dirtyStatus.Count -gt 0) {
    throw 'Source worktree is dirty. Commit or stash changes before exporting, or pass -AllowDirty explicitly.'
}

$trackedFiles = @(& git -C $resolvedRoot ls-files 2>$null)
if ($LASTEXITCODE -ne 0 -or $trackedFiles.Count -eq 0) {
    throw 'Source export requires a non-empty Git-tracked project.'
}

# A developer package is deliberately made from tracked source only. This is
# stricter than copying a directory with exclusions: local handoffs, caches,
# compiler outputs, data, logs and untracked notes cannot leak by accident.
$excludedDirectoryNames = @(
    '.git', '.venv', 'data', 'runtime', 'node_modules', 'dist', 'build',
    'tmp', 'reports', '__pycache__', '.cache', '.pytest_cache', '.ruff_cache',
    '.mypy_cache', '.pyright', 'target', 'coverage', 'secrets', 'credentials'
)
$excludedFilePatterns = @(
    '.env', '*.dpapi', '*.sqlite', '*.sqlite-*', '*.db', '*.db-*',
    '*.key', '*.pem', '*.p12', '*.pfx', '*.log', '*.pyc', '*.tsbuildinfo',
    'HANDOFF.md', 'HANDOFF-*.md', '.qdrant-initialized', 'storage-root.txt',
    'pkas-root.txt', 'weflow-export-records.json'
)

function Test-ExportableRelativePath {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    $normalized = $RelativePath.Replace('/', '\').TrimStart('\')
    if ([string]::IsNullOrWhiteSpace($normalized) -or
        $normalized.StartsWith('..\', [System.StringComparison]::Ordinal) -or
        $normalized -eq '..') {
        return $false
    }
    $segments = @($normalized.Split('\') | Where-Object { $_ })
    if ($segments.Count -eq 0) {
        return $false
    }
    foreach ($segment in $segments) {
        if ($excludedDirectoryNames -contains $segment) {
            return $false
        }
    }
    $leaf = $segments[-1]
    foreach ($pattern in $excludedFilePatterns) {
        if ($leaf -like $pattern) {
            return $false
        }
    }
    return $true
}

function Get-SourcePackageSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [System.IO.File]::OpenRead($Path)
    $hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    } finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
}

$selectedFiles = @()
foreach ($tracked in $trackedFiles) {
    if (-not (Test-ExportableRelativePath -RelativePath $tracked)) {
        continue
    }
    $relativePath = $tracked.Replace('/', '\').TrimStart('\')
    $sourcePath = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot $relativePath))
    if (-not $sourcePath.StartsWith($rootWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Tracked path resolves outside the source root: $tracked"
    }
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Tracked source file is missing or not a file: $tracked"
    }
    $destinationPath = [System.IO.Path]::GetFullPath((Join-Path $resolvedDestination $relativePath))
    if (-not $destinationPath.StartsWith($destinationWithSeparator, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Destination path resolves outside the package: $tracked"
    }
    $selectedFiles += [pscustomobject]@{
        relative_path = $relativePath
        source_path = $sourcePath
        destination_path = $destinationPath
    }
}
if ($selectedFiles.Count -eq 0) {
    throw 'No exportable tracked source files remain after safety exclusions.'
}

New-Item -ItemType Directory -Path $resolvedDestination -Force | Out-Null
foreach ($item in $selectedFiles) {
    $parent = Split-Path -Parent $item.destination_path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Copy-Item -LiteralPath $item.source_path -Destination $item.destination_path -Force
}

$expected = @{}
foreach ($item in $selectedFiles) {
    $expected[$item.relative_path] = $true
}
$actual = @(
    Get-ChildItem -LiteralPath $resolvedDestination -File -Recurse -Force |
        ForEach-Object { $_.FullName.Substring($resolvedDestination.Length).TrimStart('\') }
)
$unexpected = @($actual | Where-Object { -not $expected.ContainsKey($_) })
$missing = @($expected.Keys | Where-Object { $_ -notin $actual })
if ($unexpected.Count -gt 0 -or $missing.Count -gt 0) {
    throw 'Exported package contents do not match the reviewed Git-tracked source set.'
}

$forbidden = @(
    Get-ChildItem -LiteralPath $resolvedDestination -File -Recurse -Force |
        Where-Object {
            $_.Name -eq '.env' -or
            $_.Name -in @('HANDOFF.md', 'storage-root.txt', 'pkas-root.txt', 'weflow-export-records.json') -or
            $_.Extension -in @('.dpapi', '.sqlite', '.db', '.key', '.pem', '.p12', '.pfx')
        }
)
if ($forbidden.Count -gt 0) {
    throw 'Forbidden secret, handoff or personal-data file type was found in the package.'
}

$files = @(
    Get-ChildItem -LiteralPath $resolvedDestination -File -Recurse -Force |
        Sort-Object FullName |
        ForEach-Object {
            [pscustomobject]@{
                path = $_.FullName.Substring($resolvedDestination.Length).TrimStart('\')
                bytes = $_.Length
                sha256 = Get-SourcePackageSha256 -Path $_.FullName
            }
        }
)
$sourceCommit = (& git -C $resolvedRoot rev-parse HEAD 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($sourceCommit)) {
    throw 'Unable to resolve the source Git commit.'
}
$manifest = [pscustomobject]@{
    schema_version = 2
    package_kind = 'pkas-windows-source-no-personal-data'
    created_at = (Get-Date).ToUniversalTime().ToString('o')
    source_commit = $sourceCommit
    source_worktree_dirty = ($dirtyStatus.Count -gt 0)
    files = $files
    exclusions = [pscustomobject]@{
        git_tracked_source_only = $true
        personal_data = $true
        handoffs_and_local_notes = $true
        databases = $true
        secrets = $true
        virtual_environment = $true
        compiler_and_test_caches = $true
        qdrant_binary_and_storage = $true
        web_dependencies_and_build = $true
    }
}
$manifestPath = Join-Path $resolvedDestination 'PACKAGE_MANIFEST.json'
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

[pscustomobject]@{
    status = 'completed'
    destination = $resolvedDestination
    file_count = $files.Count
    manifest = $manifestPath
    git_tracked_source_only = $true
    personal_data_copied = $false
    secrets_copied = $false
    database_copied = $false
} | ConvertTo-Json -Compress
