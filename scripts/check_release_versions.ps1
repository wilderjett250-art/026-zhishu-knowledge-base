[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$releaseRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

function Read-ReleaseText {
    param([Parameter(Mandatory)][string]$Relative)
    $path = Join-Path $releaseRoot $Relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Release metadata is missing: $Relative"
    }
    Get-Content -LiteralPath $path -Raw -Encoding UTF8
}

function Read-SingleVersion {
    param(
        [Parameter(Mandatory)][string]$Relative,
        [Parameter(Mandatory)][string]$Pattern
    )
    $found = [regex]::Matches((Read-ReleaseText -Relative $Relative), $Pattern)
    if ($found.Count -ne 1) { throw "Invalid release metadata: $Relative" }
    $found[0].Groups['version'].Value
}

function Read-ReleaseJson {
    param([Parameter(Mandatory)][string]$Relative)
    $text = Read-ReleaseText -Relative $Relative
    try {
        # npm's root package uses an empty JSON key. Windows PowerShell 5.1
        # cannot represent it as a PSCustomObject, so retain dictionary keys.
        if ($PSVersionTable.PSVersion.Major -ge 6) {
            $value = ConvertFrom-Json -InputObject $text -AsHashtable
        } else {
            Add-Type -AssemblyName System.Web.Extensions
            $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
            $serializer.MaxJsonLength = 4194304
            $value = $serializer.DeserializeObject($text)
        }
        if ($value -isnot [System.Collections.IDictionary]) { throw 'Expected JSON object' }
    } catch {
        throw "Invalid release metadata: $Relative"
    }
    $value
}

$releaseVersion = Read-SingleVersion -Relative 'pyproject.toml' `
    -Pattern '(?m)^version\s*=\s*"(?<version>[^"\r\n]+)"\s*$'
if ($releaseVersion -notmatch '^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$') {
    throw 'Invalid release metadata: pyproject.toml'
}

$versions = [ordered]@{
    'src/pkas/__init__.py' = Read-SingleVersion 'src/pkas/__init__.py' `
        '(?m)^__version__\s*=\s*"(?<version>[^"\r\n]+)"\s*$'
    'src/pkas/config.py' = Read-SingleVersion 'src/pkas/config.py' `
        '(?m)^\s+app_version:\s*str\s*=\s*"(?<version>[^"\r\n]+)"\s*$'
    'uv.lock' = Read-SingleVersion 'uv.lock' `
        '(?ms)^\[\[package\]\]\r?\nname = "pkas"\r?\nversion = "(?<version>[^"\r\n]+)"'
    'desktop/src-tauri/Cargo.toml' = Read-SingleVersion 'desktop/src-tauri/Cargo.toml' `
        '(?ms)^\[package\]\r?\nname = "zhishu-desktop"\r?\nversion = "(?<version>[^"\r\n]+)"'
    'desktop/src-tauri/Cargo.lock' = Read-SingleVersion 'desktop/src-tauri/Cargo.lock' `
        '(?ms)^\[\[package\]\]\r?\nname = "zhishu-desktop"\r?\nversion = "(?<version>[^"\r\n]+)"'
    'desktop/src-tauri/tauri.conf.json' = (Read-ReleaseJson 'desktop/src-tauri/tauri.conf.json')['version']
    'web/package.json' = (Read-ReleaseJson 'web/package.json')['version']
}
$webLock = Read-ReleaseJson 'web/package-lock.json'
$packages = $webLock['packages']
if ($packages -isnot [System.Collections.IDictionary] -or
    $packages[''] -isnot [System.Collections.IDictionary]) {
    throw 'Invalid release metadata: web/package-lock.json:root-package'
}
$versions['web/package-lock.json'] = $webLock['version']
$versions['web/package-lock.json:root-package'] = $packages['']['version']
foreach ($entry in $versions.GetEnumerator()) {
    if ($entry.Value -isnot [string] -or $entry.Value -cne $releaseVersion) {
        throw "Release version mismatch: $($entry.Key)"
    }
}

[pscustomobject]@{
    status = 'release_versions_consistent'
    version = $releaseVersion
    checked_fields = $versions.Count + 1
} | ConvertTo-Json -Compress
