# -*- coding: utf-8 -*-
"""POFF V2.2 手机投递（MobiDrop）：内置 HTTP 服务，手机浏览器扫码即用，零安装。

双向投递：
  PC → 手机：投递到手机条目的文件先复制到「待接收区」（mobi_outbox），
            手机打开投递页点按卡片下载，下载完成后自动清理。
  手机 → PC：投递页选择文件上传，落盘到黑洞接收目录，电脑弹窗提示。

HTTP 接口（端口 17891，除 /ping 外均需配对码）：
  GET  /ping                → "POFF-MOBI"（在线探测，无需配对码）
  GET  /?t=配对码            → 手机投递页（单页 HTML，手机适配）
  GET  /files?t=配对码       → 待接收列表 JSON
  GET  /dl?t=配对码&name=x   → 下载待接收文件（完成后自动清理）
  POST /upload?t=配对码     → 手机上传文件（multipart 流式解析，单文件上限 2GB）

安全：仅校验配对码的会话可访问；文件名过滤；下载仅限待接收区内的文件。
"""
import hmac
import json
import os
import random
import re
import shutil
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import system_tools as st

MOBI_PORT = 17891
MOBI_PING_TAG = "POFF-MOBI"
MAX_UPLOAD = 2 * 1024 * 1024 * 1024        # 单个上传文件上限 2GB
LAST_SEEN_TTL = 120                        # 手机页面在此秒数内活跃视为在线

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>POFF 手机投递</title>
<style>
  :root { --bg:#0B1220; --card:#111C2E; --line:#1E293B; --txt:#E2E8F0;
          --mut:#64748B; --acc:#2F6BFF; --ok:#22C55E; }
  * { box-sizing:border-box; margin:0; padding:0; -webkit-tap-highlight-color:transparent; }
  body { background:var(--bg); color:var(--txt);
         font:15px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; padding-bottom:40px; }
  header { padding:24px 16px 14px; }
  h1 { font-size:19px; }
  h1 small { color:var(--mut); font-size:12px; font-weight:normal; display:block; margin-top:3px; }
  section { margin:0 14px 14px; background:var(--card); border:1px solid var(--line);
            border-radius:14px; padding:14px; }
  h2 { font-size:14px; color:var(--mut); font-weight:600; margin-bottom:10px; }
  .file { display:flex; align-items:center; gap:10px; padding:11px 12px; background:#0E1626;
          border:1px solid var(--line); border-radius:10px; margin-bottom:8px; }
  .file .nm { flex:1; word-break:break-all; font-size:14px; }
  .file .sz { color:var(--mut); font-size:12px; white-space:nowrap; }
  .file a { color:#fff; background:var(--acc); text-decoration:none; font-size:13px;
            padding:7px 14px; border-radius:8px; white-space:nowrap; }
  .empty { color:var(--mut); font-size:13px; text-align:center; padding:14px 0; }
  .btn { display:block; width:100%; text-align:center; background:var(--acc); color:#fff;
         border:0; border-radius:10px; padding:13px; font-size:15px; font-weight:600; }
  input[type=file] { display:none; }
  #bar { height:6px; background:#0E1626; border-radius:3px; overflow:hidden; margin-top:10px; display:none; }
  #bar i { display:block; height:100%; width:0; background:var(--ok); transition:width .2s; }
  #st { color:var(--mut); font-size:12px; margin-top:8px; min-height:16px; word-break:break-all; }
  footer { text-align:center; color:var(--mut); font-size:11px; padding:6px 20px; }
</style>
</head>
<body>
<header>
  <h1>🖥 {HOST}<small>POFF 手机投递 · 与电脑互传文件</small></h1>
</header>
<section>
  <h2>📥 待接收（电脑发来的文件）</h2>
  <div id="list"><div class="empty">加载中...</div></div>
</section>
<section>
  <h2>📤 上传文件到电脑</h2>
  <input type="file" id="fp" multiple>
  <button class="btn" onclick="document.getElementById('fp').click()">选择文件（可多选）</button>
  <div id="bar"><i></i></div>
  <div id="st"></div>
</section>
<footer>保持此页面打开即可收发 · 配对码 {TOKEN}</footer>
<script>
const T = new URLSearchParams(location.search).get('t') || '';
const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function fmt(n){
  if(n >= 1073741824) return (n/1073741824).toFixed(2)+' GB';
  if(n >= 1048576) return (n/1048576).toFixed(1)+' MB';
  if(n >= 1024) return (n/1024).toFixed(1)+' KB';
  return n+' B';
}
async function load(){
  try{
    const r = await fetch('/files?t='+encodeURIComponent(T));
    const d = await r.json();
    const el = document.getElementById('list');
    if(!d.files || !d.files.length){
      el.innerHTML = '<div class="empty">暂无待接收文件，电脑端投递后会出现在这里</div>';
      return;
    }
    el.innerHTML = d.files.map(f =>
      '<div class="file"><div class="nm">'+esc(f.name)+'</div>'+
      '<div class="sz">'+fmt(f.size)+'</div>'+
      '<a href="/dl?t='+encodeURIComponent(T)+'&name='+encodeURIComponent(f.name)+'">下载</a></div>'
    ).join('');
  }catch(e){
    document.getElementById('list').innerHTML = '<div class="empty">加载失败，请刷新重试</div>';
  }
}
document.getElementById('fp').addEventListener('change', upload);
function upload(ev){
  const files = [...ev.target.files];
  if(!files.length) return;
  const bar = document.getElementById('bar');
  const bi = bar.querySelector('i');
  const st = document.getElementById('st');
  bar.style.display = 'block';
  let i = 0;
  const next = () => {
    if(i >= files.length){
      st.textContent = '全部上传完成 ✓';
      bar.style.display = 'none';
      ev.target.value = '';
      load();
      return;
    }
    const f = files[i];
    const fd = new FormData();
    fd.append('file', f, f.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload?t='+encodeURIComponent(T));
    xhr.upload.onprogress = e => {
      if(e.lengthComputable){
        const p = Math.round(e.loaded*100/e.total);
        bi.style.width = p+'%';
        st.textContent = '正在上传 ('+(i+1)+'/'+files.length+') '+f.name+' '+p+'%';
      }
    };
    xhr.onload = () => {
      if(xhr.status === 200){ i++; bi.style.width = '0%'; next(); }
      else { st.textContent = '上传失败：'+(xhr.responseText||xhr.status); bar.style.display='none'; }
    };
    xhr.onerror = () => { st.textContent = '网络错误，上传中断'; bar.style.display='none'; };
    xhr.send(fd);
  };
  next();
}
load();
setInterval(load, 8000);
document.addEventListener('visibilitychange', () => { if(!document.hidden) load(); });
</script>
</body>
</html>
"""


class _MobiError(Exception):
    """multipart 解析失败。"""


# ===================== multipart 流式解析 =====================

class _BodyStream:
    """限长读取包装：最多读 total 字节，支持 readline 与回退 unread。"""

    def __init__(self, rfile, total):
        self._rfile = rfile
        self._left = max(0, int(total))
        self._buf = b""

    def read(self, n):
        while len(self._buf) < n and self._left > 0:
            chunk = self._rfile.read(min(65536, self._left))
            if not chunk:
                break
            self._left -= len(chunk)
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def readline(self, limit=65536):
        while b"\n" not in self._buf and self._left > 0 and len(self._buf) <= limit:
            chunk = self._rfile.read(min(65536, self._left))
            if not chunk:
                break
            self._left -= len(chunk)
            self._buf += chunk
        i = self._buf.find(b"\n")
        if i < 0:
            out, self._buf = self._buf, b""
            return out
        out, self._buf = self._buf[:i + 1], self._buf[i + 1:]
        return out

    def unread(self, data):
        if data:
            self._buf = data + self._buf


def _extract_filename(disposition):
    """从 Content-Disposition 提取 filename，兼容 filename*=UTF-8''。"""
    text = disposition.decode("utf-8", errors="ignore")
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", text)
    if m:
        try:
            return urllib.parse.unquote(m.group(1).strip())
        except Exception:
            pass
    m = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    m = re.search(r"filename\s*=\s*([^;]+)", text)
    if m:
        return m.group(1).strip().strip('"')
    return ""


def parse_multipart_files(stream, boundary, sink, max_file_size=MAX_UPLOAD):
    """流式解析 multipart/form-data，逐文件回调 sink(filename) → 可写文件对象。

    返回 [(filename, bytes)]；数据写满写出文件。boundary 匹配失败抛 _MobiError。
    """
    if isinstance(boundary, str):
        boundary = boundary.encode("latin-1")
    delim = b"--" + boundary
    end_delim = b"\r\n" + delim

    # 跳过 preamble，定位第一个 boundary 行
    while True:
        line = stream.readline()
        if not line:
            raise _MobiError("未找到 multipart 边界")
        if line.strip() == delim:
            break

    parts = []
    while True:
        headers = {}
        for _ in range(64):
            line = stream.readline(65536)
            if not line:
                raise _MobiError("part 头不完整")
            line = line.rstrip(b"\r\n")
            if not line:
                break
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower()] = v.strip()
        else:
            raise _MobiError("part 头过大")

        filename = _extract_filename(headers.get(b"content-disposition", b""))
        out = sink(filename) if filename else None

        written = 0
        carry = b""
        keep = len(end_delim) - 1
        final = False
        while True:
            chunk = stream.read(65536)
            if not chunk:
                raise _MobiError("文件数据意外结束")
            data = carry + chunk
            idx = data.find(end_delim)
            if idx >= 0:
                rest = data[idx + len(end_delim):]
                data = data[:idx]
                carry = b""
            elif len(data) > keep:
                carry = data[-keep:]
                data = data[:-keep]
            else:
                carry = data
                data = b""
            if data and out is not None:
                out.write(data)
            written += len(data)
            if idx >= 0:
                # boundary 行结尾：可选空白 + CRLF，或 "--"（结束）
                j = 0
                while j < len(rest) and rest[j:j + 1] in (b" ", b"\t"):
                    j += 1
                if rest[j:j + 2] == b"--":
                    final = True
                elif rest[j:j + 2] == b"\r\n":
                    stream.unread(rest[j + 2:])
                elif rest[j:j + 1] == b"\n":
                    stream.unread(rest[j + 1:])
                else:
                    stream.unread(rest)
                break
        if out is not None:
            out.close()
        if written > max_file_size:
            raise _MobiError("文件超过上传上限")
        if filename:
            parts.append((filename, written))
        if final:
            break
    return parts


# ===================== HTTP Handler =====================

class MobiHandler(BaseHTTPRequestHandler):
    module = None                      # 启动时注入 MobiDropModule
    server_version = "PoffMobi/2.2"

    def log_message(self, fmt, *args):  # 静默默认日志，避免刷控制台
        pass

    def setup(self):
        """手机投递也是 TCP，同样把缓冲撑起来（高延迟 WiFi 上差别很明显）。"""
        super().setup()
        try:
            from .transfer import tune_socket
            tune_socket(self.connection)
        except Exception:
            pass

    # ---- 响应辅助 ----
    def _send_bytes(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def _text(self, code, text):
        self._send_bytes(code, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _json(self, code, obj):
        self._send_bytes(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")

    # ---- 路由 ----
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._text(500, f"服务器错误：{e}")
            except Exception:
                pass

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._text(500, f"服务器错误：{e}")
            except Exception:
                pass

    def _route_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/ping":
            self._text(200, MOBI_PING_TAG)
            return
        qs = urllib.parse.parse_qs(parsed.query)
        token = (qs.get("t") or [""])[0]
        if not self.module.check_token(token):
            self._text(403, "配对码无效，请重新扫码进入")
            return
        self.module.note_seen(self.client_address[0])
        if path == "/":
            blob = self.module.render_page().encode("utf-8")
            self._send_bytes(200, blob, "text/html; charset=utf-8")
        elif path == "/files":
            self._json(200, {"files": self.module.list_outbox()})
        elif path == "/dl":
            self._download((qs.get("name") or [""])[0])
        else:
            self._text(404, "Not Found")

    def _download(self, raw_name):
        info = self.module.resolve_download(raw_name)
        if info is None:
            self._text(404, "文件不存在或已被接收")
            return
        path, name = info
        try:
            size = os.path.getsize(path)
        except OSError:
            self._text(404, "文件不存在")
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        except OSError:
            return
        sent = 0
        try:
            with open(path, "rb") as f:
                while True:
                    block = f.read(262144)
                    if not block:
                        break
                    self.wfile.write(block)
                    sent += len(block)
        except OSError:
            return                      # 手机端取消下载
        if sent >= size:
            self.module.on_downloaded(path, name)

    def _route_post(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/upload":
            self._text(404, "Not Found")
            return
        qs = urllib.parse.parse_qs(parsed.query)
        token = (qs.get("t") or [""])[0]
        if not self.module.check_token(token):
            self._text(403, "配对码无效，请重新扫码进入")
            return
        try:
            total = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        if total <= 0:
            self._text(400, "请求体为空")
            return
        if total > MAX_UPLOAD + 4 * 1024 * 1024:
            self._text(413, "文件超过 2GB 上限")
            return
        ctype = self.headers.get("Content-Type", "")
        m = re.search(r'boundary="?([^";,]+)"?', ctype)
        if not ctype.startswith("multipart/form-data") or not m:
            self._text(400, "请求格式错误")
            return
        stream = _BodyStream(self.rfile, total)
        try:
            saved = self.module.receive_upload(stream, m.group(1))
        except _MobiError as e:
            self._text(400, f"上传解析失败：{e}")
            return
        except OSError as e:
            self._text(500, f"写入失败：{e}")
            return
        self.module.note_seen(self.client_address[0])
        names = "、".join(os.path.basename(p) for p in saved) or "-"
        self.module.ui_log(f"手机上传 {len(saved)} 个文件：{names}", "ok")
        self._json(200, {"ok": True, "count": len(saved)})


# ===================== 二维码 =====================

def make_qr_image(url):
    """生成二维码 PIL Image；qrcode 库缺失或失败返回 None（UI 降级显示 URL）。"""
    try:
        import qrcode
        qr = qrcode.QRCode(border=2, box_size=4,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        return img.convert("RGB")
    except Exception:
        return None


# ===================== 模块 =====================

class MobiDropModule:
    def __init__(self, app, port=MOBI_PORT, data_dir=None, receive_dir=None):
        self.app = app
        self.port = port
        self._data_dir = data_dir
        self._receive_dir = receive_dir
        self.httpd = None
        self._seen_lock = threading.Lock()
        self._last_seen = {}            # ip -> monotonic 时间

    # ---- 路径 ----
    @property
    def outbox_root(self):
        base = self._data_dir or os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser("~"), "Poff")
        return os.path.join(base, "mobi_outbox")

    @property
    def receive_dir(self):
        return self._receive_dir or getattr(self.app, "receive_dir", ".")

    # ---- 配对码 ----
    def get_token(self):
        cfg = self.app.cfg.setdefault("mobi", {})
        token = str(cfg.get("token") or "")
        if not (len(token) == 6 and token.isdigit()):
            token = f"{random.randint(0, 999999):06d}"
            cfg["token"] = token
            try:
                self.app.save_cfg()
            except Exception:
                pass
        return token

    def regen_token(self):
        self.app.cfg.setdefault("mobi", {})["token"] = f"{random.randint(0, 999999):06d}"
        try:
            self.app.save_cfg()
        except Exception:
            pass
        return self.app.cfg["mobi"]["token"]

    def check_token(self, token):
        expect = self.get_token()
        if not expect:
            return False
        try:
            return hmac.compare_digest(str(token), str(expect))
        except Exception:
            return False

    # ---- 手机活跃状态 ----
    def note_seen(self, ip):
        with self._seen_lock:
            self._last_seen[ip] = time.monotonic()

    def is_recent(self, ip, ttl=LAST_SEEN_TTL):
        with self._seen_lock:
            ts = self._last_seen.get(ip)
        return ts is not None and (time.monotonic() - ts) < ttl

    # ---- 待接收区 ----
    def list_outbox(self):
        root = self.outbox_root
        items = []
        try:
            for name in os.listdir(root):
                path = os.path.join(root, name)
                if name.endswith(".part") or not os.path.isfile(path):
                    continue
                try:
                    items.append({"name": name, "size": os.path.getsize(path),
                                  "mtime": os.path.getmtime(path)})
                except OSError:
                    continue
        except OSError:
            pass
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return [{"name": it["name"], "size": it["size"]} for it in items]

    def resolve_download(self, raw_name):
        """仅允许待接收区内的文件，防目录穿越。"""
        name = st.sanitize_filename(raw_name, "")
        if not name:
            return None
        path = os.path.join(self.outbox_root, name)
        if not os.path.isfile(path):
            return None
        return path, name

    def on_downloaded(self, path, name):
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            self.app.ui_call(self.app.log, f"手机已接收：{name}（待接收区已自动清理）", "ok")
        except Exception:
            pass

    def push_files_async(self, paths, on_done=None):
        """PC → 手机：复制文件到待接收区，手机打开投递页即可下载。"""
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            if on_done:
                self.app.ui_call(on_done, False, "没有可发送的文件")
            return

        def task():
            ok_all = True
            last_err = ""
            st.ensure_dir(self.outbox_root)
            for idx, path in enumerate(paths, start=1):
                try:
                    name = st.sanitize_filename(os.path.basename(path), "file")
                    dest = st.unique_path(os.path.join(self.outbox_root, name))
                    shutil.copy2(path, dest)
                    self.app.log(f"已放入手机待接收区（{idx}/{len(paths)}）："
                                 f"{os.path.basename(path)}，手机打开投递页即可下载", "ok")
                except OSError as e:
                    ok_all = False
                    last_err = f"{os.path.basename(path)} - {e}"
                    self.app.log(f"放入手机待接收区失败：{last_err}", "err")
            if on_done:
                msg = (f"{len(paths)} 个文件已放入待接收区" if ok_all else last_err)
                self.app.ui_call(on_done, ok_all, msg)

        threading.Thread(target=task, daemon=True).start()

    # ---- 手机上传落盘 ----
    def receive_upload(self, stream, boundary):
        recv_dir = self.receive_dir
        st.ensure_dir(recv_dir)
        temps = []

        def sink(raw_name):
            tmp = os.path.join(
                recv_dir,
                f".mobi_{int(time.time() * 1000)}_{random.randint(100, 999)}.part")
            temps.append(tmp)
            return open(tmp, "wb")

        try:
            parts = parse_multipart_files(stream, boundary, sink, MAX_UPLOAD)
            saved = []
            replaced = []
            # 先物化配对列表：循环内不能对 temps 做 remove（会跳过后续元素）
            for (raw_name, _size), tmp in list(zip(parts, temps)):
                name = st.sanitize_filename(raw_name, "mobile_upload")
                final = st.unique_path(os.path.join(recv_dir, name))
                os.replace(tmp, final)
                replaced.append(tmp)
                saved.append(final)
            for tmp in replaced:
                temps.remove(tmp)
            return saved
        except BaseException:
            for tmp in temps:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            raise

    # ---- 页面 ----
    def render_page(self):
        try:
            host = socket.gethostname()
        except Exception:
            host = "电脑"
        return _PAGE.replace("{HOST}", host).replace("{TOKEN}", self.get_token())

    def share_url(self):
        host = getattr(self.app, "local_ip", "127.0.0.1")
        return f"http://{host}:{self.port}/?t={self.get_token()}"

    def ui_log(self, msg, level="info"):
        try:
            self.app.ui_call(self.app.log, msg, level)
        except Exception:
            pass

    # ---- 服务生命周期 ----
    def start(self):
        if self.httpd is not None:
            return True
        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), MobiHandler)
        except OSError as e:
            self.httpd = None
            try:
                self.app.log(f"手机投递服务启动失败（端口 {self.port}）：{e}", "err")
            except Exception:
                pass
            return False
        # port=0（测试用临时端口）时回填实际端口，保证 share_url 可用
        self.port = self.httpd.server_address[1]
        self.httpd.daemon_threads = True
        MobiHandler.module = self
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return True

    def stop(self):
        if self.httpd is None:
            return
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        self.httpd = None
