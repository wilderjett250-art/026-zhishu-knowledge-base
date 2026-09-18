param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$OpenOnStart
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$runRoot = Join-Path $resolvedRoot 'data\runs'
$errorPath = Join-Path $runRoot 'desktop-launch-error.log'
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
if (Test-Path -LiteralPath $errorPath) {
    Remove-Item -LiteralPath $errorPath -Force -ErrorAction SilentlyContinue
}

try {
    & (Join-Path $resolvedRoot 'scripts\pkas_desktop_host.ps1') `
        -ProjectRoot $resolvedRoot -OpenOnStart:$OpenOnStart
} catch {
    @(
        (Get-Date).ToUniversalTime().ToString('o'),
        $_.Exception.GetType().FullName,
        $_.Exception.Message,
        $_.ScriptStackTrace
    ) | Set-Content -LiteralPath $errorPath -Encoding UTF8
    exit 1
}
