# -*- coding: utf-8 -*-
"""POFF 2.4.12 侧栏「本机网络」实时监测：上下行带宽曲线 + 网关 ping。

刻意不引入 psutil —— 只为两个数字给 exe 再塞一个二进制依赖不划算，
这里直接走系统 API：

  · 带宽：iphlpapi!GetBestInterface 找到当前默认路由使用的网卡，
    再用 GetIfEntry2 读它的 64 位 InOctets / OutOctets，按 1s 差分。
    必须只取这一块网卡：Windows 上每块网卡下面会挂一串 NDIS 过滤驱动
    （WFP / QoS Packet Scheduler / Native WiFi Filter…），GetIfTable2
    里它们各自是一行、计数完全相同，全部求和会把速率放大 6 倍。
  · 延迟：IcmpSendEcho 打默认网关（IPv4），失败时用系统 ping 兜底解析。
"""

import ctypes
import socket
import struct
import threading
import time
import tkinter as tk
from collections import deque

IF_OPER_STATUS_UP = 1
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131
IF_MAX_STRING_SIZE = 256


class _MIB_IF_ROW2(ctypes.Structure):
    '''Windows SDK MIB_IF_ROW2（x64 下 sizeof=1352，InOctets 在 1208）。'''

    _fields_ = [
        ('InterfaceLuid', ctypes.c_uint64),
        ('InterfaceIndex', ctypes.c_uint32),
        ('InterfaceGuid', ctypes.c_byte * 16),
        ('Alias', ctypes.c_wchar * (IF_MAX_STRING_SIZE + 1)),
        ('Description', ctypes.c_wchar * (IF_MAX_STRING_SIZE + 1)),
        ('PhysicalAddressLength', ctypes.c_uint32),
        ('PhysicalAddress', ctypes.c_ubyte * 32),
        ('PermanentPhysicalAddress', ctypes.c_ubyte * 32),
        ('Mtu', ctypes.c_uint32),
        ('Type', ctypes.c_uint32),
        ('TunnelType', ctypes.c_uint32),
        ('MediaType', ctypes.c_uint32),
        ('PhysicalMediumType', ctypes.c_uint32),
        ('AccessType', ctypes.c_uint32),
        ('DirectionType', ctypes.c_uint32),
        ('InterfaceAndOperStatusFlags', ctypes.c_ubyte),
        ('OperStatus', ctypes.c_uint32),
        ('AdminStatus', ctypes.c_uint32),
        ('MediaConnectState', ctypes.c_uint32),
        ('NetworkGuid', ctypes.c_byte * 16),
        ('ConnectionType', ctypes.c_uint32),
        ('TransmitLinkSpeed', ctypes.c_uint64),
        ('ReceiveLinkSpeed', ctypes.c_uint64),
        ('InOctets', ctypes.c_uint64),
        ('OutOctets', ctypes.c_uint64),
        ('InUcastPkts', ctypes.c_uint64),
        ('OutUcastPkts', ctypes.c_uint64),
        ('InNUcastPkts', ctypes.c_uint64),
        ('OutNUcastPkts', ctypes.c_uint64),
        ('InDiscards', ctypes.c_uint64),
        ('OutDiscards', ctypes.c_uint64),
        ('InErrors', ctypes.c_uint64),
        ('OutErrors', ctypes.c_uint64),
        ('InUnknownProtos', ctypes.c_uint64),
        ('InUcastOctets', ctypes.c_uint64),
        ('OutUcastOctets', ctypes.c_uint64),
        ('InMulticastOctets', ctypes.c_uint64),
        ('OutMulticastOctets', ctypes.c_uint64),
        ('InBroadcastOctets', ctypes.c_uint64),
        ('OutBroadcastOctets', ctypes.c_uint64),
        ('OutQLen', ctypes.c_uint64),
    ]


class _MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [
        ('NumEntries', ctypes.c_ulong),
        ('Table', _MIB_IF_ROW2 * 1),
    ]


