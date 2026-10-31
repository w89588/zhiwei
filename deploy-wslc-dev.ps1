param(
    [switch]$InfrastructureOnly,
    [switch]$SkipBuild,
    [switch]$Recreate,
    [ValidateRange(1024,65535)][int]$Port = 8787
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$version = (Get-Content -LiteralPath 'VERSION' -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'VERSION 必须是 major.minor.patch 格式' }
$serverImage = "wechat-helper-server:$version"
$clientArchive = "WechatHelper-$version-windows-x64-portable.zip"
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot "release/$clientArchive"))) { $clientArchive = 'WechatHelper-2.0-dev.zip' }
if (-not (Get-Command wslc -ErrorAction SilentlyContinue)) { throw '需要安装 WSLC。' }
$privateRoot = Join-Path $PSScriptRoot '.private/wslc-v2'
New-Item -ItemType Directory -Path $privateRoot -Force | Out-Null
$encoding = New-Object System.Text.UTF8Encoding $false
function New-Secret {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
}
function Invoke-Wslc {
    param([string[]]$Arguments)
    & wslc @Arguments
    if ($LASTEXITCODE -ne 0) { throw "WSLC 操作失败：$($Arguments[0])" }
}
function Write-Env([string]$Name, [string[]]$Lines) {
    [IO.File]::WriteAllText((Join-Path $privateRoot $Name), (($Lines -join "`n")+"`n"), $encoding)
}
$credentialsPath = Join-Path $privateRoot 'credentials.json'
if (Test-Path -LiteralPath $credentialsPath) {
    $credentials = Get-Content -LiteralPath $credentialsPath -Raw | ConvertFrom-Json
} else {
    $credentials = [pscustomobject]@{admin_user='admin';admin_password=(New-Secret);master_key=(New-Secret);mysql_password=(New-Secret);mysql_root_password=(New-Secret);mongo_password=(New-Secret);redis_password=(New-Secret)}
    [IO.File]::WriteAllText($credentialsPath, ($credentials | ConvertTo-Json), $encoding)
}
Write-Env 'mysql.env' @("MYSQL_ROOT_PASSWORD=$($credentials.mysql_root_password)", 'MYSQL_DATABASE=wechat', 'MYSQL_USER=wechat', "MYSQL_PASSWORD=$($credentials.mysql_password)")
Write-Env 'mongo.env' @('MONGO_INITDB_ROOT_USERNAME=wechat', "MONGO_INITDB_ROOT_PASSWORD=$($credentials.mongo_password)")
Write-Env 'redis.env' @("REDIS_PASSWORD=$($credentials.redis_password)")
Write-Env 'server.env' @('WECHAT_PLATFORM_VERSION=2',
    "WECHAT_DATABASE_URL=mysql+pymysql://wechat:$($credentials.mysql_password)@wechat-v2-mysql:3306/wechat?charset=utf8mb4",
    "WECHAT_MONGO_URL=mongodb://wechat:$($credentials.mongo_password)@wechat-v2-mongo:27017/wechat?authSource=admin",
    "WECHAT_REDIS_URL=redis://:$($credentials.redis_password)@wechat-v2-redis:6379/0",
    "WECHAT_MASTER_KEY=$($credentials.master_key)", "WECHAT_ADMIN_USER=$($credentials.admin_user)", "WECHAT_ADMIN_PASSWORD=$($credentials.admin_password)",
    'WECHAT_ALLOWED_HOSTS=127.0.0.1,localhost', 'WECHAT_MODEL_ALLOW_PRIVATE=1', "WECHAT_CLIENT_DOWNLOAD=/downloads/$clientArchive")

