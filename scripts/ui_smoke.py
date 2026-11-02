#!/usr/bin/env python3
"""浏览器冒烟测试：真实打开页面、点遍导航、抓控制台异常。

为什么需要它
------------
括号闭合检查只能发现语法断裂；像 `$('#newId')` 拼错、某个面板渲染空白、
点击导航抛 TypeError 这类**运行时错误**，只有真浏览器能抓到。

为什么不用 Playwright
--------------------
本项目核心零依赖，冒烟工具也不该例外。Chrome/Edge 自带 DevTools Protocol，
这里用标准库实现了一个最小 WebSocket 客户端（握手 + 帧编解码）来驱动它。

用法
----
    python scripts/ui_smoke.py                 # 自动找 Edge/Chrome
    python scripts/ui_smoke.py --port 8799 --keep-server
    python scripts/ui_smoke.py --browser "C:/Program Files/.../msedge.exe"

退出码
------
    0 通过 / 1 有错误 / 3 环境不可用（无浏览器或服务起不来，跳过）

覆盖的检查
----------
1. `/admin` 与 `/` 加载后**无 console.error / 未捕获异常 / 网络 4xx-5xx**
2. 后台 8 个导航项逐个点击，对应视图必须真正激活
3. 打开「新增」弹窗并关闭（验证 modal 宿主与事件绑定）
4. 关键 DOM 节点存在（KPI 卡片、表格容器、AI 配置折叠块）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

SMOKE_USER = "smoke"
SMOKE_PASS = "smoke-pass-12345"

BROWSER_CANDIDATES = (
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


class SmokeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 最小 WebSocket 客户端（RFC 6455，只支持 CDP 用到的文本帧）
# --------------------------------------------------------------------------- #
class WebSocket:
    def __init__(self, url: str) -> None:
        if not url.startswith("ws://"):
            raise SmokeError(f"只支持 ws://，收到 {url}")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SmokeError("WebSocket 握手时连接被关闭")
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in head.split("\r\n")[0]:
            raise SmokeError(f"WebSocket 握手失败：{head.splitlines()[0]}")
        self._buffer = buf.split(b"\r\n\r\n", 1)[1]

    # ------------------------------------------------------------------ #
    def _read(self, n: int) -> bytes:
        while len(self._buffer) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buffer)))
            if not chunk:
                raise SmokeError("WebSocket 连接已断开")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def send(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])  # FIN + text
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> str:
        chunks: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            fin = b1 & 0x80
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else b""
            data = self._read(length)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

            if opcode == 0x8:  # close
                raise SmokeError("浏览器关闭了调试连接")
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode in (0x1, 0x0):
                chunks.append(data)
                if fin:
                    return b"".join(chunks).decode(errors="replace")
                continue

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """极简 DevTools 客户端：调用 + 事件收集。"""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._id = 0
        self.console_errors: list[str] = []
        self.exceptions: list[str] = []
        self.network_errors: list[str] = []

    def _pump(self, want_id: int | None) -> dict | None:
        while True:
            msg = json.loads(self.ws.recv())
            if "id" in msg:
                if want_id is not None and msg["id"] == want_id:
                    return msg
                continue
            self._on_event(msg)

    def _on_event(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            text = " ".join(
                str(a.get("value", a.get("description", ""))) for a in params.get("args", [])
            )
            self.console_errors.append(text[:300])
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails") or {}
            text = detail.get("exception", {}).get("description") or detail.get("text") or "未知异常"
            self.exceptions.append(str(text)[:400])
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") == "error":
                text = f"{entry.get('text', '')} {entry.get('url', '')}".strip()
                # favicon 与"标签页关闭导致的请求中止"不是缺陷，过滤掉
                if "favicon" in text or "ERR_ABORTED" in text:
                    return
                self.network_errors.append(text[:300])

    def call(self, method: str, params: dict | None = None, *, timeout: float = 20.0) -> dict:
        self._id += 1
        msg_id = self._id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            reply = self._pump(msg_id)
            if reply is not None:
                if "error" in reply:
                    raise SmokeError(f"{method} 失败：{reply['error']}")
                return reply.get("result", {})
        raise SmokeError(f"{method} 超时（{timeout}s）")

    def evaluate(self, expression: str, *, await_promise: bool = False) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            desc = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise SmokeError(f"页面内脚本抛错：{desc}")
        return (result.get("result") or {}).get("value")


# --------------------------------------------------------------------------- #
# 服务与浏览器
# --------------------------------------------------------------------------- #
def http_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def wait_http(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json(url)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def ensure_admin(root: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "scalpx.web", "create-user",
         "--username", SMOKE_USER, "--password", SMOKE_PASS, "--role", "admin"],
        cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )


def start_server(root: Path, port: int) -> subprocess.Popen | None:
    if wait_http(f"http://127.0.0.1:{port}/api/health", timeout=2):
        print(f"  复用已在运行的服务 :{port}")
        return None
    ensure_admin(root)
    proc = subprocess.Popen(
        [sys.executable, "-m", "scalpx.web", "serve", "--root", str(root), "--port", str(port)],
        cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if not wait_http(f"http://127.0.0.1:{port}/api/health", timeout=25):
        proc.terminate()
        raise SmokeError(f"服务未能在 25s 内就绪（端口 {port}）")
    print(f"  已启动服务 :{port}")
    return proc


def find_browser(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    for path in BROWSER_CANDIDATES:
        if Path(path).exists():
            return path
    for name in ("chrome", "chromium", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def launch_browser(exe: str, debug_port: int, profile: Path, headless: bool) -> subprocess.Popen:
    def spawn(flag: str | None) -> subprocess.Popen:
        args = [
            exe,
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-features=Translate,MediaRouter",
            "about:blank",
        ]
        if flag:
            args.insert(1, flag)
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    proc = spawn("--headless=new" if headless else None)
    time.sleep(1.0)
    if headless and proc.poll() is not None:
        # 旧版 Chromium 不认 --headless=new，退回旧 flag
        proc = spawn("--headless")
        time.sleep(1.0)
    return proc


def purge_smoke_user(base: str) -> None:
    """清理冒烟专用账号，避免在库里留下一个已知口令的管理员。"""
    try:
        login = http_json(
            f"{base}/api/auth/login", "POST",
            {"username": SMOKE_USER, "password": SMOKE_PASS},
        )
        token = login["data"]["token"]
        req = urllib.request.Request(
            f"{base}/api/admin/users", headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            users = json.loads(resp.read().decode())["data"]["items"]
        for user in users:
            if user["username"] != SMOKE_USER:
                continue
            del_req = urllib.request.Request(
                f"{base}/api/admin/users/{user['id']}", method="DELETE",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(del_req, timeout=5):
                print("  已清理冒烟账号")
    except Exception:  # noqa: BLE001 - 清理失败不影响测试结论
        pass


def open_target(debug_port: int, url: str) -> str:
    data = json.loads(
        urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{debug_port}/json/new?{url}", method="PUT"),
            timeout=10,
        ).read().decode()
    )
    return data["webSocketDebuggerUrl"]


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def wait_for(cdp: CDP, expression: str, timeout: float = 10.0, hint: str = "") -> None:
    """轮询直到表达式为真。

    必须包一层 `Boolean(...)` + `try/catch`：
    - 直接返回 DOM 节点时 CDP 的 returnByValue 无法序列化，会一律得到假；
    - 页面还没渲染完时表达式可能抛错（null 取属性），应视为"还没好"而不是失败。
    """
    wrapped = f"(() => {{ try {{ return Boolean({expression}); }} catch (e) {{ return false; }} }})()"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.evaluate(wrapped):
            return
        time.sleep(0.2)
    raise SmokeError(f"等待超时：{hint or expression}")


def login(cdp: CDP, base: str) -> None:
    result = cdp.evaluate(
        f"""(async () => {{
            const r = await fetch('{base}/api/auth/login', {{
                method: 'POST', headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{username: '{SMOKE_USER}', password: '{SMOKE_PASS}'}})
            }});
            const d = await r.json();
            if (!d.ok) return 'FAIL ' + JSON.stringify(d.error);
            localStorage.setItem('scalpx.token', d.data.token);
            localStorage.setItem('scalpx.user', JSON.stringify(d.data.user));
            return 'OK';
        }})()""",
        await_promise=True,
    )
    if result != "OK":
        raise SmokeError(f"登录失败：{result}")


def check_admin(cdp: CDP, base: str) -> list[str]:
    problems: list[str] = []
    cdp.call("Page.navigate", {"url": f"{base}/admin"})
    wait_for(cdp, "document.readyState === 'complete'", hint="/admin 加载")
    wait_for(cdp, "!!document.querySelector('#loginForm')", hint="登录表单渲染")

    login(cdp, base)
    cdp.call("Page.reload")
    wait_for(cdp, "document.readyState === 'complete'", hint="/admin 重载")
    wait_for(
        cdp,
        "(() => { const v = document.querySelector('#appView'); return v && v.style.display !== 'none'; })()",
        hint="登录后进入控制台",
    )

    navs = cdp.evaluate(
        "Array.from(document.querySelectorAll('.nav-item[data-view]')).map(e => e.dataset.view)"
    )
    if not navs or len(navs) < 6:
        problems.append(f"导航项过少：{navs}")
    print(f"  导航项：{', '.join(navs or [])}")

    for view in navs or []:
        clicked = cdp.evaluate(
            f"(() => {{ const el = document.querySelector('.nav-item[data-view=\"{view}\"]');"
            " if (!el) return false; el.click(); return true; })()"
        )
        if not clicked:
            problems.append(f"导航 {view} 找不到元素")
            continue
        try:
            wait_for(
                cdp,
                f"!!document.querySelector('#view-{view}.active')",
                timeout=4,
                hint=f"视图 {view} 激活",
            )
        except SmokeError:
            problems.append(f"导航 {view} 点击后视图未激活")
        time.sleep(0.35)  # 让该视图的数据加载落地，避免 9 个请求互相穿插

    # 弹窗：打开 -> 关闭
    cdp.evaluate("document.querySelector('.nav-item[data-view=\"entities\"]').click()")
    wait_for(cdp, "document.querySelector('#view-entities.active')", hint="主数据视图")
    if cdp.evaluate("!document.querySelector('#entNew')"):
        problems.append("缺少「新增」按钮 #entNew")
    else:
        cdp.evaluate("document.querySelector('#entNew').click()")
        try:
            wait_for(cdp, "document.querySelector('#modalHost').classList.contains('open')", timeout=3, hint="弹窗打开")
            cdp.evaluate("closeModal()")
            wait_for(cdp, "!document.querySelector('#modalHost').classList.contains('open')", timeout=3, hint="弹窗关闭")
        except SmokeError as exc:
            problems.append(f"弹窗交互异常：{exc}")

    # 关键节点存在
    for selector, label in (
        ("#entSelect", "数据类型下拉"),
        ("#aiCfgBody", "AI 配置面板"),
        ("#aiParseBtn", "AI 指令按钮"),
        ("#instrumentTable", "标的表"),
    ):
        ok = cdp.evaluate(f"!!document.querySelector('{selector.split(',')[0].strip()}')")
        if not ok:
            problems.append(f"缺少节点：{label}（{selector}）")

    return problems


def check_front(cdp: CDP, base: str) -> list[str]:
    problems: list[str] = []
    cdp.call("Page.navigate", {"url": f"{base}/"})
    wait_for(cdp, "document.readyState === 'complete'", hint="/ 加载")
    wait_for(cdp, "!!document.querySelector('#kpiReturn')", hint="KPI 渲染")
    for selector, label in (
        ("#equityChart", "权益曲线 canvas"),
        ("#view-dashboard", "总览视图"),
        (".nav-item", "导航"),
    ):
        if not cdp.evaluate(f"!!document.querySelector('{selector.split(',')[0].strip()}')"):
            problems.append(f"前台缺少节点：{label}")
    return problems


# --------------------------------------------------------------------------- #
def run_checks(debug_port: int, base: str, label: str, checker) -> list[str]:
    """每个页面开独立标签页。

    复用一个标签页跑两个页面时，导航会打断在途请求，
    报出一堆与被测对象无关的 "请求中止"，掩盖真正的问题。
    """
    print(f"\n{label}")
    ws = WebSocket(open_target(debug_port, "about:blank"))
    cdp = CDP(ws)
    try:
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.call("Log.enable")
        found = checker(cdp, base)
        found += [f"console.error: {e}" for e in cdp.console_errors[:5]]
        found += [f"未捕获异常: {e}" for e in cdp.exceptions[:5]]
        found += [f"请求错误: {e}" for e in cdp.network_errors[:5]]
    except SmokeError as exc:
        found = [str(exc)]
    finally:
        ws.close()

    # 就地打印，避免两个页面的问题混在一起看不出归属
    for item in found:
        print(f"  ✘ {item}")
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description="浏览器冒烟测试（零依赖，驱动 Edge/Chrome CDP）")
    ap.add_argument("--port", type=int, default=8799, help="被测服务端口")
    ap.add_argument("--debug-port", type=int, default=9333, help="浏览器调试端口")
    ap.add_argument("--browser", default=None, help="浏览器可执行文件路径")
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    ap.add_argument("--keep-server", action="store_true", help="结束后不关闭服务")
    ap.add_argument("--root", default=str(ROOT), help="项目根目录")
    args = ap.parse_args()

    exe = find_browser(args.browser)
    if not exe:
        print("跳过：未找到 Chrome/Edge，可用 --browser 指定路径")
        return 3

    base = f"http://127.0.0.1:{args.port}"
    print(f"浏览器冒烟测试\n  浏览器：{exe}\n  目标：{base}")
    server = None
    profile = Path(tempfile.mkdtemp(prefix="scalpx-smoke-"))
    browser = None
    problems: list[str] = []
    try:
        server = start_server(Path(args.root), args.port)
        browser = launch_browser(exe, args.debug_port, profile, not args.headed)
        if not wait_http(f"http://127.0.0.1:{args.debug_port}/json/version", timeout=20):
            raise SmokeError("浏览器调试端口未就绪")

        problems += run_checks(args.debug_port, base, "[1/2] 后台 /admin", check_admin)
        problems += run_checks(args.debug_port, base, "[2/2] 前台 /", check_front)
        purge_smoke_user(base)
    except SmokeError as exc:
        problems.append(str(exc))
    finally:
        if browser is not None:
            browser.terminate()
            try:
                browser.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser.kill()
        shutil.rmtree(profile, ignore_errors=True)
        if server is not None and not args.keep_server:
            server.terminate()

    print()
    if problems:
        print(f"✘ 失败（{len(problems)} 项）")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("✔ 通过：导航、弹窗、关键节点、控制台均无异常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
