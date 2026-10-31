param([string]$ApplicationDirectory = 'dist\WechatHelper')
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$applicationPath = (Resolve-Path -LiteralPath $ApplicationDirectory).Path
$testRoot = Join-Path $projectPath ('.private\background registration ' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$taskName = & (Join-Path $projectPath '.venv\Scripts\python.exe') -c 'import hashlib,sys; from pathlib import Path; print("WechatHelperMemory-"+hashlib.sha256(str(Path(sys.argv[1]).resolve()).encode()).hexdigest()[:10])' $testRoot
if ($LASTEXITCODE -ne 0 -or $taskName -notmatch '^WechatHelperMemory-[0-9a-f]{10}$') { throw '测试任务名生成失败' }
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw '隔离测试任务名冲突' }
@{enabled=$true; command='old-install.exe'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $testRoot 'background.json') -Encoding utf8NoBOM
$previousDataPath = $env:WECHAT_HELPER_DATA
$appProcess = $null
try {
    $env:WECHAT_HELPER_DATA = $testRoot
    $appProcess = Start-Process -FilePath (Join-Path $applicationPath 'WechatHelper.exe') -WorkingDirectory $applicationPath -WindowStyle Hidden -PassThru
    $registration = $null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $registration = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($registration) { break }
        if ($appProcess.HasExited) { throw '隔离测试程序提前退出' }
        Start-Sleep -Milliseconds 500
    }
    if (-not $registration) { throw '已开启的后台任务未重新注册' }
    $workerPath = Join-Path $applicationPath 'WechatHelperTasks.exe'
    if ($registration.Actions.Execute -ne $workerPath) { throw '后台任务没有指向当前程序目录' }
    if ($registration.Triggers.Repetition.Interval -ne 'PT15M') { throw '后台任务周期不正确' }
    $config = Get-Content -LiteralPath (Join-Path $testRoot 'background.json') -Raw | ConvertFrom-Json
    if (-not $config.enabled -or -not $config.command.Contains($workerPath)) { throw '更新后的注册信息未保存' }
    # Run through Task Scheduler with isolated, empty data, as in normal use.
    $previousRun = ($registration | Get-ScheduledTaskInfo).LastRunTime
    Start-ScheduledTask -TaskName $taskName
    $taskInfo = $null
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $currentTask = Get-ScheduledTask -TaskName $taskName
        $taskInfo = $currentTask | Get-ScheduledTaskInfo
        if ($taskInfo.LastRunTime -gt $previousRun -and $currentTask.State -ne 'Running') { break }
        Start-Sleep -Milliseconds 500
    }
    if ($taskInfo.LastRunTime -le $previousRun -or $currentTask.State -eq 'Running' -or $taskInfo.LastTaskResult -ne 0) { throw '后台程序通过任务计划执行失败' }
    $report = @{registration='passed'; path_with_spaces='passed'; interval='PT15M'; scheduled_worker='passed'; last_result=$taskInfo.LastTaskResult}
    $report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $testRoot 'verification.json') -Encoding utf8
    $report | ConvertTo-Json
} finally {
    $env:WECHAT_HELPER_DATA = $previousDataPath
    if ($appProcess -and -not $appProcess.HasExited) { Stop-Process -Id $appProcess.Id; $appProcess.WaitForExit() }
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
}