$networkNames = @(Invoke-Wslc @('network','list','--format','json') | ForEach-Object { ($_ | ConvertFrom-Json).Name })
if ($networkNames -notcontains 'wechat-v2') { Invoke-Wslc @('network','create','wechat-v2') }
$volumes = Invoke-Wslc @('volume','list','--quiet')
foreach ($name in @('mysql','mongo','redis','server')) {
    if ($volumes -notcontains "wechat-v2-$name") { Invoke-Wslc @('volume','create',"wechat-v2-$name") }
}
function Ensure-Container([string]$Name, [string[]]$RunArguments) {
    $existing = Invoke-Wslc @('list','--all','--quiet','--filter',"name=^${Name}$")
    if ($existing) {
        $running = Invoke-Wslc @('list','--quiet','--filter',"name=^${Name}$")
        if (-not $running) { Invoke-Wslc @('start',$Name) }
    } else { Invoke-Wslc (@('run','--detach','--name',$Name,'--network','wechat-v2') + $RunArguments) }
}
Ensure-Container 'wechat-v2-mysql' @('--env-file',(Join-Path $privateRoot 'mysql.env'),'--mount','type=volume,source=wechat-v2-mysql,target=/var/lib/mysql','--publish','127.0.0.1:13306:3306','mysql:8.4')
Ensure-Container 'wechat-v2-mongo' @('--env-file',(Join-Path $privateRoot 'mongo.env'),'--mount','type=volume,source=wechat-v2-mongo,target=/data/db','--publish','127.0.0.1:17017:27017','mongo:7')
Ensure-Container 'wechat-v2-redis' @('--env-file',(Join-Path $privateRoot 'redis.env'),'--mount','type=volume,source=wechat-v2-redis,target=/data','--publish','127.0.0.1:16379:6379','redis:7-alpine','sh','-c','exec redis-server --requirepass "$REDIS_PASSWORD" --appendonly yes')
if ($InfrastructureOnly) { Write-Output 'WSLC 开发数据库已启动。'; exit 0 }

if (-not $SkipBuild) { Invoke-Wslc @('build','--progress','plain','-t',$serverImage,$PSScriptRoot) }
# Keep the previous installation and its data volume for explicit migration or rollback.
if ($Port -eq 8787) {
    $legacy = Invoke-Wslc @('list','--quiet','--filter','name=^wechat-helper-server$')
    if ($legacy) { Invoke-Wslc @('stop','wechat-helper-server') }
}
$serverName = 'wechat-v2-server'
$existing = Invoke-Wslc @('list','--all','--quiet','--filter',"name=^${serverName}$")
if ($existing -and $Recreate) {
    $details = Invoke-Wslc @('inspect','--format','json',$serverName) | ConvertFrom-Json
    if (-not @($details.Mounts | Where-Object { $_.Type -eq 'volume' -and $_.Name -eq 'wechat-v2-server' -and $_.Destination -eq '/data' }).Count) {
        throw '服务端持久化卷与预期不一致，停止重建。'
    }
    $running = Invoke-Wslc @('list','--quiet','--filter',"name=^${serverName}$")
    if ($running) { Invoke-Wslc @('stop',$serverName) }
    Invoke-Wslc @('remove',$serverName)
}
$downloadRoot = Join-Path $PSScriptRoot 'release'
New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
Ensure-Container $serverName @('--env-file',(Join-Path $privateRoot 'server.env'), '--mount','type=volume,source=wechat-v2-server,target=/data',
    '--mount',"type=bind,source=$downloadRoot,target=/downloads,readonly", '--publish',"127.0.0.1:${Port}:8787",
    '--health-cmd','python -c "import urllib.request; urllib.request.urlopen(''http://127.0.0.1:8787/readyz'', timeout=8)"',
    '--health-interval','20s','--health-timeout','10s','--health-start-period','30s','--health-retries','3',$serverImage)
$ready = $false
$readyDeadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $readyDeadline) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/readyz" -TimeoutSec 10
        if ($health.ok) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) { throw '服务端尚未就绪，请检查 wslc logs wechat-v2-server；持久化卷与凭据保持不变。' }
Write-Output "管理页面：http://127.0.0.1:$Port"
Write-Output "初始管理员及开发环境凭据：$credentialsPath"
Write-Output '首次登录必须修改密码。旧版服务端容器和数据卷已保留。'
