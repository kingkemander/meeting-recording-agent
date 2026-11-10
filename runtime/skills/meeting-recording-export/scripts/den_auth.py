#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""企业空间（SpaceAgents Cloud / Den）登录态管理。

用途：让「会议录音导出」技能在客户电脑上拿到并复用会议接口所需的登录态，
避免每次都让用户去浏览器 F12 手工复制 Cookie。

三条取证路径：
  1) login          对话里拿到账号密码 → 调 /api/auth/sign-in/email 换 session → 存本机
  2) import-browser 复用本机浏览器（Chrome/Edge/Brave）里已有的登录态 → 解密 → 存本机
  3) import-cdp     用户在内置浏览器面板里自己登录后，从调试端口抓 cookie → 存本机

其余命令：
  status   校验本机登录态是否有效（返回账号、剩余天数）
  get      带上登录态请求任意 /v1 接口，把 JSON 打到 stdout
  logout   删除本机保存的登录态

安全约定：
  - 密码只走 stdin，不进 argv、不落盘、不打印。
  - 登录态存 ~/.spaceagents/den-session.json，权限 600。
  - 所有输出都不回显 cookie 值本身。
"""

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API_BASE = os.environ.get("DEN_API_BASE", "https://cloud.spaceagents.cn/api/den").rstrip("/")
SESSION_FILE = os.environ.get(
    "DEN_SESSION_FILE", os.path.expanduser("~/.spaceagents/den-session.json")
)
COOKIE_NAME_HINT = "better-auth.session_token"
UA = "SpaceAgents-Agent/1.0 (meeting-recording-export)"


# --------------------------------------------------------------------------- #
# 基础 IO
# --------------------------------------------------------------------------- #
def _origin(api_base):
    """https://host/api/den -> https://host"""
    p = urllib.parse.urlsplit(api_base)
    return f"{p.scheme}://{p.netloc}"


def load_session():
    if not os.path.exists(SESSION_FILE):
        return None
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_session(data):
    d = os.path.dirname(SESSION_FILE)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = SESSION_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSION_FILE)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass


