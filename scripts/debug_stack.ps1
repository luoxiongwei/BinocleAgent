<#
.SYNOPSIS
  Binocle Agent 开发三件套一键管理：任务服务(8000) + 只读控制台(8010) + Vue dev(5173)
.DESCRIPTION
  四动作 start / stop / restart / status；PID 落盘 results/debug_stack/processes.json；
  各服务日志 results/debug_stack/<name>.{out,err}.log。
  启动前自动清理两级大小写代理变量（HTTP_PROXY/http_proxy 等残留会打死 api.deepseek.com 直连）；
  Python 解释器自动探测（GDYOLO_PYTHON 环境变量 > D:\python3.12\python.exe > PATH 中的 python，
  候选须通过 import torch, fastapi, uvicorn 验证）；启动后按服务逐一等待健康检查就绪。
.PARAMETER Action
  start（默认）/ stop / restart / status
.PARAMETER Precheck
  启动前先运行 scripts/doctor.py 部署自检（28 项），FAIL 则中止启动。
.EXAMPLE
  scripts\start_debug_stack.bat              # 等价 -Action start
  scripts\start_debug_stack.bat restart
  scripts\start_debug_stack.bat status
  powershell -File scripts\debug_stack.ps1 -Action start -Precheck
#>
param(
    [ValidateSet('start','stop','restart','status')][string]$Action = 'start',
    [switch]$Precheck
)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path $PSScriptRoot -Parent
$logDir = Join-Path $projectDir 'results/debug_stack'
$statePath = Join-Path $logDir 'processes.json'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# --- 清理两级大小写代理变量（沙箱/企业代理残留会打死 api.deepseek.com 直连）---
foreach ($n in @('HTTP_PROXY','http_proxy','HTTPS_PROXY','https_proxy','ALL_PROXY','all_proxy')) {
    try { [Environment]::SetEnvironmentVariable($n, $null, 'Process') } catch {}
}

# --- 解释器/命令解析：文件路径直通，命令名走 Get-Command ---
function Resolve-ExePath([string]$name) {
    if (Test-Path -LiteralPath $name) { return (Resolve-Path -LiteralPath $name).Path }
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# --- Python 探测：服务依赖 torch + cv2，必须选对解释器（受管 3.13 无 torch，不能用）---
function Resolve-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($env:GDYOLO_PYTHON) { $candidates.Add($env:GDYOLO_PYTHON) }
    $localVenv = Join-Path $projectDir '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localVenv) { $candidates.Add($localVenv) }
    $candidates.Add('D:\python3.12\python.exe')
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $candidates.Add($cmd.Source) }
    $tried = @{}
    foreach ($c in $candidates) {
        $exe = Resolve-ExePath $c
        if (-not $exe -or $tried.ContainsKey($exe)) { continue }
        $tried[$exe] = $true
        # Invoke directly so PowerShell does not split the Python -c payload
        # into separate arguments (Start-Process caused valid interpreters to
        # be rejected during the one-click startup check).
        & $exe '-c' 'import torch, fastapi, uvicorn' 1>$null 2>$null
        if ($LASTEXITCODE -eq 0) { return $exe }
        Write-Host "[warn] $exe 无法 import torch/fastapi/uvicorn，尝试下一个候选" -ForegroundColor Yellow
    }
    return $null
}

$pythonExe = $null
if ($Action -in @('start','restart')) {
    $pythonExe = Resolve-Python
    if (-not $pythonExe) {
        Write-Host '[fail] 未找到可用 Python（需 import torch/fastapi/uvicorn）。请设置 GDYOLO_PYTHON 指向正确解释器后重试。' -ForegroundColor Red
        exit 1
    }
    Write-Host "[info] python = $pythonExe" -ForegroundColor Cyan
    if ($Precheck) {
        Write-Host '[info] 运行部署自检 scripts/doctor.py ...' -ForegroundColor Cyan
        & $pythonExe (Join-Path $projectDir 'scripts/doctor.py')
        if ($LASTEXITCODE -ne 0) {
            Write-Host '[fail] doctor 自检未通过，已中止启动。请按上方 FAIL 项修复后重试。' -ForegroundColor Red
            exit 1
        }
        Write-Host '[info] doctor 自检通过' -ForegroundColor Green
    }
}

$services = @(
    @{name='console';port=8010;url='http://127.0.0.1:8010/api/health';exe=$pythonExe;args=@('-m','uvicorn','agent.api_web:standalone','--host','127.0.0.1','--port','8010');cwd=$projectDir},
    @{name='agent';port=8000;url='http://127.0.0.1:8000/health';exe=$pythonExe;args=@('-m','uvicorn','agent.service:app','--host','127.0.0.1','--port','8000');cwd=$projectDir},
    @{name='frontend';port=5173;url='http://127.0.0.1:5173/';exe='npm.cmd';args=@('run','dev','--','--host','127.0.0.1','--port','5173','--strictPort');cwd=(Join-Path $projectDir 'frontend')}
)
function Healthy([string]$url) {
    try { return (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { return $false }
}
function Wait-Healthy([string]$url, [int]$timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Healthy $url) { return $true }
        Start-Sleep -Seconds 2
    }
    return (Healthy $url)
}

