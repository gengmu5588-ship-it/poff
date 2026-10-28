# -*- coding: utf-8 -*-
"""POFF 屏幕共享模块（A 发起 / B 接收，局域网内 MJPEG 实时流）。

能力：
  - A 端选择一块屏幕（多屏枚举）→ 启动独立 HTTP 服务（端口 17892）
    提供 MJPEG multipart/x-mixed-replace 端点 `/screenshare`
  - A 通过已有 KVM TCP 会话发送 screenshare_start 控制码给 B
    （payload 带 A 端 IP 和 17892 端口）
  - B 收到命令 → 自动弹出接收窗口，通过 HTTP 流式拉帧
  - 任一端停止 → 发 screenshare_stop + 停流 + 销毁接收窗口
  - 自动码率控制：画面变化率高时 Q=70，静止 Q=40；帧间隔最小 60ms（≈16fps）

端口规划：
  17890：主 TCP 协议（关机 / KVM 会话）
  17891：手机投递 HTTP（MobiDrop）
  17892：屏幕共享 MJPEG HTTP（本模块独占，避免和手机投递路由耦合）

线程安全：流循环在后台线程；HTTP multipart 解析走独立线程；
所有 UI 更新通过 root.after 回到主线程。
"""
import io
import json
import os
import socket
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn
from tkinter import ttk

try:
    import cv2
    _CV2_OK = True
except Exception:
    _CV2_OK = False

try:
    import numpy as np
    _NP_OK = True
except Exception:
    _NP_OK = False

try:
    from PIL import Image, ImageGrab, ImageTk
    _PIL_OK = True
except Exception:
    _PIL_OK = False


SHARE_HTTP_PORT = 17892


# ===================== A 端：共享发起 =====================