def cookie_header(cookies):
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def http_request(method, url, *, headers=None, body=None, timeout=30, allow_error=True):
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("accept", "application/json")
    req.add_header("user-agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, dict(r.headers), r.headers.get_all("Set-Cookie") or [], raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        if not allow_error:
            raise
        return e.code, dict(e.headers or {}), (e.headers.get_all("Set-Cookie") if e.headers else []) or [], raw
    except urllib.error.URLError as e:
        return 0, {}, [], json.dumps({"error": "network", "detail": str(e)}).encode()


def parse_set_cookie(lines):
    """解析 Set-Cookie 列表 -> (cookies, expires_epoch_seconds or None)"""
    cookies = []
    expires = None
    now = int(time.time())
    for line in lines or []:
        parts = [p.strip() for p in line.split(";")]
        if not parts or "=" not in parts[0]:
            continue
        name, _, value = parts[0].partition("=")
        if not name:
            continue
        exp = None
        for p in parts[1:]:
            k, _, v = p.partition("=")
            k = k.strip().lower()
            if k == "max-age":
                try:
                    exp = now + int(v.strip())
                except ValueError:
                    pass
            elif k == "expires" and exp is None:
                try:
                    exp = int(
                        time.mktime(time.strptime(v.strip(), "%a, %d-%b-%Y %H:%M:%S GMT"))
                        - time.timezone
                    )
                except Exception:
                    pass
        cookies.append({"name": name, "value": value, "domain": None, "expires": exp})
        if COOKIE_NAME_HINT in name and exp:
            expires = exp
    return cookies, expires


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def verify(api_base, cookies):
    hdr = {"cookie": cookie_header(cookies)}
    st, _, _, raw = http_request("GET", f"{api_base}/v1/meetings?scope=mine&limit=1", headers=hdr)
    if st != 200:
        return False, st, raw[:300].decode("utf-8", "replace")
    st2, _, _, raw2 = http_request("GET", f"{api_base}/v1/org", headers=hdr)
    info = {}
    if st2 == 200:
        try:
            d = json.loads(raw2)
            org = (d.get("organization") or {}).get("name")
            member = d.get("currentMember") or {}
            owner = (d.get("organization") or {}).get("owner") or {}
            info = {
                "org": org,
                "role": member.get("role"),
                "owner_email": owner.get("email"),
            }
        except Exception:
            pass
    st3, _, _, raw3 = http_request("GET", f"{api_base}/v1/me", headers=hdr)
    if st3 == 200:
        try:
            d3 = json.loads(raw3)
            u = d3.get("user") or {}
            info["email"] = u.get("email")
            info["name"] = u.get("name")
            exp = (d3.get("session") or {}).get("expiresAt")
            if exp:
                # 2027-05-09T07:46:56.634Z
                info["session_expires_at"] = int(
                    time.mktime(time.strptime(exp[:19], "%Y-%m-%dT%H:%M:%S"))
                    - time.timezone
                )
        except Exception:
            pass
    return True, 200, json.dumps(info, ensure_ascii=False)


def pick_expiry(detail, fallback=None):
    """优先用 /v1/me 返回的 session.expiresAt（这是服务端真实有效期）。"""
    try:
        d = json.loads(detail) if isinstance(detail, str) else (detail or {})
        v = d.get("session_expires_at")
        if v:
            return int(v)
    except Exception:
        pass
    return fallback


def describe_account(detail):
    try:
        d = json.loads(detail) if isinstance(detail, str) else (detail or {})
    except Exception:
        d = {}
    parts = []
    if d.get("email"):
        parts.append(f"账号：{d['email']}")
    if d.get("org"):
        parts.append(f"空间：{d['org']}")
    if d.get("role"):
        parts.append(f"角色：{d['role']}")
    return " / ".join(parts) if parts else "账号信息未知"


# --------------------------------------------------------------------------- #
# 路径 1：账号密码登录
# --------------------------------------------------------------------------- #
def cmd_login(args):
    api_base = (args.api_base or DEFAULT_API_BASE).rstrip("/")
    origin = _origin(api_base)
    email = args.email.strip()
    password = None
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    elif args.password_env:
        password = os.environ.get(args.password_env)
    if not password:
        return fail(
            "没有拿到密码。请用 --password-stdin 从标准输入传入（不要把密码写进命令行参数）。"
        )
    body = json.dumps({"email": email, "password": password}).encode()
    st, _, setc, raw = http_request(
        "POST",
        f"{origin}/api/auth/sign-in/email",
        headers={"content-type": "application/json"},
        body=body,
    )
    del password
    if st != 200:
        try:
            msg = json.loads(raw).get("message") or raw.decode("utf-8", "replace")
        except Exception:
            msg = raw[:300].decode("utf-8", "replace")
        return fail(f"登录失败（HTTP {st}）：{msg}")
    cookies, expires = parse_set_cookie(setc)
    if not cookies:
        return fail("登录接口没有返回 cookie，可能是账号需要二次验证或该部署不支持密码登录。")
    ok, vst, detail = verify(api_base, cookies)
    if not ok:
        return fail(f"登录成功但会议接口校验失败（HTTP {vst}）：{detail}")
    expires = pick_expiry(detail, expires)
    save_session(
        {
            "api_base": api_base,
            "method": "password",
            "email": email,
            "saved_at": int(time.time()),
            "expires_at": expires,
            "cookies": cookies,
            "account": json.loads(detail) if detail.startswith("{") else {},
        }
    )
    return ok_out(
        f"登录态已保存。{describe_account(detail)}。有效期：{fmt_expiry(expires)}。文件：{SESSION_FILE}"
    )


# --------------------------------------------------------------------------- #
# 路径 2：复用本机浏览器登录态
# --------------------------------------------------------------------------- #
BROWSER_PROFILES = {
    "chrome": {
        "darwin": "~/Library/Application Support/Google/Chrome",
        "win32": os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
        "linux": "~/.config/google-chrome",
        "keychain": "Chrome Safe Storage",
        "iter": 1003,
    },
    "edge": {
        "darwin": "~/Library/Application Support/Microsoft Edge",
        "win32": os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
        "linux": "~/.config/microsoft-edge",
        "keychain": "Microsoft Edge Safe Storage",
        "iter": 1003,
    },
    "brave": {
        "darwin": "~/Library/Application Support/BraveSoftware/Brave-Browser",
        "win32": os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"),
        "linux": "~/.config/BraveSoftware/Brave-Browser",
        "keychain": "Brave Safe Storage",
        "iter": 1003,
    },
}


def _chrome_epoch_to_epoch(v):
    if not v:
        return None
    try:
        return int(v) / 1_000_000 - 11644473600
    except Exception:
        return None


def _find_cookie_db(root):
    root = os.path.expanduser(root)
    cands = []
    if os.path.isdir(os.path.join(root, "Network")):
        cands.append(os.path.join(root, "Network", "Cookies"))
    for name in ("Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4", "Profile 5"):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        for sub in ("Network/Cookies", "Cookies"):
            f = os.path.join(p, sub)
            if os.path.exists(f):
                cands.append(f)
    cands.append(os.path.join(root, "Cookies"))
    seen, out = set(), []
    for c in cands:
        if c not in seen and os.path.exists(c):
            seen.add(c)
            out.append(c)
    return out


def _mac_key(service):
    """读钥匙串获取 Chromium Safe Storage 主密钥（可能触发系统授权弹窗）。"""
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return p.stdout.strip().encode()
    except Exception:
        return None


def _win_dpapi_key(root):
    """Windows: Local State 里的 os_crypt.encrypted_key 用 DPAPI 解出 AES 主密钥。"""
    try:
        import ctypes
        from ctypes import wintypes

        state = os.path.join(os.path.expanduser(root), "Local State")
        with open(state, "r", encoding="utf-8") as f:
            blob = json.load(f)["os_crypt"]["encrypted_key"]
        enc = base64.b64decode(blob)
        if enc[:5] == b"DPAPI":
            enc = enc[5:]

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(enc, len(enc))
        blob_in = DATA_BLOB(len(enc), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        ):
            return None
        data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return data
    except Exception:
        return None


def _decrypt_chromium(enc, key, host):
    """解密 Chromium cookie 的 encrypted_value。返回明文 bytes 或 None。"""
    if not enc:
        return b""
    try:
        if enc[:3] in (b"v10", b"v11"):
            if platform.system() == "Darwin":
                from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa
    except Exception:
        pass
    try:
        if enc[:3] in (b"v10", b"v11"):
            body = enc[3:]
            if key is None:
                return None
            # macOS / Linux：AES-128-CBC，PBKDF2(key, 'saltysalt')
            if platform.system() == "Windows" and len(key) == 32:
                return _aes_gcm_win(body, key)
            k = hashlib.pbkdf2_hmac("sha1", key, b"saltysalt", 1003, 16)
            plain = _aes_cbc_decrypt(body, k, b" " * 16)
            if plain is None:
                return None
            # 新版 Chrome(>=130, macOS) 前 32 字节是 host_key 的 sha256
            if len(plain) >= 32 and plain[:32] == hashlib.sha256(host.encode()).digest():
                plain = plain[32:]
            return plain
        return enc
    except Exception:
        return None


def _aes_cbc_decrypt(data, key, iv):
    """纯标准库优先用 openssl 命令；没有就用内置实现。"""
    if shutil.which("openssl"):
        p = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(), "-iv", iv.hex()],
            input=data,
            capture_output=True,
        )
        if p.returncode == 0:
            return p.stdout
        return None
    return _aes_cbc_decrypt_py(data, key, iv)


