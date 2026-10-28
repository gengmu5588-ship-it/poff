# -*- coding: utf-8 -*-
"""POFF V2.1 跨屏键鼠漫游：真实键鼠转发，双端双向。

协议（与关机/文件投递共用 TCP 端口，首行命令分发）：
  建立：控制端连接后发送 "KVM_HELLO\\n"，被控端回 "KVM_OK {w}x{h}\\n"（被控端主屏尺寸）
  事件：控制端逐行发送 "KVM_M {json}\\n"（鼠标）/ "KVM_K {json}\\n"（键盘）
  结束：控制端发送 "KVM_BYE\\n" 并关闭连接

双端双向：两台主机各自按「本机角色 + 屏幕布局」判定自己的触发边缘，
漫游时连接对端转发；任一台都既是控制端也是被控端。
"""
import sys
import json
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk

from . import system_tools as st

KVM_HELLO = "KVM_HELLO"
KVM_OK = "KVM_OK"
KVM_M = "KVM_M"
KVM_K = "KVM_K"
KVM_BYE = "KVM_BYE"

DEFAULT_TCP_PORT = 17890
CONNECT_TIMEOUT = 2.5
SEND_FAIL_LIMIT = 3

DEFAULT_LAYOUT = {
    "main": [50, 70, 190, 110],      # x, y, w, h（画布坐标）
    "laptop": [300, 90, 160, 90],
}
ROLE_NAMES = {"main": "主机屏幕", "laptop": "笔记本屏幕"}


# ===================== 被控端会话（在 handle_conn 线程内同步执行） =====================

def handle_kvm_session(conn, f, app):
    """响应 KVM_HELLO：回屏幕尺寸后循环注入对端转发来的键鼠事件。"""
    try:
        conn.settimeout(None)
    except OSError:
        pass
    try:
        w, h = st.screen_size()
    except Exception:
        w, h = 1920, 1080
    try:
        conn.sendall(f"{KVM_OK} {w}x{h}\n".encode("ascii"))
    except OSError:
        return
    try:
        app.ui_call(app.log, "远端已进入键鼠漫游（本机为被控端）", "info")
    except Exception:
        pass

    while True:
        try:
            line = f.readline(65536)
        except OSError:
            break
        if not line:
            break
        text = line.decode("utf-8", errors="ignore").strip()
        if text == KVM_BYE:
            break
        if text.startswith(KVM_M + " "):
            _inject_mouse_line(text[len(KVM_M) + 1:], w, h)
        elif text.startswith(KVM_K + " "):
            _inject_key_line(text[len(KVM_K) + 1:])
        # 畸形行忽略，保证会话健壮

    try:
        app.ui_call(app.log, "远端键鼠漫游会话已结束", "info")
    except Exception:
        pass


def _inject_mouse_line(payload, scr_w, scr_h):
    try:
        ev = json.loads(payload)
    except Exception:
        return
    kind = ev.get("kind")
    if kind == "move":
        try:
            x = float(ev.get("x", 0))
            y = float(ev.get("y", 0))
        except (TypeError, ValueError):
            return
        nx = int(max(0.0, min(x, scr_w - 1)) / max(1, scr_w - 1) * 65535)
        ny = int(max(0.0, min(y, scr_h - 1)) / max(1, scr_h - 1) * 65535)
        st.inject_mouse_abs(nx, ny)
    elif kind in ("ldown", "lup", "rdown", "rup", "mdown", "mup"):
        st.inject_mouse_button(kind[0], kind.endswith("down"))
    elif kind == "wheel":
        try:
            st.inject_wheel(int(ev.get("wheel", 0)))
        except (TypeError, ValueError):
            pass


def _inject_key_line(payload):
    try:
        ev = json.loads(payload)
    except Exception:
        return
    try:
        st.inject_key(int(ev.get("scan", 0)),
                      bool(ev.get("up")), bool(ev.get("extended")))
    except (TypeError, ValueError):
        pass


# ===================== 控制端引擎 =====================

