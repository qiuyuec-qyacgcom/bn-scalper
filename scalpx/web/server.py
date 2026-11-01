"""HTTP 传输层：路由分发、静态资源、后台任务、进程生命周期。

用 `http.server` 而不是 FastAPI 是刻意的：整条部署链只需要 Python 3.11+，
没有 wheel、没有编译、没有 node。单机后台 + 少量并发（ThreadingHTTPServer）
完全够用，且攻击面最小。

职责边界：
- 这里**只做**协议层的事（解析、鉴权提取、序列化、静态文件、任务调度）；
- 业务判断全部在 `api.py`，数据访问全部在 `repo.py`。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .api import Api, ApiError, Request, fail
from .auth import Principal, load_or_create_secret, verify_token
from .repo import TradingRepo, WebRepo

log = logging.getLogger("scalpx.web")

STATIC_ROOT = Path(__file__).parent / "static"
MAX_BODY_BYTES = 1 << 20  # 1 MiB


# --------------------------------------------------------------------------- #
# 后台任务：单 worker，避免多个回测同时打满 CPU
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Job:
    id: str
    name: str
    status: str = "queued"  # queued | running | done | error
    created_ts: float = field(default_factory=time.time)
    started_ts: float = 0.0
    finished_ts: float = 0.0
    result: Any = None
    error: str = ""
    log: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "created_ts": int(self.created_ts),
            "started_ts": int(self.started_ts) if self.started_ts else 0,
            "finished_ts": int(self.finished_ts) if self.finished_ts else 0,
            "result": self.result,
            "error": self.error,
            "elapsed_s": round((self.finished_ts or time.time()) - (self.started_ts or self.created_ts), 1),
        }


class JobManager:
    """单 worker 队列。回测是 CPU 密集型，并发跑只会互相拖慢并抢占内存。"""

    def __init__(self, *, max_history: int = 100) -> None:
        self._jobs: dict[str, Job] = {}
        self._pending: dict[str, Callable[[], Any]] = {}
        self._order: list[str] = []
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._seq = 0
        self._max_history = max_history
        self._worker = threading.Thread(target=self._loop, name="scalpx-jobs", daemon=True)
        self._worker.start()

    def submit(self, name: str, fn: Callable[[], Any]) -> str:
        with self._cond:
            self._seq += 1
            job_id = f"job-{int(time.time())}-{self._seq}"
            self._jobs[job_id] = Job(id=job_id, name=name)
            self._pending[job_id] = fn
            self._order.append(job_id)
            self._queue.append(job_id)
            self._trim()
            self._cond.notify()
        return job_id

    def _trim(self) -> None:
        while len(self._order) > self._max_history:
            old = self._order.pop(0)
            job = self._jobs.get(old)
            if job and job.status in ("queued", "running"):
                self._order.append(old)
                break
            self._jobs.pop(old, None)
            self._pending.pop(old, None)

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue:
                    self._cond.wait()
                job_id = self._queue.pop(0)
                job = self._jobs.get(job_id)
                if job is None:
                    continue
                job.status = "running"
                job.started_ts = time.time()
            fn = self._pending.pop(job_id, None)
            if fn is None:
                continue
            try:
                result = fn()
                job.status, job.result = "done", result
            except Exception as exc:  # noqa: BLE001 - 任务失败要能被前端看到
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.log.append(traceback.format_exc(limit=3))
                log.exception("后台任务失败 %s", job.name)
            finally:
                job.finished_ts = time.time()

    def get(self, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
        return job.as_dict() if job else None

    def all(self) -> list[dict]:
        with self._lock:
            return [self._jobs[i].as_dict() for i in reversed(self._order) if i in self._jobs]


# --------------------------------------------------------------------------- #
# 应用装配
# --------------------------------------------------------------------------- #
@dataclass
class WebApp:
    root: Path
    trading_db: Path
    web_db: Path
    secret_path: Path
    config_dir: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        self.web_repo = WebRepo(self.web_db)
        self.trading_repo = TradingRepo(self.trading_db)
        self.secret = load_or_create_secret(str(self.secret_path))
        self.jobs = JobManager()
        self.api = Api(
            root=self.root,
            web=self.web_repo,
            trading=self.trading_repo,
            secret=self.secret,
            submit_job=self.jobs.submit,
            jobs_of=self.jobs.get,
            jobs_all=self.jobs.all,
        )
        self.api.config_dir = Path(self.config_dir)

    def authenticate(self, token: str) -> Principal | None:
        payload = verify_token(self.secret, token)
        if payload is None:
            return None
        row = self.web_repo.get_user(int(payload.get("uid", 0)))
        if row is None or row["status"] != "active":
            return None
        if int(row["token_version"]) != int(payload.get("v", 0)):
            return None  # 改密/禁用后旧令牌立即失效
        return Principal(uid=int(row["id"]), username=row["username"], role=row["role"], version=int(row["token_version"]))

    def close(self) -> None:
        self.web_repo.close()


# --------------------------------------------------------------------------- #
# HTTP Handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "scalpx-web/1.0"
    protocol_version = "HTTP/1.1"
    app: WebApp  # 由 make_server 注入

    # ------------------------------------------------------------------ #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, code: str, message: str) -> None:
        self._json(status, fail(code, message))

    # ------------------------------------------------------------------ #
    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ApiError("PAYLOAD_TOO_LARGE", "请求体过大", 413)
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("BAD_JSON", "请求体不是合法 JSON", 400) from exc
        if not isinstance(data, dict):
            raise ApiError("BAD_JSON", "请求体必须是 JSON 对象", 400)
        return data

    def _token(self) -> str:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return ""

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    # ------------------------------------------------------------------ #
    def _serve_static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        if rel == "admin":
            rel = "admin.html"
        if not rel or rel.endswith("/"):
            rel += "index.html"
        target = (STATIC_ROOT / rel).resolve()
        if not str(target).startswith(str(STATIC_ROOT.resolve())):
            self._error(403, "FORBIDDEN", "非法路径")
            return
        if not target.is_file():
            self._error(404, "NOT_FOUND", f"资源不存在: {path}")
            return
        ctype, _ = mimetypes.guess_type(str(target))
        ctype = ctype or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    self._error(405, "METHOD_NOT_ALLOWED", "静态资源仅支持 GET")
                    return
                self._serve_static(path)
                return

            body = self._read_body() if method in ("POST", "PUT", "PATCH") else {}
            req = Request(
                method=method,
                path=path,
                query=urllib.parse.parse_qs(parsed.query),
                body=body,
                ip=self._client_ip(),
            )
            token = self._token()
            if token:
                req.principal = self.app.authenticate(token)

            response = self.app.api.dispatch(req)
            self._json(response.status, response.payload)
        except ApiError as exc:
            self._json(exc.status, fail(exc.code, exc.message))
        except BrokenPipeError:  # 客户端提前断开
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程崩溃后连接悬挂
            log.exception("未捕获异常 %s %s", method, path)
            self._error(500, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("GET")


def make_server(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    handler_cls = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler_cls)
    server.daemon_threads = True
    return server


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_app(args) -> WebApp:
    root = Path(args.root).resolve()
    return WebApp(
        root=root,
        trading_db=Path(args.db or root / "data" / "scalpx.db"),
        web_db=Path(args.web_db or root / "data" / "web.db"),
        secret_path=Path(args.secret or root / "data" / "web" / "secret.key"),
        config_dir=Path(args.config_dir or root / "config"),
    )


def cmd_serve(args) -> int:
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    app = _build_app(args)

    if app.web_repo.user_count() == 0:
        log.warning("尚未创建任何账号，请先执行: python -m scalpx.web create-user --username admin --role admin")

    try:
        server = make_server(app, args.host, args.port)
    except OSError as exc:
        log.error("监听 %s:%s 失败: %s", args.host, args.port, exc)
        return 2

    def _shutdown(signum, _frame):
        log.info("收到信号 %s，正在停止服务", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    log.info("scalpx-web 已启动: http://%s:%s  (前台 / 后台 /admin)", args.host, args.port)
    log.info("交易库: %s | 用户库: %s", app.trading_db, app.web_db)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        app.close()
        log.info("服务已停止")
    return 0


def cmd_create_user(args) -> int:
    app = _build_app(args)
    from .auth import ROLE_ADMIN, ROLE_USER, hash_password

    username = args.username
    role = args.role if args.role in (ROLE_ADMIN, ROLE_USER) else ROLE_USER
    password = args.password
    generated = False
    if not password:
        import secrets

        password = secrets.token_urlsafe(12)
        generated = True
    if app.web_repo.get_user_by_name(username) is not None:
        print(f"用户已存在: {username}")
        return 1
    try:
        pw_hash, salt = hash_password(password)
    except ValueError as exc:
        print(f"口令不合法: {exc}")
        return 2
    uid = app.web_repo.create_user(username, pw_hash, salt, role)
    app.web_repo.audit(uid=0, username="system", action="user_create", target=username, detail=role)
    print(f"已创建用户 id={uid} username={username} role={role}")
    if generated:
        print(f"初始口令（仅显示一次）: {password}")
    app.close()
    return 0


def cmd_reset_password(args) -> int:
    app = _build_app(args)
    from .auth import hash_password

    row = app.web_repo.get_user_by_name(args.username)
    if row is None:
        print(f"用户不存在: {args.username}")
        return 1
    password = args.password
    if not password:
        import secrets

        password = secrets.token_urlsafe(12)
    pw_hash, salt = hash_password(password)
    app.web_repo.update_user(int(row["id"]), password_hash=pw_hash, salt=salt, bump_version=True)
    app.web_repo.audit(uid=0, username="system", action="password_reset", target=args.username)
    print(f"已重置 {args.username} 的口令: {password}")
    print("该用户的所有旧会话已失效。")
    app.close()
    return 0


def cmd_check(args) -> int:
    """自检：给安装脚本与运维用（不启动服务）。"""
    app = _build_app(args)
    ok = True
    print(f"root          : {app.root}")
    print(f"config_dir    : {app.config_dir} ({'存在' if app.config_dir.exists() else '缺失'})")
    print(f"trading_db    : {app.trading_db} ({'存在' if app.trading_db.exists() else '未创建'})")
    print(f"web_db        : {app.web_db} ({'存在' if app.web_db.exists() else '未创建'})")
    print(f"static        : {'存在' if STATIC_ROOT.exists() else '缺失'}")
    print(f"users         : {app.web_repo.user_count()}")
    print(f"trading tables: {sum(1 for _ in app.trading_repo.table_stats().items())}")
    if not STATIC_ROOT.exists():
        ok = False
    if app.web_repo.user_count() == 0:
        ok = False
    app.close()
    print("结果: " + ("OK" if ok else "NEED_INIT"))
    return 0 if ok else 3


def main(argv: list[str] | None = None) -> int:
    import argparse

    # 全局参数同时挂在主命令与每个子命令上（子命令用 SUPPRESS 默认值，
    # 避免覆盖主命令已解析的值），这样 `--root X serve` 与 `serve --root X` 都能用。
    common = argparse.ArgumentParser(add_help=False)
    for name, help_text in (
        ("--root", "项目根目录"),
        ("--db", "交易库路径"),
        ("--web-db", "用户库路径"),
        ("--secret", "会话签名密钥路径"),
        ("--config-dir", "配置文件目录"),
    ):
        common.add_argument(name, default=argparse.SUPPRESS, help=help_text)

    parser = argparse.ArgumentParser(prog="python -m scalpx.web", description="scalpx Web 服务")
    parser.add_argument("--root", default=os.getcwd(), help="项目根目录")
    parser.add_argument("--db", default=None, help="交易库路径")
    parser.add_argument("--web-db", default=None, help="用户库路径")
    parser.add_argument("--secret", default=None, help="会话签名密钥路径")
    parser.add_argument("--config-dir", default=None, help="配置文件目录")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", parents=[common], help="启动 HTTP 服务")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--log-level", default="INFO")
    p_serve.set_defaults(func=cmd_serve)

    p_user = sub.add_parser("create-user", parents=[common], help="创建用户")
    p_user.add_argument("--username", required=True)
    p_user.add_argument("--password", default="")
    p_user.add_argument("--role", default="user", choices=["admin", "user"])
    p_user.set_defaults(func=cmd_create_user)

    p_reset = sub.add_parser("reset-password", parents=[common], help="重置口令")
    p_reset.add_argument("--username", required=True)
    p_reset.add_argument("--password", default="")
    p_reset.set_defaults(func=cmd_reset_password)

    p_check = sub.add_parser("check", parents=[common], help="自检（安装脚本使用）")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