# --- 读取 PID 跟踪，剔除已死进程 ---
$tracked = @()
if (Test-Path -LiteralPath $statePath) {
    try {
        # ConvertFrom-Json returns a nested object for some older state files.
        # Normalize and discard malformed entries before any Get-Process call.
        $rawTracked = @(Get-Content $statePath -Raw | ConvertFrom-Json)
        foreach ($item in $rawTracked) {
            if ($item -and $item.name -and ($item.pid -as [int])) {
                $tracked += [pscustomobject]@{name=[string]$item.name; pid=[int]$item.pid}
            }
        }
    } catch { $tracked = @() }
}
$alive = @()
foreach ($item in $tracked) {
    if (Get-Process -Id $item.pid -ErrorAction SilentlyContinue) { $alive += $item }
}
$tracked = $alive

if ($Action -in @('stop','restart')) {
    foreach ($item in $tracked) {
        $p = Get-Process -Id $item.pid -ErrorAction SilentlyContinue
        if ($p) {
            Write-Host "[stop] $($item.name) (pid $($item.pid))" -ForegroundColor Yellow
            Stop-Process -Id $p.Id -Force
        }
    }
    # 端口兜底：清理未被跟踪到的监听进程（防上次异常退出后的僵尸占用）
    foreach ($svc in $services) {
        $conn = Get-NetTCPConnection -LocalPort $svc.port -State Listen -ErrorAction SilentlyContinue
        if ($conn) {
            foreach ($c in $conn) {
                $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
                if ($p) {
                    Write-Host "[stop] 端口 $($svc.port) 兜底清理 pid $($p.Id) ($($p.ProcessName))" -ForegroundColor Yellow
                    Stop-Process -Id $p.Id -Force
                }
            }
        }
    }
    $tracked = @()
    @() | ConvertTo-Json | Set-Content $statePath -Encoding UTF8
    if ($Action -eq 'stop') { Write-Host '[ok] 全部服务已停止'; exit 0 }
}

if ($Action -in @('start','restart')) {
    foreach ($svc in $services) {
        if (Healthy $svc.url) {
            Write-Host "[skip] $($svc.name) 已在运行 $($svc.url)" -ForegroundColor Green
            continue
        }
        $listener = Get-NetTCPConnection -LocalPort $svc.port -State Listen -ErrorAction SilentlyContinue
        if ($listener) { throw "端口 $($svc.port) 被占用但 $($svc.name) 健康检查失败。请先执行 stop 或手动清理占用进程。" }
        $proc = Start-Process -FilePath $svc.exe -ArgumentList $svc.args -WorkingDirectory $svc.cwd -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir "$($svc.name).out.log") -RedirectStandardError (Join-Path $logDir "$($svc.name).err.log")
        $tracked += [pscustomobject]@{name=$svc.name;pid=$proc.Id}
        Write-Host "[start] $($svc.name) pid $($proc.Id) -> $($svc.url)" -ForegroundColor Cyan
    }
    $tracked | ConvertTo-Json | Set-Content $statePath -Encoding UTF8
    # 模型/RAG 加载较慢，逐服务轮询等待就绪
    foreach ($svc in $services) {
        if (Healthy $svc.url) { continue }
        $timeout = if ($svc.name -eq 'frontend') { 60 } else { 120 }
        $ok = Wait-Healthy $svc.url $timeout
        $color = if ($ok) { 'Green' } else { 'Red' }
        Write-Host "[wait] $($svc.name) $(if ($ok) {'READY'} else {'TIMEOUT'}) $($svc.url)" -ForegroundColor $color
    }
}

# --- 收尾健康报告（status 动作也走这里）---
$bad = 0
foreach ($svc in $services) {
    $ok = Healthy $svc.url
    if (-not $ok) { $bad++ }
    $pidInfo = (@($tracked | Where-Object { $_.name -eq $svc.name } | ForEach-Object { "pid=$($_.pid)" }) -join ' ')
    Write-Host "$($svc.name): $(if ($ok) {'OK'} else {'DOWN'}) $($svc.url) $pidInfo"
}
if ($tracked.Count -gt 0) { $tracked | ConvertTo-Json | Set-Content $statePath -Encoding UTF8 }
Write-Host "logs: $logDir"
if ($Action -eq 'status') { exit $(if ($bad -gt 0) { 1 } else { 0 }) }
if ($bad -gt 0) {
    Write-Host '[warn] 有服务未就绪，请查看 results/debug_stack/ 下日志' -ForegroundColor Yellow
    exit 1
}
Write-Host '[ok] 三件套全部就绪：控制台 http://127.0.0.1:8010/console ，前端 http://127.0.0.1:5173/ ，任务服务 http://127.0.0.1:8000/docs' -ForegroundColor Green
