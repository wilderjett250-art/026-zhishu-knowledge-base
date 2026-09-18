param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$DashboardPort = 8765,
    [int]$QdrantPort = 6333,
    [switch]$OpenOnStart
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$dataRoot = Join-Path $resolvedRoot 'data'
$runRoot = Join-Path $dataRoot 'runs'
$pythonw = Join-Path $resolvedRoot '.venv\Scripts\pythonw.exe'
$qdrant = Join-Path $resolvedRoot 'runtime\qdrant\qdrant.exe'
$qdrantStarter = Join-Path $PSScriptRoot 'start_qdrant.ps1'
$statePath = Join-Path $runRoot 'desktop-host.json'
$dashboardLog = Join-Path $runRoot 'desktop-dashboard.log'
$dashboardErrorLog = Join-Path $runRoot 'desktop-dashboard-error.log'
$coreLog = Join-Path $runRoot 'desktop-core.log'
$coreErrorLog = Join-Path $runRoot 'desktop-core-error.log'
$dashboardUrl = "http://127.0.0.1:$DashboardPort/"

if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "PKAS Python windowless runtime is unavailable: $pythonw"
}
if (-not (Test-Path -LiteralPath $qdrant -PathType Leaf)) {
    throw "PKAS Qdrant runtime is unavailable: $qdrant"
}

New-Item -ItemType Directory -Path $runRoot -Force | Out-Null

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\PKASDesktopHost', [ref]$createdNew)
if (-not $createdNew) {
    Start-Process $dashboardUrl
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$managed = @{}
$exiting = $false

function Test-HttpReady {
    param([string]$Uri, [int]$TimeoutSeconds = 1)
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec $TimeoutSeconds
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Test-PortListening {
    param([int]$Port)
    return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue).Count -gt 0
}

function Save-HostState {
    $state = [ordered]@{
        schema_version = 1
        host_pid = $PID
        project_root = $resolvedRoot
        dashboard_port = $DashboardPort
        qdrant_port = $QdrantPort
        managed = @($managed.GetEnumerator() | ForEach-Object {
            [ordered]@{ name = $_.Key; pid = $_.Value.Id }
        })
        started_at = (Get-Date).ToUniversalTime().ToString('o')
    }
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
}

function Start-WindowlessPython {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [string]$StandardOutput,
        [string]$StandardError
    )
    $process = Start-Process -FilePath $pythonw `
        -ArgumentList $Arguments `
        -WorkingDirectory $resolvedRoot `
        -RedirectStandardOutput $StandardOutput `
        -RedirectStandardError $StandardError `
        -WindowStyle Hidden -PassThru
    $managed[$Name] = $process
    return $process
}

function Start-DesktopServices {
    $env:PKAS_PROJECT_ROOT = $resolvedRoot
    $env:PKAS_DATA_ROOT = $dataRoot
    $env:PKAS_QDRANT_URL = "http://127.0.0.1:$QdrantPort"

    if (-not (Test-HttpReady -Uri "http://127.0.0.1:$QdrantPort/")) {
        $null = & $qdrantStarter -ProjectRoot $resolvedRoot -DataRoot $dataRoot `
            -HttpPort $QdrantPort -GrpcPort ($QdrantPort + 1)
    }

    if (-not (Test-HttpReady -Uri "${dashboardUrl}api/health")) {
        $null = Start-WindowlessPython -Name 'dashboard' `
            -Arguments @('-m', 'pkas.cli', 'serve', '--host', '127.0.0.1', '--port', "$DashboardPort") `
            -StandardOutput $dashboardLog -StandardError $dashboardErrorLog
    }

    $coreRunning = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*$resolvedRoot*" -and $_.CommandLine -like '*pkas.core_worker*'
    }).Count -gt 0
    if (-not $coreRunning) {
        $null = Start-WindowlessPython -Name 'core' `
            -Arguments @('-m', 'pkas.core_worker', '--interval-seconds', '300') `
            -StandardOutput $coreLog -StandardError $coreErrorLog
    }
    Save-HostState
}

function Wait-Dashboard {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        if (Test-HttpReady -Uri "${dashboardUrl}api/health") { return $true }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    return $false
}

function Open-ControlCenter {
    if (-not (Wait-Dashboard)) {
        [System.Windows.Forms.MessageBox]::Show(
            '知枢后台没有在30秒内就绪，请从托盘查看状态。',
            '知枢',
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        ) | Out-Null
        return
    }
    $edgeCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    )
    $edge = $edgeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if ($edge) {
        Start-Process -FilePath $edge -ArgumentList @("--app=$dashboardUrl", '--start-maximized') | Out-Null
    } else {
        Start-Process $dashboardUrl | Out-Null
    }
}

