# 停止项目内置 PostgreSQL
$root = Split-Path -Parent $PSScriptRoot
$pg = Join-Path $root '.pg'
$bin = Join-Path $pg 'pgsql\bin'
$data = Join-Path $pg 'data'
$pidFile = Join-Path $data 'postmaster.pid'

if (-not (Test-Path $pidFile)) { Write-Host "Postgres 未在运行"; exit 0 }

& (Join-Path $bin 'pg_ctl.exe') -D $data stop -m fast 2>$null
if ($LASTEXITCODE -eq 0) { Write-Host "已停止（pg_ctl）"; exit 0 }

$pgPid = [int]((Get-Content $pidFile -TotalCount 1).Trim())
Stop-Process -Id $pgPid -Force
Write-Host "已停止 PID $pgPid"