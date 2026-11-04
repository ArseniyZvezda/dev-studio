# Rebuild the web and Windows icons from the approved artwork (dev spec §2.7).
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$desktopRoot = Split-Path $PSScriptRoot -Parent
$assets = Join-Path $desktopRoot 'assets'
$public = Join-Path (Split-Path $desktopRoot -Parent) 'frontend\public'
[void][System.IO.Directory]::CreateDirectory($public)
$source = [System.Drawing.Image]::FromFile((Join-Path $assets 'logo.png'))

function Resize-Png([int]$size) {
    $bitmap = [System.Drawing.Bitmap]::new($size, $size)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $stream = [System.IO.MemoryStream]::new()
    try {
        $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $graphics.DrawImage($source, [System.Drawing.Rectangle]::new(0, 0, $size, $size))
        $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
        return ,$stream.ToArray()
    } finally {
        $stream.Dispose()
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

try {
    if ($source.Width -ne $source.Height) { throw 'The logo must be square.' }
    [System.IO.File]::WriteAllBytes((Join-Path $public 'studio-logo.png'), (Resize-Png 128))
    [System.IO.File]::WriteAllBytes((Join-Path $assets 'tray.png'), (Resize-Png 32))
    # ICO directory followed by lossless PNG frames, including Windows high-DPI sizes.
    $sizes = @(16, 24, 32, 48, 64, 128, 256)
    $frames = @($sizes | ForEach-Object { ,(Resize-Png $_) })
    $stream = [System.IO.MemoryStream]::new()
    $writer = [System.IO.BinaryWriter]::new($stream)
    try {
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]$sizes.Count)
        $offset = 6 + 16 * $sizes.Count
        for ($i = 0; $i -lt $sizes.Count; $i++) {
            $dimension = if ($sizes[$i] -eq 256) { 0 } else { $sizes[$i] }
            $writer.Write([byte]$dimension)
            $writer.Write([byte]$dimension)
            $writer.Write([uint16]0)
            $writer.Write([uint16]1)
            $writer.Write([uint16]32)
            $writer.Write([uint32]$frames[$i].Length)
            $writer.Write([uint32]$offset)
            $offset += $frames[$i].Length
        }
        foreach ($frame in $frames) { $writer.Write([byte[]]$frame) }
        $writer.Flush()
        [System.IO.File]::WriteAllBytes((Join-Path $assets 'icon.ico'), $stream.ToArray())
        [System.IO.File]::WriteAllBytes((Join-Path $public 'favicon.ico'), $stream.ToArray())
    } finally {
        $writer.Dispose()
        $stream.Dispose()
    }
} finally {
    $source.Dispose()
}