class ScreenShareModule:
    """屏幕共享模块入口，KVM 面板"屏幕共享"功能块。"""

    HTTP_PATH = "/screenshare"
    MJPEG_BOUNDARY = b"--poff-sh-frame-boundary"

    def __init__(self, app):
        self.app = app
        self.enabled = True
        self._server = None           # ThreadingHTTPServer 实例
        self._server_th = None        # HTTP 服务线程
        self._stream_thread = None    # 帧循环线程
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._screen_index = 0        # 当前共享的屏幕索引
        self._active_peers = set()    # 当前正在观看的 peer IP 集合
        self._lock = threading.Lock()
        self._peer_queues = {}        # peer → [Queue] 列表
        self._preview_win = None      # 发起端悬浮小窗（显示"正在共享给 N 台主机"）
        self._preview_label = None
        self._tick_job = None
        self._started_at = 0.0

    def set_enabled(self, on):
        self.enabled = bool(on)
        if not on:
            # 关闭开关时如果正在共享，停掉
            try:
                if self._server is not None:
                    self._stop_sharing()
            except Exception:
                pass

    # ---------------- 面板 UI ----------------
    def build_panel(self, parent):
        box = ttk.LabelFrame(parent, text="屏幕共享", padding=(12, 10))
        box.pack(fill="x", anchor="n", padx=16, pady=12)
        if not (_CV2_OK and _NP_OK and _PIL_OK):
            ttk.Label(box, text="依赖缺失：opencv / numpy / Pillow 不可用，共享禁用。",
                      style="Muted.TLabel", wraplength=440).pack(anchor="w")
            return box

        ttk.Label(box, text="A 发起屏幕共享，B 通过 KVM 会话自动接收并弹出预览窗。",
                  style="Muted.TLabel", wraplength=440).pack(anchor="w", pady=(0, 8))

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.btn_start = ttk.Button(row, text="发起屏幕共享", style="Accent.TButton",
                                     command=self._on_start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(row, text="停止共享", command=self._on_stop,
                                    state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(8, 0))
        tk.Label(row2, text="目标主机：", bg=parent.master["background"]
                 if parent.master and "background" in parent.master.keys() else "#F2F5FA",
                 fg="#475569", font=("Microsoft YaHei UI", 9)).pack(side="left")
        peers = [ip for ip, _ in self.app.peer_items()]
        self.target_var = tk.StringVar(value=self.app.selected_ip() or (peers[0] if peers else ""))
        self.target_combo = ttk.Combobox(row2, values=peers, textvariable=self.target_var,
                                         width=20, state="readonly")
        self.target_combo.pack(side="left", padx=(4, 0))
        if not peers:
            tk.Label(row2, text="（未添加主机）", bg=parent.master["background"]
                     if parent.master else "#F2F5FA", fg="#94A3B8",
                     font=("Microsoft YaHei UI", 8)).pack(side="left", padx=4)
        return box

    def refresh_peer_list(self):
        """主机列表变化后由主程序调用，刷新共享目标下拉。"""
        combo = getattr(self, "target_combo", None)
        if combo is None:
            return
        try:
            peers = [ip for ip, _ in self.app.peer_items()]
            combo.configure(values=peers)
            cur = self.target_var.get()
            if cur not in peers:
                self.target_var.set(self.app.selected_ip() or (peers[0] if peers else ""))
        except Exception:
            pass

    # ---------------- 发起 ----------------
    def _on_start(self):
        if self._stream_thread and self._stream_thread.is_alive():
            self.app.log("已经在共享中。", "warn")
            return
        # 选屏幕
        screens = list_screens()
        if len(screens) == 0:
            messagebox.showerror("无可用屏幕", "未枚举到任何屏幕。")
            return
        dlg = ScreenPickerDialog(self.app.root, screens)
        self.app.root.wait_window(dlg.win)
        if not dlg.ok:
            return

        # 选目标主机
        target = self.target_var.get()
        if not target:
            messagebox.showwarning("未选择", "请先在列表中选择目标主机。")
            return

        # 检查 KVM 会话（需要是 ROAMING_ESTABLISHED）
        roam = getattr(self.app, "roam", None)
        if roam is None or not roam.is_connected(target):
            messagebox.showwarning("KVM 未连接",
                                   "屏幕共享需要双方 KVM 会话已建立（功能总开关里的键鼠漫游已开 + 主机已配对）。")
            return

        self._screen_index = dlg.index
        self._start_sharing()

        # 通过 KVM 会话发控制码
        try:
            roam.send_share_start(target)
            self.app.log(f"已通知 {target} 准备接收屏幕共享。", "ok")
        except Exception as e:
            self.app.log(f"发送共享通知失败：{e}", "err")

    def _on_stop(self):
        target = self.target_var.get()
        self._stop_sharing()
        roam = getattr(self.app, "roam", None)
        if roam is not None and target:
            try:
                roam.send_share_stop(target)
            except Exception:
                pass

    def _start_sharing(self):
        # 启动独立 HTTP 服务（端口 17892），为 /screenshare 提供 MJPEG 流
        # 不占用黑洞的 17890，也不耦合手机投递的 17891
        try:
            self._server = self._make_http_server()
            self._server_th = threading.Thread(
                target=self._server.serve_forever, daemon=True)
            self._server_th.start()
        except OSError as e:
            self.app.log(f"启动共享 HTTP 服务失败（端口 {SHARE_HTTP_PORT}）：{e}", "err")
            messagebox.showerror("端口被占用",
                                 f"无法监听 {SHARE_HTTP_PORT} 端口。")
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")
            return

        self._stop_event.clear()
        self._pause_event.set()
        self._peer_queues = {}
        self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._stream_thread.start()
        self._started_at = time.time()

        # 防火墙放行已在主程序启动时统一处理（_ensure_firewall_rules 含 17892）

        # UI 切换
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.app.log(f"屏幕共享已启动（屏幕 #{self._screen_index}），"
                     f"等待接收方连接 http://{self.app.local_ip}:{SHARE_HTTP_PORT}/screenshare ...", "ok")
        self._show_mini_preview()

    def _stop_sharing(self):
        self._stop_event.set()
        with self._lock:
            self._active_peers.clear()
        # 停 HTTP server
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._hide_mini_preview()
        if self._tick_job is not None:
            try:
                self.app.root.after_cancel(self._tick_job)
            except tk.TclError:
                pass
        self.app.log("屏幕共享已停止。", "info")

    def _make_http_server(self):
        module_self = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return    # 关闭默认 stderr 日志

            def do_GET(self):
                if self.path not in ("/screenshare", "/screenshare/"):
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type", "multipart/x-mixed-replace; "
                                    "boundary=--poff-sh-frame-boundary")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                peer = self.client_address[0]
                with module_self._lock:
                    module_self._active_peers.add(peer)
                module_self.app.log(f"接收方已连接：{peer}", "ok")
                try:
                    module_self._stream_write_loop(self.wfile, peer)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as e:
                    module_self.app.log(f"推流异常：{e}", "warn")
                finally:
                    with module_self._lock:
                        module_self._active_peers.discard(peer)

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

        # SO_EXCLUSIVEADDRUSE：避免多实例抢占同一端口导致分流
        try:
            server = ThreadingHTTPServer(("0.0.0.0", SHARE_HTTP_PORT), _Handler)
            server.socket.setsockopt(
                socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            raise
        return server

    def _stream_write_loop(self, wfile, peer):
        """单连接的 MJPEG 写入循环。

        读 self._frame_queue（由 _stream_loop 填充），避免多 peer 抢同一份图片对象。
        没接收到帧时最多阻塞 2s（无观众时自动暂停抓屏）。
        """
        import queue
        local_q = queue.Queue(maxsize=3)
        # 共享 frame_queue → 为这个 peer 建立独立消费者
        self._peer_queues.setdefault(peer, []).append(local_q)
        try:
            while not self._stop_event.is_set():
                try:
                    jpeg_bytes = local_q.get(timeout=2.0)
                except queue.Empty:
                    continue
                try:
                    wfile.write(self.MJPEG_BOUNDARY + b"\r\n")
                    wfile.write(b"Content-Type: image/jpeg\r\n")
                    wfile.write(f"Content-Length: {len(jpeg_bytes)}\r\n".encode())
                    wfile.write(b"\r\n")
                    wfile.write(jpeg_bytes)
                    wfile.write(b"\r\n")
                except Exception:
                    break
        finally:
            qlist = self._peer_queues.get(peer, [])
            if local_q in qlist:
                qlist.remove(local_q)
                if not qlist:
                    self._peer_queues.pop(peer, None)

    # ---------------- 帧循环 ----------------
    def _stream_loop(self):
        self._peer_queues = {}   # peer_ip → [Queue] 列表
        last_frame = None
        last_change = 0.0
        try:
            while not self._stop_event.is_set():
                self._pause_event.wait()
                with self._lock:
                    n_peers = sum(len(qlist) for qlist in self._peer_queues.values())
                if n_peers == 0:
                    # 没人看，暂停抓屏减少 CPU
                    time.sleep(0.5)
                    continue

                # 抓指定屏幕
                region = get_screen_bbox(self._screen_index)
                try:
                    img = ImageGrab.grab(bbox=region)
                except Exception:
                    time.sleep(0.06)
                    continue
                arr = np.array(img)
                # 变化检测（降采样到 80×45 做 diff，差 > 5% 才发）
                try:
                    thumb = cv2.resize(arr, (80, 45))
                    if last_frame is not None:
                        diff = cv2.absdiff(thumb, last_frame)
                        change = float(diff.sum()) / (diff.size * 255)
                        if change < 0.005 and time.time() - last_change < 1.2:
                            time.sleep(0.06)
                            continue
                        if change >= 0.005:
                            last_change = time.time()
                    last_frame = thumb
                except Exception:
                    pass

                # JPEG 编码（静态 40，动态 70）
                quality = 70 if last_change > time.time() - 0.5 else 55
                try:
                    _, buf = cv2.imencode(".jpg", arr[:, :, ::-1],
                                          [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                    jpeg_bytes = buf.tobytes()
                except Exception:
                    time.sleep(0.06)
                    continue

                # 投递到所有 peer queue（溢出则丢弃最旧帧）
                for peer, qlist in list(self._peer_queues.items()):
                    for q in qlist:
                        while True:
                            try:
                                q.put_nowait(jpeg_bytes)
                                break
                            except queue.Full:
                                try:
                                    q.get_nowait()
                                except queue.Empty:
                                    break

                time.sleep(0.06)   # ~16fps
        except Exception as e:
            self.app.log(f"共享帧循环异常：{e}", "warn")

    # ---------------- 发起端悬浮窗 ----------------
    def _show_mini_preview(self):
        win = tk.Toplevel(self.app.root)
        self._preview_win = win
        win.overrideredirect(True)
        win.configure(bg="#0B1220")
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.95)
        win.resizable(False, False)

        row = tk.Frame(win, bg="#0B1220")
        row.pack(padx=14, pady=10)
        tk.Label(row, text="●", fg="#10B981", bg="#0B1220",
                 font=("Arial", 10, "bold")).pack(side="left")
        self._preview_label = tk.Label(row, text="屏幕共享中...",
                                        fg="#E5E7EB", bg="#0B1220",
                                        font=("Microsoft YaHei UI", 9))
        self._preview_label.pack(side="left", padx=(6, 0))

        sw = self.app.root.winfo_screenwidth()
        win.update_idletasks()
        w = win.winfo_width() or 180
        h = win.winfo_height() or 40
        win.geometry(f"+{sw - w - 20}+88")
        self._mini_tick()

    def _mini_tick(self):
        if self._preview_win is None or self._preview_label is None:
            return
        n = sum(len(qlist) for qlist in getattr(self, "_peer_queues", {}).values())
        elapsed = int(time.time() - self._started_at) if self._started_at else 0
        m, s = divmod(elapsed, 60)
        self._preview_label.config(text=f"屏幕共享中 · {m:02d}:{s:02d} · {n} 台主机观看")
        self._tick_job = self.app.root.after(1000, self._mini_tick)

    def _hide_mini_preview(self):
        try:
            self._preview_win.destroy()
        except tk.TclError:
            pass
        self._preview_win = None
        self._preview_label = None
        if self._tick_job is not None:
            try:
                self.app.root.after_cancel(self._tick_job)
            except tk.TclError:
                pass
            self._tick_job = None


# ===================== B 端：接收 =====================

class ScreenReceiver:
    """B 端接收窗口：从发起端 HTTP 端点拉 MJPEG 流，Canvas 周期刷新。"""

    def __init__(self, app, url, peer_ip):
        self.app = app
        self.url = url
        self.peer_ip = peer_ip
        self._stop = threading.Event()
        self._thread = None
        self._photo = None
        self._tk = None
        self._queue = []        # 主线程通过 root.after 消耗，只保留最新一帧
        self._lock = threading.Lock()
        self._build()

    def _build(self):
        win = tk.Toplevel(self.app.root)
        self.win = win
        win.title(f"屏幕共享 · {self.peer_ip}")
        win.configure(bg="#0B1220")
        win.attributes("-topmost", True)
        win.resizable(True, True)
        win.geometry("960x540")

        bar = tk.Frame(win, bg="#111827")
        bar.pack(fill="x")
        tk.Label(bar, text=f"● 正在接收 {self.peer_ip} 的屏幕",
                 bg="#111827", fg="#10B981",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=10, pady=6)
        self._status_lbl = tk.Label(bar, text="连接中...", bg="#111827", fg="#94A3B8",
                                     font=("Microsoft YaHei UI", 8))
        self._status_lbl.pack(side="left", padx=8)
        tk.Label(bar, text="关闭", bg="#111827", fg="#F87171", padx=10, pady=6,
                 cursor="hand2").pack(side="right")
        bar.winfo_children()[-1].bind("<Button-1>", lambda e: self.close())

        self.canvas = tk.Canvas(win, bg="#0B1220", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        win.bind("<Configure>", lambda e: self._redraw())
        win.bind("<Escape>", lambda e: self.close())
        win.protocol("WM_DELETE_WINDOW", self.close)

        self._thread = threading.Thread(target=self._pull_loop, daemon=True)
        self._thread.start()
        self._poll()

    # ---------------- 拉流 ----------------
    def _pull_loop(self):
        """通过 socket 直接读 HTTP，解析 multipart boundary，遇到 JPEG 部分就塞进队列。"""
        boundary = b"--poff-sh-frame-boundary"
        try:
            # 从 URL 解析 host / port
            from urllib.parse import urlparse
            parsed = urlparse(self.url)
            host = parsed.hostname or self.peer_ip
            port = parsed.port or 17890
            path = parsed.path or "/screenshare"
        except Exception:
            self.app.root.after(0, lambda: self._set_status("URL 解析失败"))
            return

        buf = b""
        frame_bytes = None
        in_headers = True
        try:
            sock = socket.create_connection((host, port), timeout=5)
            sock.settimeout(10)
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode()
            sock.sendall(request)
            self.app.root.after(0, lambda: self._set_status("已连接"))

            while not self._stop.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                # 按 boundary 分割
                while boundary in buf:
                    idx = buf.find(boundary)
                    part = buf[:idx]
                    buf = buf[idx + len(boundary):]
                    if part:
                        frame_bytes = self._extract_jpeg(part)
                        if frame_bytes:
                            with self._lock:
                                # 只保留最新一帧
                                self._queue = [frame_bytes]
        except Exception as e:
            self.app.root.after(0, lambda: self._set_status(f"连接断开：{e}"))
        finally:
            with self._lock:
                self._queue = []
            try:
                sock.close()
            except Exception:
                pass

    @staticmethod
    def _extract_jpeg(part):
        """从 multipart 单 part 里去掉 HTTP headers（直到 \r\n\r\n）返回 JPEG 字节块。"""
        sep = b"\r\n\r\n"
        idx = part.find(sep)
        if idx < 0:
            sep = b"\n\n"
            idx = part.find(sep)
        if idx < 0:
            return None
        return part[idx + len(sep):].rstrip(b"\r\n")

    def _set_status(self, text):
        try:
            self._status_lbl.config(text=text)
        except (tk.TclError, AttributeError):
            pass

    # ---------------- 主线程刷新 ----------------
    def _poll(self):
        if self._stop.is_set():
            return
        with self._lock:
            frames = self._queue[:]
            self._queue = []
        if frames:
            self._display(frames[-1])
        self._tk = self.app.root.after(30, self._poll)

    def _display(self, jpeg_bytes):
        try:
            img = Image.open(io.BytesIO(jpeg_bytes))
        except Exception:
            return
        cw = max(100, self.canvas.winfo_width())
        ch = max(100, self.canvas.winfo_height())
        # 等比缩放，居中
        tw, th = img.size
        scale = min(cw / tw, ch / th, 1.0)
        dw, dh = max(1, int(tw * scale)), max(1, int(th * scale))
        try:
            thumb = img.resize((dw, dh), Image.LANCZOS)
        except Exception:
            return
        self._photo = ImageTk.PhotoImage(thumb)
        self.canvas.delete("all")
        self.canvas.create_image((cw - dw) // 2, (ch - dh) // 2,
                                  anchor="nw", image=self._photo)

    def _redraw(self):
        # 触发重绘（下一次 _poll 自然用最新帧）
        pass

    def close(self):
        self._stop.set()
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if self._tk is not None:
            try:
                self.app.root.after_cancel(self._tk)
            except tk.TclError:
                pass
        self.app.log(f"已关闭屏幕共享接收窗口（来自 {self.peer_ip}）。", "info")


# ===================== 屏幕枚举（ctypes） =====================

def list_screens():
    """枚举所有物理显示器，返回 [(index, left, top, right, bottom)]。"""
    if os.name != "nt":
        # 非 Windows 回退：返回单屏
        root = tk._default_root or tk.Tk()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        return [(0, 0, 0, sw, sh)]
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    MONITORINFOF_PRIMARY = 1

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]
    MONITORINFO.cbSize = ctypes.sizeof(MONITORINFO)

    results = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL,
                       wintypes.HMONITOR, wintypes.HDC,
                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def callback(hmon, hdc, lprc, lparam):
        mi = MONITORINFO()
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            m = mi.rcMonitor
            results.append((m.left, m.top, m.right, m.bottom, mi.dwFlags))
        return True

    user32.EnumDisplayMonitors(None, None, callback, 0)
    # 按 left+top 排序，主屏（MONITORINFOF_PRIMARY）放第一
    results.sort(key=lambda r: (0 if r[4] & MONITORINFOF_PRIMARY else 1, r[0], r[1]))
    return [(i, r[0], r[1], r[2], r[3]) for i, r in enumerate(results)]


def get_screen_bbox(index):
    screens = list_screens()
    if 0 <= index < len(screens):
        return screens[index][1:5]
    # 回退主屏
    sw = tk._default_root.winfo_screenwidth() if tk._default_root else 1920
    sh = tk._default_root.winfo_screenheight() if tk._default_root else 1080
    return (0, 0, sw, sh)


class ScreenPickerDialog:
    def __init__(self, root, screens):
        self.screens = screens
        self.win = tk.Toplevel(root)
        self.win.title("选择要共享的屏幕")
        self.win.configure(bg="#0B1220")
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.ok = False
        self.index = 0
        self._build()

    def _build(self):
        tk.Label(self.win, text="选择要共享的屏幕：", bg="#0B1220", fg="#E5E7EB",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(14, 8))
        var = tk.StringVar()
        options = []
        for i, l, t, r, b in self.screens:
            w, h = r - l, b - t
            label = f"屏幕 #{i + 1}  ·  {w}×{h}  ·  ({l},{t})-({r},{b})"
            options.append(label)
        var.set(options[0] if options else "")
        ttk.Combobox(self.win, values=options, textvariable=var, width=50,
                     state="readonly").pack(padx=16, pady=4)
        btns = tk.Frame(self.win, bg="#0B1220")
        btns.pack(fill="x", padx=16, pady=(14, 14))
        ttk.Button(btns, text="取消", command=self._cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="开始共享", style="Accent.TButton",
                   command=lambda v=var: self._confirm(v)).pack(side="right")
        self.win.bind("<Escape>", lambda e: self._cancel())
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

    def _cancel(self):
        self.ok = False
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _confirm(self, var):
        label = var.get()
        try:
            self.index = int(label.split("#")[1].split(" ")[0]) - 1
        except Exception:
            self.index = 0
        self.ok = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass
