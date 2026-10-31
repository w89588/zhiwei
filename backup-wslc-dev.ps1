param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot ('.private/backups/v2-'+(Get-Date -Format 'yyyyMMdd-HHmmss')) }
$backupRoot = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $backupRoot) { throw '备份目录必须是新目录，避免覆盖备份。' }
New-Item -ItemType Directory -Path $backupRoot | Out-Null
function Run-Wslc([string[]]$Arguments) {
    & wslc @Arguments
    if ($LASTEXITCODE -ne 0) { throw "WSLC 备份操作失败：$($Arguments[0])" }
}
$running = Run-Wslc @('list','--quiet','--filter','name=^wechat-v2-server$')
try {
    # Stop this application's sole writer so SQL pointers and immutable Mongo versions share a checkpoint.
    if ($running) { Run-Wslc @('stop','wechat-v2-server') }
    Run-Wslc @('exec','wechat-v2-mysql','sh','-c','MYSQL_PWD="$MYSQL_PASSWORD" mysqldump --user="$MYSQL_USER" --single-transaction --no-tablespaces --set-gtid-purged=OFF wechat > /tmp/wechat-v2-backup.sql')
    Run-Wslc @('exec','wechat-v2-mongo','sh','-c','mongodump --username="$MONGO_INITDB_ROOT_USERNAME" --password="$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase=admin --db=wechat --archive=/tmp/wechat-v2-backup.archive --gzip --quiet')
    Run-Wslc @('cp','wechat-v2-mysql:/tmp/wechat-v2-backup.sql',(Join-Path $backupRoot 'mysql.sql'))
    Run-Wslc @('cp','wechat-v2-mongo:/tmp/wechat-v2-backup.archive',(Join-Path $backupRoot 'mongo.archive.gz'))
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot '.private/wslc-v2/credentials.json') -Destination (Join-Path $backupRoot 'credentials.json')
    $hashes = foreach ($name in @('mysql.sql','mongo.archive.gz','credentials.json')) {
        $hash=(Get-FileHash -LiteralPath (Join-Path $backupRoot $name) -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $name"
    }
    [IO.File]::WriteAllLines((Join-Path $backupRoot 'SHA256SUMS.txt'),$hashes,[Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $backupRoot 'README.txt'),'Contains private encryption keys. Restore MySQL and MongoDB together with credentials.json. Redis is disposable and must not be used as a recovery source.',[Text.UTF8Encoding]::new($false))
    Write-Output "一致性备份已保存：$backupRoot"
} finally {
    if ($running) { Run-Wslc @('start','wechat-v2-server') }
}
