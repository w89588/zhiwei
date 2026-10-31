param([string]$ApplicationDirectory, [string]$Compiler)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
if (-not $ApplicationDirectory) { $ApplicationDirectory = Join-Path $projectPath 'dist\WechatHelper' }
if (-not $Compiler) { $Compiler = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe' }
$testRoot = Join-Path $projectPath '.private\ui-qa'
New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
$source = Get-Content -LiteralPath (Join-Path $projectPath 'packaging\windows\WechatHelper.iss') -Raw
# Only change installation identity, registry value names and shortcuts.
# The payload, default task selection, upgrade and uninstall logic stay identical.
$source = $source.Replace('C26B0B44-9EF1-450A-ADBF-C82C47F704C1','2A0C762D-0183-4A52-93CF-6E938640C9FE')
$source = $source.Replace('Software\Zhiwei','Software\WechatHelperCompactQA')
$source = $source.Replace('ValueName: "Zhiwei"','ValueName: "WechatHelperCompactQA"')
$source = $source.Replace("Run', 'Zhiwei'", "Run', 'WechatHelperCompactQA'")
$source = $source.Replace('DefaultGroupName=Zhiwei','DefaultGroupName=WechatHelperCompactQA')
$source = $source.Replace('{autoprograms}\Zhiwei','{autoprograms}\WechatHelperCompactQA')
$source = $source.Replace('{autodesktop}\Zhiwei','{autodesktop}\WechatHelperCompactQA')
$testScript = Join-Path $testRoot 'WechatHelper-qa.iss'
$source | Set-Content -LiteralPath $testScript -Encoding utf8
$version = (Get-Content -LiteralPath (Join-Path $projectPath 'VERSION') -Raw).Trim()
& $Compiler /Qp "/DAppVersion=$version" "/DProjectRoot=$projectPath" "/DOutputRoot=$projectPath\release\qa" "/DAppSource=$ApplicationDirectory" $testScript
if ($LASTEXITCODE -ne 0) { throw '隔离安装包编译失败' }
