# -*- coding: utf-8 -*-
"""POFF macOS 系统能力封装。

Mac 版原则：
- 文件/网络/目录工具保持跨平台行为；
- 全局热键使用 pynput；
- KVM 全局输入监听/吞键与注入使用 Quartz（需要“辅助功能/输入监控”权限）；
- macOS 没有 Spacedesk，相关接口明确返回不支持；
- macOS 没有稳定公开的百分比亮度 API，本版不伪造支持。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time

winreg = None

# 兼容主程序原有热键常量
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

IS_MAC = sys.platform == "darwin"

try:
    if IS_MAC:
        import Quartz
    else:
        Quartz = None
except Exception:
    Quartz = None


def run_hidden(args, timeout=8):
    """静默执行命令行，返回 CompletedProcess；失败返回 None。"""
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, errors="ignore")
    except Exception:
        return None


def is_admin():
    """macOS 不使用 Windows UAC 语义；返回当前是否为 root 仅供兼容。"""
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def relaunch_as_admin():
    """Mac 版不通过整进程提权；需要权限的能力交由系统隐私设置授权。"""
    return False


def open_path(path):
    """用 Finder / 默认应用打开文件、目录或系统设置 URL。"""
    try:
        if isinstance(path, str) and path.startswith("x-apple.systempreferences:"):
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["open", os.fspath(path)])
        return True
    except Exception:
        return False


def open_privacy_accessibility():
    return open_path(
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")


def open_privacy_screen_recording():
    return open_path(
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")


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
    name = os.path.basename(str(name or "")).strip()
    # 同时过滤 Windows 非法字符，便于跨平台互传
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


def _require_quartz():
    return Quartz is not None


def screen_size():
    """返回主显示器像素逻辑尺寸 (w, h)。"""
    if Quartz is not None:
        try:
            did = Quartz.CGMainDisplayID()
            b = Quartz.CGDisplayBounds(did)
            return int(b.size.width), int(b.size.height)
        except Exception:
            pass
    return 1920, 1080


def virtual_screen_bounds():
    """返回活动显示器并集 (x, y, w, h)，失败退回主屏。"""
    if Quartz is not None:
        try:
            max_displays = 32
            err, ids, count = Quartz.CGGetActiveDisplayList(max_displays, None, None)
            if err == Quartz.kCGErrorSuccess and count:
                rects = [Quartz.CGDisplayBounds(d) for d in ids[:count]]
                x0 = min(r.origin.x for r in rects)
                y0 = min(r.origin.y for r in rects)
                x1 = max(r.origin.x + r.size.width for r in rects)
                y1 = max(r.origin.y + r.size.height for r in rects)
                return int(x0), int(y0), int(x1 - x0), int(y1 - y0)
        except Exception:
            pass
    w, h = screen_size()
    return 0, 0, w, h


def monitor_count():
    if Quartz is not None:
        try:
            err, _ids, count = Quartz.CGGetActiveDisplayList(32, None, None)
            if err == Quartz.kCGErrorSuccess:
                return max(1, int(count))
        except Exception:
            pass
    return 1


def get_cursor_pos():
    if Quartz is not None:
        try:
            ev = Quartz.CGEventCreate(None)
            p = Quartz.CGEventGetLocation(ev)
            return int(p.x), int(p.y)
        except Exception:
            pass
    return 0, 0


def set_cursor_pos(x, y):
    if Quartz is None:
        return False
    try:
        Quartz.CGWarpMouseCursorPosition((float(x), float(y)))
        return True
    except Exception:
        return False


def mouse_button_down(button="left"):
    if Quartz is None:
        return False
    try:
        b = Quartz.kCGMouseButtonLeft if button == "left" else Quartz.kCGMouseButtonRight
        return bool(Quartz.CGEventSourceButtonState(
            Quartz.kCGEventSourceStateCombinedSessionState, b))
    except Exception:
        return False


def is_ctrl_shift_down():
    if Quartz is None:
        return False
    try:
        src = Quartz.kCGEventSourceStateCombinedSessionState
        ctrl = Quartz.CGEventSourceKeyState(src, 59) or Quartz.CGEventSourceKeyState(src, 62)
        shift = Quartz.CGEventSourceKeyState(src, 56) or Quartz.CGEventSourceKeyState(src, 60)
        return bool(ctrl and shift)
    except Exception:
        return False


# ---------------- 全局热键 ----------------
class HotkeyManager(threading.Thread):
    """macOS 全局热键监听。

    bindings 保持原格式：{"name": (MOD_*, VK)}。当前 POFF 只使用 Ctrl+Shift+字母。
    """
    def __init__(self, bindings, callback):
        super().__init__(daemon=True)
        self.bindings = dict(bindings)
        self.callback = callback
        self.failures = []
        self._listener = None
        self._pressed = set()
        self._fired = set()
        self._stop_evt = threading.Event()

    @staticmethod
    def _vk_char(vk):
        if 0x30 <= int(vk) <= 0x5A:
            return chr(int(vk)).lower()
        return None

    def run(self):
        try:
            from pynput import keyboard
        except Exception:
            self.failures = list(self.bindings.keys())
            return

        def norm(key):
            if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                return "ctrl"
            if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
                return "shift"
            if key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r,
                       keyboard.Key.alt_gr):
                return "alt"
            if key in (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r):
                return "cmd"
            try:
                ch = key.char
                return ch.lower() if ch else None
            except Exception:
                return None

        def on_press(key):
            token = norm(key)
            if token:
                self._pressed.add(token)
            for name, (mods, vk) in self.bindings.items():
                ch = self._vk_char(vk)
                if not ch:
                    continue
                need_ctrl = bool(mods & MOD_CONTROL)
                need_shift = bool(mods & MOD_SHIFT)
                need_alt = bool(mods & MOD_ALT)
                need_cmd = bool(mods & MOD_WIN)
                ok = ((not need_ctrl or "ctrl" in self._pressed) and
                      (not need_shift or "shift" in self._pressed) and
                      (not need_alt or "alt" in self._pressed) and
                      (not need_cmd or "cmd" in self._pressed) and
                      ch in self._pressed)
                if ok and name not in self._fired:
                    self._fired.add(name)
                    try:
                        self.callback(name)
                    except Exception:
                        pass

        def on_release(key):
            token = norm(key)
            if token:
                self._pressed.discard(token)
            # 任意释放后允许下次触发，避免按住重复触发
            self._fired.clear()
            if self._stop_evt.is_set():
                return False

        try:
            self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self._listener.start()
            while not self._stop_evt.wait(0.2):
                pass
        except Exception:
            self.failures = list(self.bindings.keys())
        finally:
            try:
                if self._listener is not None:
                    self._listener.stop()
            except Exception:
                pass

    def stop(self, timeout=1.5):
        self._stop_evt.set()
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass
        if threading.current_thread() is not self and self.is_alive():
            self.join(timeout)


# ---------------- 亮度 / 平板副屏 ----------------
def get_brightness():
    return None, "macOS 未提供稳定公开的百分比亮度接口；本版暂不支持内屏亮度调节"


def set_brightness(value):
    return False, "macOS 版暂不支持直接设置内屏亮度"


def list_ddc_monitors():
    return []


def set_ddc_brightness(index, value):
    return False, "macOS 版暂不内置 DDC/CI 控制"


def find_spacedesk_service():
    return None


def control_spacedesk(action):
    return False, "Spacedesk Driver 仅支持 Windows；macOS 请使用系统 Sidecar/隔空播放等方案"


# ---------------- 键盘扫描码兼容层 ----------------
# KVM 网络协议继续使用 Windows Set-1 扫描码，这样 Mac -> 旧 Windows POFF 仍能工作。
# 接收端 Mac 再映射回 Apple 虚拟键码。
_PC_TO_MAC = {
    0x01: 53,
    0x02: 18, 0x03: 19, 0x04: 20, 0x05: 21, 0x06: 23,
    0x07: 22, 0x08: 26, 0x09: 28, 0x0A: 25, 0x0B: 29,
    0x0C: 27, 0x0D: 24, 0x0E: 51, 0x0F: 48,
    0x10: 12, 0x11: 13, 0x12: 14, 0x13: 15, 0x14: 17,
    0x15: 16, 0x16: 32, 0x17: 34, 0x18: 31, 0x19: 35,
    0x1A: 33, 0x1B: 30, 0x1C: 36, 0x1D: 59,
    0x1E: 0, 0x1F: 1, 0x20: 2, 0x21: 3, 0x22: 5,
    0x23: 4, 0x24: 38, 0x25: 40, 0x26: 37, 0x27: 41,
    0x28: 39, 0x29: 50, 0x2A: 56, 0x2B: 42,
    0x2C: 6, 0x2D: 7, 0x2E: 8, 0x2F: 9, 0x30: 11,
    0x31: 45, 0x32: 46, 0x33: 43, 0x34: 47, 0x35: 44,
    0x36: 60, 0x37: 67, 0x38: 58, 0x39: 49, 0x3A: 57,
    0x3B: 122, 0x3C: 120, 0x3D: 99, 0x3E: 118, 0x3F: 96,
    0x40: 97, 0x41: 98, 0x42: 100, 0x43: 101, 0x44: 109,
    0x57: 103, 0x58: 111,
}
_EXT_PC_TO_MAC = {
    0x1D: 62, 0x38: 61,
    0x47: 115, 0x48: 126, 0x49: 116,
    0x4B: 123, 0x4D: 124,
    0x4F: 119, 0x50: 125, 0x51: 121, 0x53: 117,
    0x5B: 55, 0x5C: 54,
}
_MAC_TO_PC = {v: (k, False) for k, v in _PC_TO_MAC.items()}
_MAC_TO_PC.update({v: (k, True) for k, v in _EXT_PC_TO_MAC.items()})


def to_wire_scan(native_scan, extended=False):
    try:
        return _MAC_TO_PC.get(int(native_scan), (int(native_scan), bool(extended)))
    except Exception:
        return 0, False


def _native_from_wire(scan, extended=False):
    table = _EXT_PC_TO_MAC if extended else _PC_TO_MAC
    try:
        return table.get(int(scan), int(scan))
    except Exception:
        return 0


def _vk_from_mac_code(code):
    # 保持 KVM 逃生门使用的 Windows VK 语义
    if code == 53:
        return 0x1B  # Esc
    # mac keycode -> 字母 VK
    mac_letters = {
        0: 'A', 11: 'B', 8: 'C', 2: 'D', 14: 'E', 3: 'F', 5: 'G', 4: 'H',
        34: 'I', 38: 'J', 40: 'K', 37: 'L', 46: 'M', 45: 'N', 31: 'O',
        35: 'P', 12: 'Q', 15: 'R', 1: 'S', 17: 'T', 32: 'U', 9: 'V',
        13: 'W', 7: 'X', 16: 'Y', 6: 'Z'
    }
    ch = mac_letters.get(int(code))
    return ord(ch) if ch else 0


# ---------------- Quartz 全局键鼠钩子 ----------------
class InputHookThread(threading.Thread):
    """macOS Quartz Event Tap。

    on_mouse(dict) / on_key(dict) 返回 True 时吞掉事件。
    需要“辅助功能”与（部分系统版本上）“输入监控”权限。
    """
    def __init__(self, on_mouse=None, on_key=None):
        super().__init__(daemon=True)
        self.on_mouse = on_mouse
        self.on_key = on_key
        self.error = None
        self._tap = None
        self._runloop = None
        self._auto_stopped = False
        self._stop_evt = threading.Event()

    def run(self):
        if Quartz is None:
            self.error = "未安装 pyobjc-framework-Quartz"
            self._auto_stopped = True
            return
        try:
            types = [
                Quartz.kCGEventMouseMoved,
                Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
                Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
                Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp,
                Quartz.kCGEventScrollWheel,
                Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp,
                Quartz.kCGEventLeftMouseDragged, Quartz.kCGEventRightMouseDragged,
                Quartz.kCGEventOtherMouseDragged,
            ]
            mask = 0
            for t in types:
                mask |= Quartz.CGEventMaskBit(t)

            def callback(proxy, event_type, event, refcon):
                if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                                  Quartz.kCGEventTapDisabledByUserInput):
                    try:
                        Quartz.CGEventTapEnable(self._tap, True)
                    except Exception:
                        pass
                    return event
                try:
                    mouse_map = {
                        Quartz.kCGEventMouseMoved: "move",
                        Quartz.kCGEventLeftMouseDragged: "move",
                        Quartz.kCGEventRightMouseDragged: "move",
                        Quartz.kCGEventOtherMouseDragged: "move",
                        Quartz.kCGEventLeftMouseDown: "ldown",
                        Quartz.kCGEventLeftMouseUp: "lup",
                        Quartz.kCGEventRightMouseDown: "rdown",
                        Quartz.kCGEventRightMouseUp: "rup",
                        Quartz.kCGEventOtherMouseDown: "mdown",
                        Quartz.kCGEventOtherMouseUp: "mup",
                        Quartz.kCGEventScrollWheel: "wheel",
                    }
                    if event_type in mouse_map and self.on_mouse is not None:
                        p = Quartz.CGEventGetLocation(event)
                        wheel = 0
                        if event_type == Quartz.kCGEventScrollWheel:
                            wheel = int(Quartz.CGEventGetIntegerValueField(
                                event, Quartz.kCGScrollWheelEventDeltaAxis1)) * 120
                        ev = {"kind": mouse_map[event_type], "x": int(p.x),
                              "y": int(p.y), "wheel": wheel}
                        if self.on_mouse(ev):
                            return None
                    elif event_type in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp) \
                            and self.on_key is not None:
                        code = int(Quartz.CGEventGetIntegerValueField(
                            event, Quartz.kCGKeyboardEventKeycode))
                        ev = {"vk": _vk_from_mac_code(code), "scan": code,
                              "up": event_type == Quartz.kCGEventKeyUp,
                              "extended": False}
                        if self.on_key(ev):
                            return None
                except Exception:
                    pass
                return event

            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGHIDEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionDefault,
                mask, callback, None)
            if self._tap is None:
                self.error = "无法创建输入监听；请在系统设置中授予 POFF 辅助功能/输入监控权限"
                self._auto_stopped = True
                return
            source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._runloop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(self._runloop, source, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self._tap, True)
            Quartz.CFRunLoopRun()
        except Exception as e:
            self.error = f"输入监听异常：{e}"
            self._auto_stopped = True
        finally:
            try:
                if self._tap is not None:
                    Quartz.CGEventTapEnable(self._tap, False)
            except Exception:
                pass

    def stop(self, timeout=2.0):
        self._stop_evt.set()
        try:
            if self._runloop is not None:
                Quartz.CFRunLoopStop(self._runloop)
        except Exception:
            pass
        if threading.current_thread() is not self and self.is_alive():
            self.join(timeout)


# ---------------- Quartz 输入注入 ----------------
def inject_mouse_abs(nx, ny):
    if Quartz is None:
        return False
    try:
        w, h = screen_size()
        x = max(0.0, min(65535.0, float(nx))) / 65535.0 * max(1, w - 1)
        y = max(0.0, min(65535.0, float(ny))) / 65535.0 * max(1, h - 1)
        ev = Quartz.CGEventCreateMouseEvent(
            None, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return True
    except Exception:
        return False


def inject_mouse_button(btn, down):
    if Quartz is None:
        return False
    try:
        x, y = get_cursor_pos()
        if btn == "l":
            button = Quartz.kCGMouseButtonLeft
            et = Quartz.kCGEventLeftMouseDown if down else Quartz.kCGEventLeftMouseUp
        elif btn == "r":
            button = Quartz.kCGMouseButtonRight
            et = Quartz.kCGEventRightMouseDown if down else Quartz.kCGEventRightMouseUp
        else:
            button = Quartz.kCGMouseButtonCenter
            et = Quartz.kCGEventOtherMouseDown if down else Quartz.kCGEventOtherMouseUp
        ev = Quartz.CGEventCreateMouseEvent(None, et, (x, y), button)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return True
    except Exception:
        return False


def inject_wheel(delta):
    if Quartz is None:
        return False
    try:
        lines = int(delta / 120) if abs(int(delta)) >= 120 else int(delta)
        if lines == 0 and delta:
            lines = 1 if delta > 0 else -1
        ev = Quartz.CGEventCreateScrollWheelEvent(
            None, Quartz.kCGScrollEventUnitLine, 1, lines)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return True
    except Exception:
        return False


def inject_key(scan, up, extended):
    if Quartz is None:
        return False
    try:
        code = _native_from_wire(scan, extended)
        ev = Quartz.CGEventCreateKeyboardEvent(None, int(code), not bool(up))
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        return True
    except Exception:
        return False
