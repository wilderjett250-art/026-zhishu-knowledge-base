[CmdletBinding()]
param(
    [string]$SourcePath = (Join-Path $PSScriptRoot "..\desktop\src-tauri\icons\icon.png"),
    [string]$OutputPath = (Join-Path $PSScriptRoot "..\desktop\src-tauri\icons\icon.ico")
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

function New-PngIconFrame {
    param(
        [Parameter(Mandatory)][System.Drawing.Image]$Source,
        [Parameter(Mandatory)][int]$Size
    )

    $canvas = [System.Drawing.Bitmap]::new($Size, $Size)
    $graphics = [System.Drawing.Graphics]::FromImage($canvas)
    $stream = [System.IO.MemoryStream]::new()
    try {
        $graphics.Clear([System.Drawing.Color]::Transparent)
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
        $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
        $graphics.DrawImage($Source, [System.Drawing.Rectangle]::new(0, 0, $Size, $Size))
        $canvas.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
        return $stream.ToArray()
    } finally {
        $stream.Dispose()
        $graphics.Dispose()
        $canvas.Dispose()
    }
}

$resolvedSource = [System.IO.Path]::GetFullPath($SourcePath)
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputPath)
if (-not (Test-Path -LiteralPath $resolvedSource -PathType Leaf)) {
    throw "未找到图标源 PNG：$resolvedSource"
}

$sizes = @(16, 24, 32, 48, 64, 128, 256)
$payloads = New-Object 'System.Collections.Generic.List[byte[]]'
$image = [System.Drawing.Image]::FromFile($resolvedSource)
try {
    foreach ($size in $sizes) {
        $payloads.Add((New-PngIconFrame -Source $image -Size $size))
    }
} finally {
    $image.Dispose()
}

$outputDirectory = Split-Path -Parent $resolvedOutput
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$stream = [System.IO.File]::Open(
    $resolvedOutput,
    [System.IO.FileMode]::Create,
    [System.IO.FileAccess]::Write,
    [System.IO.FileShare]::None
)
$writer = [System.IO.BinaryWriter]::new($stream)
try {
    $writer.Write([uint16]0)
    $writer.Write([uint16]1)
    $writer.Write([uint16]$sizes.Count)

    $offset = 6 + (16 * $sizes.Count)
    for ($index = 0; $index -lt $sizes.Count; $index++) {
        $size = $sizes[$index]
        $payload = $payloads[$index]
        $dimension = if ($size -eq 256) { [byte]0 } else { [byte]$size }
        $writer.Write($dimension)
        $writer.Write($dimension)
        $writer.Write([byte]0)
        $writer.Write([byte]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]32)
        $writer.Write([uint32]$payload.Length)
        $writer.Write([uint32]$offset)
        $offset += $payload.Length
    }
    foreach ($payload in $payloads) {
        $writer.Write($payload)
    }
} finally {
    $writer.Dispose()
    $stream.Dispose()
}

[pscustomobject]@{
    source = $resolvedSource
    output = $resolvedOutput
    frames = $sizes
    bytes = (Get-Item -LiteralPath $resolvedOutput).Length
} | ConvertTo-Json -Compress
