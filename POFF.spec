# -*- mode: python ; coding: utf-8 -*-
"""POFF Windows 单文件打包配置（PyInstaller 6.x）。

用法：
    pyinstaller --clean --noconfirm POFF.spec
产物：
    dist/Poff.exe
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

hiddenimports = [
    'pystray._win32',
    'PIL._tkinter_finder',
]
datas = [('poff.ico', '.')]
binaries = []

for pkg in ('pystray', 'PIL'):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# tkinterdnd2 包含 TkDND 动态库与 Tcl 脚本，不能只收 Python 子模块。
try:
    d, b, h = collect_all('tkinterdnd2')
    datas += d
    binaries += b
    hiddenimports += h
except Exception:
    hiddenimports += collect_submodules('tkinterdnd2')

a = Analysis(
    ['poff.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # macOS 专用依赖不应进入 Windows 包体
    excludes=['Quartz', 'AppKit', 'Foundation', 'objc', 'pynput'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Poff',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon='poff.ico',
    version=None,
)
