param([switch]$SkipDependencyInstall,[string]$OutputDirectory='dist',[string]$WorkDirectory='build',[string]$Python)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $Python) {
    $Python = if (Test-Path -LiteralPath '.\.venv-dev\Scripts\python.exe') { '.\.venv-dev\Scripts\python.exe' } else { '.\.venv\Scripts\python.exe' }
}
$outputRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $OutputDirectory))
$workRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $WorkDirectory))
foreach ($target in @($outputRoot,$workRoot)) {
    if (-not $target.StartsWith($PSScriptRoot + '\',[StringComparison]::OrdinalIgnoreCase)) { throw '构建目录必须位于当前仓库内部' }
}
$applicationPath = Join-Path $outputRoot 'WechatHelper'
if (-not $SkipDependencyInstall) {
& $Python -m pip install -r requirements-dev-lock.txt
if ($LASTEXITCODE -ne 0) { throw '运行依赖安装失败' }
}
& $Python -m PyInstaller --distpath $outputRoot --workpath $workRoot --noconfirm --clean --windowed --onedir --name WechatHelper --icon 'helper/collector_static/app.ico' --add-data 'helper/collector_static;helper/collector_static' --collect-all webview --collect-all pythonnet --collect-all imageio_ffmpeg --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.lifespan.on client_main.py
if ($LASTEXITCODE -ne 0) { throw '打包失败' }
Copy-Item -LiteralPath 'README.md','LICENSE','NOTICE.txt','LICENSE-wcdb-key-tool.txt','LICENSE-wechat-decrypt.txt','VERIFICATION.md','VERSION','RELEASE-NOTES.md' -Destination $applicationPath
$docsPath = Join-Path $applicationPath 'docs'
New-Item -ItemType Directory -Path $docsPath -Force | Out-Null
Get-ChildItem -LiteralPath 'docs' -File | Where-Object { $_.Name -ne 'MINIPC-DEPLOYMENT.md' } | Copy-Item -Destination $docsPath -Force
