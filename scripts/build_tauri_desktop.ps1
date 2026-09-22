[CmdletBinding()]
param(
    [ValidateSet("Check", "Debug", "Release", "Installer")]
    [string]$Mode = "Debug",
    [string]$BuildRoot = '',
    [string]$StagingRoot = ''
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$rustupHome = Join-Path $projectRoot "runtime\rustup"
$cargoHome = Join-Path $projectRoot "runtime\cargo"
$cargoExe = Join-Path $cargoHome "bin\cargo.exe"
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath $cargoExe -PathType Leaf)) {
    throw "Project-local Rust toolchain was not found: $cargoExe"
}
if ([string]::IsNullOrWhiteSpace($BuildRoot)) {
    if (Test-Path -LiteralPath 'I:\') {
        $BuildRoot = 'I:\PKAS-builds\zhishu-desktop'
    } elseif (Test-Path -LiteralPath 'G:\') {
        $BuildRoot = 'G:\PKAS-builds\zhishu-desktop'
    } else {
        throw 'Provide -BuildRoot on a non-system data drive; desktop builds must not use C:.'
    }
}
$resolvedBuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
if ($resolvedBuildRoot.StartsWith('C:\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'BuildRoot must not be on C:.'
}
New-Item -ItemType Directory -Path $resolvedBuildRoot -Force | Out-Null

$targetDir = Join-Path $resolvedBuildRoot 'tauri-target'
$manifestPath = Join-Path $projectRoot "desktop\src-tauri\Cargo.toml"
$env:RUSTUP_HOME = $rustupHome
$env:CARGO_HOME = $cargoHome
$env:RUSTUP_TOOLCHAIN = 'stable-x86_64-pc-windows-msvc'
$env:PATH = "$(Join-Path $cargoHome 'bin');$env:PATH"
$env:CARGO_TARGET_DIR = $targetDir
$env:CARGO_INCREMENTAL = '0'

function Remove-StalePackagedResourceTree {
    param([Parameter(Mandatory)][string]$TargetDirectory)

    # Tauri reuses CARGO_TARGET_DIR. Its resource-copy phase does not remove
    # files that disappeared from a resource source tree, so a prior build can
    # otherwise leak stale bytecode into a later installer. This exact path is
    # generated build output below the caller-selected build root, never user
    # knowledge data or project source.
    $targetFullPath = [System.IO.Path]::GetFullPath($TargetDirectory).TrimEnd('\')
    $resourceRoot = Join-Path $targetFullPath 'release\pkas-app'
    $resourceFullPath = [System.IO.Path]::GetFullPath($resourceRoot)
    if (-not $resourceFullPath.StartsWith($targetFullPath + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a packaged resource path outside the build target: $resourceFullPath"
    }
    if (Test-Path -LiteralPath $resourceFullPath) {
        Remove-Item -LiteralPath $resourceFullPath -Recurse -Force
    }
}

switch ($Mode) {
    "Check" {
        & $cargoExe check --locked --manifest-path $manifestPath
        if ($LASTEXITCODE -ne 0) { throw "Tauri desktop check failed with exit code $LASTEXITCODE" }
    }
    "Debug" {
        & $cargoExe build --locked --manifest-path $manifestPath
        if ($LASTEXITCODE -ne 0) { throw "Tauri desktop build failed with exit code $LASTEXITCODE" }
    }
    "Release" {
        & $cargoExe build --locked --release --manifest-path $manifestPath
        if ($LASTEXITCODE -ne 0) { throw "Tauri desktop release build failed with exit code $LASTEXITCODE" }
    }
    "Installer" {
        if ($null -eq $npm) {
            throw 'The builder needs Node.js/npm; customers installing the generated setup.exe do not.'
        }
        $uvExe = Join-Path $projectRoot 'runtime\tauri-payload\uv.exe'
        if (-not (Test-Path -LiteralPath $uvExe -PathType Leaf)) {
            throw 'The builder needs the pinned uv.exe to verify uv.lock before packaging.'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web\node_modules\.bin\vite.cmd'))) {
            throw 'Web build dependencies are missing. Run npm ci in web once on the developer machine, then retry.'
        }
        if ([string]::IsNullOrWhiteSpace($StagingRoot)) {
            $StagingRoot = Join-Path $resolvedBuildRoot 'staging'
        }
        New-Item -ItemType Directory -Path $StagingRoot -Force | Out-Null
        $env:npm_config_cache = Join-Path $resolvedBuildRoot 'npm-cache'

        Push-Location $projectRoot
        try {
            & $uvExe lock --locked
            if ($LASTEXITCODE -ne 0) {
                throw 'uv.lock does not match pyproject.toml; update and review the lock before packaging.'
            }
        } finally {
            Pop-Location
        }

        Push-Location (Join-Path $projectRoot 'web')
        try {
            & $npm.Source run build
            if ($LASTEXITCODE -ne 0) { throw "Web production build failed with exit code $LASTEXITCODE" }
        } finally {
            Pop-Location
        }

        & (Join-Path $PSScriptRoot 'prepare_tauri_resources.ps1') `
            -ProjectRoot $projectRoot -StagingRoot $StagingRoot
        if ($LASTEXITCODE -ne 0) { throw 'Pinned installer resources could not be prepared.' }

        Remove-StalePackagedResourceTree -TargetDirectory $targetDir

        Push-Location (Join-Path $projectRoot 'desktop')
        try {
            & $npm.Source exec --yes --package='@tauri-apps/cli@2.11.4' -- tauri build --bundles nsis
            if ($LASTEXITCODE -ne 0) { throw "NSIS installer build failed with exit code $LASTEXITCODE" }
        } finally {
            Pop-Location
        }

        $installerDirectory = Join-Path $targetDir 'release\bundle\nsis'
        $tauriConfig = Get-Content -LiteralPath (Join-Path $projectRoot 'desktop\src-tauri\tauri.conf.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $installerPattern = "*_$($tauriConfig.version)_*-setup.exe"
        $installers = @(Get-ChildItem -LiteralPath $installerDirectory -Filter $installerPattern -File -ErrorAction SilentlyContinue)
        if ($installers.Count -ne 1) {
            throw "Expected one current-version NSIS setup.exe ($installerPattern) in $installerDirectory; found $($installers.Count)."
        }
        $installer = $installers[0]
        [pscustomobject]@{
            status = 'installer_built'
            installer = $installer.FullName
            bytes = $installer.Length
            sha256 = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            installer_requires_admin = $false
            customer_requires_node_or_uv = $false
            customer_first_run_requires_internet = $true
        } | ConvertTo-Json -Compress
    }
}
