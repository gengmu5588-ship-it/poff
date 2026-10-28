#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "ERROR: POFF.app 必须在 macOS 上构建。PyInstaller 不能在 Linux/Windows 交叉生成 macOS App。"
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "ERROR: 未找到 python3。建议安装 Python 3.12（python.org 官方安装包）。"
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("需要 Python 3.10+，推荐 3.12")
print("Python:", sys.version)
PY

# tkinter 是 POFF GUI 的基础。python.org 版本通常自带；某些 Homebrew Python 需要额外 Tcl/Tk。
if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import tkinter
root = tkinter.Tk(); root.withdraw(); root.destroy()
PY
then
  echo "ERROR: 当前 Python 缺少可用的 tkinter/Tk。"
  echo "建议安装 python.org 的 Python 3.12，或为 Homebrew Python 配置 Tcl/Tk 后再构建。"
  exit 1
fi

VENV=.venv-mac
"$PYTHON_BIN" -m venv "$VENV"
source "$VENV/bin/activate"
python -m pip install --upgrade pip wheel setuptools
python -m pip install -r requirements-macos.txt

# 在真正打包前先做语法检查和关键模块导入检查。
python -m py_compile poff.py poff_mods/*.py
python - <<'PY'
import Quartz
from PIL import ImageGrab
import pynput
import pystray
import qrcode
import tkinterdnd2
print("macOS dependencies: OK")
PY

rm -rf build dist
pyinstaller --clean --noconfirm POFF.spec

# 本机使用先做 ad-hoc 签名，减少 bundle 内嵌二进制的签名问题。
codesign --force --deep --sign - dist/POFF.app
codesign --verify --deep --strict --verbose=2 dist/POFF.app

echo
echo "构建完成：$(pwd)/dist/POFF.app"
echo "首次使用 KVM：系统设置 > 隐私与安全性 > 辅助功能 / 输入监控"
echo "首次截屏：系统设置 > 隐私与安全性 > 屏幕与系统录音"
echo "如需 DMG：./make_dmg.sh"
