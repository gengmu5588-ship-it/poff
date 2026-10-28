# -*- coding: utf-8 -*-
"""POFF V2.0 系统能力封装：提权、全局热键、笔记本亮度、Spacedesk 服务、目录与格式化工具。

所有能力都做了失败降级：调用方拿到 (ok, err) 后自行提示，不会因为某个系统接口不可用而崩溃。
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import threading

try:
    import winreg
except ImportError:
    winreg = None

CREATE_NO_WINDOW = 0x08000000

# 热键修饰符
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


def run_hidden(args, timeout=8):
    """静默执行命令行，返回 CompletedProcess；失败返回 None。"""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              errors="ignore", creationflags=CREATE_NO_WINDOW)
    except Exception:
        return None


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """以管理员身份重新拉起自己（UAC 确认）。成功返回 True，调用方随后退出当前实例。"""
    try:
        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe = sys.executable
            script = os.path.abspath(sys.argv[0])
            params = f'"{script}"'
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return rc > 32
    except Exception:
        return False


def open_path(path):
    """用资源管理器打开文件或目录。"""
    try:
        os.startfile(path)
        return True
    except Exception:
        return False


def human_size(num):
    try:
        num = float(num)
    except Exception:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return "-"


def unique_path(path):
    """目标文件已存在时追加 (1)(2)...，避免覆盖。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


def sanitize_filename(name, fallback="received_file"):
    """去掉路径分隔符与非法字符，只保留文件名。"""
    name = os.path.basename(str(name or "")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    return name or fallback


def ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except OSError:
        return False


def dir_writable(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".poff_write_test")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def monitor_count():
    """当前显示器数量（含外接），失败按 1 处理。"""
    try:
        return max(1, int(ctypes.windll.user32.GetSystemMetrics(80)))
    except Exception:
        return 1


class HotkeyManager(threading.Thread):
    """全局热键：RegisterHotKey + 独立线程消息循环。

    bindings: {"名称": (修饰符, 虚拟键码)}，回调在热键线程触发，
    调用方必须用 root.after 转交 UI 线程。
    """

    def __init__(self, bindings, callback):
        super().__init__(daemon=True)
        self.bindings = dict(bindings)
        self.callback = callback
        self.failures = []
        self._ids = {i + 1: name for i, name in enumerate(self.bindings)}
        self._thread_id = None
        self._running = False

    def run(self):
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        self._running = True
        for hid, name in self._ids.items():
            mods, vk = self.bindings[name]
            if not user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                self.failures.append(name)
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                name = self._ids.get(int(msg.wParam))
                if name:
                    try:
                        self.callback(name)
                    except Exception:
                        pass
        for hid in self._ids:
            try:
                user32.UnregisterHotKey(None, hid)
            except Exception:
                pass
        self._running = False

    def stop(self, timeout=1.5):
        """请求热键线程退出，并等待其注销热键后再返回。

        RegisterHotKey 是系统级占用，若旧线程尚未注销就重新注册，
        新热键会注册失败；因此这里必须等待线程结束（通常只需几毫秒）。
        """
        try:
            if self._thread_id:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
        if threading.current_thread() is not self and self.is_alive():
            self.join(timeout)


# ===================== 笔记本内屏亮度（WMI） =====================
_VK_NONE = 0


def get_brightness():
    """读取本机内屏亮度。返回 (value, err)；不支持的设备 value 为 None。"""
    cmd = ("$b = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness "
           "-ErrorAction Stop; $b | Select-Object -First 1 -ExpandProperty CurrentBrightness")
    proc = run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                       "Bypass", "-Command", cmd], timeout=12)
    if proc is None:
        return None, "调用系统接口超时"
    if proc.returncode != 0:
        return None, "当前设备不支持系统亮度调节"
    m = re.search(r"\d+", proc.stdout or "")
    if not m:
        return None, "当前设备不支持系统亮度调节"
    return max(0, min(100, int(m.group()))), None