class _MIB_IPFORWARDROW(ctypes.Structure):
    _fields_ = [
        ('dwForwardDest', ctypes.c_ulong),
        ('dwForwardMask', ctypes.c_ulong),
        ('dwForwardPolicy', ctypes.c_ulong),
        ('dwForwardNextHop', ctypes.c_ulong),
        ('dwForwardIfIndex', ctypes.c_ulong),
        ('dwForwardType', ctypes.c_ulong),
        ('dwForwardProto', ctypes.c_ulong),
        ('dwForwardAge', ctypes.c_ulong),
        ('dwForwardNextHopAS', ctypes.c_ulong),
        ('dwForwardMetric1', ctypes.c_ulong),
        ('dwForwardMetric2', ctypes.c_ulong),
        ('dwForwardMetric3', ctypes.c_ulong),
        ('dwForwardMetric4', ctypes.c_ulong),
        ('dwForwardMetric5', ctypes.c_ulong),
    ]


class _MIB_IPFORWARDTABLE(ctypes.Structure):
    _fields_ = [
        ('dwNumEntries', ctypes.c_ulong),
        ('table', _MIB_IPFORWARDROW * 1),
    ]


class _IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [
        ('Ttl', ctypes.c_ubyte),
        ('Tos', ctypes.c_ubyte),
        ('Flags', ctypes.c_ubyte),
        ('OptionsSize', ctypes.c_ubyte),
        ('OptionsData', ctypes.c_void_p),
    ]


class _ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [
        ('Address', ctypes.c_ulong),
        ('Status', ctypes.c_ulong),
        ('RoundTripTime', ctypes.c_ulong),
        ('DataSize', ctypes.c_ushort),
        ('Reserved', ctypes.c_ushort),
        ('Data', ctypes.c_void_p),
        ('Options', _IP_OPTION_INFORMATION),
    ]


def _ip_to_u32(text):
    return struct.unpack('<L', socket.inet_aton(text))[0]


def _u32_to_ip(value):
    return socket.inet_ntoa(struct.pack('<L', value & 0xFFFFFFFF))


def _fmt_rate(value):
    '''字节/秒 → 便于一眼比较的单位。'''

    if value < 1024:
        return f'{value:.0f} B/s'
    if value < 1024 * 1024:
        return f'{value / 1024:.1f} KB/s'
    return f'{value / (1024 * 1024):.2f} MB/s'


