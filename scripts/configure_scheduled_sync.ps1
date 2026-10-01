[CmdletBinding()]
param(
    [ValidateSet('Status', 'EnableLocal', 'Disable')][string]$Action = 'Status',
    [switch]$Approve,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = '',
    [string]$RuntimeRoot = '',
    [ValidatePattern('^[A-Za-z0-9_-]{1,100}$')][string]$TaskName = 'PKAS-Knowledge-Sync'
)

$ErrorActionPreference = 'Stop'
if ($Action -ne 'Status' -and -not $Approve) {
    throw 'Explicit approval required: pass -Approve to change the current user scheduled task.'
}
. (Join-Path $PSScriptRoot 'resolve_sync_runtime.ps1')
$context = Resolve-PkasSyncRuntime -ProjectRoot $ProjectRoot -DataRoot $DataRoot -RuntimeRoot $RuntimeRoot
$runner = Join-Path $context.ProjectRoot 'scripts\run_scheduled_sync.ps1'
$taskNames = @($TaskName, ($TaskName + '-CatchUp'))
$ownedTasks = @()
foreach ($name in $taskNames) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) { continue }
    if (@($task.Actions).Count -ne 1 -or
        -not ([string]$task.Actions[0].WorkingDirectory).TrimEnd('\').Equals(
            $context.ProjectRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) -or
        ([string]$task.Actions[0].Arguments).IndexOf('"' + $runner + '"', [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw 'Task name belongs to another installation; no settings were changed.'
    }
    if ($Action -ne 'Status' -and [string]$task.State -eq 'Running') {
        throw 'A sync task is running; wait until it finishes before changing the schedule.'
    }
    $ownedTasks += $task
}
if ($Action -eq 'Status') {
    [pscustomobject]@{
        status = $(if (@($ownedTasks | Where-Object { $_.TaskName -eq $TaskName }).Count) { 'configured' } else { 'not_configured' })
        packaged = $context.Packaged
        tasks = @($ownedTasks | ForEach-Object { @{ name = $_.TaskName; state = [string]$_.State } })
        default_daily_at = '00:00'
        default_boot_delay_minutes = 60
        imports_only_registered_sources = $true
        weflow_export_included = $false
    } | ConvertTo-Json -Depth 4 -Compress
    exit 0
}

function Write-AtomicSyncConfig {
    param([string]$Path, [byte[]]$Bytes)
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllBytes($temporary, $Bytes)
    try {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [IO.File]::Replace($temporary, $Path, [System.Management.Automation.Language.NullString]::Value)
        } else { [IO.File]::Move($temporary, $Path) }
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary -Force }
    }
}

# One small, independently restorable settings checkpoint. No database copy,
# exported messages, credentials, or new recovery directory per invocation.
$configRoot = Join-Path $context.DataRoot 'config'
New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
$policyPath = Join-Path $configRoot 'windows_autostart_policy.json'
$previousPolicy = $null
if (Test-Path -LiteralPath $policyPath -PathType Leaf) {
    if ((Get-Item -LiteralPath $policyPath).Length -gt 64KB) { throw 'Autostart policy is too large.' }
    $previousPolicy = [IO.File]::ReadAllBytes($policyPath)
    $policy = [Text.Encoding]::UTF8.GetString($previousPolicy).TrimStart([char]0xFEFF) | ConvertFrom-Json
    if ($policy.schema_version -ne 1 -or $policy.autostart_enabled -isnot [bool]) {
        throw 'Unknown autostart policy; repair it explicitly before proceeding.'
    }
} else { $policy = [pscustomobject]@{ schema_version = 1; autostart_enabled = $false } }
$taskSnapshots = @($ownedTasks | ForEach-Object {
    @{ name = $_.TaskName; xml = [string](Export-ScheduledTask -TaskName $_.TaskName) }
})
$recovery = [ordered]@{
    schema_version = 1
    scope = 'scheduled-sync-settings'
    project_root = $context.ProjectRoot
    policy_path = $policyPath
    policy_existed = $null -ne $previousPolicy
    policy_base64 = $(if ($null -ne $previousPolicy) { [Convert]::ToBase64String($previousPolicy) } else { $null })
    tasks = $taskSnapshots
    absent_tasks = @($taskNames | Where-Object { $_ -notin @($ownedTasks.TaskName) })
    recorded_at = [DateTime]::UtcNow.ToString('o')
}
$recoveryPath = Join-Path $configRoot 'sync-task-recovery.json'
if (Test-Path -LiteralPath $recoveryPath -PathType Leaf) {
    if ((Get-Item -LiteralPath $recoveryPath).Length -gt 1MB) { throw 'Unexpected recovery file; preserve it.' }
    $old = Get-Content -LiteralPath $recoveryPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($old.schema_version -ne 1 -or $old.scope -ne 'scheduled-sync-settings' -or
        $old.project_root -ne $context.ProjectRoot) { throw 'Recovery file belongs to another scope; preserve it.' }
}
$bytes = [Text.UTF8Encoding]::new($false).GetBytes(($recovery | ConvertTo-Json -Depth 6))
Write-AtomicSyncConfig -Path $recoveryPath -Bytes $bytes
$verified = [IO.File]::ReadAllBytes($recoveryPath)
if ([Convert]::ToBase64String($verified) -ne [Convert]::ToBase64String($bytes)) { throw 'Settings recovery verification failed.' }
try {
    if ($Action -eq 'EnableLocal') {
        $policy.autostart_enabled = $true
        $policyBytes = [Text.UTF8Encoding]::new($false).GetBytes(($policy | ConvertTo-Json -Depth 6))
        Write-AtomicSyncConfig -Path $policyPath -Bytes $policyBytes
        & (Join-Path $PSScriptRoot 'install_scheduled_sync.ps1') `
            -ProjectRoot $context.ProjectRoot -DataRoot $context.DataRoot `
            -RuntimeRoot $context.RuntimeRoot -LocalOnly -TaskName $TaskName
    } else {
        & (Join-Path $PSScriptRoot 'uninstall_scheduled_sync.ps1') -ProjectRoot $context.ProjectRoot -TaskName $TaskName
    }
} catch {
    # Restore exactly the prior policy and task definitions, not a guessed one.
    if ($null -ne $previousPolicy) { Write-AtomicSyncConfig -Path $policyPath -Bytes $previousPolicy }
    elseif (Test-Path -LiteralPath $policyPath -PathType Leaf) { Remove-Item -LiteralPath $policyPath -Force }
    foreach ($snapshot in $taskSnapshots) {
        Register-ScheduledTask -TaskName $snapshot.name -Xml $snapshot.xml -Force | Out-Null
    }
    foreach ($name in $recovery.absent_tasks) {
        $created = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($null -ne $created -and @($created.Actions).Count -eq 1 -and
            ([string]$created.Actions[0].WorkingDirectory).TrimEnd('\') -eq $context.ProjectRoot -and
            ([string]$created.Actions[0].Arguments).Contains('"' + $runner + '"')) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
        }
    }
    throw
}
