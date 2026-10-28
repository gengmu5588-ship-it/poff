# -*- coding: utf-8 -*-
"""POFF V2.2　局域网多主机控制工具

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
"""
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
from tkinter import ttk, messagebox, filedialog

from poff_mods import system_tools as st
from poff_mods import transfer
from poff_mods.blackhole import BlackholeModule
from poff_mods.kvm import RoamModule, handle_kvm_session, KVM_CTRL
from poff_mods.mobidrop import MOBI_PORT, MobiDropModule
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

# ====================================================
APP_NAME = "POFF V2.2 局域网多主机控制"
APP_TITLE = "POFF V2.2"
VERSION = "2.3.2"
CODE_APP_TAG = "poff"
CODE_VERSION = 2

TCP_PORT = 17890
CONNECT_TIMEOUT = 1.0
STARTUP_MODE = "--startup" in sys.argv
AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_NAME = "Poff"

MSG_HELLO = "HELLO"
MSG_HELLO_ACK = "HELLO_ACK"
MSG_START_COUNTDOWN = "START"
MSG_CANCEL = "CANCEL"
MSG_SHUTDOWN = "SHUTDOWN"
MSG_SINGLE_SHUTDOWN = "SINGLE_SHUTDOWN"
MSG_PRECHECK = "PRECHECK"

PRECHECK_RECV_TIMEOUT = 5.0
PRECHECK_SCAN_TIMEOUT = 8.0

# 关机时会弹"是否保存"对话框的高风险应用白名单
USER_APP_PROCESS_NAMES = [
    "WINWORD.EXE", "EXCEL.EXE", "POWERPNT.EXE", "ONENOTE.EXE", "OUTLOOK.EXE",
    "ACCESS.EXE", "MSPUB.EXE",
    "WPS.EXE", "ET.EXE", "WPP.EXE", "WPSPP.EXE",
    "NOTEPAD.EXE", "WORDPAD.EXE",
    "ACRORD32.EXE", "ACROBAT.EXE", "ACROBAT_RW.EXE",
    "PHOTOSHOP.EXE", "ILLUSTRATOR.EXE", "PAINT.NET.EXE", "GIMP-2.10.EXE", "MSPAINT.EXE",
    "CODE.EXE", "PYCHARM64.EXE", "IDEA64.EXE", "RIDER64.EXE", "STUDIO64.EXE",
    "CLION64.EXE", "WEBIDEAS64.EXE", "NOTEPAD++.EXE", "SUBLIME_TEXT.EXE",
    "CHROME.EXE", "MSEDGE.EXE", "FIREFOX.EXE", "OPERA.EXE", "BRAVE.EXE",
    "WINRAR.EXE", "7Z.EXE",
]

# ===================== 视觉规范 =====================
# 开关色板（扁平化 + 跟随 POFF accent 绿）
SW_ON = "#16A34A"              # 开态轨道（与日志区 OK 状态同色）
SW_ON_HOVER = "#12873E"        # 开态 hover 加深
SW_OFF = "#E3EAF5"             # 关态轨道（低饱和蓝灰，贴合 CARD #FFFFFF + BORDER #DCE3ED）
SW_OFF_HOVER = "#D3DDEC"       # 关态 hover 加深
SW_FRAME = "#C5D0E1"           # 边框（仅关态可见）
SW_THUMB = "#FFFFFF"           # thumb 白色始终不变

BG = "#F2F5FA"
CARD = "#FFFFFF"
SOFT = "#F7F9FC"
BORDER = "#DCE3ED"
TEXT = "#1B2434"
MUTED = "#6B7789"
ACCENT = "#2F6BFF"
ACCENT_DARK = "#1E4FD8"
OK = "#16A34A"
WARN = "#D97706"
ERR = "#DC2626"
HEADER_BG = "#0F172A"
LOG_BG = "#0B1220"


# ===================== 配置与目录 =====================
def get_data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "Poff")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


class FlatToggle(tk.Canvas):
    """扁平化开关：圆角轨道 + 圆形 thumb，跟随 BooleanVar 切换。

    设计：
      - 轨道宽 40 × 高 20（紧凑，不抢标签空间）
      - 开态：OK 绿（#16A34A，与日志区一致）；关态：低饱和蓝灰（#E3EAF5）
      - 边框 1px，仅关态可见；开态被绿色填充覆盖
      - thumb 白色正圆，位置由关态左 → 开态右 ease-out 过渡（90ms / 7 帧）
      - hover 颜色加深 + 光标变手型
    """

    W = 40
    H = 20
    PAD = 2                      # 轨道内 padding，让 thumb 不贴边
    FRAME = 1                    # 边框宽度

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
        self._off_x = self._r                     # thumb 关态中心 x
        self._on_x = self.W - self._r             # thumb 开态中心 x
        self._thumb_y = self._r

        # 圆角轨道（左半圆 + 矩形 + 右半圆）+ 同轮廓边框
        self._body_l = self.create_oval(0, 0, self.H, self.H, outline='', tags='body')
        self._body_m = self.create_rectangle(
            self._r, 0, self.W - self._r, self.H, outline='', tags='body')
        self._body_r = self.create_oval(
            self.W - self.H, 0, self.W, self.H, outline='', tags='body')
        self._frame_l = self.create_oval(
            0, 0, self.H, self.H, outline=SW_FRAME, width=self.FRAME, tags='frame')
        self._frame_m = self.create_rectangle(
            self._r, 0, self.W - self._r, self.H, outline='', width=1, tags='frame')
        self._frame_r = self.create_oval(
            self.W - self.H, 0, self.W, self.H, outline=SW_FRAME, width=self.FRAME, tags='frame')

        # thumb（白色正圆，始终不变）
        self._thumb = self.create_oval(0, 0, 1, 1, fill=SW_THUMB, outline='')

        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

        self._var.trace_add('write', lambda *a: self._render(animate=True))
        self._render(animate=False)

    # ---- 渲染 ----
    def _body_color(self):
        if bool(self._var.get()):
            return SW_ON_HOVER if self._hovering else SW_ON
        return SW_OFF_HOVER if self._hovering else SW_OFF

    def _frame_color(self):
        on = bool(self._var.get())
        # 开态：边框颜色 = 轨道颜色（视觉上被绿色填充盖住）
        if on:
            return self._body_color()
        # 关态：边框始终可见
        return SW_FRAME

    def _render(self, animate):
        fill = self._body_color()
        for iid in (self._body_l, self._body_m, self._body_r):
            self.itemconfigure(iid, fill=fill)
        frame_c = self._frame_color()
        for iid in (self._frame_l, self._frame_r):
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
        step_ms = 13              # 7 × 13 ≈ 90ms
        self._anim_job = None

        def step(i):
            if i >= steps:
                self._place_thumb(target)
                self._anim_job = None
                return
            t = i / steps
            eased = 1 - (1 - t) ** 2   # ease-out 二次方
            self._place_thumb(start + delta * eased)
            self._anim_job = self.after(step_ms, lambda: step(i + 1))

        step(0)

    # ---- 交互 ----
    def _on_click(self, _event=None):
        self._var.set(not bool(self._var.get()))
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    def _on_enter(self, _event):
        self._hovering = True
        self.config(cursor="hand2")
        self._render(animate=False)

    def _on_leave(self, _event):
        self._hovering = False
        self.config(cursor="")
        self._render(animate=False)


PEERS_FILE = os.path.join(get_data_dir(), "peers.json")
CONFIG_FILE = os.path.join(get_data_dir(), "config.json")


def _resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def default_cfg():
    return {
        "modules": {"screenshot": True, "brightness": True, "tablet": True,
                    "roam": False, "blackhole": True},
        "dirs": {},
        "blackhole": {"show": True, "topmost": True, "auto_hide": True,
                      "ignore_drag": False},
        "roam": {"threshold": 30, "layout": {}, "self_role": "main",
                 "target_ip": "", "target_port": TCP_PORT},
        "mobi": {"token": ""},
    }


def load_cfg():
    cfg = default_cfg()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in ("modules", "dirs", "blackhole", "roam", "mobi"):
                if isinstance(data.get(key), dict):
                    cfg[key].update(data[key])
    except Exception:
        pass
    return cfg


def save_cfg_file(cfg):
    tmp = CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError as e:
        print(f"保存配置失败: {e}")


def resolve_dir(cfg, key, rel_folder):
    """优先用户自定义目录，其次用户主目录下的默认位置，最后回退到配置目录。"""
    saved = (cfg.get("dirs") or {}).get(key)
    if saved and st.dir_writable(saved):
        return saved
    candidate = os.path.join(os.path.expanduser("~"), rel_folder)
    if st.dir_writable(candidate):
        return candidate
    fallback = os.path.join(get_data_dir(), os.path.basename(rel_folder) or "files")
    st.ensure_dir(fallback)
    return fallback