def set_brightness(value):
    """设置本机内屏亮度（0-100）。"""
    value = max(0, min(100, int(value)))
    cmd = ("$m = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods "
           "-ErrorAction Stop; "
           f"$null = $m | Invoke-CimMethod -MethodName WmiSetBrightness "
           f"-Arguments @{{Timeout=1;Brightness={value}}}; exit 0")
    proc = run_hidden(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                       "Bypass", "-Command", cmd], timeout=12)
    if proc is None:
        return False, "调用系统接口超时"
    if proc.returncode != 0:
        return False, "当前设备不支持系统亮度调节"
    return True, None


# ===================== Spacedesk 服务调度 =====================
_SPACEDESK_CANDIDATES = ("spacedeskService", "spacedeskservice", "spacedesk")


def _sc_query(name):
    proc = run_hidden(["sc", "query", name], timeout=6)
    if proc is None or proc.returncode != 0:
        return None
    out = (proc.stdout or "").upper()
    if "RUNNING" in out:
        state = "running"
    elif "STOPPED" in out:
        state = "stopped"
    else:
        state = "unknown"
    return {"name": name, "state": state}


def find_spacedesk_service():
    """返回 {"name","state"}；未安装返回 None。"""
    for name in _SPACEDESK_CANDIDATES:
        info = _sc_query(name)
        if info:
            return info
    return None


def control_spacedesk(action):
    """action: start / stop。返回 (ok, message)。需要管理员权限。"""
    info = find_spacedesk_service()
    if not info:
        return False, "未检测到 Spacedesk 服务，请先在电脑上安装 Spacedesk 驱动"
    if not is_admin():
        return False, "需要管理员权限，请先点击「以管理员身份重启」"
    proc = run_hidden(["sc", action, info["name"]], timeout=20)
    if proc is None:
        return False, "服务操作超时"
    if proc.returncode != 0:
        return False, f"服务操作失败（错误码 {proc.returncode}），请检查 Spacedesk 安装状态"
    return True, "Spacedesk 服务已" + ("启动" if action == "start" else "停止")


# ===================== 外接显示器亮度（DDC/CI，V2.1） =====================
_VCP_BRIGHTNESS = 0x10


class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", wt.HANDLE),
                ("szPhysicalMonitorDescription", wt.WCHAR * 128)]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)


def _for_each_physical_monitor(fn):
    """枚举全部物理显示器，对每个调用 fn(handle, desc)，随后销毁句柄。

    fn 返回非 None 的值会被收集进结果列表。任何一步失败都跳过该显示器，
    保证句柄不泄漏、调用方不崩溃。
    """
    try:
        user32 = ctypes.windll.user32
        dxva2 = ctypes.windll.dxva2
    except Exception:
        return []
    hmons = []

    def _cb(hmon, hdc, lprect, lparam):
        hmons.append(hmon)
        return 1

    cb = _MONITORENUMPROC(_cb)
    try:
        if not user32.EnumDisplayMonitors(None, None, cb, 0):
            return []
    except Exception:
        return []

    out = []
    for hmon in hmons:
        count = wt.DWORD(0)
        arr = None
        try:
            if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                    hmon, ctypes.byref(count)):
                continue
            n = count.value
            if n <= 0 or n > 32:
                continue
            arr = (_PHYSICAL_MONITOR * n)()
            if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, n, arr):
                arr = None
                continue
            for i in range(n):
                try:
                    val = fn(arr[i].hPhysicalMonitor,
                             arr[i].szPhysicalMonitorDescription)
                    if val is not None:
                        out.append(val)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if arr is not None:
                try:
                    dxva2.DestroyPhysicalMonitors(count.value, arr)
                except Exception:
                    pass
    return out


def list_ddc_monitors():
    """枚举支持 DDC/CI 的外接显示器亮度。

    返回 [{"index": 枚举序号（含被跳过的）, "name": 描述, "value": 0-100}]。
    读取失败或不支持的显示器会被跳过，但 index 保持与完整枚举一致，
    以便 set_ddc_brightness 按同一序号定位。
    """
    state = {"i": 0}

    def probe(h, desc):
        idx = state["i"]
        state["i"] += 1
        cur = wt.DWORD(0)
        mx = wt.DWORD(0)
        try:
            ok = ctypes.windll.dxva2.GetVCPFeatureAndVCPFeatureReply(
                h, _VCP_BRIGHTNESS, None, ctypes.byref(cur), ctypes.byref(mx))
        except Exception:
            return None
        if not ok or mx.value < 1:
            return None
        value = int(round(cur.value * 100.0 / mx.value)) if mx.value != 100 \
            else int(cur.value)
        name = (desc or "").strip() or f"显示器 {idx + 1}"
        return {"index": idx, "name": name,
                "value": max(0, min(100, value))}

    try:
        return _for_each_physical_monitor(probe)
    except Exception:
        return []


