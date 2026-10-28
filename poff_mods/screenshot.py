# -*- coding: utf-8 -*-
"""POFF 截屏模块：全屏 / 选区截屏，预览，标注编辑（画笔/矩形/箭头/马赛克），
复制到剪贴板，本地保存，发送到局域网主机。

编辑始终作用在「已经保存到本地」的图片上，编辑结果直接覆盖该文件，
保持"截完即落地"的既有逻辑不变。
"""
import ctypes
import datetime
import io
import os
import tempfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageDraw, ImageGrab, ImageTk
    _PIL_OK = True
except Exception:
    _PIL_OK = False

BG = "#F2F5FA"
CARD = "#FFFFFF"
TEXT = "#1B2434"
MUTED = "#6B7789"
ACCENT = "#2F6BFF"
ERR = "#DC2626"


class ScreenshotModule:
    def __init__(self, app):
        self.app = app
        self.enabled = True
        self._last_path = None
        self._preview_win = None
        self.buttons = []

    # ---------------- 界面 ----------------
    def build_tab(self, parent):
        box = ttk.Frame(parent, style="Card.TFrame", padding=16)
        box.pack(fill="both", expand=True)

        ttk.Label(box, text="屏幕截屏", style="H2.TLabel").pack(anchor="w")
        ttk.Label(box, text="截屏后自动保存到本地，并打开标注编辑器（画笔 / 矩形 / 箭头 / 马赛克），"
                            "可复制到剪贴板，或直接发送到局域网中的其他 POFF 主机。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(2, 12))

        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x")
        b1 = ttk.Button(row, text="全屏截屏  Ctrl+Shift+F",
                        style="Accent.TButton", command=self.capture_full)
        b1.pack(side="left")
        b2 = ttk.Button(row, text="选区截屏  Alt+Q", command=self.capture_region)
        b2.pack(side="left", padx=8)
        self.buttons = [b1, b2]

        dir_row = ttk.Frame(box, style="Card.TFrame")
        dir_row.pack(fill="x", pady=(14, 4))
        ttk.Label(dir_row, text="截图保存目录：", style="Card.TLabel").pack(side="left")
        self.lbl_dir = ttk.Label(dir_row, text=self.app.screenshot_dir,
                                 style="Muted.TLabel")
        self.lbl_dir.pack(side="left")
        ttk.Button(dir_row, text="打开目录",
                   command=lambda: self.app.open_path(self.app.screenshot_dir)).pack(
            side="right", padx=4)
        ttk.Button(dir_row, text="更改目录", command=self.change_dir).pack(side="right")

        ttk.Separator(box).pack(fill="x", pady=14)
        ttk.Label(box, text="最近一张截图", style="H3.TLabel").pack(anchor="w")
        self.lbl_last = ttk.Label(box, text="还没有截图", style="Muted.TLabel",
                                  wraplength=640)
        self.lbl_last.pack(anchor="w", pady=(4, 8))

        row2 = ttk.Frame(box, style="Card.TFrame")
        row2.pack(fill="x")
        self.btn_send_last = ttk.Button(row2, text="发送到选中主机",
                                        command=self.send_last_to_selected, state="disabled")
        self.btn_send_last.pack(side="left")
        self.btn_reopen = ttk.Button(row2, text="预览", command=self.preview_last,
                                     state="disabled")
        self.btn_reopen.pack(side="left", padx=8)
        self.buttons += [self.btn_send_last, self.btn_reopen]

        ttk.Label(box, text="提示：个别游戏使用独占全屏时截屏可能黑屏；桌面与普通窗口场景正常。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(16, 0))

    def set_enabled(self, on):
        self.enabled = bool(on)
        for btn in self.buttons:
            try:
                btn.config(state=("normal" if on else "disabled"))
            except tk.TclError:
                pass
        if self._last_path:
            try:
                self.btn_send_last.config(state=("normal" if on else "disabled"))
                self.btn_reopen.config(state=("normal" if on else "disabled"))
            except tk.TclError:
                pass

    def change_dir(self):
        path = filedialog.askdirectory(title="选择截图保存目录",
                                       initialdir=self.app.screenshot_dir)
        if not path:
            return
        self.app.screenshot_dir = path
        self.app.cfg.setdefault("dirs", {})["screenshot"] = path
        self.app.save_cfg()
        self.lbl_dir.config(text=path)

    # ---------------- 截屏 ----------------
    def capture_full(self):
        if not self._guard():
            return
        root = self.app.root
        root.withdraw()
        root.after(160, self._do_capture_full)

    def _do_capture_full(self):
        root = self.app.root
        try:
            img = ImageGrab.grab(all_screens=True)
        except Exception as e:
            root.deiconify()
            messagebox.showerror("截屏失败", f"无法获取屏幕画面：{e}")
            return
        root.deiconify()
        path = self._save_temp(img)
        self.app.log(f"全屏截屏完成：{os.path.basename(path)}", "ok")
        self.open_preview(img, path)

    def capture_region(self):
        if not self._guard():
            return
        try:
            full = ImageGrab.grab(all_screens=True)
        except Exception as e:
            messagebox.showerror("截屏失败", f"无法获取屏幕画面：{e}")
            return

        user32 = ctypes.windll.user32
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)

        overlay = tk.Toplevel(self.app.root)
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.geometry(f"{vw}x{vh}+{vx}+{vy}")
        overlay.configure(bg="black")
        try:
            overlay.grab_set()
        except tk.TclError:
            pass

        photo = ImageTk.PhotoImage(full)
        overlay._photo = photo        # 必须持有引用，否则图片会被回收成空白
        canvas = tk.Canvas(overlay, width=vw, height=vh, highlightthickness=0,
                           cursor="crosshair", bg="black")
        canvas.pack(fill="both", expand=True)
        canvas.create_image(0, 0, anchor="nw", image=photo)
        # 半透明点阵遮罩，让选区外的画面变暗（Tk 无真实 alpha，用 stipple 近似）
        canvas.create_rectangle(0, 0, vw, vh, fill="black", outline="",
                                stipple="gray50")
        canvas.create_text(vw // 2, 28,
                           text="拖拽框选截屏区域　·　Esc 取消",
                           fill="#FFFFFF", font=("Microsoft YaHei UI", 13, "bold"))

        state = {"x": 0, "y": 0, "rect": None, "info": None}

        def on_press(e):
            state["x"], state["y"] = e.x, e.y
            if state["rect"]:
                canvas.delete(state["rect"])
            state["rect"] = canvas.create_rectangle(
                e.x, e.y, e.x, e.y, outline="#22D3EE", width=2)
            state["info"] = canvas.create_text(e.x + 6, e.y + 14, anchor="nw",
                                               text="0 × 0", fill="#FFFFFF",
                                               font=("Consolas", 10))

        def on_drag(e):
            if not state["rect"]:
                return
            x0, y0 = state["x"], state["y"]
            canvas.coords(state["rect"], x0, y0, e.x, e.y)
            w, h = abs(e.x - x0), abs(e.y - y0)
            if state["info"]:
                canvas.delete(state["info"])
            state["info"] = canvas.create_text(
                min(x0, e.x) + 6, max(y0, e.y) - 18, anchor="nw",
                text=f"{w} × {h}", fill="#22D3EE", font=("Consolas", 10))

        def finish(e):
            x0, y0 = state["x"], state["y"]
            x1, y1 = e.x, e.y
            if abs(x1 - x0) < 5 or abs(y1 - y0) < 5:
                cleanup()
                return
            # 高 DPI 下窗口坐标与位图坐标可能不同，按比例换算后再裁剪
            sx = full.width / float(vw) if vw else 1.0
            sy = full.height / float(vh) if vh else 1.0
            box = (int(min(x0, x1) * sx), int(min(y0, y1) * sy),
                   int(max(x0, x1) * sx), int(max(y0, y1) * sy))
            box = (max(0, box[0]), max(0, box[1]),
                   min(full.width, box[2]), min(full.height, box[3]))
            if box[2] <= box[0] or box[3] <= box[1]:
                cleanup()
                return
            cropped = full.crop(box)
            cleanup()
            if cropped.size[0] == 0 or cropped.size[1] == 0:
                return
            path = self._save_temp(cropped)
            self.app.log(f"选区截屏完成：{os.path.basename(path)}"
                         f"（{cropped.size[0]}×{cropped.size[1]}）", "ok")
            self.open_preview(cropped, path)

        def cleanup():
            try:
                overlay.grab_release()
            except tk.TclError:
                pass
            try:
                overlay.destroy()
            except tk.TclError:
                pass

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", finish)
        overlay.bind("<Escape>", lambda e: cleanup())
        overlay.focus_force()

    def _guard(self):
        if not _PIL_OK:
            messagebox.showerror("截屏不可用", "缺少 Pillow 图像库，请使用官方打包版本。")
            return False
        if not self.enabled:
            messagebox.showwarning("模块已关闭", "截屏模块未开启，请在功能总开关中启用。")
            return False
        return True

    def _save_temp(self, img):
        """自动保存到截图目录，避免误关预览后丢失画面。"""
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.app.screenshot_dir, f"poff_{stamp}.png")
        try:
            img.save(path, "PNG")
        except OSError:
            path = os.path.join(tempfile.gettempdir(), f"poff_{stamp}.png")
            img.save(path, "PNG")
        self._last_path = path
        try:
            self.lbl_last.config(text=path)
            self.btn_send_last.config(state="normal")
            self.btn_reopen.config(state="normal")
        except (tk.TclError, AttributeError):
            pass
        return path

    # ---------------- 预览 / 编辑 ----------------
    def open_preview(self, img, path):
        if self._preview_win is not None:
            try:
                self._preview_win.destroy()
            except tk.TclError:
                pass
        try:
            editor = ShotEditor(self, path)
        except Exception as e:
            messagebox.showerror("打开失败", f"无法载入截图：{e}")
            return
        self._preview_win = editor.win

    def save_as(self, path):
        target = filedialog.asksaveasfilename(
            title="保存截图", defaultextension=".png",
            initialdir=self.app.screenshot_dir,
            initialfile=os.path.basename(path),
            filetypes=[("PNG 图片", "*.png")])
        if not target:
            return
        try:
            with open(path, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
            self.app.log(f"截图已保存：{target}", "ok")
        except OSError as e:
            messagebox.showerror("保存失败", str(e))

    def _send(self, path, ip, win=None):
        if not ip:
            messagebox.showwarning("提示", "请选择目标主机")
            return
        self.app.send_files_async([path], ip, kind="screenshot",
                                  on_done=lambda ok, msg: self._after_send(ok, msg, win))

    def _after_send(self, ok, msg, win):
        if ok:
            self.app.log(f"截图已发送：{msg}", "ok")
            if win is not None:
                try:
                    win.destroy()
                except tk.TclError:
                    pass
        else:
            messagebox.showerror("发送失败", msg)

    def send_last_to_selected(self):
        ip = self.app.selected_ip()
        if not ip:
            messagebox.showwarning("提示", "请先在左侧设备列表选中一台主机")
            return
        if not self._last_path or not os.path.exists(self._last_path):
            messagebox.showwarning("提示", "还没有可发送的截图")
            return
        self._send(self._last_path, ip)

    def preview_last(self):
        if not self._last_path or not os.path.exists(self._last_path):
            return
        try:
            img = Image.open(self._last_path)
        except Exception:
            return
        self.open_preview(img, self._last_path)

    # ---------------- 远端收到截图 ----------------
    def receive_preview(self, path, remote_ip):
        if self._preview_win is not None:
            try:
                self._preview_win.destroy()
            except tk.TclError:
                pass
        win = tk.Toplevel(self.app.root)
        self._preview_win = win
        win.title(f"来自 {remote_ip} 的截图")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        try:
            img = Image.open(path)
            thumb = img.copy()
            thumb.thumbnail((980, 600))
            photo = ImageTk.PhotoImage(thumb)
            win._photo = photo
            holder = tk.Frame(win, bg="#0F172A")
            holder.pack(fill="both", expand=True, padx=12, pady=(12, 6))
            tk.Label(holder, image=photo, bg="#0F172A").pack(padx=6, pady=6)
        except Exception:
            ttk.Label(win, text="图片无法预览，但文件已保存").pack(padx=16, pady=16)

        ttk.Label(win, text=f"已保存：{path}", style="Muted.TLabel",
                  wraplength=900).pack(anchor="w", padx=14)
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Button(bar, text="打开所在文件夹",
                   command=lambda: self.app.open_path(os.path.dirname(path))).pack(side="left")
        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")


def _copy_image_to_clipboard(img):
    """把 PIL 图片写入 Windows 剪贴板（CF_DIB）。

    走"BMP 序列化 → 去掉 14 字节文件头 → SetClipboardData"，是 Windows 上
    不依赖 pywin32 的最稳做法。
    """
    if os.name != "nt":
        return False
    try:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "BMP")
        data = buf.getvalue()[14:]          # 去掉 BMP 文件头，只留 DIB
        buf.close()

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 64 位下必须显式声明指针返回类型，否则句柄会被截断成 32 位
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

        GMEM_MOVEABLE = 0x0002
        CF_DIB = 8
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return False
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_DIB, handle):
                return False
        finally:
            user32.CloseClipboard()
        return True
    except Exception:
        return False


