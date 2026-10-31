'''POFF V2.2　局域网多主机控制工具

V2.2 相对 V2.1：
  1. 手机投递（MobiDrop）：内置 HTTP 服务（端口 17891），手机浏览器扫码即用、
     零安装；电脑投递的文件进入待接收区，手机点按下载（完成后自动清理）
  2. 双向投递：手机在投递页上传文件，落盘到电脑接收目录并弹窗提示
  3. 设备目录支持手机条目（kind=mobile）：手动添加 IP，投递自动分流到待接收区，
     在线状态按投递页活跃 / ICMP 探测，关机指令自动跳过手机

V2.1 相对 V2.0：
  1. 跨屏键鼠漫游开放真实转发：WH_MOUSE_LL/WH_KEYBOARD_LL 钩子 + 持久 TCP 会话
     （KVM_HELLO/KVM_M/KVM_K/KVM_BYE），被控端 SendInput 注入，双端双向互为控制端
  2. 外接显示器亮度：DDC/CI（VCP 0x10）动态生成滑块；平板亮度确认为系统侧控制
  3. 漫游紧急解锁：转发期间 Ctrl+Shift+L 不吞不转发，保证本机热键可解锁退回

V2.0 相对 V1.x：
  1. 界面重构为四区布局：左设备列表 / 右上功能总开关 / 右下功能面板 / 底部运行日志
  2. 新增截屏模块（全屏 / 选区、预览、本地保存、发送到远端）
  3. 新增黑洞悬浮面板（拖拽文件投递）
  4. 新增亮度控制（本机内屏 WMI）、平板副屏 Spacedesk 调度
  5. 新增跨屏键鼠漫游布局编辑器
  6. 保留并沿用 V1.x 的全部修复：应答式连通校验、并发发送、倒计时线程安全、
     仅发起方广播 SHUTDOWN、端口独占、配置持久化、托盘与开机自启、关机前安全预检

权限策略：默认按需提权（asInvoker），避免与开机自启的 UAC 冲突；
需要管理员的功能（Spacedesk 服务）在界面内提供重新提权入口。
'''

import base64
import ctypes
import datetime
import hashlib
import ipaddress
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import (ttk, messagebox)

# poff_mods 里有若干 `from poff import SectionCard, PAD_CARD, ...`。
# 直接运行 poff.py 时本模块的名字是 __main__，Python 找不到 'poff'，
# 会把整个 poff.py 再执行一遍——于是运行期同时存在两份 SectionCard /
# RoundedSurface 类、两份 _TOKENS，isinstance 判断对另一份永远为假，
# poff_mods 里延迟导入的颜色常量也永远停在启动时那套。
# 先登记别名，让 `from poff import ...` 命中同一个模块对象。
if 'poff' not in sys.modules:
    _self_mod = sys.modules.get(__name__)
    _self_base = os.path.basename(str(getattr(_self_mod, '__file__', '') or '')).lower()
    if _self_mod is not None and _self_base in ('', 'poff.py'):
        sys.modules['poff'] = _self_mod

from poff_mods import system_tools as st
from poff_mods import transfer
from poff_mods.blackhole import BlackholeModule
from poff_mods.kvm import RoamModule
from poff_mods.mobidrop import MOBI_PORT, MobiDropModule
from poff_mods.netmon import NetMonitor
from poff_mods.panels import BrightnessModule, TabletModule
from poff_mods.recorder import RecorderModule
from poff_mods.screenshot import ScreenshotModule
from poff_mods.screenshare import ScreenShareModule, SHARE_HTTP_PORT
try:
    from tkinterdnd2 import TkinterDnD
    _DND_OK = True
except Exception:
    TkinterDnD = None
    _DND_OK = False
APP_NAME = 'POFF 局域网多主机控制'
APP_TITLE = 'POFF'
VERSION = '2.4.13'
CODE_APP_TAG = 'poff'
CODE_VERSION = 2
TCP_PORT = 17890
CONNECT_TIMEOUT = 1.0
STARTUP_MODE = '--startup' in sys.argv
AUTOSTART_REG_PATH = 'Software\\Microsoft\\Windows\\CurrentVersion\\Run'
AUTOSTART_REG_NAME = 'Poff'
MSG_HELLO = 'HELLO'
MSG_HELLO_ACK = 'HELLO_ACK'
MSG_START_COUNTDOWN = 'START'
MSG_CANCEL = 'CANCEL'
MSG_SHUTDOWN = 'SHUTDOWN'
MSG_SINGLE_SHUTDOWN = 'SINGLE_SHUTDOWN'
MSG_PRECHECK = 'PRECHECK'
MSG_SHARE = 'SHARE'
PRECHECK_RECV_TIMEOUT = 5.0
PRECHECK_SCAN_TIMEOUT = 8.0
USER_APP_PROCESS_NAMES = ['WINWORD.EXE', 'EXCEL.EXE', 'POWERPNT.EXE', 'ONENOTE.EXE', 'OUTLOOK.EXE', 'ACCESS.EXE', 'MSPUB.EXE', 'WPS.EXE', 'ET.EXE', 'WPP.EXE', 'WPSPP.EXE', 'NOTEPAD.EXE', 'WORDPAD.EXE', 'ACRORD32.EXE', 'ACROBAT.EXE', 'ACROBAT_RW.EXE', 'PHOTOSHOP.EXE', 'ILLUSTRATOR.EXE', 'PAINT.NET.EXE', 'GIMP-2.10.EXE', 'MSPAINT.EXE', 'CODE.EXE', 'PYCHARM64.EXE', 'IDEA64.EXE', 'RIDER64.EXE', 'STUDIO64.EXE', 'CLION64.EXE', 'WEBIDEAS64.EXE', 'NOTEPAD++.EXE', 'SUBLIME_TEXT.EXE', 'CHROME.EXE', 'MSEDGE.EXE', 'FIREFOX.EXE', 'OPERA.EXE', 'BRAVE.EXE', 'WINRAR.EXE', '7Z.EXE']
_TOKENS_DARK = dict(BG='#0B1120', CARD='#141B2D', SOFT='#1B2437', HOVER='#121C31', BORDER='#25324A', TEXT='#E6EBF5', MUTED='#8894A9', ACCENT='#3B82F6', ACCENT_DARK='#2563EB', OK='#22C55E', WARN='#F59E0B', ERR='#EF4444', HEADER_BG='#080C18', LOG_BG='#050810', SW_ON='#22C55E', SW_ON_HOVER='#16A34A', SW_OFF='#2A3650', SW_OFF_HOVER='#374668', SW_FRAME='#3A4A6A', SW_THUMB='#FFFFFF')
_TOKENS_LIGHT = dict(BG='#E8EDF6', CARD='#FFFFFF', SOFT='#DFE7F3', HOVER='#E9EEF8', BORDER='#DCE3ED', TEXT='#1B2434', MUTED='#6B7789', ACCENT='#2F6BFF', ACCENT_DARK='#1E4FD8', OK='#16A34A', WARN='#D97706', ERR='#DC2626', HEADER_BG='#F2F5FA', LOG_BG='#0B1220', SW_ON='#16A34A', SW_ON_HOVER='#12873E', SW_OFF='#E3EAF5', SW_OFF_HOVER='#D3DDEC', SW_FRAME='#C5D0E1', SW_THUMB='#FFFFFF')
_TOKENS = dict(_TOKENS_DARK)
BG = _TOKENS['BG']
CARD = _TOKENS['CARD']
SOFT = _TOKENS['SOFT']
HOVER = _TOKENS['HOVER']
BORDER = _TOKENS['BORDER']
TEXT = _TOKENS['TEXT']
MUTED = _TOKENS['MUTED']
ACCENT = _TOKENS['ACCENT']
ACCENT_DARK = _TOKENS['ACCENT_DARK']
OK = _TOKENS['OK']
WARN = _TOKENS['WARN']
ERR = _TOKENS['ERR']
HEADER_BG = _TOKENS['HEADER_BG']
LOG_BG = _TOKENS['LOG_BG']
SW_ON = _TOKENS['SW_ON']
SW_ON_HOVER = _TOKENS['SW_ON_HOVER']
SW_OFF = _TOKENS['SW_OFF']
SW_OFF_HOVER = _TOKENS['SW_OFF_HOVER']
SW_FRAME = _TOKENS['SW_FRAME']
SW_THUMB = _TOKENS['SW_THUMB']

def _mix(hex_a, hex_b, t):
    '''把两个 #RRGGBB 按比例插值（t=0 取 a，t=1 取 b），用于派生中间色。'''

    a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02X%02X%02X' % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _resolve_theme(theme):
    """theme 可以是 'dark'/'light'/'system'；system 按系统深色模式自动选。"""
    if theme == 'system':
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize')
            val, _ = winreg.QueryValueEx(key, 'AppsUseLightTheme')
            winreg.CloseKey(key)
            return 'dark' if val == 0 else 'light'
        except Exception:
            return 'dark'
    if theme in ('dark', 'light'):
        return theme
    return 'dark'

def _h_padding(widget):
    '''容器左右内边距之和（ttk 的 padding 可以是 1 / 2 / 4 个值）。

    ttk 的 cget('padding') 有时返回元组、有时返回字符串，两种都要认。
    '''

    try:
        raw = widget.cget('padding')
    except Exception:
        return 0
    if isinstance(raw, (tuple, list)):
        parts = list(raw)
    else:
        parts = str(raw).replace('(', ' ').replace(')', ' ').replace(',', ' ').split()
    vals = []
    for v in parts:
        try:
            vals.append(int(float(v)))
        except (TypeError, ValueError):
            return 0
    if not vals:
        return 0
    if len(vals) == 1:
        return 2 * vals[0]
    if len(vals) == 2:
        return 2 * vals[1]
    if len(vals) >= 4:
        return vals[1] + vals[3]
    return 0


def auto_wrap(label, margin=0):
    '''让说明文字的换行宽度跟着容器宽度走。

    这些说明原来写死 wraplength=640/660，而右侧卡片实际只有 ~430px 宽，
    文字被卡片右边缘直接裁掉（"Spacedesk AP…"这类长句最明显）。
    这里绑父容器的 <Configure>，按实时宽度重设 wraplength；容器的
    padding 要一起扣掉，否则文字会正好宽出一个内边距、被裁掉尾巴。

    模块级函数是为了让 poff_mods 里的面板也能直接用（那边只有 app 引用）。
    '''

    host = label.master

    def _fit(_e=None):
        try:
            w = host.winfo_width() - margin - _h_padding(host)
        except tk.TclError:
            return
        if w < 160:
            return
        try:
            label.configure(wraplength=w)
        except tk.TclError:
            pass

    host.bind('<Configure>', _fit, add='+')
    try:
        label.after(80, _fit)
    except tk.TclError:
        pass


def _apply_theme_token_set(theme):
    '''切换全局 _TOKENS 字典（所有模块用 dict 动态查表）。'''

    global BG, CARD, SOFT, HOVER, BORDER, TEXT, MUTED, ACCENT, ACCENT_DARK, OK, WARN, ERR, HEADER_BG, LOG_BG, SW_ON, SW_ON_HOVER, SW_OFF, SW_OFF_HOVER, SW_FRAME, SW_THUMB
    src = _TOKENS_DARK if theme == 'dark' else _TOKENS_LIGHT
    _TOKENS.clear()
    _TOKENS.update(src)
    BG = src['BG']
    CARD = src['CARD']
    SOFT = src['SOFT']
    HOVER = src['HOVER']
    BORDER = src['BORDER']
    TEXT = src['TEXT']
    MUTED = src['MUTED']
    ACCENT = src['ACCENT']
    ACCENT_DARK = src['ACCENT_DARK']
    OK = src['OK']
    WARN = src['WARN']
    ERR = src['ERR']
    HEADER_BG = src['HEADER_BG']
    LOG_BG = src['LOG_BG']
    SW_ON = src['SW_ON']
    SW_ON_HOVER = src['SW_ON_HOVER']
    SW_OFF = src['SW_OFF']
    SW_OFF_HOVER = src['SW_OFF_HOVER']
    SW_FRAME = src['SW_FRAME']
    SW_THUMB = src['SW_THUMB']

FONT_H1 = ('Microsoft YaHei UI', 13, 'bold')
FONT_H2 = ('Microsoft YaHei UI', 11, 'bold')
FONT_BODY = ('Microsoft YaHei UI', 9)
FONT_BODY_BOLD = ('Microsoft YaHei UI', 9, 'bold')
FONT_CAP = ('Microsoft YaHei UI', 8)
FONT_MONO = ('Consolas', 9)
# 日志时间戳：两套主题的日志区底色都是深色（LOG_BG），所以固定用一个
# 中灰蓝。早先这里取 BORDER（深色主题 #25324A），几乎和底色一个亮度，
# 时间戳等于看不见。
LOG_TIME_FG = '#7C8DA8'
PAD_CARD = 16
PAD_INNER = 12
GAP_SEC = 12
GAP_CTRL = 8
HEADER_H = 40
TITLEBAR_H = 34
SIDEBAR_W = 220
ROW_H = 36
CARD_RADIUS = 12
WIN_MARGIN = 26          # 原生投影需要的外扩余量（截图/命中判定用）
_LAYOUT_DEFER = False    # 手动缩放窗口期间为 True：卡片高度自动调整先挂起