def _aes_cbc_decrypt_py(data, key, iv):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        out = d.update(data) + d.finalize()
        return out[: -out[-1]] if out else out
    except Exception:
        return None


def _aes_gcm_win(nonce_and_ct, key):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce, ct = nonce_and_ct[:12], nonce_and_ct[12:]
        return AESGCM(key).decrypt(nonce, ct, None)
    except Exception:
        return None


def cmd_import_browser(args):
    api_base = (args.api_base or DEFAULT_API_BASE).rstrip("/")
    sysname = "darwin" if platform.system() == "Darwin" else (
        "win32" if platform.system() == "Windows" else "linux"
    )
    browsers = [args.browser] if args.browser else ["chrome", "edge", "brave"]
    tried = []
    for b in browsers:
        cfg = BROWSER_PROFILES.get(b)
        if not cfg:
            continue
        root = os.path.expanduser(cfg[sysname])
        if not os.path.isdir(root):
            tried.append(f"{b}: 未安装/无配置目录")
            continue
        dbs = _find_cookie_db(root)
        if not dbs:
            tried.append(f"{b}: 找不到 Cookies 数据库")
            continue
        key = None
        if sysname == "darwin":
            key = _mac_key(cfg["keychain"])
            if key is None:
                tried.append(f"{b}: 钥匙串未授权（弹窗被拒绝或超时）")
                continue
        elif sysname == "win32":
            key = _win_dpapi_key(root)
        else:
            key = b"peanuts"
        for db in dbs:
            cookies = _read_cookies_from_db(db, key)
            if not cookies:
                continue
            ok, st, detail = verify(api_base, cookies)
            if ok:
                exp = pick_expiry(detail, max([c.get("expires") or 0 for c in cookies]) or None)
                save_session(
                    {
                        "api_base": api_base,
                        "method": f"browser:{b}",
                        "email": None,
                        "saved_at": int(time.time()),
                        "expires_at": exp,
                        "cookies": cookies,
                        "account": json.loads(detail) if detail.startswith("{") else {},
                    }
                )
                return ok_out(
                    f"已复用 {b} 的登录态。{describe_account(detail)}。有效期：{fmt_expiry(exp)}。"
                )
            tried.append(f"{b}:{os.path.basename(os.path.dirname(db))} 里的登录态无效（HTTP {st}）")
    return fail("没找到可用的浏览器登录态：\n- " + "\n- ".join(tried))


