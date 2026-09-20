[CmdletBinding()]
param(
    [switch] $SelfTest
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms

$projectRoot = Split-Path -Parent $PSScriptRoot
$selfTestEnvPath = $null
if ($SelfTest) {
    $selfTestEnvPath = [System.IO.Path]::GetTempFileName()
    $envPath = $selfTestEnvPath
}
else {
    $envPath = Join-Path $projectRoot ".env"
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "PKAS - DeepSeek Setup"
$form.StartPosition = "CenterScreen"
$form.ClientSize = New-Object System.Drawing.Size(560, 190)
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true

$label = New-Object System.Windows.Forms.Label
$label.AutoSize = $true
$label.Location = New-Object System.Drawing.Point(24, 22)
$label.Text = "Paste the DeepSeek API key. It will not be displayed or written to Codex logs."
$form.Controls.Add($label)

$keyTextBox = New-Object System.Windows.Forms.TextBox
$keyTextBox.Location = New-Object System.Drawing.Point(27, 58)
$keyTextBox.Size = New-Object System.Drawing.Size(505, 28)
$keyTextBox.UseSystemPasswordChar = $true
$form.Controls.Add($keyTextBox)

$status = New-Object System.Windows.Forms.Label
$status.AutoSize = $true
$status.ForeColor = [System.Drawing.Color]::Firebrick
$status.Location = New-Object System.Drawing.Point(24, 96)
$form.Controls.Add($status)

$saveButton = New-Object System.Windows.Forms.Button
$saveButton.Text = "Save"
$saveButton.Location = New-Object System.Drawing.Point(326, 132)
$saveButton.Size = New-Object System.Drawing.Size(100, 32)
$form.Controls.Add($saveButton)

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Text = "Cancel"
$cancelButton.Location = New-Object System.Drawing.Point(432, 132)
$cancelButton.Size = New-Object System.Drawing.Size(100, 32)
$cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($cancelButton)

$form.AcceptButton = $saveButton
$form.CancelButton = $cancelButton

$saveHandler = {
    try {
        $key = $keyTextBox.Text.Trim()
        if ($key -notmatch '^sk-[A-Za-z0-9_-]{20,}$') {
            $status.Text = "The key format is invalid. Please paste it again."
            return
        }

        [string[]] $lines = @()
        $replaced = $false
        if (Test-Path -LiteralPath $envPath) {
            foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
                if ($line -match '^\s*PKAS_DEEPSEEK_API_KEY\s*=') {
                    if (-not $replaced) {
                        $lines += "PKAS_DEEPSEEK_API_KEY=$key"
                        $replaced = $true
                    }
                }
                else {
                    $lines += $line
                }
            }
        }
        if (-not $replaced) {
            $lines += "PKAS_DEEPSEEK_API_KEY=$key"
        }

        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        $temporaryPath = "$envPath.new"
        [System.IO.File]::WriteAllLines($temporaryPath, $lines, $utf8NoBom)
        Move-Item -LiteralPath $temporaryPath -Destination $envPath -Force

        $keyTextBox.Clear()
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    }
    catch {
        $status.Text = "Save failed: $($_.Exception.Message)"
        if ($SelfTest) {
            $form.DialogResult = [System.Windows.Forms.DialogResult]::Abort
            $form.Close()
        }
    }
}.GetNewClosure()
$saveButton.Add_Click($saveHandler)

if ($SelfTest) {
    $shownHandler = {
        $keyTextBox.Text = ""
        $saveButton.PerformClick()
    }.GetNewClosure()
}
else {
    $shownHandler = { $keyTextBox.Select() }.GetNewClosure()
}
$form.Add_Shown($shownHandler)
$result = $form.ShowDialog()

if ($SelfTest) {
    $saved = $false
    if ($result -eq [System.Windows.Forms.DialogResult]::OK -and $selfTestEnvPath) {
        $saved = [System.IO.File]::ReadAllText($selfTestEnvPath) -match '^PKAS_DEEPSEEK_API_KEY=sk-[A-Za-z0-9_-]{20,}\s*$'
    }
    if ($selfTestEnvPath -and (Test-Path -LiteralPath $selfTestEnvPath)) {
        Remove-Item -LiteralPath $selfTestEnvPath -Force
    }
    if ($saved) {
        Write-Output "self_test=passed"
        exit 0
    }
    Write-Output "self_test=failed"
    exit 1
}

if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
    $pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        [System.Windows.Forms.MessageBox]::Show(
            "The key was saved, but the PKAS Python runtime was not found.",
            "PKAS Setup",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        ) | Out-Null
        Write-Output "configured=true;verified=false"
        exit 1
    }

    Push-Location $projectRoot
    try {
        $validationOutput = & $pythonPath -m pkas.setup_verify 2>&1 | Out-String
        $validationExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($validationExitCode -eq 0) {
        [System.Windows.Forms.MessageBox]::Show(
            "DeepSeek is connected and the LangGraph Agent verification passed.",
            "PKAS Setup",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
        Write-Output "configured=true;verified=true"
        exit 0
    }

    [System.Windows.Forms.MessageBox]::Show(
        "The key was saved, but live verification failed. See data\runs\deepseek-setup-verification.json.",
        "PKAS Setup",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning
    ) | Out-Null
    Write-Output "configured=true;verified=false"
    if ($null -ne $validationOutput) {
        Write-Output $validationOutput.Trim()
    }
    exit 1
}

Write-Output "configured=false"
exit 2
