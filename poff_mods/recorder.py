# -*- coding: utf-8 -*-
"""POFF 录屏 / 录音模块。

能力：
  - 选区录屏（支持全屏或拖拽选区，复用截屏区域选择）
  - 可选音频来源：麦克风（sounddevice）/ 系统播放回环（soundcard WASAPI）/ 同时录 / 不录
  - 纯录音（不录屏，仅音频）
  - 录制中显示计时悬浮窗 + 暂停/继续/停止
  - 停止后自动保存到本地，打开发送对话框（可发往其他 POFF 主机）
  - Windows 上可用；其他平台降级提示

技术选型：
  视频：OpenCV VideoWriter + MJPG fourcc（.avi），opencv-python-headless
  音频：sounddevice（麦克风） + soundcard（WASAPI 回环录系统声），16kHz 单声道 WAV
  注：音视频分别保存，本次 MVP 不自动合成 mp4（需要 ffmpeg），后期可加。

线程安全：录屏/录音循环在后台线程，所有 UI 更新通过 root.after 回到主线程。
"""
import datetime
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import cv2                             # opencv-python-headless
    _CV2_OK = True
except Exception:
    _CV2_OK = False

try:
    import numpy as np
    _NP_OK = True
except Exception:
    _NP_OK = False

try:
    import sounddevice as sd
    _SD_OK = True
except Exception:
    _SD_OK = False

try:
    import soundcard as sc
    _SC_OK = True
except Exception:
    _SC_OK = False

try:
    from PIL import ImageGrab
    _PIL_GRAB_OK = True
except Exception:
    _PIL_GRAB_OK = False


class _Status:
    IDLE = "idle"
    PREPARING = "preparing"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPING = "stopping"


