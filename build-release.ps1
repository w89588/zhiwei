param(
    [string]$Version,
    [switch]$SkipBuild,
    [string]$Compiler,
    [string]$ApplicationDirectory,
    [string]$Python
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $Version) { $Version = (Get-Content -LiteralPath 'VERSION' -Raw).Trim() }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw '版本号必须是 major.minor.patch 格式' }
if ($Version -ne (Get-Content -LiteralPath 'VERSION' -Raw).Trim()) { throw '发布版本必须与 VERSION 一致' }
if (-not $Compiler) {
    $compilerCommand = Get-Command iscc.exe,iscc -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($compilerCommand) { $Compiler = $compilerCommand.Source }
}
if (-not $Compiler) {
    $compilerCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe')
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe')
    )
    if ($env:SCOOP) { $compilerCandidates += (Join-Path $env:SCOOP 'apps\innosetup-np\current\ISCC.exe') }
    if ($env:SCOOP_GLOBAL) { $compilerCandidates += (Join-Path $env:SCOOP_GLOBAL 'apps\innosetup-np\current\ISCC.exe') }
    $Compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $Compiler) { throw '未找到 Inno Setup 编译器。请安装 Inno Setup 6，或使用 -Compiler 指定 ISCC.exe' }
if (-not (Test-Path -LiteralPath $Compiler -PathType Leaf)) { throw "找不到 Inno Setup 编译器：$Compiler" }
if (-not $SkipBuild) {
    & '.\build.ps1' -Python $Python
    if ($LASTEXITCODE -ne 0) { throw '应用构建失败' }
}
$applicationPath = if ($ApplicationDirectory) { (Resolve-Path -LiteralPath $ApplicationDirectory).Path } else { Join-Path $PSScriptRoot 'dist\WechatHelper' }
if (-not (Test-Path -LiteralPath (Join-Path $applicationPath 'WechatHelper.exe'))) {
    throw '请先运行 build.ps1 生成应用文件'
}
if ((Get-Content -LiteralPath (Join-Path $applicationPath 'VERSION') -Raw).Trim() -ne $Version) { throw '应用构建版本与发布版本不一致，请重新构建' }
$outputPath = Join-Path $PSScriptRoot 'release'
New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
Copy-Item -LiteralPath 'README.md','LICENSE','NOTICE.txt','LICENSE-wcdb-key-tool.txt','LICENSE-wechat-decrypt.txt','VERIFICATION.md','VERSION','RELEASE-NOTES.md' -Destination $applicationPath
& $Compiler "/DAppVersion=$Version" "/DProjectRoot=$PSScriptRoot" "/DOutputRoot=$outputPath" "/DAppSource=$applicationPath" 'packaging\windows\WechatHelper.iss'
if ($LASTEXITCODE -ne 0) { throw '安装包构建失败' }
$portablePath = Join-Path $outputPath "WechatHelper-$Version-windows-x64-portable.zip"
Compress-Archive -LiteralPath $applicationPath -DestinationPath $portablePath -Force
$installerPath = Join-Path $outputPath "WechatHelper-$Version-windows-x64-setup.exe"
$assets = @($installerPath,$portablePath)
$checksumLines = foreach ($assetPath in $assets) {
    $hash = (Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $([IO.Path]::GetFileName($assetPath))"
}
[IO.File]::WriteAllLines((Join-Path $outputPath 'SHA256SUMS.txt'),$checksumLines,[Text.UTF8Encoding]::new($false))
Write-Output "发布文件已生成：$outputPath"