class _NetApi:
    '''iphlpapi 的最小封装，所有取数都在后台线程里调。'''

    def __init__(self):
        self.ok = False
        self._icmp_handle = None
        try:
            self._api = ctypes.WinDLL('iphlpapi')
            self._api.GetBestInterface.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
            self._api.GetBestInterface.restype = ctypes.c_ulong
            self._api.GetIfEntry2.argtypes = [ctypes.POINTER(_MIB_IF_ROW2)]
            self._api.GetIfEntry2.restype = ctypes.c_ulong
            self._api.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.POINTER(_MIB_IF_TABLE2))]
            self._api.GetIfTable2.restype = ctypes.c_ulong
            self._api.FreeMibTable.argtypes = [ctypes.c_void_p]
            self._api.GetIpForwardTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]
            self._api.GetIpForwardTable.restype = ctypes.c_ulong
            self._api.IcmpCreateFile.restype = ctypes.c_void_p
            self._api.IcmpSendEcho.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p,
                                               ctypes.c_ushort, ctypes.c_void_p, ctypes.c_void_p,
                                               ctypes.c_ulong, ctypes.c_ulong]
            self._api.IcmpSendEcho.restype = ctypes.c_ulong
            self._api.IcmpCloseHandle.argtypes = [ctypes.c_void_p]
            self.ok = True
        except Exception:
            self.ok = False
        self._ifindex = 0
        self._ifindex_at = 0.0
        self._ifalias = ''
        self._gateway = None
        self._gateway_at = 0.0

    # ---------------- 网卡 ----------------

    def _default_ifindex(self):
        now = time.time()
        if self._ifindex and now - self._ifindex_at < 30:
            return self._ifindex
        idx = ctypes.c_ulong(0)
        try:
            if self._api.GetBestInterface(_ip_to_u32('223.5.5.5'), ctypes.byref(idx)) != 0:
                idx.value = 0
        except Exception:
            idx.value = 0
        if idx.value and self._read_row(idx.value) is None:
            idx.value = 0
        if not idx.value:
            idx.value = self._busiest_ifindex()
        self._ifindex, self._ifindex_at = idx.value, now
        return idx.value

    def _read_row(self, index):
        row = _MIB_IF_ROW2()
        row.InterfaceIndex = int(index)
        try:
            if self._api.GetIfEntry2(ctypes.byref(row)) != 0:
                return None
        except Exception:
            return None
        if row.OperStatus != IF_OPER_STATUS_UP:
            return None
        if row.Type in (IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL):
            return None
        self._ifalias = str(row.Alias or '')
        return row

    def _busiest_ifindex(self):
        '''兜底：没有默认路由时，取累计流量最大的那块已连接网卡。'''

        if not self.ok:
            return 0
        table = ctypes.POINTER(_MIB_IF_TABLE2)()
        try:
            if self._api.GetIfTable2(ctypes.byref(table)) != 0:
                return 0
        except Exception:
            return 0
        best, best_bytes = 0, -1
        try:
            base = ctypes.cast(table, ctypes.c_void_p).value + _MIB_IF_TABLE2.Table.offset
            rows = ctypes.cast(ctypes.c_void_p(base), ctypes.POINTER(_MIB_IF_ROW2))
            for i in range(table.contents.NumEntries):
                row = rows[i]
                if row.OperStatus != IF_OPER_STATUS_UP:
                    continue
                if row.Type in (IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL):
                    continue
                total = row.InOctets + row.OutOctets
                if total > best_bytes:
                    best, best_bytes = int(row.InterfaceIndex), total
        finally:
            try:
                self._api.FreeMibTable(ctypes.cast(table, ctypes.c_void_p))
            except Exception:
                pass
        return best

    def counters(self):
        '''(收到字节, 发出字节, 网卡名)；失败返回 None。'''

        if not self.ok:
            return None
        index = self._default_ifindex()
        if not index:
            return None
        row = self._read_row(index)
        if row is None:
            self._ifindex = 0
            return None
        return row.InOctets, row.OutOctets, self._ifalias

    # ---------------- 网关 / ping ----------------

    def default_gateway(self):
        now = time.time()
        if self._gateway and now - self._gateway_at < 60:
            return self._gateway
        gw = None
        try:
            size = ctypes.c_ulong(0)
            self._api.GetIpForwardTable(None, ctypes.byref(size), 0)
            if size.value:
                buf = ctypes.create_string_buffer(size.value)
                if self._api.GetIpForwardTable(buf, ctypes.byref(size), 0) == 0:
                    count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong)).contents.value
                    base = ctypes.addressof(buf) + _MIB_IPFORWARDTABLE.table.offset
                    rows = ctypes.cast(ctypes.c_void_p(base), ctypes.POINTER(_MIB_IPFORWARDROW))
                    for i in range(count):
                        row = rows[i]
                        if row.dwForwardDest == 0 and row.dwForwardMask == 0 and row.dwForwardNextHop:
                            gw = _u32_to_ip(row.dwForwardNextHop)
                            break
        except Exception:
            gw = None
        self._gateway, self._gateway_at = gw, now
        return gw

    def ping(self, host, timeout_ms=900):
        '''ICMP 往返毫秒；超时/失败返回 None。'''

        if not (self.ok and host):
            return None
        try:
            if self._icmp_handle is None:
                self._icmp_handle = self._api.IcmpCreateFile()
                if not self._icmp_handle:
                    self._icmp_handle = None
                    return None
            payload = b'poff-net-monitor'
            reply = ctypes.create_string_buffer(ctypes.sizeof(_ICMP_ECHO_REPLY) + len(payload) + 8)
            n = self._api.IcmpSendEcho(self._icmp_handle, _ip_to_u32(host), payload, len(payload),
                                       None, reply, len(reply), timeout_ms)
            if n <= 0:
                return None
            echo = ctypes.cast(reply, ctypes.POINTER(_ICMP_ECHO_REPLY)).contents
            if echo.Status != 0:
                return None
            return int(echo.RoundTripTime)
        except Exception:
            return None

    def close(self):
        try:
            if self._icmp_handle:
                self._api.IcmpCloseHandle(self._icmp_handle)
        except Exception:
            pass
        self._icmp_handle = None