def set_ddc_brightness(index, value):
    """按枚举序号设置外接显示器亮度（0-100）。返回 (ok, err)。"""
    value = max(0, min(100, int(value)))
    state = {"i": 0, "done": False, "ok": False}

    def apply(h, desc):
        if state["done"]:
            return None
        idx = state["i"]
        state["i"] += 1
        if idx != index:
            return None
        state["done"] = True
        try:
            state["ok"] = bool(ctypes.windll.dxva2.SetVCPFeature(
                h, _VCP_BRIGHTNESS, wt.DWORD(value)))
        except Exception:
            state["ok"] = False
        return None

    try:
        _for_each_physical_monitor(apply)
    except Exception as e:
        return False, f"枚举显示器失败：{e}"
    if not state["done"]:
        return False, "未找到指定显示器"
    if not state["ok"]:
        return False, "显示器拒绝了亮度设置（可能未开启 DDC/CI）"
    return True, None


# ===================== 全局键鼠钩子（V2.1 漫游转发） =====================
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13

_MOUSE_KINDS = {
    0x200: "move",
    0x201: "ldown", 0x202: "lup",
    0x204: "rdown", 0x205: "rup",
    0x207: "mdown", 0x208: "mup",
    0x20A: "wheel",
}

_HOOKPROC = ctypes.WINFUNCTYPE(wt.LPARAM, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class InputHookThread(threading.Thread):
    """WH_MOUSE_LL / WH_KEYBOARD_LL 全局钩子，独立线程消息循环。

    on_mouse(dict) / on_key(dict) 返回 True 表示吞掉该事件（不传给系统）。
    回调必须快：仅允许判断状态与入队，禁止阻塞 IO，否则会拖慢全系统输入。
    mouse dict: {"kind","x","y","wheel"}；key dict: {"vk","scan","up","extended"}。
    """

    def __init__(self, on_mouse=None, on_key=None):
        super().__init__(daemon=True)
        self.on_mouse = on_mouse
        self.on_key = on_key
        self.error = None
        self._thread_id = None
        self._h_mouse = None
        self._h_key = None
        self._mouse_proc = None      # 保持引用，防 GC
        self._key_proc = None
        self._hook_errors = 0        # 回调异常计数（熔断保险丝）
        self._auto_stopped = False   # 异常过多时自动停钩子，恢复系统输入

    def run(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        call_next = user32.CallNextHookEx
        # 关键：必须显式声明参数类型。64 位系统上 lparam 是指向钩子结构体的
        # 64 位指针，ctypes 默认按 32 位 int 传参会抛 ArgumentError，导致
        # 回调异常返回 0，系统视为"事件被吞"——全系统键鼠瞬间卡死。
        call_next.argtypes = (wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        call_next.restype = wt.LPARAM

        def _safe_next(ncode, wparam, lparam):
            try:
                return call_next(None, ncode, wparam, lparam)
            except Exception:
                self._hook_errors += 1
                return 0

        def _fuse_blown():
            # 异常熔断：连续异常说明钩子层不可用，立即停用恢复系统输入，
            # 避免输入被无限吞掉只能强制关机。
            if self._hook_errors >= 100 and not self._auto_stopped:
                self._auto_stopped = True
                try:
                    self.stop(timeout=0)  # 回调就在钩子线程，同线程安全
                except Exception:
                    pass

        def mouse_proc(ncode, wparam, lparam):
            if ncode == 0 and self.on_mouse is not None:
                try:
                    s = ctypes.cast(lparam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                    kind = _MOUSE_KINDS.get(wparam)
                    if kind is not None:
                        ev = {"kind": kind, "x": s.pt.x, "y": s.pt.y, "wheel": 0}
                        if kind == "wheel":
                            # HIWORD 是有符号的滚轮增量
                            ev["wheel"] = ctypes.c_short(
                                (s.mouseData >> 16) & 0xFFFF).value
                        if self.on_mouse(ev):
                            return 1
                except Exception:
                    self._hook_errors += 1
                    _fuse_blown()
            return _safe_next(ncode, wparam, lparam)

        def key_proc(ncode, wparam, lparam):
            if ncode == 0 and self.on_key is not None:
                try:
                    s = ctypes.cast(lparam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                    ev = {"vk": s.vkCode, "scan": s.scanCode,
                          "up": bool(s.flags & 0x80),
                          "extended": bool(s.flags & 0x01)}
                    if self.on_key(ev):
                        return 1
                except Exception:
                    self._hook_errors += 1
                    _fuse_blown()
            return _safe_next(ncode, wparam, lparam)

        self._mouse_proc = _HOOKPROC(mouse_proc)
        self._key_proc = _HOOKPROC(key_proc)
        try:
            try:
                self._h_mouse = user32.SetWindowsHookExW(
                    WH_MOUSE_LL, self._mouse_proc, None, 0)
                self._h_key = user32.SetWindowsHookExW(
                    WH_KEYBOARD_LL, self._key_proc, None, 0)
            except Exception as e:
                self.error = f"钩子注册异常：{e}"
                return
            if not self._h_mouse and not self._h_key:
                self.error = "钩子注册失败（可能被安全软件拦截）"
                return

            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            # 钩子必须随线程退出注销；若泄漏到已死线程，全系统输入会被卡死
            for h in (self._h_mouse, self._h_key):
                if h:
                    try:
                        user32.UnhookWindowsHookEx(h)
                    except Exception:
                        pass
            self._h_mouse = self._h_key = None

    def stop(self, timeout=2.0):
        try:
            if self._thread_id:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass
        if threading.current_thread() is not self and self.is_alive():
            self.join(timeout)


# ===================== SendInput 注入（V2.1 被控端） =====================
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1

_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_MOUSEEVENTF_ABSOLUTE = 0x8000

_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008

_BUTTON_FLAGS = {
    "l": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "r": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "m": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUT_UNION)]


def _send_input(inp):
    arr = (_INPUT * 1)(inp)
    try:
        return ctypes.windll.user32.SendInput(1, arr, ctypes.sizeof(_INPUT)) == 1
    except Exception:
        return False


def inject_mouse_abs(nx, ny):
    """绝对坐标移动（0-65535 映射主屏）。"""
    nx = max(0, min(65535, int(nx)))
    ny = max(0, min(65535, int(ny)))
    inp = _INPUT(type=_INPUT_MOUSE)
    inp.u.mi = _MOUSEINPUT(nx, ny, 0,
                           _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE, 0, 0)
    return _send_input(inp)


def inject_mouse_button(btn, down):
    """btn: "l"/"r"/"m"；down: True 按下 / False 抬起。"""
    flags = _BUTTON_FLAGS.get(btn)
    if not flags:
        return False
    inp = _INPUT(type=_INPUT_MOUSE)
    inp.u.mi = _MOUSEINPUT(0, 0, 0, flags[0] if down else flags[1], 0, 0)
    return _send_input(inp)


def inject_wheel(delta):
    inp = _INPUT(type=_INPUT_MOUSE)
    inp.u.mi = _MOUSEINPUT(0, 0, wt.DWORD(int(delta) & 0xFFFFFFFF),
                           _MOUSEEVENTF_WHEEL, 0, 0)
    return _send_input(inp)


def inject_key(scan, up, extended):
    """按扫描码注入按键，避免键盘布局差异。"""
    flags = _KEYEVENTF_SCANCODE
    if extended:
        flags |= _KEYEVENTF_EXTENDEDKEY
    if up:
        flags |= _KEYEVENTF_KEYUP
    inp = _INPUT(type=_INPUT_KEYBOARD)
    inp.u.ki = _KEYBDINPUT(0, max(0, min(0xFFFF, int(scan))), flags, 0, 0)
    return _send_input(inp)
