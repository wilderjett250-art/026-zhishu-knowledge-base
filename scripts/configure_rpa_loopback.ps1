[CmdletBinding()]
param(
    [ValidateSet('Status', 'Provision', 'Enable', 'Disable', 'Rotate')]
    [string]$Action = 'Status',
    [switch]$AllowRestrictedContext,
    [switch]$HybridSearch,
    [string]$ProjectRoot = ''
)

$ErrorActionPreference = 'Stop'

function Test-PkasProjectRoot {
    param([Parameter(Mandatory)][string]$Path)

    (Test-Path -LiteralPath (Join-Path $Path 'src\pkas\cli.py') -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Path 'pyproject.toml') -PathType Leaf)
}

function Get-DevelopmentContext {
    param([Parameter(Mandatory)][string]$Root)

    $python = Join-Path $Root '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        return $null
    }
    [pscustomobject]@{
        ProjectRoot = $Root
        DataRoot = Join-Path $Root 'data'
        RuntimeRoot = Join-Path $Root 'runtime'
        Python = $python
        Kind = 'development'
    }
}

function Get-PackagedContext {
    param([Parameter(Mandatory)][string]$Root)

    $localAppData = [Environment]::GetFolderPath('LocalApplicationData')
    $pointer = Join-Path $localAppData 'Zhishu\storage-root.txt'
    if (-not (Test-Path -LiteralPath $pointer -PathType Leaf)) {
        throw '尚未找到知域的数据位置。请先正常打开一次新版知域，完成本地运行环境准备后再配置 RPA。'
    }
    $locations = @(
        Get-Content -LiteralPath $pointer -Encoding UTF8 |
            ForEach-Object { $_.Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($locations.Count -lt 2) {
        throw '知域的数据位置配置不完整；为避免连到空知识库，已停止。请先从新版知域正常启动一次。'
    }
    $appHome = [System.IO.Path]::GetFullPath($locations[0])
    $dataRoot = [System.IO.Path]::GetFullPath($locations[1])
    $python = Join-Path $appHome 'runtime\python-env\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw '知域的本地 Python 环境尚未准备完成。请先正常打开新版知域一次，等待本地服务可用后再运行此脚本。'
    }
    if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) {
        throw '知域原有数据盘当前不可用；为避免创建空知识库，已停止。请连接原数据盘后重试。'
    }
    [pscustomobject]@{
        ProjectRoot = $Root
        DataRoot = $dataRoot
        RuntimeRoot = Join-Path $appHome 'runtime'
        Python = $python
        Kind = 'packaged'
    }
}

function Resolve-RpaContext {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($ProjectRoot)) {
        $candidates += [System.IO.Path]::GetFullPath($ProjectRoot)
    }
    $candidates += [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $installedRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) '知域\pkas-app'
    $candidates += $installedRoot

    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (-not (Test-PkasProjectRoot -Path $candidate)) {
            continue
        }
        if (Test-Path -LiteralPath (Join-Path $candidate 'runtime\uv.exe') -PathType Leaf) {
            return (Get-PackagedContext -Root $candidate)
        }
        $development = Get-DevelopmentContext -Root $candidate
        if ($null -ne $development) {
            return $development
        }
    }
    throw '未找到可用的知域运行环境。请先安装新版知域，或在源码目录中运行此脚本并传入 -ProjectRoot。'
}

if (($AllowRestrictedContext -or $HybridSearch) -and $Action -notin @('Provision', 'Enable')) {
    throw '仅在 Provision 或 Enable 时允许设置受限资料和混合检索；默认始终使用普通文件全文检索。'
}

$context = Resolve-RpaContext
$savedEnvironment = @{}
foreach ($name in @('PKAS_PROJECT_ROOT', 'PKAS_DATA_ROOT', 'PKAS_RUNTIME_ROOT', 'PKAS_ENV_FILE', 'PYTHONPATH')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

try {
    [Environment]::SetEnvironmentVariable('PKAS_PROJECT_ROOT', $context.ProjectRoot, 'Process')
    [Environment]::SetEnvironmentVariable('PKAS_DATA_ROOT', $context.DataRoot, 'Process')
    [Environment]::SetEnvironmentVariable('PKAS_RUNTIME_ROOT', $context.RuntimeRoot, 'Process')
    [Environment]::SetEnvironmentVariable('PKAS_ENV_FILE', (Join-Path $context.DataRoot 'config\.env'), 'Process')
    [Environment]::SetEnvironmentVariable('PYTHONPATH', (Join-Path $context.ProjectRoot 'src'), 'Process')

    $cliArguments = @(switch ($Action) {
        'Status' { @('rpa-bridge-status') }
        'Provision' { @('rpa-bridge-provision', '--yes') }
        'Enable' { @('rpa-bridge-enable', '--yes') }
        'Disable' { @('rpa-bridge-disable', '--yes') }
        'Rotate' { @('rpa-bridge-rotate-token', '--yes') }
    })
    if ($AllowRestrictedContext) {
        $cliArguments += '--allow-restricted-context'
    }
    if ($HybridSearch) {
        $cliArguments += '--hybrid-search'
    }

    if ($Action -in @('Provision', 'Rotate')) {
        Write-Host '令牌将只在这一次命令输出中显示。请只粘贴到实在 Agent 的凭据字段，不要保存到工作流正文、截图或聊天记录。' -ForegroundColor Yellow
    }
    & $context.Python -m pkas.cli @cliArguments
    if ($LASTEXITCODE -ne 0) {
        throw "RPA 本机桥操作失败，退出码：$LASTEXITCODE"
    }
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
}