class NetMonitor:
    '''侧栏底部：↑/↓ 实时速率 + 网关 ping，60 秒滚动曲线。'''

    SPAN = 60          # 曲线保留的采样点（1 秒一点）
    TICK = 1.0         # 采样间隔
    FLOOR = 8 * 1024  # 曲线纵轴下限，避免空闲时把几十字节的抖动拉满屏

    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.api = _NetApi()
        self.up = deque([0.0] * self.SPAN, maxlen=self.SPAN)
        self.down = deque([0.0] * self.SPAN, maxlen=self.SPAN)
        self.rate_up = 0.0
        self.rate_down = 0.0
        self.rtt = None
        self.ping_target = None
        self._fail = 0
        self.nic = ''
        self._last = None
        self._last_at = 0.0
        self._stop = threading.Event()
        self._box = None
        self._canvas = None

    # ---------------- 构建 ----------------

    def build(self):
        from poff import HEADER_BG, MUTED, FONT_CAP

        box = tk.Frame(self.parent, bg=HEADER_BG)
        # 直接贴在侧栏底色上，不套圆角卡片：这里是常驻看板，
        # 再包一层卡片会和左侧导航的扁平视觉打架。
        box.pack(side='bottom', fill='x', padx=14, pady=(0, 8))
        self._box = box

        head = tk.Frame(box, bg=HEADER_BG)
        head.pack(fill='x')
        self._lbl_title = tk.Label(head, text='本机网络', bg=HEADER_BG, fg=MUTED,
                                   font=('Microsoft YaHei UI', 8, 'bold'))
        self._lbl_title.pack(side='left')
        self._lbl_nic = tk.Label(head, text='--', bg=HEADER_BG, fg=MUTED, font=FONT_CAP)
        self._lbl_nic.pack(side='right')

        vals = tk.Frame(box, bg=HEADER_BG)
        vals.pack(fill='x', pady=(6, 0))
        vals.columnconfigure(1, weight=1)
        mono = ('Consolas', 9, 'bold')
        self._lbl_arrow_up = tk.Label(vals, text='↑ 上行', bg=HEADER_BG, fg=MUTED,
                                      font=('Microsoft YaHei UI', 8))
        self._lbl_arrow_up.grid(row=0, column=0, sticky='w')
        self._lbl_up = tk.Label(vals, text='--', bg=HEADER_BG, fg=MUTED, font=mono, anchor='e')
        self._lbl_up.grid(row=0, column=1, sticky='e', padx=(8, 0))
        self._lbl_arrow_down = tk.Label(vals, text='↓ 下行', bg=HEADER_BG, fg=MUTED,
                                        font=('Microsoft YaHei UI', 8))
        self._lbl_arrow_down.grid(row=1, column=0, sticky='w')
        self._lbl_down = tk.Label(vals, text='--', bg=HEADER_BG, fg=MUTED, font=mono, anchor='e')
        self._lbl_down.grid(row=1, column=1, sticky='e', padx=(8, 0))

        ping = tk.Frame(box, bg=HEADER_BG)
        ping.pack(fill='x', pady=(2, 0))
        self._lbl_ping = tk.Label(ping, text='PING --', bg=HEADER_BG, fg=MUTED,
                                  font=('Microsoft YaHei UI', 8), anchor='w')
        self._lbl_ping.pack(side='left')
        self._lbl_rtt = tk.Label(ping, text='--', bg=HEADER_BG, fg=MUTED,
                                 font=('Consolas', 9, 'bold'), anchor='e')
        self._lbl_rtt.pack(side='right')

        self._canvas = tk.Canvas(box, height=64, bg=HEADER_BG, highlightthickness=0, bd=0)
        self._canvas.pack(fill='x', pady=(4, 0))
        self._canvas.bind('<Configure>', lambda e: self._draw())
        # 主题切换时 _redraw_all_canvases 会遍历带 _redraw 的控件
        self._canvas._redraw = self._redraw
        self._paint()
        return box

    # ---------------- 采样 ----------------

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        self.api.close()

    def _loop(self):
        tick = 0
        while not self._stop.is_set():
            try:
                sample = self.api.counters()
                now = time.time()
                if sample is not None:
                    in_bytes, out_bytes, alias = sample
                    if alias:
                        self.nic = alias
                    if self._last is not None:
                        dt = max(0.2, now - self._last_at)
                        d_in = in_bytes - self._last[0]
                        d_out = out_bytes - self._last[1]
                        # 网卡重连会让计数清零，负值直接当 0，别画成尖峰
                        self.rate_down = max(0.0, d_in / dt) if d_in >= 0 else 0.0
                        self.rate_up = max(0.0, d_out / dt) if d_out >= 0 else 0.0
                    self._last, self._last_at = (in_bytes, out_bytes), now
                else:
                    self.nic = ''
                self.down.append(self.rate_down)
                self.up.append(self.rate_up)
                if tick % 2 == 0:
                    self._sample_ping()
                self.app.ui_call(self._paint)
            except Exception as e:
                print(f'网络监测采样失败: {e}')
            tick += 1
            self._stop.wait(self.TICK)

    def _sample_ping(self):
        target = self.api.default_gateway()
        if not target:
            # 后台线程里不能碰 Tk 控件，直接读 peer_list 的键
            try:
                peers = list(self.app.peer_list.keys())
            except Exception:
                peers = []
            target = peers[0] if peers else None
        if target != self.ping_target:
            self._fail = 0
        self.ping_target = target
        rtt = self.api.ping(target) if target else None
        if rtt is None:
            # 家用路由偶尔丢一个 ICMP 回复，单次失败不改读数，
            # 连续两次才判定「超时」，否则看板会一秒红一下。
            self._fail += 1
            if self._fail >= 2 or not target:
                self.rtt = None
            return
        self._fail = 0
        self.rtt = rtt if self.rtt is None else int(self.rtt * 0.6 + rtt * 0.4)

    # ---------------- 绘制 ----------------

    def _tokens(self):
        import poff
        return poff.HEADER_BG, poff.MUTED, poff.ACCENT, poff.OK, poff.WARN, poff.ERR, poff.BORDER

    def _paint(self):
        if self._box is None:
            return
        try:
            head_bg, muted, accent, ok, warn, err, _border = self._tokens()
        except Exception:
            return
        try:
            self._lbl_up.config(text=_fmt_rate(self.rate_up))
            self._lbl_down.config(text=_fmt_rate(self.rate_down))
            nic = self.nic or '未连接'
            self._lbl_nic.config(text=nic if len(nic) <= 12 else nic[:11] + '…')
            if self.rtt is None:
                self._lbl_ping.config(text=f'PING · {self.ping_target}' if self.ping_target else 'PING')
                self._lbl_rtt.config(text='超时' if self.ping_target else '--',
                                     fg=err if self.ping_target else muted)
            else:
                self._lbl_ping.config(text=f'PING · {self.ping_target}')
                self._lbl_rtt.config(text=f'{self.rtt} ms',
                                     fg=ok if self.rtt < 50 else (warn if self.rtt < 120 else err))
            self._lbl_up.config(fg=accent if self.rate_up > 0 else muted)
            self._lbl_down.config(fg=ok if self.rate_down > 0 else muted)
            self._lbl_arrow_up.config(fg=accent)
            self._lbl_arrow_down.config(fg=ok)
        except tk.TclError:
            return
        self._draw()

    def _redraw(self):
        '''主题切换后的重绘：底色/文字色都按当前 token 重取。'''

        try:
            head_bg, muted, _a, _o, _w, _e, _b = self._tokens()
        except Exception:
            return
        widgets = [self._box, self._lbl_title, self._lbl_nic, self._lbl_up, self._lbl_down,
                   self._lbl_arrow_up, self._lbl_arrow_down, self._lbl_ping, self._lbl_rtt,
                   self._canvas]
        for w in widgets:
            try:
                w.configure(bg=head_bg)
            except tk.TclError:
                pass
        for w in (self._lbl_title, self._lbl_nic):
            try:
                w.configure(fg=muted)
            except tk.TclError:
                pass
        for frame in (self._box, self._canvas):
            try:
                for child in frame.winfo_children():
                    if isinstance(child, tk.Frame):
                        child.configure(bg=head_bg)
            except tk.TclError:
                pass
        self._paint()

    def _draw(self):
        c = self._canvas
        if c is None:
            return
        try:
            head_bg, muted, accent, ok, _warn, _err, border = self._tokens()
        except Exception:
            return
        try:
            c.configure(bg=head_bg)
            w = max(c.winfo_width(), 40)
            h = max(c.winfo_height(), 24)
            c.delete('all')
            grid = _blend(head_bg, border, 0.45)
            for frac in (0.33, 0.66):
                y = int(h * frac) + 0.5
                c.create_line(0, y, w, y, fill=grid)
            down = list(self.down)
            up = list(self.up)
            peak = max(max(down), max(up), self.FLOOR)
            top = _nice_ceiling(peak)
            pad_r = 42
            span = max(1, self.SPAN - 1)
            plot_w = max(10, w - pad_r)
            step = plot_w / span

            def xy(series):
                out = []
                for i, v in enumerate(series):
                    x = i * step
                    y = h - 3 - (min(v, top) / top) * (h - 9)
                    out.append((x, y))
                return out

            dpts = xy(down)
            upts = xy(up)
            # 曲线下方铺一层淡底，静默时不至于只剩一条横线；
            # 不用 stipple（点阵在 192px 宽的窄图里会变成一串竖虚线）。
            area = [0, h - 1]
            for x, y in dpts:
                area.extend((x, y))
            area.extend((plot_w, h - 1))
            c.create_polygon(*area, fill=_blend(head_bg, ok, 0.22), outline='')
            c.create_line(*[v for p in upts for v in p], fill=accent, width=2, smooth=True)
            c.create_line(*[v for p in dpts for v in p], fill=ok, width=2, smooth=True)
            # 当前值端点
            for (x, y), color in ((dpts[-1], ok), (upts[-1], accent)):
                c.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5, fill=color, outline='')
            # 纵轴量程 + 60s 时间轴，数值直接标在图上
            c.create_text(w - 2, 9, text=_fmt_rate(top), fill=muted, anchor='ne', font=('Consolas', 7))
            c.create_text(w - 2, h - 2, text='60s', fill=muted, anchor='se', font=('Consolas', 7))
        except tk.TclError:
            return


def _blend(hex_a, hex_b, t):
    a = [int(hex_a[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(hex_b[i:i + 2], 16) for i in (1, 3, 5)]
    return '#%02X%02X%02X' % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _nice_ceiling(value):
    '''把纵轴上限抬到 1/2/5×10^n，曲线顶到边而不出框。'''

    if value <= 0:
        return 1.0
    import math
    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for mult in (1, 2, 5, 10):
        if value <= base * mult:
            return base * mult
    return base * 10
