param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = ''
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $DataRoot = Join-Path $resolvedRoot 'data'
}
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$policyPath = Join-Path $resolvedDataRoot 'config\windows_autostart_policy.json'

if (-not (Test-Path -LiteralPath $policyPath -PathType Leaf)) {
    throw "PKAS Windows autostart policy is missing: $policyPath. Autostart is denied by default; new explicit user approval is required before creating an enabled policy."
}

try {
    $policy = Get-Content -LiteralPath $policyPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    throw "PKAS Windows autostart policy is unreadable: $policyPath"
}

if ($policy.schema_version -ne 1) {
    throw "PKAS Windows autostart policy schema is unsupported: $policyPath"
}

if ($policy.autostart_enabled -ne $true) {
    throw "PKAS Windows autostart is disabled by local user policy: $policyPath. New explicit user approval is required before changing this policy."
}
