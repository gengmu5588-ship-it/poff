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
$versionMatch = Get-Content 'poff.py' -Raw | Select-String -Pattern 'VERSION\s*=\s*[''"]([^''"]+)[''"]'
if (-not $versionMatch) { throw '未能从 poff.py 读取 VERSION，请确认文件结构' }
$VERSION = $versionMatch.Matches[0].Groups[1].Value
$EXE_NAME = "Poff-$VERSION"                    # 最终 exe 文件名，如 Poff-2.2.1.exe

Write-Host "[POFF] 版本号：$VERSION" -ForegroundColor Cyan
Write-Host "[POFF] 产物目标：dist\$EXE_NAME.exe" -ForegroundColor Cyan

Write-Host '[POFF] 检查 Python...' -ForegroundColor Cyan
# 解释器优先级：环境变量 POFF_PY > 本机 C:\POFFPy（含 tcltk）> PATH 里的 python。
# 注意：有些环境 PATH 上的 python 不带 tkinter，会导致打包出的 exe 启动即崩，
# 所以用 $PY 统一调用，避免误用。
$PY = $env:POFF_PY
if (-not $PY) {
    if (Test-Path 'C:\POFFPy\python.exe') { $PY = 'C:\POFFPy\python.exe' }
    else { $PY = 'python' }
}
Write-Host "[POFF] 使用解释器：$PY" -ForegroundColor Cyan
& $PY --version
if ($LASTEXITCODE -ne 0) { throw "未找到可用的 Python（$PY），请安装 Python 3.10+ 或设置环境变量 POFF_PY" }

if (-not $SkipDeps) {
    Write-Host '[POFF] 安装依赖...' -ForegroundColor Cyan
    & $PY -m pip install --upgrade pip wheel setuptools
    & $PY -m pip install -r requirements.txt
}

Write-Host '[POFF] 语法检查...' -ForegroundColor Cyan
& $PY -m py_compile poff.py
Get-ChildItem poff_mods -Filter *.py | ForEach-Object { & $PY -m py_compile $_.FullName }

Write-Host '[POFF] 清理旧构建...' -ForegroundColor Cyan
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host '[POFF] PyInstaller 打包...' -ForegroundColor Cyan
# spec 的 EXE name 读 POFF_NAME 环境变量，自动生成带版本号的产物名
$env:POFF_NAME = $EXE_NAME
# PyInstaller 把进度日志写到 stderr，在 EAP=Stop 下会被当成异常中断构建，
# 这里临时放宽为 Continue，仅以退出码判定成败。
$oldEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $PY -m PyInstaller --clean --noconfirm POFF.spec 2>&1 | Out-Host
$buildCode = $LASTEXITCODE
$ErrorActionPreference = $oldEAP
if ($buildCode -ne 0) { throw 'PyInstaller 构建失败' }
Remove-Item Env:POFF_NAME -ErrorAction SilentlyContinue

$out = Join-Path $PSScriptRoot "dist\$EXE_NAME.exe"
if (-not (Test-Path $out)) { throw "构建失败：未生成 $out" }

$size = [math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host "[OK] 构建完成：$out（$size MB）" -ForegroundColor Green
