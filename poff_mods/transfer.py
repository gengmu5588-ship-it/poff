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

CHUNK = 64 * 1024
MAX_FILE_SIZE = 8 * 1024 * 1024 * 1024      # 单文件上限 8GB，防止异常头部撑爆磁盘
MSG_FILE_PUSH = "FILE_PUSH"
MSG_FILE_OK = "FILE_OK"
MSG_FILE_ERR = "FILE_ERR"


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
            sock.sendall(f"{MSG_FILE_PUSH}\n".encode("utf-8"))
            sock.sendall((json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8"))
            sent = 0
            with open(path, "rb") as f:
                while True:
                    block = f.read(CHUNK)
                    if not block:
                        break
                    sock.sendall(block)
                    sent += len(block)
                    if progress:
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
