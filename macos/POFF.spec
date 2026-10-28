# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, collect_all

hiddenimports = []
datas = []
binaries = []

for pkg in ['pystray', 'pynput', 'qrcode', 'PIL', 'Quartz', 'AppKit', 'Foundation']:
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

# tkinterdnd2 还包含 TkDND 动态库和 Tcl 脚本，不能只收 Python 子模块。
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
    excludes=['winreg'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='POFF',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='POFF',
)

app = BUNDLE(
    coll,
    name='POFF.app',
    icon=None,
    bundle_identifier='com.poff.app',
    info_plist={
        'CFBundleDisplayName': 'POFF',
        'CFBundleName': 'POFF',
        'CFBundleShortVersionString': '2.2-mac',
        'CFBundleVersion': '2.2.0',
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'NSAppleEventsUsageDescription': 'POFF uses System Events only when you explicitly request a local shutdown.',
    },
)