def _read_cookies_from_db(db_path, key):
    tmpdir = tempfile.mkdtemp(prefix="denck-")
    tmp = os.path.join(tmpdir, "Cookies")
    try:
        shutil.copy(db_path, tmp)
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None
    cookies = []
    try:
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        rows = con.execute(
            "select host_key,name,encrypted_value,value,expires_utc,is_secure from cookies"
        ).fetchall()
        con.close()
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None
    shutil.rmtree(tmpdir, ignore_errors=True)
    for host, name, enc, plain, exp, is_secure in rows:
        if "better-auth" not in name:
            continue
        domain = (host or "").lstrip(".")
        if domain and not domain.endswith("spaceagents.cn"):
            continue
        val = None
        if plain:
            val = plain
        else:
            dec = _decrypt_chromium(enc, key, host or "")
            if dec is None:
                continue
            try:
                val = dec.decode("utf-8")
            except UnicodeDecodeError:
                continue
        cookies.append(
            {
                "name": name,
                "value": val,
                "domain": host,
                "expires": _chrome_epoch_to_epoch(exp),
            }
        )
    # 只要 session cookie 就够；顺带带上同域其它 better-auth cookie
    if not any(COOKIE_NAME_HINT in c["name"] for c in cookies):
        return None
    return cookies


# --------------------------------------------------------------------------- #
# 路径 3：从内置浏览器调试端口抓 cookie（用户自己登录，不接触密码）
# --------------------------------------------------------------------------- #
def _ws_connect(url, timeout=15):
    u = urllib.parse.urlsplit(url)
    port = u.port or (443 if u.scheme == "wss" else 80)
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    if u.scheme == "wss":
        import ssl

        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
    key = base64.b64encode(os.urandom(16)).decode()
    path = u.path + (("?" + u.query) if u.query else "")
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("WS 握手失败")
        resp += chunk
    if b"101" not in resp.split(b"\r\n")[0]:
        raise RuntimeError("WS 握手被拒绝：" + resp.split(b"\r\n")[0].decode("latin1"))
    return sock


