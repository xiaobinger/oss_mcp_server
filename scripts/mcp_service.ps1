<#
.SYNOPSIS
    OSS MCP 服务管理脚本（Windows PowerShell）

.DESCRIPTION
    启动 / 停止 / 重启 / 查看状态 / 查看日志，默认执行 start。

    首次使用：
      1. Copy-Item config\oss_config.example.json config\oss_config.json
      2. 编辑 config\oss_config.json，填写分组、bucket 和 AccessKey
      3. python -m pip install -r requirements.txt

    可选环境变量：
      PYTHON_BIN、OSS_MCP_CONFIG、OSS_MCP_HOST、OSS_MCP_PORT、OSS_MCP_TRANSPORT
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'help')]
    [string]$Command = 'start'
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$ServerScript = Join-Path $ProjectDir 'src\oss_mcp_server.py'
$ConfigFile = if ($env:OSS_MCP_CONFIG) { $env:OSS_MCP_CONFIG } else { Join-Path $ProjectDir 'config\oss_config.json' }
$BindHost = if ($env:OSS_MCP_HOST) { $env:OSS_MCP_HOST } else { '127.0.0.1' }
$Port = if ($env:OSS_MCP_PORT) { [int]$env:OSS_MCP_PORT } else { 8000 }
$Transport = if ($env:OSS_MCP_TRANSPORT) { $env:OSS_MCP_TRANSPORT } else { 'streamable-http' }
$LogDir = Join-Path $ProjectDir 'logs'
$RunDir = Join-Path $ProjectDir 'run'
$PidFile = Join-Path $RunDir 'mcp.pid'
$LogFile = Join-Path $LogDir 'mcp.log'
$OutLogFile = Join-Path $LogDir 'mcp.out.log'