class RecorderModule:
    """录屏 / 录音模块，与 ScreenshotModule 同级。"""

    PANEL_W = 280
    PANEL_H = 140

    def __init__(self, app):
        self.app = app
        self.enabled = True
        self.session = None
        self._status = _Status.IDLE
        self._timer_win = None
        self._timer_job = None
        self._start_time = 0.0
        self._paused_sec = 0.0
        self._pause_start = 0.0
        self._pause_total = 0.0
        self._pause_text_var = tk.StringVar(value="")

    def set_enabled(self, on):
        self.enabled = bool(on)
        if not on:
            # 关闭开关时如果正在录制，立即停止
            try:
                if self._status in (_Status.RECORDING, _Status.PAUSED) \
                        or self.session is not None:
                    self._stop()
            except Exception:
                pass

    # ---------------- 面板 UI ----------------
    def build_panel(self, parent):
        box = ttk.LabelFrame(parent, text="录屏 / 录音", padding=(12, 10))
        if not (_CV2_OK and _NP_OK and _PIL_GRAB_OK):
            ttk.Label(box, text="依赖缺失：opencv 或 numpy 不可用，录屏功能禁用。",
                      style="Muted.TLabel", wraplength=440).pack(anchor="w")
            return box

        ttk.Label(box, text="录屏后自动保存到本地，可单独发送视频或音频到其他主机。",
                  style="Muted.TLabel", wraplength=440).pack(anchor="w", pady=(0, 8))

        row1 = ttk.Frame(box)
        row1.pack(fill="x")
        ttk.Button(row1, text="选区录屏", command=self._start_region_video).pack(side="left")
        ttk.Button(row1, text="全屏录屏", command=self._start_full_video).pack(side="left", padx=(8, 0))

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Button(row2, text="纯录音", command=self._start_audio_only).pack(side="left")
        if not (_SD_OK or _SC_OK):
            ttk.Label(row2, text="（音频依赖缺失）", style="Muted.TLabel").pack(side="left", padx=8)

        self.btn_row = row1
        self.btn_row2 = row2
        return box

    # ---------------- 入口 ----------------
    def _start_region_video(self):
        if not self._can_start():
            return
        # 和截屏同样的选区选择方式：全屏遮罩 + 拖拽框
        from poff_mods.screenshot import RegionSelector
        self.app.root.withdraw()
        try:
            sel = RegionSelector(self.app.root)
            self.app.root.wait_window(sel.win)
        finally:
            self.app.root.deiconify()
            self.app.root.lift()

        if not sel.confirmed:
            self.app.log("已取消选区录屏", "info")
            return
        region = sel.final_box   # (left, top, right, bottom)
        self._start_video(region)

    def _start_full_video(self):
        if not self._can_start():
            return
        sw = self.app.root.winfo_screenwidth()
        sh = self.app.root.winfo_screenheight()
        self._start_video((0, 0, sw, sh))

    def _start_audio_only(self):
        if not self._can_start():
            return
        self._start_video(None)      # region=None 表示纯录音

    def _can_start(self):
        if self._status in (_Status.RECORDING, _Status.PAUSED, _Status.PREPARING):
            self.app.log("已经在录制中，请先停止当前录制。", "warn")
            return False
        if not self.app.enabled:
            self.app.log("录屏模块已停用，先在功能开关中打开。", "warn")
            return False
        return True

    # ---------------- 录制核心 ----------------
    def _start_video(self, region):
        """统一录制入口：region=None 表示纯录音，否则为录屏（可选音频）。"""
        audio_cfg = AudioSourceDialog(self.app.root, allow_none=True)
        self.app.root.wait_window(audio_cfg.win)
        if not audio_cfg.ok:
            return

        # 录屏需要视频依赖
        if region is not None and not (_CV2_OK and _NP_OK and _PIL_GRAB_OK):
            messagebox.showerror("依赖缺失", "录屏需要 opencv、numpy 和 Pillow。")
            return
        # 录音需要至少一个音频后端
        if audio_cfg.mic is None and audio_cfg.sysloop is None and region is None:
            messagebox.showwarning("未选择音频来源", "请选择麦克风或系统回环来源。")
            return

        self._status = _Status.PREPARING
        self.app.log("正在初始化录制...", "info")

        try:
            self.session = RecordingSession(self.app, region, audio_cfg)
            self.session.start()
        except Exception as e:
            self._status = _Status.IDLE
            self.app.log(f"启动录制失败：{e}", "err")
            messagebox.showerror("启动失败", str(e))
            return

        self._status = _Status.RECORDING
        self._start_time = time.time()
        self._pause_total = 0.0
        self._pause_start = 0.0
        self._show_timer()

    def _show_timer(self):
        win = tk.Toplevel(self.app.root)
        self._timer_win = win
        win.overrideredirect(True)
        win.configure(bg="#0B1220")
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.92)
        win.resizable(False, False)
        win.attributes("-toolwindow", True)

        frame = tk.Frame(win, bg="#0B1220")
        frame.pack(padx=12, pady=8)

        # 红点 + 计时
        row1 = tk.Frame(frame, bg="#0B1220")
        row1.pack()
        self._dot = tk.Label(row1, text="●", fg="#FF3B30", bg="#0B1220",
                             font=("Arial", 12, "bold"))
        self._dot.pack(side="left")
        self._time_lbl = tk.Label(row1, text="00:00", fg="#FFFFFF", bg="#0B1220",
                                  font=("Consolas", 16, "bold"))
        self._time_lbl.pack(side="left", padx=(6, 0))

        # 按钮行
        row2 = tk.Frame(frame, bg="#0B1220")
        row2.pack(pady=(6, 0))
        self._btn_pause = tk.Label(row2, text="暂停", fg="#E5E7EB", bg="#1E293B",
                                   padx=10, pady=3, cursor="hand2")
        self._btn_pause.pack(side="left", padx=2)
        self._btn_pause.bind("<Button-1>", lambda e: self._toggle_pause())
        self._btn_stop = tk.Label(row2, text="停止", fg="#FFFFFF", bg="#DC2626",
                                  padx=10, pady=3, cursor="hand2")
        self._btn_stop.pack(side="left", padx=2)
        self._btn_stop.bind("<Button-1>", lambda e: self._stop())

        # 贴右上角
        sw = self.app.root.winfo_screenwidth()
        win.update_idletasks()
        w = win.winfo_width() or 160
        h = win.winfo_height() or 70
        win.geometry(f"+{sw - w - 40}+40")

        self._timer_tick()

    def _timer_tick(self):
        if self._timer_win is None:
            return
        if self._status == _Status.RECORDING:
            elapsed = time.time() - self._start_time - self._pause_total
        elif self._status == _Status.PAUSED:
            elapsed = self._pause_total
        else:
            return
        m, s = divmod(int(elapsed), 60)
        h, m = divmod(m, 60)
        self._time_lbl.config(text=f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}")
        self._timer_job = self._timer_win.after(500, self._timer_tick)

    def _toggle_pause(self):
        if self._status == _Status.RECORDING:
            self._status = _Status.PAUSED
            self._pause_start = time.time()
            self._btn_pause.config(text="继续")
            self._dot.config(fg="#FACC15")
            if self.session:
                self.session.pause()
        elif self._status == _Status.PAUSED:
            now = time.time()
            self._pause_total += (now - self._pause_start)
            self._pause_start = 0.0
            self._status = _Status.RECORDING
            self._btn_pause.config(text="暂停")
            self._dot.config(fg="#FF3B30")
            if self.session:
                self.session.resume()

    def _stop(self):
        if self._status not in (_Status.RECORDING, _Status.PAUSED):
            return
        self._status = _Status.STOPPING
        if self._timer_job is not None:
            try:
                self._timer_win.after_cancel(self._timer_job)
            except tk.TclError:
                pass
            self._timer_job = None
        try:
            self._timer_win.destroy()
        except tk.TclError:
            pass
        self._timer_win = None

        video_path, audio_paths = None, []
        if self.session:
            try:
                video_path, audio_paths = self.session.stop()
            except Exception as e:
                self.app.log(f"停止录制异常：{e}", "err")

        self._status = _Status.IDLE
        self.session = None

        if video_path is None and not audio_paths:
            self.app.log("录制已停止，但未生成有效文件。", "warn")
            return

        # 日志 + 打开发送对话框
        parts = []
        if video_path:
            self.app.log(f"视频已保存：{video_path}", "ok")
            parts.append(("视频", video_path))
        for p in audio_paths:
            self.app.log(f"音频已保存：{p}", "ok")
            parts.append(("音频", p))
        self._prompt_send(parts)

    # ---------------- 发送 ----------------
    def _prompt_send(self, parts):
        """弹出对话框：列出所有生成文件，每个可单独发送/另存。"""
        win = tk.Toplevel(self.app.root)
        win.title("录制完成")
        win.configure(bg="#0B1220")
        win.attributes("-topmost", True)
        win.transient(self.app.root)
        win.resizable(False, False)

        tk.Label(win, text="录制已完成，可发送到其他主机或另存。",
                 bg="#0B1220", fg="#E5E7EB",
                 font=("Microsoft YaHei UI", 11)).pack(padx=16, pady=(14, 8))

        frame = tk.Frame(win, bg="#0B1220")
        frame.pack(padx=16, pady=4, fill="x")

        peers = [ip for ip, _ in self.app.peer_items()]
        default = self.app.selected_ip() or (peers[0] if peers else "")

        for i, (label, path) in enumerate(parts):
            row = tk.Frame(frame, bg="#111827")
            row.pack(fill="x", pady=3)
            tk.Label(row, text=f"【{label}】", bg="#111827", fg="#60A5FA",
                     font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 4))
            tk.Label(row, text=os.path.basename(path), bg="#111827", fg="#E5E7EB",
                     font=("Consolas", 9)).pack(side="left")
            ip_var = tk.StringVar(value=default)
            combo = ttk.Combobox(row, values=peers, textvariable=ip_var, width=18,
                                 state="readonly")
            combo.pack(side="right", padx=(4, 4))
            ttk.Button(row, text="发送",
                       command=lambda p=path, v=ip_var, w=win: self._send_file(p, v.get(), w)
                       ).pack(side="right", padx=4, pady=3)
            ttk.Button(row, text="另存",
                       command=lambda p=path: self._save_as(p)
                       ).pack(side="right", padx=4, pady=3)

        close_row = tk.Frame(win, bg="#0B1220")
        close_row.pack(fill="x", padx=16, pady=(10, 14))
        ttk.Button(close_row, text="关闭", command=win.destroy).pack(side="right")
        if not peers:
            tk.Label(close_row, text="（尚未添加主机，无法发送）",
                     bg="#0B1220", fg="#94A3B8",
                     font=("Microsoft YaHei UI", 8)).pack(side="left")

        win.bind("<Escape>", lambda e: win.destroy())
        win.protocol("WM_DELETE_WINDOW", win.destroy)

    def _send_file(self, path, peer_ip, parent):
        if not peer_ip:
            messagebox.showwarning("未选择主机", "请先选择目标主机 IP。")
            return
        self.app.log(f"正在发送 {os.path.basename(path)} → {peer_ip} ...", "info")
        try:
            self.app.http_send_file(peer_ip, path, kind="recording")
            self.app.log(f"发送成功：{os.path.basename(path)} → {peer_ip}", "ok")
            messagebox.showinfo("发送成功", f"已发送到 {peer_ip}。")
        except Exception as e:
            self.app.log(f"发送失败：{e}", "err")
            messagebox.showerror("发送失败", str(e))

    def _save_as(self, path):
        target = filedialog.asksaveasfilename(
            title="另存为", initialdir=os.path.dirname(path),
            initialfile=os.path.basename(path),
            defaultextension=os.path.splitext(path)[1] or ".avi")
        if not target:
            return
        try:
            with open(path, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
            self.app.log(f"已另存：{target}", "ok")
        except OSError as e:
            messagebox.showerror("另存失败", str(e))


class AudioSourceDialog:
    """选择录制音频来源：麦克风 / 系统回环 / 都要 / 不要。

    返回 ok=True 时 mic 和 sysloop 是两个端点标识符（或 None）。
    """

    def __init__(self, root, allow_none=True):
        self.win = tk.Toplevel(root)
        self.win.title("选择音频来源")
        self.win.configure(bg="#0B1220")
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.transient(root)

        self.ok = False
        self.mic = None
        self.sysloop = None

        self._build(root, allow_none)

    def _build(self, root, allow_none):
        # 麦克风列表（sounddevice）
        mics = []
        if _SD_OK:
            try:
                info = sd.query_devices()
                for i, dev in enumerate(info):
                    if dev.get("max_input_channels", 0) > 0:
                        mics.append((i, dev.get("name", f"设备 {i}")))
            except Exception:
                pass

        # 系统播放回环（soundcard WASAPI）
        loops = []
        if _SC_OK:
            try:
                for sp in sc.all_speakers():
                    loops.append((sp, sp.name))
            except Exception:
                pass

        # 标题
        tk.Label(self.win, text="音频来源选择", bg="#0B1220", fg="#E5E7EB",
                 font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(14, 8))

        # 麦克风
        mic_frame = tk.Frame(self.win, bg="#0B1220")
        mic_frame.pack(fill="x", padx=16, pady=4)
        tk.Label(mic_frame, text="麦克风（输入）", bg="#0B1220", fg="#94A3B8",
                 font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w")
        self.mic_var = tk.StringVar(value="（不录）")
        mic_opts = ["（不录）"] + [name for _, name in mics] if mics else ["（不录）"]
        self.mic_combo = ttk.Combobox(mic_frame, values=mic_opts,
                                       textvariable=self.mic_var, width=42, state="readonly")
        self.mic_combo.grid(row=0, column=1, padx=(8, 0))
        self._mic_items = mics

        # 系统播放
        loop_frame = tk.Frame(self.win, bg="#0B1220")
        loop_frame.pack(fill="x", padx=16, pady=4)
        tk.Label(loop_frame, text="系统播放回环", bg="#0B1220", fg="#94A3B8",
                 font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w")
        self.loop_var = tk.StringVar(value="（不录）")
        loop_opts = ["（不录）"] + [name for _, name in loops] if loops else ["（不录）"]
        self.loop_combo = ttk.Combobox(loop_frame, values=loop_opts,
                                        textvariable=self.loop_var, width=42, state="readonly")
        self.loop_combo.grid(row=0, column=1, padx=(8, 0))
        self._loop_items = loops

        if not loops:
            tk.Label(self.win, text="提示：未检测到可用的系统播放回环（需要开启声音设备）。",
                     bg="#0B1220", fg="#64748B",
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=16, pady=(2, 0))
        if not mics:
            tk.Label(self.win, text="提示：未检测到可用的麦克风输入设备。",
                     bg="#0B1220", fg="#64748B",
                     font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=16, pady=(2, 0))

        # 按钮
        btns = tk.Frame(self.win, bg="#0B1220")
        btns.pack(fill="x", padx=16, pady=(14, 14))
        ttk.Button(btns, text="取消", command=self._cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="开始录制", style="Accent.TButton",
                   command=self._confirm).pack(side="right")

        self.win.bind("<Escape>", lambda e: self._cancel())
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)

    def _cancel(self):
        self.ok = False
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _confirm(self):
        # 把下拉显示名映射回实际端点
        mic_name = self.mic_var.get()
        loop_name = self.loop_var.get()
        self.mic = next((i for i, n in self._mic_items if n == mic_name), None)
        self.sysloop = next((s for s, n in self._loop_items if n == loop_name), None)

        if not allow_none and self.mic is None and self.sysloop is None:
            messagebox.showwarning("请选择", "至少选择一个音频来源。", parent=self.win)
            return

        self.ok = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class RecordingSession:
    """单次录制会话：视频线程 + 可选音频线程，停止后返回保存路径。"""

    FPS = 20
    VIDEO_EXT = ".avi"
    AUDIO_SR = 16000          # 16kHz 单声道 WAV
    AUDIO_CH = 1
    AUDIO_SAMPLE_W = 2        # int16

    def __init__(self, app, region, audio_cfg):
        self.app = app
        self.region = region           # (left, top, right, bottom) or None
        self.audio_cfg = audio_cfg     # AudioSourceDialog
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._pause.set()              # set = 正在运行，clear = 暂停
        self._video_th = None
        self._audio_th_mic = None
        self._audio_th_sys = None
        self._out_dir = None
        self._video_path = None
        self._audio_paths = []

    def start(self):
        self._out_dir = self._make_dir()
        if self.region is not None:
            self._video_path = os.path.join(
                self._out_dir, self._ts("视频") + self.VIDEO_EXT)
            self._video_th = threading.Thread(
                target=self._video_loop, name="poff-video", daemon=True)
            self._video_th.start()

        if self.audio_cfg.mic is not None:
            p = os.path.join(self._out_dir, self._ts("麦克风") + ".wav")
            self._audio_paths.append(p)
            self._audio_th_mic = threading.Thread(
                target=self._audio_loop, args=(p, "mic"),
                name="poff-mic", daemon=True)
            self._audio_th_mic.start()

        if self.audio_cfg.sysloop is not None:
            p = os.path.join(self._out_dir, self._ts("系统声音") + ".wav")
            self._audio_paths.append(p)
            self._audio_th_sys = threading.Thread(
                target=self._audio_loop, args=(p, "sys"),
                name="poff-sys", daemon=True)
            self._audio_th_sys.start()

    def pause(self):
        self._pause.clear()

    def resume(self):
        self._pause.set()

    def stop(self):
        self._stop.set()
        if self._video_th:
            self._video_th.join(timeout=3)
        if self._audio_th_mic:
            self._audio_th_mic.join(timeout=3)
        if self._audio_th_sys:
            self._audio_th_sys.join(timeout=3)
        return self._video_path, self._audio_paths

    # ---------------- 视频 ----------------
    def _video_loop(self):
        region = self.region
        if region is not None:
            w = region[2] - region[0]
            h = region[3] - region[1]
        else:
            sw = self.app.root.winfo_screenwidth()
            sh = self.app.root.winfo_screenheight()
            w, h = sw, sh
        # 必须是偶数，MJPG 编码器要求
        w -= w % 2
        h -= h % 2

        try:
            fourcc = cv2.VideoWriter_fourcc(*"MJPG")
            writer = cv2.VideoWriter(self._video_path, fourcc, float(self.FPS), (w, h))
        except Exception as e:
            self.app.log(f"视频初始化失败：{e}", "err")
            return

        frame_interval = 1.0 / self.FPS
        next_frame = time.monotonic()
        try:
            while not self._stop.is_set():
                self._pause.wait()          # 暂停时阻塞在这里
                now = time.monotonic()
                if now < next_frame:
                    time.sleep(max(0, next_frame - now))
                next_frame += frame_interval
                try:
                    if region is None:
                        img = ImageGrab.grab()
                    else:
                        img = ImageGrab.grab(bbox=region)
                    arr = np.array(img)        # RGB
                    frame = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
                    # 尺寸可能因为 bbox 边界奇数差 1，这里强制校正
                    if frame.shape[1] != w or frame.shape[0] != h:
                        frame = cv2.resize(frame, (w, h))
                    writer.write(frame)
                except Exception as e:
                    self.app.log(f"视频帧失败：{e}", "warn")
        finally:
            writer.release()

    # ---------------- 音频 ----------------
    def _audio_loop(self, out_path, kind):
        sample_bytes = self.AUDIO_SAMPLE_W
        channels = self.AUDIO_CH
        sr = self.AUDIO_SR

        # 写 WAV header（用 wave 模块）
        import wave
        wf = wave.open(out_path, "wb")
        wf.setnchannels(channels)
        wf.setsampwidth(sample_bytes)
        wf.setframerate(sr)

        # 后端：mic 用 sounddevice 非阻塞回调；sys 用 soundcard 同步读
        if kind == "mic":
            self._record_mic(wf, sr, channels)
        else:
            self._record_sysloop(wf, sr, channels)

        try:
            wf.close()
        except Exception:
            pass

    def _record_mic(self, wf, sr, channels):
        if not _SD_OK:
            return
        dev_idx = self.audio_cfg.mic
        q = []

        def cb(indata, frames, tm, st):
            # indata shape (frames, in_ch) → 取单声道
            data = indata[:, 0] if indata.ndim == 2 else indata
            q.append(data.copy())

        try:
            with sd.InputStream(samplerate=sr, device=dev_idx, channels=1,
                                dtype="int16", callback=cb,
                                blocksize=int(sr * 0.05)):
                while not self._stop.is_set():
                    self._pause.wait()
                    # 每 200ms flush 一次到 wav
                    time.sleep(0.2)
                    while q:
                        chunk = q.pop(0)
                        wf.writeframes(chunk.astype(np.int16).tobytes())
        except Exception as e:
            self.app.log(f"麦克风录音异常：{e}", "warn")

    def _record_sysloop(self, wf, sr, channels):
        if not _SC_OK or self.audio_cfg.sysloop is None:
            return
        sp = self.audio_cfg.sysloop
        chunk_sec = 0.1
        try:
            with sp.recorder(samplerate=sr, channels=channels) as rec:
                while not self._stop.is_set():
                    self._pause.wait()
                    try:
                        data = rec.record(numframes=int(sr * chunk_sec))
                        # soundcard 返回 float32 [-1,1] → int16
                        if data.ndim == 2 and data.shape[1] > 1:
                            data = data.mean(axis=1)
                        chunk = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
                        wf.writeframes(chunk.tobytes())
                    except Exception:
                        pass
        except Exception as e:
            self.app.log(f"系统回环录音异常：{e}", "warn")

    # ---------------- 辅助 ----------------
    def _make_dir(self):
        base = getattr(self.app, "recording_dir", None)
        if not base:
            base = os.path.join(os.path.expanduser("~"), "PoffRecordings")
        os.makedirs(base, exist_ok=True)
        return base

    def _ts(self, prefix=""):
        t = datetime.datetime.now()
        tag = prefix + "_" if prefix else ""
        return t.strftime(f"{tag}%Y%m%d-%H%M%S")