class _Win32Frame:
    '''无标题栏窗口的 Windows 原生外框：DWM 细边框 + 投影 + 圆角 + 任务栏收起。

    背景：程序要自绘标题栏，早先的做法是 overrideredirect(True)，但这会
    连带丢掉 DWM 外框（1px 描边、投影、圆角）和窗口的"任务栏身份"——
    任务栏里根本没有这个窗口的按钮，点任务栏图标自然既收不回也唤不出。

    解法：窗口保持普通顶层窗口（有任务栏按钮），只把 WS_CAPTION 摘掉，
    并让 WM_NCCALCSIZE 返回 0，使客户区覆盖整窗——标题栏区域就彻底没了，
    同时 DWM 依旧按"厚边框窗口"给它画描边、投影和圆角，且能正常最小化。

    副作用：摘掉标题栏后 Tk 缓存的非客户区尺寸不会更新，`wm geometry`
    会多算 16px 宽；所以窗口的位置与尺寸统一走 set_rect（SetWindowPos），
    不再经过 Tk 的边框换算。

    关于拖动/缩放：曾尝试把按下转成 WM_NCLBUTTONDOWN，让系统进入自己的
    SC_MOVE / SC_SIZE 模态循环。视觉上确实更顺，但该循环里系统会回调
    Python 窗口过程，形成 ctypes→Python→ctypes 的嵌套，重入三层后
    CPython 直接 fatal（PyEval_RestoreThread / GIL released）；改成跨线程
    发送又拿不到鼠标捕获、循环立刻退出。因此位移仍由 Tk 侧驱动，
    真正要解决的抖动放在"别在 <Configure> 里重建图元"（见 RoundedSurface）。
    '''

    WS_THICKFRAME = 0x00040000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_SYSMENU = 0x00080000
    WS_CAPTION = 0x00C00000
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080
    GWL_STYLE, GWL_EXSTYLE, GWL_WNDPROC = -16, -20, -4
    GWL_STYLE_U = 0xFFFFFFF0           # GWL_STYLE 作为 WPARAM 传入时的无符号形式
    WM_NCCALCSIZE, WM_SETICON = 0x0083, 0x0080
    WM_NCACTIVATE, WM_STYLECHANGING = 0x0086, 0x007C
    WM_GETMINMAXINFO = 0x0024
    SM_CXFRAME, SM_CXPADDEDBORDER = 32, 92
    MONITOR_DEFAULTTONEAREST = 2
    SWP_FRAME = 0x0001 | 0x0002 | 0x0004 | 0x0020
    SWP_NOZORDER_NOACT = 0x0004 | 0x0010
    RDW_INVALIDATE, RDW_UPDATENOW, RDW_ALLCHILDREN = 0x0001, 0x0100, 0x0080

    def __init__(self, root):
        self.root = root
        self.hwnd = None
        self._u = None
        self._proc_ref = None
        self._old_proc = None
        self._theme = None
        self._icon_path = None
        self._alive = False

    def _bind_api(self):
        from ctypes import wintypes as wt
        u = ctypes.windll.user32
        u.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
        u.GetAncestor.restype = wt.HWND
        u.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
        u.SetWindowLongW.restype = ctypes.c_long
        u.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
        u.GetWindowLongPtrW.restype = ctypes.c_void_p
        u.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
        u.SetWindowLongPtrW.restype = ctypes.c_void_p
        u.CallWindowProcW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.CallWindowProcW.restype = ctypes.c_ssize_t
        u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.SetWindowPos.restype = wt.BOOL
        u.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u.SendMessageW.restype = wt.LPARAM
        u.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT,
                                 ctypes.c_int, ctypes.c_int, wt.UINT]
        u.LoadImageW.restype = wt.HANDLE
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u.ShowWindow.restype = wt.BOOL
        u.IsZoomed.argtypes = [wt.HWND]
        u.IsZoomed.restype = wt.BOOL
        # 预先解析，避免在重入的窗口过程里做任何查找/导入
        self._ptr_rect = ctypes.POINTER(wt.RECT)
        self._rect_type = wt.RECT
        self._cast = ctypes.cast

        class STYLESTRUCT(ctypes.Structure):
            _fields_ = [('styleOld', ctypes.c_uint32),
                        ('styleNew', ctypes.c_uint32)]

        self._ptr_style = ctypes.POINTER(STYLESTRUCT)
        u.RedrawWindow.argtypes = [wt.HWND, ctypes.c_void_p, wt.HANDLE, wt.UINT]
        u.RedrawWindow.restype = wt.BOOL
        u.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
        u.InvalidateRect.restype = wt.BOOL

        class MONITORINFO(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_ulong),
                        ('rcMonitor', wt.RECT),
                        ('rcWork', wt.RECT),
                        ('dwFlags', ctypes.c_ulong)]

        class MINMAXINFO(ctypes.Structure):
            _fields_ = [('ptReserved', wt.POINT),
                        ('ptMaxSize', wt.POINT),
                        ('ptMaxPosition', wt.POINT),
                        ('ptMinTrackSize', wt.POINT),
                        ('ptMaxTrackSize', wt.POINT)]

        self._mi_type = MONITORINFO
        self._ptr_mm = ctypes.POINTER(MINMAXINFO)
        u.MonitorFromWindow.argtypes = [wt.HWND, ctypes.c_uint]
        u.MonitorFromWindow.restype = wt.HMONITOR
        u.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
        u.GetMonitorInfoW.restype = wt.BOOL
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u.GetWindowRect.restype = wt.BOOL
        u.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT,
                                            ctypes.c_void_p, wt.UINT]
        u.SystemParametersInfoW.restype = wt.BOOL
        self._u = u

    def attach(self):
        '''在 _build_ui() + update_idletasks() 之后调用一次。'''
        if sys.platform != 'win32':
            return False
        try:
            from ctypes import wintypes as wt
            self._bind_api()
            u = self._u
            self.root.update_idletasks()
            wid = int(self.root.winfo_id())
            hwnd = u.GetAncestor(wid, 2) or wid
            self.hwnd = hwnd

            # 去掉标题栏，但保留 WS_THICKFRAME / WS_MINIMIZEBOX / WS_SYSMENU：
            # 有 THICKFRAME，DWM 才会给窗口画 1px 描边 + 投影 + 圆角；
            # 有 MINIMIZEBOX，点任务栏图标才能真正收起。
            style = u.GetWindowLongW(hwnd, self.GWL_STYLE) & 0xFFFFFFFF
            ex = u.GetWindowLongW(hwnd, self.GWL_EXSTYLE) & 0xFFFFFFFF
            u.SetWindowLongW(hwnd, self.GWL_STYLE,
                             (style & ~self.WS_CAPTION) | self.WS_THICKFRAME
                             | self.WS_MINIMIZEBOX | self.WS_MAXIMIZEBOX | self.WS_SYSMENU)
            u.SetWindowLongW(hwnd, self.GWL_EXSTYLE,
                             (ex | self.WS_EX_APPWINDOW) & ~self.WS_EX_TOOLWINDOW)

            wndproc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT,
                                         wt.WPARAM, wt.LPARAM)
            self._old_proc = u.GetWindowLongPtrW(hwnd, self.GWL_WNDPROC)
            self._proc_ref = wndproc(self._wndproc)
            u.SetWindowLongPtrW(hwnd, self.GWL_WNDPROC,
                                ctypes.cast(self._proc_ref, ctypes.c_void_p))
            self._alive = True

            # 子类化装好后重算框架，NCCALCSIZE 才会生效（客户区 == 整窗）
            u.SetWindowPos(hwnd, None, 0, 0, 0, 0, self.SWP_FRAME)
            self.root.update_idletasks()
            if self._theme:
                self.apply_theme(*self._theme)
            if self._icon_path:
                self.set_icon(self._icon_path)
            return True
        except Exception as e:
            print(f'原生外框初始化失败: {e}')
            return False

    def restyle(self):
        '''重新摘掉标题栏。

        Tk 在 deiconify / 重新 map 时会把窗口样式按自己的记录重设一遍，
        WS_CAPTION 有可能被加回来（标题栏重新出现、客户区缩水）；
        另外 DWM 在窗口框架被重新评估时，偶尔会按"经典"样式把标题栏
        直接画在客户区顶部（一条灰蓝色条，盖住自绘标题栏）。
        这里统一处理：样式里没有 CAPTION 也做一次框架重算 + 非客户区重绘。
        '''

        if not self.hwnd:
            return False
        try:
            u = self._u
            style = u.GetWindowLongW(self.hwnd, self.GWL_STYLE) & 0xFFFFFFFF
            if style & self.WS_CAPTION:
                u.SetWindowLongW(self.hwnd, self.GWL_STYLE,
                                 (style & ~self.WS_CAPTION) | self.WS_THICKFRAME
                                 | self.WS_MINIMIZEBOX | self.WS_MAXIMIZEBOX | self.WS_SYSMENU)
            u.SetWindowPos(self.hwnd, None, 0, 0, 0, 0, self.SWP_FRAME)
            return True
        except Exception:
            return False

    def fit_size(self, w, h):
        '''把窗口调整到指定的客户区尺寸。

        去掉 WS_CAPTION 后 Tk 缓存的非客户区尺寸没更新，`wm geometry`
        会把窗口多撑出 16px 宽；调用方按自己设计好的尺寸调这里即可。
        '''

        if not self.hwnd or not w or not h:
            return False
        return self.set_rect(self.root.winfo_x(), self.root.winfo_y(), w, h)

    def set_rect(self, x, y, w, h):
        '''按"窗口 == 客户区"直接定位定尺，绕开 Tk 的边框换算。'''

        if not self.hwnd:
            return False
        try:
            self._u.SetWindowPos(self.hwnd, None, int(x), int(y),
                                 max(1, int(w)), max(1, int(h)),
                                 self.SWP_NOZORDER_NOACT)
            return True
        except Exception:
            return False

    def detach(self):
        self._alive = False
        if not self.hwnd or not self._old_proc:
            return
        try:
            self._u.SetWindowLongPtrW(self.hwnd, self.GWL_WNDPROC, self._old_proc)
        except Exception:
            pass
        self._old_proc = None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        '''窗口过程：只拦非客户区相关的几条消息，其余全部交回原过程。

        关键点：WM_NCCALCSIZE 必须**两种 wParam 都拦**。
        wParam=TRUE 时 lParam 是 NCCALCSIZEPARAMS（首字段即 rgrc[0]），
        wParam=FALSE 时是 RECT——但两者首地址都是 RECT，可以同一套读法。
        早先只在 wParam 非零时返回 0，窗口尺寸变化 / 从遮挡中恢复时
        系统会走 wParam=FALSE 那条路按默认过程减去标题栏高度，
        表现就是：顶部突然多出一条淡蓝色系统标题栏 + 左上角一个小图标，
        把自绘标题栏整条盖住。

        注意：本函数会在 SendMessage 触发的系统模态循环里被重入调用，
        此时 ctypes 已释放 GIL。函数体内绝对不能出现 import / 模块查找，
        否则会触发 "PyEval_RestoreThread: the GIL is released" 致命错误
        （实测拖动窗口时 100% 崩溃）。因此所有 ctypes 类型在 _bind_api
        里预先解析好，这里只用局部引用。
        '''

        if not self._alive:
            return self._u.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)
        if msg == self.WM_NCCALCSIZE:
            try:
                rect = self._cast(lparam, self._ptr_rect).contents
                if self._u.IsZoomed(hwnd):
                    # 系统最大化时会把窗口矩形向四周各"撑"出一个边框宽度
                    # （典型 8px），让缩放边框落到屏幕外。无标题栏窗口的客户区
                    # == 整窗，于是这 8px 会直接把内容画到相邻显示器上——
                    # 这就是"最大化后程序延伸到副屏"的来源。
                    # 只有矩形确实被撑得比显示器还大时才回缩，否则（已经由
                    # WM_GETMINMAXINFO 限定成工作区）反而会在四周留下空隙。
                    mon, _work = self.monitor_rects(hwnd)
                    if mon and (rect.right - rect.left) > (mon[2] - mon[0]):
                        f = (self._u.GetSystemMetrics(self.SM_CXFRAME)
                             + self._u.GetSystemMetrics(self.SM_CXPADDEDBORDER))
                        rect.left += f
                        rect.top += f
                        rect.right -= f
                        rect.bottom -= f
                return 0
            except Exception:
                pass
        elif msg == self.WM_GETMINMAXINFO:
            # 把"最大化后的尺寸/位置"钉死在窗口当前所在显示器的工作区上。
            # 默认值只按主显示器算，副屏上最大化会跑到主屏；而原生最大化
            # 默认的矩形又比显示器大一圈，两端都会溢出到相邻显示器。
            ret = self._u.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)
            try:
                _, work = self.monitor_rects(hwnd)
                if work:
                    mmi = self._cast(lparam, self._ptr_mm).contents
                    mmi.ptMaxSize.x = work[2] - work[0]
                    mmi.ptMaxSize.y = work[3] - work[1]
                    mmi.ptMaxPosition.x = work[0]
                    mmi.ptMaxPosition.y = work[1]
            except Exception:
                pass
            return ret
        elif msg == self.WM_STYLECHANGING:
            # 只要有人想把 WS_CAPTION 加回来（Tk 的 deiconify / wm 重设样式），
            # 就在这条消息里摘掉它，从源头上杜绝标题栏重现。
            try:
                if (int(wparam) & 0xFFFFFFFF) == self.GWL_STYLE_U:
                    st = self._cast(lparam, self._ptr_style).contents
                    st.styleNew &= ~self.WS_CAPTION
                    return 0
            except Exception:
                pass
        elif msg == self.WM_NCACTIVATE:
            # 返回 TRUE = 已处理，阻止系统重绘非客户区（它只会画出一条 caption）
            return 1
        return self._u.CallWindowProcW(self._old_proc, hwnd, msg, wparam, lparam)

    def repaint(self, deep=True):
        '''强制整窗（含全部子控件）立即重绘。

        窗口用 SetWindowPos 缩小时，Tk 只对"尺寸变了的布局"重排，
        顶部 tab 条这类自绘区域可能沿用它上一帧的像素，表现为
        旧画面叠在新画面上（拖小窗口时最明显）。这里直接让系统
        把所有子窗口标记失效并立刻重画一次。
        '''

        if not self.hwnd:
            return False
        try:
            flags = self.RDW_INVALIDATE | self.RDW_UPDATENOW
            if deep:
                flags |= self.RDW_ALLCHILDREN
            self._u.RedrawWindow(self.hwnd, None, None, flags)
            return True
        except Exception:
            return False

    def apply_theme(self, dark, header_hex, border_hex, text_hex):
        '''DWM 外框配色跟随主题：深色主题用暗边，浅色用浅边。'''
        self._theme = (dark, header_hex, border_hex, text_hex)
        if not self.hwnd:
            return
        try:
            from ctypes import wintypes as wt
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

            def colorref(hexstr):
                h = hexstr.lstrip('#')
                r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
                return ctypes.c_uint(r | g << 8 | b << 16)

            def set_int(attr, value):
                v = ctypes.c_int(value)
                dwm.DwmSetWindowAttribute(self.hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

            def set_color(attr, hexstr):
                v = colorref(hexstr)
                dwm.DwmSetWindowAttribute(self.hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

            set_int(20, 1 if dark else 0)          # 沉浸式暗色
            set_color(35, header_hex)              # 标题栏底色（角色：窗口轮廓底色）
            set_color(36, text_hex)                # 标题文字
            set_color(34, border_hex)              # 1px 描边
            set_int(33, 2)                         # 圆角：2 = 系统默认
        except Exception:
            pass
        # 兜底：极少数时序下（例如主题色刚写入、DWM 又重新评估窗口框架）
        # 系统会按"经典"样式把标题栏直接画在客户区上，表现为顶部一条灰蓝色条。
        # 这里重摘一次 WS_CAPTION 并强制重算框架，把它压回去。
        self.restyle()

    def set_icon(self, path):
        '''按槽位分别加载 16/32/48，避免系统拿 256 帧硬缩到任务栏尺寸而发糊。'''
        from ctypes import wintypes as wt
        self._icon_path = path
        if not self.hwnd:
            return
        try:
            for slot, size in ((0, 16), (2, 16), (1, 48)):
                hicon = self._u.LoadImageW(None, path, 1, size, size, 0x00000010)
                if hicon:
                    self._u.SendMessageW(self.hwnd, self.WM_SETICON, slot, wt.LPARAM(hicon))
        except Exception:
            pass

    def minimize(self):
        '''真正最小化到任务栏（可再点任务栏还原），而不是 hide。'''
        if not self.hwnd:
            return False
        try:
            self._u.ShowWindow(self.hwnd, 6)   # SW_MINIMIZE
            return True
        except Exception:
            return False

    def toggle_max(self):
        '''原生最大化/还原。

        保留以供外部（如未来托盘菜单）使用；主界面窗口按钮走
        PoffApp._win_toggle_max，按工作区尺寸手动铺满，避免盖住任务栏。
        '''

        if not self.hwnd:
            return None
        try:
            maxed = bool(self._u.IsZoomed(self.hwnd))
            self._u.ShowWindow(self.hwnd, 9 if maxed else 3)   # SW_RESTORE / SW_MAXIMIZE
            return not maxed
        except Exception:
            return None

    def is_maxed(self):
        if not self.hwnd:
            return False
        try:
            return bool(self._u.IsZoomed(self.hwnd))
        except Exception:
            return False

    def restore(self):
        '''还原（SW_RESTORE）：用于窗口被系统原生最大化（Win+↑ / 吸附）后复位。'''
        if not self.hwnd:
            return False
        try:
            self._u.ShowWindow(self.hwnd, 9)   # SW_RESTORE
            return True
        except Exception:
            return False

    def get_rect(self):
        '''当前窗口矩形 (x, y, w, h)，虚拟屏幕坐标（副屏可能为负）。'''
        if not self.hwnd:
            return None
        try:
            r = self._rect_type()
            if self._u.GetWindowRect(self.hwnd, ctypes.byref(r)):
                return r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            pass
        return None

    def monitor_rects(self, hwnd=None):
        '''返回 (整屏矩形, 工作区矩形)，均为虚拟屏幕坐标（副屏可能为负）。

        多显示器下必须走 MonitorFromWindow：SPI_GETWORKAREA 只代表**主显示器**。
        笔记本外接显示器、外屏设为主屏时，笔记本屏常常排在主屏左侧（x 为负），
        此时若按"屏幕尺寸"或 SPI_GETWORKAREA 算最大化矩形，窗口会整块跳到
        另一块屏，或者越过边界延伸到副屏上。
        '''

        hwnd = hwnd or self.hwnd
        if not hwnd:
            return None, None
        try:
            mi = self._mi_type()
            mi.cbSize = ctypes.sizeof(self._mi_type)
            hmon = self._u.MonitorFromWindow(hwnd, self.MONITOR_DEFAULTTONEAREST)
            if hmon and self._u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                m, w = mi.rcMonitor, mi.rcWork
                return ((m.left, m.top, m.right, m.bottom),
                        (w.left, w.top, w.right, w.bottom))
        except Exception:
            pass
        return None, None

    def monitor_work_area(self, hwnd=None):
        '''窗口所在显示器的工作区 (x, y, w, h)，已排除任务栏。'''

        _, work = self.monitor_rects(hwnd)
        if work:
            l, t, r, b = work
            if r > l and b > t:
                return l, t, r - l, b - t
        try:
            r = self._rect_type()
            self._u.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
            if r.right > r.left and r.bottom > r.top:
                return r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            pass
        return None


class RoundedSurface(tk.Canvas):
    '''圆角卡片容器：Canvas 自绘圆角矩形，内容挂 self.body（ttk.Frame）。

    用法：
        surface = RoundedSurface(parent, padding=16)
        ttk.Label(surface.body, ...).pack(...)
    高度随内容自增；宽度跟随父容器（pack fill="x"/"both"）。
'''

    def __init__(self, master, padding=12, radius=None, **kw):
        kw.pop('bg', None)
        kw.setdefault('highlightthickness', 0)
        kw.setdefault('bd', 0)
        super().__init__(master, bg=BG, **kw)
        self._radius = CARD_RADIUS if radius is None else radius
        self.body = ttk.Frame(self, style='Card.TFrame', padding=padding)
        self._win = self.create_window((0, 0), window=self.body, anchor='nw')
        self._geo = (0, 0)          # 上一次绘制时的画布尺寸
        self._items = None          # 圆角图元句柄，复用而不重建
        self._fill = CARD
        self._applied_h = 0
        self._want_h = None
        self.bind('<Configure>', self._on_resize)
        self.body.bind('<Configure>', self._on_content)
        self._draw()

    def _on_resize(self, _e=None):
        '''尺寸真的变了才重绘。

        <Configure> 在窗口被拖动（纯位置变化）时也会触发，早先版本无条件
        走一遍 delete('card') + 重建 6 个图元，于是拖窗口时整屏卡片都在
        反复擦除重画——这是"拖动掉帧、内容一直跳动"的主因。
        '''

        w, h = self.winfo_width(), self.winfo_height()
        if (w, h) == self._geo:
            return
        self._geo = (w, h)
        # 只跟宽度：宽度决定文字换行。高度刻意不动——RoundedSurface 靠
        # body 的 <Configure> 反推卡片高度（_on_content），一旦把内容窗口的
        # 高度钉死，文字换行后 body 尺寸不再变化，这个反馈环就断了，卡片会
        # 永久停在旧高度上（表现为按钮被压扁成一条）。
        self.itemconfigure(self._win, width=max(0, w - 2 * self._radius))
        self._draw(w, h)

    def _on_content(self, _e=None):
        if _LAYOUT_DEFER:
            # 拖边缩放时每个 <Configure> 都算一次 reqheight，整棵树会算到
            # 100ms+/帧；先挂起，松手后由 PoffApp 统一补算一次。
            return
        need = self.body.winfo_reqheight() + 2 * self._radius
        if need == self._want_h:
            return
        self._want_h = need
        self.after_idle(self._apply_height)

    def _apply_height(self, *_):
        '''只有目标高度相对"已应用值"变化时才 configure，避免自激循环。'''

        need = self._want_h
        if not need or need == self._applied_h:
            return
        self._applied_h = need
        try:
            self.configure(height=need)
        except tk.TclError:
            pass

    def _draw(self, w=None, h=None):
        if w is None:
            w, h = self.winfo_width(), self.winfo_height()
        r = self._radius
        if w < 2 * r + 2 or h < 2 * r + 2:
            if self._items:
                self.itemconfigure('card', state='hidden')
            return
        if self._items is None:
            kw = dict(fill=CARD, outline='', tags='card')
            self._items = (
                self.create_rectangle(0, 0, 0, 0, **kw),
                self.create_rectangle(0, 0, 0, 0, **kw),
                self.create_arc(0, 0, 1, 1, start=90, extent=90, style='pieslice', **kw),
                self.create_arc(0, 0, 1, 1, start=0, extent=90, style='pieslice', **kw),
                self.create_arc(0, 0, 1, 1, start=270, extent=90, style='pieslice', **kw),
                self.create_arc(0, 0, 1, 1, start=180, extent=90, style='pieslice', **kw),
            )
            self.tag_lower('card')
        it = self._items
        self.coords(it[0], r, 0, w - r, h)
        self.coords(it[1], 0, r, w, h - r)
        self.coords(it[2], 0, 0, 2 * r, 2 * r)
        self.coords(it[3], w - 2 * r, 0, w, 2 * r)
        self.coords(it[4], w - 2 * r, h - 2 * r, w, h)
        self.coords(it[5], 0, h - 2 * r, 2 * r, h)
        self.coords(self._win, r, r)
        self.itemconfigure('card', state='normal')
        if self._fill != CARD:
            self._fill = CARD
            self.itemconfigure('card', fill=CARD)

    def _recolor(self):
        '''主题切换时重设背景并重绘圆角。

        这里要无条件把图元填充色改掉：被 grid_remove 隐藏的页面尺寸是
        1×1，_draw 会走"尺寸太小直接返回"的分支，颜色就永远停在
        构建时那套（深色主题的 CARD）——表现是切到那个标签页时
        整块卡片是一坨深色。
        '''

        self.configure(bg=BG)
        self._fill = CARD
        try:
            self.itemconfigure('card', fill=CARD)
        except tk.TclError:
            pass
        self._draw()

    def _redraw(self):
        '''给 _redraw_all_canvases 统一接口用（同 _recolor）。'''

        self._recolor()


class SectionCard(RoundedSurface):
    '''带标题的圆角卡片：标题行 + 内容容器。

    2.4.8 起去掉标题上方的 accent 顶条：深色/浅色两套界面里它都像一条
    "贴在卡片上沿的蓝胶带"，和整体的留白风格不搭；卡片之间改用间距区分。

    用法：
        card = SectionCard(parent, title="屏幕截屏")
        card.pack(fill="x", ...)
        inner = card.body   # ttk.Frame，在这里 pack 具体控件
'''

    def __init__(self, master, title, accent=None, **kw):
        super().__init__(master, padding=0, **kw)
        self._accent = accent          # 保留参数兼容旧调用，不再绘制
        self.body.configure(padding=(PAD_CARD, PAD_CARD - 6, PAD_CARD, PAD_CARD))
        head = ttk.Frame(self.body, style='Card.TFrame')
        head.pack(fill='x')
        self._title_lbl = tk.Label(head, text=title, bg=CARD, fg=TEXT, font=FONT_H2)
        self._title_lbl.pack(side='left')
        self._header_extra = head

    def pack_extra(self, label_or_widget, side='right'):
        '''给标题行右侧加 Muted 文本或控件。'''

        if isinstance(label_or_widget, str):
            tk.Label(self._header_extra, text=label_or_widget, bg=CARD, fg=MUTED, font=FONT_CAP).pack(side=side)
            return
        label_or_widget.pack(in_=self._header_extra, side=side)

    def _recolor(self):
        '''主题切换时重设 SectionCard 自身及其子元素的颜色。'''

        self.configure(bg=BG)
        self._fill = CARD
        try:
            self.itemconfigure('card', fill=CARD)
        except tk.TclError:
            pass
        try:
            self._title_lbl.configure(bg=CARD, fg=TEXT)
        except tk.TclError:
            pass
        for child in self._header_extra.winfo_children():
            try:
                child.configure(bg=CARD)
            except tk.TclError:
                continue
        self._draw()


class NiceScrollbar(tk.Canvas):
    '''极简竖向滚动条：轨道 CARD 底 + BORDER 色滑块 + 自适应高度。

用法（和 ttk.Scrollbar 一致）：
    sb = NiceScrollbar(parent, command=canvas.yview)
    canvas.configure(yscrollcommand=sb.set)
'''

    def __init__(self, master, command=None, **kw):
        kw.pop('orient', None)
        self._cmd = command
        self._thumb = None
        self._knob = None
        self._knob_y = 0
        self._knob_h = 0
        self._hover = False
        self._lo = 0.0
        self._hi = 1.0
        bg = kw.pop('bg', CARD)
        super().__init__(master, width=8, highlightthickness=0, bd=0, bg=bg, **kw)
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)
        self.bind('<Button-1>', self._on_press)
        self.bind('<B1-Motion>', self._on_drag)

    def _on_enter(self, _e):
        self._hover = True
        self._redraw()

    def _on_leave(self, _e):
        self._hover = False
        self._redraw()

    def set(self, lo, hi):
        self._lo = float(lo)
        self._hi = float(hi)
        self.after_idle(self._redraw)

    def _knob_color(self):
        '''每次 redraw 动态读全局 token（支持主题切换）。'''

        if self._hover:
            return ACCENT
        # 早先用 BORDER：浅色主题下 #DCE3ED 与底色 #E8EDF6 几乎同亮度，
        # 滚动条等于不存在（设置页能滚但用户看不见能滚）。
        return _mix(BG, MUTED, 0.55)

    def _redraw(self):
        self.delete('all')
        w = self.winfo_width() or 8
        h = self.winfo_height() or 80
        if h <= 20:
            return
        # 内容没超出可视区（lo=0, hi=1）时不画滑块：否则设置页这种短页面
        # 右侧会挂一根和页面等高的"竖条"，像多画了一道边框。
        if self._hi - self._lo >= 0.999:
            return
        self._knob_h = max(18, int(h * (self._hi - self._lo)))
        self._knob_y = int(h * self._lo)
        col = self._knob_color()
        self._knob = self.create_rectangle(2, self._knob_y, w - 2, self._knob_y + self._knob_h, fill=col, outline='')

    def _on_press(self, e):
        self._drag_offset = e.y - self._knob_y
        self._on_drag(e)

    def _on_drag(self, e):
        if not self._cmd:
            return
        h = self.winfo_height() or 100
        y = max(0, min(h - self._knob_h, e.y - self._drag_offset))
        self._cmd('moveto', y / h)


def get_data_dir():
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    path = os.path.join(base, 'Poff')
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path

class FlatToggle(tk.Canvas):
    '''扁平化开关：圆角轨道 + 圆形 thumb，跟随 BooleanVar 切换。

设计：
  - 轨道宽 40 × 高 20（紧凑，不抢标签空间）
  - 开态：OK 绿（#16A34A，与日志区一致）；关态：低饱和蓝灰（#E3EAF5）
  - 边框 1px，仅关态可见；开态被绿色填充覆盖
  - thumb 白色正圆，位置由关态左 → 开态右 ease-out 过渡（90ms / 7 帧）
  - hover 颜色加深 + 光标变手型
'''

    W = 40
    H = 20
    PAD = 2
    FRAME = 1
    def __init__(self, master, variable, on_change=None, **kw):
        self._var = variable
        self._on_change = on_change
        self._anim_job = None
        self._hovering = False
        self._thumb_x = None
        kw.setdefault('width', self.W)
        kw.setdefault('height', self.H)
        kw.setdefault('highlightthickness', 0)
        kw.setdefault('bd', 0)
        kw.setdefault('bg', CARD)
        super().__init__(master, **kw)
        self._r = self.H // 2
        self._thumb_r = self._r - self.PAD
        self._off_x = self._r
        self._on_x = self.W - self._r
        self._thumb_y = self._r
        self._body_l = self.create_oval(0, 0, self.H, self.H, outline='', tags='body')
        self._body_m = self.create_rectangle(self._r, 0, self.W - self._r, self.H, outline='', tags='body')
        self._body_r = self.create_oval(self.W - self.H, 0, self.W, self.H, outline='', tags='body')
        self._frame_l = self.create_oval(0, 0, self.H, self.H, outline=SW_FRAME, width=self.FRAME, tags='frame')
        self._frame_m = self.create_rectangle(self._r, 0, self.W - self._r, self.H, outline='', width=1, tags='frame')
        self._frame_r = self.create_oval(self.W - self.H, 0, self.W, self.H, outline=SW_FRAME, width=self.FRAME, tags='frame')
        self._thumb = self.create_oval(0, 0, 1, 1, fill=SW_THUMB, outline='')
        self.bind('<Button-1>', self._on_click)
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)
        self._var.trace_add('write', (lambda *a: self._render(animate=True)))
        self._render(animate=False)

    def _redraw(self):
        self._render(animate=False)

    def _body_color(self):
        if bool(self._var.get()):
            if self._hovering:
                return SW_ON_HOVER
            return SW_ON
        if self._hovering:
            return SW_OFF_HOVER
        return SW_OFF

    def _frame_color(self):
        on = bool(self._var.get())
        if on:
            return self._body_color()
        return SW_FRAME

    def _render(self, animate):
        fill = self._body_color()
        for iid in self._body_l, self._body_m, self._body_r:
            self.itemconfigure(iid, fill=fill)
        frame_c = self._frame_color()
        for iid in self._frame_l, self._frame_r:
            self.itemconfigure(iid, outline=frame_c)
        target = self._on_x if bool(self._var.get()) else self._off_x
        if not animate or self._thumb_x is None:
            self._thumb_x = target
            self._place_thumb(target)
            return
        self._animate_to(target)

    def _thumb_geom(self, cx):
        r = self._thumb_r
        cy = self._thumb_y
        return cx - r, cy - r, cx + r, cy + r

    def _place_thumb(self, cx):
        self._thumb_x = cx
        self.coords(self._thumb, self._thumb_geom(cx))

    def _animate_to(self, target):
        if self._anim_job is not None:
            self.after_cancel(self._anim_job)
        start = self._thumb_x
        delta = target - start
        if abs(delta) < 0.5:
            self._place_thumb(target)
            return
        steps = 7
        step_ms = 13
        self._anim_job = None
        def step(i):
            if i >= steps:
                self._place_thumb(target)
                self._anim_job = None
                return
            t = i / steps
            eased = 1 - (1 - t) ** 2
            self._place_thumb(start + delta * eased)
            self._anim_job = self.after(step_ms, (lambda: step(i + 1)))

        step(0)

    def _on_click(self, _event=None):
        self._var.set(not bool(self._var.get()))
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    def _on_enter(self, _event):
        self._hovering = True
        self.config(cursor='hand2')
        self._render(animate=False)

    def _on_leave(self, _event):
        self._hovering = False
        self.config(cursor='')
        self._render(animate=False)


PEERS_FILE = os.path.join(get_data_dir(), 'peers.json')
CONFIG_FILE = os.path.join(get_data_dir(), 'config.json')

def _resource_path(name):
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

def default_cfg():
    return {'modules': {'screenshot': True, 'brightness': True, 'tablet': True, 'roam': False, 'blackhole': True}, 'dirs': {}, 'blackhole': {'show': True, 'topmost': True, 'auto_hide': True, 'ignore_drag': False, 'open_on_hide': True}, 'roam': {'threshold': 30, 'layout': {}, 'self_role': 'main', 'target_ip': '', 'target_port': TCP_PORT}, 'mobi': {'token': ''}, 'theme': 'system'}

def load_cfg():
    cfg = default_cfg()
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in ('modules', 'dirs', 'blackhole', 'roam', 'mobi'):
                if isinstance(data.get(key), dict):
                    cfg[key].update(data[key])
            # 主题是标量，早先漏在这个白名单外，导致每次启动都被重置成「跟随系统」
            if data.get('theme') in ('dark', 'light', 'system'):
                cfg['theme'] = data['theme']
    except Exception:
        pass
    return cfg

