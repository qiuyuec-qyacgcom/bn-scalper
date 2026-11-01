"""接口层：路由表、参数校验、权限判定、响应封装。

**所有业务约束都落在这里**，页面只负责展示。三条硬规则：

1. 统一响应信封：成功 `{"ok":true,"data":...,"meta":...}`，
   失败 `{"ok":false,"error":{"code","message"}}`；HTTP 状态码与错误语义一致。
2. 权限在服务端判定（`roles` 元组），页面隐藏入口只是体验优化，不是安全边界。
3. 资源级鉴权：`user` 只能访问自己发起的运行；`admin` 不受限。
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import __version__ as core_version
from . import __version__ as web_version
from .auth import (
    ROLE_ADMIN,
    ROLE_USER,
    LoginLimiter,
    Principal,
    allowed_roles,
    hash_password,
    issue_token,
    verify_password,
)
from .repo import TradingRepo, WebRepo
from .tomlio import dumps as toml_dumps

# --------------------------------------------------------------------------- #
# 基础类型
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(slots=True)
class Request:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    principal: Principal | None = None
    ip: str = ""

    def q(self, key: str, default: str = "") -> str:
        values = self.query.get(key) or []
        return values[0] if values else default

    def q_int(self, key: str, default: int, *, lo: int = 0, hi: int = 1000) -> int:
        raw = self.q(key)
        if raw == "":
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ApiError("BAD_QUERY", f"参数 {key} 必须是整数") from exc
        return max(lo, min(hi, value))

    def require(self, key: str, *, max_len: int = 256) -> str:
        value = self.body.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ApiError("BAD_REQUEST", f"缺少字段 {key}")
        if len(value) > max_len:
            raise ApiError("BAD_REQUEST", f"字段 {key} 过长")
        return value.strip()


@dataclass(slots=True)
class Response:
    status: int
    payload: dict[str, Any]


def ok(data: Any, **meta: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "data": data}
    if meta:
        payload["meta"] = meta
    return payload


def fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


# --------------------------------------------------------------------------- #
# 路由表：(method, regex, handler, roles)  roles=None 表示免登录
# --------------------------------------------------------------------------- #
Route = tuple[str, re.Pattern[str], str, tuple[str, ...] | None]

ROUTES: list[Route] = [
    ("GET", re.compile(r"^/api/health$"), "health", None),
    ("POST", re.compile(r"^/api/auth/login$"), "login", None),
    ("POST", re.compile(r"^/api/auth/logout$"), "logout", (ROLE_ADMIN, ROLE_USER)),
    ("GET", re.compile(r"^/api/auth/me$"), "me", (ROLE_ADMIN, ROLE_USER)),
    ("GET", re.compile(r"^/api/dashboard$"), "dashboard", (ROLE_ADMIN, ROLE_USER)),
    ("GET", re.compile(r"^/api/runs$"), "runs", (ROLE_ADMIN, ROLE_USER)),
    ("GET", re.compile(r"^/api/runs/(?P<run_id>[^/]+)$"), "run_detail", (ROLE_ADMIN, ROLE_USER)),
    ("GET", re.compile(r"^/api/trades$"), "trades", (ROLE_ADMIN, ROLE_USER)),
    ("GET", re.compile(r"^/api/signals$"), "signals", (ROLE_ADMIN, ROLE_USER)),
    ("GET", re.compile(r"^/api/micro$"), "micro", (ROLE_ADMIN, ROLE_USER)),
    ("POST", re.compile(r"^/api/backtest$"), "run_backtest", (ROLE_ADMIN, ROLE_USER)),
    # --- 后台 ---
    ("GET", re.compile(r"^/api/admin/users$"), "users_list", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/users$"), "users_create", (ROLE_ADMIN,)),
    ("PATCH", re.compile(r"^/api/admin/users/(?P<uid>\d+)$"), "users_update", (ROLE_ADMIN,)),
    ("DELETE", re.compile(r"^/api/admin/users/(?P<uid>\d+)$"), "users_delete", (ROLE_ADMIN,)),
    ("GET", re.compile(r"^/api/admin/system$"), "system", (ROLE_ADMIN,)),
    ("GET", re.compile(r"^/api/admin/config$"), "config_get", (ROLE_ADMIN,)),
    ("PUT", re.compile(r"^/api/admin/config$"), "config_put", (ROLE_ADMIN,)),
    ("GET", re.compile(r"^/api/admin/audit$"), "audit", (ROLE_ADMIN,)),
    ("GET", re.compile(r"^/api/admin/jobs$"), "jobs_list", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/data/download$"), "data_download", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/risk$"), "risk_control", (ROLE_ADMIN,)),
    ("DELETE", re.compile(r"^/api/admin/runs/(?P<run_id>[^/]+)$"), "run_delete", (ROLE_ADMIN,)),
]


# --------------------------------------------------------------------------- #
# Api
# --------------------------------------------------------------------------- #
class Api:
    def __init__(
        self,
        *,
        root: Path,
        web: WebRepo,
        trading: TradingRepo,
        secret: bytes,
        submit_job: Callable[[str, Callable[[], Any]], str],
        jobs_of: Callable[[str], dict | None],
        jobs_all: Callable[[], list[dict]],
    ) -> None:
        self.root = Path(root).resolve()
        self.web = web
        self.trading = trading
        self.secret = secret
        self.limiter = LoginLimiter()
        self._submit_job = submit_job
        self._jobs_of = jobs_of
        self._jobs_all = jobs_all
        self.started_ts = time.time()
        self.config_dir = self.root / "config"
        self.kill_file = self.root / "data" / "KILL"

    # ------------------------------------------------------------------ #
    # 分发
    # ------------------------------------------------------------------ #
    def dispatch(self, req: Request) -> Response:
        for method, pattern, handler_name, roles in ROUTES:
            if method != req.method:
                continue
            match = pattern.match(req.path)
            if not match:
                continue
            req.params = {k: v for k, v in match.groupdict().items()}
            if roles is not None:
                if req.principal is None:
                    raise ApiError("UNAUTHORIZED", "请先登录", 401)
                if not allowed_roles(req.principal.role, roles):
                    raise ApiError("FORBIDDEN", "无权访问该资源", 403)
            handler = getattr(self, f"h_{handler_name}")
            return Response(200, ok(handler(req)))
        raise ApiError("NOT_FOUND", f"接口不存在: {req.method} {req.path}", 404)

    # ------------------------------------------------------------------ #
    # 认证
    # ------------------------------------------------------------------ #
    def h_health(self, req: Request) -> dict:
        return {
            "status": "ok",
            "service": "scalpx-web",
            "version": web_version,
            "core": core_version,
            "uptime_s": int(time.time() - self.started_ts),
            "trading_db": self.trading.available(),
        }

    def h_login(self, req: Request) -> dict:
        username = req.require("username", max_len=64)
        password = req.require("password", max_len=256)
        locked = self.limiter.check(req.ip, username)
        if locked:
            raise ApiError("LOCKED", f"登录失败次数过多，请 {locked} 秒后再试", 429)

        row = self.web.get_user_by_name(username)
        if row is None or row["status"] != "active" or not verify_password(password, row["salt"], row["password_hash"]):
            self.limiter.record_failure(req.ip, username)
            self.web.audit(uid=0, username=username, action="login_failed", ip=req.ip)
            raise ApiError("BAD_CREDENTIALS", "用户名或口令错误", 401)

        self.limiter.reset(req.ip, username)
        self.web.touch_login(int(row["id"]))
        token = issue_token(
            self.secret,
            uid=int(row["id"]),
            username=row["username"],
            role=row["role"],
            version=int(row["token_version"]),
        )
        self.web.audit(uid=int(row["id"]), username=row["username"], action="login", ip=req.ip)
        return {
            "token": token,
            "expires_in": 12 * 3600,
            "user": {"id": row["id"], "username": row["username"], "role": row["role"]},
        }

    def h_logout(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        self.web.audit(uid=p.uid, username=p.username, action="logout", ip=req.ip)
        return {"logged_out": True}

    def h_me(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        row = self.web.get_user(p.uid)
        if row is None:
            raise ApiError("UNAUTHORIZED", "账号不存在", 401)
        return {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "last_login_ts": row["last_login_ts"],
            "capabilities": self._capabilities(p.role),
        }

    @staticmethod
    def _capabilities(role: str) -> list[str]:
        base = ["view_dashboard", "view_runs", "view_trades", "run_backtest"]
        if role == ROLE_ADMIN:
            base += ["manage_users", "manage_config", "manage_data", "control_risk", "view_audit", "delete_runs"]
        return base

    # ------------------------------------------------------------------ #
    # 鉴权辅助
    # ------------------------------------------------------------------ #
    def _assert_run_access(self, req: Request, run_id: str) -> None:
        p = req.principal
        assert p is not None
        if p.is_admin:
            return
        owner = self.web.run_owners().get(run_id)
        if owner is not None and owner != p.uid:
            raise ApiError("FORBIDDEN", "无权访问他人的运行", 403)

    def _resolve_run(self, req: Request) -> str:
        """取 run_id；未指定时用可见范围内最新的一次运行。"""
        run_id = req.q("run_id")
        if run_id:
            self._assert_run_access(req, run_id)
            return run_id
        p = req.principal
        assert p is not None
        owners = self.web.run_owners()
        only = None if p.is_admin else [rid for rid, uid in owners.items() if uid == p.uid]
        rows = self.trading.runs(limit=1, only_ids=only)
        if not rows:
            raise ApiError("NO_DATA", "暂无回测数据，请先运行一次回测", 404)
        return str(rows[0]["run_id"])

    # ------------------------------------------------------------------ #
    # 前台
    # ------------------------------------------------------------------ #
    def h_dashboard(self, req: Request) -> dict:
        run_id = self._resolve_run(req)
        run = self.trading.run(run_id)
        if run is None:
            raise ApiError("NOT_FOUND", f"运行不存在: {run_id}", 404)

        metrics: dict[str, Any] = {}
        if run.get("metrics_json"):
            try:
                metrics = json.loads(run["metrics_json"])
            except json.JSONDecodeError:
                metrics = {}

        equity = self.trading.equity(run_id)
        trades = self.trading.trades(run_id, limit=20)
        exits = self.trading.exit_breakdown(run_id)
        risk = self.trading.risk_events(run_id, limit=8)

        return {
            "run": {
                "run_id": run["run_id"],
                "mode": run.get("mode"),
                "strategy": run.get("strategy"),
                "symbols": (run.get("symbols") or "").split(","),
                "interval": run.get("interval"),
                "started_ts": run.get("started_ts"),
                "finished_ts": run.get("finished_ts"),
                "status": run.get("status"),
            },
            "kpi": {
                "total_return": metrics.get("total_return", 0.0),
                "max_drawdown": metrics.get("max_drawdown", 0.0),
                "win_rate": metrics.get("win_rate", 0.0),
                "avg_r": metrics.get("avg_r", 0.0),
                "trades": metrics.get("trades", 0.0),
                "profit_factor": metrics.get("profit_factor", 0.0),
                "sharpe": metrics.get("sharpe", 0.0),
                "fee_drag_pct": metrics.get("fee_drag_pct", 0.0),
                "final_equity": metrics.get("final_equity", 0.0),
            },
            "equity": equity,
            "recent_trades": [
                {
                    "symbol": t["symbol"],
                    "side": t["side"],
                    "qty": t["qty"],
                    "entry_price": t["entry_price"],
                    "exit_price": t["exit_price"],
                    "entry_ts": t["entry_ts"],
                    "exit_ts": t["exit_ts"],
                    "pnl": t["pnl"],
                    "r_multiple": t["r_multiple"],
                    "reason": t["reason"],
                }
                for t in trades
            ],
            "exit_breakdown": exits,
            "risk_events": [
                {"ts": e["ts"], "level": e["level"], "code": e["code"], "message": e["message"]}
                for e in risk
            ],
        }

    def h_runs(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        limit = req.q_int("limit", 20, lo=1, hi=100)
        offset = req.q_int("offset", 0, lo=0, hi=100000)
        owners = self.web.run_owners()
        only = None if p.is_admin else [rid for rid, uid in owners.items() if uid == p.uid]
        rows = self.trading.runs(limit=limit, offset=offset, only_ids=only)
        out = []
        for r in rows:
            metrics = {}
            if r.get("metrics_json"):
                try:
                    metrics = json.loads(r["metrics_json"])
                except json.JSONDecodeError:
                    pass
            out.append(
                {
                    "run_id": r["run_id"],
                    "mode": r["mode"],
                    "strategy": r["strategy"],
                    "symbols": (r["symbols"] or "").split(","),
                    "interval": r["interval"],
                    "started_ts": r["started_ts"],
                    "status": r["status"],
                    "total_return": metrics.get("total_return"),
                    "trades": metrics.get("trades"),
                }
            )
        return {"items": out, "limit": limit, "offset": offset}

    def h_run_detail(self, req: Request) -> dict:
        run_id = req.params["run_id"]
        self._assert_run_access(req, run_id)
        run = self.trading.run(run_id)
        if run is None:
            raise ApiError("NOT_FOUND", "运行不存在", 404)
        try:
            config = json.loads(run.get("config_json") or "{}")
        except json.JSONDecodeError:
            config = {}
        try:
            metrics = json.loads(run.get("metrics_json") or "{}")
        except json.JSONDecodeError:
            metrics = {}
        return {"run": run, "metrics": metrics, "config": config}

    def h_trades(self, req: Request) -> dict:
        run_id = self._resolve_run(req)
        limit = req.q_int("limit", 50, lo=1, hi=500)
        offset = req.q_int("offset", 0, lo=0, hi=1000000)
        symbol = req.q("symbol")
        rows = self.trading.trades(run_id, limit=limit, offset=offset, symbol=symbol)
        return {
            "items": rows,
            "limit": limit,
            "offset": offset,
            "total": self.trading.trade_count(run_id),
        }

    def h_signals(self, req: Request) -> dict:
        run_id = self._resolve_run(req)
        limit = req.q_int("limit", 30, lo=1, hi=200)
        rows = self.trading.signals(run_id, limit=limit)
        for r in rows:
            try:
                r["meta"] = json.loads(r.pop("meta_json") or "{}")
            except json.JSONDecodeError:
                r["meta"] = {}
        return {"items": rows}

    def h_micro(self, req: Request) -> dict:
        stats = self.trading.flow_stats()
        symbol = req.q("symbol") or (self._symbols_hint() or "BTCUSDT")
        series = self.trading.cvd_series(symbol, limit=req.q_int("limit", 300, lo=10, hi=1000))
        return {"flow": stats, "symbol": symbol, "cvd": series}

    def _symbols_hint(self) -> str:
        rows = self.trading.runs(limit=1)
        if not rows:
            return ""
        parts = (rows[0].get("symbols") or "").split(",")
        return parts[0] if parts else ""

    def h_run_backtest(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        profile = str(req.body.get("profile") or "backtest")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", profile):
            raise ApiError("BAD_REQUEST", "非法的配置名")

        # 配额：普通用户默认 10 次/日，管理员不限
        limit = 0 if p.is_admin else int(self.web.get_setting("user_backtest_daily", 10))
        allowed, used = self.web.consume_quota(p.uid, limit)
        if not allowed:
            raise ApiError("QUOTA_EXCEEDED", f"今日回测次数已用完（{used}/{limit}）", 429)

        cfg_path = self.config_dir / f"{profile}.toml"
        if not cfg_path.exists():
            raise ApiError("NOT_FOUND", f"配置不存在: {cfg_path.name}", 404)

        overrides = req.body.get("params") or {}
        if not isinstance(overrides, dict):
            raise ApiError("BAD_REQUEST", "params 必须是对象")

        owner = p.uid
        username = p.username

        def _job() -> dict:
            return self._execute_backtest(cfg_path, overrides, owner)

        job_id = self._submit_job(f"backtest:{profile}", _job)
        self.web.audit(
            uid=p.uid, username=username, action="backtest_submit",
            target=profile, detail=json.dumps(overrides, ensure_ascii=False), ip=req.ip,
        )
        return {"job_id": job_id, "profile": profile, "used_today": used, "quota": limit}

    def _execute_backtest(self, cfg_path: Path, overrides: dict[str, Any], owner: int) -> dict:
        from dataclasses import replace

        from ..config import load_config
        from ..data.feed import build_feed
        from ..data.store import Store
        from ..engine.backtest import BacktestEngine
        from ..models import now_ms
        from ..strategy.registry import create_strategy

        cfg = load_config(cfg_path)
        if overrides:
            params = {**cfg.strategy.params, **overrides}
            cfg = replace(cfg, strategy=replace(cfg.strategy, params=params))

        run_id = f"web-{cfg.strategy.name}-{now_ms()}"
        strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
        feed = build_feed(cfg)
        store = Store(cfg.data.sqlite_path, run_id=run_id, persist=cfg.data.persist)
        try:
            engine = BacktestEngine(
                cfg, strategy, feed, store=store if cfg.data.persist else None, run_id=run_id
            )
            result = engine.run()
        finally:
            store.close()

        self.web.set_run_owner(run_id, owner)
        return {"run_id": run_id, "metrics": result.metrics, "trades": len(result.trades)}

    # ------------------------------------------------------------------ #
    # 后台：用户与权限
    # ------------------------------------------------------------------ #
    def h_users_list(self, req: Request) -> dict:
        return {"items": self.web.list_users(), "roles": [ROLE_ADMIN, ROLE_USER]}

    def h_users_create(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        username = req.require("username", max_len=64)
        password = req.require("password", max_len=256)
        role = str(req.body.get("role") or ROLE_USER)
        if role not in (ROLE_ADMIN, ROLE_USER):
            raise ApiError("BAD_REQUEST", "非法角色")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            raise ApiError("BAD_REQUEST", "用户名只能包含字母数字与 _.-，长度 3-32")
        if self.web.get_user_by_name(username) is not None:
            raise ApiError("CONFLICT", "用户名已存在", 409)
        try:
            pw_hash, salt = hash_password(password)
        except ValueError as exc:
            raise ApiError("BAD_REQUEST", str(exc)) from exc
        uid = self.web.create_user(username, pw_hash, salt, role)
        self.web.audit(uid=p.uid, username=p.username, action="user_create", target=username, detail=role, ip=req.ip)
        return {"id": uid, "username": username, "role": role}

    def h_users_update(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        uid = int(req.params["uid"])
        row = self.web.get_user(uid)
        if row is None:
            raise ApiError("NOT_FOUND", "用户不存在", 404)

        role = req.body.get("role")
        status = req.body.get("status")
        password = req.body.get("password")

        if role is not None and role not in (ROLE_ADMIN, ROLE_USER):
            raise ApiError("BAD_REQUEST", "非法角色")
        if status is not None and status not in ("active", "disabled"):
            raise ApiError("BAD_REQUEST", "非法状态")

        # 防止把最后一个管理员降级/禁用，导致后台永久锁死
        if (role == ROLE_USER or status == "disabled") and row["role"] == ROLE_ADMIN:
            if self._count_active_admins() <= 1:
                raise ApiError("CONFLICT", "至少保留一个启用的管理员", 409)

        pw_hash = salt = None
        if password:
            try:
                pw_hash, salt = hash_password(str(password))
            except ValueError as exc:
                raise ApiError("BAD_REQUEST", str(exc)) from exc

        changed = self.web.update_user(
            uid,
            role=role,
            status=status,
            password_hash=pw_hash,
            salt=salt,
            bump_version=bool(password or status == "disabled"),
        )
        if not changed:
            raise ApiError("BAD_REQUEST", "没有可更新的字段")
        self.web.audit(
            uid=p.uid, username=p.username, action="user_update", target=row["username"],
            detail=json.dumps({"role": role, "status": status, "password": bool(password)}), ip=req.ip,
        )
        return {"id": uid, "updated": True}

    def h_users_delete(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        uid = int(req.params["uid"])
        if uid == p.uid:
            raise ApiError("CONFLICT", "不能删除当前登录账号", 409)
        row = self.web.get_user(uid)
        if row is None:
            raise ApiError("NOT_FOUND", "用户不存在", 404)
        if row["role"] == ROLE_ADMIN and self._count_active_admins() <= 1:
            raise ApiError("CONFLICT", "至少保留一个启用的管理员", 409)
        self.web.delete_user(uid)
        self.web.audit(uid=p.uid, username=p.username, action="user_delete", target=row["username"], ip=req.ip)
        return {"id": uid, "deleted": True}

    def _count_active_admins(self) -> int:
        return sum(1 for u in self.web.list_users() if u["role"] == ROLE_ADMIN and u["status"] == "active")

    # ------------------------------------------------------------------ #
    # 后台：配置
    # ------------------------------------------------------------------ #
    def h_config_get(self, req: Request) -> dict:
        profiles = sorted(p.stem for p in self.config_dir.glob("*.toml"))
        if not profiles:
            raise ApiError("NO_DATA", "未找到任何配置文件", 404)
        profile = req.q("profile") or ("backtest" if "backtest" in profiles else profiles[0])
        path = self.config_dir / f"{profile}.toml"
        if not path.exists():
            raise ApiError("NOT_FOUND", f"配置不存在: {profile}", 404)
        import tomllib

        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ApiError("CONFIG_INVALID", f"读取配置失败: {exc}", 500) from exc
        return {"profile": profile, "profiles": profiles, "data": data, "path": str(path.name)}

    def h_config_put(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        profile = str(req.body.get("profile") or "")
        data = req.body.get("data")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", profile):
            raise ApiError("BAD_REQUEST", "非法的配置名")
        if not isinstance(data, dict):
            raise ApiError("BAD_REQUEST", "data 必须是对象")

        path = self.config_dir / f"{profile}.toml"
        if not path.exists():
            raise ApiError("NOT_FOUND", f"配置不存在: {profile}", 404)

        # 先写临时文件并用正式加载器校验，通过后才原子替换 —— 绝不写坏生产配置
        tmp = path.with_suffix(".toml.tmp")
        try:
            tmp.write_text(toml_dumps(data), encoding="utf-8")
            from ..config import load_config

            load_config(tmp)
        except Exception as exc:  # noqa: BLE001 - 校验失败要原样回给前端
            tmp.unlink(missing_ok=True)
            raise ApiError("CONFIG_INVALID", f"配置校验失败：{exc}", 400) from exc

        backup = path.with_suffix(f".toml.bak.{int(time.time())}")
        try:
            shutil.copy2(path, backup)
            os.replace(tmp, path)
        except OSError as exc:
            raise ApiError("IO_ERROR", f"写入配置失败: {exc}", 500) from exc

        self.web.audit(uid=p.uid, username=p.username, action="config_update", target=profile, ip=req.ip)
        return {"profile": profile, "saved": True, "backup": backup.name}

    # ------------------------------------------------------------------ #
    # 后台：运维
    # ------------------------------------------------------------------ #
    def h_system(self, req: Request) -> dict:
        stats = self.trading.table_stats()
        flow = self.trading.flow_stats()
        return {
            "service": {
                "version": web_version,
                "core_version": core_version,
                "python": sys.version.split()[0],
                "platform": f"{platform.system()} {platform.release()}",
                "uptime_s": int(time.time() - self.started_ts),
                "cwd": str(self.root),
            },
            "storage": {
                "trading_db": str(self.trading.path),
                "trading_db_size": self.trading.db_size(),
                "web_db": str(self.web.path),
                "web_db_size": self.web.path.stat().st_size if self.web.path.exists() else 0,
                "tables": stats,
            },
            "microstructure": flow,
            "risk": {"kill_switch": self.kill_file.exists()},
            "configs": sorted(p.stem for p in self.config_dir.glob("*.toml")),
            "jobs": self._jobs_all()[:20],
        }

    def h_audit(self, req: Request) -> dict:
        limit = req.q_int("limit", 100, lo=1, hi=500)
        offset = req.q_int("offset", 0, lo=0, hi=100000)
        return {"items": self.web.list_audit(limit, offset), "limit": limit, "offset": offset}

    def h_jobs_list(self, req: Request) -> dict:
        return {"items": self._jobs_all()[:50]}

    def h_data_download(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        kind = str(req.body.get("kind") or "kline")
        symbol = req.require("symbol", max_len=24).upper()
        interval = str(req.body.get("interval") or "1m")
        start = req.require("start", max_len=32)
        end = req.require("end", max_len=32)
        if kind not in ("kline", "flow"):
            raise ApiError("BAD_REQUEST", "kind 只能是 kline 或 flow")
        if not re.fullmatch(r"[A-Z0-9]{5,20}", symbol):
            raise ApiError("BAD_REQUEST", "非法的交易对")

        cfg_path = self.config_dir / "backtest.toml"
        if not cfg_path.exists():
            raise ApiError("NOT_FOUND", "缺少 backtest.toml", 404)

        def _job() -> dict:
            return self._execute_download(cfg_path, kind, symbol, interval, start, end)

        job_id = self._submit_job(f"download:{kind}:{symbol}", _job)
        self.web.audit(
            uid=p.uid, username=p.username, action="data_download",
            target=f"{symbol} {interval} {start}~{end}", detail=kind, ip=req.ip,
        )
        return {"job_id": job_id, "kind": kind, "symbol": symbol}

    def _execute_download(self, cfg_path: Path, kind: str, symbol: str, interval: str, start: str, end: str) -> dict:
        from ..config import load_config
        from ..data.binance_rest import BinanceRest
        from ..data.flow_source import write_flow_csv
        from ..models import iso_to_ts, ts_to_iso

        cfg = load_config(cfg_path)
        client = BinanceRest(base_url=cfg.exchange.base_url)
        start_ms, end_ms = iso_to_ts(start), iso_to_ts(end)
        out_dir = Path(cfg.data.csv_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if kind == "kline":
            import csv

            bars = client.download_klines(symbol, interval, start_ms, end_ms, bar_ms=cfg.market.bar_ms)
            path = out_dir / f"{symbol}_{interval}.csv"
            with path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    ["ts", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_base"]
                )
                for b in bars:
                    writer.writerow(
                        [b.ts, b.open, b.high, b.low, b.close, b.volume, b.quote_volume, b.trades, b.taker_buy_base]
                    )
            n = len(bars)
        else:
            flows = client.download_flow(
                symbol, interval, start_ms, end_ms,
                bar_ms=cfg.market.bar_ms, large_trade_usd=cfg.microstructure.large_trade_usd,
            )
            path = out_dir / f"{symbol}_{interval}_flow.csv"
            n = write_flow_csv(path, flows)

        return {"symbol": symbol, "rows": n, "file": path.name, "range": f"{ts_to_iso(start_ms)}~{ts_to_iso(end_ms)}"}

    def h_risk_control(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        action = str(req.body.get("action") or "")
        reason = str(req.body.get("reason") or "")
        if action not in ("halt", "resume"):
            raise ApiError("BAD_REQUEST", "action 只能是 halt 或 resume")

        self.kill_file.parent.mkdir(parents=True, exist_ok=True)
        if action == "halt":
            self.kill_file.write_text(
                json.dumps({"ts": int(time.time()), "by": p.username, "reason": reason}, ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            self.kill_file.unlink(missing_ok=True)

        self.web.audit(
            uid=p.uid, username=p.username, action=f"risk_{action}", target="kill_switch",
            detail=reason, ip=req.ip,
        )
        return {"action": action, "kill_switch": self.kill_file.exists(), "reason": reason}

    def h_run_delete(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        run_id = req.params["run_id"]
        run = self.trading.run(run_id)
        if run is None:
            raise ApiError("NOT_FOUND", "运行不存在", 404)
        deleted = self.trading.delete_run(run_id)
        if not deleted:
            raise ApiError("IO_ERROR", "删除失败（数据库可能只读）", 500)
        self.web.audit(uid=p.uid, username=p.username, action="run_delete", target=run_id, ip=req.ip)
        return {"run_id": run_id, "deleted": deleted}
