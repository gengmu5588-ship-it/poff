# -*- coding: utf-8 -*-
"""POFF V2.0 截屏模块：全屏 / 选区截屏，预览、本地保存、发送到局域网主机。"""
import ctypes
import datetime
import os
import tempfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageGrab, ImageTk
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
        ttk.Label(box, text="截屏后可本地另存，或直接发送到局域网中的其他 POFF 主机。",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 12))

        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x")
        b1 = ttk.Button(row, text="全屏截屏  Ctrl+Shift+F",
                        style="Accent.TButton", command=self.capture_full)
        b1.pack(side="left")
        b2 = ttk.Button(row, text="选区截屏  Ctrl+Shift+S", command=self.capture_region)
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

    # ---------------- 预览 ----------------
    def open_preview(self, img, path):
        if self._preview_win is not None:
            try:
                self._preview_win.destroy()
            except tk.TclError:
                pass
        win = tk.Toplevel(self.app.root)
        self._preview_win = win
        win.title("截屏预览")
        win.configure(bg=BG)
        win.attributes("-topmost", True)

        thumb = img.copy()
        thumb.thumbnail((980, 600))
        photo = ImageTk.PhotoImage(thumb)
        win._photo = photo     # 防止被回收

        holder = tk.Frame(win, bg="#0F172A")
        holder.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        tk.Label(holder, image=photo, bg="#0F172A").pack(padx=6, pady=6)

        info = ttk.Label(win, text=f"已自动保存：{path}", style="Muted.TLabel",
                         wraplength=900)
        info.pack(anchor="w", padx=14)

        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=12, pady=10)
        ttk.Button(bar, text="另存为...", command=lambda: self.save_as(path)).pack(side="left")
        ttk.Button(bar, text="打开目录",
                   command=lambda: self.app.open_path(os.path.dirname(path))).pack(
            side="left", padx=8)

        ttk.Label(bar, text="发送到：").pack(side="left", padx=(18, 4))
        peers = [ip for ip, _ in self.app.peer_items()]
        var_ip = tk.StringVar(value=(self.app.selected_ip() or (peers[0] if peers else "")))
        combo = ttk.Combobox(bar, values=peers, textvariable=var_ip, width=18,
                             state="readonly")
        combo.pack(side="left")
        btn_send = ttk.Button(bar, text="发送", style="Accent.TButton",
                              command=lambda: self._send(path, var_ip.get(), win))
        btn_send.pack(side="left", padx=8)
        if not peers:
            btn_send.config(state="disabled")
            ttk.Label(bar, text="（尚未添加主机）", style="Muted.TLabel").pack(side="left")

        ttk.Button(bar, text="关闭", command=win.destroy).pack(side="right")
        win.bind("<Escape>", lambda e: win.destroy())
        win.protocol("WM_DELETE_WINDOW", win.destroy)

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
