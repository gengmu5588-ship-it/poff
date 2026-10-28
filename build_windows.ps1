# POFF Windows 一键构建：生成单文件 dist\Poff-X.Y.Z.exe
#
# 版本号来源：自动从 poff.py 读取 VERSION = "X.Y.Z"
# 用法：  .\build_windows.ps1
# 跳过依赖安装：  .\build_windows.ps1 -SkipDeps
param(
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# ---- 从源码读取版本号（单一权威，避免多处维护） ----
$versionMatch = Get-Content 'poff.py' -Raw | Select-String -Pattern 'VERSION\s*=\s*"([^"]+)"'
if (-not $versionMatch) { throw '未能从 poff.py 读取 VERSION，请确认文件结构' }
$VERSION = $versionMatch.Matches[0].Groups[1].Value
$EXE_NAME = "Poff-$VERSION"                    # 最终 exe 文件名，如 Poff-2.2.1.exe

Write-Host "[POFF] 版本号：$VERSION" -ForegroundColor Cyan
Write-Host "[POFF] 产物目标：dist\$EXE_NAME.exe" -ForegroundColor Cyan

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
# spec 的 EXE name 读 POFF_NAME 环境变量，自动生成带版本号的产物名
$env:POFF_NAME = $EXE_NAME
pyinstaller --clean --noconfirm POFF.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败' }
Remove-Item Env:POFF_NAME -ErrorAction SilentlyContinue

$out = Join-Path $PSScriptRoot "dist\$EXE_NAME.exe"
if (-not (Test-Path $out)) { throw "构建失败：未生成 $out" }

$size = [math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host "[OK] 构建完成：$out（$size MB）" -ForegroundColor Green
