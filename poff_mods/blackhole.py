# -*- coding: utf-8 -*-
"""POFF 黑洞投递：悬浮面板拖拽投递到局域网主机；V2.2 扩展手机投递（扫码互传）。

拖拽依赖 tkinterdnd2；缺失时自动降级为"选择文件投递"。
"""
import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from . import mobidrop as mobi_mod

try:
    from tkinterdnd2 import DND_FILES
except Exception:
    DND_FILES = None

PANEL_W = 252
STRIP_W = 14


class BlackholeModule:
    def __init__(self, app):
        self.app = app
        self.enabled = bool(app.cfg.get("modules", {}).get("blackhole", True))
        self.panel = None
        self.rows_box = None
        self.status_lbl = None
        self.selected_ip = None
        self._expanded = False
        self._animating = False
        self._watch_job = None
        self._drag_offset = 0

    # ---------------- 设置页 ----------------
    def build_tab(self, parent):
        box = ttk.Frame(parent, style="Card.TFrame", padding=16)
        box.pack(fill="both", expand=True)

        ttk.Label(box, text="黑洞悬浮面板", style="H2.TLabel").pack(anchor="w")
        ttk.Label(box, text="把文件拖到侧边悬浮面板里的主机条目上，即可一次性投递到对方接收目录。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(2, 12))

        opts = ttk.Frame(box, style="Card.TFrame")
        opts.pack(fill="x")
        self.var_show = tk.BooleanVar(value=bool(self.app.cfg.get("blackhole", {}).get("show", True)))
        self.var_top = tk.BooleanVar(value=bool(self.app.cfg.get("blackhole", {}).get("topmost", True)))
        self.var_auto = tk.BooleanVar(value=bool(self.app.cfg.get("blackhole", {}).get("auto_hide", True)))
        self.var_ignore = tk.BooleanVar(value=bool(self.app.cfg.get("blackhole", {}).get("ignore_drag", False)))

        ttk.Checkbutton(opts, text="显示悬浮面板", variable=self.var_show,
                        command=self._apply_opts).grid(row=0, column=0, sticky="w", padx=(0, 18))
        ttk.Checkbutton(opts, text="保持置顶", variable=self.var_top,
                        command=self._apply_opts).grid(row=0, column=1, sticky="w", padx=(0, 18))
        ttk.Checkbutton(opts, text="鼠标移出自动收起", variable=self.var_auto,
                        command=self._apply_opts).grid(row=1, column=0, sticky="w", pady=6)
        ttk.Checkbutton(opts, text="忽略拖拽（防误投）", variable=self.var_ignore,
                        command=self._apply_opts).grid(row=1, column=1, sticky="w", pady=6)

        ttk.Separator(box).pack(fill="x", pady=12)

        dir_row = ttk.Frame(box, style="Card.TFrame")
        dir_row.pack(fill="x")
        ttk.Label(dir_row, text="接收目录：", style="Card.TLabel").pack(side="left")
        self.lbl_dir = ttk.Label(dir_row, text=self.app.receive_dir, style="Muted.TLabel")
        self.lbl_dir.pack(side="left")
        ttk.Button(dir_row, text="打开目录",
                   command=lambda: self.app.open_path(self.app.receive_dir)).pack(side="right", padx=4)
        ttk.Button(dir_row, text="更改目录", command=self.change_dir).pack(side="right")

        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x", pady=14)
        ttk.Button(row, text="展开 / 收起悬浮面板",
                   style="Accent.TButton", command=self.toggle_panel).pack(side="left")
        ttk.Button(row, text="选择文件投递...", command=self.pick_and_send).pack(side="left", padx=8)

        ttk.Separator(box).pack(fill="x", pady=12)
        self._build_mobi_section(box)

        ttk.Label(box,
                  text="说明：投递为一次性复制，不做双向同步与冲突处理；"
                       "同名文件自动加 (1) 后缀，不会覆盖原文件。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(6, 0))
        if DND_FILES is None:
            ttk.Label(box, text="当前环境未加载拖拽组件，已自动降级为「选择文件投递」。",
                      style="Warn.TLabel", wraplength=660).pack(anchor="w", pady=(4, 0))

    def change_dir(self):
        path = filedialog.askdirectory(title="选择文件接收目录",
                                       initialdir=self.app.receive_dir)
        if not path:
            return
        self.app.receive_dir = path
        self.app.cfg.setdefault("dirs", {})["receive"] = path
        self.app.save_cfg()
        self.lbl_dir.config(text=path)

    # ---------------- 手机投递（V2.2） ----------------
    def _build_mobi_section(self, box):
        ttk.Label(box, text="手机投递", style="H2.TLabel").pack(anchor="w")
        ttk.Label(box,
                  text="手机浏览器扫码（或输入网址）即可与电脑互传文件，无需安装 App。"
                       "电脑投递给手机的文件进入「待接收区」，手机打开页面点按下载；"
                       "手机上传的文件保存到上方接收目录。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(2, 10))

        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x")
        self.lbl_qr = ttk.Label(row, text="二维码生成中...")
        self.lbl_qr.pack(side="left", padx=(0, 14))

        info = ttk.Frame(row, style="Card.TFrame")
        info.pack(side="left", fill="both", expand=True)
        self.lbl_mobi_token = ttk.Label(info, text="", style="Accent.TLabel")
        self.lbl_mobi_token.pack(anchor="w")
        self.lbl_mobi_url = ttk.Label(info, text="", style="Muted.TLabel", wraplength=430)
        self.lbl_mobi_url.pack(anchor="w", pady=(2, 8))

        btns = ttk.Frame(info, style="Card.TFrame")
        btns.pack(fill="x")
        ttk.Button(btns, text="复制链接",
                   command=self.copy_mobi_url).pack(side="left")
        ttk.Button(btns, text="重新生成配对码",
                   command=self.regen_token).pack(side="left", padx=6)
        ttk.Button(btns, text="添加手机设备",
                   command=self.add_mobile).pack(side="left")

        self.refresh_mobi()

    def refresh_mobi(self):
        if not hasattr(self, "lbl_qr"):
            return
        try:
            url = self.app.mobi.share_url()
        except Exception:
            return
        self.lbl_mobi_url.config(text=url)
        self.lbl_mobi_token.config(text=f"配对码 {self.app.mobi.get_token()}")
        img = mobi_mod.make_qr_image(url)
        if img is not None:
            try:
                from PIL import ImageTk
                self._qr_photo = ImageTk.PhotoImage(img)
                self.lbl_qr.config(image=self._qr_photo, text="")
                return
            except Exception:
                pass
        self.lbl_qr.config(image="", text="二维码不可用\n请输入右侧网址")

    def copy_mobi_url(self):
        url = self.app.mobi.share_url()
        self.app.root.clipboard_clear()
        self.app.root.clipboard_append(url)
        self.app.log("手机投递链接已复制，发到手机浏览器打开即可", "ok")

    def regen_token(self):
        if not messagebox.askyesno(
                "重新生成配对码",
                "旧配对码与二维码将立即失效，已连接的手机需要重新扫码。是否继续？"):
            return
        self.app.mobi.regen_token()
        self.refresh_mobi()
        self.app.log("已重新生成手机投递配对码", "warn")

    def add_mobile(self):
        ip_text = simpledialog.askstring(
            "添加手机设备",
            "输入手机的局域网 IP（手机 Wi-Fi 详情里可查看）：\n例如 192.168.101.66",
            parent=self.app.root)
        if not ip_text:
            return
        ok, err = self.app.add_mobile_peer(ip_text)
        if not ok:
            messagebox.showerror("添加失败", err)

    # ---------------- 悬浮面板 ----------------
    def _screen_geo(self):
        root = self.app.root
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        height = max(360, int(sh * 0.62))
        y = max(80, (sh - height) // 2 - 40)
        return sw, sh, height, y

    def ensure_panel(self):
        if self.panel is not None and self._alive():
            return
        sw, sh, height, y = self._screen_geo()
        panel = tk.Toplevel(self.app.root)
        self.panel = panel
        panel.overrideredirect(True)
        panel.configure(bg="#0B1220")
        if self.app.cfg.get("blackhole", {}).get("topmost", True):
            panel.attributes("-topmost", True)
        panel.geometry(f"{PANEL_W}x{height}+{sw - STRIP_W}+{y}")

        header = tk.Frame(panel, bg="#0B1220", height=46)
        header.pack(fill="x")
        header.pack_propagate(False)
        title = tk.Label(header, text="◍ 黑洞投递", bg="#0B1220", fg="#E2E8F0",
                         font=("Microsoft YaHei UI", 11, "bold"))
        title.pack(side="left", padx=14)
        close = tk.Label(header, text="⟩", bg="#0B1220", fg="#94A3B8",
                         font=("Microsoft YaHei UI", 12, "bold"), cursor="hand2")
        close.pack(side="right", padx=12)
        close.bind("<Button-1>", lambda e: self.collapse(force=True))

        sub = tk.Label(panel, text="拖文件到主机条目即可投递", bg="#0B1220",
                       fg="#64748B", font=("Microsoft YaHei UI", 8))
        sub.pack(anchor="w", padx=14, pady=(0, 6))

        self.rows_box = tk.Frame(panel, bg="#0B1220")
        self.rows_box.pack(fill="both", expand=True, padx=10)

        self.status_lbl = tk.Label(panel, text="", bg="#0B1220", fg="#94A3B8",
                                   font=("Microsoft YaHei UI", 8), wraplength=PANEL_W - 28,
                                   anchor="w", justify="left")
        self.status_lbl.pack(fill="x", padx=14, pady=(4, 12))

        # 面板顶部可拖动调整纵向位置
        for widget in (header, title):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        panel.bind("<Enter>", lambda e: self.expand())
        self.refresh()
        self._watch()

    def _alive(self):
        try:
            return bool(self.panel.winfo_exists())
        except (tk.TclError, AttributeError):
            return False

    def _drag_start(self, event):
        self._drag_offset = event.y_root - self.panel.winfo_y()

    def _drag_move(self, event):
        sw, sh, height, _ = self._screen_geo()
        y = max(0, min(sh - height, event.y_root - self._drag_offset))
        x = self.panel.winfo_x()
        self.panel.geometry(f"{PANEL_W}x{height}+{x}+{y}")

    def expand(self):
        if not self.enabled or not self._alive():
            return
        if self._expanded or self._animating:
            return
        self._slide(to=sw_expanded(self.app.root.winfo_screenwidth()), expand=True)

    def collapse(self, force=False):
        if not self._alive():
            return
        if not force and not self.app.cfg.get("blackhole", {}).get("auto_hide", True):
            return
        if not self._expanded or self._animating:
            return
        self._slide(to=self.app.root.winfo_screenwidth() - STRIP_W, expand=False)

    def _slide(self, to, expand):
        self._animating = True
        start = self.panel.winfo_x()
        steps = 10
        step = (to - start) / steps

        def tick(i=1):
            if not self._alive():
                self._animating = False
                return
            x = int(start + step * i)
            _, _, height, _ = self._screen_geo()
            self.panel.geometry(f"{PANEL_W}x{height}+{x}+{self.panel.winfo_y()}")
            if i < steps:
                self.panel.after(12, lambda: tick(i + 1))
            else:
                self._expanded = expand
                self._animating = False

        tick()

    def toggle_panel(self):
        if not self.enabled:
            self.app.log("黑洞模块未开启", "warn")
            return
        self.ensure_panel()
        if self._expanded:
            self.collapse(force=True)
        else:
            self.expand()

    def show(self):
        if not self.enabled:
            self.app.log("黑洞投递模块未开启，请先在功能总开关中启用", "warn")
            return
        self.ensure_panel()
        self.expand()

    def hide_panel(self):
        """只销毁悬浮面板，不改变模块开关状态（最小化到托盘时使用）。"""
        if self._alive():
            try:
                self.panel.destroy()
            except tk.TclError:
                pass
        self.panel = None
        self._expanded = False
        self._animating = False

    def set_enabled(self, on):
        self.enabled = bool(on)
        if self.enabled:
            if self.app.cfg.get("blackhole", {}).get("show", True):
                self.ensure_panel()
        else:
            self.hide_panel()

    def _apply_opts(self):
        bh = self.app.cfg.setdefault("blackhole", {})
        bh["show"] = bool(self.var_show.get())
        bh["topmost"] = bool(self.var_top.get())
        bh["auto_hide"] = bool(self.var_auto.get())
        bh["ignore_drag"] = bool(self.var_ignore.get())
        self.app.save_cfg()
        if not self.enabled:
            return
        if self.var_show.get():
            self.ensure_panel()
            if self._alive():
                self.panel.attributes("-topmost", bool(self.var_top.get()))
            self.refresh()
        else:
            self.hide_panel()

    def _watch(self):
        """轮询指针位置实现展开/收起，避免 Enter/Leave 在子控件间抖动。"""
        if not self._alive():
            return
        try:
            px, py = self.app.root.winfo_pointerxy()
            panel_x = self.panel.winfo_x()
            panel_y = self.panel.winfo_y()
            panel_h = self.panel.winfo_height()
            over = (px >= panel_x and py >= panel_y and py <= panel_y + panel_h)
            auto = self.app.cfg.get("blackhole", {}).get("auto_hide", True)
            if over and not self._expanded:
                self.expand()
            elif (not over) and self._expanded and auto and not self._animating:
                sw = self.app.root.winfo_screenwidth()
                if px < sw - STRIP_W - 4:
                    self.collapse()
        except tk.TclError:
            return
        self._watch_job = self.panel.after(320, self._watch)

    # ---------------- 设备条目 ----------------
    def refresh(self):
        if not self._alive() or self.rows_box is None:
            return
        for child in self.rows_box.winfo_children():
            child.destroy()

        peers = self.app.peer_items()
        if not peers:
            tk.Label(self.rows_box, text="还没有配对主机\n请先在主界面添加串码",
                     bg="#0B1220", fg="#64748B", justify="left",
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=6, pady=12)
            return

        for ip, info in peers:
            online = bool(info.get("ok"))
            is_mobi = info.get("kind") == "mobile"
            row = tk.Frame(self.rows_box, bg="#111C2E", highlightthickness=1,
                           highlightbackground="#1E293B")
            row.pack(fill="x", pady=4)

            dot = tk.Label(row, text="●", bg="#111C2E",
                           fg=("#22C55E" if online else "#64748B"),
                           font=("Microsoft YaHei UI", 10))
            dot.pack(side="left", padx=(10, 4), pady=8)
            name = tk.Label(row, text=("📱 " + ip) if is_mobi else ip,
                            bg="#111C2E", fg="#E2E8F0",
                            font=("Consolas", 10), anchor="w")
            name.pack(side="left", fill="x", expand=True)
            hint = tk.Label(row, text=("拖入📱" if is_mobi else "拖入"),
                            bg="#111C2E", fg="#475569",
                            font=("Microsoft YaHei UI", 8))
            hint.pack(side="right", padx=8)

            widgets = (row, dot, name, hint)

            def on_click(_e, target=ip):
                self.selected_ip = target
                self.app.log(f"黑洞面板已选中 {target}", "info")

            for w in widgets:
                w.bind("<Button-1>", on_click)

            if DND_FILES and not self.app.cfg.get("blackhole", {}).get("ignore_drag", False):
                try:
                    # TkDnD 只在注册过的控件上触发 Drop；行内的主机名等
                    # 子控件会遮住 row，必须一并注册，否则拖到文字上无反应
                    targets = [row] + list(row.winfo_children())
                    for t in targets:
                        t.drop_target_register(DND_FILES)
                    row.dnd_bind("<<DropEnter>>", lambda e, r=row: self._hl(r, True))
                    row.dnd_bind("<<DropLeave>>", lambda e, r=row: self._hl(r, False))
                    for t in targets:
                        t.dnd_bind("<<Drop>>",
                                   lambda e, target=ip, r=row: self._on_drop(e, target, r))
                except Exception:
                    pass

    def _hl(self, row, on):
        try:
            row.configure(highlightbackground=("#2F6BFF" if on else "#1E293B"),
                          highlightthickness=(2 if on else 1))
            if on:
                row.configure(bg="#1B2A44")
                for c in row.winfo_children():
                    c.configure(bg="#1B2A44")
            else:
                row.configure(bg="#111C2E")
                for c in row.winfo_children():
                    c.configure(bg="#111C2E")
        except tk.TclError:
            pass

    def _on_drop(self, event, ip, row=None):
        self._hl(row if row is not None else event.widget, False)
        if self.app.cfg.get("blackhole", {}).get("ignore_drag", False):
            self._set_status("已开启「忽略拖拽」，请关闭后再投递")
            return
        try:
            files = self.app.root.tk.splitlist(event.data)
        except Exception:
            files = [event.data]
        files = [f for f in files if os.path.exists(f)]
        if not files:
            self._set_status("拖入的内容不是有效文件")
            return
        dirs = [f for f in files if os.path.isdir(f)]
        if dirs:
            self._set_status("V2.0 暂不支持文件夹投递（V2.1 提供）")
        files = [f for f in files if os.path.isfile(f)]
        if files:
            self._send(ip, files)

    def pick_and_send(self):
        ip = self.selected_ip or self.app.selected_ip()
        if not ip:
            self.app.log("请先选中一台主机（左侧列表或悬浮面板）", "warn")
            return
        files = filedialog.askopenfilenames(title=f"选择要发送到 {ip} 的文件")
        if files:
            self._send(ip, list(files))

    def _send(self, ip, files):
        total = len(files)
        self._set_status(f"正在投递 0/{total} ...")
        self.app.log(f"开始投递 {total} 个文件到 {ip}", "info")

        def on_done(ok, msg):
            self._set_status(("投递完成" if ok else f"投递失败：{msg}"))
            self.app.log(f"黑洞投递到 {ip}：{'完成' if ok else '失败 - ' + msg}",
                         "ok" if ok else "err")

        def on_progress(path, sent, size):
            pct = int(sent * 100 / size) if size else 100
            self._set_status(f"{os.path.basename(path)}  {pct}%")

        self.app.send_files_async(files, ip, kind="file", on_done=on_done,
                                  on_progress=on_progress)

    def _set_status(self, text):
        if self._alive() and self.status_lbl is not None:
            try:
                self.status_lbl.config(text=text)
            except tk.TclError:
                pass


def sw_expanded(screen_width):
    return screen_width - PANEL_W
