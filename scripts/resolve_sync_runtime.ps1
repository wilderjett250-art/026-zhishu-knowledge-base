# Resolve the runtime belonging to this installation, never a different checkout.
function Resolve-PkasSyncRuntime {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$DataRoot = '',
        [string]$RuntimeRoot = ''
    )
    $ErrorActionPreference = 'Stop'
    $root = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
    $packaged = Test-Path -LiteralPath (Join-Path $root 'runtime\desktop-runtime.json') -PathType Leaf
    if ($packaged) {
        if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
            $pointer = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Zhishu\storage-root.txt'
            if (-not (Test-Path -LiteralPath $pointer -PathType Leaf)) {
                throw 'Open the installed application once to prepare its local runtime.'
            }
            $locations = @(Get-Content -LiteralPath $pointer -Encoding UTF8 |
                ForEach-Object { $_.Trim() } | Where-Object { $_ })
            if ($locations.Count -ne 2) { throw 'Installed storage configuration is incomplete.' }
            $selectedData = [IO.Path]::GetFullPath($locations[1]).TrimEnd('\')
            if (-not [string]::IsNullOrWhiteSpace($DataRoot) -and
                -not ([IO.Path]::GetFullPath($DataRoot).TrimEnd('\')).Equals(
                    $selectedData, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'The requested data root differs from the installed storage configuration.'
            }
            $DataRoot = $selectedData
            $RuntimeRoot = Join-Path ([IO.Path]::GetFullPath($locations[0])) 'runtime'
        } elseif ([string]::IsNullOrWhiteSpace($DataRoot)) {
            throw 'An explicit installed runtime also requires an explicit data root.'
        }
        # A missing removable/data drive is not permission to create an empty KB.
        if (-not (Test-Path -LiteralPath $DataRoot -PathType Container)) {
            throw 'The configured knowledge data drive is unavailable.'
        }
        $python = Join-Path $RuntimeRoot 'python-env\Scripts\python.exe'
        $node = Join-Path $root 'runtime\node\node.exe'
    } else {
        if ([string]::IsNullOrWhiteSpace($DataRoot)) { $DataRoot = Join-Path $root 'data' }
        if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) { $RuntimeRoot = Join-Path $root 'runtime' }
        $python = Join-Path $root '.venv\Scripts\python.exe'
        $node = Join-Path $root 'runtime\tauri-payload\node\node.exe'
        if (-not (Test-Path -LiteralPath $node -PathType Leaf)) {
            $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
            if ($null -ne $nodeCommand) { $node = $nodeCommand.Source }
        }
    }
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'The selected Python runtime is unavailable; repair this installation.'
    }
    [pscustomobject]@{
        ProjectRoot = $root
        DataRoot = [IO.Path]::GetFullPath($DataRoot)
        RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
        Python = [IO.Path]::GetFullPath($python)
        Node = $node
        NpmCli = Join-Path (Split-Path -Parent $node) 'node_modules\npm\bin\npm-cli.js'
        Packaged = $packaged
    }
}
