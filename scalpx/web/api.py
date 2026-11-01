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
from .ai import AIAssistant, validate_plan
from .generic import EntityError, EntityStore
from .instruments import (
    canonical_key,
    parse_batch_text,
    type_options,
    validate_instrument,
)
from .repo import TradingRepo, WebRepo
from .schema import ENTITIES, get_entity
from .tomlio import dumps as toml_dumps

# --------------------------------------------------------------------------- #
# 基础类型
# --------------------------------------------------------------------------- #


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


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


def fail(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"ok": False, "error": error}


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
    # --- 标的库 ---
    ("GET", re.compile(r"^/api/instruments$"), "instruments", (ROLE_ADMIN, ROLE_USER)),
    ("GET", re.compile(r"^/api/admin/instruments$"), "instruments_admin", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/instruments$"), "instruments_create", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/instruments/validate$"), "instruments_validate", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/instruments/batch$"), "instruments_batch", (ROLE_ADMIN,)),
    ("PUT", re.compile(r"^/api/admin/instruments/order$"), "instruments_reorder", (ROLE_ADMIN,)),
    ("PATCH", re.compile(r"^/api/admin/instruments/(?P<iid>\d+)$"), "instruments_update", (ROLE_ADMIN,)),
    ("DELETE", re.compile(r"^/api/admin/instruments/(?P<iid>\d+)$"), "instruments_delete", (ROLE_ADMIN,)),
    # --- 通用实体管理（schema 驱动） ---
    ("GET", re.compile(r"^/api/admin/entities$"), "entities_catalog", (ROLE_ADMIN,)),
    ("GET", re.compile(r"^/api/admin/entities/(?P<entity>[a-z_]+)$"), "entities_list", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/entities/(?P<entity>[a-z_]+)$"), "entities_create", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/entities/(?P<entity>[a-z_]+)/validate$"), "entities_validate", (ROLE_ADMIN,)),
    ("PATCH", re.compile(r"^/api/admin/entities/(?P<entity>[a-z_]+)/(?P<rid>\d+)$"), "entities_update", (ROLE_ADMIN,)),
    ("DELETE", re.compile(r"^/api/admin/entities/(?P<entity>[a-z_]+)/(?P<rid>\d+)$"), "entities_delete", (ROLE_ADMIN,)),
    # --- AI 辅助 ---
    ("POST", re.compile(r"^/api/admin/ai/suggest$"), "ai_suggest", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/ai/fix$"), "ai_fix", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/ai/command$"), "ai_command", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/ai/apply$"), "ai_apply", (ROLE_ADMIN,)),
    ("POST", re.compile(r"^/api/admin/ai/explain$"), "ai_explain", (ROLE_ADMIN,)),
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
        self.store = EntityStore(web)
        self.ai = AIAssistant()
        self._register_dynamic_options()

    def _register_dynamic_options(self) -> None:
        """把运行时可枚举的选项注入 schema（策略名等），避免硬编码。"""
        spec = ENTITIES.get("strategy_instances")
        if spec is None:
            return
        field = spec.field_spec("strategy")
        if field is None:
            return
        try:
            # 从包级导出导入，触发策略注册（registry 单独导入不会执行注册）
            from ..strategy import available

            names = list(available())
        except Exception:  # noqa: BLE001 - 策略注册表异常不应拖垮后台
            names = []
        if names:
            field.options = [{"value": n, "label": n} for n in names]

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
        """健康检查。

        不只返回 "ok"：把安装脚本与监控真正关心的几项拆开暴露，
        这样 `curl /api/health` 就能定位"到底是库坏了、静态资源丢了、还是配置目录不见了"。
        """
        static_dir = Path(__file__).parent / "static"
        checks = {
            "web_db": self._check_web_db(),
            "static": (static_dir / "index.html").is_file() and (static_dir / "admin.html").is_file(),
            "config": self.config_dir.is_dir(),
            "trading_db": self.trading.available(),
        }
        # blocking 项不通过即视为不健康，wall 侧其余项只降级
        healthy = checks["web_db"] and checks["static"] and checks["config"]
        return {
            "status": "ok" if healthy else "degraded",
            "service": "scalpx-web",
            "version": web_version,
            "core": core_version,
            "uptime_s": int(time.time() - self.started_ts),
            "trading_db": checks["trading_db"],
            "checks": checks,
            "users": self.web.user_count(),
        }

    def _check_web_db(self) -> bool:
        try:
            self.web.connection.execute("SELECT 1").fetchone()
            return True
        except Exception:  # noqa: BLE001 - 健康检查不应抛异常
            return False

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

        selection = self._resolve_symbols(req, cfg_path)

        owner = p.uid
        username = p.username

        def _job() -> dict:
            return self._execute_backtest(cfg_path, overrides, owner, selection)

        job_id = self._submit_job(f"backtest:{profile}", _job)
        self.web.audit(
            uid=p.uid, username=username, action="backtest_submit",
            target=profile,
            detail=json.dumps(
                {"params": overrides, "symbols": [s["code"] for s in selection] if selection else None},
                ensure_ascii=False,
            ),
            ip=req.ip,
        )
        return {
            "job_id": job_id,
            "profile": profile,
            "used_today": used,
            "quota": limit,
            "symbols": [s["code"] for s in selection] if selection else None,
        }

    # ------------------------------------------------------------------ #
    # 标的库
    # ------------------------------------------------------------------ #
    def _resolve_symbols(self, req: Request, cfg_path: Path) -> list[dict] | None:
        """把前端选择的标的代码解析成标的记录，并校验"可回测性"。

        两道校验缺一不可：
        1. 必须在标的库里注册且启用；
        2. 本地必须有对应的行情文件——注册了不等于有数据，否则回测会静默跑出 0 笔。
        """
        raw = req.body.get("symbols")
        if raw in (None, [], ""):
            return None
        if not isinstance(raw, list) or not all(isinstance(s, str) and s.strip() for s in raw):
            raise ApiError("BAD_REQUEST", "symbols 必须是字符串数组")
        codes = [s.strip().upper() for s in raw]
        if len(codes) > 10:
            raise ApiError("BAD_REQUEST", "单次最多选择 10 个标的")
        if len(set(codes)) != len(codes):
            raise ApiError("BAD_REQUEST", "symbols 存在重复项")

        registry = {i["code"]: i for i in self.web.list_instruments(active_only=True)}
        missing = [c for c in codes if c not in registry]
        if missing:
            raise ApiError(
                "INSTRUMENT_NOT_FOUND",
                f"未注册或已停用：{', '.join(missing)}。请先在「标的库」中登记。",
                404,
                details={"missing": missing, "registered": sorted(registry)},
            )

        items = [registry[c] for c in codes]
        ticks = {round(float(i["price_tick"]), 12) for i in items}
        steps = {round(float(i["qty_step"]), 12) for i in items}
        if len(ticks) > 1 or len(steps) > 1:
            raise ApiError(
                "TICK_MISMATCH",
                "所选标的的 price_tick / qty_step 不一致；引擎一次运行只支持同一套精度。"
                "请分批回测，或统一这两项属性。",
                400,
            )

        from ..config import load_config

        try:
            cfg = load_config(cfg_path)
        except Exception as exc:  # noqa: BLE001 - 配置错误要原样回给前端
            raise ApiError("CONFIG_INVALID", f"读取配置失败：{exc}", 500) from exc

        csv_dir = Path(cfg.data.csv_dir)
        no_data = [c for c in codes if not (csv_dir / f"{c}_{cfg.market.interval}.csv").exists()]
        if no_data:
            raise ApiError(
                "DATA_MISSING",
                f"本地缺少行情数据：{', '.join(no_data)}（{cfg.market.interval}）。请先在「数据管理」中下载。",
                400,
                details={"missing": no_data, "csv_dir": str(csv_dir)},
            )
        return items

    def h_instruments(self, req: Request) -> dict:
        items = self.web.list_instruments(
            asset_type=req.q("type"), query=req.q("q"), active_only=req.q("active_only") == "1"
        )
        return {
            "items": [
                {
                    "id": i["id"],
                    "code": i["code"],
                    "name": i["name"],
                    "asset_type": i["asset_type"],
                    "market": i["market"],
                    "currency": i["currency"],
                    "is_active": i["is_active"],
                }
                for i in items
            ],
            "types": type_options(),
            "total": len(items),
        }

    def h_instruments_admin(self, req: Request) -> dict:
        items = self.web.list_instruments(
            asset_type=req.q("type"), query=req.q("q")
        )
        return {"items": items, "types": type_options(), "total": len(items)}

    def h_instruments_validate(self, req: Request) -> dict:
        result = validate_instrument(req.body, partial=bool(req.body.get("_partial")))
        payload = result.as_dict()
        if result.ok:
            existing = self.web.find_instrument(result.data["symbol_key"])
            payload["duplicate"] = existing is not None
            payload["existing"] = (
                {"id": existing["id"], "code": existing["code"], "name": existing["name"]}
                if existing
                else None
            )
        return payload

    def h_instruments_create(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        result = validate_instrument(req.body)
        if not result.ok:
            raise ApiError(
                "VALIDATION_FAILED",
                "；".join(f"{i.field}: {i.message}" for i in result.issues),
                400,
                details=[i.as_dict() for i in result.issues],
            )
        existing = self.web.find_instrument(result.data["symbol_key"])
        if existing is not None:
            raise ApiError(
                "DUPLICATE",
                f"标的已存在：{existing['code']} {existing['name']}（{existing['asset_type']}/{existing['market']}）",
                409,
                details={"existing": {"id": existing["id"], "code": existing["code"]}},
            )
        iid = self.web.create_instrument(result.data)
        self.web.audit(
            uid=p.uid, username=p.username, action="instrument_create",
            target=result.data["symbol_key"], detail=result.data["name"], ip=req.ip,
        )
        return {"id": iid, "item": self.web.get_instrument(iid)}

    def h_instruments_update(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        iid = int(req.params["iid"])
        current = self.web.get_instrument(iid)
        if current is None:
            raise ApiError("NOT_FOUND", "标的不存在", 404)

        merged = {**current, **req.body}
        result = validate_instrument(merged)
        if not result.ok:
            raise ApiError(
                "VALIDATION_FAILED",
                "；".join(f"{i.field}: {i.message}" for i in result.issues),
                400,
                details=[i.as_dict() for i in result.issues],
            )
        if result.data["symbol_key"] != current["symbol_key"]:
            other = self.web.find_instrument(result.data["symbol_key"])
            if other is not None:
                raise ApiError(
                    "DUPLICATE",
                    f"与已有标的冲突：{other['code']} {other['name']}",
                    409,
                    details={"existing": {"id": other["id"], "code": other["code"]}},
                )
        self.web.update_instrument(iid, result.data)
        self.web.audit(
            uid=p.uid, username=p.username, action="instrument_update",
            target=result.data["symbol_key"], ip=req.ip,
        )
        return {"id": iid, "item": self.web.get_instrument(iid)}

    def h_instruments_delete(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        iid = int(req.params["iid"])
        current = self.web.get_instrument(iid)
        if current is None:
            raise ApiError("NOT_FOUND", "标的不存在", 404)
        self.web.delete_instrument(iid)
        self.web.audit(
            uid=p.uid, username=p.username, action="instrument_delete",
            target=current["symbol_key"], ip=req.ip,
        )
        return {"id": iid, "deleted": True, "code": current["code"]}

    def h_instruments_reorder(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        ids = req.body.get("ids")
        if not isinstance(ids, list) or not ids:
            raise ApiError("BAD_REQUEST", "ids 必须是非空数组")
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError) as exc:
            raise ApiError("BAD_REQUEST", "ids 必须是整数数组") from exc
        if len(set(ids)) != len(ids):
            raise ApiError("BAD_REQUEST", "ids 存在重复项")
        known = {i["id"] for i in self.web.list_instruments()}
        unknown = [i for i in ids if i not in known]
        if unknown:
            raise ApiError("NOT_FOUND", f"标的不存在：{unknown}", 404)
        self.web.reorder_instruments(ids)
        self.web.audit(
            uid=p.uid, username=p.username, action="instrument_reorder",
            target=f"{len(ids)} items", ip=req.ip,
        )
        return {"ordered": len(ids)}

    def h_instruments_batch(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        rows = req.body.get("rows")
        if rows is None:
            text = req.body.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ApiError("BAD_REQUEST", "请提供 rows 数组或待解析的 text")
            rows = parse_batch_text(text)
        if not isinstance(rows, list) or not rows:
            raise ApiError("BAD_REQUEST", "没有可导入的行")
        if len(rows) > 500:
            raise ApiError("BAD_REQUEST", "单次最多导入 500 条")

        results: list[dict] = []
        created = duplicated = invalid = 0
        seen: set[str] = set()

        for index, raw in enumerate(rows, start=1):
            if not isinstance(raw, dict):
                invalid += 1
                results.append({"line": index, "code": "", "status": "invalid",
                                "issues": [{"field": "-", "message": "行格式错误"}]})
                continue

            result = validate_instrument(raw)
            code = str(raw.get("code") or "")
            if not result.ok:
                invalid += 1
                results.append({
                    "line": index, "code": code, "status": "invalid",
                    "issues": [i.as_dict() for i in result.issues],
                })
                continue

            key = result.data["symbol_key"]
            existing = self.web.find_instrument(key)
            if key in seen or existing is not None:
                duplicated += 1
                results.append({
                    "line": index, "code": result.data["code"], "status": "duplicate",
                    "existing_id": existing["id"] if existing else None,
                    "message": "本批内重复" if key in seen else "已存在于标的库",
                })
                continue

            seen.add(key)
            iid = self.web.create_instrument(result.data)
            created += 1
            results.append({
                "line": index, "code": result.data["code"], "name": result.data["name"],
                "status": "created", "id": iid,
            })

        self.web.audit(
            uid=p.uid, username=p.username, action="instrument_import",
            target=f"{created} created / {duplicated} dup / {invalid} invalid", ip=req.ip,
        )
        return {
            "total": len(rows),
            "created": created,
            "duplicated": duplicated,
            "invalid": invalid,
            "items": results,
        }

    def _execute_backtest(
        self,
        cfg_path: Path,
        overrides: dict[str, Any],
        owner: int,
        selection: list[dict] | None = None,
    ) -> dict:
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
        if selection:
            # 用标的库里的精度覆盖配置：不同市场的最小变动价位差别很大，
            # 沿用配置里的默认值会导致撮合与风控按错误精度取整。
            cfg = replace(
                cfg,
                market=replace(
                    cfg.market,
                    symbols=tuple(i["code"] for i in selection),
                    price_tick=float(selection[0]["price_tick"]),
                    qty_step=float(selection[0]["qty_step"]),
                    min_notional=max(float(i["min_notional"]) for i in selection),
                ),
            )

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
            "instruments": {"count": self.web.instrument_count()},
            "ai": self.ai.status(),
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

    # ------------------------------------------------------------------ #
    # 通用实体管理（schema 驱动）
    # ------------------------------------------------------------------ #
    def _entity(self, req: Request):
        key = req.params.get("entity", "")
        spec = get_entity(key)
        if spec is None:
            raise ApiError("NOT_FOUND", f"未知数据类型：{key}", 404)
        return spec

    @staticmethod
    def _raise_entity_error(exc: EntityError):
        raise ApiError(
            exc.code,
            exc.message,
            exc.status,
            details=[i.as_dict() for i in exc.issues] or None,
        )

    def h_entities_catalog(self, req: Request) -> dict:
        from .schema import entity_catalog

        return {"items": entity_catalog(), "ai": self.ai.status()}

    def h_entities_list(self, req: Request) -> dict:
        spec = self._entity(req)
        items = self.store.list(spec, q=req.q("q"), limit=req.q_int("limit", 200, lo=1, hi=1000))
        return {"entity": spec.key, "spec": spec.as_dict(), "items": items, "total": len(items)}

    def h_entities_create(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        spec = self._entity(req)
        try:
            item = self.store.create(spec, req.body)
        except EntityError as exc:
            self._raise_entity_error(exc)
            raise  # pragma: no cover
        self.web.audit(
            uid=p.uid, username=p.username, action=f"{spec.key}_create",
            target=str(item.get("id")), detail=str(item.get("name", "")), ip=req.ip,
        )
        return {"entity": spec.key, "item": item}

    def h_entities_update(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        spec = self._entity(req)
        rid = int(req.params["rid"])
        try:
            item = self.store.update(spec, rid, req.body)
        except EntityError as exc:
            self._raise_entity_error(exc)
            raise  # pragma: no cover
        self.web.audit(
            uid=p.uid, username=p.username, action=f"{spec.key}_update",
            target=str(rid), detail=str(item.get("name", "")), ip=req.ip,
        )
        return {"entity": spec.key, "item": item}

    def h_entities_delete(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        spec = self._entity(req)
        rid = int(req.params["rid"])
        try:
            item = self.store.delete(spec, rid)
        except EntityError as exc:
            self._raise_entity_error(exc)
            raise  # pragma: no cover
        self.web.audit(
            uid=p.uid, username=p.username, action=f"{spec.key}_delete",
            target=str(rid), detail=str(item.get("name", "")), ip=req.ip,
        )
        return {"entity": spec.key, "deleted": True, "item": item}

    def h_entities_validate(self, req: Request) -> dict:
        spec = self._entity(req)
        rid = req.body.get("_id")
        return {
            "entity": spec.key,
            **self.store.validate(
                spec, req.body, partial=bool(req.body.get("_partial")),
                rid=int(rid) if isinstance(rid, (int, str)) and str(rid).isdigit() else None,
            ),
        }

    # ------------------------------------------------------------------ #
    # AI 辅助
    # ------------------------------------------------------------------ #
    def _entity_for(self, req: Request):
        key = str(req.body.get("entity") or "")
        return get_entity(key)

    def h_ai_suggest(self, req: Request) -> dict:
        spec = self._entity_for(req)
        if spec is None:
            raise ApiError("BAD_REQUEST", "未知数据类型", 400)
        partial = req.body.get("partial") or {}
        if not isinstance(partial, dict):
            raise ApiError("BAD_REQUEST", "partial 必须是对象")
        return self.ai.suggest(spec, partial, str(req.body.get("hint") or ""))

    def h_ai_fix(self, req: Request) -> dict:
        spec = self._entity_for(req)
        if spec is None:
            raise ApiError("BAD_REQUEST", "未知数据类型", 400)
        payload = req.body.get("payload") or {}
        if not isinstance(payload, dict):
            raise ApiError("BAD_REQUEST", "payload 必须是对象")
        issues = self.store.validate(spec, payload)["issues"]
        if not issues:
            return {"mode": self.ai.status()["mode"], "payload": payload, "changes": [], "unresolved": []}
        return self.ai.fix(spec, payload, issues)

    def h_ai_command(self, req: Request) -> dict:
        text = str(req.body.get("text") or "").strip()
        if not text:
            raise ApiError("BAD_REQUEST", "请输入指令", 400)
        if len(text) > 500:
            raise ApiError("BAD_REQUEST", "指令过长（上限 500 字）", 400)
        result = self.ai.command(text)
        plan = result.get("plan") or {}
        # 预览：把计划跑一遍 schema 校验，让用户在确认前就看到会不会失败
        _, issues = validate_plan(plan)
        spec = ENTITIES.get(str(plan.get("entity") or ""))
        preview: list[dict] = [i.as_dict() for i in issues]
        if not preview and spec is not None and plan.get("action") in ("create", "update"):
            preview = self.store.validate(spec, plan.get("data") or {})["issues"]
        result["preview_issues"] = preview
        result["requires_confirmation"] = True
        return result

    def h_ai_apply(self, req: Request) -> dict:
        p = req.principal
        assert p is not None
        raw_plan = req.body.get("plan") or {}
        if not isinstance(raw_plan, dict):
            raise ApiError("BAD_REQUEST", "plan 必须是对象", 400)
        plan, issues = validate_plan(raw_plan)
        if issues:
            raise ApiError(
                "VALIDATION_FAILED",
                "；".join(f"{i.field}: {i.message}" for i in issues),
                400,
                details=[i.as_dict() for i in issues],
            )

        spec = ENTITIES[plan["entity"]]
        action = plan["action"]
        try:
            if action == "create":
                result = self.store.create(spec, plan["data"])
            elif action == "update":
                result = self.store.update(spec, plan["id"], plan["data"])
            elif action == "delete":
                result = self.store.delete(spec, plan["id"])
            else:
                result = self.store.list(spec, limit=200)
        except EntityError as exc:
            explanation = self.ai.explain(spec, action, False, exc.message, [i.as_dict() for i in exc.issues], None)
            raise ApiError(
                exc.code,
                exc.message,
                exc.status,
                details={"issues": [i.as_dict() for i in exc.issues], "explanation": explanation},
            ) from exc

        self.web.audit(
            uid=p.uid, username=p.username, action=f"ai_{action}",
            target=spec.key, detail=plan.get("summary", "")[:200], ip=req.ip,
        )
        explanation = self.ai.explain(spec, action, True, plan.get("summary", ""), [], result)
        return {"entity": spec.key, "action": action, "result": result, "explanation": explanation}

    def h_ai_explain(self, req: Request) -> dict:
        spec = self._entity_for(req)
        action = str(req.body.get("action") or "")
        ok = bool(req.body.get("ok"))
        message = str(req.body.get("message") or "")
        issues = req.body.get("issues") or []
        data = req.body.get("data")
        if not isinstance(issues, list):
            issues = []
        return self.ai.explain(spec, action, ok, message, issues, data)

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
