$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Test-Python312($Executable) {
    if (-not (Test-Path -LiteralPath $Executable)) { return $false }
    & $Executable -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 12) else 1)'
    return $LASTEXITCODE -eq 0
}

$devPython = Join-Path $PSScriptRoot '.venv-dev\Scripts\python.exe'
$localPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$runtime = $null
if (Test-Python312 $devPython) {
    $runtime = $devPython
} elseif (Test-Python312 $localPython) {
    $runtime = $localPython
} else {
    if (Test-Path -LiteralPath '.venv-dev') {
        throw '现有 .venv-dev 不是可用的 Python 3.12 环境；请检查该环境，或将其移走后重试。'
    }
    Write-Host '正在使用 Python 3.12 创建开发环境 .venv-dev ...'
    $python312 = Get-Command python3.12 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python312) {
        & $python312.Source -m venv .venv-dev
    } else {
        $launcher = Get-Command py -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $launcher) { throw '未找到 Python 3.12，请先安装 Python 3.12 后重试。' }
        & $launcher.Source -3.12 -m venv .venv-dev
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Python312 $devPython)) {
        throw '无法创建 Python 3.12 环境，请检查 Python 安装后重试。'
    }
    $runtime = $devPython
}

# An existing environment may be incomplete, for example after a failed install.
& $runtime -c 'import importlib.util, sys; names = ("fastapi", "uvicorn", "httpx", "Crypto", "zstandard", "psutil", "webview", "mcp", "tomlkit"); sys.exit(any(importlib.util.find_spec(name) is None for name in names))'
$missingDependencies = $LASTEXITCODE -ne 0
if (-not $missingDependencies) {
    & $runtime -m pip check
    $missingDependencies = $LASTEXITCODE -ne 0
}
if ($missingDependencies) {
    Write-Host '正在安装客户端依赖 ...'
    & $runtime -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请检查上方错误信息后重试。' }
}

& $runtime client_main.py @args
exit $LASTEXITCODE
