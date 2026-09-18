param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PortList = '6333,6334,8765',
    [switch]$SkipPortChecks,
    [double]$MinimumFreeGiB = 2.0
)

$ErrorActionPreference = 'Stop'
$checks = [System.Collections.Generic.List[object]]::new()

function Add-Check {
    param(
        [string]$Name,
        [bool]$Passed,
        [string]$Detail,
        [bool]$Required = $true
    )
    $checks.Add([pscustomobject]@{
        name = $Name
        passed = $Passed
        required = $Required
        detail = $Detail
    })
}

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    Add-Check -Name 'project_root' -Passed $false -Detail 'Project root does not exist.'
    $result = [pscustomobject]@{ status = 'failed'; checks = $checks; errors = 1 }
    $result | ConvertTo-Json -Depth 5 -Compress
    exit 1
}

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
Add-Check -Name 'windows' -Passed ($env:OS -eq 'Windows_NT') -Detail $env:OS
Add-Check -Name 'powershell' -Passed ($PSVersionTable.PSVersion -ge [version]'5.1') `
    -Detail ([string]$PSVersionTable.PSVersion)
Add-Check -Name 'architecture' -Passed ([Environment]::Is64BitOperatingSystem) `
    -Detail $(if ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' })

foreach ($relativePath in @(
    'pyproject.toml',
    'uv.lock',
    'web\package.json',
    'web\package-lock.json',
    'src\pkas\config.py'
)) {
    $present = Test-Path -LiteralPath (Join-Path $resolvedRoot $relativePath) -PathType Leaf
    Add-Check -Name "source:$relativePath" -Passed $present `
        -Detail $(if ($present) { 'present' } else { 'missing' })
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
Add-Check -Name 'uv' -Passed ($null -ne $uv) `
    -Detail $(if ($null -ne $uv) { (& $uv.Source --version) } else { 'missing' })

$node = Get-Command node -ErrorAction SilentlyContinue
$nodeMajor = 0
if ($null -ne $node) {
    $nodeVersion = (& $node.Source --version).TrimStart('v')
    [void][int]::TryParse(($nodeVersion -split '\.')[0], [ref]$nodeMajor)
}
Add-Check -Name 'node' -Passed ($nodeMajor -ge 20) `
    -Detail $(if ($null -ne $node) { "major=$nodeMajor" } else { 'missing' })

$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npm) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
}
Add-Check -Name 'npm' -Passed ($null -ne $npm) `
    -Detail $(if ($null -ne $npm) { 'available' } else { 'missing' })

$driveRoot = [System.IO.Path]::GetPathRoot($resolvedRoot)
$drive = Get-PSDrive -Name $driveRoot.TrimEnd('\').TrimEnd(':') -ErrorAction SilentlyContinue
$freeGiB = if ($null -ne $drive) { [math]::Round($drive.Free / 1GB, 2) } else { 0 }
Add-Check -Name 'disk_free' -Passed ($freeGiB -ge $MinimumFreeGiB) `
    -Detail "$freeGiB GiB"

if (-not $SkipPortChecks) {
    $ports = @(
        $PortList.Split(',') |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -match '^\d+$' } |
            ForEach-Object { [int]$_ } |
            Select-Object -Unique
    )
    if ($ports.Count -eq 0) {
        Add-Check -Name 'ports' -Passed $false -Detail 'No valid ports were supplied.'
    }
    foreach ($port in $ports) {
        $busy = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        Add-Check -Name "port:$port" -Passed ($busy.Count -eq 0) `
            -Detail $(if ($busy.Count -eq 0) { 'available' } else { 'already listening' })
    }
}

$errors = @($checks | Where-Object { $_.required -and -not $_.passed }).Count
[pscustomobject]@{
    status = $(if ($errors -eq 0) { 'passed' } else { 'failed' })
    project_root = $resolvedRoot
    errors = $errors
    checks = $checks
} | ConvertTo-Json -Depth 5 -Compress
exit $(if ($errors -eq 0) { 0 } else { 1 })