class RoamEngine(threading.Thread):
    """轮询本机光标，命中触发边缘后接管键鼠并转发到对端。

    物理光标停靠在触发边缘附近的安全点（每次事件后重定位），
    用事件坐标差值驱动对端虚拟光标，避免屏幕边缘钳制导致增量丢失。
    钩子回调只做状态判断 + 入队，socket 发送在引擎线程完成。
    """

    POLL_INTERVAL = 0.008
    EDGE_COOLDOWN = 0.4

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self._stop_flag = threading.Event()
        self.state = "LOCAL"               # LOCAL / REMOTE
        self.status = "未启动"
        self._hook = None
        self._sock = None
        self._queue = queue.Queue()
        self._exit_requested = False
        self._send_fails = 0
        self._peer_w = 0
        self._peer_h = 0
        self._scr_w = 0
        self._scr_h = 0
        self._vx = 0.0
        self._vy = 0.0
        self._pin_x = 0
        self._pin_y = 0
        self._last_mx = 0
        self._last_my = 0
        self._edge_ticks = 0           # 触发区内连续轮询计数（防误触）
        self._need_leave = True        # 启动时光标已在边缘则锁定，直到离开过一次
        self._grace_until = 0.0        # 启动宽限期（勾选开关时鼠标恰在边缘旁）
        self._esc_times = []           # Esc 连击时间戳（强制退出逃生门）

    # ---------------- 配置 ----------------
    def _cfg(self):
        roam = self.app.cfg.get("roam") or {}
        try:
            threshold = max(5, min(120, int(roam.get("threshold", 30))))
        except (TypeError, ValueError):
            threshold = 30
        try:
            port = int(roam.get("target_port") or DEFAULT_TCP_PORT)
        except (TypeError, ValueError):
            port = DEFAULT_TCP_PORT
        return {
            "target_ip": (roam.get("target_ip") or "").strip(),
            "target_port": port,
            "threshold": threshold,
            "direction": self._direction(),
        }

    def _direction(self):
        roam = self.app.cfg.get("roam") or {}
        role = roam.get("self_role") or "main"
        peer_role = "laptop" if role == "main" else "main"
        layout = roam.get("layout") or {}
        s = layout.get(role) or DEFAULT_LAYOUT[role]
        p = layout.get(peer_role) or DEFAULT_LAYOUT[peer_role]
        dx = (p[0] + p[2] / 2.0) - (s[0] + s[2] / 2.0)
        dy = (p[1] + p[3] / 2.0) - (s[1] + s[3] / 2.0)
        if abs(dx) >= abs(dy):
            return "right" if dx > 0 else "left"
        return "down" if dy > 0 else "up"

    # ---------------- 主循环 ----------------
    def run(self):
        self._hook = st.InputHookThread(on_mouse=self._on_mouse,
                                        on_key=self._on_key)
        self._hook.start()
        self.status = "运行中"
        cooldown_until = 0.0
        self._grace_until = time.time() + 2.0

        while not self._stop_flag.is_set():
            try:
                # 钩子层异常熔断联动：钩子自动停用后立即结束引擎，
                # 状态栏给出提示；系统输入此时已由钩子线程自行恢复。
                if self._hook is not None and getattr(self._hook, "_auto_stopped", False):
                    if self.state == "REMOTE":
                        try:
                            self._leave_remote(send_bye=False)
                        except Exception:
                            self.state = "LOCAL"
                    self.status = "输入钩子异常，漫游已自动停用（键鼠已恢复）"
                    self._stop_flag.set()
                    break
                if self.state == "REMOTE":
                    if getattr(self.app, "roam_locked", False):
                        self._leave_remote(send_bye=True)
                        cooldown_until = time.time() + self.EDGE_COOLDOWN
                        continue
                    if self._exit_requested:
                        self._exit_requested = False
                        self._leave_remote(send_bye=True)
                        cooldown_until = time.time() + self.EDGE_COOLDOWN
                        continue
                    self._drain_queue()
                    time.sleep(0.004)
                    continue

                # LOCAL
                if getattr(self.app, "roam_locked", False):
                    self.status = "已锁定（Ctrl+Shift+L 解锁）"
                    time.sleep(0.1)
                    continue
                cfg = self._cfg()
                if not cfg["target_ip"]:
                    self.status = "未选择漫游目标主机"
                    time.sleep(0.25)
                    continue
                self.status = "运行中"
                x, y = st.get_cursor_pos()
                self._scr_w, self._scr_h = st.screen_size()
                # 仅在主屏范围内判定；副屏上的坐标可能越过主屏右缘造成误判
                if not (0 <= x < self._scr_w and 0 <= y < self._scr_h):
                    self._edge_ticks = 0
                    time.sleep(self.POLL_INTERVAL)
                    continue
                # 启动时光标已停在触发边缘（如刚勾选开关/刚重启）：必须先离开
                # 边缘一次才允许触发，避免程序一启动就进入漫游"劫持"键鼠
                if self._need_leave:
                    if self._at_trigger_edge(x, y, cfg):
                        self.status = "运行中（光标离开边缘后激活）"
                        time.sleep(self.POLL_INTERVAL)
                        continue
                    self._need_leave = False
                if time.time() < cooldown_until:
                    time.sleep(self.POLL_INTERVAL)
                    continue
                if (time.time() >= self._grace_until
                        and self._at_trigger_edge(x, y, cfg)
                        and self._edge_ready()):
                    if self._enter_remote(cfg, x, y):
                        continue
                    self.status = "连接对端失败，2 秒后重试"
                    time.sleep(2.0)
                else:
                    time.sleep(self.POLL_INTERVAL)
            except Exception:
                time.sleep(0.1)

        self._leave_remote(send_bye=False)
        try:
            if self._hook is not None:
                self._hook.stop()
        except Exception:
            pass
        self.status = "已停止"

    def stop(self):
        self._stop_flag.set()

    # ---------------- 边缘判定 ----------------
    def _at_trigger_edge(self, x, y, cfg):
        """只有光标顶住物理边缘（±2px）才算进入触发区。

        不用阈值区域做触发：30px 范围内有滚动条/关闭按钮，极易误触。
        阈值参数只作为漫游期间光标的停靠边距与退出后的回位距离。
        """
        d = cfg["direction"]
        if d == "right":
            hit = x >= self._scr_w - 2
        elif d == "left":
            hit = x <= 1
        elif d == "down":
            hit = y >= self._scr_h - 2
        else:
            hit = y <= 1
        if not hit:
            self._edge_ticks = 0
        return hit

    def _edge_ready(self):
        """防误触：光标需顶住边缘约 240ms，且没有按住鼠标键（拖拽不触发）。"""
        try:
            if st.mouse_button_down("left") or st.mouse_button_down("right"):
                self._edge_ticks = 0
                return False
        except Exception:
            pass
        self._edge_ticks += 1
        return self._edge_ticks >= 30

    def _pin_point(self, x, y):
        """漫游期间物理光标的停靠点：触发边缘内侧安全位置。"""
        m = self._cfg()["threshold"] + 12
        d = self._direction()
        if d == "right":
            return self._scr_w - m, max(m, min(self._scr_h - 1 - m, y))
        if d == "left":
            return m, max(m, min(self._scr_h - 1 - m, y))
        if d == "down":
            return max(m, min(self._scr_w - 1 - m, x)), self._scr_h - m
        return max(m, min(self._scr_w - 1 - m, x)), m

    # ---------------- 进入 / 退出漫游 ----------------
    def _enter_remote(self, cfg, x, y):
        sock = None
        try:
            sock = socket.create_connection(
                (cfg["target_ip"], cfg["target_port"]), timeout=CONNECT_TIMEOUT)
            sock.sendall((KVM_HELLO + "\n").encode("ascii"))
            f = sock.makefile("rb")
            line = f.readline(256)
            f.close()
            text = line.decode("ascii", errors="ignore").strip()
            parts = text.split()
            if len(parts) < 2 or parts[0] != KVM_OK:
                sock.close()
                return False
            dims = parts[1].split("x")
            self._peer_w = max(2, int(dims[0]))
            self._peer_h = max(2, int(dims[1]))
            # 发送带超时：网络卡死时让 sendall 尽快失败，避免引擎线程阻塞、
            # 输入被无限吞掉（这是"键鼠卡死"的根因之一）
            sock.settimeout(0.5)
        except (OSError, ValueError, IndexError):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            return False

        self._sock = sock
        self._send_fails = 0
        self._exit_requested = False
        # 虚拟光标从对端对应边缘的镜像点起步（内缩 2px 避免立刻触发返回）
        d = cfg["direction"]
        if d == "right":
            self._vx = 2.0
            self._vy = y / max(1, self._scr_h - 1) * (self._peer_h - 1)
        elif d == "left":
            self._vx = float(self._peer_w - 3)
            self._vy = y / max(1, self._scr_h - 1) * (self._peer_h - 1)
        elif d == "down":
            self._vy = 2.0
            self._vx = x / max(1, self._scr_w - 1) * (self._peer_w - 1)
        else:
            self._vy = float(self._peer_h - 3)
            self._vx = x / max(1, self._scr_w - 1) * (self._peer_w - 1)
        self._clamp_virtual()

        self._pin_x, self._pin_y = self._pin_point(x, y)
        self._last_mx, self._last_my = self._pin_x, self._pin_y
        try:
            st.set_cursor_pos(self._pin_x, self._pin_y)
        except Exception:
            pass

        self.state = "REMOTE"
        self._edge_ticks = 0
        self.status = f"漫游中 → {cfg['target_ip']}"
        try:
            self.app.ui_call(self.app.log,
                             f"已进入漫游：键鼠转发到 {cfg['target_ip']}"
                             f"（Ctrl+Shift+L 可紧急退出）", "ok")
            self.app.ui_call(self.app.roam.show_roam_banner, cfg["target_ip"])
        except Exception:
            pass
        self._queue.put(("M", {"kind": "move",
                               "x": int(self._vx), "y": int(self._vy)}))
        return True

    def _leave_remote(self, send_bye=True):
        was_remote = self.state == "REMOTE"
        self.state = "LOCAL"
        sock = self._sock
        self._sock = None
        if sock is not None:
            if send_bye:
                try:
                    sock.sendall((KVM_BYE + "\n").encode("ascii"))
                except OSError:
                    pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        if was_remote:
            try:
                self.app.ui_call(self.app.roam.hide_roam_banner)
            except Exception:
                pass
            # 光标回本屏触发边缘内侧
            try:
                cfg = self._cfg()
                t = cfg["threshold"] + 6
                cx, cy = st.get_cursor_pos()
                d = cfg["direction"]
                if d == "right":
                    nx, ny = self._scr_w - t, cy
                elif d == "left":
                    nx, ny = t, cy
                elif d == "down":
                    nx, ny = cx, self._scr_h - t
                else:
                    nx, ny = cx, t
                st.set_cursor_pos(
                    max(0, min(self._scr_w - 1, nx)),
                    max(0, min(self._scr_h - 1, ny)))
            except Exception:
                pass
            self.status = "运行中"
            try:
                self.app.ui_call(self.app.log, "已退出漫游，键鼠恢复本机", "info")
            except Exception:
                pass

    # ---------------- 钩子回调（必须快） ----------------
    def _enqueue(self, tag, ev):
        """入队转发；积压过多说明对端/网络异常，主动请求退出，防止无限吞输入。"""
        if self._queue.qsize() > 400:
            self._exit_requested = True
            return
        self._queue.put((tag, ev))

    def _on_mouse(self, ev):
        if self.state != "REMOTE":
            return False
        # 物理逃生门：光标甩到屏幕左上角立即退出漫游，不依赖任何热键
        if ev["kind"] == "move" and ev["x"] <= 6 and ev["y"] <= 6:
            self._exit_requested = True
            return True
        kind = ev["kind"]
        if kind == "move":
            dx = ev["x"] - self._last_mx
            dy = ev["y"] - self._last_my
            # 重定位到停靠点，随后产生的人造事件 delta 为 0，天然无害
            self._last_mx, self._last_my = self._pin_x, self._pin_y
            try:
                st.set_cursor_pos(self._pin_x, self._pin_y)
            except Exception:
                pass
            if dx or dy:
                self._vx += dx
                self._vy += dy
                self._clamp_virtual()
                self._enqueue("M", {"kind": "move",
                                    "x": int(self._vx), "y": int(self._vy)})
                self._check_exit_edge()
        elif kind == "wheel":
            self._enqueue("M", {"kind": "wheel", "wheel": ev.get("wheel", 0)})
        else:
            self._enqueue("M", {"kind": kind})
        return True

    def _on_key(self, ev):
        if self.state != "REMOTE":
            return False
        # 紧急退出组合键 Ctrl+Shift+L 不吞不转发，保证本机热键可解锁
        if ev["vk"] == 0x4C and not ev["up"]:
            if st.is_ctrl_shift_down():
                return False
        # 物理逃生门：1.2 秒内连按 3 次 Esc 立即退出漫游
        if ev["vk"] == 0x1B and not ev["up"]:
            now = time.time()
            self._esc_times = [t for t in self._esc_times if now - t < 1.2]
            self._esc_times.append(now)
            if len(self._esc_times) >= 3:
                self._exit_requested = True
            return True
        scan, extended = st.to_wire_scan(ev["scan"], ev.get("extended", False))
        self._enqueue("K", {"scan": scan, "up": ev["up"],
                            "extended": extended})
        return True

    # ---------------- 虚拟光标 ----------------
    def _clamp_virtual(self):
        self._vx = max(0.0, min(float(self._peer_w - 1), self._vx))
        self._vy = max(0.0, min(float(self._peer_h - 1), self._vy))

    def _check_exit_edge(self):
        d = self._direction()
        if d == "right" and self._vx <= 0:
            self._exit_requested = True
        elif d == "left" and self._vx >= self._peer_w - 1:
            self._exit_requested = True
        elif d == "down" and self._vy <= 0:
            self._exit_requested = True
        elif d == "up" and self._vy >= self._peer_h - 1:
            self._exit_requested = True

    def _drain_queue(self):
        sock = self._sock
        if sock is None:
            return
        latest_move = None
        while True:
            try:
                tag, ev = self._queue.get_nowait()
            except queue.Empty:
                break
            if tag == "M" and isinstance(ev, dict) and ev.get("kind") == "move":
                # move 事件是绝对虚拟坐标，中间帧无意义；Wi-Fi 抖动时事件
                # 批量到达，逐帧转发会造成漂移。只保留最新一帧，发送频率
                # 更稳定，对端注入压力更低。
                latest_move = ev
                continue
            if latest_move is not None:
                self._send_line(sock, "M", latest_move)
                latest_move = None
            self._send_line(sock, tag, ev)
        if latest_move is not None:
            self._send_line(sock, "M", latest_move)

    def _send_line(self, sock, tag, ev):
        line = (KVM_M if tag == "M" else KVM_K) + " " + \
            json.dumps(ev, separators=(",", ":")) + "\n"
        try:
            sock.sendall(line.encode("utf-8"))
        except OSError:
            self._send_fails += 1
            if self._send_fails >= SEND_FAIL_LIMIT:
                self._exit_requested = True
                try:
                    self.app.ui_call(
                        self.app.log, "漫游连接中断，已退回本机", "err")
                except Exception:
                    pass