def save_cfg_file(cfg):
    tmp = CONFIG_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        print(f'保存配置失败: {e}')
        return

def resolve_dir(cfg, key, rel_folder):
    '''优先用户自定义目录，其次用户主目录下的默认位置，最后回退到配置目录。'''

    saved = (cfg.get('dirs') or {}).get(key)
    if saved and st.dir_writable(saved):
        return saved
    candidate = os.path.join(os.path.expanduser('~'), rel_folder)
    if st.dir_writable(candidate):
        return candidate
    fallback = os.path.join(get_data_dir(), os.path.basename(rel_folder) or 'files')
    st.ensure_dir(fallback)
    return fallback

def load_saved_peers(local_ip):
    try:
        with open(PEERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return {}
    peers = {}
    for item in data.get('peers', []):
        try:
            ip = str(ipaddress.ip_address(item['ip']))
            kind = item.get('kind', 'pc')
            default_port = MOBI_PORT if kind == 'mobile' else TCP_PORT
            port = int(item.get('port', default_port))
            if 1 <= port <= 65535:
                if ip == local_ip:
                    continue
        except Exception:
            continue
        try:
            peers[ip] = {'port': port, 'ok': False, 'kind': kind,
                         'name': str(item.get('name') or '').strip()[:24]}
        except Exception:
            continue
    return peers

def save_peers(peer_list):
    payload = {'app': CODE_APP_TAG, 'v': CODE_VERSION, 'peers': [{'ip': ip, 'port': info['port'], 'kind': info.get('kind', 'pc'), 'name': info.get('name', '')} for ip, info in peer_list.items()]}
    tmp = PEERS_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PEERS_FILE)
    except OSError as e:
        print(f'保存主机列表失败: {e}')
        return

def _autostart_command():
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}" --startup'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --startup'

def is_autostart_enabled():
    if st.winreg is None:
        return False
    try:
        with st.winreg.OpenKey(st.winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH) as key:
            st.winreg.QueryValueEx(key, AUTOSTART_REG_NAME)
    except OSError:
        return False
    return True

def set_autostart(enable):
    if st.winreg is None:
        return False
    try:
        with st.winreg.CreateKeyEx(st.winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH, 0, st.winreg.KEY_SET_VALUE) as key:
            if enable:
                st.winreg.SetValueEx(key, AUTOSTART_REG_NAME, 0, st.winreg.REG_SZ, _autostart_command())
            else:
                try:
                    st.winreg.DeleteValue(key, AUTOSTART_REG_NAME)
                except FileNotFoundError:
                    pass
    except OSError as e:
        print(f'设置开机自启失败: {e}')
        return False
    return True

def do_local_shutdown():
    '''本机立即关机。不加 /f，避免强杀进程造成数据丢失。'''

    try:
        kwargs = {}
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        subprocess.run(['shutdown', '/s', '/t', '0'], **kwargs)
    except Exception as e:
        print(f'执行关机失败: {e}')
        return

