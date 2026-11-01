"""数据访问层。

两个库，职责严格分开：
- **web.db**（可写）：用户、审计、设置、运行归属、配额。属于 Web 应用自己的状态。
- **交易库**（只读）：runs / trades / equity / signals / bar_flow / book_snapshot。
  Web 层绝不写入交易库（唯一例外是管理员的"删除运行"，属于显式运维动作）。

交易库可能不存在或表不存在（新装机器还没跑过回测），所有读方法都必须
优雅降级为空结果，而不是抛异常把首页打成 500。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

WEB_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    status        TEXT NOT NULL DEFAULT 'active',
    token_version INTEGER NOT NULL DEFAULT 0,
    created_ts    INTEGER NOT NULL,
    last_login_ts INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    uid      INTEGER DEFAULT 0,
    username TEXT DEFAULT '',
    action   TEXT NOT NULL,
    target   TEXT DEFAULT '',
    detail   TEXT DEFAULT '',
    ip       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS run_owner (
    run_id   TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    created_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS quota (
    uid    INTEGER NOT NULL,
    day    TEXT NOT NULL,
    used   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (uid, day)
);
"""


class WebRepo:
    """Web 应用自身的状态库（可写）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(WEB_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 用户
    # ------------------------------------------------------------------ #
    def user_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def get_user_by_name(self, username: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

    def get_user(self, uid: int) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    def list_users(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, username, role, status, created_ts, last_login_ts FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def create_user(self, username: str, password_hash: str, salt: str, role: str) -> int:
        now = int(time.time())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO users(username, password_hash, salt, role, status, created_ts) "
                "VALUES(?,?,?,?, 'active', ?)",
                (username, password_hash, salt, role, now),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def update_user(
        self,
        uid: int,
        *,
        role: str | None = None,
        status: str | None = None,
        password_hash: str | None = None,
        salt: str | None = None,
        bump_version: bool = False,
    ) -> bool:
        fields: list[str] = []
        params: list[Any] = []
        if role is not None:
            fields.append("role = ?")
            params.append(role)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if password_hash is not None and salt is not None:
            fields.append("password_hash = ?")
            fields.append("salt = ?")
            params.extend([password_hash, salt])
        if bump_version:
            fields.append("token_version = token_version + 1")
        if not fields:
            return False
        params.append(uid)
        with self._lock:
            cur = self._conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
            self._conn.commit()
            return cur.rowcount > 0

    def delete_user(self, uid: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM users WHERE id = ?", (uid,))
            self._conn.commit()
            return cur.rowcount > 0

    def touch_login(self, uid: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET last_login_ts = ? WHERE id = ?", (int(time.time()), uid))
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    def audit(self, *, uid: int, username: str, action: str, target: str = "", detail: str = "", ip: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit(ts, uid, username, action, target, detail, ip) VALUES(?,?,?,?,?,?,?)",
                (int(time.time()), uid, username, action, target, detail, ip),
            )
            self._conn.commit()

    def list_audit(self, limit: int = 100, offset: int = 0) -> list[dict]:
        rows = self._conn.execute(
            "SELECT ts, uid, username, action, target, detail, ip FROM audit "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 设置 / 运行归属 / 配额
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value, updated_ts) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                (key, json.dumps(value, ensure_ascii=False), int(time.time())),
            )
            self._conn.commit()

    def set_run_owner(self, run_id: str, owner_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO run_owner(run_id, owner_id, created_ts) VALUES(?,?,?)",
                (run_id, owner_id, int(time.time())),
            )
            self._conn.commit()

    def run_owners(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT run_id, owner_id FROM run_owner").fetchall()
        return {r["run_id"]: int(r["owner_id"]) for r in rows}

    def consume_quota(self, uid: int, limit: int) -> tuple[bool, int]:
        """按天配额。返回 (是否允许, 今日已用)。limit <= 0 表示不限制。"""
        if limit <= 0:
            return True, 0
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            row = self._conn.execute(
                "SELECT used FROM quota WHERE uid = ? AND day = ?", (uid, day)
            ).fetchone()
            used = int(row["used"]) if row else 0
            if used >= limit:
                return False, used
            self._conn.execute(
                "INSERT INTO quota(uid, day, used) VALUES(?,?,1) "
                "ON CONFLICT(uid, day) DO UPDATE SET used = used + 1",
                (uid, day),
            )
            self._conn.commit()
            return True, used + 1

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


class TradingRepo:
    """交易库只读视图（runs/trades/equity/signals/微观结构）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection | None:
        if not self.path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def available(self) -> bool:
        return bool(self._query("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1"))

    # ------------------------------------------------------------------ #
    # 运行 / 交易
    # ------------------------------------------------------------------ #
    def runs(self, *, limit: int = 20, offset: int = 0, only_ids: list[str] | None = None) -> list[dict]:
        if only_ids is not None:
            if not only_ids:
                return []
            marks = ",".join("?" * len(only_ids))
            return self._query(
                f"SELECT run_id, mode, strategy, symbols, interval, started_ts, finished_ts, "
                f"metrics_json, status FROM runs WHERE run_id IN ({marks}) "
                f"ORDER BY started_ts DESC LIMIT ? OFFSET ?",
                (*only_ids, limit, offset),
            )
        return self._query(
            "SELECT run_id, mode, strategy, symbols, interval, started_ts, finished_ts, "
            "metrics_json, status FROM runs ORDER BY started_ts DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def run(self, run_id: str) -> dict | None:
        rows = self._query("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return rows[0] if rows else None

    def trades(self, run_id: str, *, limit: int = 200, offset: int = 0, symbol: str = "") -> list[dict]:
        if symbol:
            return self._query(
                "SELECT * FROM trades WHERE run_id = ? AND symbol = ? "
                "ORDER BY exit_ts DESC LIMIT ? OFFSET ?",
                (run_id, symbol, limit, offset),
            )
        return self._query(
            "SELECT * FROM trades WHERE run_id = ? ORDER BY exit_ts DESC LIMIT ? OFFSET ?",
            (run_id, limit, offset),
        )

    def trade_count(self, run_id: str) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM trades WHERE run_id = ?", (run_id,))
        return int(rows[0]["n"]) if rows else 0

    def equity(self, run_id: str, *, max_points: int = 600) -> list[dict]:
        rows = self._query(
            "SELECT ts, equity, drawdown FROM equity WHERE run_id = ? ORDER BY ts", (run_id,)
        )
        if len(rows) <= max_points:
            return rows
        stride = max(1, len(rows) // max_points)
        sampled = rows[::stride]
        if sampled[-1] is not rows[-1]:
            sampled.append(rows[-1])
        return sampled

    def signals(self, run_id: str, *, limit: int = 50) -> list[dict]:
        return self._query(
            "SELECT ts, symbol, action, side, price, stop_price, tp_price, reason, meta_json "
            "FROM signals WHERE run_id = ? ORDER BY ts DESC LIMIT ?",
            (run_id, limit),
        )

    def risk_events(self, run_id: str, *, limit: int = 50) -> list[dict]:
        return self._query(
            "SELECT ts, level, code, message, meta_json FROM risk_events "
            "WHERE run_id = ? ORDER BY ts DESC LIMIT ?",
            (run_id, limit),
        )

    def exit_breakdown(self, run_id: str) -> dict[str, int]:
        rows = self._query(
            "SELECT reason, COUNT(*) AS n FROM trades WHERE run_id = ? GROUP BY reason", (run_id,)
        )
        return {r["reason"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ #
    # 微观结构
    # ------------------------------------------------------------------ #
    def flow_stats(self) -> dict[str, Any]:
        rows = self._query(
            "SELECT COUNT(*) AS bars, MIN(ts) AS first_ts, MAX(ts) AS last_ts, "
            "SUM(trades) AS trades FROM bar_flow"
        )
        books = self._query("SELECT COUNT(*) AS snaps FROM book_snapshot")
        out = dict(rows[0]) if rows else {"bars": 0, "first_ts": None, "last_ts": None, "trades": 0}
        out["book_snapshots"] = int(books[0]["snaps"]) if books else 0
        return out

    def cvd_series(self, symbol: str, *, limit: int = 400) -> list[dict]:
        rows = self._query(
            "SELECT ts, buy_vol, sell_vol FROM bar_flow WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (symbol, limit),
        )
        rows.reverse()
        cvd = 0.0
        out: list[dict] = []
        for r in rows:
            cvd += float(r["buy_vol"]) - float(r["sell_vol"])
            out.append({"ts": r["ts"], "cvd": round(cvd, 4)})
        return out

    # ------------------------------------------------------------------ #
    # 运维
    # ------------------------------------------------------------------ #
    def table_stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        for table in ("runs", "trades", "orders", "fills", "signals", "risk_events", "bar_flow", "book_snapshot"):
            rows = self._query(f"SELECT COUNT(*) AS n FROM {table}")
            stats[table] = int(rows[0]["n"]) if rows else 0
        return stats

    def db_size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def delete_run(self, run_id: str) -> dict[str, int]:
        # 唯一需要写交易库的操作，必须用可写连接（只读连接会直接抛 read-only）
        if not self.path.exists():
            return {}
        try:
            conn = sqlite3.connect(self.path, timeout=10.0)
        except sqlite3.Error:
            return {}
        deleted: dict[str, int] = {}
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            for table in ("trades", "orders", "fills", "equity", "signals", "risk_events", "runtime_state"):
                cur = conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
                deleted[table] = cur.rowcount
            cur = conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            deleted["runs"] = cur.rowcount
            conn.commit()
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        return deleted