def _ws_send(sock, payload):
    data = payload.encode()
    hdr = bytearray([0x81])
    n = len(data)
    mask = os.urandom(4)
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127)
        hdr += struct.pack(">Q", n)
    hdr += mask
    sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_recv(sock):
    def recvn(n):
        buf = b""
        while len(buf) < n:
            c = sock.recv(n - len(buf))
            if not c:
                raise RuntimeError("WS 连接关闭")
            buf += c
        return buf

    h = recvn(2)
    fin = h[0] & 0x80
    opcode = h[0] & 0x0F
    masked = h[1] & 0x80
    ln = h[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", recvn(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", recvn(8))[0]
    mk = recvn(4) if masked else None
    data = recvn(ln) if ln else b""
    if mk:
        data = bytes(b ^ mk[i % 4] for i, b in enumerate(data))
    if opcode == 0x8:
        raise RuntimeError("WS 被对端关闭")
    if opcode == 0x9:
        return None
    return data


def _cdp_call(ws_url, method, params=None, timeout=20):
    sock = _ws_connect(ws_url)
    try:
        _ws_send(sock, json.dumps({"id": 1, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = _ws_recv(sock)
            if raw is None:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result") or {}
        raise RuntimeError("CDP 响应超时")
    finally:
        sock.close()


def cmd_import_cdp(args):
    api_base = (args.api_base or DEFAULT_API_BASE).rstrip("/")
    port = args.port or 9223
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=8) as r:
            targets = json.load(r)
    except Exception as e:
        return fail(
            f"连不上内置浏览器调试端口 127.0.0.1:{port}（{e}）。"
            "请先在右侧浏览器面板打开企业空间并完成登录。"
        )
    cookie_sets = []
    for t in targets:
        if t.get("type") != "page":
            continue
        ws = t.get("webSocketDebuggerUrl")
        if not ws:
            continue
        try:
            res = _cdp_call(ws, "Network.getAllCookies")
        except Exception:
            continue
        for c in res.get("cookies") or []:
            dom = (c.get("domain") or "").lstrip(".")
            if "better-auth" in (c.get("name") or "") and dom.endswith("spaceagents.cn"):
                cookie_sets.append(
                    {
                        "name": c["name"],
                        "value": c["value"],
                        "domain": c.get("domain"),
                        "expires": c.get("expires") or None,
                    }
                )
    if not cookie_sets:
        return fail(
            "内置浏览器里没找到企业空间登录态。请在浏览器面板打开 "
            f"{_origin(api_base)} 并完成登录后再执行一次。"
        )
    ok, st, detail = verify(api_base, cookie_sets)
    if not ok:
        return fail(f"从内置浏览器抓到的登录态无效（HTTP {st}）：{detail}")
    exp = pick_expiry(detail, max([c.get("expires") or 0 for c in cookie_sets]) or None)
    save_session(
        {
            "api_base": api_base,
            "method": "builtin-browser",
            "email": None,
            "saved_at": int(time.time()),
            "expires_at": exp,
            "cookies": cookie_sets,
            "account": json.loads(detail) if detail.startswith("{") else {},
        }
    )
    return ok_out(
        f"已从内置浏览器保存登录态。{describe_account(detail)}。有效期：{fmt_expiry(exp)}。"
    )


# --------------------------------------------------------------------------- #
# 导入手工 cookie（兼容旧流程）
# --------------------------------------------------------------------------- #
def cmd_import_cookie(args):
    api_base = (args.api_base or DEFAULT_API_BASE).rstrip("/")
    raw = args.cookie
    if raw == "-":
        raw = sys.stdin.read()
    raw = (raw or "").strip()
    if not raw:
        return fail("cookie 为空")
    cookies = []
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            cookies.append({"name": name, "value": value, "domain": None, "expires": None})
    if not cookies:
        return fail("cookie 格式不对，应形如 name=value; name2=value2")
    ok, st, detail = verify(api_base, cookies)
    if not ok:
        return fail(f"cookie 无效（HTTP {st}）：{detail}")
    save_session(
        {
            "api_base": api_base,
            "method": "manual-cookie",
            "email": None,
            "saved_at": int(time.time()),
            "expires_at": None,
            "cookies": cookies,
            "account": json.loads(detail) if detail.startswith("{") else {},
        }
    )
    return ok_out("手工 cookie 已校验并保存。")


# --------------------------------------------------------------------------- #
# status / get / logout
# --------------------------------------------------------------------------- #
def cmd_status(args):
    s = load_session()
    if not s:
        return fail("本机还没有保存登录态，请先执行 login / import-browser / import-cdp。")
    api_base = (args.api_base or s.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    ok, st, detail = verify(api_base, s.get("cookies") or [])
    if not ok:
        return fail(
            f"已保存的登录态已失效（HTTP {st}）。请重新执行 login / import-browser / import-cdp 更新。"
        )
    acct = json.loads(detail) if detail.startswith("{") else {}
    left = ""
    if s.get("expires_at"):
        days = (s["expires_at"] - time.time()) / 86400
        left = f"，剩余 {days:.1f} 天"
        if days < 7:
            left += "（建议尽快重新登录）"
    return ok_out(
        "登录态有效。"
        f"接口：{api_base}，来源：{s.get('method')}，"
        f"{describe_account(detail)}{left}。"
    )


def cmd_get(args):
    s = load_session()
    if not s:
        return fail("本机还没有保存登录态。")
    api_base = (args.api_base or s.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    path = args.path if args.path.startswith("/") else "/" + args.path
    url = api_base + path
    st, _, _, raw = http_request(
        "GET", url, headers={"cookie": cookie_header(s.get("cookies") or [])}
    )
    sys.stdout.write(raw.decode("utf-8", "replace"))
    sys.stdout.write("\n")
    return 0 if st == 200 else 1


def cmd_logout(args):
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
        return ok_out(f"已删除本机登录态：{SESSION_FILE}")
    return ok_out("本机本来就没有登录态。")


# --------------------------------------------------------------------------- #
# 输出工具
# --------------------------------------------------------------------------- #
def fmt_expiry(exp):
    if not exp:
        return "未知（浏览器会话级或未返回 Max-Age）"
    t = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
    days = (exp - time.time()) / 86400
    return f"{t}（剩余 {days:.1f} 天）"


def ok_out(msg):
    print(json.dumps({"ok": True, "message": msg}, ensure_ascii=False))
    return 0


def fail(msg):
    print(json.dumps({"ok": False, "message": msg}, ensure_ascii=False))
    return 1


def main():
    ap = argparse.ArgumentParser(description="企业空间登录态管理")
    ap.add_argument("--api-base", default=None, help="默认取 DEN_API_BASE 或 https://cloud.spaceagents.cn/api/den")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="用账号密码登录并保存登录态")
    p.add_argument("--email", required=True)
    p.add_argument("--password-stdin", action="store_true", help="从标准输入读密码（推荐）")
    p.add_argument("--password-env", default=None, help="从环境变量读密码")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("import-browser", help="复用本机浏览器已登录的会话")
    p.add_argument("--browser", choices=list(BROWSER_PROFILES), default=None)
    p.set_defaults(func=cmd_import_browser)

    p = sub.add_parser("import-cdp", help="从内置浏览器调试端口抓登录态")
    p.add_argument("--port", type=int, default=9223)
    p.set_defaults(func=cmd_import_cdp)

    p = sub.add_parser("import-cookie", help="导入手工复制的 cookie 串")
    p.add_argument("--cookie", required=True, help="name=value; name2=value2，用 - 表示从 stdin 读")
    p.set_defaults(func=cmd_import_cookie)

    p = sub.add_parser("status", help="查看本机登录态是否有效")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("get", help="带登录态 GET 一个 /v1 接口")
    p.add_argument("path")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("logout", help="删除本机登录态")
    p.set_defaults(func=cmd_logout)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