# ===================== 界面模块 =====================

class RoamModule:
    ROLE_DISPLAY = {"main": "主机（main）", "laptop": "笔记本（laptop）"}
    ROLE_BY_DISPLAY = {v: k for k, v in ROLE_DISPLAY.items()}

    def __init__(self, app):
        self.app = app
        roam = app.cfg.setdefault("roam", {})
        layout = roam.get("layout") or {}
        self.layout = {
            "main": list(layout.get("main") or DEFAULT_LAYOUT["main"]),
            "laptop": list(layout.get("laptop") or DEFAULT_LAYOUT["laptop"]),
        }
        self.canvas = None
        self.items = {}
        self._drag = None
        self.lbl_lock = None
        self.threshold_var = tk.IntVar(value=int(roam.get("threshold", 30)))
        self.role_var = tk.StringVar(
            value=self.ROLE_DISPLAY.get(roam.get("self_role") or "main",
                                        self.ROLE_DISPLAY["main"]))
        self.target_var = tk.StringVar(value=roam.get("target_ip") or "")
        self.var_enable = None
        self.enabled = False
        self.spin = None
        self.btn_save = None
        self.role_box = None
        self.target_box = None
        self.btn_refresh = None
        self.lbl_engine = None
        self.lbl_state = None
        self.engine = None
        self._widgets = []
        self._banner = None

    # ---------------- 界面 ----------------
    def build_tab(self, parent):
        box = ttk.Frame(parent, style="Card.TFrame", padding=16)
        box.pack(fill="both", expand=True)

        head = ttk.Frame(box, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text="跨屏键鼠漫游", style="H2.TLabel").pack(side="left")
        ttk.Label(head, text="V2.1 真实转发 · 双端双向", style="Badge.TLabel").pack(
            side="left", padx=10)
        self.lbl_lock = ttk.Label(head, text="", style="Muted.TLabel")
        self.lbl_lock.pack(side="right")
        self.update_lock_label()

        ttk.Label(box,
                  text="拖拽下方两块屏幕调整相对位置。把鼠标推到本机屏幕的对应边缘并停留片刻即进入漫游，"
                       "键鼠将转发到目标主机；把光标移回该边缘内侧即返回本机。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(4, 10))

        self.canvas = tk.Canvas(box, width=560, height=250, bg="#0F172A",
                                highlightthickness=0)
        self.canvas.pack(anchor="w")
        self._draw()

        # 双端双向配置行
        row0 = ttk.Frame(box, style="Card.TFrame")
        row0.pack(fill="x", pady=(12, 0))
        ttk.Label(row0, text="本机角色：", style="Card.TLabel").pack(side="left")
        self.role_box = ttk.Combobox(row0, width=14, state="readonly",
                                     textvariable=self.role_var,
                                     values=list(self.ROLE_DISPLAY.values()))
        self.role_box.pack(side="left")
        self.role_box.bind("<<ComboboxSelected>>", lambda e: self.save(silent=True))
        ttk.Label(row0, text="漫游目标：", style="Card.TLabel").pack(
            side="left", padx=(16, 0))
        self.target_box = ttk.Combobox(row0, width=18, state="readonly",
                                       textvariable=self.target_var, values=[])
        self.target_box.pack(side="left")
        self.target_box.bind("<<ComboboxSelected>>", lambda e: self.save(silent=True))
        self.btn_refresh = ttk.Button(row0, text="刷新", width=6,
                                      command=self.refresh_targets)
        self.btn_refresh.pack(side="left", padx=6)

        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x", pady=12)
        ttk.Label(row, text="光标停靠边距：", style="Card.TLabel").pack(side="left")
        self.spin = ttk.Spinbox(row, from_=5, to=120, width=6,
                                textvariable=self.threshold_var)
        self.spin.pack(side="left")
        ttk.Label(row, text="px", style="Muted.TLabel").pack(side="left", padx=(4, 16))
        ttk.Label(row, text="漫游锁定热键：Ctrl+Shift+L", style="Muted.TLabel").pack(side="left")
        self.btn_save = ttk.Button(row, text="保存布局", command=self.save)
        self.btn_save.pack(side="right")

        stat = ttk.Frame(box, style="Soft.TFrame", padding=10)
        stat.pack(fill="x", pady=(0, 10))
        self.lbl_engine = ttk.Label(stat, text="引擎状态：未启动", style="Card.TLabel")
        self.lbl_engine.pack(side="left")
        self.lbl_state = ttk.Label(stat, text="当前：本机", style="Muted.TLabel")
        self.lbl_state.pack(side="right")

        ttk.Label(box,
                  text="双端双向：两台主机都打开漫游开关并各自完成配置（本机角色与布局互为镜像）后，"
                       "把鼠标推到本机屏幕边缘并停留片刻即进入漫游。漫游中可随时：把鼠标移回该边缘内侧"
                       "返回本机；甩到屏幕左上角、连按 3 次 Esc 或 Ctrl+Shift+L 强制退出。"
                       "风险提示：Wi-Fi 环境鼠标可能出现漂移，有线局域网最佳。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(4, 0))

        self._widgets = [self.role_box, self.target_box, self.btn_refresh]
        self.refresh_targets()
        self._poll_status()

    def _draw(self):
        c = self.canvas
        c.delete("all")
        for x in range(0, 561, 40):
            c.create_line(x, 0, x, 250, fill="#16233A")
        for y in range(0, 251, 40):
            c.create_line(0, y, 560, y, fill="#16233A")
        c.create_text(280, 20, text="屏幕布局（拖拽调整）", fill="#64748B",
                      font=("PingFang SC", 9))

        colors = {"main": ("#1D4ED8", "#93C5FD", "主机屏幕"),
                  "laptop": ("#047857", "#6EE7B7", "笔记本屏幕")}
        self.items = {}
        for key, rect in self.layout.items():
            x, y, w, h = rect
            fill, outline, label = colors[key]
            rect_tag = f"rect_{key}"
            text_tag = f"text_{key}"
            c.create_rectangle(x, y, x + w, y + h, fill=fill, outline=outline,
                               width=2, tags=(rect_tag,))
            c.create_text(x + w / 2, y + h / 2, text=label, fill="#FFFFFF",
                          font=("PingFang SC", 10, "bold"), tags=(text_tag,))
            for tag in (rect_tag, text_tag):
                c.tag_bind(tag, "<ButtonPress-1>", lambda e, k=key: self._press(e, k))
                c.tag_bind(tag, "<B1-Motion>", self._move)
                c.tag_bind(tag, "<ButtonRelease-1>", lambda e: self.save(silent=True))
                c.tag_bind(tag, "<Enter>", lambda e: c.config(cursor="fleur"))
                c.tag_bind(tag, "<Leave>", lambda e: c.config(cursor=""))

    def _press(self, event, key):
        if not self.enabled:
            return
        x, y, w, h = self.layout[key]
        self._drag = {"key": key, "dx": event.x - x, "dy": event.y - y}

    def _move(self, event):
        if not self._drag:
            return
        key = self._drag["key"]
        x, y, w, h = self.layout[key]
        nx = max(0, min(560 - w, event.x - self._drag["dx"]))
        ny = max(0, min(250 - h, event.y - self._drag["dy"]))
        self.layout[key] = [nx, ny, w, h]
        self.canvas.coords(f"rect_{key}", nx, ny, nx + w, ny + h)
        self.canvas.coords(f"text_{key}", nx + w / 2, ny + h / 2)

    # ---------------- 目标主机 ----------------
    def refresh_targets(self):
        try:
            with self.app.peer_lock:
                ips = sorted(self.app.peer_list.keys())
        except Exception:
            ips = []
        if self.target_box is not None:
            current = self.target_var.get()
            self.target_box.config(values=ips)
            if current and current not in ips:
                self.target_var.set("")
        if ips and not self.target_var.get():
            self.target_var.set(ips[0])
            self.save(silent=True)

    def _poll_status(self):
        try:
            eng = self.engine
            if eng is None:
                self.lbl_engine.config(text="引擎状态：未启动")
                self.lbl_state.config(text="当前：本机")
            else:
                self.lbl_engine.config(text=f"引擎状态：{eng.status}")
                self.lbl_state.config(
                    text=("当前：漫游中（远端）" if eng.state == "REMOTE"
                          else "当前：本机"))
        except (tk.TclError, AttributeError):
            return
        try:
            self.app.root.after(500, self._poll_status)
        except (tk.TclError, RuntimeError):
            pass

    # ---------------- 保存 ----------------
    def save(self, silent=False):
        roam = self.app.cfg.setdefault("roam", {})
        try:
            threshold = max(5, min(120, int(self.threshold_var.get())))
        except (tk.TclError, ValueError):
            threshold = 30
            self.threshold_var.set(threshold)
        roam["layout"] = self.layout
        roam["threshold"] = threshold
        roam["self_role"] = self.ROLE_BY_DISPLAY.get(self.role_var.get(), "main")
        target = (self.target_var.get() or "").strip()
        roam["target_ip"] = target
        port = DEFAULT_TCP_PORT
        if target:
            try:
                with self.app.peer_lock:
                    info = self.app.peer_list.get(target) or {}
                port = int(info.get("port") or DEFAULT_TCP_PORT)
            except Exception:
                port = DEFAULT_TCP_PORT
        roam["target_port"] = port
        self.app.save_cfg()
        if not silent:
            self.app.log("漫游配置已保存", "ok")

    def update_lock_label(self):
        if self.lbl_lock is None:
            return
        locked = getattr(self.app, "roam_locked", False)
        self.lbl_lock.config(
            text=("漫游已锁定（热键可解锁）" if locked else "漫游未锁定"),
            style=("Warn.TLabel" if locked else "Muted.TLabel"))

    # ---------------- 漫游提示横幅（主线程调用） ----------------
    def show_roam_banner(self, ip):
        """进入漫游时在屏幕顶部显示常驻提示，避免用户以为键鼠卡死。"""
        try:
            self.hide_roam_banner()
            win = tk.Toplevel(self.app.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            try:
                win.attributes("-alpha", 0.94)
            except tk.TclError:
                pass
            bar = tk.Frame(win, bg="#7F1D1D", padx=16, pady=7)
            bar.pack()
            tk.Label(bar,
                     text=f"正在控制 {ip}　·　鼠标移回边缘返回本机　·　甩到屏幕左上角或连按 3 次 Esc 强制退出",
                     bg="#7F1D1D", fg="#FEE2E2",
                     font=("PingFang SC", 10, "bold")).pack()
            win.update_idletasks()
            w = win.winfo_reqwidth()
            sw = win.winfo_screenwidth()
            win.geometry(f"+{max(0, int((sw - w) / 2))}+6")
            self._banner = win
        except Exception:
            self._banner = None

    def hide_roam_banner(self):
        win = self._banner
        self._banner = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    # ---------------- 引擎生命周期 ----------------
    def set_enabled(self, on):
        self.enabled = bool(on)
        state = "normal" if self.enabled else "disabled"
        for widget in [self.spin, self.btn_save] + self._widgets:
            if widget is None:
                continue
            try:
                widget.config(state=("readonly" if widget in (self.role_box, self.target_box)
                                     and self.enabled else state))
            except (tk.TclError, AttributeError):
                pass
        if self.enabled:
            self.start_engine()
        else:
            self.stop_engine()

    def start_engine(self):
        if self.engine is not None:
            return
        try:
            self.engine = RoamEngine(self.app)
            self.engine.start()
            self.app.log("漫游转发引擎已启动（双端双向）", "ok")
        except Exception as e:
            self.engine = None
            self.app.log(f"漫游引擎启动失败：{e}", "err")

    def stop_engine(self):
        eng = self.engine
        self.engine = None
        self.hide_roam_banner()
        if eng is not None:
            try:
                eng.stop()
                eng.join(timeout=2.0)
            except Exception:
                pass