def load_saved_peers(local_ip):
    try:
        with open(PEERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    peers = {}
    for item in data.get("peers", []):
        try:
            ip = str(ipaddress.ip_address(item["ip"]))
            kind = item.get("kind", "pc")
            default_port = MOBI_PORT if kind == "mobile" else TCP_PORT
            port = int(item.get("port", default_port))
            if not (1 <= port <= 65535) or ip == local_ip:
                continue
            peers[ip] = {"port": port, "ok": False, "kind": kind}
        except Exception:
            continue
    return peers


def save_peers(peer_list):
    payload = {"app": CODE_APP_TAG, "v": CODE_VERSION,
               "peers": [{"ip": ip, "port": info["port"],
                          "kind": info.get("kind", "pc")}
                         for ip, info in peer_list.items()]}
    tmp = PEERS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PEERS_FILE)
    except OSError as e:
        print(f"保存主机列表失败: {e}")


def _autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --startup'


def is_autostart_enabled():
    if st.winreg is None:
        return False
    try:
        with st.winreg.OpenKey(st.winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH) as key:
            st.winreg.QueryValueEx(key, AUTOSTART_REG_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    if st.winreg is None:
        return False
    try:
        with st.winreg.CreateKeyEx(st.winreg.HKEY_CURRENT_USER, AUTOSTART_REG_PATH,
                                   0, st.winreg.KEY_SET_VALUE) as key:
            if enable:
                st.winreg.SetValueEx(key, AUTOSTART_REG_NAME, 0, st.winreg.REG_SZ,
                                     _autostart_command())
            else:
                try:
                    st.winreg.DeleteValue(key, AUTOSTART_REG_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        print(f"设置开机自启失败: {e}")
        return False


# ===================== 本机能力 =====================
def do_local_shutdown():
    """本机立即关机。不加 /f，避免强杀进程造成数据丢失。"""
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.run(["shutdown", "/s", "/t", "0"], **kwargs)
    except Exception as e:
        print(f"执行关机失败: {e}")


def scan_running_user_apps():
    """扫描本机高风险应用进程（白名单过滤，避免返回上百个系统进程）。"""
    result = []
    if sys.platform != "win32":
        return result
    try:
        proc = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                              capture_output=True, text=True,
                              timeout=PRECHECK_SCAN_TIMEOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW)
        whitelist = {p.lower() for p in USER_APP_PROCESS_NAMES}
        seen = set()
        for line in proc.stdout.splitlines():
            parts = line.strip().split('","')
            if not parts:
                continue
            raw = parts[0].strip('"').strip()
            key = raw.lower()
            if key in whitelist and key not in seen:
                seen.add(key)
                result.append(raw)
    except Exception as e:
        print(f"预检扫描失败: {e}")
    return result


class PoffApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.minsize(980, 640)
        self.root.configure(bg=BG)
        self._center_window()

        self.local_ip = self.get_local_ip()
        self.cfg = load_cfg()
        self.screenshot_dir = resolve_dir(self.cfg, "screenshot",
                                          os.path.join("Pictures", "POFF 截图"))
        self.recording_dir = resolve_dir(self.cfg, "recording",
                                         os.path.join("Videos", "POFF 录屏"))
        self.receive_dir = resolve_dir(self.cfg, "receive",
                                       os.path.join("Downloads", "POFF 接收"))
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

        self._setup_style()
        self._set_window_icon()
        self.root.after(60, self._pump_ui)

        # 模块实例先于界面创建，便于各标签页构建
        self.mobi = MobiDropModule(self)
        self.screenshot = ScreenshotModule(self)
        self.recorder = RecorderModule(self)
        self.blackhole = BlackholeModule(self)
        self.brightness = BrightnessModule(self)
        self.tablet = TabletModule(self)
        self.roam = RoamModule(self)
        self.screenshare = ScreenShareModule(self)

        self._build_ui()
        self.refresh_peer_ui()
        self.start_server()
        if self.mobi.start():
            self.log(f"手机投递服务已启动：http://{self.local_ip}:{MOBI_PORT}"
                     "（手机扫码即用）", "ok")
        self.root.after(3000, self._poll_mobiles)

        self.tray_icon = self._init_tray()
        try:
            if is_autostart_enabled():
                set_autostart(True)
        except Exception:
            pass

        self._apply_module_states()

        if self.cfg.get("blackhole", {}).get("show", True) and \
                self.cfg.get("modules", {}).get("blackhole", True):
            try:
                self.blackhole.ensure_panel()
            except Exception as e:
                print(f"黑洞面板初始化失败: {e}")

        if STARTUP_MODE:
            if self.tray_icon is not None:
                self.root.withdraw()
            else:
                self.root.after(200, self.root.iconify)

        self.root.protocol("WM_DELETE_WINDOW", self.on_main_close)
        self.log(f"POFF V2.2 已启动，正在监听 {TCP_PORT}（主机）/ {MOBI_PORT}（手机投递）端口", "ok")
        threading.Thread(target=self._ensure_firewall_rules, daemon=True).start()

    def _ensure_firewall_rules(self):
        """管理员模式下自动放行 17890（TCP/UDP 主机协议）与 17891（TCP 手机投递）。

        防火墙首次弹窗若被忽略或点错，Windows 会生成 Block 规则，
        导致对端永远"未在线"。以管理员运行时这里先删同名规则再放行，
        幂等可重复执行，让端口连通性不再依赖用户手动点弹窗。
        """
        if not st.is_admin():
            return
        ok = True
        for proto, port in (("TCP", TCP_PORT), ("UDP", TCP_PORT),
                            ("TCP", MOBI_PORT), ("TCP", SHARE_HTTP_PORT)):
            st.run_hidden(["netsh", "advfirewall", "firewall", "delete", "rule",
                           f"name=POFF LAN {proto} {port}"], timeout=15)
            proc = st.run_hidden(
                ["netsh", "advfirewall", "firewall", "add", "rule",
                 f"name=POFF LAN {proto} {port}", "dir=in", "action=allow",
                 f"protocol={proto}", f"localport={port}",
                 "profile=any", "enable=yes"], timeout=15)
            if proc is None or proc.returncode != 0:
                ok = False
        if ok:
            self.ui_call(self.log, "防火墙已自动放行 17890 / 17891 / 17892 端口（管理员模式）", "ok")
        else:
            self.ui_call(self.log, "防火墙自动放行失败，请手动检查防火墙设置", "warn")

    # ---------------- 基础 ----------------
    def ui_call(self, fn, *args):
        """后台线程 → 主线程：放入队列，由主线程的消息泵统一执行。

        直接从子线程调用 tkinter 的 after/控件方法在 mainloop 未运行时
        会抛 RuntimeError，因此所有跨线程回 UI 的操作都走这里。
        """
        self._ui_queue.put((fn, args))

    def _pump_ui(self):
        try:
            while True:
                try:
                    fn, args = self._ui_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn(*args)
                except Exception as e:
                    print(f"UI 回调执行失败: {e}")
        finally:
            try:
                self.root.after(50, self._pump_ui)
            except tk.TclError:
                pass

    def _center_window(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(1180, max(960, sw - 60))
        h = min(860, max(680, sh - 90))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 20)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.minsize(min(980, w), min(640, h))

    def _set_window_icon(self):
        try:
            self.root.iconbitmap(_resource_path("poff.ico"))
        except Exception:
            pass

    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TEXT,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Bg.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Soft.TFrame", background=SOFT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Soft.TLabel", background=SOFT, foreground=TEXT)
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        style.configure("Warn.TLabel", background=CARD, foreground=WARN)
        style.configure("Ok.TLabel", background=CARD, foreground=OK)
        style.configure("Accent.TLabel", background=CARD, foreground=ACCENT,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("H2.TLabel", background=CARD, foreground=TEXT,
                        font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("H3.TLabel", background=CARD, foreground=TEXT,
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Badge.TLabel", background="#E8EFFF", foreground=ACCENT,
                        font=("Microsoft YaHei UI", 8, "bold"), padding=(8, 3))
        style.configure("Card.TButton", padding=(12, 7))
        style.configure("Accent.TButton", background=ACCENT, foreground="#FFFFFF",
                        padding=(14, 9), font=("Microsoft YaHei UI", 9, "bold"),
                        borderwidth=0)
        style.map("Accent.TButton",
                  background=[("pressed", ACCENT_DARK), ("active", ACCENT_DARK),
                              ("disabled", "#A9B6CC")])
        style.configure("Danger.TButton", background=ERR, foreground="#FFFFFF",
                        padding=(12, 8), borderwidth=0)
        style.map("Danger.TButton",
                  background=[("active", "#B91C1C"), ("disabled", "#E5B4B4")])
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
        style.configure("TNotebook.Tab", background="#E6ECF6", foreground=MUTED,
                        padding=(20, 9), borderwidth=0,
                        font=("Microsoft YaHei UI", 9))
        style.map("TNotebook.Tab",
                  background=[("selected", CARD)],
                  foreground=[("selected", ACCENT)])
        style.configure("Peer.Treeview", background=CARD, fieldbackground=CARD,
                        borderwidth=0, rowheight=28,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Peer.Treeview.Heading", background="#F1F5FB",
                        foreground=MUTED, borderwidth=0,
                        font=("Microsoft YaHei UI", 8, "bold"))
        style.map("Peer.Treeview",
                  background=[("selected", "#E3ECFF")],
                  foreground=[("selected", TEXT)])
        style.configure("TSeparator", background=BORDER)
        style.configure("TCheckbutton", background=CARD, foreground=TEXT)
        style.configure("TEntry", fieldbackground="#F8FAFD", bordercolor=BORDER)
        style.configure("TPushbutton", padding=(10, 6))

    # ---------------- 界面骨架 ----------------
    def _build_ui(self):
        self._build_header()

        body = ttk.Frame(self.root, style="Bg.TFrame")
        body.pack(fill="both", expand=True, padx=14, pady=(12, 10))
        body.columnconfigure(0, minsize=336, weight=0)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        body.rowconfigure(1, minsize=186, weight=0)

        left = ttk.Frame(body, style="Card.TFrame", padding=14)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        right = ttk.Frame(body, style="Bg.TFrame")
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_switches(right)
        # 先建左侧设备列表（peer_box），再建右侧 Tab：
        # 屏幕共享等面板构建时会读取 peer_box / 主机列表
        self._build_left(left)
        self._build_tabs(right)
        self._build_log(body)

    def _build_header(self):
        header = tk.Frame(self.root, bg=HEADER_BG, height=66)
        header.pack(fill="x")
        header.pack_propagate(False)

        brand = tk.Frame(header, bg=HEADER_BG)
        brand.pack(side="left", padx=18)
        tk.Label(brand, text=APP_TITLE, bg=HEADER_BG, fg="#FFFFFF",
                 font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        tk.Label(brand, text="局域网多主机控制 · 同步关机 / 截屏 / 投递 / 手机互传",
                 bg=HEADER_BG, fg="#7C8CA8",
                 font=("Microsoft YaHei UI", 8)).pack(anchor="w")

        right = tk.Frame(header, bg=HEADER_BG)
        right.pack(side="right", padx=16)

        self.chip_ip = tk.Label(right, text=f"本机 {self.local_ip}:{TCP_PORT}",
                                bg="#182236", fg="#AFC0DA",
                                font=("Consolas", 9), padx=10, pady=5)
        self.chip_ip.pack(side="left", padx=6)

        self.chip_admin = tk.Label(right, text="", bg="#182236", fg="#AFC0DA",
                                   font=("Microsoft YaHei UI", 8), padx=10,
                                   pady=5, cursor="hand2")
        self.chip_admin.pack(side="left", padx=6)
        self.chip_admin.bind("<Button-1>", lambda e: self._on_admin_chip())
        self._refresh_admin_chip()

        tk.Label(right, text="帮助", bg=HEADER_BG, fg="#94A3B8",
                 font=("Microsoft YaHei UI", 9), cursor="hand2").pack(side="left", padx=12)
        right.winfo_children()[-1].bind("<Button-1>", lambda e: self.show_help())

    def _build_left(self, parent):
        head = ttk.Frame(parent, style="Card.TFrame")
        head.pack(fill="x", side="top")
        ttk.Label(head, text="局域网设备", style="H3.TLabel").pack(side="left")
        self.lbl_peer_count = ttk.Label(head, text="0 台", style="Muted.TLabel")
        self.lbl_peer_count.pack(side="right")

        # 底部固定区先占位：窗口变矮时由设备列表让出空间，避免按钮被裁切
        self.var_autostart = tk.BooleanVar(value=is_autostart_enabled())
        auto = ttk.Frame(parent, style="Card.TFrame")
        auto.pack(side="bottom", fill="x")
        ttk.Separator(auto).pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(auto, text="开机自动启动（登录后在托盘常驻）",
                        variable=self.var_autostart,
                        command=self.toggle_autostart).pack(anchor="w")
        ttk.Label(auto, text="主机列表与开关状态自动保存",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        add = ttk.Frame(parent, style="Card.TFrame")
        add.pack(side="bottom", fill="x", pady=(10, 0))
        ttk.Separator(add).pack(fill="x", pady=(0, 10))
        ttk.Label(add, text="添加主机", style="H3.TLabel").pack(anchor="w")
        ttk.Label(add, text="粘贴对方的串码，或直接输入 IP:端口",
                  style="Muted.TLabel", wraplength=290).pack(anchor="w", pady=(2, 6))

        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(add, textvariable=self.code_var)
        self.code_entry.pack(fill="x")
        self.code_entry.bind("<Return>", lambda e: self.add_peer())

        row = ttk.Frame(add, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="生成本机串码", command=self.gen_code).pack(
            side="left", expand=True, fill="x")
        ttk.Button(row, text="复制串码", command=self.copy_code).pack(
            side="left", expand=True, fill="x", padx=(6, 0))
        ttk.Button(add, text="添加主机", style="Accent.TButton",
                   command=self.add_peer).pack(fill="x", pady=(8, 0))

        tree_box = ttk.Frame(parent, style="Card.TFrame")
        self.peer_box = ttk.Treeview(tree_box, columns=("ip", "state"), show="headings",
                                     height=6, style="Peer.Treeview",
                                     selectmode="browse")
        self.peer_box.heading("ip", text="主机地址")
        self.peer_box.heading("state", text="状态")
        self.peer_box.column("ip", width=190, anchor="w")
        self.peer_box.column("state", width=90, anchor="center")
        self.peer_box.tag_configure("online", foreground=OK)
        self.peer_box.tag_configure("offline", foreground="#8A94A6")
        self.peer_box.tag_configure("unknown", foreground=WARN)
        self.peer_box.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_box, orient="vertical", command=self.peer_box.yview)
        sb.pack(side="right", fill="y")
        self.peer_box.configure(yscrollcommand=sb.set)
        self.peer_box.bind("<<TreeviewSelect>>", self.on_select_change)
        self.peer_box.bind("<Double-1>", lambda e: self.check_selected())

        btns = ttk.Frame(parent, style="Card.TFrame")
        btns.pack(fill="x", side="bottom", pady=(0, 10))
        ttk.Button(btns, text="全部校验", command=self.check_all).pack(
            side="left", expand=True, fill="x")
        ttk.Button(btns, text="清空列表", command=self.clear_peers).pack(
            side="left", expand=True, fill="x", padx=(8, 0))

        # 设备列表最后 pack 并 expand，窗口变矮时只压缩列表，不压缩按钮
        tree_box.pack(fill="both", expand=True, pady=(10, 10))

    def _build_switches(self, parent):
        card = ttk.Frame(parent, style="Card.TFrame", padding=(14, 12))
        card.grid(row=0, column=0, sticky="ew", pady=(0, 10))

        head = ttk.Frame(card, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="功能总开关", style="H3.TLabel").pack(side="left")
        ttk.Label(head, text="关闭后对应模块的入口与热键一并停用",
                  style="Muted.TLabel").pack(side="left", padx=10)

        grid = ttk.Frame(card, style="Card.TFrame")
        grid.pack(fill="x", pady=(10, 0))
        mods = self.cfg.get("modules", {})
        labels = [
            # row=0：原 5 个 + 新增 1 个（6 个）
            ("screenshot", "屏幕截屏"), ("recorder", "录屏录音"),
            ("brightness", "亮度控制"), ("tablet", "平板副屏"),
            ("roam", "键鼠漫游"), ("screenshare", "屏幕共享"),
            # row=1：黑洞投递（单独一行，因为上面 6 个排满一行会挤）
            ("blackhole", "黑洞投递"),
        ]
        # 两列布局：第一行 6 个（row=0），第二行 1 个（row=1），每列 padx=18
        COLS_PER_ROW = 6
        for i, (key, label) in enumerate(labels):
            row = 0 if i < COLS_PER_ROW else 1
            col = i if row == 0 else 0
            var = tk.BooleanVar(value=bool(mods.get(key, True)))
            self.mod_vars[key] = var
            cell = ttk.Frame(grid, style="Card.TFrame")
            cell.grid(row=row, column=col, sticky="nw", padx=(0, 18),
                      pady=(0, 8) if row == 1 else 0)
            FlatToggle(cell, variable=var,
                       on_change=lambda k=key: self.on_module_toggle(k)).grid(row=0, column=0, sticky="w")
            ttk.Label(cell, text=label, style="Muted.TLabel",
                      font=("PingFang SC", 8)).grid(row=1, column=0, sticky="w", pady=(6, 0))

        ttk.Label(card,
                  text="全局热键：Alt+Q 选区截屏　Ctrl+Shift+F 全屏截屏　"
                       "Ctrl+Shift+H 黑洞面板　Ctrl+Shift+L 漫游锁定（漫游开启后生效）",
                  style="Muted.TLabel", wraplength=700).pack(anchor="w", pady=(4, 0))

    def _build_tabs(self, parent):
        self.nb = ttk.Notebook(parent)
        self.nb.grid(row=1, column=0, sticky="nsew")
        self._tab_scroll = {}

        def make_tab(text, builder):
            """每个标签页内容放在可滚动容器里，窗口较矮时也不会裁切控件。"""
            outer = ttk.Frame(self.nb, style="Card.TFrame")
            canvas = tk.Canvas(outer, bg=CARD, highlightthickness=0, bd=0)
            vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            inner = ttk.Frame(canvas, style="Card.TFrame")
            win = canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.configure(yscrollcommand=vsb.set)
            canvas.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            inner.bind("<Configure>",
                       lambda e, c=canvas: c.configure(scrollregion=c.bbox("all")))
            canvas.bind("<Configure>",
                        lambda e, c=canvas, w=win: c.itemconfigure(w, width=e.width))
            self._tab_scroll[str(outer)] = (canvas, inner)
            self.nb.add(outer, text=text)
            builder(inner)
            return outer

        self.tab_device = make_tab("设备操作", self._build_device_tab)
        self.tab_shot = make_tab("截屏", self.screenshot.build_tab)
        # 录屏 / 录音 追加到截屏 Tab 后面（同一个 Tab 内，不新起一页）
        self.tab_rec = make_tab("录屏录音", self.recorder.build_panel)
        self.tab_hole = make_tab("黑洞投递", self.blackhole.build_tab)
        self.tab_bright = make_tab("亮度", self.brightness.build_tab)
        self.tab_tablet = make_tab("平板副屏", self.tablet.build_tab)
        self.tab_roam = make_tab("键鼠漫游", self.roam.build_tab)
        # 屏幕共享 追加到键鼠漫游 Tab 后面
        self.tab_share = make_tab("屏幕共享", self.screenshare.build_panel)
        self.root.bind_all("<MouseWheel>", self._on_tab_wheel)

    def _on_tab_wheel(self, event):
        """仅当指针位于当前标签页内容上且内容超出可视区时才滚动。"""
        pair = self._tab_scroll.get(str(self.nb.nametowidget(self.nb.select())))
        if not pair:
            return
        canvas, inner = pair
        if inner.winfo_reqheight() <= canvas.winfo_height():
            return
        widget = canvas.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget is inner or widget is canvas:
                break
            widget = getattr(widget, "master", None)
        if widget is None:
            return
        canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def _build_device_tab(self, parent):
        box = ttk.Frame(parent, style="Card.TFrame", padding=16)
        box.pack(fill="both", expand=True)

        ttk.Label(box, text="设备操作", style="H2.TLabel").pack(anchor="w")

        sel = ttk.Frame(box, style="Soft.TFrame", padding=12)
        sel.pack(fill="x", pady=(10, 0))
        ttk.Label(sel, text="当前选中", style="Soft.TLabel").grid(
            row=0, column=0, sticky="w")
        self.lbl_sel_ip = ttk.Label(sel, text="未选中主机", style="Accent.TLabel")
        self.lbl_sel_ip.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.lbl_sel_state = ttk.Label(sel, text="请在左侧列表选择一台主机",
                                       style="Soft.TLabel")
        self.lbl_sel_state.grid(row=1, column=1, sticky="e", padx=10)
        sel.columnconfigure(1, weight=1)

        row = ttk.Frame(sel, style="Soft.TFrame")
        row.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.btn_check = ttk.Button(row, text="连通校验", command=self.check_selected)
        self.btn_check.pack(side="left")
        self.btn_single = ttk.Button(row, text="单独关机（预检）", style="Danger.TButton",
                                     command=self.single_shutdown, state="disabled")
        self.btn_single.pack(side="left", padx=8)
        ttk.Button(row, text="截屏发给它", command=self.shot_for_selected).pack(
            side="left")

        par = ttk.Frame(box, style="Soft.TFrame", padding=12)
        par.pack(fill="x", pady=12)
        ttk.Label(par, text="并行同步关机", style="H3.TLabel").pack(anchor="w")
        ttk.Label(par,
                  text="关机前会先扫描各主机正在运行的高风险应用（Office / 浏览器 / 编辑器等），"
                       "汇总确认后再进入 10 秒倒计时，避免对方弹出保存对话框时无人处理。",
                  style="Soft.TLabel", wraplength=640, justify="left").pack(
            anchor="w", pady=(4, 10))
        self.btn_start = ttk.Button(par, text="启动 10 秒并行同步关机",
                                    style="Accent.TButton", command=self.start_sync,
                                    state="disabled")
        self.btn_start.pack(anchor="w")

        tip = ttk.Frame(box, style="Card.TFrame")
        tip.pack(fill="x", pady=(4, 0))
        ttk.Label(tip, text="其他操作", style="H3.TLabel").pack(anchor="w")
        ttk.Label(tip,
                  text="截屏发送、文件投递、亮度调节分别在对应标签页；"
                       "黑洞面板把文件拖到主机条目即可投递。",
                  style="Muted.TLabel", wraplength=640).pack(anchor="w", pady=(4, 0))

    def _build_log(self, body):
        card = ttk.Frame(body, style="Card.TFrame", padding=(12, 8))
        card.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))

        head = ttk.Frame(card, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="运行日志", style="H3.TLabel").pack(side="left")
        ttk.Button(head, text="复制", command=self.copy_log).pack(side="right")
        ttk.Button(head, text="清空", command=self.clear_log).pack(side="right", padx=6)

        wrap = tk.Frame(card, bg=LOG_BG)
        wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.log_text = tk.Text(wrap, height=7, bg=LOG_BG, fg="#C7D2E3",
                                insertbackground="#FFFFFF", relief="flat",
                                font=("Consolas", 9), padx=10, pady=6,
                                state="disabled", wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.tag_configure("time", foreground="#5B6B85")
        self.log_text.tag_configure("info", foreground="#C7D2E3")
        self.log_text.tag_configure("ok", foreground="#4ADE80")
        self.log_text.tag_configure("warn", foreground="#FBBF24")
        self.log_text.tag_configure("err", foreground="#F87171")

    # ---------------- 日志 ----------------
    def log(self, msg, level="info"):
        self.ui_call(self._append_log, msg, level)

    def _append_log(self, msg, level):
        try:
            if not hasattr(self, "log_text"):
                return
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            self.log_text.config(state="normal")
            self.log_text.insert("end", f"[{ts}] ", "time")
            self.log_text.insert("end", f"{msg}\n", level if level in
                                 ("info", "ok", "warn", "err") else "info")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        except tk.TclError:
            pass

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def copy_log(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.log_text.get("1.0", "end").strip())
            self.log("日志已复制到剪贴板", "ok")
        except tk.TclError:
            pass

    # ---------------- 主机管理 ----------------
    def get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "127.0.0.1"

    def peer_items(self):
        with self.peer_lock:
            return list(self.peer_list.items())

    def selected_ip(self):
        peer_box = getattr(self, "peer_box", None)
        if peer_box is None:
            return None
        sel = peer_box.selection()
        if not sel:
            return None
        try:
            return peer_box.item(sel[0], "values")[0]
        except Exception:
            return None

    def on_select_change(self, _event=None):
        ip = self.selected_ip()
        if not ip:
            self.lbl_sel_ip.config(text="未选中主机")
            self.lbl_sel_state.config(text="请在左侧列表选择一台主机")
            self.btn_single.config(state="disabled")
            return
        info = self.peer_list.get(ip, {})
        if info.get("kind") == "mobile":
            self.lbl_sel_ip.config(text=f"📱 {ip}")
            self.lbl_sel_state.config(
                text=("手机在线" if info.get("ok") else "手机待连接（打开投递页后在线）"))
            self.btn_single.config(state="disabled")
            return
        self.lbl_sel_ip.config(text=ip)
        self.lbl_sel_state.config(
            text=("已确认在线" if info.get("ok") else "未校验连通状态"))
        self.btn_single.config(state="normal")

    def refresh_peer_ui(self):
        prev = self.selected_ip()
        for item in self.peer_box.get_children():
            self.peer_box.delete(item)
        for ip, info in sorted(self.peer_list.items()):
            if info.get("kind") == "mobile":
                tag, state = (("online", "📱 手机·在线") if info.get("ok")
                              else ("unknown", "📱 手机·待连接"))
            elif info.get("ok"):
                tag, state = "online", "● 在线"
            else:
                tag, state = "unknown", "○ 未校验"
            self.peer_box.insert("", "end", values=(ip, state), tags=(tag,))
        self.lbl_peer_count.config(text=f"{len(self.peer_list)} 台")
        if prev and prev in self.peer_list:
            for item in self.peer_box.get_children():
                if self.peer_box.item(item, "values")[0] == prev:
                    self.peer_box.selection_set(item)
                    break
        self.btn_start.config(
            state=("normal" if self.peer_list else "disabled"))
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
        raw = f"{CODE_APP_TAG}|{CODE_VERSION}|{self.local_ip}|{TCP_PORT}"
        code = base64.b32encode(raw.encode()).decode().rstrip("=")
        self.code_var.set(code)
        self.log(f"已生成本机串码：{code}", "info")

    def copy_code(self):
        code = self.code_var.get().strip()
        if not code:
            self.gen_code()
            code = self.code_var.get().strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        self.log("串码已复制到剪贴板，可直接发给对方", "ok")

    def parse_code(self, text):
        text = text.strip().upper().replace(" ", "")
        if not text:
            raise ValueError("请输入串码")
        pad = "=" * (-len(text) % 8)
        raw = base64.b32decode(text + pad).decode("utf-8")
        parts = raw.split("|")
        if len(parts) != 4 or parts[0] != CODE_APP_TAG:
            raise ValueError("串码格式不正确")
        ip = str(ipaddress.ip_address(parts[2]))
        port = int(parts[3])
        if not (1 <= port <= 65535):
            raise ValueError("串码端口非法")
        return ip, port

    def add_peer(self):
        text = self.code_var.get().strip().replace("：", ":")
        try:
            if text and (":" in text and "|" not in text) and len(text) < 40:
                host, _, port_s = text.partition(":")
                ip, port = str(ipaddress.ip_address(host)), int(port_s)
                if not (1 <= port <= 65535):
                    raise ValueError("端口超出范围")
            else:
                ip, port = self.parse_code(text)
        except Exception as e:
            messagebox.showerror("添加失败", f"串码无法识别：{e}")
            return

        if ip == self.local_ip:
            messagebox.showwarning("提示", "这是本机地址，不需要添加")
            return
        with self.peer_lock:
            if ip in self.peer_list:
                self.peer_list[ip]["port"] = port
                added = False
            else:
                self.peer_list[ip] = {"port": port, "ok": False}
                added = True
        self.refresh_peer_ui()
        self.save_peer_list()
        self.code_var.set("")
        self.log(f"{'已添加' if added else '已更新'}主机 {ip}:{port}", "ok")

    def add_mobile_peer(self, ip_text):
        """黑洞面板「添加手机设备」入口。返回 (ok, 错误信息)。"""
        text = str(ip_text or "").strip()
        try:
            ip = str(ipaddress.ip_address(text))
        except ValueError:
            return False, "IP 格式不正确，例如 192.168.101.66"
        if ip == self.local_ip:
            return False, "这是本机地址，不需要添加"
        with self.peer_lock:
            exists = ip in self.peer_list
            self.peer_list[ip] = {"port": MOBI_PORT, "ok": False, "kind": "mobile"}
        self.refresh_peer_ui()
        self.save_peer_list()
        self.log(f"{'已更新' if exists else '已添加'}手机设备 {ip}，"
                 "手机打开投递页后自动标记在线", "ok")
        return True, ""

    def _poll_mobiles(self):
        """每 30 秒探测手机设备：投递页最近活跃 或 ICMP 可达即视为在线。"""
        try:
            with self.peer_lock:
                mobs = [(ip, dict(info)) for ip, info in self.peer_list.items()
                        if info.get("kind") == "mobile"]
            if mobs:
                def task():
                    results = {ip: self._probe_peer(ip, info) for ip, info in mobs}

                    def apply():
                        with self.peer_lock:
                            for ip, ok in results.items():
                                if ip in self.peer_list:
                                    self.peer_list[ip]["ok"] = ok
                        self.refresh_peer_ui()
                    self.ui_call(apply)
                threading.Thread(target=task, daemon=True).start()
        finally:
            try:
                self.root.after(30000, self._poll_mobiles)
            except tk.TclError:
                pass

    def clear_peers(self):
        if not self.peer_list:
            return
        if not messagebox.askyesno("确认", "清空全部主机列表？"):
            return
        with self.peer_lock:
            self.peer_list.clear()
        self.save_peer_list()
        self.refresh_peer_ui()
        self.log("已清空主机列表", "warn")

    # ---------------- 连通校验 ----------------
    def tcp_hello(self, host, port):
        try:
            with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as s:
                s.settimeout(CONNECT_TIMEOUT)
                s.sendall(f"{MSG_HELLO}\n".encode("utf-8"))
                data = s.recv(256).decode("utf-8", errors="ignore")
                return MSG_HELLO_ACK in data
        except Exception:
            return False

    def _icmp_ping(self, host, timeout_ms=2000):
        try:
            proc = subprocess.run(
                ["ping", "-n", "1", "-w", str(timeout_ms), host],
                capture_output=True, text=True, timeout=timeout_ms / 1000 + 2,
                creationflags=subprocess.CREATE_NO_WINDOW)
            return proc.returncode == 0
        except Exception:
            return False

    def _probe_peer(self, ip, info):
        """按设备类型选择探测方式：PC 用 POFF HELLO，手机用投递页活跃 / ICMP。"""
        if info.get("kind") == "mobile":
            if self.mobi.is_recent(ip):
                return True
            return self._icmp_ping(ip)
        return self.tcp_hello(ip, info["port"])

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
        self.log("正在并发校验连通状态...", "info")

    def _after_check(self, results):
        with self.peer_lock:
            for ip, ok in results.items():
                if ip in self.peer_list:
                    self.peer_list[ip]["ok"] = ok
        self.refresh_peer_ui()
        online = sum(1 for v in results.values() if v)
        self.log(f"校验完成：{online}/{len(results)} 台在线", "ok")

    def check_selected(self):
        ip = self.selected_ip()
        if not ip:
            return
        info = self.peer_list.get(ip)
        if not info:
            return
        self.log(f"正在校验 {ip} ...", "info")

        def task():
            ok = self._probe_peer(ip, info)

            def done():
                if ip in self.peer_list:
                    self.peer_list[ip]["ok"] = ok
                self.refresh_peer_ui()
                self.log(f"{ip} {'在线' if ok else '无法连接'}", "ok" if ok else "err")
            self.ui_call(done)

        threading.Thread(target=task, daemon=True).start()

    # ---------------- 预检 ----------------
    def tcp_precheck_one(self, host, port):
        result = {"ip": host, "ok": False, "apps": [], "error": None}
        try:
            with socket.create_connection((host, port), timeout=PRECHECK_RECV_TIMEOUT) as sock:
                sock.settimeout(PRECHECK_RECV_TIMEOUT)
                sock.sendall(f"{MSG_PRECHECK}\n".encode("utf-8"))
                chunks = []
                while True:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                text = b"".join(chunks).decode("utf-8", errors="ignore")
                json_line = None
                for ln in text.splitlines():
                    ln = ln.strip()
                    if ln.startswith("{") and ln.endswith("}"):
                        json_line = ln
                        break
                if json_line is None:
                    result["error"] = "对方未返回预检结果（可能是旧版本）"
                    return result
                payload = json.loads(json_line)
                result["ok"] = True
                result["apps"] = list(payload.get("apps", []))
        except socket.timeout:
            result["error"] = "连接超时"
        except (ConnectionRefusedError, OSError) as e:
            result["error"] = f"无法连接：{e}"
        except json.JSONDecodeError as e:
            result["error"] = f"响应解析失败：{e}"
        return result

    def tcp_precheck_all(self):
        with self.peer_lock:
            # 手机设备不参与关机，预检仅覆盖 PC 主机
            targets = [(ip, info) for ip, info in self.peer_list.items()
                       if info.get("kind") != "mobile"]
        results = {}
        threads = []

        def worker(ip, port):
            results[ip] = self.tcp_precheck_one(ip, port)

        for ip, info in targets:
            t = threading.Thread(target=worker, args=(ip, info["port"]), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        return results

    # ---------------- 单机操作 ----------------
    def shot_for_selected(self):
        ip = self.selected_ip()
        if not ip:
            messagebox.showwarning("提示", "请先在左侧列表选中一台主机")
            return
        if not self.mod_vars["screenshot"].get():
            messagebox.showwarning("提示", "截屏模块未开启")
            return
        self.nb.select(self.tab_shot)
        self.screenshot.capture_full()

    def single_shutdown(self):
        sel = self.peer_box.selection()
        if not sel:
            return
        target_ip = self.peer_box.item(sel[0], "values")[0]
        info = self.peer_list.get(target_ip)
        if not info:
            return
        self.btn_single.config(state="disabled", text="正在预检...")

        def task():
            pre = self.tcp_precheck_one(target_ip, info["port"])
            self.ui_call(self._show_single_precheck, target_ip, info["port"], pre)

        threading.Thread(target=task, daemon=True).start()

    def _show_single_precheck(self, target_ip, port, pre):
        self.btn_single.config(state="normal", text="单独关机（预检）")
        win = tk.Toplevel(self.root)
        win.title(f"关机预检 · {target_ip}")
        win.geometry("640x340")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.transient(self.root)
        win.grab_set()

        card = ttk.Frame(win, style="Card.TFrame", padding=14)
        card.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(card, text=f"目标主机 {target_ip}", style="H3.TLabel").pack(anchor="w")
        ttk.Label(card,
                  text="单独关机指令发出后立即执行、没有倒计时。以下是对方当前运行的高风险应用：",
                  style="Muted.TLabel", wraplength=580).pack(anchor="w", pady=(2, 8))

        text = tk.Text(card, height=8, wrap="word", relief="flat", bg="#F8FAFD",
                       font=("Microsoft YaHei UI", 9), padx=8, pady=6)
        text.pack(fill="both", expand=True)
        if not pre["ok"]:
            text.insert("end", f"注意：无法预检（{pre['error']}）\n\n"
                               "对方可能离线或版本过旧，仍可尝试发送关机指令。")
        elif pre["apps"]:
            text.insert("end", "注意：以下应用正在运行，关机时可能弹出保存对话框：\n\n")
            for app in pre["apps"]:
                text.insert("end", f"   · {app}\n")
        else:
            text.insert("end", "未检测到高风险应用，可以安全关机。")
        text.config(state="disabled")

        decided = {"value": False}

        def go():
            decided["value"] = True
            win.destroy()

        def cancel():
            decided["value"] = False
            win.destroy()

        bar = ttk.Frame(card, style="Card.TFrame")
        bar.pack(fill="x", pady=(10, 0))
        ttk.Label(bar, text="确认立即关机？", style="Warn.TLabel").pack(side="left")
        ttk.Button(bar, text="继续关机", style="Danger.TButton", command=go).pack(
            side="right")
        ttk.Button(bar, text="取消", command=cancel).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.wait_window()

        if not decided["value"]:
            return

        def fire():
            ok = self.send_msg(target_ip, port, MSG_SINGLE_SHUTDOWN)
            self.ui_call(self.log,
                         f"关机指令已发送至 {target_ip}" if ok else f"无法连接 {target_ip}",
                         "ok" if ok else "err")

        threading.Thread(target=fire, daemon=True).start()

    # ---------------- 并行关机（预检 → 倒计时） ----------------
    def start_sync(self):
        if self.countdown_win is not None:
            return
        with self.peer_lock:
            has_pc = any(info.get("kind") != "mobile"
                         for info in self.peer_list.values())
        if not has_pc:
            messagebox.showinfo("提示", "当前列表没有可关机的电脑主机"
                                "（手机设备不参与关机）。")
            return
        if not self.peer_list:
            return
        self.btn_start.config(state="disabled", text="正在预检...")

        def task():
            results = self.tcp_precheck_all()
            self.ui_call(self._show_precheck_dialog, results)

        threading.Thread(target=task, daemon=True).start()

    def _show_precheck_dialog(self, results):
        self.btn_start.config(state="normal", text="启动 10 秒并行同步关机")
        win = tk.Toplevel(self.root)
        win.title("关机前安全预检")
        win.geometry("760x460")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.transient(self.root)
        win.grab_set()

        card = ttk.Frame(win, style="Card.TFrame", padding=14)
        card.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(card, text="安全预检结果", style="H2.TLabel").pack(anchor="w")
        ttk.Label(card,
                  text="关机时 Windows 会向各应用发送关闭请求，未保存的文档会弹出对话框等待处理。"
                       "建议先让远端的同事保存，再确认关机。",
                  style="Muted.TLabel", wraplength=690, justify="left").pack(
            anchor="w", pady=(2, 10))

        cols = ("ip", "status", "apps")
        tree = ttk.Treeview(card, columns=cols, show="headings", height=9)
        tree.heading("ip", text="主机")
        tree.heading("status", text="状态")
        tree.heading("apps", text="检测到的应用")
        tree.column("ip", width=150, anchor="w")
        tree.column("status", width=110, anchor="center")
        tree.column("apps", width=420, anchor="w")
        tree.tag_configure("err", foreground=ERR)
        tree.tag_configure("warn", foreground=WARN)
        tree.tag_configure("ok", foreground=OK)
        tree.pack(fill="both", expand=True)

        attention = 0
        for ip in sorted(results):
            r = results[ip]
            if not r["ok"]:
                tree.insert("", "end", values=(ip, "离线/旧版", r["error"] or "无法连接"),
                            tags=("err",))
                attention += 1
            elif r["apps"]:
                tree.insert("", "end", values=(ip, "有应用在跑", ", ".join(r["apps"])),
                            tags=("warn",))
                attention += 1
            else:
                tree.insert("", "end", values=(ip, "安全", "—"), tags=("ok",))

        summary = f"共 {len(results)} 台主机，其中 {attention} 台需要关注"
        ttk.Label(card, text=summary, style=("Warn.TLabel" if attention else "Ok.TLabel"),
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=8)

        decided = {"value": False}

        def go():
            decided["value"] = True
            win.destroy()

        def cancel():
            decided["value"] = False
            win.destroy()

        bar = ttk.Frame(card, style="Card.TFrame")
        bar.pack(fill="x")
        ttk.Button(bar, text="继续关机（风险自负）", style="Accent.TButton",
                   command=go).pack(side="right")
        ttk.Button(bar, text="取消关机", command=cancel).pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.wait_window()

        if not decided["value"]:
            self.log("已取消并行关机", "warn")
            return

        self.send_msg_to_all_async(MSG_START_COUNTDOWN)
        self.open_countdown_window(is_initiator=True)
        self.log("已广播同步关机指令，各主机进入 10 秒倒计时", "warn")

    # ---------------- 倒计时 ----------------
    def open_countdown_window(self, is_initiator):
        if self.countdown_win is not None:
            self.log("已存在进行中的倒计时，忽略重复请求", "warn")
            return
        self.countdown_is_initiator = bool(is_initiator)
        self.countdown_cancelled = False
        self.countdown_left = 10

        win = tk.Toplevel(self.root)
        self.countdown_win = win
        win.title("即将关机")
        win.geometry("420x300")
        win.configure(bg="#FFFFFF")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.transient(self.root)

        tk.Label(win, text="同步关机倒计时", bg="#FFFFFF", fg=TEXT,
                 font=("Microsoft YaHei UI", 12, "bold")).pack(pady=(24, 4))
        self.lbl_count = tk.Label(win, text="10", bg="#FFFFFF", fg=ERR,
                                  font=("Microsoft YaHei UI", 56, "bold"))
        self.lbl_count.pack()
        tk.Label(win, text="倒计时结束后本机与已同步主机将关机\n点击下方按钮可取消本次关机",
                 bg="#FFFFFF", fg=MUTED, font=("Microsoft YaHei UI", 9),
                 justify="center").pack()
        ttk.Button(win, text="取消关机", style="Accent.TButton",
                   command=self.cancel_countdown).pack(pady=12)

        win.protocol("WM_DELETE_WINDOW", self.cancel_countdown)
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
            return
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
        self.log("倒计时结束，开始关机", "warn")
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
            self.log("已取消本次关机", "warn")

    def _close_countdown_window(self):
        win = self.countdown_win
        self.countdown_win = None
        self.countdown_cancelled = True
        try:
            if win is not None:
                win.destroy()
        except tk.TclError:
            pass

    # ---------------- 网络 ----------------
    def start_server(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        try:
            if exclusive is not None:
                sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            sock.bind(("0.0.0.0", TCP_PORT))
            sock.listen(16)
        except OSError as e:
            sock.close()
            self.log(f"监听 {TCP_PORT} 失败：{e}", "err")
            messagebox.showerror(
                "端口被占用",
                f"无法监听 {TCP_PORT} 端口，可能已有一个 POFF 正在运行。\n"
                "请关闭重复实例后重启本程序。")
            return
        self.server_socket = sock
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while self.server_socket is not None:
            try:
                conn, addr = self.server_socket.accept()
            except OSError:
                break
            threading.Thread(target=self.handle_conn, args=(conn,), daemon=True).start()

    def handle_conn(self, conn):
        """单连接单指令；FILE_PUSH 走长度头部 + 原始字节流。"""
        remote_ip = conn.getpeername()[0]
        conn.settimeout(120)
        f = conn.makefile("rb")
        try:
            line = f.readline(4096)
            if not line:
                return
            cmd = line.decode("utf-8", errors="ignore").strip()

            if cmd == MSG_HELLO:
                try:
                    conn.sendall(f"{MSG_HELLO_ACK}\n".encode("utf-8"))
                except OSError:
                    pass
                return

            if cmd == MSG_PRECHECK:
                apps = scan_running_user_apps()
                payload = {"app": CODE_APP_TAG, "v": CODE_VERSION,
                           "ip": self.local_ip, "apps": apps}
                blob = f"{MSG_PRECHECK}\n{json.dumps(payload, ensure_ascii=False)}\n"
                try:
                    conn.sendall(blob.encode("utf-8"))
                except OSError:
                    pass
                return

            if cmd == transfer.MSG_FILE_PUSH:
                self._receive_file(f, conn, remote_ip)
                return

            if cmd == "KVM_HELLO":
                # V2.1 漫游被控端会话：同步处理直到对端结束（长连接）
                handle_kvm_session(conn, f, self)
                return

            if cmd in (MSG_START_COUNTDOWN, MSG_CANCEL, MSG_SHUTDOWN,
                       MSG_SINGLE_SHUTDOWN):
                self.ui_call(self.dispatch_msg, cmd)
        except Exception as e:
            print(f"处理连接失败: {e}")
        finally:
            try:
                f.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _receive_file(self, f, conn, remote_ip):
        header_line = f.readline(65536)
        if not header_line:
            return
        try:
            header = json.loads(header_line.decode("utf-8", errors="ignore"))
        except Exception:
            self._file_reply(conn, False, "文件头解析失败")
            return

        name = st.sanitize_filename(header.get("name"), "received_file")
        try:
            size = int(header.get("size", 0))
        except Exception:
            size = -1
        if size <= 0 or size > transfer.MAX_FILE_SIZE:
            self._file_reply(conn, False, "文件大小非法")
            return

        kind = "screenshot" if header.get("kind") == "screenshot" else "file"
        target_dir = self.screenshot_dir if kind == "screenshot" else self.receive_dir
        st.ensure_dir(target_dir)
        tmp_path = os.path.join(target_dir, f".{os.getpid()}_{int(time.time() * 1000)}.part")

        # 同名文件不覆盖：先落盘再改名
        digest = hashlib.sha256()
        remaining = size
        try:
            with open(tmp_path, "wb") as out:
                while remaining > 0:
                    block = f.read(min(transfer.CHUNK, remaining))
                    if not block:
                        raise OSError("连接提前断开")
                    out.write(block)
                    digest.update(block)
                    remaining -= len(block)
        except OSError as e:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            self._file_reply(conn, False, f"接收失败：{e}")
            return

        if header.get("sha256") and digest.hexdigest() != header["sha256"]:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            self._file_reply(conn, False, "校验不一致，已丢弃")
            return

        final_path = st.unique_path(os.path.join(target_dir, name))
        try:
            os.replace(tmp_path, final_path)
        except OSError as e:
            self._file_reply(conn, False, f"写入失败：{e}")
            return

        self._file_reply(conn, True, "接收成功", final_path)
        self.ui_call(self.on_file_received, final_path, kind, remote_ip)

    def _file_reply(self, conn, ok, message, path=None):
        tag = transfer.MSG_FILE_OK if ok else transfer.MSG_FILE_ERR
        payload = {"path": path or "", "error": "" if ok else message}
        try:
            conn.sendall((tag + "\n" + json.dumps(payload, ensure_ascii=False) + "\n")
                         .encode("utf-8"))
        except OSError:
            pass

    def on_file_received(self, path, kind, remote_ip):
        size = st.human_size(os.path.getsize(path)) if os.path.exists(path) else "-"
        if kind == "screenshot":
            self.log(f"收到来自 {remote_ip} 的截图：{os.path.basename(path)}（{size}）", "ok")
            self.screenshot.receive_preview(path, remote_ip)
            return

        self.log(f"收到来自 {remote_ip} 的文件：{os.path.basename(path)}（{size}）", "ok")
        win = tk.Toplevel(self.root)
        win.title("收到文件")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.geometry("520x180")
        card = ttk.Frame(win, style="Card.TFrame", padding=14)
        card.pack(fill="both", expand=True, padx=12, pady=12)
        ttk.Label(card, text=f"来自 {remote_ip} 的文件", style="H3.TLabel").pack(anchor="w")
        ttk.Label(card, text=f"{os.path.basename(path)}　{size}",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 4))
        ttk.Label(card, text=path, style="Muted.TLabel",
                  wraplength=470).pack(anchor="w")
        bar = ttk.Frame(card, style="Card.TFrame")
        bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="打开文件夹",
                   command=lambda: self.open_path(os.path.dirname(path))).pack(side="left")
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")

    def dispatch_msg(self, msg):
        if msg == MSG_START_COUNTDOWN:
            if self.countdown_win is not None:
                self.log("收到重复的关机指令，已在倒计时中，忽略", "warn")
                return
            self.log("收到远端同步关机指令，进入倒计时", "warn")
            self.open_countdown_window(is_initiator=False)
        elif msg == MSG_CANCEL:
            if self.countdown_win is not None:
                self.cancel_countdown(broadcast=False, silent=True)
                self.log("远端已取消关机，本地倒计时终止", "warn")
        elif msg == MSG_SHUTDOWN:
            self.log("收到远端关机指令，立即关机", "warn")
            do_local_shutdown()
        elif msg == MSG_SINGLE_SHUTDOWN:
            self.log("收到远端单独关机指令，立即关机", "warn")
            do_local_shutdown()

    def send_msg(self, ip, port, msg):
        try:
            with socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT) as s:
                s.sendall((msg + "\n").encode("utf-8"))
            return True
        except Exception:
            return False

    def send_msg_to_all_async(self, msg):
        with self.peer_lock:
            targets = [(ip, info) for ip, info in self.peer_list.items()
                       if info.get("kind") != "mobile"]

        def task():
            for ip, info in targets:
                self.send_msg(ip, info["port"], msg)
        threading.Thread(target=task, daemon=True).start()

    # ---------------- 文件发送（供各模块调用） ----------------
    def send_files_async(self, paths, ip, kind="file", on_done=None, on_progress=None):
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            if on_done:
                on_done(False, "没有可发送的文件")
            return
        info = self.peer_list.get(ip)
        # 手机设备：复制到待接收区，手机打开投递页点按下载
        if info and info.get("kind") == "mobile":
            return self.mobi.push_files_async(paths, on_done=on_done)
        port = info["port"] if info else TCP_PORT

        def task():
            ok_all = True
            last_err = ""
            for idx, path in enumerate(paths, start=1):
                def progress(sent, total, p=path):
                    if on_progress:
                        self.ui_call(on_progress, p, sent, total)
                ok, msg = transfer.send_file(ip, port, path, kind=kind, progress=progress)
                if ok:
                    self.log(f"文件已送达 {ip}：{os.path.basename(path)}"
                             f"（{idx}/{len(paths)}）", "ok")
                else:
                    ok_all = False
                    last_err = msg
                    self.log(f"发送到 {ip} 失败：{os.path.basename(path)} - {msg}", "err")

            if on_done:
                summary = (f"{len(paths)} 个文件已发送" if ok_all else last_err)
                self.ui_call(on_done, ok_all, summary)

        threading.Thread(target=task, daemon=True).start()

    def open_path(self, path):
        if not st.open_path(path):
            self.log(f"无法打开路径：{path}", "err")

    # ---------------- 模块开关 / 热键 ----------------
    def on_module_toggle(self, key):
        self.cfg.setdefault("modules", {})[key] = bool(self.mod_vars[key].get())
        self.save_cfg()
        self._apply_module_states()
        if key == "blackhole":
            on = self.mod_vars[key].get()
            self.blackhole.set_enabled(on)
            if on and self.cfg.get("blackhole", {}).get("show", True):
                self.blackhole.ensure_panel()
        elif key == "recorder":
            self.recorder.set_enabled(self.mod_vars[key].get())
        elif key == "screenshare":
            self.screenshare.set_enabled(self.mod_vars[key].get())
        self.log(f"{'已开启' if self.mod_vars[key].get() else '已关闭'}模块：{key}", "info")

    def _apply_module_states(self):
        shot_on = self.mod_vars["screenshot"].get()
        self.screenshot.set_enabled(shot_on)
        self.recorder.set_enabled(self.mod_vars.get("recorder", True))
        self.brightness.set_enabled(self.mod_vars["brightness"].get())
        self.tablet.set_enabled(self.mod_vars["tablet"].get())
        self.roam.set_enabled(self.mod_vars["roam"].get())
        self.blackhole.set_enabled(self.mod_vars["blackhole"].get())
        self.screenshare.set_enabled(self.mod_vars.get("screenshare", True))
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
        if self.mod_vars.get("screenshot") and self.mod_vars["screenshot"].get():
            bindings["shot_full"] = (st.MOD_CONTROL | st.MOD_SHIFT, 0x46)   # F
            bindings["shot_area"] = (st.MOD_ALT, 0x51)                       # Alt+Q
            bindings["shot_area2"] = (st.MOD_CONTROL | st.MOD_SHIFT, 0x53)   # S（旧习惯保留）
        if self.mod_vars.get("blackhole") and self.mod_vars["blackhole"].get():
            bindings["blackhole"] = (st.MOD_CONTROL | st.MOD_SHIFT, 0x48)   # H
        if self.mod_vars.get("roam") and self.mod_vars["roam"].get():
            bindings["roam_lock"] = (st.MOD_CONTROL | st.MOD_SHIFT, 0x4C)   # L

        try:
            self.hotkey_mgr = st.HotkeyManager(bindings, self._on_hotkey)
            self.hotkey_mgr.start()
        except Exception as e:
            self.log(f"全局热键注册失败：{e}", "warn")
            return

        def report():
            try:
                if self.hotkey_mgr and self.hotkey_mgr.failures:
                    names = "、".join(self.hotkey_mgr.failures)
                    self.log(f"以下热键被其他程序占用：{names}", "warn")
            except Exception:
                pass
        self.root.after(600, report)

    def _on_hotkey(self, name):
        self.ui_call(self._handle_hotkey, name)

    def _handle_hotkey(self, name):
        if name == "shot_full":
            if self.mod_vars["screenshot"].get():
                self.screenshot.capture_full()
        elif name in ("shot_area", "shot_area2"):
            if self.mod_vars["screenshot"].get():
                self.screenshot.capture_region()
        elif name == "blackhole":
            self.blackhole.toggle_panel()
        elif name == "roam_lock":
            self.roam_locked = not self.roam_locked
            self.roam.update_lock_label()
            self.log("漫游已锁定" if self.roam_locked else "漫游已解锁", "warn")

    # ---------------- 权限 / 帮助 ----------------
    def _refresh_admin_chip(self):
        if st.is_admin():
            self.chip_admin.config(text="● 管理员权限", fg="#4ADE80")
        else:
            self.chip_admin.config(text="普通权限 · 点击提权", fg="#FBBF24")

    def _on_admin_chip(self):
        if st.is_admin():
            messagebox.showinfo("权限状态", "当前已具备管理员权限。")
            return
        if not messagebox.askyesno(
                "以管理员身份重启",
                "重启后本程序将获得管理员权限，可启停 Spacedesk 服务。\n"
                "注意：管理员实例不会随开机自启静默启动。是否继续？"):
            return
        if st.relaunch_as_admin():
            self.log("正在以管理员身份重启...", "info")
            self.root.after(400, self.quit_app)
        else:
            messagebox.showerror("提权失败", "未能启动管理员实例（UAC 被取消或系统限制）。")

    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("使用说明")
        win.configure(bg=BG)
        win.geometry("700x520")
        text = tk.Text(win, wrap="word", relief="flat", bg=CARD, fg=TEXT,
                       font=("Microsoft YaHei UI", 9), padx=16, pady=14)
        text.pack(fill="both", expand=True, padx=12, pady=12)
        content = (
            "POFF V2.2 使用说明\n\n"
            "如遇问题或需要定制功能，欢迎联系开发者微信：shot2021\n\n"
            "一、配对\n"
            "  两台电脑都运行 POFF，任一侧点击「生成本机串码」并复制给对方；\n"
            "  对方粘贴后点「添加主机」即可。列表会自动保存，下次无需重复添加。\n"
            "  若路由器更换了 IP，串码会失效，建议在路由器上为这两台机器绑定 IP。\n\n"
            "二、关机\n"
            "  单独关机：先扫描对方运行的高风险应用，确认后立即关机，无倒计时。\n"
            "  并行关机：先预检全部主机，再进入 10 秒倒计时；倒计时内可取消。\n"
            "  关机命令使用系统优雅关机（不带 /f），不会强杀进程，但对方若有未保存文档\n"
            "  仍会弹出保存对话框，需要有人在那台机器上处理。手机设备不参与关机。\n\n"
            "三、截屏\n"
            "  Alt+Q 选区截屏（Ctrl+Shift+S 同样可用），Ctrl+Shift+F 全屏截屏。\n"
            "  截完立即自动保存到截图目录，随后打开编辑窗口：\n"
            "  画笔 / 矩形 / 箭头 / 马赛克，可撤销（Ctrl+Z），可复制（Ctrl+C）；\n"
            "  标注直接写回同一张已保存的图片，也可另存或发送到其他主机。\n"
            "  Alt+Q 是全局热键，关闭截屏模块即可释放该组合键。\n\n"
            "四、黑洞投递\n"
            "  鼠标移到屏幕右侧细条展开悬浮面板，把文件拖到主机条目即可投递；\n"
            "  对方文件保存在接收目录，并弹窗提示。\n\n"
            "五、手机投递（V2.2，双向、零安装）\n"
            "  在「黑洞投递」页查看二维码，手机浏览器扫码（或输入网址）打开投递页；\n"
            "  电脑投递到手机的文件先进入待接收区，手机在页面点按即可下载，\n"
            "  下载完成后自动清理；手机选文件上传，电脑接收目录弹窗提示。\n"
            "  「添加手机设备」可把手机加入设备目录与悬浮面板；配对码可重新生成，\n"
            "  旧码立即失效。手机无法被远程关机，关机指令会自动跳过。\n\n"
            "六、亮度 / 平板副屏\n"
            "  亮度支持笔记本内置屏（WMI）与外接显示器（DDC/CI，需在显示器菜单开启）；\n"
            "  平板亮度由平板系统自行控制。平板副屏通过调度 Spacedesk 服务实现，\n"
            "  启停服务需要管理员权限。\n\n"
            "七、跨屏键鼠漫游（双端双向）\n"
            "  两台主机都打开漫游开关，各自选择「本机角色」与「漫游目标」，并把布局调成\n"
            "  互为镜像（例如 A 看笔记本在右，则 B 看主机在左）。把鼠标推到本机屏幕边缘\n"
            "  并停留片刻即进入漫游，键鼠转发到对方；把光标移回该边缘内侧即返回本机。\n"
            "  漫游中 Ctrl+Shift+L 可紧急退回本机；建议以管理员权限运行获得完整体验。\n\n"
            "八、权限与自启\n"
            "  默认普通权限启动，避免开机自启被 UAC 卡住；需要管理员时点击右上角提权。\n"
            "  首次运行如弹出防火墙提示，请允许「专用网络」，否则收不到局域网指令。\n"
        )
        text.insert("end", content)
        text.config(state="disabled")

    # ---------------- 托盘 / 自启 / 退出 ----------------
    def toggle_autostart(self):
        want = self.var_autostart.get()
        if not set_autostart(want):
            self.var_autostart.set(not want)
            messagebox.showerror("设置失败", "无法修改开机自启项，请检查系统权限。")
            return
        self.log("已开启开机自启" if want else "已关闭开机自启", "ok")

    def _init_tray(self):
        try:
            import pystray
            from PIL import Image
        except Exception:
            return None
        try:
            image = Image.open(_resource_path("poff.ico"))
            menu = pystray.Menu(
                pystray.MenuItem("显示主界面",
                                 lambda *_: self.ui_call(self.show_window),
                                 default=True),
                pystray.MenuItem("黑洞面板",
                                 lambda *_: self.ui_call(self.blackhole.show)),
                pystray.MenuItem("退出 POFF", lambda *_: self.ui_call(self.quit_app)),
            )
            icon = pystray.Icon("poff", image, APP_TITLE, menu)
            threading.Thread(target=self._tray_runner, args=(icon,), daemon=True).start()
            return icon
        except Exception as e:
            print(f"托盘初始化失败: {e}")
            return None

    def _tray_runner(self, icon):
        try:
            icon.run()
        except Exception as e:
            print(f"托盘运行失败: {e}")
            self.ui_call(self._on_tray_failed)

    def _on_tray_failed(self):
        self.tray_icon = None
        self.show_window()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        # 窗口重新显示时恢复黑洞面板（关闭主窗口时曾临时销毁）
        if self.mod_vars.get("blackhole") and self.mod_vars["blackhole"].get() and \
                self.cfg.get("blackhole", {}).get("show", True):
            try:
                self.blackhole.ensure_panel()
            except Exception:
                pass
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def hide_to_tray(self):
        self.root.withdraw()
        if not self._tray_hinted:
            self._tray_hinted = True
            try:
                self.tray_icon.notify("POFF 仍在后台接收指令，右键托盘图标可退出", APP_TITLE)
            except Exception:
                pass

    def on_main_close(self):
        if self.countdown_win is not None:
            if not messagebox.askyesno(
                    "确认", "倒计时进行中，关闭主窗口会取消本次关机。\n是否继续？",
                    icon="warning"):
                return
            self.countdown_cancelled = True
            self.send_msg_to_all_async(MSG_CANCEL)
            self._close_countdown_window()

        try:
            self.blackhole.hide_panel()
        except Exception:
            pass

        if self.tray_icon is not None:
            self.hide_to_tray()
        else:
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
    # 声明 DPI 感知：截屏坐标与窗口坐标一致，避免高缩放屏错位
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
    app = PoffApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
