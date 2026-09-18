[CmdletBinding()]
param(
    [ValidateSet("Check", "Debug", "Release")]
    [string]$Mode = "Debug"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$rustupHome = Join-Path $projectRoot "runtime\rustup"
$cargoHome = Join-Path $projectRoot "runtime\cargo"
$cargoExe = Join-Path $cargoHome "bin\cargo.exe"
$manifestPath = Join-Path $projectRoot "desktop\src-tauri\Cargo.toml"

if (-not (Test-Path -LiteralPath $cargoExe)) {
    throw "Project-local Rust toolchain was not found: $cargoExe"
}

$env:RUSTUP_HOME = $rustupHome
$env:CARGO_HOME = $cargoHome
$env:PATH = "$(Join-Path $cargoHome 'bin');$env:PATH"
$env:CARGO_TARGET_DIR = Join-Path $projectRoot "desktop\src-tauri\target"

switch ($Mode) {
    "Check" {
        & $cargoExe check --locked --manifest-path $manifestPath
    }
    "Debug" {
        & $cargoExe build --locked --manifest-path $manifestPath
    }
    "Release" {
        & $cargoExe build --locked --release --manifest-path $manifestPath
    }
}

if ($LASTEXITCODE -ne 0) {
    throw "Tauri desktop build failed with exit code $LASTEXITCODE"
}
