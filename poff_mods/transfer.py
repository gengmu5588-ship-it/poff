# -*- coding: utf-8 -*-
"""POFF V2.0 局域网文件/截图传输。

协议（单条 TCP 连接只做一件事）：
    客户端 -> FILE_PUSH\\n
    客户端 -> {"name":..., "size":..., "kind":"file|screenshot", "sha256":...}\\n
    客户端 -> 原始字节流（size 字节）
    服务端 <- FILE_OK\\n{"path":...}\\n   或   FILE_ERR\\n{"error":...}\\n
"""
import hashlib
import json
import os
import socket
import time

# 单块 1MB：1GB 文件只有 1024 次 send（原 64KB 要 16384 次 syscall）。
# 大块还能让内核一次塞满拥塞窗口，吞吐更接近链路极限。
CHUNK = 1024 * 1024
# 套接字缓冲 4MB。Windows 默认 SO_SNDBUF 只有 64KB，链路 RTT 一旦上到 5ms
# 在途数据就只有 64KB/5ms ≈ 13MB/s（WiFi 上这是最常见的"1GB 传半天"的元凶）。
SOCK_BUF = 4 * 1024 * 1024
# 进度回调最小间隔：原来每 64KB 就 post 一次 UI 任务（1GB = 16384 次），
# 主线程被淹没、界面卡顿，反过来拖慢发送线程。
PROGRESS_INTERVAL = 0.08
MAX_FILE_SIZE = 8 * 1024 * 1024 * 1024      # 单文件上限 8GB，防止异常头部撑爆磁盘
MSG_FILE_PUSH = "FILE_PUSH"
MSG_FILE_OK = "FILE_OK"
MSG_FILE_ERR = "FILE_ERR"


def tune_socket(sock, rcv=False, snd=False):
    """把 TCP 连接调成"跑大文件"的状态。

    - TCP_NODELAY：关掉 Nagle，避免小尾巴包等 ACK；
    - SO_SNDBUF / SO_RCVBUF：把在途窗口撑到 4MB，高延迟链路上才跑得满带宽。
    """
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    for opt, on in ((socket.SO_SNDBUF, snd or not rcv), (socket.SO_RCVBUF, rcv or not snd)):
        if not on:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, SOCK_BUF)
        except OSError:
            pass


def sha256_file(path, progress=None):
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK * 4)
            if not block:
                break
            h.update(block)
            done += len(block)
            if progress:
                progress(done, total)
    return h.hexdigest()


def send_file(host, port, path, kind="file", progress=None, timeout=30):
    """推送单个文件，返回 (ok, message)。progress(sent, total) 在发送过程中回调。"""
    name = os.path.basename(path)
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, f"无法读取文件：{e}"
    if size > MAX_FILE_SIZE:
        return False, "文件超过 8GB 上限"

    try:
        digest = sha256_file(path)
    except OSError as e:
        return False, f"校验文件失败：{e}"

    header = {"name": name, "size": size, "kind": kind, "sha256": digest}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            tune_socket(sock, snd=True)
            sock.sendall(f"{MSG_FILE_PUSH}\n".encode("utf-8"))
            sock.sendall((json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8"))
            sent = 0
            last = 0.0
            buf = bytearray(CHUNK)
            view = memoryview(buf)
            # buffering=0：绕开 Python 的 8KB 缓冲层，直接 readinto 到大块里，
            # 少一次拷贝，机械盘上顺序读也更顺。
            with open(path, "rb", buffering=0) as f:
                while True:
                    n = f.readinto(view)
                    if not n:
                        break
                    sock.sendall(view[:n])
                    sent += n
                    if progress:
                        now = time.monotonic()
                        if now - last >= PROGRESS_INTERVAL or sent >= size:
                            last = now
                            progress(sent, size)
            if progress and last == 0.0:
                progress(sent, size)
            reply = _read_line(sock, timeout=timeout)
    except (OSError, socket.timeout) as e:
        return False, f"连接或传输中断：{e}"

    if reply.startswith(MSG_FILE_OK):
        return True, "传输完成"
    if reply.startswith(MSG_FILE_ERR):
        return False, "对方接收失败"
    return False, "对方无响应（可能是旧版本 Poff）"


def _read_line(sock, timeout=30, limit=8192):
    sock.settimeout(timeout)
    buf = b""
    while b"\n" not in buf and len(buf) < limit:
        block = sock.recv(256)
        if not block:
            break
        buf += block
    return buf.split(b"\n", 1)[0].decode("utf-8", errors="ignore")
