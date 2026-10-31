param([string]$Python='.\.venv-dev\Scripts\python.exe')
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
& .\build.ps1 -Python $Python -SkipDependencyInstall -OutputDirectory 'dist/v2-dev' -WorkDirectory 'build/v2-dev'
if ($LASTEXITCODE -ne 0) { throw '客户端构建失败' }
$application = Join-Path $PSScriptRoot 'dist/v2-dev/WechatHelper'
$version = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'VERSION') -Raw).Trim()
[IO.File]::WriteAllText((Join-Path $application 'VERSION'),($version + '-dev'),[Text.UTF8Encoding]::new($false))
$archive = Join-Path $PSScriptRoot 'release/WechatHelper-2.0-dev.zip'
Compress-Archive -LiteralPath $application -DestinationPath $archive -Force
$checksum = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($archive+'.sha256'),($checksum+'  WechatHelper-2.0-dev.zip'),[Text.UTF8Encoding]::new($false))
Write-Output "客户端已生成：$archive"