class ShotEditor:
    """截屏标注编辑器。

    设计要点：
      - 图片在打开时已经保存到本地，所有标注直接写回同一个文件，
        不改变"截完即落地"的原始逻辑；
      - 显示用缩放图，绘制坐标按 scale 换算回原图分辨率，保证清晰度；
      - 每次编辑前把整图压入 undo_stack，Ctrl+Z 可回退（保留 12 步）。
    """

    MAX_W = 1040
    MAX_H = 560
    TOOLS = [("pen", "画笔"), ("rect", "矩形"), ("arrow", "箭头"), ("mosaic", "马赛克")]
    COLORS = ["#FF3B30", "#FF9500", "#FFD60A", "#34C759", "#0A84FF", "#FFFFFF", "#111827"]
    WIDTHS = [("细", 2), ("中", 4), ("粗", 7)]

    def __init__(self, module, path):
        self.module = module
        self.app = module.app
        self.path = path
        self.img = Image.open(path).convert("RGB")
        self.scale = min(1.0, self.MAX_W / self.img.width,
                         self.MAX_H / self.img.height)
        self.dw = max(1, int(self.img.width * self.scale))
        self.dh = max(1, int(self.img.height * self.scale))

        self.tool = "pen"
        self.color = self.COLORS[0]
        self.pen_width = 4
        self.undo_stack = []
        self._press = None
        self._live = []
        self._points = []
        self._photo = None
        self._tool_btns = {}
        self._color_btns = {}
        self._width_btns = {}

        self._build()
        self.set_tool("pen")
        self.set_color(self.COLORS[0])
        self._set_width(self.pen_width)

    # ---------------- 界面 ----------------
    def _build(self):
        win = tk.Toplevel(self.app.root)
        self.win = win
        win.title("截屏编辑")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)

        bar = tk.Frame(win, bg="#0F172A")
        bar.pack(fill="x")

        for key, label in self.TOOLS:
            b = tk.Label(bar, text=label, bg="#1E293B", fg="#CBD5E1", padx=11, pady=5,
                         font=("Microsoft YaHei UI", 9), cursor="hand2")
            b.pack(side="left", padx=(10 if key == "pen" else 4, 0), pady=7)
            b.bind("<Button-1>", lambda e, k=key: self.set_tool(k))
            self._tool_btns[key] = b

        tk.Frame(bar, bg="#334155", width=1).pack(side="left", fill="y", padx=8, pady=9)

        for c in self.COLORS:
            sw = tk.Frame(bar, width=17, height=17, bg=c, cursor="hand2",
                          highlightthickness=2, highlightbackground="#0F172A")
            sw.pack(side="left", padx=3)
            sw.pack_propagate(False)
            sw.bind("<Button-1>", lambda e, col=c: self.set_color(col))
            self._color_btns[c] = sw

        tk.Frame(bar, bg="#334155", width=1).pack(side="left", fill="y", padx=8, pady=9)

        for label, w in self.WIDTHS:
            b = tk.Label(bar, text=label, bg="#1E293B", fg="#CBD5E1", padx=8, pady=5,
                         font=("Microsoft YaHei UI", 8), cursor="hand2")
            b.pack(side="left", padx=2)
            b.bind("<Button-1>", lambda e, width=w: self._set_width(width))
            self._width_btns[w] = b

        tk.Label(bar, text="关闭", bg="#1E293B", fg="#94A3B8", padx=11, pady=5,
                 font=("Microsoft YaHei UI", 9), cursor="hand2").pack(
            side="right", padx=10)
        bar.winfo_children()[-1].bind("<Button-1>", lambda e: self.close())
        for text, cmd in (("另存为", self.save_as), ("复制", self.copy),
                          ("撤销", self.undo)):
            lbl = tk.Label(bar, text=text, bg="#1E293B", fg="#E2E8F0", padx=11, pady=5,
                           font=("Microsoft YaHei UI", 9), cursor="hand2")
            lbl.pack(side="right", padx=3)
            lbl.bind("<Button-1>", lambda e, fn=cmd: fn())

        self.canvas = tk.Canvas(win, width=self.dw, height=self.dh, bg="#0F172A",
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack(padx=12, pady=(10, 4))
        self._render()

        tk.Label(win, text="拖拽绘制　·　Ctrl+Z 撤销　·　Ctrl+C 复制图片　·　Esc 关闭",
                 bg=BG, fg=MUTED, font=("Microsoft YaHei UI", 8)).pack(anchor="w", padx=14)

        bottom = tk.Frame(win, bg=BG)
        bottom.pack(fill="x", padx=12, pady=(2, 10))
        self.lbl_state = tk.Label(bottom, text=self._state_text(), bg=BG, fg=MUTED,
                                  font=("Microsoft YaHei UI", 8), anchor="w")
        self.lbl_state.pack(side="left")

        ttk.Button(bottom, text="打开目录",
                   command=lambda: self.app.open_path(os.path.dirname(self.path))).pack(
            side="right", padx=(6, 0))
        peers = [ip for ip, _ in self.app.peer_items()]
        var_ip = tk.StringVar(value=(self.app.selected_ip() or (peers[0] if peers else "")))
        btn_send = ttk.Button(bottom, text="发送", style="Accent.TButton",
                              command=lambda: self.module._send(self.path, var_ip.get(),
                                                                self.win))
        btn_send.pack(side="right", padx=(6, 0))
        ttk.Combobox(bottom, values=peers, textvariable=var_ip, width=16,
                     state="readonly").pack(side="right", padx=(4, 0))
        tk.Label(bottom, text="发送到：", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 8)).pack(side="right")
        if not peers:
            btn_send.config(state="disabled")

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        win.bind("<Escape>", lambda e: self.close())
        win.bind("<Control-z>", lambda e: self.undo())
        win.bind("<Control-Z>", lambda e: self.undo())
        win.bind("<Control-c>", lambda e: self.copy())
        win.protocol("WM_DELETE_WINDOW", self.close)

    def _state_text(self):
        return (f"已保存：{os.path.basename(self.path)}　"
                f"（{self.img.width}×{self.img.height}）")

    def _refresh_state(self):
        try:
            self.lbl_state.config(text=self._state_text())
        except (tk.TclError, AttributeError):
            pass

    # ---------------- 工具状态 ----------------
    def set_tool(self, key):
        self.tool = key
        for k, btn in self._tool_btns.items():
            if k == key:
                btn.config(bg="#0A84FF", fg="#FFFFFF")
            else:
                btn.config(bg="#1E293B", fg="#CBD5E1")

    def set_color(self, color):
        self.color = color
        for c, sw in self._color_btns.items():
            sw.config(highlightbackground=("#FFFFFF" if c == color else "#0F172A"))

    def _set_width(self, width):
        self.pen_width = width
        for w, btn in self._width_btns.items():
            if w == width:
                btn.config(bg="#0A84FF", fg="#FFFFFF")
            else:
                btn.config(bg="#1E293B", fg="#CBD5E1")

    # ---------------- 绘制 ----------------
    def _to_img(self, x, y):
        return (max(0, min(self.img.width - 1, int(x / self.scale))),
                max(0, min(self.img.height - 1, int(y / self.scale))))

    def _clear_live(self):
        for iid in self._live:
            try:
                self.canvas.delete(iid)
            except tk.TclError:
                pass
        self._live = []

    def _on_press(self, event):
        self._press = (event.x, event.y)
        self._points = [(event.x, event.y)]
        self._clear_live()
        x, y = event.x, event.y
        if self.tool == "pen":
            self._live.append(self.canvas.create_line(
                x, y, x + 1, y + 1, fill=self.color, width=self.pen_width,
                capstyle="round", smooth=True))
        elif self.tool == "rect":
            self._live.append(self.canvas.create_rectangle(
                x, y, x, y, outline=self.color, width=max(2, self.pen_width - 1)))
        elif self.tool == "mosaic":
            self._live.append(self.canvas.create_rectangle(
                x, y, x, y, outline="#0A84FF", width=2, dash=(4, 3)))
        elif self.tool == "arrow":
            self._live.append(self.canvas.create_line(
                x, y, x, y, fill=self.color, width=self.pen_width,
                arrow="last", arrowshape=(13, 15, 6)))

    def _on_drag(self, event):
        if not self._press or not self._live:
            return
        x0, y0 = self._press
        if self.tool == "pen":
            self._points.append((event.x, event.y))
            flat = [c for p in self._points for c in p]
            try:
                self.canvas.coords(self._live[0], *flat)
            except tk.TclError:
                pass
        else:
            try:
                self.canvas.coords(self._live[0], x0, y0, event.x, event.y)
            except tk.TclError:
                pass

    def _on_release(self, event):
        if not self._press:
            return
        x0, y0 = self._press
        x1, y1 = event.x, event.y
        self._press = None
        live, self._live = self._live, []
        for iid in live:
            try:
                self.canvas.delete(iid)
            except tk.TclError:
                pass

        if self.tool == "pen":
            pts = [self._to_img(x, y) for x, y in self._points] + [self._to_img(x1, y1)]
            if len(pts) < 2 or self._dist_sum(pts) < 2:
                return
            width = max(1, int(round(self.pen_width / self.scale)))
            self._apply(lambda d: d.line(pts, fill=self.color, width=width,
                                         joint="curve"))
            return

        if abs(x1 - x0) < 3 and abs(y1 - y0) < 3:
            return                       # 只是点了一下，不产生图元

        box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if self.tool == "rect":
            ix0, iy0 = self._to_img(box[0], box[1])
            ix1, iy1 = self._to_img(box[2], box[3])
            width = max(1, int(round(self.pen_width / self.scale)))
            self._apply(lambda d, a=(ix0, iy0, ix1, iy1), w=width:
                        d.rectangle(a, outline=self.color, width=w))
        elif self.tool == "mosaic":
            ix0, iy0 = self._to_img(box[0], box[1])
            ix1, iy1 = self._to_img(box[2], box[3])
            self._apply(lambda _d: self._mosaic((ix0, iy0, ix1, iy1)))
        elif self.tool == "arrow":
            p0, p1 = self._to_img(x0, y0), self._to_img(x1, y1)
            width = max(2, int(round(self.pen_width / self.scale)))
            self._apply(lambda d, a=p0, b=p1, w=width: self._arrow(d, a, b, w))

    @staticmethod
    def _dist_sum(pts):
        total = 0.0
        for i in range(1, len(pts)):
            dx = pts[i][0] - pts[i - 1][0]
            dy = pts[i][1] - pts[i - 1][1]
            total += (dx * dx + dy * dy) ** 0.5
        return total

    def _arrow(self, draw, p0, p1, width):
        draw.line([p0, p1], fill=self.color, width=width)
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        length = max(1.0, (dx * dx + dy * dy) ** 0.5)
        ux, uy = dx / length, dy / length
        head = max(8, width * 4)
        px, py = -uy, ux                    # 垂直方向
        left = (p1[0] - ux * head + px * head * 0.5,
                p1[1] - uy * head + py * head * 0.5)
        right = (p1[0] - ux * head - px * head * 0.5,
                 p1[1] - uy * head - py * head * 0.5)
        draw.polygon([p1, left, right], fill=self.color)

    def _mosaic(self, box):
        left, top, right, bottom = box
        w, h = right - left, bottom - top
        if w < 2 or h < 2:
            return
        block = max(4, min(w, h) // 12)
        small = self.img.crop((left, top, right, bottom)).resize(
            (max(1, w // block), max(1, h // block)), Image.NEAREST)
        self.img.paste(small.resize((w, h), Image.NEAREST), (left, top))

    # ---------------- 应用 / 保存 ----------------
    def _apply(self, draw_fn):
        """把一次编辑应用到图片（先压栈，再绘制，再写盘刷新）。"""
        self.undo_stack.append(self.img.copy())
        if len(self.undo_stack) > 12:
            self.undo_stack.pop(0)
        try:
            draw_fn(ImageDraw.Draw(self.img))
        except Exception as e:
            self.app.log(f"标注绘制失败：{e}", "err")
            return
        self._save()
        self._render()
        self._refresh_state()

    def _save(self):
        try:
            self.img.save(self.path, "PNG")
        except OSError as e:
            messagebox.showerror("保存失败", f"标注内容写回文件失败：{e}")

    def _render(self):
        try:
            thumb = self.img.resize((self.dw, self.dh), Image.LANCZOS)
        except Exception:
            thumb = self.img.copy()
        self._photo = ImageTk.PhotoImage(thumb)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    # ---------------- 操作 ----------------
    def undo(self):
        if not self.undo_stack:
            self.app.log("没有可撤销的标注", "info")
            return
        self.img = self.undo_stack.pop()
        self._save()
        self._render()
        self._refresh_state()

    def copy(self):
        if _copy_image_to_clipboard(self.img):
            self.app.log("截图已复制到剪贴板（含标注）", "ok")
        else:
            messagebox.showerror("复制失败", "写入剪贴板失败，可能被其他程序占用。")

    def save_as(self):
        self.module.save_as(self.path)

    def close(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.module._preview_win = None
