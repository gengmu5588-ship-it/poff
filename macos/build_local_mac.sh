#!/bin/bash
set -euo pipefail

# POFF macOS 本地构建脚本（仅生成 POFF.app）
#
# 功能：
#   1. 检查 macOS / Python
#   2. 创建并复用本地虚拟环境
#   3. 安装构建依赖
#   4. 使用 POFF.spec（优先）或 poff.py 构建 POFF.app
#   5. 使用 ad-hoc 签名（无需 Apple Developer 账号）
#   6. 验证签名
#
# 最终产物：
#   dist/POFF.app
#
# 用法：
#   chmod +x build_local_mac.sh
#   ./build_local_mac.sh
#
# 可选：
#   SKIP_DEPS=1 ./build_local_mac.sh

APP_NAME="POFF"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv-macos}"
SKIP_DEPS="${SKIP_DEPS:-0}"

APP_PATH="$ROOT_DIR/dist/${APP_NAME}.app"

info() {
  printf '\033[1;34m[POFF]\033[0m %s\n' "$1"
}

ok() {
  printf '\033[1;32m[OK]\033[0m %s\n' "$1"
}

warn() {
  printf '\033[1;33m[WARN]\033[0m %s\n' "$1"
}

fail() {
  printf '\033[1;31m[ERROR]\033[0m %s\n' "$1" >&2
  exit 1
}

if [[ "$(uname -s)" != "Darwin" ]]; then
  fail "build_local_mac.sh 只能在 macOS 上运行。"
fi

command -v python3 >/dev/null 2>&1 || fail "未找到 python3，请先安装 Python 3。"
command -v codesign >/dev/null 2>&1 || fail "未找到 codesign，请先安装 Xcode Command Line Tools。"

if [[ ! -f "$ROOT_DIR/poff.py" ]]; then
  fail "未找到 $ROOT_DIR/poff.py，请把脚本放在 POFF 工程根目录。"
fi

info "工程目录：$ROOT_DIR"
info "输出目录：$ROOT_DIR/dist"

# ------------------------------------------------------------
# Python 虚拟环境
# ------------------------------------------------------------
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  info "创建 Python 虚拟环境：$VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
PYINSTALLER="$VENV_DIR/bin/pyinstaller"

if [[ "$SKIP_DEPS" != "1" ]]; then
  info "更新 pip / wheel / setuptools..."
  "$PYTHON" -m pip install --upgrade pip wheel setuptools

  if [[ -f "$ROOT_DIR/requirements-macos.txt" ]]; then
    info "安装 requirements-macos.txt..."
    "$PIP" install -r "$ROOT_DIR/requirements-macos.txt"
  fi

  if [[ ! -x "$PYINSTALLER" ]]; then
    info "安装 PyInstaller..."
    "$PIP" install pyinstaller
  fi
else
  warn "已设置 SKIP_DEPS=1，跳过依赖安装。"
fi

[[ -x "$PYINSTALLER" ]] || fail "PyInstaller 不可用。"

# ------------------------------------------------------------
# 清理旧构建
# ------------------------------------------------------------
info "清理旧构建..."
rm -rf "$ROOT_DIR/build"
rm -rf "$ROOT_DIR/dist"

# ------------------------------------------------------------
# 构建 .app
# ------------------------------------------------------------
if [[ -f "$ROOT_DIR/POFF.spec" ]]; then
  info "使用 POFF.spec 构建..."
  "$PYINSTALLER" --noconfirm --clean "$ROOT_DIR/POFF.spec"
else
  warn "未找到 POFF.spec，使用 poff.py 直接构建。"

  EXTRA_ARGS=()

  if [[ -f "$ROOT_DIR/poff.icns" ]]; then
    EXTRA_ARGS+=(--icon "$ROOT_DIR/poff.icns")
  fi

  "$PYINSTALLER" \
    --noconfirm \
    --clean \
    --windowed \
    --name "$APP_NAME" \
    "${EXTRA_ARGS[@]}" \
    "$ROOT_DIR/poff.py"
fi

[[ -d "$APP_PATH" ]] || fail "构建失败：未生成 $APP_PATH"
ok "已生成 $APP_PATH"

# ------------------------------------------------------------
# ad-hoc 签名
# ------------------------------------------------------------
info "对 POFF.app 执行本地 ad-hoc 签名..."
codesign \
  --force \
  --deep \
  --sign - \
  "$APP_PATH"

info "验证签名..."
codesign --verify --deep --strict --verbose=2 "$APP_PATH"

ok "ad-hoc 签名验证通过"

echo
echo "============================================================"
echo " POFF.app 构建完成"
echo "============================================================"
echo
echo "产物："
echo "  $APP_PATH"
echo
echo "如需运行："
echo "  open \"$APP_PATH\""
echo
echo "首次使用 KVM / 截屏时，请在："
echo "  系统设置 → 隐私与安全性 → 辅助功能"
echo "  系统设置 → 隐私与安全性 → 输入监控"
echo "  系统设置 → 隐私与安全性 → 屏幕与系统录音"
echo "中为 POFF 授予对应权限。"
echo
