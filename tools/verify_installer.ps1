param([string]$Version, [string]$Python, [string]$ApplicationDirectory, [switch]$IsolatedIdentity)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
if (-not $Version) { $Version = (Get-Content -LiteralPath (Join-Path $projectPath 'VERSION') -Raw).Trim() }
if (-not $Python) {
    $Python = if (Test-Path -LiteralPath (Join-Path $projectPath '.venv-dev\Scripts\python.exe')) { Join-Path $projectPath '.venv-dev\Scripts\python.exe' } else { Join-Path $projectPath '.venv\Scripts\python.exe' }
}
if (-not $ApplicationDirectory) { $ApplicationDirectory = Join-Path $projectPath 'dist\WechatHelper' }
$testRoot = Join-Path $projectPath '.private\installer-compact-smoke'
$installPath = Join-Path $testRoot 'App With Spaces'
$dataPath = Join-Path $testRoot 'Data'
$installer = Join-Path $projectPath "release\WechatHelper-$Version-windows-x64-setup.exe"
$registryPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{C26B0B44-9EF1-450A-ADBF-C82C47F704C1}_is1'
$startupName = 'Zhiwei'
$appRegistryPath = 'HKCU:\Software\Zhiwei'
if ($IsolatedIdentity) {
    $registryPath = $registryPath.Replace('C26B0B44-9EF1-450A-ADBF-C82C47F704C1','2A0C762D-0183-4A52-93CF-6E938640C9FE')
    $startupName = 'WechatHelperCompactQA'
    $appRegistryPath = 'HKCU:\Software\WechatHelperCompactQA'
    $installer = Join-Path $projectPath "release\qa\WechatHelper-$Version-windows-x64-setup.exe"
}
if (Test-Path -LiteralPath $registryPath) { throw '已有正式安装；避免覆盖用户安装，请在独立 Windows 用户下验证' }
$startupPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
function Read-Startup { (Get-ItemProperty -LiteralPath $startupPath -Name $startupName -ErrorAction SilentlyContinue).$startupName }
if ((Read-Startup) -or (Test-Path $appRegistryPath)) { throw '存在应用启动项或注册信息；停止测试以保护用户设置' }
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
$markerPath = Join-Path $dataPath 'uninstall-preserves-data.txt'
Set-Content -LiteralPath $markerPath -Value 'installer-smoke-test' -Encoding utf8
$installLog = Join-Path $testRoot 'install.log'
$installArguments = @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/NOICONS',('/DIR="' + $installPath + '"'),('/LOG="' + $installLog + '"'))
$process = Start-Process -FilePath $installer -ArgumentList $installArguments -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "安装失败：$($process.ExitCode)" }
$executable = Join-Path $installPath 'WechatHelper.exe'
if (-not (Test-Path -LiteralPath $executable)) { throw '安装未生成应用程序' }
$expectedHash = (Get-FileHash -LiteralPath (Join-Path $ApplicationDirectory 'WechatHelper.exe')).Hash
if ((Get-FileHash -LiteralPath $executable).Hash -ne $expectedHash) { throw '安装后的程序校验不一致' }
if (-not (Test-Path -LiteralPath $registryPath)) { throw '没有注册卸载入口' }
$taskListener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
$taskListener.Start()
$port = $taskListener.LocalEndpoint.Port
$taskListener.Stop()
$previousDataPath = $env:WECHAT_HELPER_DATA
$appProcess = $null
try {
    $env:WECHAT_HELPER_DATA = $dataPath
    $expectedStartup = '"' + $executable + '" --startup'
    if ((Read-Startup) -ne $expectedStartup) { throw '首次安装没有默认开启正确的启动项' }
    $appProcess = Start-Process -FilePath $executable -ArgumentList '--browser','--port',"$port" -WorkingDirectory $installPath -WindowStyle Hidden -PassThru
    $url = "http://127.0.0.1:$port"
    $html = $null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try { $html = (Invoke-WebRequest -Uri $url -TimeoutSec 2).Content; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $html) { throw '安装后的应用服务未启动' }
    $token = [regex]::Match($html,"window.HELPER_TOKEN='([^']+)'").Groups[1].Value
    if (-not $token) { throw '应用未返回会话 token' }
    $status = Invoke-RestMethod -Uri "$url/api/status" -Headers @{'X-Helper-Token'=$token}
    if ($status.configured -or $status.changed_messages -ne 0) { throw '隔离首次启动状态不正确' }
    foreach ($asset in @('collector.js','desktop.js','api.js','app.ico','app.svg')) {
        if ((Invoke-WebRequest -Uri "$url/static/$asset").StatusCode -ne 200) { throw "缺少界面资源 $asset" }
    }
    Stop-Process -Id $appProcess.Id; $appProcess.WaitForExit(); $appProcess = $null
    foreach ($enabled in @('false','true')) {
        & $Python -c 'import sys; from helper import autostart; sys.frozen=True; sys.executable=sys.argv[1]; autostart.VALUE_NAME=sys.argv[3]; autostart.set_autostart(sys.argv[2]=="true")' $executable $enabled $startupName
        if ($LASTEXITCODE -ne 0) { throw '软件内自启设置失败' }
        $upgrade = Start-Process -FilePath $installer -ArgumentList $installArguments -WindowStyle Hidden -Wait -PassThru
        if ($upgrade.ExitCode -ne 0) { throw '覆盖安装失败' }
        if ($enabled -eq 'false' -and (Read-Startup)) { throw '覆盖安装重新开启了已关闭的自启' }
        if ($enabled -eq 'true' -and (Read-Startup) -ne $expectedStartup) { throw '覆盖安装未保留已开启的自启' }
    }
} finally {
    $env:WECHAT_HELPER_DATA = $previousDataPath
    if ($appProcess -and -not $appProcess.HasExited) { Stop-Process -Id $appProcess.Id; $appProcess.WaitForExit() }
    $uninstaller = Join-Path $installPath 'unins000.exe'
    if (Test-Path -LiteralPath $uninstaller) {
        $uninstallLog = Join-Path $testRoot 'uninstall.log'
        $uninstall = Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',('/LOG="' + $uninstallLog + '"') -WindowStyle Hidden -Wait -PassThru
        if ($uninstall.ExitCode -ne 0) { throw "卸载失败：$($uninstall.ExitCode)" }
    }
}
if (Test-Path -LiteralPath $executable) { throw '卸载后仍存在应用程序' }
if (Test-Path -LiteralPath $registryPath) { throw '卸载入口未清除' }
if (Read-Startup) { throw '卸载未清理启动项' }
if (-not (Test-Path -LiteralPath $markerPath)) { throw '卸载删除了用户数据' }
try {
    $optOut = Start-Process -FilePath $installer -ArgumentList ($installArguments + @('/TASKS=')) -WindowStyle Hidden -Wait -PassThru
    if ($optOut.ExitCode -ne 0) { throw '取消自启选项的安装失败' }
    if (Read-Startup) { throw '安装时取消自启后仍存在启动项' }
    & $Python -c 'import sys; from helper import autostart; sys.frozen=True; sys.executable=sys.argv[1]; autostart.VALUE_NAME=sys.argv[2]; autostart.set_autostart(True)' $executable $startupName
    if ($LASTEXITCODE -ne 0) { throw '安装时取消后无法在软件内开启' }
} finally {
    if (Test-Path -LiteralPath $uninstaller) {
        $uninstall = Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -WindowStyle Hidden -Wait -PassThru
        if ($uninstall.ExitCode -ne 0) { throw '取消自启选项安装的清理失败' }
    }
}
if ((Read-Startup) -or (Test-Path -LiteralPath $registryPath)) { throw '最终卸载残留启动项或卸载入口' }
$report = @{version=$Version; install='passed'; binary_hash='passed'; isolated_start='passed'; assets='passed'; default_autostart='passed'; install_opt_out='passed'; in_app_toggle='passed'; upgrade_preserves_enabled='passed'; upgrade_preserves_disabled='passed'; uninstall_cleans_startup='passed'; user_data_preserved='passed'}
$report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $testRoot 'verification.json') -Encoding utf8
$report | ConvertTo-Json
