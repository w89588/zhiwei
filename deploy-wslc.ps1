param(
    [switch]$SkipBuild,
    [switch]$Recreate,
    [ValidateRange(1024,65535)][int]$Port = 8787,
    [ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')][string]$ContainerName = 'wechat-helper-server'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command wslc -ErrorAction SilentlyContinue)) { throw '请先安装支持容器 CLI 的 WSL / wslc。' }

$privateRoot = Join-Path $PSScriptRoot '.private/wslc-server'
New-Item -ItemType Directory -Path $privateRoot -Force | Out-Null
$connectionPath = Join-Path $privateRoot 'connection.json'
function New-AccessToken {
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $random.GetBytes($bytes) } finally { $random.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
}
if (Test-Path -LiteralPath $connectionPath) {
    $connection = Get-Content -LiteralPath $connectionPath -Raw | ConvertFrom-Json
} else {
    $connection = [pscustomobject]@{account='default';upload_token=(New-AccessToken);read_token=(New-AccessToken)}
}
$url = "http://127.0.0.1:$Port"
$record = [ordered]@{url=$url;mcp_url="$url/mcp";account=$connection.account;upload_token=$connection.upload_token;read_token=$connection.read_token}
$encoding = New-Object System.Text.UTF8Encoding $false
[IO.File]::WriteAllText($connectionPath,($record | ConvertTo-Json),$encoding)
$accounts = @{}
$accounts[$connection.account] = @{upload_token=$connection.upload_token;read_token=$connection.read_token}
$envPath = Join-Path $privateRoot 'server.env'
[IO.File]::WriteAllText($envPath,"WECHAT_SERVER_ACCOUNTS=$($accounts | ConvertTo-Json -Compress)`n",$encoding)
$mcpConfig = @{mcpServers=@{'wechat-helper'=@{url="$url/mcp";headers=@{Authorization="Bearer $($connection.read_token)"}}}}
[IO.File]::WriteAllText((Join-Path $privateRoot 'mcp-config.json'),($mcpConfig | ConvertTo-Json -Depth 6),$encoding)

if (-not $SkipBuild) {
    & wslc build --progress plain -t wechat-helper-server:local $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { throw 'WSLC 镜像构建失败。' }
}
$existing = & wslc list --all --quiet --filter "name=^${ContainerName}$"
if ($LASTEXITCODE -ne 0) { throw '无法读取 WSLC 容器状态。' }
if ($existing -and $Recreate) {
    $details = & wslc inspect --format json $ContainerName | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw '无法检查现有 WSLC 容器。' }
    $dataMount = @($details.Mounts | Where-Object { $_.Destination -eq '/data' -and $_.Type -eq 'volume' -and $_.Name -eq 'wechat-helper-data' })
    if ($dataMount.Count -ne 1) { throw '现有容器没有使用预期的持久化卷，已停止重新创建，避免丢失数据。' }
    $running = & wslc list --quiet --filter "name=^${ContainerName}$"
    if ($LASTEXITCODE -ne 0) { throw '无法读取 WSLC 容器状态。' }
    if ($running) {
        & wslc stop $ContainerName
        if ($LASTEXITCODE -ne 0) { throw '停止旧容器失败。' }
    }
    & wslc remove $ContainerName
    if ($LASTEXITCODE -ne 0) { throw '移除旧容器失败。持久化卷保持不变。' }
    $existing = $null
}
if ($existing) {
    $running = & wslc list --quiet --filter "name=^${ContainerName}$"
    if ($LASTEXITCODE -ne 0) { throw '无法读取 WSLC 容器状态。' }
    if (-not $running) {
        & wslc start $ContainerName
        if ($LASTEXITCODE -ne 0) { throw '启动现有 WSLC 容器失败。' }
    }
    Write-Output '已启动现有容器；容器继续使用创建时的镜像、端口和凭据。'
} else {
    $volumes = & wslc volume list --quiet
    if ($LASTEXITCODE -ne 0) { throw '无法读取 WSLC 卷列表。' }
    if ($volumes -notcontains 'wechat-helper-data') {
        & wslc volume create wechat-helper-data
        if ($LASTEXITCODE -ne 0) { throw '创建 WSLC 持久化卷失败。' }
    }
    $health = 'python -c "import urllib.request; urllib.request.urlopen(''http://127.0.0.1:8787/healthz'', timeout=3)"'
    & wslc run --detach --name $ContainerName --publish "127.0.0.1:${Port}:8787" --env-file $envPath --mount 'type=volume,source=wechat-helper-data,target=/data' --health-cmd $health --health-interval 15s --health-timeout 5s --health-start-period 5s --health-retries 3 wechat-helper-server:local
    if ($LASTEXITCODE -ne 0) { throw '创建 WSLC 容器失败。' }
}
Write-Output "服务端：$url"
Write-Output "健康检查：$url/healthz"
Write-Output "MCP：$url/mcp"
Write-Output "本机凭据：$connectionPath（不要提交或分享此文件）"
