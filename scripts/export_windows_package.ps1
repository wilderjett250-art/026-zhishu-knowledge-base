param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$resolvedDestination = [System.IO.Path]::GetFullPath($Destination)
$rootWithSeparator = $resolvedRoot.TrimEnd('\') + '\'
$destinationWithSeparator = $resolvedDestination.TrimEnd('\') + '\'
if ($resolvedDestination.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Destination must not be the source project root.'
}
if (-not $destinationWithSeparator.StartsWith(
    $rootWithSeparator,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    # The export tool supports another drive when the operator invokes it, but
    # this guard prevents broad roots and home directories from being targets.
    $destinationParent = Split-Path -Parent $resolvedDestination
    if ([string]::IsNullOrWhiteSpace($destinationParent)) {
        throw 'Destination must be a specific child directory.'
    }
}
if (Test-Path -LiteralPath $resolvedDestination) {
    $existing = @(Get-ChildItem -LiteralPath $resolvedDestination -Force)
    if ($existing.Count -gt 0) {
        throw 'Destination already exists and is not empty; refusing to overwrite it.'
    }
} else {
    New-Item -ItemType Directory -Path $resolvedDestination -Force | Out-Null
}

$excludedDirectories = @(
    '.git', '.venv', 'data', 'runtime', 'node_modules', 'dist', 'build',
    'tmp', 'reports', '__pycache__', '.cache'
)
$excludedFiles = @(
    '.env', '*.dpapi', '*.sqlite', '*.sqlite-*', '*.db', '*.db-*',
    '*.key', '*.pem', '*.p12', '*.pfx', '*.log', '*.pyc'
)
$arguments = @(
    $resolvedRoot, $resolvedDestination, '/E', '/R:1', '/W:1',
    '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/XD'
) + $excludedDirectories + @('/XF') + $excludedFiles
& robocopy.exe @arguments | Out-Null
$robocopyCode = $LASTEXITCODE
if ($robocopyCode -ge 8) {
    throw "Package copy failed with Robocopy code $robocopyCode."
}

$forbidden = @(
    Get-ChildItem -LiteralPath $resolvedDestination -File -Recurse -Force |
        Where-Object {
            $_.Name -eq '.env' -or
            $_.Extension -in @('.dpapi', '.sqlite', '.db', '.key', '.pem', '.p12', '.pfx')
        }
)
if ($forbidden.Count -gt 0) {
    throw 'Forbidden secret or personal-data file type was found in the package.'
}

$files = @(
    Get-ChildItem -LiteralPath $resolvedDestination -File -Recurse -Force |
        Sort-Object FullName |
        ForEach-Object {
            [pscustomobject]@{
                path = $_.FullName.Substring($resolvedDestination.Length).TrimStart('\')
                bytes = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
)
$manifest = [pscustomobject]@{
    schema_version = 1
    package_kind = 'pkas-windows-source-no-personal-data'
    created_at = (Get-Date).ToUniversalTime().ToString('o')
    source_commit = (& git -C $resolvedRoot rev-parse HEAD 2>$null | Out-String).Trim()
    source_worktree_dirty = (@(& git -C $resolvedRoot status --porcelain 2>$null).Count -gt 0)
    files = $files
    exclusions = [pscustomobject]@{
        personal_data = $true
        databases = $true
        secrets = $true
        virtual_environment = $true
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
    personal_data_copied = $false
    secrets_copied = $false
    database_copied = $false
} | ConvertTo-Json -Compress
