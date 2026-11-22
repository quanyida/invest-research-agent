# 启动项目内置 PostgreSQL（127.0.0.1:55432）
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pg = Join-Path $root '.pg'
$bin = Join-Path $pg 'pgsql\bin'
$data = Join-Path $pg 'data'

if (-not (Test-Path (Join-Path $bin 'postgres.exe'))) { throw "PostgreSQL 未安装：$bin" }

& (Join-Path $bin 'pg_isready.exe') -h 127.0.0.1 -p 55432 *> $null
if ($LASTEXITCODE -eq 0) { Write-Host "Postgres 已在运行：127.0.0.1:55432"; exit 0 }

Start-Process -FilePath (Join-Path $bin 'postgres.exe') `
  -ArgumentList '-D', $data, '-p', '55432' `
  -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $pg 'pg.out') `
  -RedirectStandardError (Join-Path $pg 'pg.err')

for ($i = 0; $i -lt 20; $i++) {
  Start-Sleep -Milliseconds 500
  & (Join-Path $bin 'pg_isready.exe') -h 127.0.0.1 -p 55432 *> $null
  if ($LASTEXITCODE -eq 0) { Write-Host "Postgres 已启动：127.0.0.1:55432"; exit 0 }
}
throw "启动失败，查看 $pg\pg.err"