function Get-ServiceSummary {
    $dashboardReady = Test-HttpReady -Uri "${dashboardUrl}api/health"
    $qdrantReady = Test-HttpReady -Uri "http://127.0.0.1:$QdrantPort/"
    $coreReady = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*$resolvedRoot*" -and $_.CommandLine -like '*pkas.core_worker*'
    }).Count -gt 0
    return [ordered]@{ dashboard = $dashboardReady; qdrant = $qdrantReady; core = $coreReady }
}

function Stop-ExactManagedProcess {
    param([int]$ProcessId, [string]$ExpectedFragment)
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $candidate) { return }
    if ($candidate.CommandLine -notlike "*$resolvedRoot*" -or $candidate.CommandLine -notlike "*$ExpectedFragment*") {
        return
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Stop-DesktopServices {
    foreach ($entry in @($managed.GetEnumerator())) {
        $fragment = if ($entry.Key -eq 'dashboard') { 'pkas.cli' } else { 'pkas.core_worker' }
        Stop-ExactManagedProcess -ProcessId $entry.Value.Id -ExpectedFragment $fragment
    }
    $qdrantPidPath = Join-Path $runRoot 'qdrant.pid.json'
    if (Test-Path -LiteralPath $qdrantPidPath) {
        try {
            $qdrantState = Get-Content -LiteralPath $qdrantPidPath -Raw | ConvertFrom-Json
            Stop-ExactManagedProcess -ProcessId ([int]$qdrantState.pid) -ExpectedFragment 'qdrant.exe'
        } catch { }
    }
    $managed.Clear()
    if (Test-Path -LiteralPath $statePath) {
        Remove-Item -LiteralPath $statePath -Force -ErrorAction SilentlyContinue
    }
}

function New-BrandIcon {
    $bitmap = New-Object System.Drawing.Bitmap 32, 32
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.Clear([System.Drawing.Color]::Transparent)
    $cyan = [System.Drawing.Color]::FromArgb(81, 214, 208)
    $navy = [System.Drawing.Color]::FromArgb(13, 23, 31)
    $points = [System.Drawing.Point[]]@(
        (New-Object System.Drawing.Point 16, 2),
        (New-Object System.Drawing.Point 30, 16),
        (New-Object System.Drawing.Point 16, 30),
        (New-Object System.Drawing.Point 2, 16)
    )
    $graphics.FillPolygon((New-Object System.Drawing.SolidBrush $navy), $points)
    $graphics.DrawPolygon((New-Object System.Drawing.Pen $cyan, 2), $points)
    $graphics.FillRectangle((New-Object System.Drawing.SolidBrush $cyan), 12, 12, 8, 8)
    $graphics.Dispose()
    return [System.Drawing.Icon]::FromHandle($bitmap.GetHicon())
}

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = New-BrandIcon
$notify.Text = '知枢 · 正在启动'
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$statusItem = New-Object System.Windows.Forms.ToolStripMenuItem('正在检查服务…')
$statusItem.Enabled = $false
$openItem = New-Object System.Windows.Forms.ToolStripMenuItem('打开控制中心')
$restartItem = New-Object System.Windows.Forms.ToolStripMenuItem('重新启动后台')
$exitItem = New-Object System.Windows.Forms.ToolStripMenuItem('退出知枢')
$menu.Items.Add($statusItem) | Out-Null
$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
$menu.Items.Add($openItem) | Out-Null
$menu.Items.Add($restartItem) | Out-Null
$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
$menu.Items.Add($exitItem) | Out-Null
$notify.ContextMenuStrip = $menu

$openAction = { Open-ControlCenter }
$openItem.Add_Click($openAction)
$notify.Add_DoubleClick($openAction)
$restartItem.Add_Click({
    Stop-DesktopServices
    Start-DesktopServices
})
$exitItem.Add_Click({
    $script:exiting = $true
    $notify.Visible = $false
    Stop-DesktopServices
    [System.Windows.Forms.Application]::Exit()
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.Add_Tick({
    $summary = Get-ServiceSummary
    if ($summary.dashboard -and $summary.qdrant -and $summary.core) {
        $statusItem.Text = '运行正常 · 知识与向量已就绪'
        $notify.Text = '知枢 · 运行正常'
    } else {
        $statusItem.Text = "需要检查 · 控制台:$($summary.dashboard) 向量:$($summary.qdrant) 后台:$($summary.core)"
        $notify.Text = '知枢 · 需要检查'
    }
})

try {
    Start-DesktopServices
    $timer.Start()
    if ($OpenOnStart) { Open-ControlCenter }
    [System.Windows.Forms.Application]::Run()
} finally {
    $timer.Stop()
    $notify.Visible = $false
    if (-not $exiting) { Stop-DesktopServices }
    $notify.Dispose()
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