def scan_running_user_apps():
    '''扫描本机高风险应用进程（白名单过滤，避免返回上百个系统进程）。'''

    result = []
    if sys.platform != 'win32':
        return result
    try:
        proc = subprocess.run(['tasklist', '/fo', 'csv', '/nh'], capture_output=True, text=True, errors='replace', timeout=PRECHECK_SCAN_TIMEOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        print(f'预检扫描失败: {e}')
        return result
    try:
        whitelist = {p.lower() for p in USER_APP_PROCESS_NAMES}
        seen = set()
        for line in proc.stdout.splitlines():
            parts = line.strip().split('","')
            if parts:
                raw = parts[0].strip('"').strip()
                key = raw.lower()
                if key in whitelist and key not in seen:
                    seen.add(key)
                    result.append(raw)
    except Exception as e:
        print(f'预检扫描失败: {e}')
    return result

class PoffApp:
    def __init__(self, root):
        self.root = root
        self.frame32 = _Win32Frame(root)
        self.root.title('')
        self.root.minsize(1040, 700)
        self.root.configure(bg=BG)
        self._center_window()
        self.local_ip = self.get_local_ip()
        self.cfg = load_cfg()
        self.screenshot_dir = resolve_dir(self.cfg, 'screenshot', os.path.join('Pictures', 'POFF 截图'))
        self.recording_dir = resolve_dir(self.cfg, 'recording', os.path.join('Videos', 'POFF 录屏'))
        self.receive_dir = resolve_dir(self.cfg, 'receive', os.path.join('Downloads', 'POFF 接收'))
        self.peer_list = load_saved_peers(self.local_ip)
        self.peer_lock = threading.Lock()
        self.server_socket = None
        self.tray_icon = None
        self._tray_hinted = False
        self.hotkey_mgr = None
        self.roam_locked = False
        self.countdown_win = None
        self.countdown_left = 10
        self.countdown_cancelled = False
        self.countdown_is_initiator = False
        self.modules = {}
        self.mod_vars = {}
        self._ui_queue = queue.Queue()
        self.var_autostart = tk.BooleanVar(value=is_autostart_enabled())
        self.var_open_on_hide = tk.BooleanVar(value=bool(self.cfg.get('blackhole', {}).get('open_on_hide', True)))
        self._log_hint = ''
        self._setup_style()
        self._set_window_icon()
        self.root.after(60, self._pump_ui)
        self.mobi = MobiDropModule(self)
        self.screenshot = ScreenshotModule(self)
        self.recorder = RecorderModule(self)
        self.blackhole = BlackholeModule(self)
        self.brightness = BrightnessModule(self)
        self.tablet = TabletModule(self)
        self.roam = RoamModule(self)
        self.screenshare = ScreenShareModule(self)
        self._build_ui()
        self.root.update_idletasks()
        self.frame32.attach()
        self.frame32.fit_size(*getattr(self, '_ideal_size', (0, 0)))
        self._install_resize_edges()
        try:
            build_tokens = dict(_TOKENS)
            saved_theme = self.cfg.get('theme', 'system')
            actual = _resolve_theme(saved_theme)
            _apply_theme_token_set(actual)
            self._apply_titlebar_theme(dark=actual == 'dark')
            self._setup_style()
            self.root.configure(bg=BG)
            self._recolor_all(build_tokens)
            # 复选框是"位图 + 多个颜色选项"的组合控件，光靠 _recolor_all
            # 还不够：图标本身也要按新配色重画一遍，否则浅色主题下仍是
            # 深色底 + 深色边框的旧图。
            self._refresh_check_images()
        except Exception as e:
            print(f'主题初始化失败: {e}')
        self.refresh_peer_ui()
        self.start_server()
        if self.mobi.start():
            self.log(f'手机投递服务已启动：http://{self.local_ip}:{MOBI_PORT}（手机扫码即用）', 'ok')
        self.root.after(3000, self._poll_mobiles)
        self.tray_icon = self._init_tray()
        self._win_state = self.root.state()
        self._root_after(800, self._watch_window_state)
        try:
            if is_autostart_enabled():
                set_autostart(True)
        except Exception:
            pass
        self._apply_module_states()
        try:
            self.net_monitor.start()
        except Exception as e:
            print(f'网络监测启动失败: {e}')
        if self.cfg.get('blackhole', {}).get('show', True) and self.cfg.get('modules', {}).get('blackhole', True):
            try:
                self.blackhole.ensure_panel()
            except Exception as e:
                print(f'黑洞面板初始化失败: {e}')
        if STARTUP_MODE:
            if self.tray_icon is not None:
                self.root.withdraw()
            else:
                self.root.after(200, self.root.iconify)
        self.root.protocol('WM_DELETE_WINDOW', self.on_main_close)
        # 窗口映射后再次应用标题栏色，确保 DWM 属性在窗口句柄完全就绪后生效
        self.root.after(150, lambda: self._apply_titlebar_theme(
            dark=_resolve_theme(self.cfg.get('theme', 'system')) == 'dark'))
        self.log(f'POFF V{VERSION} 已启动，正在监听 {TCP_PORT}（主机）/ {MOBI_PORT}（手机投递）端口', 'ok')
        threading.Thread(target=self._ensure_firewall_rules, daemon=True).start()

    def _ensure_firewall_rules(self):
        '''管理员模式下自动放行 17890（TCP/UDP 主机协议）与 17891（TCP 手机投递）。

        防火墙首次弹窗若被忽略或点错，Windows 会生成 Block 规则，
        导致对端永远"未在线"。以管理员运行时这里先删同名规则再放行，
        幂等可重复执行，让端口连通性不再依赖用户手动点弹窗。
        '''

        if not st.is_admin():
            return
        ok = True
        for proto, port in ('TCP', TCP_PORT), ('UDP', TCP_PORT), ('TCP', MOBI_PORT), ('TCP', SHARE_HTTP_PORT):
            st.run_hidden(['netsh', 'advfirewall', 'firewall', 'delete', 'rule', f'name=POFF LAN {proto} {port}'], timeout=15)
            proc = st.run_hidden(['netsh', 'advfirewall', 'firewall', 'add', 'rule', f'name=POFF LAN {proto} {port}', 'dir=in', 'action=allow', f'protocol={proto}', f'localport={port}', 'profile=any', 'enable=yes'], timeout=15)
            if proc is None or proc.returncode != 0:
                ok = False
        if ok:
            self.ui_call(self.log, '防火墙已自动放行 17890 / 17891 / 17892 端口（管理员模式）', 'ok')
            return
        self.ui_call(self.log, '防火墙自动放行失败，请手动检查防火墙设置', 'warn')

    def ui_call(self, fn, *args):
        '''后台线程 → 主线程：放入队列，由主线程的消息泵统一执行。

        直接从子线程调用 tkinter 的 after/控件方法在 mainloop 未运行时
        会抛 RuntimeError，因此所有跨线程回 UI 的操作都走这里。
        '''

        self._ui_queue.put((fn, args))

    def _pump_ui(self):
        try:
            while True:
                fn, args = self._ui_queue.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    print(f'UI 回调执行失败: {e}')
        except queue.Empty:
            pass
        # 每秒左右巡检一次：外接屏被拔掉 / 显示器布局变了以后，窗口可能整个
        # 落在已经不存在的那块屏的坐标上（看起来就是"程序不见了"或"跑到副屏"）。
        self._tick = getattr(self, '_tick', 0) + 1
        if self._tick % 20 == 0:
            try:
                self._ensure_visible()
            except Exception:
                pass
        try:
            self.root.after(50, self._pump_ui)
        except tk.TclError:
            return

    def _ensure_visible(self):
        '''窗口若已完全落在工作区之外（换屏 / 拔屏 / 改分辨率），夹回可见区域。

        只有"完全不可见"才动手：窗口跨在两块屏之间是正常的，不该被拽回来。
        '''

        r = self.frame32.get_rect()
        wa = self._work_area()
        if not r or not wa:
            return False
        wx, wy, ww, wh = wa
        if (r[0] + r[2] <= wx or r[1] + r[3] <= wy
                or r[0] >= wx + ww or r[1] >= wy + wh):
            self.frame32.set_rect(*self._clamp_rect_to_monitor(r))
            return True
        return False

    def _center_window(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(1180, max(960, sw - 60))
        h = min(860, max(680, sh - 90))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self._ideal_size = (w, h)
        self.root.geometry(f'{w}x{h}+{x}+{y}')
        self.root.minsize(min(1040, w), min(700, h))

    def _set_window_icon(self):
        '''只负责给窗口挂上多尺寸图标；不再用透明图标遮盖。'''

        try:
            self.root.iconbitmap(_resource_path('poff.ico'))
        except Exception:
            pass
        try:
            self.frame32.set_icon(_resource_path('poff.ico'))
        except Exception:
            pass

    # 窗口轮廓色：比卡片描边略深一档，保证在浅色桌面/深色桌面上都能看出边界
    _OUTLINE = {'dark': '#2C3A55', 'light': '#D3DBE8'}

    def _apply_titlebar_theme(self, dark=True):
        '''把主题的明暗与轮廓色交给原生外框（DWM 1px 描边 + 投影 + 圆角）。

        早先版本为了隐藏系统标题栏左侧的 logo，往 ICON_SMALL 塞了一个
        全透明的 16×16 图标——副作用是任务栏和 Alt+Tab 里也变透明，
        任务栏只剩系统对 256 帧硬缩的结果，看起来发糊。现在标题栏本身
        已经不存在，这个遮盖不再需要，图标按槽位换成真实帧。
        '''

        try:
            self.frame32.apply_theme(dark, HEADER_BG,
                                     self._OUTLINE['dark' if dark else 'light'], TEXT)
        except Exception:
            pass

    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        # clam 主题的很多元素（Notebook.client / Treeview.field / Frame.border…）
        # 直接用内置的 #9E9A91 当描边色，在浅色界面上就是一圈脏灰边。
        # 这里把全局默认描边色统一到主题的 BORDER 上，再逐项清零。
        style.configure('.', background=BG, foreground=TEXT, font=('Microsoft YaHei UI', 9),
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        # clam 主题的 Frame 布局里带一个 "Frame.border" 元素，默认 1px 黑色。
        # 不显式清零的话，每个 ttk.Frame 都会描一圈黑边——卡片外侧那圈
        # "脏边"就是这么来的（嵌套几层就叠成几像素的黑框）。
        #
        # 底色给 CARD 而不是 BG：裸露的 ttk.Frame 几乎都出现在卡片内部
        # 做按钮行/状态行的容器，早先沿用页面底色，于是白卡片里横着一条
        # 比卡片暗一档的"按钮条"（录屏、屏幕共享那两个面板最明显）。
        style.configure('TFrame', background=CARD, borderwidth=0, relief='flat', bordercolor=CARD, lightcolor=CARD, darkcolor=CARD)
        style.configure('Bg.TFrame', background=BG, borderwidth=0, relief='flat', bordercolor=BG)
        style.configure('Card.TFrame', background=CARD, borderwidth=0, relief='flat', bordercolor=CARD)
        style.configure('Soft.TFrame', background=SOFT, borderwidth=0, relief='flat', bordercolor=SOFT)
        style.configure('Card.TLabel', background=CARD, foreground=TEXT)
        style.configure('Soft.TLabel', background=SOFT, foreground=TEXT)
        style.configure('Muted.TLabel', background=CARD, foreground=MUTED)
        style.configure('Warn.TLabel', background=CARD, foreground=WARN)
        style.configure('Ok.TLabel', background=CARD, foreground=OK)
        style.configure('Accent.TLabel', background=CARD, foreground=ACCENT, font=('Microsoft YaHei UI', 10, 'bold'))
        style.configure('H2.TLabel', background=CARD, foreground=TEXT, font=('Microsoft YaHei UI', 13, 'bold'))
        style.configure('H3.TLabel', background=CARD, foreground=TEXT, font=('Microsoft YaHei UI', 10, 'bold'))
        style.configure('Badge.TLabel', background=SOFT, foreground=ACCENT, font=('Microsoft YaHei UI', 8, 'bold'), padding=(8, 3))
        style.configure('Card.TButton', padding=(12, 7), background=SOFT, foreground=TEXT, borderwidth=0, focuscolor='', font=('Microsoft YaHei UI', 9))
        style.map('Card.TButton', background=[('active', BORDER), ('pressed', TEXT)], foreground=[('disabled', MUTED)])
        style.configure('TButton', padding=(12, 7), background=SOFT, foreground=TEXT, borderwidth=0, focuscolor='', font=('Microsoft YaHei UI', 9))
        style.map('TButton', background=[('active', BORDER), ('pressed', TEXT)], foreground=[('disabled', MUTED)])
        # 禁用态用「主色混卡片底」的淡色，而不是写死的深色号：
        # 写死 #2A3650 / #3D1F1F 在浅色卡片上就是两块黑疙瘩。
        style.configure('Accent.TButton', background=ACCENT, foreground='#FFFFFF', padding=(14, 9), font=('Microsoft YaHei UI', 9, 'bold'), borderwidth=0)
        style.map('Accent.TButton', background=[('pressed', ACCENT_DARK), ('active', ACCENT_DARK), ('disabled', _mix(ACCENT, CARD, 0.72))], foreground=[('disabled', MUTED)])
        style.configure('Danger.TButton', background=ERR, foreground='#FFFFFF', padding=(12, 8), borderwidth=0)
        style.map('Danger.TButton', background=[('active', '#DC2626'), ('disabled', _mix(ERR, CARD, 0.8))], foreground=[('disabled', MUTED)])
        # 二级入口（「打开显示设置」这类）：不占一整颗按钮，用无底色的链接样式。
        style.configure('Link.TButton', padding=(2, 2), background=CARD, foreground=ACCENT,
                        borderwidth=0, focuscolor='', font=('Microsoft YaHei UI', 9))
        style.map('Link.TButton', background=[('active', CARD), ('pressed', CARD)],
                  foreground=[('active', ACCENT_DARK), ('disabled', MUTED)])
        # clam 默认的 Scale 轨道是米灰色（#B9B4A9 一档），在浅色卡片上像一根脏木条。
        style.configure('Horizontal.TScale', background=ACCENT, troughcolor=SOFT,
                        bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)
        style.map('Horizontal.TScale', background=[('disabled', SW_FRAME)],
                  troughcolor=[('disabled', SOFT)])
        style.configure('TNotebook', background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0),
                        bordercolor=BG, lightcolor=BG, darkcolor=BG)
        # 原生 tab 栏整条隐藏（layout 置空 = tab 宽度为 0）：
        # Tk 8.6 的 Notebook 在放不下 tab 时会自动折成多行，窗口缩小时
        # 就变成几行 tab 互相叠着画，观感极差。tab 改由 _build_tabs 自绘。
        style.layout('TNotebook.Tab', [])
        style.configure('TNotebook.Tab', background=SOFT, foreground=MUTED, padding=(10, 8), borderwidth=0, font=('Microsoft YaHei UI', 9, 'bold'))
        style.map('TNotebook.Tab', background=[('selected', CARD), ('active', BORDER)], foreground=[('selected', ACCENT), ('active', TEXT)])
        style.configure('Peer.Treeview', background=CARD, fieldbackground=CARD, foreground=TEXT, borderwidth=0, rowheight=30, font=('Microsoft YaHei UI', 9))
        style.configure('Peer.Treeview.Heading', background=SOFT, foreground=MUTED, borderwidth=0, relief='flat', font=('Microsoft YaHei UI', 8, 'bold'))
        style.map('Peer.Treeview', background=[('selected', SOFT)], foreground=[('selected', ACCENT)])
        style.configure('TSeparator', background=BORDER)
        style.configure('TCheckbutton', background=CARD, foreground=TEXT, fieldbackground=CARD)
        style.map('TCheckbutton', background=[('active', CARD)])
        style.configure('TEntry', fieldbackground=SOFT, foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure('TPushbutton', padding=(10, 6))
        style.configure('TCombobox', fieldbackground=SOFT, foreground=TEXT, background=SOFT, arrowcolor=MUTED, bordercolor=BORDER)
        style.map('TCombobox', fieldbackground=[('readonly', SOFT)], foreground=[('readonly', TEXT)])
        style.configure('TScrollbar', background=SOFT, troughcolor=CARD, bordercolor=BORDER, arrowcolor=MUTED, darkcolor=SOFT, lightcolor=SOFT, thumbcolor=BORDER)
        style.map('TScrollbar', background=[('active', BORDER), ('pressed', ACCENT)])
        style.configure('TLabelframe', background=CARD, bordercolor=BORDER, darkcolor=BORDER, lightcolor=BORDER)
        style.configure('TLabelframe.Label', background=CARD, foreground=TEXT, font=('Microsoft YaHei UI', 9, 'bold'))

    def _check_images(self):
        '''生成自绘复选框图标（随主题重建）。

        clam 主题的 Checkbutton 指示器是个"✕"：选中时在浅灰小方框里画一个
        深色叉，看着像"禁用 / 关闭"，和界面里其它圆角控件完全不搭。
        这里用 PIL 画一对图标（空圆角框 / 蓝底白勾），塞给 tk.Checkbutton
        当 image/selectimage，选中时自动换图；图标右侧留出 7px 让文字不贴边。
        '''

        key = (CARD, BORDER, ACCENT)
        if getattr(self, '_chk_key', None) == key:
            return self._chk_img
        try:
            from PIL import Image, ImageDraw, ImageTk
        except Exception:
            return None
        size, scale, gap = 15, 3, 7
        box = size * scale
        width = (size + gap) * scale
        radius = int(box * 0.22)
        off = Image.new('RGBA', (width, box), (0, 0, 0, 0))
        ImageDraw.Draw(off).rounded_rectangle(
            [0, 0, box - 1, box - 1], radius=radius, outline=BORDER,
            width=max(2, int(box * 0.1)))
        on = Image.new('RGBA', (width, box), (0, 0, 0, 0))
        pen = ImageDraw.Draw(on)
        pen.rounded_rectangle([0, 0, box - 1, box - 1], radius=radius, fill=ACCENT)
        pen.line([(box * 0.26, box * 0.52), (box * 0.43, box * 0.71), (box * 0.76, box * 0.3)],
                 fill='#FFFFFF', width=max(2, int(box * 0.12)), joint='curve')
        self._chk_img = {
            'off': ImageTk.PhotoImage(off.resize((size + gap, size), Image.LANCZOS)),
            'on': ImageTk.PhotoImage(on.resize((size + gap, size), Image.LANCZOS)),
        }
        self._chk_key = key
        # PhotoImage 被回收后，Tk 里引用了它的控件会变成"image pyimageN
        # doesn't exist"，而且之后对这个控件的任何 configure 都会报错
        # （包括改 bg）。所以旧主题的图标也要留着，不能让它被 GC 掉。
        if not hasattr(self, '_chk_pool'):
            self._chk_pool = []
        self._chk_pool.append(self._chk_img)
        del self._chk_pool[:-3]
        return self._chk_img

    def make_check(self, parent, text, var, command=None, pady=2):
        '''带自绘指示器的复选框（替代 clam 的"✕"样式）。'''

        imgs = self._check_images()
        cb = tk.Checkbutton(parent, text=text, variable=var, command=command,
                            bg=CARD, fg=TEXT, activebackground=CARD, activeforeground=TEXT,
                            selectcolor=CARD, bd=0, highlightthickness=0, anchor='w',
                            font=('Microsoft YaHei UI', 9), cursor='hand2', pady=pady)
        if imgs:
            cb.configure(image=imgs['off'], selectimage=imgs['on'],
                         indicatoron=False, compound='left', padx=0)
        if not hasattr(self, '_checks'):
            self._checks = []
        self._checks.append(cb)
        return cb

    def _refresh_check_images(self):
        '''主题切换后重画复选框图标（旧 PhotoImage 绑的是旧配色）。'''

        imgs = self._check_images()
        for cb in getattr(self, '_checks', []):
            try:
                cb.configure(bg=CARD, activebackground=CARD, selectcolor=CARD,
                             fg=TEXT, activeforeground=TEXT)
                if imgs:
                    cb.configure(image=imgs['off'], selectimage=imgs['on'])
            except tk.TclError:
                pass

    def _build_ui(self):
        '''骨架：左侧导航通顶（与原生标题栏同色连成一体），右侧 = 状态 chips 行 + 4 页主内容。

        页面内容由板块注册表（_page_layout）驱动、流式渲染（_render_page），
        板块可在页面间迁移，迁移后剩余板块自动收拢重排。
        '''

        self._build_titlebar()
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, minsize=SIDEBAR_W, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        sidebar = tk.Frame(body, bg=HEADER_BG, width=SIDEBAR_W)
        # sticky 必须含 'e'：否则侧栏只按自身请求宽度贴左，栅格列宽 220 里
        # 剩下的部分会露出 body 底色，形成一条从标题栏切到底部的竖向缝隙。
        sidebar.grid(row=0, column=0, sticky='nsew')
        sidebar.pack_propagate(False)   # 子项用 pack，用 grid_propagate 是无效的
        self._build_sidebar(sidebar)
        main = tk.Frame(body, bg=BG)
        main.grid(row=0, column=1, sticky='nsew')
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=0)
        main.rowconfigure(1, weight=1)
        header = tk.Frame(main, bg=HEADER_BG, height=HEADER_H)
        header.grid(row=0, column=0, sticky='ew')
        header.pack_propagate(False)
        self._build_header(header)
        pages = tk.Frame(main, bg=BG)
        pages.grid(row=1, column=0, sticky='nsew')
        pages.columnconfigure(0, weight=1)
        pages.rowconfigure(0, weight=1)
        self._pages = {'control': tk.Frame(pages, bg=BG), 'sw': tk.Frame(pages, bg=BG), 'log': tk.Frame(pages, bg=BG), 'set': tk.Frame(pages, bg=BG)}
        for p in self._pages.values():
            p.grid(row=0, column=0, sticky='nsew')
        self._page_layout = {'control': [('hsplit', {'left': ['devices'], 'right': ['tabs', 'actions']})], 'sw': ['switches'], 'log': ['log'], 'set': ['autostart', 'background', 'theme', 'hotkeys', 'about']}
        self._page_scroll = {}
        self._current_page = 'control'
        for key in self._pages:
            self._render_page(key)
        self._nav_select('control')

    EXPAND_SECTIONS = ('tabs', 'log', 'devices')
    # 右列里「快捷操作」是直接 pack 的卡片，而标签页内容自带 PAD_CARD 内缩。
    # 不补同样的内缩，两者左右边线会差 16px，看着就是一列卡片没对齐。
    SECTION_INSET = {'actions': PAD_CARD}
    def _section_devices(self, parent):
        '''板块：局域网设备（列表 + 添加 + 校验）。返回未 pack 的容器，摆放交给 _place_section。'''

        card = SectionCard(parent, title='局域网设备')
        self.lbl_peer_count = tk.Label(card._header_extra, text='0 台', bg=CARD, fg=MUTED, font=FONT_CAP)
        self.lbl_peer_count.pack(side='right', padx=(8, 0))
        left_body = card.body
        add_head = tk.Frame(left_body, bg=CARD)
        add_head.pack(fill='x', pady=(0, GAP_CTRL))
        tk.Label(add_head, text='添加主机', bg=CARD, fg=TEXT, font=FONT_BODY_BOLD).pack(side='left')
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(left_body, textvariable=self.code_var)
        self.code_entry.pack(fill='x')
        self.code_entry.bind('<Return>', (lambda e: self.add_peer()))
        # 备注：多主机时用来区分（客厅台式机 / 书房笔记本…），留空就用 IP 显示
        name_row = tk.Frame(left_body, bg=CARD)
        name_row.pack(fill='x', pady=(GAP_CTRL, 0))
        tk.Label(name_row, text='备注', bg=CARD, fg=MUTED, font=FONT_CAP).pack(side='left')
        self.remark_var = tk.StringVar()
        self.remark_entry = ttk.Entry(name_row, textvariable=self.remark_var)
        self.remark_entry.pack(side='left', fill='x', expand=True, padx=(6, 0))
        self.remark_entry.bind('<Return>', (lambda e: self.add_peer()))

        row_btns = tk.Frame(left_body, bg=CARD)
        row_btns.pack(fill='x', pady=(GAP_CTRL, 0))
        ttk.Button(row_btns, text='生成本机串码', command=self.gen_code).pack(side='left', expand=True, fill='x')
        ttk.Button(row_btns, text='复制串码', command=self.copy_code).pack(side='left', expand=True, fill='x', padx=(GAP_CTRL, 0))
        ttk.Button(left_body, text='添加主机', style='Accent.TButton', command=self.add_peer).pack(fill='x', pady=(GAP_CTRL, 0))
        tk.Frame(left_body, bg=BORDER, height=1).pack(fill='x', pady=(GAP_SEC, GAP_CTRL))
        tree_box = tk.Frame(left_body, bg=CARD)
        tree_box.pack(fill='both', expand=True)
        self.peer_box = ttk.Treeview(tree_box, columns=('name', 'ip', 'state'), show='headings', height=6, style='Peer.Treeview', selectmode='browse')
        self.peer_box.heading('name', text='备注')
        self.peer_box.heading('ip', text='主机地址')
        self.peer_box.heading('state', text='状态')
        self.peer_box.column('name', width=96, minwidth=60, anchor='w', stretch=True)
        self.peer_box.column('ip', width=126, minwidth=96, anchor='w', stretch=False)
        self.peer_box.column('state', width=70, minwidth=62, anchor='center', stretch=False)
        self.peer_box.tag_configure('online', foreground=OK)
        self.peer_box.tag_configure('offline', foreground=MUTED)
        self.peer_box.tag_configure('unknown', foreground=WARN)
        self.peer_box.pack(side='left', fill='both', expand=True)
        sb = NiceScrollbar(tree_box, command=self.peer_box.yview, bg=CARD)
        sb.pack(side='right', fill='y')
        self.peer_box.configure(yscrollcommand=sb.set)
        self.peer_box.bind('<<TreeviewSelect>>', self.on_select_change)
        self.peer_box.bind('<Double-1>', (lambda e: self.check_selected()))
        self.peer_box.bind('<Button-3>', self._peer_menu_popup)
        if sys.platform.startswith('win'):
            self.peer_box.bind('<Button-2>', self._peer_menu_popup)
        bot = tk.Frame(left_body, bg=CARD)
        bot.pack(fill='x', pady=(GAP_SEC, 0))
        ttk.Button(bot, text='全部校验', command=self.check_all).pack(side='left', expand=True, fill='x')
        ttk.Button(bot, text='编辑备注', command=self.edit_peer_remark).pack(side='left', expand=True, fill='x', padx=(GAP_CTRL, 0))
        ttk.Button(bot, text='清空列表', command=self.clear_peers).pack(side='left', expand=True, fill='x', padx=(GAP_CTRL, 0))
        return card

    def _section_tabs(self, parent):
        '''板块：功能区 Notebook（截屏/黑洞/亮度/平板/漫游…）。'''

        outer = tk.Frame(parent, bg=BG)
        self._build_tabs(outer)
        return outer

    def _section_actions(self, parent):
        '''板块：快捷操作（校验 / 单机关机 / 并行关机）。'''

        card = SectionCard(parent, title='快捷操作')
        row = tk.Frame(card.body, bg=CARD)
        row.pack(fill='x')
        self.btn_quick_check = ttk.Button(row, text='校验选中主机', command=self.check_selected)
        self.btn_quick_check.pack(side='left')
        ttk.Button(row, text='单机关机', style='Danger.TButton', command=self.single_shutdown).pack(side='left', padx=(GAP_CTRL, 0))
        ttk.Button(row, text='并行关机', style='Accent.TButton', command=self.start_sync).pack(side='right')
        return card

    def _section_switches(self, parent):
        '''板块：模块开关（上名下说明，右侧 FlatToggle）。'''

        card = SectionCard(parent, title='模块开关')
        box = card.body
        tk.Label(box, text='关闭后对应模块的入口、热键、后台服务一并停用。', bg=CARD, fg=MUTED, font=FONT_CAP, wraplength=500, justify='left').pack(anchor='w', pady=(0, GAP_SEC))
        rows = [('screenshot', '屏幕截屏', 'Alt+Q 选区 / Ctrl+Shift+F 全屏 / 编辑标注 / 发送'), ('recorder', '录屏录音', '选区录屏 / 全屏录屏 / 纯录音 / 发送'), ('brightness', '亮度控制', '本机屏幕亮度 + 外接显示器 DDC/CI 亮度'), ('tablet', '平板副屏', '通过 Spacedesk 把平板当第二屏'), ('roam', '键鼠漫游', '跨屏键鼠切换 + 剪贴板同步'), ('screenshare', '屏幕共享', 'A 发起 / B 接收，MJPEG 实时流'), ('blackhole', '黑洞投递', '悬浮面板拖拽文件到其他主机')]
        mods = self.cfg.get('modules', {})
        for key, name, hint in rows:
            row = tk.Frame(box, bg=CARD)
            row.pack(fill='x', pady=4)
            if key == 'recorder':
                var = self.mod_vars.get('screenshot')
            else:
                var = self.mod_vars.get(key)
                if var is None:
                    var = tk.BooleanVar(value=bool(mods.get(key, True)))
                    self.mod_vars[key] = var
            FlatToggle(row, variable=var, on_change=(lambda k=key: self.on_module_toggle(k))).pack(side='right')
            left = tk.Frame(row, bg=CARD)
            left.pack(side='left', fill='x', expand=True)
            tk.Label(left, text=name, bg=CARD, fg=TEXT, font=FONT_BODY_BOLD).pack(anchor='w')
            tk.Label(left, text=hint, bg=CARD, fg=MUTED, font=FONT_CAP, wraplength=500, justify='left').pack(anchor='w')
            continue
        tk.Frame(box, bg=CARD, height=1).pack(fill='x', pady=(GAP_SEC, GAP_CTRL))
        sync_row = tk.Frame(box, bg=CARD)
        sync_row.pack(fill='x')
        ttk.Button(sync_row, text='应用全部变更', style='Accent.TButton', command=self._sync_all_modules).pack(side='right')
        tk.Label(sync_row, text='开关变更会立即生效并持久化到配置文件', bg=CARD, fg=MUTED, font=FONT_CAP).pack(side='left')
        return card

    def _section_log(self, parent):
        '''板块：运行日志（占满剩余空间）。'''

        card = SectionCard(parent, title='运行日志')
        body = card.body
        wrap = tk.Frame(body, bg=LOG_BG)
        wrap.pack(fill='both', expand=True)
        self.log_text = tk.Text(wrap, height=16, bg=LOG_BG, fg=TEXT, insertbackground='#FFFFFF', relief='flat', wrap='word', font=FONT_MONO, padx=14, pady=10, highlightthickness=1, highlightbackground=BORDER, highlightcolor=BORDER)
        self.log_text.pack(side='left', fill='both', expand=True)
        sb = NiceScrollbar(wrap, command=self.log_text.yview, bg=LOG_BG)
        sb.pack(side='right', fill='y')
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.tag_configure('time', foreground=LOG_TIME_FG)
        self.log_text.tag_configure('info', foreground=TEXT)
        self.log_text.tag_configure('ok', foreground=OK)
        self.log_text.tag_configure('warn', foreground=WARN)
        self.log_text.tag_configure('err', foreground=ERR)
        bot = tk.Frame(body, bg=CARD)
        bot.pack(fill='x', pady=(GAP_SEC, 0))
        ttk.Button(bot, text='清空日志', command=self._clear_log).pack(side='right')
        tk.Label(bot, text=self._log_hint or '正在运行…', bg=CARD, fg=MUTED, font=FONT_CAP).pack(side='left')
        # 日志卡片是页面上唯一 expand 的卡片：它被 pack 撑高后，内容窗口仍停在
        # reqheight，深色日志框只有一半高、下面留一大片白。这里让 Text 的行数
        # 跟着卡片实际高度走（只改 height 请求，钉死不涉及卡片自身的反馈环）。
        def _fit_lines(_e=None):
            try:
                h = card.winfo_height()
            except tk.TclError:
                return
            if h < 120:
                return
            lines = max(8, int((h - 104) / 16))
            if lines != int(self.log_text.cget('height')):
                self.log_text.configure(height=lines)
        card.bind('<Configure>', _fit_lines, add='+')
        return card

    def _section_autostart(self, parent):
        card = SectionCard(parent, title='自启动')
        self.make_check(card.body, '开机自动启动（登录后在托盘常驻）', self.var_autostart,
                        command=self.toggle_autostart).pack(anchor='w')
        tk.Label(card.body, text='首次开启会触发 UAC，允许后下次开机自动常驻。防火墙需允许「专用网络」', bg=CARD, fg=MUTED, font=FONT_CAP, wraplength=560, justify='left').pack(anchor='w', pady=(6, 0))
        return card

    def _section_background(self, parent):
        '''板块：后台行为（最小化 / 关闭后仍在运行时的表现）。'''

        card = SectionCard(parent, title='后台运行')
        self._chk_open_on_hide = self.make_check(
            card.body, '最小化 / 关闭窗口时自动打开黑洞面板',
            self.var_open_on_hide,
            command=lambda: self.set_open_on_hide(self.var_open_on_hide.get()))
        self._chk_open_on_hide.pack(anchor='w')
        hint = tk.Label(card.body, text='最小化或点「×」后程序不会退出：任务栏通知区里的图标一直显示「运行中」，'
                                       '鼠标移到屏幕右边缘即可把文件拖给其他主机。'
                                       '此开关同样可以在托盘图标右键菜单里切换。',
                        bg=CARD, fg=MUTED, font=FONT_CAP, justify='left')
        hint.pack(anchor='w', fill='x', pady=(6, 0))
        auto_wrap(hint)
        return card

    def _section_theme(self, parent):
        '''板块：主题（深色 / 浅色 / 跟随系统，点选即生效）。'''

        card = SectionCard(parent, title='主题')
        self._current_theme_setting = self.cfg.get('theme', 'system')
        theme_options = [('system', '跟随系统', '读取 Windows 深色/浅色设置自动切换'), ('dark', '深色', '夜间模式 · 省电 · 专注'), ('light', '浅色', '日间模式 · 明亮清晰')]
        self._theme_rows = {}
        for key, label, hint in theme_options:
            row = tk.Frame(card.body, bg=CARD, cursor='hand2')
            row.pack(fill='x', pady=4)
            dot = tk.Label(row, text='●', bg=CARD, fg=OK if key == self._current_theme_setting else MUTED, font=FONT_BODY_BOLD)
            dot.pack(side='left', padx=(0, 6))
            row._dot = dot
            left = tk.Frame(row, bg=CARD)
            left.pack(side='left', fill='x', expand=True)
            tk.Label(left, text=label, bg=CARD, fg=TEXT, font=FONT_BODY_BOLD).pack(anchor='w')
            tk.Label(left, text=hint, bg=CARD, fg=MUTED, font=FONT_CAP, wraplength=500, justify='left').pack(anchor='w')
            self._theme_rows[key] = row
            def on_click(e, k=key):
                self._select_theme_option(k)

            def on_enter(e, r=row):
                for w in [r] + list(r.winfo_children()):
                    try:
                        w.configure(bg=HOVER)
                    except Exception:
                        pass
                    for c in w.winfo_children():
                        try:
                            c.configure(bg=HOVER)
                        except Exception:
                            continue

            def on_leave(e, r=row):
                for w in [r] + list(r.winfo_children()):
                    try:
                        w.configure(bg=CARD)
                    except Exception:
                        pass
                    for c in w.winfo_children():
                        try:
                            c.configure(bg=CARD)
                        except Exception:
                            continue

            for w in row, left:
                w.bind('<Button-1>', on_click)
                w.bind('<Enter>', on_enter)
                w.bind('<Leave>', on_leave)
            continue
        return card

    def _select_theme_option(self, key):
        '''点选主题选项：更新 radio dot + 立即切换主题并持久化。'''

        self._current_theme_setting = key
        for k, row in self._theme_rows.items():
            try:
                row._dot.configure(fg=OK if k == key else MUTED)
            except Exception:
                continue
        self.apply_theme(key)

    def _section_hotkeys(self, parent):
        card = SectionCard(parent, title='全局热键')
        rows = [('Alt + Q', '选区截屏'), ('Ctrl + Shift + F', '全屏截屏'), ('Ctrl + Shift + H', '黑洞面板显示/隐藏'), ('Ctrl + Shift + L', '漫游中紧急退回本机'), ('Esc', '关闭弹出窗口')]
        for k, v in rows:
            r = tk.Frame(card.body, bg=CARD)
            r.pack(fill='x', pady=2)
            tk.Label(r, text=k, bg=CARD, fg=ACCENT, font=FONT_BODY_BOLD, width=22, anchor='e').pack(side='left')
            tk.Label(r, text=v, bg=CARD, fg=TEXT, font=FONT_BODY).pack(side='left', padx=(10, 0))
        return card

    def _section_about(self, parent):
        card = SectionCard(parent, title='关于 & 帮助')
        ab = card.body
        info = tk.Frame(ab, bg=CARD)
        info.pack(fill='x')
        tk.Label(info, text=f'POFF V{VERSION}', bg=CARD, fg=TEXT, font=FONT_H1).pack(anchor='w')
        tk.Label(info, text='局域网多主机控制 · 远程关机 · 键鼠漫游 · 文件投递 · 屏幕共享', bg=CARD, fg=MUTED, font=FONT_CAP).pack(anchor='w')
        btns = tk.Frame(ab, bg=CARD)
        btns.pack(fill='x', pady=(GAP_SEC, 0))
        ttk.Button(btns, text='检查更新', command=self._check_update).pack(side='left')
        ttk.Button(btns, text='查看帮助', command=self.show_help).pack(side='left', padx=(GAP_CTRL, 0))
        ttk.Button(btns, text='复制版本号', command=(lambda: self._copy_to_clip(f'POFF V{VERSION}'))).pack(side='left', padx=(GAP_CTRL, 0))
        contact = tk.Frame(ab, bg=CARD)
        contact.pack(fill='x', pady=(GAP_SEC, 0))
        tk.Label(contact, text='如遇问题或需要定制功能，欢迎联系开发者微信 shot2021', bg=CARD, fg=MUTED, font=FONT_CAP, wraplength=560, justify='left').pack(anchor='w')
        tk.Label(ab, text='Copyright © 2025 shot2021 · 基于 Python / Tkinter / OpenCV 构建', bg=CARD, fg=MUTED, font=FONT_CAP).pack(anchor='w', pady=(GAP_SEC, 0))
        return card

    SECTION_BUILDERS = {'devices': _section_devices, 'tabs': _section_tabs, 'actions': _section_actions, 'switches': _section_switches, 'log': _section_log, 'autostart': _section_autostart, 'background': _section_background, 'theme': _section_theme, 'hotkeys': _section_hotkeys, 'about': _section_about}
    def _render_page(self, key):
        '''按注册表渲染页面：纯 pack 流式布局，无绝对定位。

        板块迁移后调用此函数即可让剩余板块自动收拢、间距自动均衡。
        '''

        page = self._pages[key]
        for w in page.winfo_children():
            w.destroy()
        layout = self._page_layout[key]
        if layout and isinstance(layout[0], tuple) and layout[0][0] == 'hsplit':
            _, cols = layout[0]
            wrap = tk.Frame(page, bg=BG)
            wrap.pack(fill='both', expand=True, padx=PAD_CARD, pady=PAD_CARD)
            wrap.columnconfigure(0, minsize=320, weight=0)
            wrap.columnconfigure(1, weight=1)
            wrap.rowconfigure(0, weight=1)
            lc = tk.Frame(wrap, bg=BG)
            lc.grid(row=0, column=0, sticky='nsew', padx=(0, GAP_SEC))
            rc = tk.Frame(wrap, bg=BG)
            rc.grid(row=0, column=1, sticky='nsew')
            for side, col in ('left', lc), ('right', rc):
                names = cols[side]
                for i, name in enumerate(names):
                    expand = name in self.EXPAND_SECTIONS
                    last = i == len(names) - 1
                    self._place_section(col, name, expand=expand,
                                        pady=(0, 0 if last else GAP_SEC),
                                        padx=self.SECTION_INSET.get(name, 0))
            return
        n = len(layout)
        # 含弹性板块的页面（日志）靠 expand 撑满，不能塞进 canvas；
        # 其余页面（开关/设置）内容是固定高度，窗口变矮时会超出可视区，
        # 必须给一个纵向滚动容器，否则最后一张卡片直接被 pack 截断。
        if any(name in self.EXPAND_SECTIONS for name in layout):
            inner = tk.Frame(page, bg=BG)
            # 左右必须与主控页一样留出 PAD_CARD：早先只给了上下留白，
            # 结果开关/日志/设置页的白色圆角卡片直接贴在侧栏右边线上。
            inner.pack(fill='both', expand=True, padx=PAD_CARD)
            padx = 0
        else:
            inner = self._scroll_host(page, key)
            padx = PAD_CARD
        for i, name in enumerate(layout):
            expand = name in self.EXPAND_SECTIONS
            top = PAD_CARD if i == 0 else 0
            bottom = PAD_CARD if i == n - 1 else GAP_SEC
            self._place_section(inner, name, expand=expand, pady=(top, bottom), padx=padx)

    def _scroll_host(self, page, key):
        '''给固定高度内容的页面套一层纵向滚动容器，返回内容容器。

        2.4.7 的设置页四张卡片总高约 850px，窗口一矮就超可视区：Tk 的 pack
        在空间不够时会把最后一棵子树压缩到剩余高度，于是「关于 & 帮助」只剩
        半张卡片，而且整页没有任何滚动条，用户根本滑不动。这里改成
        canvas + 自绘滚动条，滚轮由 _on_tab_wheel 统一分发。
        '''

        host = tk.Frame(page, bg=BG)
        host.pack(fill='both', expand=True)
        canvas = tk.Canvas(host, bg=BG, highlightthickness=0, bd=0)
        inner = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.pack(fill='both', expand=True)
        vsb = NiceScrollbar(host, command=canvas.yview, bg=BG)
        vsb.place(relx=1.0, y=0, anchor='ne', relheight=1.0, width=8)
        canvas.configure(yscrollcommand=vsb.set)
        inner.bind('<Configure>', (lambda e, c=canvas: c.configure(scrollregion=c.bbox('all'))))
        canvas.bind('<Configure>', (lambda e, c=canvas, w=win: c.itemconfigure(w, width=e.width)))
        self._page_scroll[key] = (canvas, inner)
        return inner

    def _place_section(self, parent, name, expand=False, pady=0, padx=0):
        '''把一个板块 pack 进容器（弹性流式，无绝对定位）。'''

        builder = self.SECTION_BUILDERS[name]
        frame = builder(self, parent)
        frame.pack(fill='both' if expand else 'x', expand=expand, pady=pady, padx=padx)
        self._auto_wrap_tree(frame)

    def _auto_wrap_tree(self, widget):
        '''把板块里写死 wraplength 的说明标签统一改成"跟着容器宽度换行"。

        poff_mods 里的面板普遍写着 wraplength=660/640，而右列卡片实际只有
        ~430px，多出来的部分直接被卡片右边缘裁掉（"Spacedesk AP…"、
        "平板亮度由平板系统自…"就是这么断的）。与其逐个改写字面量，
        这里在板块建完后递归扫一遍。
        '''

        for child in widget.winfo_children():
            try:
                if child.winfo_class() == 'TLabel' and int(child.cget('wraplength') or 0) > 0:
                    auto_wrap(child)
            except (tk.TclError, ValueError):
                pass
            self._auto_wrap_tree(child)

    def move_section(self, name, to_page, index=0):
        '''把板块迁移到目标页面（可跨页），两页自动重渲染、剩余板块自动收拢重排。'''

        def pop_from(lst):
            for i, it in enumerate(lst):
                if isinstance(it, tuple) and it[0] == 'hsplit':
                    for side in ('left', 'right'):
                        if not name in it[1][side]:
                            continue
                        it[1][side].remove(name)
                        return True
                elif not it == name:
                    continue
                lst.pop(i)
                return True
            return False

        affected = []
        for pk, lst in self._page_layout.items():
            if pop_from(lst):
                affected.append(pk)
        self._page_layout[to_page].insert(index, name)
        for pk in affected + [to_page]:
            self._render_page(pk)
        self.log(f'板块「{name}」已迁移至页面「{to_page}」，布局已自动重排。', 'info')

    def _build_titlebar(self):
        '''自绘标题栏。

        Windows 10 不提供 DWMWA_CAPTION_COLOR（该属性需 Windows 11 22000+），
        原生标题栏只能跟随系统主题色。因此移除原生标题栏后自绘一条，
        背景取 HEADER_BG，与左侧导航栏、右侧状态行连成一体。
        '''

        bar = tk.Frame(self.root, bg=HEADER_BG, height=TITLEBAR_H)
        bar.pack(fill='x', side='top')
        bar.pack_propagate(False)
        self._titlebar = bar
        self._maxed = False
        self._restore_rect = None

        btns = tk.Frame(bar, bg=HEADER_BG)
        btns.pack(side='right', fill='y')
        self._win_btns = {}
        specs = (('min', '—', self._win_minimize, HOVER),
                 ('max', '□', self._win_toggle_max, HOVER),
                 ('close', '✕', self.on_main_close, '#E81123'))
        for key, glyph, cmd, hover_bg in specs:
            b = tk.Label(btns, text=glyph, bg=HEADER_BG, fg=TEXT,
                         font=('Segoe UI', 11), width=4, cursor='hand2')
            b.pack(side='left', fill='y')
            b.bind('<Button-1>', lambda e, c=cmd: c())
            b.bind('<Enter>', lambda e, w=b, h=hover_bg: w.configure(
                bg=h, fg='#FFFFFF' if h == '#E81123' else TEXT))
            b.bind('<Leave>', lambda e, w=b: w.configure(bg=HEADER_BG, fg=TEXT))
            self._win_btns[key] = b

        bar.bind('<Button-1>', self._titlebar_press)
        bar.bind('<B1-Motion>', self._titlebar_drag)
        bar.bind('<Double-Button-1>', lambda e: self._win_toggle_max())

    def _titlebar_press(self, event):
        '''记录按下点与窗口原点的偏移，拖动时只改窗口位置。'''

        # 系统原生最大化（Win+↑ / 吸附到顶部）后再拖标题栏：先还原，
        # 否则会在"最大化"状态下改坐标，窗口既变形又没法拖到别的显示器。
        if self.frame32.is_maxed():
            self._maxed = False
            self.frame32.restore()
            self.root.update_idletasks()
            try:
                self._win_btns['max'].configure(text='□')
            except Exception:
                pass
        direction = self._edge_dir(event.x_root, event.y_root)
        if direction:
            # 落在缩放热区上：这里直接起缩放，并 break 掉冒泡，
            # 否则根窗口那层的拖边处理器就再也收不到这次按下（上边缘尤其明显）。
            self._drag_off = None
            self._begin_resize(direction, event)
            return 'break'
        self._drag_off = (event.x_root - self.root.winfo_x(),
                          event.y_root - self.root.winfo_y())

    def _titlebar_drag(self, event):
        '''纯移动只发一个位置变更，不触发任何重排。

        早先这里同样只是改坐标，卡顿的真正来源是每个 <Configure>（移动窗口
        也会触发）都去 delete+重建卡片图元。那部分修在 RoundedSurface 里，
        这里只做最小的事。
        '''

        if self._resize_dir or getattr(self, '_maxed', False) or self.frame32.is_maxed():
            return
        off = getattr(self, '_drag_off', None)
        if not off:
            return
        self.frame32.set_rect(event.x_root - off[0], event.y_root - off[1],
                              self.root.winfo_width(), self.root.winfo_height())

    def _win_minimize(self):
        '''最小化到任务栏（可再点任务栏图标还原）。

        无边框窗口原先只能 withdraw()，于是任务栏图标点了没反应——
        现在窗口带 WS_MINIMIZEBOX，可以真正最小化。
        '''

        if not self.frame32.minimize():
            self.root.withdraw()
        # 收起来之后托盘图标仍在，顺手把黑洞面板叫出来（可在托盘右键 / 设置里关掉）
        self._auto_open_blackhole()

    def _win_toggle_max(self):
        '''最大化：铺满**窗口所在那块屏**的工作区（不含任务栏），并记住还原尺寸。

        不走 ShowWindow(SW_MAXIMIZE) 而是自己 set_rect 到工作区，原因是原生
        最大化会把窗口矩形向四周各撑出一个边框宽度（那圈缩放边框要落到屏幕
        外），无标题栏窗口的客户区 == 整窗，多出来的部分就会画到相邻显示器上。
        另外 WM_GETMINMAXINFO 也已经把原生最大化的矩形钉在显示器工作区上，
        两条路径都不会再溢出到副屏。
        '''

        native = self.frame32.is_maxed()
        if native or getattr(self, '_maxed', False):
            self._maxed = False
            self.frame32.restore()
            # 原生还原会回到最大化前的矩形；若那块屏已拔掉 / 换了布局，
            # 这个矩形可能整个落在看不见的地方，夹回当前显示器。
            r = self.frame32.get_rect()
            if r:
                self.frame32.set_rect(*self._clamp_rect_to_monitor(r))
            glyph = '□'
        else:
            self._restore_rect = (self.root.winfo_x(), self.root.winfo_y(),
                                  self.root.winfo_width(), self.root.winfo_height())
            self.frame32.toggle_max()
            # 兜底：个别时序下系统仍会按默认尺寸铺（比显示器大一圈），
            # 这里按工作区再钉一次，确保不会溢出到相邻显示器。
            wa = self._work_area()
            r = self.frame32.get_rect()
            if wa and r and (r[0] < wa[0] or r[1] < wa[1]
                             or r[0] + r[2] > wa[0] + wa[2]
                             or r[1] + r[3] > wa[1] + wa[3]):
                self.frame32.set_rect(*wa)
            self._maxed = True
            glyph = '❐'
        try:
            self._win_btns['max'].configure(text=glyph)
        except Exception:
            pass

    def _work_area(self):
        '''返回**窗口当前所在显示器**的可用区域（已排除任务栏）。

        注意：不能只用 SystemParametersInfo(SPI_GETWORKAREA)——它只代表主显示器。
        外屏当主屏、笔记本屏当副屏时，窗口一旦被拖到笔记本屏上，按主显示器算
        出来的最大化矩形会把窗口整块搬回外屏。
        '''

        if sys.platform == 'win32':
            wa = self.frame32.monitor_work_area()
            if wa:
                return wa
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _clamp_rect_to_monitor(self, rect):
        '''把一个矩形收进窗口当前所在显示器的工作区内（尺寸优先，再夹位置）。

        用途：还原窗口时，如果上一次记住的位置在另一块屏上（或者那块屏已经
        拔掉了），直接 set_rect 会让窗口落在看不见的地方——看起来就像"程序
        跑到副屏去了"。
        '''

        x, y, w, h = rect
        wa = self._work_area()
        if not wa:
            return x, y, w, h
        wx, wy, ww, wh = wa
        w = int(min(w, ww))
        h = int(min(h, wh))
        x = int(max(wx, min(x, wx + ww - w)))
        y = int(max(wy, min(y, wy + wh - h)))
        return x, y, w, h

    def _sync_max_state(self):
        '''系统原生最大化（Win+↑ / 拖到顶部吸附 / 系统菜单）之后同步按钮态。'''

        native = self.frame32.is_maxed()
        if native == bool(getattr(self, '_maxed', False)):
            return
        self._maxed = native
        try:
            self._win_btns['max'].configure(text='❐' if native else '□')
        except Exception:
            pass

    # 无边框窗口需要自己实现拖边缩放：边缘热区宽度与各方向光标
    RESIZE_MARGIN = 6
    CURSOR_BY_DIR = {
        'n': 'size_ns', 's': 'size_ns', 'e': 'size_we', 'w': 'size_we',
        'nw': 'size_nw_se', 'se': 'size_nw_se', 'ne': 'size_ne_sw', 'sw': 'size_ne_sw',
    }

    def _install_resize_edges(self):
        '''在根窗口上安装八方向拖边缩放（事件经 toplevel bindtag 传到根窗口）。'''

        self._resize_dir = None
        self._resize_geo = None
        self._resize_start = None
        self._cur_cursor = ''
        self.root.bind('<Configure>', (lambda e: self._sync_max_state()), add='+')
        self.root.bind('<Motion>', self._on_root_motion, add='+')
        self.root.bind('<ButtonPress-1>', self._on_resize_press, add='+')
        self.root.bind('<B1-Motion>', self._on_resize_drag, add='+')
        self.root.bind('<ButtonRelease-1>', self._on_resize_release, add='+')

    def _edge_dir(self, x_root, y_root):
        '''返回鼠标所在的缩放方向（n/s/e/w/nw/ne/sw/se），不在边缘则返回 None。'''

        if getattr(self, '_maxed', False) or self.frame32.is_maxed():
            return None
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        m = self.RESIZE_MARGIN
        if not (x <= x_root <= x + w and y <= y_root <= y + h):
            return None
        left = x_root <= x + m
        right = x_root >= x + w - m
        top = y_root <= y + m
        bottom = y_root >= y + h - m
        if top and left:
            return 'nw'
        if top and right:
            return 'ne'
        if bottom and left:
            return 'sw'
        if bottom and right:
            return 'se'
        if left:
            return 'w'
        if right:
            return 'e'
        if top:
            return 'n'
        if bottom:
            return 's'
        return None

    def _on_root_motion(self, event):
        if self._resize_dir:
            return
        cur = self.CURSOR_BY_DIR.get(self._edge_dir(event.x_root, event.y_root), '')
        if cur != self._cur_cursor:
            self._cur_cursor = cur
            self.root.configure(cursor=cur)

    def _on_resize_press(self, event):
        direction = self._edge_dir(event.x_root, event.y_root)
        if direction:
            self._begin_resize(direction, event)

    def _begin_resize(self, direction, event):
        '''开始一次拖边缩放：记住起始矩形与光标位置，后续按增量算新尺寸。'''

        global _LAYOUT_DEFER
        _LAYOUT_DEFER = True
        self._resize_dir = direction
        self._resize_geo = (self.root.winfo_x(), self.root.winfo_y(),
                            self.root.winfo_width(), self.root.winfo_height())
        self._resize_start = (event.x_root, event.y_root)

    def _on_resize_drag(self, event):
        if not self._resize_dir:
            return
        gx, gy, gw, gh = self._resize_geo
        dx = event.x_root - self._resize_start[0]
        dy = event.y_root - self._resize_start[1]
        mw, mh = self.root.minsize()
        minw = mw if mw > 100 else 1040
        minh = mh if mh > 100 else 700

        nx, ny, nw, nh = gx, gy, gw, gh
        if 'w' in self._resize_dir:
            nw = max(minw, gw - dx)
            nx = gx + (gw - nw)
        if 'e' in self._resize_dir:
            nw = max(minw, gw + dx)
        if 'n' in self._resize_dir:
            nh = max(minh, gh - dy)
            ny = gy + (gh - nh)
        if 's' in self._resize_dir:
            nh = max(minh, gh + dy)
        self.frame32.set_rect(nx, ny, nw, nh)
        self.root.update_idletasks()
        # update_idletasks 只跑 Tk 的 idle 回调，**不碰 Windows 消息队列**，
        # 所以窗口缩小后新暴露出来的区域不会立刻收到 WM_PAINT。表现就是
        # 上一帧的画面留在那里，和新布局叠在一起（顶部 tab 条重复好几遍）。
        # RedrawWindow(RDW_UPDATENOW) 同步触发一次整窗绘制，绕开消息队列，
        # 也不会像 update() 那样把待处理事件一起放进来（避免拖动重入）。
        self.frame32.repaint()

    def _on_resize_release(self, event):
        if not self._resize_dir:
            return
        global _LAYOUT_DEFER
        self._resize_dir = None
        self._resize_geo = None
        self._resize_start = None
        _LAYOUT_DEFER = False
        self._refresh_card_heights()
        self._draw_tab_under()
        self.frame32.repaint()
        self._on_root_motion(event)

    def _refresh_card_heights(self):
        '''缩放结束后统一补算一次卡片高度（见 _on_content 的延时逻辑）。'''

        def walk(w):
            try:
                children = w.winfo_children()
            except tk.TclError:
                return
            for c in children:
                # 不用 isinstance：poff_mods 会二次加载 poff.py，存在两份类对象
                if callable(getattr(c, '_on_content', None)):
                    try:
                        c._on_content()
                    except Exception:
                        pass
                walk(c)

        try:
            walk(self.root)
            self.root.update_idletasks()
        except tk.TclError:
            pass

    # 旧版 _remove_native_titlebar 已并入 _Win32Frame.attach()

    def _build_header(self, parent):
        '''状态行：只留 IP chip + 权限 chip（bg 与原生标题栏同色，连成一体）。

        chip 默认不画底色（bg 取 HEADER_BG），只在鼠标移上去时浮出一层
        淡色作为"可点"的反馈——原先常驻的 SOFT 矩形底在这条窄状态行里
        显得像两个按钮贴在上面，很抢视线。
        '''

        right = tk.Frame(parent, bg=HEADER_BG)
        right.pack(side='right', padx=16)
        self.chip_ip = tk.Label(right, text=f'本机 {self.local_ip}:{TCP_PORT}', bg=HEADER_BG, fg=MUTED, font=('Consolas', 9), padx=10, pady=4)
        self.chip_ip.pack(side='left', padx=6)
        self.chip_admin = tk.Label(right, text='', bg=HEADER_BG, fg=MUTED, font=('Microsoft YaHei UI', 8), padx=10, pady=4, cursor='hand2')
        self.chip_admin.pack(side='left', padx=6)
        self.chip_admin.bind('<Button-1>', (lambda e: self._on_admin_chip()))
        self.chip_admin.bind('<Enter>', (lambda e: self.chip_admin.configure(bg=HOVER, fg=TEXT)))
        self.chip_admin.bind('<Leave>', (lambda e: (self.chip_admin.configure(bg=HEADER_BG), self._refresh_admin_chip())))
        self._refresh_admin_chip()

    def _refresh_logo(self):
        '''把 poff.ico 渲染成侧栏 LOGO。

        图标本身是深色圆底，深色主题下和侧栏底色（#080C18）贴得太近，
        因此超采样后再补一圈 1px 主题描边，浅色主题则几乎不可见。
        '''

        lbl = getattr(self, '_logo_lbl', None)
        if lbl is None:
            return
        try:
            if not lbl.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            from PIL import Image, ImageDraw, ImageTk
        except Exception:
            return
        size, ss = 40, 4
        big = size * ss
        canvas = Image.new('RGBA', (big, big), (0, 0, 0, 0))
        try:
            ico = Image.open(_resource_path('poff.ico'))
            sizes = ico.info.get('sizes') or set()
            if sizes:
                ico.size = max(sizes)
            art = ico.convert('RGBA')
            inner = int(big * 0.94)
            art = art.resize((inner, inner), Image.LANCZOS)
            canvas.alpha_composite(art, ((big - inner) // 2, (big - inner) // 2))
        except Exception:
            pass
        dark = _TOKENS['TEXT'] == _TOKENS_DARK['TEXT']
        ring = '#31405C' if dark else '#D3DBE8'
        try:
            d = ImageDraw.Draw(canvas)
            d.ellipse((ss, ss, big - ss - 1, big - ss - 1), outline=ring, width=ss)
        except Exception:
            pass
        try:
            photo = ImageTk.PhotoImage(canvas.resize((size, size), Image.LANCZOS))
            lbl.configure(image=photo, bg=HEADER_BG)
            self._logo_photo = photo      # 必须留引用，否则被 GC 后图标变空白
        except Exception:
            pass

    def _build_sidebar(self, parent):
        '''侧栏：顶部 LOGO + POFF + 4 个导航项。

        LOGO 直接用程序自身 ico 渲染，不再手绘一个蓝色圆——
        早先自绘的蓝色圆和任务栏/Alt-Tab 里的深色图标是两套视觉，
        同一个程序在系统里外长得不一样。
        '''

        # 2.4.8：LOGO + 产品名作为一个整体在侧栏里水平居中，并整体上移一点。
        # 早先是 padx=18 左对齐，图标挤在最左边、右侧空掉一大片，视觉重心偏左。
        logo = tk.Frame(parent, bg=HEADER_BG)
        logo.pack(fill='x', pady=(6, 12))
        brand = tk.Frame(logo, bg=HEADER_BG)
        brand.pack(anchor='center')
        self._logo_lbl = tk.Label(brand, bg=HEADER_BG, bd=0, highlightthickness=0)
        self._logo_lbl.pack(side='left')
        self._logo_title = tk.Label(brand, text=APP_TITLE, bg=HEADER_BG, fg=TEXT,
                                    font=('Segoe UI Semibold', 15, 'bold'))
        # side='left' 默认 anchor=center，与 40px 的图标自动垂直居中对齐
        self._logo_title.pack(side='left', padx=(10, 0))
        self._refresh_logo()
        tk.Frame(parent, bg=BORDER, height=1).pack(fill='x', padx=18)
        self._nav_items = {}
        items = [('control', '主控', '⌂'), ('sw', '开关', '◉'), ('log', '日志', '◷'), ('set', '设置', '⚙')]
        nav_wrap = tk.Frame(parent, bg=HEADER_BG)
        nav_wrap.pack(fill='x', padx=10, pady=(10, 0))
        for key, label, icon in items:
            row_h = 44
            row = tk.Frame(nav_wrap, bg=HEADER_BG, height=row_h, cursor='hand2')
            row.pack(fill='x', pady=2)
            row.pack_propagate(False)
            self._nav_items[key] = row
            strip = tk.Frame(row, bg=ACCENT, width=3)
            strip.pack_forget()
            icon_lbl = tk.Label(row, text=icon, bg=HEADER_BG, fg=MUTED, font=('Segoe UI Symbol', 14))
            icon_lbl.pack(side='left', padx=(16, 10))
            lbl = tk.Label(row, text=label, bg=HEADER_BG, fg=MUTED, font=FONT_BODY_BOLD)
            lbl.pack(side='left')
            row._accent_bar = strip
            row._icon = icon_lbl
            self._nav_items[key]._label = lbl
            self._nav_items[key]._bg_normal = HEADER_BG
            self._nav_items[key]._bg_active = SOFT
            for widget in row, lbl, icon_lbl:
                widget.bind('<Button-1>', (lambda e, k=key: self._nav_select(k)))
                widget.bind('<Enter>', (lambda e, r=row: self._nav_apply_state(r, hovered=True)))
                widget.bind('<Leave>', (lambda e, r=row: self._nav_apply_state(r, hovered=False)))
            continue
        bottom = tk.Frame(parent, bg=HEADER_BG)
        bottom.pack(side='bottom', fill='x', padx=18, pady=14)
        tk.Label(bottom, text=f'shot2021 · POFF V{VERSION}', bg=HEADER_BG, fg=MUTED, font=FONT_CAP, wraplength=SIDEBAR_W - 36, justify='left').pack(anchor='w')
        # 网络看板压在版本号上方（pack 的 side='bottom' 后 pack 的先贴底，
        # 所以这里必须排在版本号之后，才能出现在它上面）。
        self.net_monitor = NetMonitor(self, parent)
        self.net_monitor.build()

    def _nav_apply_state(self, row, hovered=None):
        '''导航行三态渲染：激活(SOFT+accent图标) / 悬浮(HOVER) / 默认(chrome)。

        hover 状态通过 widget 上动态挂的 _hover 标记维护，
        Enter/Leave 均调本函数重算，激活优先级高于悬浮。
        '''

        if hovered is not None:
            row._hover = bool(hovered)
        active = getattr(row, '_active', False)
        hover = getattr(row, '_hover', False)
        if active:
            bg, fg, icon_fg = SOFT, TEXT, ACCENT
        elif hover:
            bg, fg, icon_fg = HOVER, TEXT, TEXT
        else:
            bg, fg, icon_fg = HEADER_BG, MUTED, MUTED
        try:
            row.config(bg=bg)
            row._label.config(bg=bg, fg=fg)
            row._icon.config(bg=bg, fg=icon_fg)
        except tk.TclError:
            pass

    def _nav_hover_in(self, row, lbl=None):
        self._nav_apply_state(row, hovered=True)

    def _nav_hover_out(self, row, lbl=None, key=None):
        self._nav_apply_state(row, hovered=False)

    def _nav_select(self, key):
        '''切到某一页。'''

        self._current_page = key
        page_map = {'control': self._pages['control'], 'sw': self._pages['sw'], 'log': self._pages['log'], 'set': self._pages['set']}
        for k, row in self._nav_items.items():
            row._active = k == key
            try:
                if k == key:
                    row._accent_bar.pack(side='left', fill='y', before=row._icon)
                else:
                    row._accent_bar.pack_forget()
            except Exception:
                pass
            self._nav_apply_state(row)
        # 用 grid_remove 让非当前页彻底退出布局：四个页面叠在同一格里，
        # 若只是 lift()，缩放窗口时四页都会跟着重排（实测占到 ~100ms/帧）。
        for k, p in page_map.items():
            if p is None:
                continue
            if k == key:
                p.grid()
            else:
                p.grid_remove()
        if key == 'log':
            self._refresh_log_hint()

    def _sync_all_modules(self):
        '''应用全部模块开关变更（写配置 + 实时启停）。'''

        for k, v in self.mod_vars.items():
            self.on_module_toggle(k)
        self.save_cfg()
        self.log('模块开关变更已应用并保存。', 'ok')

    def _clear_log(self):
        try:
            self.log_text.delete('1.0', 'end')
        except tk.TclError:
            pass

    def _refresh_log_hint(self):
        '''切换到日志页时刷新底部提示。'''

        try:
            if getattr(self, '_log_hint', None):
                return
        except Exception:
            pass

    def _check_update(self):
        self.log(f'当前版本 POFF V{VERSION}。检查更新功能暂未接入，请关注 GitHub release。', 'info')
        messagebox.showinfo('版本信息', f'POFF V{VERSION}\n\n检查更新功能后续接入。\nGitHub: https://github.com/gengmu5588-ship-it/poff')

    def _copy_to_clip(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.log(f'已复制到剪贴板：{text}', 'info')
        except Exception:
            pass

    def apply_theme(self, theme_name):
        '''切换主题（dark / light / system），递归重刷所有控件颜色。'''

        old = dict(_TOKENS)
        actual = _resolve_theme(theme_name)
        _apply_theme_token_set(actual)
        self._apply_titlebar_theme(dark=actual == 'dark')
        self._setup_style()
        self._recolor_all(old)
        self._refresh_check_images()
        self.cfg['theme'] = theme_name
        self.save_cfg()
        self.log(f'已切换到「{theme_name}」主题（实际={actual}）。', 'ok')

    _RECOLOR_BG_KEYS = ('BG', 'CARD', 'SOFT', 'HOVER', 'BORDER', 'HEADER_BG', 'LOG_BG')
    _RECOLOR_FG_KEYS = ('TEXT', 'MUTED', 'ACCENT', 'ACCENT_DARK', 'OK', 'WARN', 'ERR')
    def _recolor_all(self, old=None):
        '''递归把 root 下所有 widget 按「旧 token 值 → 新 token 值」重刷。

        只有当前颜色能在旧调色板中找到出处的控件才会被改，
        硬编码业务色（计时窗、指示灯等）不受影响。
        '''

        if old is None:
            old = dict(_TOKENS)
        bg_map = {old[k].lower(): _TOKENS[k] for k in self._RECOLOR_BG_KEYS}
        fg_map = {old[k].lower(): _TOKENS[k] for k in self._RECOLOR_FG_KEYS}
        self.root.configure(bg=BG)
        self._recolor_recursive(self.root, bg_map, fg_map)
        try:
            self._redraw_all_canvases()
        except Exception:
            pass
        # LOGO 是位图，不会跟着 recolor 走，主题切换后重建一次描边色
        try:
            self._refresh_logo()
        except Exception:
            pass
        try:
            self._draw_tab_under()
        except Exception:
            pass

    def _recolor_recursive(self, widget, bg_map, fg_map):
        '''递归重刷 widget 树：旧值命中映射才改，未命中保持原色。'''

        cls_name = widget.winfo_class()
        # 这里刻意用"有没有 _recolor 方法"来判断，而不是 isinstance：
        # poff_mods 里的 `from poff import SectionCard` 会把 poff.py 再作为
        # 独立模块加载一遍，于是运行时存在两份 SectionCard 类，isinstance
        # 对另一半永远返回 False——表现就是录屏/共享面板的主题色永远停在
        # 启动时那套深色，别的地方都正常。
        recolor = getattr(widget, '_recolor', None)
        if callable(recolor):
            try:
                recolor()
            except Exception:
                pass
        elif cls_name == 'Checkbutton':
            # 自绘复选框（indicatoron=0 + image）在选中时整块用 selectcolor 铺底，
            # 早先这里没处理 Checkbutton，主题初始化跑在 UI 建好之前，于是
            # 复选框一直带着"建模时那套深色 CARD"——浅色主题下就是一块黑底。
            try:
                cur_bg = str(widget.cget('bg')).lower()
                new_bg = bg_map.get(cur_bg)
                if new_bg is not None:
                    widget.configure(bg=new_bg, activebackground=new_bg, selectcolor=new_bg)
                cur_fg = str(widget.cget('fg')).lower()
                new_fg = fg_map.get(cur_fg)
                if new_fg is not None:
                    widget.configure(fg=new_fg, activeforeground=new_fg)
            except tk.TclError:
                pass
        elif cls_name in ('Frame', 'Label', 'Canvas', 'Text', 'Toplevel', 'Listbox'):
            try:
                cur_bg = str(widget.cget('bg')).lower()
                new_bg = bg_map.get(cur_bg)
                if new_bg is not None:
                    widget.configure(bg=new_bg)
                if cls_name in ('Label', 'Text', 'Listbox'):
                    cur_fg = str(widget.cget('fg')).lower()
                    new_fg = fg_map.get(cur_fg)
                    if new_fg is not None:
                        widget.configure(fg=new_fg)
                if cls_name == 'Text':
                    widget.configure(insertbackground='#FFFFFF')
            except tk.TclError:
                pass
        for child in widget.winfo_children():
            self._recolor_recursive(child, bg_map, fg_map)

    def _redraw_all_canvases(self):
        '''遍历 widget 树，对有 _redraw 的实例（FlatToggle / SectionCard / NiceScrollbar）重绘。'''

        def walk(w):
            try:
                widgets = w.winfo_children()
            except tk.TclError:
                return
            for child in widgets:
                if hasattr(child, '_redraw') and callable(child._redraw):
                    try:
                        child._redraw()
                    except Exception:
                        pass
                walk(child)

        walk(self.root)

    def _build_switches(self, parent):
        card = RoundedSurface(parent, padding=(14, 12))
        card.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        head = ttk.Frame(card.body, style='Card.TFrame')
        head.pack(fill='x')
        ttk.Label(head, text='功能总开关', style='H3.TLabel').pack(side='left')
        ttk.Label(head, text='关闭后对应模块的入口与热键一并停用', style='Muted.TLabel').pack(side='left', padx=10)
        grid = ttk.Frame(card.body, style='Card.TFrame')
        grid.pack(fill='x', pady=(10, 0))
        mods = self.cfg.get('modules', {})
        labels = [('screenshot', '屏幕截屏'), ('recorder', '录屏录音'), ('brightness', '亮度控制'), ('tablet', '平板副屏'), ('roam', '键鼠漫游'), ('screenshare', '屏幕共享'), ('blackhole', '黑洞投递')]
        COLS_PER_ROW = 6
        for i, (key, label) in enumerate(labels):
            row = 0 if i < COLS_PER_ROW else 1
            col = i if row == 0 else 0
            var = tk.BooleanVar(value=bool(mods.get(key, True)))
            self.mod_vars[key] = var
            cell = ttk.Frame(grid, style='Card.TFrame')
            cell.grid(row=row, column=col, sticky='nw', padx=(0, 18), pady=(0, 8) if row == 1 else 0)
            FlatToggle(cell, variable=var, on_change=(lambda k=key: self.on_module_toggle(k))).grid(row=0, column=0, sticky='w')
            ttk.Label(cell, text=label, style='Muted.TLabel', font=('PingFang SC', 8)).grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Label(card.body, text='全局热键：Alt+Q 选区截屏　Ctrl+Shift+F 全屏截屏　Ctrl+Shift+H 黑洞面板　Ctrl+Shift+L 漫游锁定（漫游开启后生效）', style='Muted.TLabel', wraplength=700).pack(anchor='w', pady=(4, 0))

    def _build_tabs(self, parent):
        '''功能区：自绘标签条 + 无原生 tab 的 Notebook。

        自绘的原因见 _setup_style 里的注释（原生 tab 会折行重叠）。
        自绘后每个 tab 等宽撑满，标签永远不会溢出，缩放时也不会多行。
        '''

        host = parent                     # _section_tabs 已经建好容器
        tabbar = tk.Frame(host, bg=BG, height=42)
        tabbar.pack(fill='x')
        tabbar.pack_propagate(False)
        self._tabbar = tabbar
        self._tab_btns = []
        self._tab_hover_idx = -1
        self.nb = ttk.Notebook(host)
        self.nb.pack(fill='both', expand=True)
        self._tab_scroll = {}
        tab_texts = []

        def make_tab(text, builder, wrap=True):
            '''每个标签页内容放在可滚动容器里，窗口较矮时也不会裁切控件。

            2.4.8：滚动条改成浮在右侧留白上（place），不再占用 8px 布局宽度。
            早先 vsb 是 pack 进来的，画布比右列窄 8px，标签页里的卡片就比右列
            下方的「快捷操作」卡片窄 8px 且整体左移，一列卡片左右边线全错开。
            '''

            outer = ttk.Frame(self.nb, style='Bg.TFrame')
            canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
            inner = ttk.Frame(canvas, style='Bg.TFrame')
            win = canvas.create_window((0, 0), window=inner, anchor='nw')
            canvas.configure(background=BG)
            canvas.pack(fill='both', expand=True)
            vsb = NiceScrollbar(outer, command=canvas.yview, bg=BG)
            vsb.place(relx=1.0, y=0, anchor='ne', relheight=1.0, width=8)
            canvas.configure(yscrollcommand=vsb.set)
            inner.bind('<Configure>', (lambda e, c=canvas: c.configure(scrollregion=c.bbox('all'))))
            canvas.bind('<Configure>', (lambda e, c=canvas, w=win: c.itemconfigure(w, width=e.width)))
            self._tab_scroll[str(outer)] = canvas, inner
            self.nb.add(outer, text='')
            tab_texts.append(text)
            if wrap:
                page = RoundedSurface(inner, padding=0)
                page.pack(fill='both', expand=True, padx=PAD_CARD, pady=(PAD_CARD, 14))
                builder(page.body)
            else:
                builder(inner)
            return outer

        def _gap(parent):
            '''两张卡片之间的竖向留白（pack 一个 12px 高的空 Frame）。'''

            tk.Frame(parent, bg=BG, height=GAP_SEC).pack(fill='x')

        def _shot_and_recorder(parent):
            '''截屏 Tab 内含：截屏功能区（圆角卡） + 录屏录音面板。'''

            page = RoundedSurface(parent, padding=0)
            page.pack(fill='both', expand=True, padx=PAD_CARD, pady=(PAD_CARD, 0))
            self.screenshot.build_tab(page.body)
            # 面板自带 pady=(0, GAP_SEC)，上沿是 0：这里补一条间距，
            # 否则录屏卡会直接贴在截屏卡的下边上（2.4.7 就是这样紧贴的）。
            _gap(parent)
            self.recorder.build_panel(parent)

        def _roam_and_share(parent):
            '''键鼠漫游 Tab 内含：漫游面板（圆角卡） + 屏幕共享面板。'''

            page = RoundedSurface(parent, padding=0)
            page.pack(fill='both', expand=True, padx=PAD_CARD, pady=(PAD_CARD, 0))
            self.roam.build_tab(page.body)
            _gap(parent)
            self.screenshare.build_panel(parent)

        self.tab_device = make_tab('设备操作', self._build_device_tab, wrap=False)
        self.tab_shot = make_tab('截屏录屏', _shot_and_recorder, wrap=False)
        self.tab_hole = make_tab('黑洞投递', self.blackhole.build_tab)
        self.tab_bright = make_tab('亮度', self.brightness.build_tab)
        self.tab_tablet = make_tab('平板副屏', self.tablet.build_tab)
        self.tab_roam = make_tab('漫游共享', _roam_and_share, wrap=False)
        self.root.bind_all('<MouseWheel>', self._on_tab_wheel)

        for i, text in enumerate(tab_texts):
            cell = tk.Frame(tabbar, bg=BG)
            cell.pack(side='left', fill='both', expand=True)
            lbl = tk.Label(cell, text=text, bg=BG, fg=MUTED, font=FONT_BODY_BOLD, cursor='hand2')
            lbl.pack(fill='both', expand=True)
            ind = tk.Frame(cell, bg=BG, height=2)
            ind.place(relx=0.18, rely=1.0, relwidth=0.64, anchor='sw', y=-1)
            self._tab_btns.append((cell, lbl, ind))
            for w in (cell, lbl):
                w.bind('<Button-1>', lambda e, k=i: self._select_tab(k))
                w.bind('<Enter>', lambda e, k=i: self._tab_hover(k, True))
                w.bind('<Leave>', lambda e, k=i: self._tab_hover(k, False))

        self.nb.bind('<<NotebookTabChanged>>', (lambda e: self._draw_tab_under()))
        self._root_after(80, self._draw_tab_under)

    def _select_tab(self, index):
        try:
            self.nb.select(index)
        except Exception:
            pass
        self._draw_tab_under()

    def _tab_hover(self, index, on):
        self._tab_hover_idx = index if on else -1
        self._draw_tab_under()

    def _draw_tab_under(self):
        '''同步自绘标签条的选中/悬浮状态。

        早先这里是按 nb.bbox() 算坐标、再用 place 摆一条 2px 下划线，
        窗口缩放时既要 update_idletasks 又要 place_configure，本身就是
        抖动的来源之一。现在 tab 全部自绘，只要改几个 Label 的颜色。
        '''

        btns = getattr(self, '_tab_btns', None)
        nb = getattr(self, 'nb', None)
        if not btns or nb is None:
            return
        try:
            sel = nb.index('current')
        except Exception:
            return
        hover_idx = getattr(self, '_tab_hover_idx', -1)
        for i, (cell, lbl, ind) in enumerate(btns):
            active = (i == sel)
            hover = (i == hover_idx and not active)
            if active:
                bg, fg, bar = BG, ACCENT, ACCENT
            elif hover:
                bg, fg, bar = HOVER, TEXT, HOVER
            else:
                bg, fg, bar = BG, MUTED, BG
            try:
                cell.configure(bg=bg)
                lbl.configure(bg=bg, fg=fg)
                ind.configure(bg=bar)
            except tk.TclError:
                return

    def _root_after(self, ms, fn):
        try:
            self.root.after(ms, fn)
        except tk.TclError:
            pass

    @staticmethod
    def _wheel_over(event, canvas, inner):
        '''指针是否落在某个滚动容器内部。'''

        try:
            widget = canvas.winfo_containing(event.x_root, event.y_root)
        except tk.TclError:
            return False
        while widget is not None:
            if widget is inner or widget is canvas:
                return True
            widget = getattr(widget, 'master', None)
        return False

    @staticmethod
    def _scroll_pair(event, pair, delta):
        canvas, inner = pair
        try:
            if inner.winfo_reqheight() <= canvas.winfo_height():
                return False
            canvas.yview_scroll(-1 if delta > 0 else 1, 'units')
        except tk.TclError:
            return False
        return True

    def _on_tab_wheel(self, event):
        '''滚轮：先滚当前页自己的滚动容器（设置/开关），再滚标签页内容。

        两者都只在指针位于容器内、且内容确实超出可视区时才响应，
        否则滚轮会被"吃掉"，其他地方（比如设备列表）就没法滚了。
        '''

        pair = self._page_scroll.get(getattr(self, '_current_page', ''))
        if pair and self._wheel_over(event, pair[0], pair[1]):
            if self._scroll_pair(event, pair, event.delta):
                return
        try:
            pair = self._tab_scroll.get(str(self.nb.nametowidget(self.nb.select())))
        except (KeyError, tk.TclError):
            return
        if not pair or not self._wheel_over(event, pair[0], pair[1]):
            return
        self._scroll_pair(event, pair, event.delta)

    def _build_device_tab(self, parent):
        card = SectionCard(parent, title='设备操作')
        # 设备操作标签页没有外层圆角页卡，卡片自己承担左右留白，
        # 否则会比其它标签页里的卡片宽 GAP 一截，切标签时边线会跳。
        card.pack(fill='x', padx=PAD_CARD, pady=(PAD_CARD, GAP_SEC))
        box = card.body
        # 选中信息直接排在卡片白底上。早先用 SOFT 色块把这一整块兜住，
        # 块里的 Accent/Muted 标签各自带 CARD 底色，于是"当前选中"和右侧
        # 提示成了灰蓝条上的两块白补丁；按钮又是 SOFT 底色，在 SOFT 条上
        # 几乎没有边界——这就是那条"按钮条"的来历。
        info = ttk.Frame(box, style='Card.TFrame')
        info.pack(fill='x')
        ttk.Label(info, text='当前选中', style='Muted.TLabel').grid(row=0, column=0, sticky='w')
        self.lbl_sel_ip = ttk.Label(info, text='未选中主机', style='Accent.TLabel')
        self.lbl_sel_ip.grid(row=1, column=0, sticky='w', pady=(2, 0))
        self.lbl_sel_state = ttk.Label(info, text='请在左侧列表选择一台主机', style='Muted.TLabel')
        self.lbl_sel_state.grid(row=1, column=1, sticky='e', padx=(12, 0))
        info.columnconfigure(1, weight=1)
        row = ttk.Frame(box, style='Card.TFrame')
        row.pack(anchor='w', pady=(12, 0))
        # 三个按钮等宽但不铺满整行：uniform 只取最宽那颗的宽度，
        # 窗口缩到最小时按钮整体左移，而不会把「截屏发给它」挤出卡片。
        for i in range(3):
            row.columnconfigure(i, weight=1, uniform='acts')
        self.btn_check = ttk.Button(row, text='连通校验', command=self.check_selected)
        self.btn_check.grid(row=0, column=0, sticky='ew')
        self.btn_single = ttk.Button(row, text='单独关机', style='Danger.TButton', command=self.single_shutdown, state='disabled')
        self.btn_single.grid(row=0, column=1, sticky='ew', padx=GAP_CTRL)
        ttk.Button(row, text='截屏发给它', command=self.shot_for_selected).grid(row=0, column=2, sticky='ew')
        par_card = SectionCard(parent, title='并行同步关机')
        par_card.pack(fill='x', padx=PAD_CARD, pady=(0, GAP_SEC))
        par = par_card.body
        par_lbl = ttk.Label(par, text='关机前会先扫描各主机正在运行的高风险应用（Office / 浏览器 / 编辑器等），汇总确认后再进入 10 秒倒计时，避免对方弹出保存对话框时无人处理。', style='Muted.TLabel', justify='left')
        par_lbl.pack(anchor='w', pady=(2, 0))
        self._auto_wrap(par_lbl)
        self.btn_start = ttk.Button(par, text='启动 10 秒并行同步关机', style='Accent.TButton', command=self.start_sync, state='disabled')
        self.btn_start.pack(anchor='w', pady=(10, 0))
        tip_card = SectionCard(parent, title='其他操作')
        tip_card.pack(fill='x', padx=PAD_CARD, pady=(0, PAD_CARD))
        tip = tip_card.body
        tip_lbl = ttk.Label(tip, text='截屏发送、文件投递、亮度调节分别在对应标签页；黑洞面板把文件拖到主机条目即可投递。', style='Muted.TLabel', justify='left')
        tip_lbl.pack(anchor='w', pady=(2, 0))
        self._auto_wrap(tip_lbl)

    def _auto_wrap(self, label, margin=0):
        '''兼容旧调用点：实现见模块级 auto_wrap()。'''

        auto_wrap(label, margin)

    def _build_log(self, body):
        card = ttk.Frame(body, style='Card.TFrame', padding=(12, 8))
        card.grid(row=1, column=0, columnspan=2, sticky='nsew', pady=(10, 0))
        head = ttk.Frame(card, style='Card.TFrame')
        head.pack(fill='x')
        ttk.Label(head, text='运行日志', style='H3.TLabel').pack(side='left')
        ttk.Button(head, text='复制', command=self.copy_log).pack(side='right')
        ttk.Button(head, text='清空', command=self.clear_log).pack(side='right', padx=6)
        wrap = tk.Frame(card, bg=LOG_BG)
        wrap.pack(fill='both', expand=True, pady=(6, 0))
        self.log_text = tk.Text(wrap, height=7, bg=LOG_BG, fg='#D4DCED', insertbackground='#FFFFFF', relief='flat', font=('Consolas', 9), padx=10, pady=6, state='disabled', wrap='word')
        self.log_text.pack(side='left', fill='both', expand=True)
        sb = NiceScrollbar(wrap, command=self.log_text.yview, bg=LOG_BG)
        sb.pack(side='right', fill='y')
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.tag_configure('time', foreground=LOG_TIME_FG)
        self.log_text.tag_configure('info', foreground=TEXT)
        self.log_text.tag_configure('ok', foreground=OK)
        self.log_text.tag_configure('warn', foreground=WARN)
        self.log_text.tag_configure('err', foreground=ERR)

    def log(self, msg, level='info'):
        self.ui_call(self._append_log, msg, level)

    def _append_log(self, msg, level):
        try:
            if not hasattr(self, 'log_text'):
                return
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            self.log_text.config(state='normal')
            self.log_text.insert('end', f'[{ts}] ', 'time')
            self.log_text.insert('end', f'{msg}\n', level if level in ('info', 'ok', 'warn', 'err') else 'info')
            self.log_text.see('end')
            self.log_text.config(state='disabled')
        except tk.TclError:
            pass

    def clear_log(self):
        self.log_text.config(state='normal')
        self.log_text.delete('1.0', 'end')
        self.log_text.config(state='disabled')

    def copy_log(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.log_text.get('1.0', 'end').strip())
            self.log('日志已复制到剪贴板', 'ok')
        except tk.TclError:
            pass

    def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            try:
                pass
            except Exception:
                return '127.0.0.1'
            return socket.gethostbyname(socket.gethostname())
        return ip

    def peer_items(self):
        with self.peer_lock:
            return list(self.peer_list.items())

    def selected_ip(self):
        '''当前选中的主机 IP。

        2.4.12：行的 iid 直接就是 IP。列表为了带设备图标，地址列写成了
        "🖥 192.168.0.5"（含图标），早先这里取整串当 IP，peer_list 里自然
        查不到 —— 于是「连通校验」「单独关机」「截屏发给它」「共享/录屏
        目标」全部静默返回，"未校验连通状态"永远不变。
        现在 iid 就是 IP，values 只作兜底（第 2 列是地址）。
        '''

        peer_box = getattr(self, 'peer_box', None)
        if peer_box is None:
            return
        sel = peer_box.selection()
        if not sel:
            return
        item = sel[0]
        if item in self.peer_list:
            return item
        try:
            vals = peer_box.item(item, 'values')
            if len(vals) >= 2:
                return str(vals[1]).split()[-1]
            return str(vals[0]).split()[-1] if vals else None
        except Exception:
            return

    def on_select_change(self, _event=None):
        ip = self.selected_ip()
        if not ip:
            self.lbl_sel_ip.config(text='未选中主机')
            self.lbl_sel_state.config(text='请在左侧列表选择一台主机')
            self.btn_single.config(state='disabled')
            return
        info = self.peer_list.get(ip, {})
        name = str(info.get('name') or '').strip()
        if info.get('kind') == 'mobile':
            self.lbl_sel_ip.config(text=f'📱 {name or ip}')
            self.lbl_sel_state.config(text='手机在线' if info.get('ok') else '手机待连接（打开投递页后在线）')
            self.btn_single.config(state='disabled')
            return
        self.lbl_sel_ip.config(text=f'{name} · {ip}' if name else ip)
        if info.get('ok'):
            self.lbl_sel_state.config(text='已确认在线')
        elif info.get('checked'):
            self.lbl_sel_state.config(text='无法连接')
        else:
            self.lbl_sel_state.config(text='未校验连通状态')
        self.btn_single.config(state='normal')

    def peer_name(self, ip):
        '''主机备注（没设过就返回空串）。'''

        info = self.peer_list.get(ip) or {}
        return str(info.get('name') or '').strip()

    def peer_label(self, ip):
        '''给日志 / 弹窗用的显示名：有备注就是「备注（IP）」，否则就是 IP。'''

        if not ip:
            return ''
        name = self.peer_name(ip)
        return f'{name}（{ip}）' if name else ip

    def refresh_peer_ui(self):
        prev = self.selected_ip()
        for item in self.peer_box.get_children():
            self.peer_box.delete(item)
        for ip, info in sorted(self.peer_list.items()):
            kind = info.get('kind', 'pc')
            icon = '📱' if kind == 'mobile' else '🖥'
            if kind == 'mobile':
                tag = 'online' if info.get('ok') else 'unknown'
                state = '在线' if info.get('ok') else '待连接'
            elif info.get('ok'):
                tag, state = 'online', '在线'
            elif info.get('checked'):
                tag, state = 'offline', '无法连接'
            else:
                tag, state = 'unknown', '待校验'
            name = str(info.get('name') or '').strip()
            if kind == 'mobile' and not name:
                name = '手机设备'
            self.peer_box.insert('', 'end', iid=ip, values=(name, f'{icon} {ip}', state), tags=(tag,))
        self.lbl_peer_count.config(text=f'{len(self.peer_list)} 台')
        if prev:
            try:
                if self.peer_box.exists(prev):
                    self.peer_box.selection_set(prev)
                    self.peer_box.focus(prev)
            except tk.TclError:
                pass
        self.btn_start.config(state='normal' if self.peer_list else 'disabled')
        self.on_select_change()
        try:
            self.blackhole.refresh()
        except Exception:
            pass
        try:
            self.screenshare.refresh_peer_list()
        except Exception:
            pass

    def save_peer_list(self):
        with self.peer_lock:
            snapshot = dict(self.peer_list)
        save_peers(snapshot)

    def gen_code(self):
        raw = f'{CODE_APP_TAG}|{CODE_VERSION}|{self.local_ip}|{TCP_PORT}'
        code = base64.b32encode(raw.encode()).decode().rstrip('=')
        self.code_var.set(code)
        self.log(f'已生成本机串码：{code}', 'info')

    def copy_code(self):
        code = self.code_var.get().strip()
        if not code:
            self.gen_code()
            code = self.code_var.get().strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        self.log('串码已复制到剪贴板，可直接发给对方', 'ok')

    def parse_code(self, text):
        text = text.strip().upper().replace(' ', '')
        if not text:
            raise ValueError('请输入串码')
        pad = '=' * (-len(text) % 8)
        raw = base64.b32decode(text + pad).decode('utf-8')
        parts = raw.split('|')
        if len(parts) != 4 or parts[0] != CODE_APP_TAG:
            raise ValueError('串码格式不正确')
        ip = str(ipaddress.ip_address(parts[2]))
        port = int(parts[3])
        if not 1 <= port <= 65535:
            raise ValueError('串码端口非法')
        return ip, port

    def add_peer(self):
        text = self.code_var.get().strip().replace('：', ':')
        try:
            if text and ':' in text and '|' not in text and len(text) < 40:
                host, _, port_s = text.partition(':')
                ip, port = str(ipaddress.ip_address(host)), int(port_s)
                if not 1 <= port <= 65535:
                    raise ValueError('端口超出范围')
            else:
                ip, port = self.parse_code(text)
        except Exception as e:
            messagebox.showerror('添加失败', f'串码无法识别：{e}')
            return
        if ip == self.local_ip:
            messagebox.showwarning('提示', '这是本机地址，不需要添加')
            return
        remark = self.remark_var.get().strip()[:24] if hasattr(self, 'remark_var') else ''
        with self.peer_lock:
            if ip in self.peer_list:
                self.peer_list[ip]['port'] = port
                # 备注留空表示"不动原来的"（改备注请用列表右键 / 编辑备注按钮）
                if remark:
                    self.peer_list[ip]['name'] = remark
                added = False
            else:
                self.peer_list[ip] = {'port': port, 'ok': False, 'name': remark}
                added = True
        self.refresh_peer_ui()
        self.save_peer_list()
        self.code_var.set('')
        try:
            self.remark_var.set('')
        except tk.TclError:
            pass
        self.log(f'{'已添加' if added else '已更新'}主机 {self.peer_label(ip)}:{port}', 'ok')

    def _peer_menu_popup(self, event):
        '''设备列表右键菜单：设备注 / 校验 / 移除。'''

        ip = self.peer_box.identify_row(event.y)
        if not ip:
            return
        try:
            self.peer_box.selection_set(ip)
            self.peer_box.focus(ip)
        except tk.TclError:
            pass
        menu = tk.Menu(self.root, tearoff=0, bg=CARD, fg=TEXT, activebackground=SOFT,
                       activeforeground=TEXT, bd=0, font=FONT_BODY)
        menu.add_command(label='设置备注…', command=lambda: self.edit_peer_remark(ip))
        menu.add_command(label='校验连通状态', command=self.check_selected)
        menu.add_separator()
        menu.add_command(label='移除这台主机', command=lambda: self.remove_peer(ip))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def remove_peer(self, ip=None):
        ip = ip or self.selected_ip()
        if not ip:
            self.log('请先在左侧列表选中一台主机', 'warn')
            return
        if not messagebox.askyesno('移除主机', f'从列表移除 {self.peer_label(ip)}？\n（只影响本机列表，不影响对方）'):
            return
        with self.peer_lock:
            self.peer_list.pop(ip, None)
        self.save_peer_list()
        self.refresh_peer_ui()
        self.log(f'已移除主机 {self.peer_label(ip)}', 'warn')

    def edit_peer_remark(self, ip=None):
        '''给主机写/改备注——多台同型号机器时靠它区分。'''

        ip = ip or self.selected_ip()
        if not ip:
            self.log('请先在左侧列表选中一台主机', 'warn')
            return
        info = self.peer_list.get(ip)
        if not info:
            return
        win = tk.Toplevel(self.root)
        win.title('主机备注')
        win.configure(bg=BG)
        win.attributes('-topmost', True)
        win.transient(self.root)
        win.resizable(False, False)
        card = ttk.Frame(win, style='Card.TFrame', padding=16)
        card.pack(fill='both', expand=True, padx=12, pady=12)
        ttk.Label(card, text=f'给 {ip} 取个好认的名字', style='H3.TLabel').pack(anchor='w')
        ttk.Label(card, text='例如「客厅台式机」「书房笔记本」「财务部-PC02」', style='Muted.TLabel').pack(anchor='w', pady=(2, 8))
        var = tk.StringVar(value=str(info.get('name') or ''))
        entry = ttk.Entry(card, textvariable=var, width=34)
        entry.pack(fill='x')
        entry.focus_set()
        entry.select_range(0, 'end')

        def save(_e=None):
            name = var.get().strip()[:24]
            with self.peer_lock:
                if ip in self.peer_list:
                    self.peer_list[ip]['name'] = name
            self.save_peer_list()
            self.refresh_peer_ui()
            self.log(f'{ip} 备注设为「{name}」' if name else f'已清除 {ip} 的备注', 'ok')
            win.destroy()

        bar = ttk.Frame(card, style='Card.TFrame')
        bar.pack(fill='x', pady=(12, 0))
        ttk.Button(bar, text='保存', style='Accent.TButton', command=save).pack(side='right')
        ttk.Button(bar, text='取消', command=win.destroy).pack(side='right', padx=(0, 8))
        if str(info.get('name') or '').strip():
            ttk.Button(bar, text='清除备注', command=lambda: (var.set(''), save())).pack(side='left')
        entry.bind('<Return>', save)
        win.bind('<Escape>', lambda e: win.destroy())
        win.update_idletasks()
        # 居中到主窗口
        try:
            wx = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
            wy = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
            win.geometry(f'+{max(0, wx)}+{max(0, wy)}')
        except tk.TclError:
            pass
        try:
            win.grab_set()
        except tk.TclError:
            pass

    def add_mobile_peer(self, ip_text):
        '''黑洞面板「添加手机设备」入口。返回 (ok, 错误信息)。'''

        text = str(ip_text or '').strip()
        try:
            ip = str(ipaddress.ip_address(text))
        except ValueError:
            return (False, 'IP 格式不正确，例如 192.168.101.66')
        if ip == self.local_ip:
            return (False, '这是本机地址，不需要添加')
        with self.peer_lock:
            exists = ip in self.peer_list
            self.peer_list[ip] = {'port': MOBI_PORT, 'ok': False, 'kind': 'mobile'}
        self.refresh_peer_ui()
        self.save_peer_list()
        self.log(f'{'已更新' if exists else '已添加'}手机设备 {ip}，手机打开投递页后自动标记在线', 'ok')
        return (True, '')

    def _poll_mobiles(self):
        '''每 30 秒探测手机设备：投递页最近活跃 或 ICMP 可达即视为在线。'''

        try:
            with self.peer_lock:
                mobs = [(ip, dict(info)) for ip, info in self.peer_list.items() if info.get('kind') == 'mobile']
            if mobs:
                def task():
                    results = {ip: self._probe_peer(ip, info) for ip, info in mobs}
                    def apply():
                        with self.peer_lock:
                            for ip, ok in results.items():
                                if ip in self.peer_list:
                                    self.peer_list[ip]['ok'] = ok
                        self.refresh_peer_ui()

                    self.ui_call(apply)

                threading.Thread(target=task, daemon=True).start()
            self.root.after(30000, self._poll_mobiles)
        except tk.TclError:
            return

    def clear_peers(self):
        if not self.peer_list:
            return
        if not messagebox.askyesno('确认', '清空全部主机列表？'):
            return
        with self.peer_lock:
            self.peer_list.clear()
        self.save_peer_list()
        self.refresh_peer_ui()
        self.log('已清空主机列表', 'warn')

    def tcp_hello(self, host, port):
        try:
            with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as s:
                s.settimeout(CONNECT_TIMEOUT)
                s.sendall(f'{MSG_HELLO}\n'.encode('utf-8'))
                data = s.recv(256).decode('utf-8', errors='ignore')
                return MSG_HELLO_ACK in data
        except Exception:
            return False

    def _icmp_ping(self, host, timeout_ms=2000):
        try:
            # 中文版 Windows 的 ping 输出是 GBK，按 UTF-8 解码会在 subprocess
            # 的读线程里直接抛 UnicodeDecodeError（只影响日志，但会刷一堆栈）。
            proc = subprocess.run(['ping', '-n', '1', '-w', str(timeout_ms), host], capture_output=True, text=True, errors='replace', timeout=timeout_ms / 1000 + 2, creationflags=subprocess.CREATE_NO_WINDOW)
            return proc.returncode == 0
        except Exception:
            return False

    def _probe_peer(self, ip, info):
        '''按设备类型选择探测方式：PC 用 POFF HELLO，手机用投递页活跃 / ICMP。'''

        if info.get('kind') == 'mobile':
            if self.mobi.is_recent(ip):
                return True
            return self._icmp_ping(ip)
        return self.tcp_hello(ip, info['port'])

    def check_all(self):
        with self.peer_lock:
            targets = list(self.peer_list.items())
        if not targets:
            return
        def task():
            results = {}
            def worker(ip, info):
                results[ip] = self._probe_peer(ip, info)

            threads = []
            for ip, info in targets:
                t = threading.Thread(target=worker, args=(ip, info), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            self.ui_call(self._after_check, results)

        threading.Thread(target=task, daemon=True).start()
        self._set_checking(True)
        self.log('正在并发校验连通状态...', 'info')

    def _after_check(self, results):
        with self.peer_lock:
            for ip, ok in results.items():
                if ip in self.peer_list:
                    self.peer_list[ip]['ok'] = ok
                    self.peer_list[ip]['checked'] = True
        self._set_checking(False)
        self.refresh_peer_ui()
        online = sum((1 for v in results.values() if v))
        self.log(f'校验完成：{online}/{len(results)} 台在线', 'ok')

    def check_selected(self):
        ip = self.selected_ip()
        if not ip:
            self.log('请先在左侧列表选中一台主机', 'warn')
            return
        info = self.peer_list.get(ip)
        if not info:
            self.log(f'主机列表里没有 {ip}，请刷新后重试', 'warn')
            return
        label = self.peer_label(ip)
        self.log(f'正在校验 {label} ...', 'info')
        self._set_checking(True)
        def task():
            ok = self._probe_peer(ip, info)
            def done():
                self._set_checking(False)
                if ip in self.peer_list:
                    self.peer_list[ip]['ok'] = ok
                    self.peer_list[ip]['checked'] = True
                self.refresh_peer_ui()
                self.log(f'{label} {"在线" if ok else "无法连接"}', 'ok' if ok else 'err')

            self.ui_call(done)

        threading.Thread(target=task, daemon=True).start()

    def _set_checking(self, busy):
        '''校验期间的可见反馈：按钮置灰改字、选中信息立刻变成"正在校验"。'''

        for name, text in (('btn_check', '连通校验'), ('btn_quick_check', '校验选中主机')):
            btn = getattr(self, name, None)
            if btn is None:
                continue
            try:
                btn.config(state='disabled' if busy else 'normal',
                           text='校验中…' if busy else text)
            except tk.TclError:
                pass
        if busy:
            try:
                self.lbl_sel_state.config(text='正在校验连通状态…')
            except tk.TclError:
                pass

    def tcp_precheck_one(self, host, port):
        result = {'ip': host, 'ok': False, 'apps': [], 'error': None}
        try:
            with socket.create_connection((host, port), timeout=PRECHECK_RECV_TIMEOUT) as sock:
                sock.settimeout(PRECHECK_RECV_TIMEOUT)
                sock.sendall(f'{MSG_PRECHECK}\n'.encode('utf-8'))
                chunks = []
                while True:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
        except socket.timeout:
            result['error'] = '连接超时'
        except (ConnectionRefusedError, OSError) as e:
            result['error'] = f'无法连接：{e}'
            return result
        except json.JSONDecodeError as e:
            result['error'] = f'响应解析失败：{e}'
            return result
        text = b''.join(chunks).decode('utf-8', errors='ignore')
        json_line = None
        for ln in text.splitlines():
            ln = ln.strip()
            if not ln.startswith('{'):
                continue
            if not ln.endswith('}'):
                continue
            json_line = ln
            break
        if json_line is None:
            result['error'] = '对方未返回预检结果（可能是旧版本）'
            return result
        payload = json.loads(json_line)
        result['ok'] = True
        result['apps'] = list(payload.get('apps', []))
        return result

    def tcp_precheck_all(self):
        with self.peer_lock:
            targets = [(ip, info) for ip, info in self.peer_list.items() if info.get('kind') != 'mobile']
        results = {}
        threads = []
        def worker(ip, port):
            results[ip] = self.tcp_precheck_one(ip, port)

        for ip, info in targets:
            t = threading.Thread(target=worker, args=(ip, info['port']), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return results

    def shot_for_selected(self):
        ip = self.selected_ip()
        if not ip:
            messagebox.showwarning('提示', '请先在左侧列表选中一台主机')
            return
        if not self.mod_vars['screenshot'].get():
            messagebox.showwarning('提示', '截屏模块未开启')
            return
        self.nb.select(self.tab_shot)
        self.screenshot.capture_full()

    def single_shutdown(self):
        target_ip = self.selected_ip()
        if not target_ip:
            self.log('请先在左侧列表选中一台主机', 'warn')
            return
        info = self.peer_list.get(target_ip)
        if not info:
            return
        self.btn_single.config(state='disabled', text='正在预检...')
        def task():
            pre = self.tcp_precheck_one(target_ip, info['port'])
            self.ui_call(self._show_single_precheck, target_ip, info['port'], pre)

        threading.Thread(target=task, daemon=True).start()

    def _show_single_precheck(self, target_ip, port, pre):
        self.btn_single.config(state='normal', text='单独关机')
        label = self.peer_label(target_ip)
        win = tk.Toplevel(self.root)
        win.title(f'关机预检 · {label}')
        win.geometry('640x340')
        win.configure(bg=BG)
        win.attributes('-topmost', True)
        win.transient(self.root)
        win.grab_set()
        card = ttk.Frame(win, style='Card.TFrame', padding=14)
        card.pack(fill='both', expand=True, padx=12, pady=12)
        ttk.Label(card, text=f'目标主机 {label}', style='H3.TLabel').pack(anchor='w')
        ttk.Label(card, text='单独关机指令发出后立即执行、没有倒计时。以下是对方当前运行的高风险应用：', style='Muted.TLabel', wraplength=580).pack(anchor='w', pady=(2, 8))
        text = tk.Text(card, height=8, wrap='word', relief='flat', bg=SOFT, fg=TEXT, font=('Microsoft YaHei UI', 9), padx=8, pady=6)
        text.pack(fill='both', expand=True)
        if not pre['ok']:
            text.insert('end', f'注意：无法预检（{pre['error']}）\n\n对方可能离线或版本过旧，仍可尝试发送关机指令。')
        elif pre['apps']:
            text.insert('end', '注意：以下应用正在运行，关机时可能弹出保存对话框：\n\n')
            for app in pre['apps']:
                text.insert('end', f'   · {app}\n')
        else:
            text.insert('end', '未检测到高风险应用，可以安全关机。')
        text.config(state='disabled')
        decided = {'value': False}
        def go():
            decided['value'] = True
            win.destroy()

        def cancel():
            decided['value'] = False
            win.destroy()

        bar = ttk.Frame(card, style='Card.TFrame')
        bar.pack(fill='x', pady=(10, 0))
        ttk.Label(bar, text='确认立即关机？', style='Warn.TLabel').pack(side='left')
        ttk.Button(bar, text='继续关机', style='Danger.TButton', command=go).pack(side='right')
        ttk.Button(bar, text='取消', command=cancel).pack(side='right', padx=(0, 8))
        win.protocol('WM_DELETE_WINDOW', cancel)
        win.wait_window()
        if not decided['value']:
            return
        def fire():
            ok = self.send_msg(target_ip, port, MSG_SINGLE_SHUTDOWN)
            self.ui_call(self.log, f'关机指令已发送至 {label}' if ok else f'无法连接 {label}', 'ok' if ok else 'err')

        threading.Thread(target=fire, daemon=True).start()

    def start_sync(self):
        if self.countdown_win is not None:
            return
        with self.peer_lock:
            has_pc = any(info.get('kind') != 'mobile' for info in self.peer_list.values())
        if not has_pc:
            messagebox.showinfo('提示', '当前列表没有可关机的电脑主机（手机设备不参与关机）。')
            return
        if not self.peer_list:
            return
        self.btn_start.config(state='disabled', text='正在预检...')
        def task():
            results = self.tcp_precheck_all()
            self.ui_call(self._show_precheck_dialog, results)

        threading.Thread(target=task, daemon=True).start()

    def _show_precheck_dialog(self, results):
        self.btn_start.config(state='normal', text='启动 10 秒并行同步关机')
        win = tk.Toplevel(self.root)
        win.title('关机前安全预检')
        win.geometry('760x460')
        win.configure(bg=BG)
        win.attributes('-topmost', True)
        win.transient(self.root)
        win.grab_set()
        card = ttk.Frame(win, style='Card.TFrame', padding=14)
        card.pack(fill='both', expand=True, padx=12, pady=12)
        ttk.Label(card, text='安全预检结果', style='H2.TLabel').pack(anchor='w')
        ttk.Label(card, text='关机时 Windows 会向各应用发送关闭请求，未保存的文档会弹出对话框等待处理。建议先让远端的同事保存，再确认关机。', style='Muted.TLabel', wraplength=690, justify='left').pack(anchor='w', pady=(2, 10))
        cols = ('ip', 'status', 'apps')
        tree = ttk.Treeview(card, columns=cols, show='headings', height=9)
        tree.heading('ip', text='主机')
        tree.heading('status', text='状态')
        tree.heading('apps', text='检测到的应用')
        tree.column('ip', width=150, anchor='w')
        tree.column('status', width=110, anchor='center')
        tree.column('apps', width=420, anchor='w')
        tree.tag_configure('err', foreground=ERR)
        tree.tag_configure('warn', foreground=WARN)
        tree.tag_configure('ok', foreground=OK)
        tree.pack(fill='both', expand=True)
        attention = 0
        for ip in sorted(results):
            r = results[ip]
            if not r['ok']:
                tree.insert('', 'end', values=(ip, '离线/旧版', r['error'] or '无法连接'), tags=('err',))
                attention += 1
            elif r['apps']:
                tree.insert('', 'end', values=(ip, '有应用在跑', ', '.join(r['apps'])), tags=('warn',))
                attention += 1
            else:
                tree.insert('', 'end', values=(ip, '安全', '—'), tags=('ok',))
        summary = f'共 {len(results)} 台主机，其中 {attention} 台需要关注'
        ttk.Label(card, text=summary, style='Warn.TLabel' if attention else 'Ok.TLabel', font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w', pady=8)
        decided = {'value': False}
        def go():
            decided['value'] = True
            win.destroy()

        def cancel():
            decided['value'] = False
            win.destroy()

        bar = ttk.Frame(card, style='Card.TFrame')
        bar.pack(fill='x')
        ttk.Button(bar, text='继续关机（风险自负）', style='Accent.TButton', command=go).pack(side='right')
        ttk.Button(bar, text='取消关机', command=cancel).pack(side='right', padx=(0, 8))
        win.protocol('WM_DELETE_WINDOW', cancel)
        win.wait_window()
        if not decided['value']:
            self.log('已取消并行关机', 'warn')
            return
        self.send_msg_to_all_async(MSG_START_COUNTDOWN)
        self.open_countdown_window(is_initiator=True)
        self.log('已广播同步关机指令，各主机进入 10 秒倒计时', 'warn')

    def open_countdown_window(self, is_initiator):
        if self.countdown_win is not None:
            self.log('已存在进行中的倒计时，忽略重复请求', 'warn')
            return
        self.countdown_is_initiator = bool(is_initiator)
        self.countdown_cancelled = False
        self.countdown_left = 10
        win = tk.Toplevel(self.root)
        self.countdown_win = win
        win.title('即将关机')
        win.geometry('420x300')
        win.configure(bg=CARD)
        win.attributes('-topmost', True)
        win.resizable(False, False)
        win.transient(self.root)
        tk.Label(win, text='同步关机倒计时', bg=CARD, fg=TEXT, font=('Microsoft YaHei UI', 12, 'bold')).pack(pady=(24, 4))
        self.lbl_count = tk.Label(win, text='10', bg=CARD, fg=ERR, font=('Microsoft YaHei UI', 56, 'bold'))
        self.lbl_count.pack()
        tk.Label(win, text='倒计时结束后本机与已同步主机将关机\n点击下方按钮可取消本次关机', bg=CARD, fg=MUTED, font=('Microsoft YaHei UI', 9), justify='center').pack()
        ttk.Button(win, text='取消关机', style='Accent.TButton', command=self.cancel_countdown).pack(pady=12)
        win.protocol('WM_DELETE_WINDOW', self.cancel_countdown)
        self._tick_countdown()

    def _tick_countdown(self):
        win = self.countdown_win
        if win is None or self.countdown_cancelled:
            return
        if self.countdown_left <= 0:
            self._finish_countdown()
            return
        try:
            self.lbl_count.config(text=str(self.countdown_left))
        except tk.TclError:
            pass
        else:
            self.countdown_left -= 1
            win.after(1000, self._tick_countdown)

    def _finish_countdown(self):
        win = self.countdown_win
        is_init = self.countdown_is_initiator
        self.countdown_win = None
        try:
            if win is not None:
                win.destroy()
        except tk.TclError:
            pass
        self.log('倒计时结束，开始关机', 'warn')
        if is_init:
            self.send_msg_to_all_async(MSG_SHUTDOWN)
        do_local_shutdown()

    def cancel_countdown(self, broadcast=True, silent=False):
        if self.countdown_win is None:
            return
        self.countdown_cancelled = True
        if broadcast:
            self.send_msg_to_all_async(MSG_CANCEL)
        win = self.countdown_win
        self.countdown_win = None
        try:
            if win is not None:
                win.destroy()
        except tk.TclError:
            pass
        if not silent:
            self.log('已取消本次关机', 'warn')

    def _close_countdown_window(self):
        win = self.countdown_win
        self.countdown_win = None
        self.countdown_cancelled = True
        try:
            if win is not None:
                win.destroy()
                return
        except tk.TclError:
            pass

    def start_server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        exclusive = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
        try:
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            sock.bind(('0.0.0.0', TCP_PORT))
            sock.listen(16)
        except OSError as e:
            sock.close()
            self.log(f'监听 {TCP_PORT} 失败：{e}', 'err')
            messagebox.showerror('端口被占用', f'无法监听 {TCP_PORT} 端口，可能已有一个 POFF 正在运行。\n请关闭重复实例后重启本程序。')
            return
        self.server_socket = sock
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while self.server_socket is not None:
            try:
                conn, addr = self.server_socket.accept()
            except OSError:
                return
            threading.Thread(target=self.handle_conn, args=(conn,), daemon=True).start()

    @staticmethod
    def _close_conn(f, conn):
        try:
            f.close()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    def handle_conn(self, conn):
        '''单连接单指令；FILE_PUSH 走长度头部 + 原始字节流。

        早期版本这里只认 HELLO / PRECHECK，其余分支会落到一个未定义的
        `blob` 上抛 NameError 后直接关连接 —— 于是 START / SHUTDOWN /
        SINGLE_SHUTDOWN / CANCEL / FILE_PUSH 全部收不到，`dispatch_msg`
        成了死代码（关机联动、文件投递、屏幕共享邀请都是这么哑掉的）。
        现在按标签显式分发。
        '''

        remote_ip = conn.getpeername()[0]
        conn.settimeout(120)
        f = conn.makefile('rb')
        try:
            line = f.readline(4096)
            if not line:
                self._close_conn(f, conn)
                return
            cmd = line.decode('utf-8', errors='ignore').strip()
            if cmd == MSG_HELLO:
                try:
                    conn.sendall(f'{MSG_HELLO_ACK}\n'.encode('utf-8'))
                except OSError:
                    pass
                self._close_conn(f, conn)
                return
            if cmd == MSG_PRECHECK:
                apps = scan_running_user_apps()
                payload = {'app': CODE_APP_TAG, 'v': CODE_VERSION, 'ip': self.local_ip, 'apps': apps}
                blob = f'{MSG_PRECHECK}\n{json.dumps(payload, ensure_ascii=False)}\n'
                try:
                    conn.sendall(blob.encode('utf-8'))
                except OSError:
                    pass
                self._close_conn(f, conn)
                return
            if cmd == transfer.MSG_FILE_PUSH:
                self._receive_file(f, conn, remote_ip)
                self._close_conn(f, conn)
                return
            if cmd == MSG_SHARE:
                # 第二行是 JSON：{"type": "start"/"stop", "url": ..., "ip": ...}
                try:
                    raw = f.readline(65536).decode('utf-8', errors='ignore')
                    payload = json.loads(raw) if raw.strip() else {}
                except Exception:
                    payload = {}
                try:
                    conn.sendall(b'SHARE_OK\n')
                except OSError:
                    pass
                self._close_conn(f, conn)
                self.ui_call(self._on_share_msg, payload, remote_ip)
                return
            # START / CANCEL / SHUTDOWN / SINGLE_SHUTDOWN
            self._close_conn(f, conn)
            self.ui_call(self.dispatch_msg, cmd)
        except Exception as e:
            print(f'处理连接失败: {e}')
            self._close_conn(f, conn)

    def _on_share_msg(self, payload, remote_ip):
        '''收到对端屏幕共享邀请 / 停止（主线程执行）。'''

        t = payload.get('type')
        if t == 'start':
            url = payload.get('url', '')
            peer = payload.get('ip') or remote_ip
            if not url:
                self.log(f'收到 {peer} 的共享邀请但缺少地址，已忽略', 'warn')
                return
            try:
                from poff_mods.screenshare import ScreenReceiver
            except Exception as e:
                self.log(f'屏幕共享模块加载失败：{e}', 'err')
                return
            prev = getattr(self, '_screen_receiver', None)
            if prev is not None:
                try:
                    prev.close()
                except Exception:
                    pass
            try:
                self._screen_receiver = ScreenReceiver(self, url, peer_ip=peer)
                self.log(f'收到 {peer} 的屏幕共享邀请：{url}', 'ok')
            except Exception as e:
                self.log(f'打开屏幕共享接收窗口失败：{e}', 'err')
            return
        if t == 'stop':
            r = getattr(self, '_screen_receiver', None)
            if r is None:
                return
            try:
                r.close()
            except Exception:
                pass
            self._screen_receiver = None
            self.log(f'{payload.get("ip") or remote_ip} 已停止屏幕共享', 'info')

    def _receive_file(self, f, conn, remote_ip):
        header_line = f.readline(65536)
        if not header_line:
            return
        try:
            header = json.loads(header_line.decode('utf-8', errors='ignore'))
        except Exception:
            self._file_reply(conn, False, '文件头解析失败')
            return
        name = st.sanitize_filename(header.get('name'), 'received_file')
        try:
            size = int(header.get('size', 0))
        except Exception:
            size = -1
        if size <= 0 or size > transfer.MAX_FILE_SIZE:
            self._file_reply(conn, False, '文件大小非法')
            return
        kind = 'screenshot' if header.get('kind') == 'screenshot' else 'file'
        target_dir = self.screenshot_dir if kind == 'screenshot' else self.receive_dir
        st.ensure_dir(target_dir)
        tmp_path = os.path.join(target_dir, f'.{os.getpid()}_{int(time.time() * 1000)}.part')
        digest = hashlib.sha256()
        remaining = size
        try:
            with open(tmp_path, 'wb') as out:
                while remaining > 0:
                    block = f.read(min(transfer.CHUNK, remaining))
                    if not block:
                        raise OSError('连接提前断开')
                    out.write(block)
                    digest.update(block)
                    remaining -= len(block)
        except OSError as e:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            self._file_reply(conn, False, f'接收失败：{e}')
        if header.get('sha256') and digest.hexdigest() != header['sha256']:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            self._file_reply(conn, False, '校验不一致，已丢弃')
            return
        final_path = st.unique_path(os.path.join(target_dir, name))
        try:
            os.replace(tmp_path, final_path)
        except OSError as e:
            self._file_reply(conn, False, f'写入失败：{e}')
            return
        self._file_reply(conn, True, '接收成功', final_path)
        self.ui_call(self.on_file_received, final_path, kind, remote_ip)

    def _file_reply(self, conn, ok, message, path=None):
        tag = transfer.MSG_FILE_OK if ok else transfer.MSG_FILE_ERR
        payload = {'path': path or '', 'error': '' if ok else message}
        try:
            conn.sendall((tag + '\n' + json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
        except OSError:
            pass

    def on_file_received(self, path, kind, remote_ip):
        size = st.human_size(os.path.getsize(path)) if os.path.exists(path) else '-'
        if kind == 'screenshot':
            self.log(f'收到来自 {remote_ip} 的截图：{os.path.basename(path)}（{size}）', 'ok')
            self.screenshot.receive_preview(path, remote_ip)
            return
        self.log(f'收到来自 {remote_ip} 的文件：{os.path.basename(path)}（{size}）', 'ok')
        win = tk.Toplevel(self.root)
        win.title('收到文件')
        win.configure(bg=BG)
        win.attributes('-topmost', True)
        win.geometry('520x180')
        card = ttk.Frame(win, style='Card.TFrame', padding=14)
        card.pack(fill='both', expand=True, padx=12, pady=12)
        ttk.Label(card, text=f'来自 {remote_ip} 的文件', style='H3.TLabel').pack(anchor='w')
        ttk.Label(card, text=f'{os.path.basename(path)}　{size}', style='Muted.TLabel').pack(anchor='w', pady=(4, 4))
        ttk.Label(card, text=path, style='Muted.TLabel', wraplength=470).pack(anchor='w')
        bar = ttk.Frame(card, style='Card.TFrame')
        bar.pack(fill='x', pady=(10, 0))
        ttk.Button(bar, text='打开文件夹', command=(lambda: self.open_path(os.path.dirname(path)))).pack(side='left')
        ttk.Button(bar, text='关闭', command=win.destroy).pack(side='right')

    def dispatch_msg(self, msg):
        if msg == MSG_START_COUNTDOWN:
            if self.countdown_win is not None:
                self.log('收到重复的关机指令，已在倒计时中，忽略', 'warn')
                return
            self.log('收到远端同步关机指令，进入倒计时', 'warn')
            self.open_countdown_window(is_initiator=False)
            return
        if msg == MSG_CANCEL:
            if self.countdown_win is not None:
                self.cancel_countdown(broadcast=False, silent=True)
                self.log('远端已取消关机，本地倒计时终止', 'warn')
                return
            return
        if msg == MSG_SHUTDOWN:
            self.log('收到远端关机指令，立即关机', 'warn')
            do_local_shutdown()
            return
        if msg == MSG_SINGLE_SHUTDOWN:
            self.log('收到远端单独关机指令，立即关机', 'warn')
            do_local_shutdown()

    def send_msg(self, ip, port, msg):
        try:
            with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT) as s:
                s.sendall((msg + '\n').encode('utf-8'))
        except Exception:
            return False
        return True

    def send_msg_json(self, ip, port, tag, payload, expect_reply=True):
        '''发「标签行 + JSON 行」，用于需要带参数的指令（如屏幕共享邀请）。

        要求对端回一行 ACK：旧版 POFF 收到未知标签会直接关连接，
        这时 recv 拿到空字节 —— 返回 False，调用方可以据此提示
        "对方版本过旧"，而不是误报"已通知"。
        '''

        try:
            with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT) as s:
                s.settimeout(1.2)
                s.sendall((tag + '\n' + json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
                if expect_reply:
                    try:
                        reply = s.recv(256)
                    except Exception:
                        reply = b''
                    if not reply:
                        return False
        except Exception:
            return False
        return True

    def send_msg_to_all_async(self, msg):
        with self.peer_lock:
            targets = [(ip, info) for ip, info in self.peer_list.items() if info.get('kind') != 'mobile']
        def task():
            for ip, info in targets:
                self.send_msg(ip, info['port'], msg)

        threading.Thread(target=task, daemon=True).start()

    def send_files_async(self, paths, ip, kind='file', on_done=None, on_progress=None):
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            if on_done:
                on_done(False, '没有可发送的文件')
            return
        info = self.peer_list.get(ip)
        if info and info.get('kind') == 'mobile':
            return self.mobi.push_files_async(paths, on_done=on_done)
        port = info['port'] if info else TCP_PORT
        def task():
            ok_all = True
            last_err = ''
            for idx, path in enumerate(paths, start=1):
                def progress(sent, total, p=path):
                    if on_progress:
                        self.ui_call(on_progress, p, sent, total)

                ok, msg = transfer.send_file(ip, port, path, kind=kind, progress=progress)
                if ok:
                    self.log(f'文件已送达 {ip}：{os.path.basename(path)}（{idx}/{len(paths)}）', 'ok')
                else:
                    ok_all = False
                    last_err = msg
                    self.log(f'发送到 {ip} 失败：{os.path.basename(path)} - {msg}', 'err')
            if on_done:
                summary = f'{len(paths)} 个文件已发送' if ok_all else last_err
                self.ui_call(on_done, ok_all, summary)

        threading.Thread(target=task, daemon=True).start()

    def open_path(self, path):
        if not st.open_path(path):
            self.log(f'无法打开路径：{path}', 'err')

    def on_module_toggle(self, key):
        self.cfg.setdefault('modules', {})[key] = bool(self.mod_vars[key].get())
        self.save_cfg()
        self._apply_module_states()
        if key == 'blackhole':
            on = self.mod_vars[key].get()
            self.blackhole.set_enabled(on)
            if on and self.cfg.get('blackhole', {}).get('show', True):
                self.blackhole.ensure_panel()
        elif key == 'recorder':
            self.recorder.set_enabled(self.mod_vars[key].get())
        elif key == 'screenshare':
            self.screenshare.set_enabled(self.mod_vars[key].get())
        self.log(f'{'已开启' if self.mod_vars[key].get() else '已关闭'}模块：{key}', 'info')

    def _apply_module_states(self):
        shot_on = self.mod_vars['screenshot'].get()
        self.screenshot.set_enabled(shot_on)
        self.recorder.set_enabled(self.mod_vars.get('recorder', True))
        self.brightness.set_enabled(self.mod_vars['brightness'].get())
        self.tablet.set_enabled(self.mod_vars['tablet'].get())
        self.roam.set_enabled(self.mod_vars['roam'].get())
        self.blackhole.set_enabled(self.mod_vars['blackhole'].get())
        self.screenshare.set_enabled(self.mod_vars.get('screenshare', True))
        self._init_hotkeys()

    def save_cfg(self):
        save_cfg_file(self.cfg)

    def _init_hotkeys(self):
        try:
            if self.hotkey_mgr is not None:
                self.hotkey_mgr.stop()
                self.hotkey_mgr = None
        except Exception:
            pass
        bindings = {}
        if self.mod_vars.get('screenshot') and self.mod_vars['screenshot'].get():
            bindings['shot_full'] = st.MOD_CONTROL | st.MOD_SHIFT, 70
            bindings['shot_area'] = st.MOD_ALT, 81
            bindings['shot_area2'] = st.MOD_CONTROL | st.MOD_SHIFT, 83
        if self.mod_vars.get('blackhole') and self.mod_vars['blackhole'].get():
            bindings['blackhole'] = st.MOD_CONTROL | st.MOD_SHIFT, 72
        if self.mod_vars.get('roam') and self.mod_vars['roam'].get():
            bindings['roam_lock'] = st.MOD_CONTROL | st.MOD_SHIFT, 76
        try:
            self.hotkey_mgr = st.HotkeyManager(bindings, self._on_hotkey)
            self.hotkey_mgr.start()
        except Exception as e:
            self.log(f'全局热键注册失败：{e}', 'warn')
            return
        def report():
            try:
                if self.hotkey_mgr and self.hotkey_mgr.failures:
                    names = '、'.join(self.hotkey_mgr.failures)
                    self.log(f'以下热键被其他程序占用：{names}', 'warn')
                    return
            except Exception:
                pass

        self.root.after(600, report)

    def _on_hotkey(self, name):
        self.ui_call(self._handle_hotkey, name)

    def _handle_hotkey(self, name):
        if name == 'shot_full':
            if self.mod_vars['screenshot'].get():
                self.screenshot.capture_full()
                return
            return
        if name in ('shot_area', 'shot_area2'):
            if self.mod_vars['screenshot'].get():
                self.screenshot.capture_region()
                return
            return
        if name == 'blackhole':
            self.blackhole.toggle_panel()
            return
        if name == 'roam_lock':
            self.roam_locked = not self.roam_locked
            self.roam.update_lock_label()
            self.log('漫游已锁定' if self.roam_locked else '漫游已解锁', 'warn')

    def _refresh_admin_chip(self):
        if st.is_admin():
            self.chip_admin.config(text='● 管理员权限', fg='#4ADE80')
            return
        self.chip_admin.config(text='普通权限 · 点击提权', fg='#FBBF24')

    def _on_admin_chip(self):
        if st.is_admin():
            messagebox.showinfo('权限状态', '当前已具备管理员权限。')
            return
        if not messagebox.askyesno('以管理员身份重启', '重启后本程序将获得管理员权限，可启停 Spacedesk 服务。\n注意：管理员实例不会随开机自启静默启动。是否继续？'):
            return
        if st.relaunch_as_admin():
            self.log('正在以管理员身份重启...', 'info')
            self.root.after(400, self.quit_app)
            return
        messagebox.showerror('提权失败', '未能启动管理员实例（UAC 被取消或系统限制）。')

    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title('使用说明')
        win.configure(bg=BG)
        win.geometry('700x520')
        text = tk.Text(win, wrap='word', relief='flat', bg=CARD, fg=TEXT, font=('Microsoft YaHei UI', 9), padx=16, pady=14)
        text.pack(fill='both', expand=True, padx=12, pady=12)
        content = 'POFF 使用说明\n\n如遇问题或需要定制功能，欢迎联系开发者微信：shot2021\n\n一、配对\n  两台电脑都运行 POFF，任一侧点击「生成本机串码」并复制给对方；\n  对方粘贴后点「添加主机」即可。列表会自动保存，下次无需重复添加。\n  主机多时建议填「备注」（客厅台式机 / 书房笔记本），列表和黑洞面板都会显示；\n  之后要改，选中主机点「编辑备注」或右键列表条目即可。\n  若路由器更换了 IP，串码会失效，建议在路由器上为这两台机器绑定 IP。\n\n二、关机\n  单独关机：先扫描对方运行的高风险应用，确认后立即关机，无倒计时。\n  并行关机：先预检全部主机，再进入 10 秒倒计时；倒计时内可取消。\n  关机命令使用系统优雅关机（不带 /f），不会强杀进程，但对方若有未保存文档\n  仍会弹出保存对话框，需要有人在那台机器上处理。手机设备不参与关机。\n\n三、截屏\n  Alt+Q 选区截屏（Ctrl+Shift+S 同样可用），Ctrl+Shift+F 全屏截屏。\n  截完立即自动保存到截图目录，随后打开编辑窗口：\n  画笔 / 矩形 / 箭头 / 马赛克，可撤销（Ctrl+Z），可复制（Ctrl+C）；\n  标注直接写回同一张已保存的图片，也可另存或发送到其他主机。\n  Alt+Q 是全局热键，关闭截屏模块即可释放该组合键。\n\n四、黑洞投递\n  鼠标移到屏幕右侧细条展开悬浮面板，把文件拖到主机条目即可投递；\n  对方文件保存在接收目录，并弹窗提示。\n\n五、手机投递（双向、零安装）\n  在「黑洞投递」页查看二维码，手机浏览器扫码（或输入网址）打开投递页；\n  电脑投递到手机的文件先进入待接收区，手机在页面点按即可下载，\n  下载完成后自动清理；手机选文件上传，电脑接收目录弹窗提示。\n  「添加手机设备」可把手机加入设备目录与悬浮面板；配对码可重新生成，\n  旧码立即失效。手机无法被远程关机，关机指令会自动跳过。\n\n六、亮度 / 平板副屏\n  亮度支持笔记本内置屏（WMI）与外接显示器（DDC/CI，需在显示器菜单开启）；\n  平板亮度由平板系统自行控制。平板副屏通过调度 Spacedesk 服务实现，\n  启停服务需要管理员权限。\n\n七、跨屏键鼠漫游（双端双向）\n  两台主机都打开漫游开关，各自选择「本机角色」与「漫游目标」，并把布局调成\n  互为镜像（例如 A 看笔记本在右，则 B 看主机在左）。把鼠标推到本机屏幕边缘\n  并停留片刻即进入漫游，键鼠转发到对方；把光标移回该边缘内侧即返回本机。\n  漫游中 Ctrl+Shift+L 可紧急退回本机；建议以管理员权限运行获得完整体验。\n\n八、权限与自启\n  默认普通权限启动，避免开机自启被 UAC 卡住；需要管理员时点击右上角提权。\n  首次运行如弹出防火墙提示，请允许「专用网络」，否则收不到局域网指令。\n  最小化或点 × 不会退出：通知区图标显示「运行中」，并把黑洞面板弹出来方便拖文件；\n  不想自动弹的话，右键托盘图标取消勾选，或到「设置 → 后台运行」关掉。\n'
        text.insert('end', content)
        text.config(state='disabled')

    def toggle_autostart(self):
        want = self.var_autostart.get()
        if not set_autostart(want):
            self.var_autostart.set(not want)
            messagebox.showerror('设置失败', '无法修改开机自启项，请检查系统权限。')
            return
        self.log('已开启开机自启' if want else '已关闭开机自启', 'ok')

    # ---------------- 托盘 / 后台行为 ----------------
    def open_on_hide_enabled(self):
        '''最小化 / 关闭主窗口时是否自动弹出黑洞面板。'''

        return bool(self.cfg.get('blackhole', {}).get('open_on_hide', True))

    def set_open_on_hide(self, on, notify=True):
        bh = self.cfg.setdefault('blackhole', {})
        bh['open_on_hide'] = bool(on)
        self.save_cfg()
        var = getattr(self, 'var_open_on_hide', None)
        if var is not None:
            try:
                var.set(bool(on))
            except tk.TclError:
                pass
        try:
            if self.tray_icon is not None:
                self.tray_icon.update_menu()
        except Exception:
            pass
        if notify:
            self.log('最小化 / 关闭时自动打开黑洞面板：已开启' if on else '最小化 / 关闭时自动打开黑洞面板：已关闭', 'ok')

    def toggle_open_on_hide(self):
        self.set_open_on_hide(not self.open_on_hide_enabled())

    def _blackhole_available(self):
        if not self.mod_vars.get('blackhole') or not self.mod_vars['blackhole'].get():
            return False
        if not self.cfg.get('modules', {}).get('blackhole', True):
            return False
        return True

    def _watch_window_state(self):
        '''盯着主窗口的最小化状态。

        自定义标题栏的「—」按钮走 _win_minimize，但用户也可能用任务栏缩略图
        右键 → 最小化、快捷键等由系统直接下发 SC_MINIMIZE，那条路径不经过
        我们的按钮回调。这里用 600ms 轮询兜底，状态一变就补上同样的行为。
        '''

        try:
            st = self.root.state()
        except tk.TclError:
            return
        prev = getattr(self, '_win_state', 'normal')
        if st != prev:
            self._win_state = st
            if st == 'iconic':
                self._auto_open_blackhole()
            elif st == 'normal' and prev == 'iconic':
                # 主窗口回来了，把刚才自动弹出的面板收回细条，别挡着干活
                try:
                    if getattr(self.blackhole, '_pinned', False):
                        self.blackhole.collapse(force=True)
                except Exception:
                    pass
        self._root_after(600, self._watch_window_state)

    def _auto_open_blackhole(self):
        '''窗口收起来后把黑洞面板叫出来——否则主界面一没，程序看着就像退出了。'''

        if not self.open_on_hide_enabled() or not self._blackhole_available():
            return
        try:
            self.blackhole.ensure_panel()
            self.blackhole.expand(pin=True)
        except Exception as e:
            print(f'自动打开黑洞面板失败: {e}')

    def _tray_menu(self, pystray):
        return pystray.Menu(
            pystray.MenuItem('显示主界面', (lambda *_: self.ui_call(self.show_window)), default=True),
            pystray.MenuItem('黑洞面板', (lambda *_: self.ui_call(self.blackhole.toggle_panel))),
            pystray.MenuItem('最小化时自动打开黑洞面板',
                             (lambda *_: self.ui_call(self.toggle_open_on_hide)),
                             checked=(lambda item: self.open_on_hide_enabled())),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('退出 POFF', (lambda *_: self.ui_call(self.quit_app))),
        )

    def _init_tray(self):
        try:
            import pystray
            from PIL import Image
        except Exception:
            return
        try:
            image = Image.open(_resource_path('poff.ico'))
            icon = pystray.Icon('poff', image, f'{APP_TITLE} · 运行中', self._tray_menu(pystray))
            threading.Thread(target=self._tray_runner, args=(icon,), daemon=True).start()
        except Exception as e:
            print(f'托盘初始化失败: {e}')
            return
        return icon

    def _tray_runner(self, icon):
        try:
            icon.run()
        except Exception as e:
            print(f'托盘运行失败: {e}')
            self.ui_call(self._on_tray_failed)
            return

    def _on_tray_failed(self):
        self.tray_icon = None
        self.show_window()

    def show_window(self):
        self.root.deiconify()
        self.root.update_idletasks()
        # 从托盘唤出时显示器布局可能已经变了（外接屏拔掉 / 换了分辨率），
        # 这时窗口矩形可能整个落在看不见的坐标上，先夹回可见区域。
        self._ensure_visible()
        self.frame32.restyle()
        self.root.lift()
        if self.mod_vars.get('blackhole') and self.mod_vars['blackhole'].get() and self.cfg.get('blackhole', {}).get('show', True):
            try:
                self.blackhole.ensure_panel()
            except Exception:
                pass
        # 之前是因为最小化 / 关闭主窗口自动弹出的面板，现在主界面回来了就收回细条。
        # （托盘路径走的是 withdraw，状态是 withdrawn 不是 iconic，
        #   _watch_window_state 那条链路抓不到，所以在这里补一次。）
        try:
            if getattr(self.blackhole, '_pinned', False):
                self.blackhole.collapse(force=True)
        except Exception:
            pass
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def hide_to_tray(self):
        self.root.withdraw()
        # 早先这里什么都不做，主界面一 withdraw，桌面上只剩托盘图标，
        # 用户以为程序退了；现在默认把黑洞面板留在桌面上当"还活着"的入口。
        self._auto_open_blackhole()
        if not self._tray_hinted:
            self._tray_hinted = True
            try:
                self.tray_icon.notify('POFF 仍在后台运行，正在接收局域网指令。\n右键托盘图标可显示主界面或退出。', APP_TITLE)
            except Exception:
                pass

    def on_main_close(self):
        if self.countdown_win is not None:
            if not messagebox.askyesno('确认', '倒计时进行中，关闭主窗口会取消本次关机。\n是否继续？', icon='warning'):
                return
            self.countdown_cancelled = True
            self.send_msg_to_all_async(MSG_CANCEL)
            self._close_countdown_window()
        if self.tray_icon is not None:
            self.hide_to_tray()
            return
        try:
            self.blackhole.hide_panel()
        except Exception:
            pass
        self.quit_app()

    def quit_app(self):
        if self.countdown_win is not None:
            self.send_msg_to_all_async(MSG_CANCEL)
        try:
            self.roam.stop_engine()
        except Exception:
            pass
        try:
            self.mobi.stop()
        except Exception:
            pass
        try:
            if self.hotkey_mgr is not None:
                self.hotkey_mgr.stop()
        except Exception:
            pass
        try:
            if self.tray_icon is not None:
                self.tray_icon.stop()
        except Exception:
            pass
        self.tray_icon = None
        self._close_countdown_window()
        try:
            if self.server_socket is not None:
                self.server_socket.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = None
    if _DND_OK:
        try:
            root = TkinterDnD.Tk()
        except Exception:
            root = None
    if root is None:
        root = tk.Tk()
    # 实例无需保存到变量：Tk 的 protocol / after / bind 回调都持有其方法，不会被回收
    PoffApp(root)
    root.mainloop()

if __name__ == '__main__':
    main()
# WARNING: Decompyle incomplete
