# POFF Windows 一键构建：生成单文件 dist\Poff.exe
#
# 用法：  .\build_windows.ps1
# 跳过依赖安装：  .\build_windows.ps1 -SkipDeps
param(
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host '[POFF] 检查 Python...' -ForegroundColor Cyan
python --version
if ($LASTEXITCODE -ne 0) { throw '未找到 python，请安装 Python 3.10+ 并加入 PATH' }

if (-not $SkipDeps) {
    Write-Host '[POFF] 安装依赖...' -ForegroundColor Cyan
    python -m pip install --upgrade pip wheel setuptools
    python -m pip install -r requirements.txt
}

Write-Host '[POFF] 语法检查...' -ForegroundColor Cyan
python -m py_compile poff.py
Get-ChildItem poff_mods -Filter *.py | ForEach-Object { python -m py_compile $_.FullName }

Write-Host '[POFF] 清理旧构建...' -ForegroundColor Cyan
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host '[POFF] PyInstaller 打包...' -ForegroundColor Cyan
pyinstaller --clean --noconfirm POFF.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }

$out = Join-Path $PSScriptRoot 'dist\Poff.exe'
if (-not (Test-Path $out)) { throw "构建失败：未生成 $out" }

$size = [math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host "[OK] 构建完成：$out（$size MB）" -ForegroundColor Green
