# Render the repository's geometric app.svg design into a multi-resolution Windows ICO.
# Uses built-in GDI+ only; no downloaded assets or runtime dependencies.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$iconPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'helper\collector_static\app.ico'
$images = @()
foreach ($size in @(16,20,24,32,48,64,128,256)) {
    $bitmap = [Drawing.Bitmap]::new($size * 4, $size * 4)
    $graphics = [Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.ScaleTransform(($size * 4 / 64), ($size * 4 / 64))
    $green = [Drawing.SolidBrush]::new([Drawing.ColorTranslator]::FromHtml('#166553'))
    $mint = [Drawing.SolidBrush]::new([Drawing.ColorTranslator]::FromHtml('#87ddbb'))
    $pen = [Drawing.Pen]::new($green, 3.5)
    $pen.StartCap = $pen.EndCap = [Drawing.Drawing2D.LineCap]::Round
    $outline = [Drawing.Drawing2D.GraphicsPath]::new()
    $outline.AddArc(2,2,34,34,180,90)
    $outline.AddArc(28,2,34,34,270,90)
    $outline.AddArc(28,28,34,34,0,90)
    $outline.AddArc(2,28,34,34,90,90)
    $outline.CloseFigure()
    $graphics.FillPath($green,$outline)
    $points = [Drawing.PointF[]]@(
        [Drawing.PointF]::new(17,17),[Drawing.PointF]::new(47,17),
        [Drawing.PointF]::new(47,40),[Drawing.PointF]::new(30,40),
        [Drawing.PointF]::new(20,48),[Drawing.PointF]::new(20,40),[Drawing.PointF]::new(17,40))
    $graphics.FillPolygon([Drawing.Brushes]::White,$points)
    $graphics.DrawLine($pen,24,25,40,25)
    $graphics.DrawLine($pen,24,32,34,32)
    $graphics.FillEllipse($mint,38,36,18,18)
    $graphics.DrawEllipse($pen,38,36,18,18)
    $scaled = [Drawing.Bitmap]::new($size,$size)
    $target = [Drawing.Graphics]::FromImage($scaled)
    $target.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $target.DrawImage($bitmap,0,0,$size,$size)
    $stream = [IO.MemoryStream]::new()
    $scaled.Save($stream,[Drawing.Imaging.ImageFormat]::Png)
    $images += @{Size=$size; Bytes=$stream.ToArray()}
    $stream.Dispose(); $target.Dispose(); $scaled.Dispose(); $outline.Dispose()
    $pen.Dispose(); $green.Dispose(); $mint.Dispose(); $graphics.Dispose(); $bitmap.Dispose()
}
$file = [IO.File]::Create($iconPath)
$writer = [IO.BinaryWriter]::new($file)
try {
    $writer.Write([uint16]0); $writer.Write([uint16]1); $writer.Write([uint16]$images.Count)
    $offset = 6 + 16 * $images.Count
    foreach ($image in $images) {
        $dimension = if ($image.Size -eq 256) { 0 } else { $image.Size }
        $writer.Write([byte]$dimension); $writer.Write([byte]$dimension)
        $writer.Write([byte]0); $writer.Write([byte]0)
        $writer.Write([uint16]1); $writer.Write([uint16]32)
        $writer.Write([uint32]$image.Bytes.Length); $writer.Write([uint32]$offset)
        $offset += $image.Bytes.Length
    }
    foreach ($image in $images) { $writer.Write([byte[]]$image.Bytes) }
} finally { $writer.Dispose(); $file.Dispose() }
Write-Output "Generated app.ico (8 sizes)"
