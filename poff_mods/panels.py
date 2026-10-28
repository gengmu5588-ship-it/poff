# -*- coding: utf-8 -*-
"""POFF V2.0 亮度控制与平板副屏调度面板。"""
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from . import system_tools as st


class BrightnessModule:
    """亮度：本机内置屏（WMI）+ 外接显示器（DDC/CI，V2.1 动态生成滑块）。"""

    def __init__(self, app):
        self.app = app
        self.enabled = True
        self.supported = None          # None=检测中 / True / False
        self.scale = None
        self.lbl_value = None
        self.lbl_state = None
        self._applying = False
        self.ext_box = None            # 外接显示器滑块容器
        self.lbl_ext_state = None
        self._ddc_rows = []            # [(index, scale, lbl_value)]
        self._ddc_applying = set()     # 正在后台设置的 index

    def build_tab(self, parent):
        box = ttk.Frame(parent, style="Card.TFrame", padding=16)
        box.pack(fill="both", expand=True)

        ttk.Label(box, text="亮度控制", style="H2.TLabel").pack(anchor="w")
        ttk.Label(box, text="支持本机笔记本内置屏（WMI）与外接显示器（DDC/CI）；"
                            "平板亮度由平板系统自行控制。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(2, 14))

        card = ttk.Frame(box, style="Soft.TFrame", padding=12)
        card.pack(fill="x")
        top = ttk.Frame(card, style="Soft.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="本机内置屏", style="Card.TLabel").pack(side="left")
        self.lbl_value = ttk.Label(top, text="--", style="Accent.TLabel")
        self.lbl_value.pack(side="left", padx=8)
        self.lbl_state = ttk.Label(top, text="正在检测设备能力...", style="Muted.TLabel")
        self.lbl_state.pack(side="right")

        self.scale = ttk.Scale(card, from_=0, to=100, orient="horizontal",
                               command=self._on_move)
        self.scale.pack(fill="x", pady=10)
        self.scale.bind("<ButtonRelease-1>", self._on_release)

        ttk.Separator(box).pack(fill="x", pady=16)

        ext = ttk.Frame(box, style="Card.TFrame")
        ext.pack(fill="x")
        ttk.Label(ext, text="外接显示器（DDC/CI）", style="Card.TLabel").pack(anchor="w")
        self.lbl_ext_state = ttk.Label(ext, text="正在检测...", style="Muted.TLabel")
        self.lbl_ext_state.pack(anchor="w", pady=(6, 0))
        self.ext_box = ttk.Frame(ext, style="Card.TFrame")
        self.ext_box.pack(fill="x")

        pad = ttk.Frame(box, style="Card.TFrame")
        pad.pack(fill="x", pady=(14, 0))
        ttk.Label(pad, text="平板无线副屏", style="Card.TLabel").pack(anchor="w")
        row2 = ttk.Frame(pad, style="Card.TFrame")
        row2.pack(fill="x", pady=(6, 0))
        ttk.Scale(row2, from_=0, to=100, state="disabled").pack(
            side="left", fill="x", expand=True)
        ttk.Label(row2, text="平板亮度由平板系统控制（Spacedesk 无开放接口）",
                  style="Muted.TLabel").pack(side="left", padx=8)

        ttk.Label(box,
                  text="说明：外接显示器需在其 OSD 菜单中开启 DDC/CI；"
                       "部分台式机 / 虚拟机没有可调亮度的内置屏，此时滑块会自动置灰。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(18, 0))

        self.scale.config(state="disabled")
        self.app.root.after(120, self.detect)

    def detect(self):
        def task():
            value, err = st.get_brightness()
            monitors = st.list_ddc_monitors()
            self.app.ui_call(self._apply_detected, value, err, monitors)
        threading.Thread(target=task, daemon=True).start()

    def _apply_detected(self, value, err, monitors):
        if value is None:
            self.supported = False
            self.lbl_state.config(text=(err or "设备不支持亮度调节"), style="Warn.TLabel")
            self.lbl_value.config(text="--")
            self.scale.config(state="disabled")
        else:
            self.supported = True
            self.lbl_state.config(text="已连接系统亮度接口", style="Muted.TLabel")
            self._applying = True
            self.scale.set(value)
            self.lbl_value.config(text=f"{int(value)}%")
            self._applying = False
            self.scale.config(state=("normal" if self.enabled else "disabled"))
        self._build_ddc_rows(monitors or [])

    # ---------------- 外接显示器（DDC/CI） ----------------
    def _build_ddc_rows(self, monitors):
        for child in self.ext_box.winfo_children():
            try:
                child.destroy()
            except tk.TclError:
                pass
        self._ddc_rows = []

        if not monitors:
            self.lbl_ext_state.config(
                text="未检测到支持 DDC/CI 的外接显示器", style="Muted.TLabel")
            return
        self.lbl_ext_state.config(
            text=f"检测到 {len(monitors)} 台支持亮度调节", style="Muted.TLabel")

        for mon in monitors:
            row = ttk.Frame(self.ext_box, style="Card.TFrame")
            row.pack(fill="x", pady=(8, 0))
            ttk.Label(row, text=mon["name"], style="Card.TLabel",
                      width=22).pack(side="left")
            lbl_val = ttk.Label(row, text=f"{mon['value']}%", style="Accent.TLabel",
                                width=5)
            lbl_val.pack(side="right", padx=(8, 0))
            scale = ttk.Scale(row, from_=0, to=100, orient="horizontal",
                              state=("normal" if self.enabled else "disabled"))
            scale.set(mon["value"])
            scale.pack(side="left", fill="x", expand=True, padx=(8, 0))
            idx = mon["index"]
            scale.config(command=lambda v, l=lbl_val: self._on_ddc_move(v, l))
            scale.bind("<ButtonRelease-1>",
                       lambda e, i=idx, s=scale: self._on_ddc_release(i, s))
            self._ddc_rows.append((idx, scale, lbl_val))

    def _on_ddc_move(self, value, lbl):
        try:
            lbl.config(text=f"{int(float(value))}%")
        except (tk.TclError, ValueError):
            pass

    def _on_ddc_release(self, index, scale):
        if not self.enabled or index in self._ddc_applying:
            return
        value = int(float(scale.get()))
        self._ddc_applying.add(index)

        def task():
            ok, err = st.set_ddc_brightness(index, value)
            self.app.ui_call(self._after_ddc_set, index, ok, err, value)
        threading.Thread(target=task, daemon=True).start()

    def _after_ddc_set(self, index, ok, err, value):
        self._ddc_applying.discard(index)
        if ok:
            self.app.log(f"外接显示器亮度已调整到 {value}%（DDC/CI）", "ok")
        else:
            self.lbl_ext_state.config(text=(err or "亮度设置失败"), style="Warn.TLabel")
            self.app.log(f"外接显示器亮度调整失败：{err}", "err")

    def _on_move(self, value):
        try:
            self.lbl_value.config(text=f"{int(float(value))}%")
        except (tk.TclError, ValueError):
            pass

    def _on_release(self, _event):
        if not self.supported or not self.enabled or self._applying:
            return
        value = int(float(self.scale.get()))

        def task():
            ok, err = st.set_brightness(value)
            self.app.ui_call(self._after_set, ok, err, value)
        threading.Thread(target=task, daemon=True).start()

    def _after_set(self, ok, err, value):
        if ok:
            self.app.log(f"亮度已调整到 {value}%（本机内屏）", "ok")
        else:
            self.lbl_state.config(text=(err or "亮度接口调用失败"), style="Warn.TLabel")
            self.app.log(f"亮度调整失败：{err}", "err")

    def set_enabled(self, on):
        self.enabled = bool(on)
        if self.scale is not None:
            self.scale.config(state=("normal" if (on and self.supported) else "disabled"))
        for _idx, scale, _lbl in self._ddc_rows:
            try:
                scale.config(state=("normal" if on else "disabled"))
            except tk.TclError:
                pass


class TabletModule:
    """平板无线副屏：只做 Spacedesk 服务调度，不自研画面推流。"""

    def __init__(self, app):
        self.app = app
        self.enabled = True
        self.lbl_status = None
        self.btns = []
        self.service = None

    def build_tab(self, parent):
        box = ttk.Frame(parent, style="Card.TFrame", padding=16)
        box.pack(fill="both", expand=True)

        ttk.Label(box, text="平板无线副屏调度", style="H2.TLabel").pack(anchor="w")
        ttk.Label(box,
                  text="本程序不编码画面，仅作为 Spacedesk 的调度入口。平板端需安装 Spacedesk APP，"
                       "建议使用 5G Wi-Fi（2.4G 延迟明显）。",
                  style="Muted.TLabel", wraplength=660).pack(anchor="w", pady=(2, 14))

        card = ttk.Frame(box, style="Soft.TFrame", padding=12)
        card.pack(fill="x")
        ttk.Label(card, text="Spacedesk 服务状态", style="Card.TLabel").pack(anchor="w")
        self.lbl_status = ttk.Label(card, text="正在检测...", style="Muted.TLabel")
        self.lbl_status.pack(anchor="w", pady=(6, 10))

        row = ttk.Frame(card, style="Soft.TFrame")
        row.pack(fill="x")
        b1 = ttk.Button(row, text="启动服务", command=lambda: self.control("start"))
        b1.pack(side="left")
        b2 = ttk.Button(row, text="停止服务", command=lambda: self.control("stop"))
        b2.pack(side="left", padx=8)
        b3 = ttk.Button(row, text="刷新状态", command=self.refresh)
        b3.pack(side="left")
        b4 = ttk.Button(row, text="打开显示设置",
                        command=lambda: st.open_path("ms-settings:display"))
        b4.pack(side="left", padx=8)
        self.btns = [b1, b2, b3, b4]

        ttk.Separator(box).pack(fill="x", pady=16)
        ttk.Label(box, text="使用步骤", style="H3.TLabel").pack(anchor="w")
        steps = ("1. 在平板安装 Spacedesk Viewer APP，并与本机连接到同一局域网；\n"
                 "2. 本机安装 Spacedesk Driver（Windows 端），点击「启动服务」；\n"
                 "3. 平板上选择本机名称即可扩展为第二块屏幕；\n"
                 "4. 平板亮度与音量由平板系统自行控制（Spacedesk 无开放接口）。")
        ttk.Label(box, text=steps, style="Muted.TLabel", justify="left").pack(
            anchor="w", pady=(6, 0))

        self.admin_hint = ttk.Label(
            box, text="", style="Warn.TLabel", wraplength=660)
        self.admin_hint.pack(anchor="w", pady=(14, 0))
        self._update_admin_hint()

        self.app.root.after(200, self.refresh)

    def _update_admin_hint(self):
        if st.is_admin():
            self.admin_hint.config(text="当前已具备管理员权限，可直接启停服务。",
                                   style="Muted.TLabel")
        else:
            self.admin_hint.config(
                text="启停 Windows 服务需要管理员权限，请点击右上角「以管理员身份重启」。",
                style="Warn.TLabel")

    def refresh(self):
        self.lbl_status.config(text="正在检测...", style="Muted.TLabel")

        def task():
            info = st.find_spacedesk_service()
            self.app.ui_call(self._apply, info)
        threading.Thread(target=task, daemon=True).start()

    def _apply(self, info):
        self.service = info
        if not info:
            self.lbl_status.config(text="未检测到 Spacedesk 服务（驱动未安装）",
                                   style="Warn.TLabel")
            return
        state = "运行中" if info["state"] == "running" else "已停止"
        self.lbl_status.config(text=f"{info['name']}　{state}", style="Card.TLabel")

    def control(self, action):
        name = "启动" if action == "start" else "停止"
        if not st.is_admin():
            messagebox.showwarning("需要管理员权限",
                                   "启停 Spacedesk 服务需要管理员权限。\n"
                                   "请点击右上角「以管理员身份重启」后重试。")
            return

        def task():
            ok, msg = st.control_spacedesk(action)
            self.app.ui_call(self._after_control, ok, msg, name)
        threading.Thread(target=task, daemon=True).start()

    def _after_control(self, ok, msg, name):
        if ok:
            self.app.log(f"Spacedesk 服务{name}成功", "ok")
        else:
            self.app.log(f"Spacedesk 服务{name}失败：{msg}", "err")
            messagebox.showerror("操作失败", msg)
        self.refresh()

    def set_enabled(self, on):
        self.enabled = bool(on)
        for b in self.btns:
            try:
                b.config(state=("normal" if on else "disabled"))
            except tk.TclError:
                pass
