# 一键启动本地开发环境：依赖容器 -> Python 依赖 -> 数据库初始化 -> Web 服务
# 用法：.\scripts\dev.ps1
# 可选：-SkipDocker（已有 PostgreSQL/Redis）、-SkipInstall（依赖已装好）、-Port 8080
param(
    [switch]$SkipDocker,
    [switch]$SkipInstall,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host '已从 .env.example 创建 .env；需要问答时请填写 LLM_API_KEY。'
}

if (-not $SkipDocker) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw '未找到 docker。请安装 Docker Desktop，或自行启动 PostgreSQL 后加 -SkipDocker。'
    }
    Write-Host '启动 PostgreSQL / Redis …'
    docker compose up -d postgres redis
    if ($LASTEXITCODE -ne 0) {
        throw 'docker compose 启动失败，请确认 Docker Desktop 正在运行。'
    }
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw '未找到 python。请安装 Python 3.12 并加入 PATH。'
    }
    Write-Host '创建虚拟环境 .venv …'
    python -m venv .venv
}

if (-not $SkipInstall) {
    Write-Host '安装/校验 Python 依赖 …'
    & $python -m pip install --disable-pip-version-check -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败。' }
}

Write-Host '初始化数据库（可重复执行）…'
& $python scripts\bootstrap_db.py
if ($LASTEXITCODE -ne 0) { throw '数据库初始化失败。' }

Write-Host ''
Write-Host "服务地址：http://127.0.0.1:$Port/ui/"
Write-Host '按 Ctrl+C 停止。'
# 单进程开发：Worker 跑在 API 进程内。生产用 api_main(8000) + agent_service_main(8001) 两个进程。
$env:RUN_WORKER_ENABLED = 'true'
& $python -m uvicorn app.api_main:app --host 127.0.0.1 --port $Port