function Resolve-Python {
    if ($env:PYTHON_BIN) {
        $candidate = $env:PYTHON_BIN
        if (-not [System.IO.Path]::IsPathRooted($candidate)) {
            $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
            if ($resolved) { return $resolved.Source }
        }
        return $candidate
    }
    $candidates = @(
        (Join-Path $ProjectDir '.venv\Scripts\python.exe'),
        (Join-Path $ProjectDir 'venv\Scripts\python.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command python3 -ErrorAction SilentlyContinue }
    if ($cmd) { return $cmd.Source }
    return $null
}

$PythonExe = Resolve-Python

function Show-Usage {
    @"
OSS MCP 服务管理脚本（Windows）

用法（在项目根目录执行）：.\scripts\mcp_service.ps1 {start|stop|restart|status|logs}

  start     启动服务（后台运行）
  stop      停止服务
  restart   重启服务
  status    查看运行状态
  logs      实时查看日志（Ctrl+C 退出）

当前配置：
  服务脚本  $ServerScript
  配置文件  $ConfigFile
  监听地址  ${BindHost}:$Port
  传输方式  $Transport
  日志文件  $LogFile

可选环境变量：PYTHON_BIN、OSS_MCP_CONFIG、OSS_MCP_HOST、OSS_MCP_PORT、OSS_MCP_TRANSPORT
"@ | Write-Host
}

function Get-CheckHost {
    if ($BindHost -eq '0.0.0.0' -or $BindHost -eq '') { return '127.0.0.1' }
    if ($BindHost -eq '::') { return '::1' }
    return $BindHost
}

function Test-Port {
    param([string]$TargetHost, [int]$TargetPort)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($TargetHost, $TargetPort, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-ProcessAlive {
    param([int]$ProcessId)
    return ($null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue))
}

function Get-ServicePid {
    if (-not (Test-Path $PidFile)) { return $null }
    $raw = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $parsed = 0
    if (-not [int]::TryParse([string]$raw, [ref]$parsed)) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$parsed" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -and $proc.CommandLine -like '*oss_mcp_server.py*') {
        return $parsed
    }
    return $null
}

function Find-ServiceProcess {
    $servicePid = Get-ServicePid
    if ($servicePid) { return $servicePid }
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like '*oss_mcp_server.py*' -and
            $_.CommandLine -like "*--port $Port*"
        }
    if ($procs) { return @($procs)[0].ProcessId }
    return $null
}

function Show-Url {
    $urlPath = if ($Transport -eq 'sse') { '/sse' } else { '/mcp' }
    $checkHost = Get-CheckHost
    Write-Host "地址：http://${checkHost}:$Port$urlPath（监听 $BindHost，transport=$Transport）"
}

function Show-LogTail {
    if (Test-Path $LogFile) {
        Write-Host "---- 最近日志（$LogFile）----"
        Get-Content $LogFile -Tail 30 -Encoding UTF8 -ErrorAction SilentlyContinue | Write-Host
        Write-Host '------------------------------'
    } else {
        Write-Host "暂无日志文件：$LogFile"
    }
}

function Start-ServiceEx {
    if (-not (Test-Path $ServerScript)) {
        throw "未找到服务脚本：$ServerScript"
    }
    if (-not (Test-Path $ConfigFile)) {
        throw "未找到配置文件：$ConfigFile`n请先执行：Copy-Item `"$ProjectDir\config\oss_config.example.json`" `"$ConfigFile`"，然后填写 AccessKey。"
    }
    if (-not $PythonExe) {
        throw '未找到 Python，请先安装 Python 3，或通过 PYTHON_BIN 环境变量指定解释器。'
    }

    $existingPid = Find-ServiceProcess
    if ($existingPid) {
        Write-Host "服务已在运行（PID $existingPid）。" -ForegroundColor Yellow
        Show-Url
        return $true
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue

    if (Test-Port (Get-CheckHost) $Port) {
        throw "端口 $Port 已被其他进程占用，请更换 OSS_MCP_PORT 或先释放端口。"
    }

    & $PythonExe -c "import oss2, mcp" 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "当前 Python 缺少依赖：$PythonExe`n请执行：`"$PythonExe`" -m pip install oss2 mcp requests"
    }

    New-Item -ItemType Directory -Force -Path $LogDir, $RunDir | Out-Null
    if (Test-Path $LogFile) { Move-Item -Force $LogFile "$LogFile.prev" }
    if (Test-Path $OutLogFile) { Move-Item -Force $OutLogFile "$OutLogFile.prev" }

    $argLine = '"{0}" --config "{1}" --transport {2} --host {3} --port {4}' -f `
        $ServerScript, $ConfigFile, $Transport, $BindHost, $Port

    $oldIoEncoding = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        $process = Start-Process -FilePath $PythonExe -ArgumentList $argLine `
            -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $OutLogFile -RedirectStandardError $LogFile
    } finally {
        if ($null -eq $oldIoEncoding) {
            Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
        } else {
            $env:PYTHONIOENCODING = $oldIoEncoding
        }
    }

    Set-Content -Path $PidFile -Value $process.Id -Encoding ASCII

    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            Remove-Item $PidFile -ErrorAction SilentlyContinue
            Write-Host "启动失败，最近日志：" -ForegroundColor Red
            Show-LogTail
            return $false
        }
        if (Test-Port (Get-CheckHost) $Port) {
            Write-Host "服务已启动（PID $($process.Id)）。" -ForegroundColor Green
            Show-Url
            return $true
        }
        Start-Sleep -Milliseconds 500
    }

    Write-Host "进程已启动（PID $($process.Id)），但端口 $Port 在 20 秒内未就绪。" -ForegroundColor Yellow
    Show-LogTail
    return $false
}

function Stop-ServiceEx {
    $servicePid = Find-ServiceProcess
    if (-not $servicePid) {
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        if (Test-Port (Get-CheckHost) $Port) {
            Write-Host "未找到本服务的进程记录，但端口 $Port 仍被占用，请手动检查。" -ForegroundColor Yellow
            return $false
        }
        Write-Host '服务未运行。'
        return $true
    }

    Write-Host "正在停止服务（PID $servicePid）..."
    Stop-Process -Id $servicePid -Force -ErrorAction SilentlyContinue

    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Test-ProcessAlive $servicePid)) {
        Start-Sleep -Milliseconds 300
    }
    if (Test-ProcessAlive $servicePid) {
        Write-Host '进程未在 10 秒内退出，请手动结束。' -ForegroundColor Red
        return $false
    }

    Remove-Item $PidFile -ErrorAction SilentlyContinue
    Write-Host '服务已停止。' -ForegroundColor Green
    return $true
}

function Show-Status {
    $servicePid = Find-ServiceProcess
    if ($servicePid) {
        Write-Host '状态：运行中' -ForegroundColor Green
        Write-Host "PID ：$servicePid"
        Show-Url
        Write-Host "配置：$ConfigFile"
        Write-Host "日志：$LogFile"
        return $true
    }

    Write-Host '状态：未运行' -ForegroundColor Yellow
    if (Test-Port (Get-CheckHost) $Port) {
        Write-Host "注意：端口 $Port 被其他进程占用。" -ForegroundColor Yellow
    }
    Write-Host "配置：$ConfigFile"
    Write-Host "日志：$LogFile"
    return $false
}

function Show-Logs {
    if (-not (Test-Path $LogFile)) {
        Write-Host "暂无日志文件：$LogFile（服务可能尚未启动）"
        return
    }
    Get-Content $LogFile -Tail 100 -Wait -Encoding UTF8
}

try {
    if ($Transport -notin @('streamable-http', 'sse')) {
        if ($Transport -eq 'stdio') {
            throw 'stdio 模式由 MCP 客户端直接拉起，不能作为后台服务运行；请使用 streamable-http 或 sse。'
        }
        throw "不支持的 transport：$Transport（可选 streamable-http / sse）"
    }

    switch ($Command) {
        'start' {
            if (Start-ServiceEx) { exit 0 } else { exit 1 }
        }
        'stop' {
            if (Stop-ServiceEx) { exit 0 } else { exit 1 }
        }
        'restart' {
            if (-not (Stop-ServiceEx)) { exit 1 }
            if (Start-ServiceEx) { exit 0 } else { exit 1 }
        }
        'status' {
            if (Show-Status) { exit 0 } else { exit 1 }
        }
        'logs' {
            Show-Logs
            exit 0
        }
        default {
            Show-Usage
            exit 0
        }
    }
} catch {
    Write-Host "错误：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
