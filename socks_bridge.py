#!/usr/bin/env python3
"""socks_bridge.py — 本地 SOCKS5 桥：把"无认证"的本地入口转发到"带认证"的上游 socks5(h)

用途：chromium 的 --proxy-server 不支持 URL 内嵌凭据（无法给 socks5 传账号密码）。
本桥监听本地回环（默认 127.0.0.1:1080），chromium 直连本地即可；上游认证由桥负责。

配置（env）：
  BRIDGE_UPSTREAM  上游 socks5 地址，形如 socks5h://user:pass@host:port（必填；为空则桥不启动）
  BRIDGE_LISTEN    本地监听，默认 127.0.0.1:1080
"""
import os
import socket
import struct
import threading


def _parse_upstream(url: str):
    """socks5h://user:pass@host:port → (host, port, user, password)"""
    u = url.strip()
    if "://" in u:
        u = u.split("://", 1)[1]
    creds, _, hostport = u.rpartition("@")
    user = password = None
    if creds:
        if ":" in creds:
            user, _, password = creds.partition(":")
        else:
            user = creds
    host, _, port = hostport.rpartition(":")
    return host, int(port), user, password


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def _upstream_connect(host: str, port: int, user, password):
    """连上游 socks5 并完成（可选）认证 + CONNECT 目标。返回已联通的 socket。"""
    s = socket.create_connection((host, port), timeout=20)
    if user:
        s.sendall(b"\x05\x02\x00\x02")
    else:
        s.sendall(b"\x05\x01\x00")
    resp = _recv_exact(s, 2)
    if resp[0] != 5:
        raise ConnectionError("upstream is not socks5")
    if resp[1] == 2 and user:
        ub, pb = user.encode(), (password or "").encode()
        s.sendall(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
        if _recv_exact(s, 2)[1] != 0:
            raise ConnectionError("upstream auth failed")
    return s


def _handle(client: socket.socket, up_host, up_port, up_user, up_pass):
    try:
        ver, n = _recv_exact(client, 2)
        methods = _recv_exact(client, n)
        if ver != 5:
            return
        client.sendall(b"\x05\x00")            # 本地不要求认证
        hdr = _recv_exact(client, 4)
        if hdr[1] != 1:                         # 只支持 CONNECT
            client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            return
        atyp = hdr[3]
        if atyp == 1:
            dst = socket.inet_ntoa(_recv_exact(client, 4))
        elif atyp == 3:
            ln = _recv_exact(client, 1)[0]
            dst = _recv_exact(client, ln).decode()
        elif atyp == 4:
            dst = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
        else:
            return
        dport = struct.unpack("!H", _recv_exact(client, 2))[0]

        up = _upstream_connect(up_host, up_port, up_user, up_pass)
        # 上游 CONNECT（域名交给上游解析 = socks5h 语义）
        req = b"\x05\x01\x00\x03" + bytes([len(dst)]) + dst.encode() + struct.pack("!H", dport)
        up.sendall(req)
        r = _recv_exact(up, 4)
        if r[1] != 0:
            client.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            up.close()
            return
        # 吃掉上游 BND.ADDR/BND.PORT
        if r[3] == 1:
            _recv_exact(up, 6)
        elif r[3] == 3:
            _recv_exact(up, _recv_exact(up, 1)[0] + 2)
        elif r[3] == 4:
            _recv_exact(up, 18)
        client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)

        def pipe(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except Exception:
                pass
            finally:
                for sk in (a, b):
                    try:
                        sk.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
        t = threading.Thread(target=pipe, args=(up, client), daemon=True)
        t.start()
        pipe(client, up)
    except Exception:
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def main():
    upstream = os.environ.get("BRIDGE_UPSTREAM", "").strip()
    if not upstream:
        print("[bridge] BRIDGE_UPSTREAM 为空，桥不启动（chromium 将直连）", flush=True)
        return
    listen = os.environ.get("BRIDGE_LISTEN", "127.0.0.1:1080")
    lhost, _, lport = listen.rpartition(":")
    up_host, up_port, up_user, up_pass = _parse_upstream(upstream)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((lhost or "127.0.0.1", int(lport)))
    srv.listen(128)
    print(f"[bridge] {listen} → {up_host}:{up_port} (auth={'yes' if up_user else 'no'})", flush=True)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=_handle, args=(c, up_host, up_port, up_user, up_pass), daemon=True).start()


if __name__ == "__main__":
    main()
