"""SQLite 数据层：行情、订单、成交、持仓、权益曲线、风控事件。

选型说明：
- 超短线单机场景下 SQLite(WAL) 的写入吞吐（数万行/秒）足够；
- 单文件便于随策略一起归档，回放与审计只需一份 .db；
- 通过 `threading.Lock` 串行化写入，异步引擎里用 `asyncio.to_thread` 调用，
  避免 greenlet/线程池污染撮合逻辑。

表结构刻意保持"原始事件"形态（append-only），派生指标一律在内存计算，
这样后续加指标不需要迁移数据库。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
import logging
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

from ..models import Bar, Fill, Order, Position, TradeRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    quote_volume REAL DEFAULT 0,
    trades      INTEGER DEFAULT 0,
    taker_buy_base REAL DEFAULT 0,
    PRIMARY KEY (symbol, interval, ts)
);
CREATE INDEX IF NOT EXISTS idx_bars_ts ON bars(ts);

CREATE TABLE IF NOT EXISTS orders (
    client_id   TEXT PRIMARY KEY,
    exchange_id TEXT,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    type        TEXT NOT NULL,
    qty         REAL NOT NULL,
    price       REAL,
    stop_price  REAL,
    tif         TEXT,
    status      TEXT NOT NULL,
    filled_qty  REAL DEFAULT 0,
    avg_price   REAL DEFAULT 0,
    fee         REAL DEFAULT 0,
    reduce_only INTEGER DEFAULT 0,
    tag         TEXT DEFAULT '',
    created_ts  INTEGER NOT NULL,
    updated_ts  INTEGER NOT NULL,
    reject_reason TEXT DEFAULT '',
    run_id      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_orders_run ON orders(run_id, created_ts);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT DEFAULT '',
    ts          INTEGER NOT NULL,
    order_id    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         REAL NOT NULL,
    price       REAL NOT NULL,
    fee         REAL DEFAULT 0,
    is_maker    INTEGER DEFAULT 0,
    reason      TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fills_run ON fills(run_id, ts);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT DEFAULT '',
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price  REAL NOT NULL,
    entry_ts    INTEGER NOT NULL,
    exit_ts     INTEGER NOT NULL,
    pnl         REAL NOT NULL,
    fees        REAL NOT NULL,
    r_multiple  REAL NOT NULL,
    reason      TEXT NOT NULL,
    bars_held   INTEGER DEFAULT 0,
    entry_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trades_run ON trades(run_id, exit_ts);

CREATE TABLE IF NOT EXISTS equity (
    run_id      TEXT DEFAULT '',
    ts          INTEGER NOT NULL,
    equity      REAL NOT NULL,
    cash        REAL NOT NULL,
    unrealized  REAL NOT NULL,
    exposure    REAL NOT NULL,
    drawdown    REAL NOT NULL,
    PRIMARY KEY (run_id, ts)
);

CREATE TABLE IF NOT EXISTS bar_flow (
    symbol      TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    buy_vol     REAL NOT NULL,
    sell_vol    REAL NOT NULL,
    trades      INTEGER DEFAULT 0,
    max_delta   REAL DEFAULT 0,
    min_delta   REAL DEFAULT 0,
    large_vol   REAL DEFAULT 0,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_bar_flow_ts ON bar_flow(ts);

CREATE TABLE IF NOT EXISTS book_snapshot (
    symbol      TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    bid_px      REAL NOT NULL,
    ask_px      REAL NOT NULL,
    bid_qty     REAL NOT NULL,
    ask_qty     REAL NOT NULL,
    bid_depth   REAL DEFAULT 0,
    ask_depth   REAL DEFAULT 0,
    microprice  REAL DEFAULT 0,
    levels      INTEGER DEFAULT 0,
    bid_stack   INTEGER DEFAULT 0,
    ask_stack   INTEGER DEFAULT 0,
    stack_score REAL DEFAULT 0,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_book_ts ON book_snapshot(ts);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT DEFAULT '',
    ts          INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    action      TEXT NOT NULL,
    side        TEXT,
    price       REAL,
    stop_price  REAL,
    tp_price    REAL,
    reason      TEXT DEFAULT '',
    meta_json   TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_signals_run ON signals(run_id, ts);

CREATE TABLE IF NOT EXISTS risk_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT DEFAULT '',
    ts          INTEGER NOT NULL,
    level       TEXT NOT NULL,
    code        TEXT NOT NULL,
    message     TEXT DEFAULT '',
    meta_json   TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_risk_run ON risk_events(run_id, ts);

CREATE TABLE IF NOT EXISTS runtime_state (
    run_id      TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    updated_ts  INTEGER NOT NULL,
    PRIMARY KEY (run_id, key)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    mode        TEXT NOT NULL,
    strategy     TEXT NOT NULL,
    symbols     TEXT NOT NULL,
    interval    TEXT NOT NULL,
    started_ts  INTEGER NOT NULL,
    finished_ts INTEGER,
    config_json TEXT NOT NULL,
    metrics_json TEXT,
    status      TEXT DEFAULT 'running'
);
"""


# 新增列（表名, 列名, DDL）。只增不改，保证旧库可直接升级。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("book_snapshot", "bid_stack", "INTEGER DEFAULT 0"),
    ("book_snapshot", "ask_stack", "INTEGER DEFAULT 0"),
    ("book_snapshot", "stack_score", "REAL DEFAULT 0"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    """轻量迁移：为已存在的旧库补齐新增列（SQLite 的 ADD COLUMN 是 O(1)）。"""
    for table, column, ddl in _MIGRATIONS:
        try:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


class Store:
    """线程安全的 SQLite 封装。`run_id` 用于隔离不同回测/实盘批次。"""

    def __init__(self, path: str | Path, run_id: str = "", *, persist: bool = True) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.persist = persist
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        # persist=False 的语义是"**只读**"：不建库、不写入，但已有的库仍可读。
        # （否则"回放历史订单流/盘口"这类纯读场景也会拿不到数据。）
        if not persist and not self.path.exists():
            return
        if persist:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        if persist:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
        try:
            # 只读连接也要补列：否则旧库缺列会让回放查询直接报错。
            # 若文件真的只读（权限），忽略即可，查询层会退化为空结果。
            _migrate(conn)
            conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover - 只读文件系统
            log.warning("无法迁移旧库（只读？）: %s", exc)
        self._conn = conn

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.commit()
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store 未启用持久化")
        return self._conn

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        if self._conn is None or not self.persist:
            return
        with self._lock:
            self._conn.execute(sql, params)

    def commit(self) -> None:
        if self._conn is None:
            return
        with self._lock:
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def register_run(
        self, *, mode: str, strategy: str, symbols: Iterable[str], interval: str, started_ts: int, config: dict[str, Any]
    ) -> None:
        self._execute(
            "INSERT OR REPLACE INTO runs(run_id,mode,strategy,symbols,interval,started_ts,config_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                self.run_id,
                mode,
                strategy,
                ",".join(symbols),
                interval,
                started_ts,
                json.dumps(config, ensure_ascii=False, default=str),
            ),
        )
        self.commit()

    def finish_run(self, status: str, metrics: dict[str, Any] | None, finished_ts: int) -> None:
        self._execute(
            "UPDATE runs SET status=?, metrics_json=?, finished_ts=? WHERE run_id=?",
            (status, json.dumps(metrics or {}, ensure_ascii=False, default=str), finished_ts, self.run_id),
        )
        self.commit()

    def save_bars(self, interval: str, bars: Iterable[Bar]) -> None:
        rows = [
            (b.symbol, interval, b.ts, b.open, b.high, b.low, b.close, b.volume, b.quote_volume, b.trades, b.taker_buy_base)
            for b in bars
        ]
        if not rows or self._conn is None or not self.persist:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO bars(symbol,interval,ts,open,high,low,close,volume,quote_volume,trades,taker_buy_base) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def load_bars(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Bar]:
        if self._conn is None:
            return []
        cur = self._conn.execute(
            "SELECT symbol,ts,open,high,low,close,volume,quote_volume,trades,taker_buy_base FROM bars "
            "WHERE symbol=? AND interval=? AND ts>=? AND ts<? ORDER BY ts ASC",
            (symbol, interval, start_ms, end_ms),
        )
        return [
            Bar(
                symbol=r["symbol"], ts=r["ts"], open=r["open"], high=r["high"], low=r["low"],
                close=r["close"], volume=r["volume"], quote_volume=r["quote_volume"],
                trades=r["trades"], taker_buy_base=r["taker_buy_base"],
            )
            for r in cur.fetchall()
        ]

    def save_order(self, order: Order) -> None:
        self._execute(
            "INSERT OR REPLACE INTO orders(client_id,exchange_id,symbol,side,type,qty,price,stop_price,tif,status,"
            "filled_qty,avg_price,fee,reduce_only,tag,created_ts,updated_ts,reject_reason,run_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order.client_id, order.exchange_id, order.symbol, order.side.value, order.type.value,
                order.qty, order.price, order.stop_price, order.tif.value, order.status.value,
                order.filled_qty, order.avg_price, order.fee, int(order.reduce_only), order.tag,
                order.created_ts or order.ts, order.updated_ts or order.ts, order.reject_reason, self.run_id,
            ),
        )

    def save_fill(self, fill: Fill) -> None:
        self._execute(
            "INSERT INTO fills(run_id,ts,order_id,symbol,side,qty,price,fee,is_maker,reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id, fill.ts, fill.order_id, fill.symbol, fill.side.value, fill.qty,
                fill.price, fill.fee, int(fill.is_maker), fill.reason,
            ),
        )

    def save_trade(self, trade: TradeRecord) -> None:
        self._execute(
            "INSERT INTO trades(run_id,symbol,side,qty,entry_price,exit_price,entry_ts,exit_ts,pnl,fees,"
            "r_multiple,reason,bars_held,entry_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id, trade.symbol, trade.side.value, trade.qty, trade.entry_price,
                trade.exit_price, trade.entry_ts, trade.exit_ts, trade.pnl, trade.fees,
                trade.r_multiple, trade.reason.value, trade.bars_held, trade.entry_reason,
            ),
        )

    # ------------------------------------------------------------------ #
    # 微观结构：订单流 / 盘口
    # ------------------------------------------------------------------ #
    def save_bar_flow(self, rows: Iterable) -> None:
        """批量写入按 bar 聚合的成交流（`FlowBar`）。"""
        payload = [
            (r.symbol, r.ts, r.buy_vol, r.sell_vol, r.trades, r.max_delta, r.min_delta, r.large_vol)
            for r in rows
        ]
        if not payload or self._conn is None or not self.persist:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO bar_flow(symbol,ts,buy_vol,sell_vol,trades,max_delta,min_delta,large_vol) "
                "VALUES(?,?,?,?,?,?,?,?)",
                payload,
            )

    def load_bar_flow(self, symbol: str, *, start_ms: int | None = None, end_ms: int | None = None) -> dict:
        from ..models import FlowBar

        if self._conn is None:
            return {}
        sql = "SELECT symbol,ts,buy_vol,sell_vol,trades,max_delta,min_delta,large_vol FROM bar_flow WHERE symbol=?"
        params: list = [symbol]
        if start_ms is not None:
            sql += " AND ts>=?"
            params.append(start_ms)
        if end_ms is not None:
            sql += " AND ts<?"
            params.append(end_ms)
        cur = self._conn.execute(sql + " ORDER BY ts", params)
        return {
            r["ts"]: FlowBar(
                symbol=r["symbol"], ts=r["ts"], buy_vol=r["buy_vol"], sell_vol=r["sell_vol"],
                trades=r["trades"], max_delta=r["max_delta"], min_delta=r["min_delta"],
                large_vol=r["large_vol"],
            )
            for r in cur.fetchall()
        }

    def save_book_snapshots(self, snaps: Iterable) -> None:
        payload = [
            (
                s.symbol, s.ts, s.bid_px, s.ask_px, s.bid_qty, s.ask_qty, s.bid_depth,
                s.ask_depth, s.microprice, s.levels, s.bid_stack_levels, s.ask_stack_levels,
                s.stack_score,
            )
            for s in snaps
        ]
        if not payload or self._conn is None or not self.persist:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO book_snapshot(symbol,ts,bid_px,ask_px,bid_qty,ask_qty,bid_depth,"
                "ask_depth,microprice,levels,bid_stack,ask_stack,stack_score) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )

    def load_book_snapshots(self, symbol: str, *, start_ms: int | None = None, end_ms: int | None = None) -> dict:
        from ..models import OrderBookSnapshot

        if self._conn is None:
            return {}
        sql = (
            "SELECT symbol,ts,bid_px,ask_px,bid_qty,ask_qty,bid_depth,ask_depth,microprice,levels,"
            "bid_stack,ask_stack,stack_score FROM book_snapshot WHERE symbol=?"
        )
        params: list = [symbol]
        if start_ms is not None:
            sql += " AND ts>=?"
            params.append(start_ms)
        if end_ms is not None:
            sql += " AND ts<?"
            params.append(end_ms)
        cur = self._conn.execute(sql + " ORDER BY ts", params)
        return {
            r["ts"]: OrderBookSnapshot(
                symbol=r["symbol"], ts=r["ts"], bid_px=r["bid_px"], ask_px=r["ask_px"],
                bid_qty=r["bid_qty"], ask_qty=r["ask_qty"], bid_depth=r["bid_depth"],
                ask_depth=r["ask_depth"], microprice=r["microprice"], levels=r["levels"],
                bid_stack_levels=r["bid_stack"], ask_stack_levels=r["ask_stack"],
                stack_score=r["stack_score"],
            )
            for r in cur.fetchall()
        }

    def save_signal(
        self, *, ts: int, symbol: str, action: str, side: str | None = None, price: float | None = None,
        stop_price: float | None = None, tp_price: float | None = None, reason: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._execute(
            "INSERT INTO signals(run_id,ts,symbol,action,side,price,stop_price,tp_price,reason,meta_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                self.run_id, ts, symbol, action, side, price, stop_price, tp_price, reason,
                json.dumps(meta or {}, ensure_ascii=False, default=str),
            ),
        )

    def save_risk_event(self, *, ts: int, level: str, code: str, message: str = "", meta: dict[str, Any] | None = None) -> None:
        self._execute(
            "INSERT INTO risk_events(run_id,ts,level,code,message,meta_json) VALUES(?,?,?,?,?,?)",
            (self.run_id, ts, level, code, message, json.dumps(meta or {}, ensure_ascii=False, default=str)),
        )

    def save_equity(self, *, ts: int, equity: float, cash: float, unrealized: float, exposure: float, drawdown: float) -> None:
        self._execute(
            "INSERT OR REPLACE INTO equity(run_id,ts,equity,cash,unrealized,exposure,drawdown) VALUES(?,?,?,?,?,?,?)",
            (self.run_id, ts, equity, cash, unrealized, exposure, drawdown),
        )

    def save_position_snapshot(self, position: Position | None, ts: int) -> None:
        """持仓快照写入 runtime_state，崩溃恢复时可直接读取。"""
        payload = json.dumps(
            {
                "symbol": position.symbol,
                "side": position.side.value,
                "qty": position.qty,
                "entry_price": position.entry_price,
                "entry_ts": position.entry_ts,
                "stop_price": position.stop_price,
                "take_profit_price": position.take_profit_price,
                "initial_stop": position.initial_stop,
                "bars_held": position.bars_held,
                "entry_order_id": position.entry_order_id,
            },
            ensure_ascii=False,
        ) if position else "null"
        key = f"position:{position.symbol if position else 'none'}"
        self._execute(
            "INSERT OR REPLACE INTO runtime_state(run_id,key,value,updated_ts) VALUES(?,?,?,?)",
            (self.run_id, key, payload, ts),
        )

    def save_state(self, key: str, value: Any, ts: int) -> None:
        self._execute(
            "INSERT OR REPLACE INTO runtime_state(run_id,key,value,updated_ts) VALUES(?,?,?,?)",
            (self.run_id, key, json.dumps(value, ensure_ascii=False, default=str), ts),
        )

    def load_state(self, key: str) -> Any:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT value FROM runtime_state WHERE run_id=? AND key=?", (self.run_id, key)
        ).fetchone()
        return None if row is None else json.loads(row["value"])

    # ------------------------------------------------------------------ #
    # 读取（报表用）
    # ------------------------------------------------------------------ #
    def fetch_trades(self, run_id: str | None = None) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        cur = self._conn.execute(
            "SELECT * FROM trades WHERE run_id=? ORDER BY exit_ts ASC", (run_id or self.run_id,)
        )
        return cur.fetchall()

    def fetch_equity(self, run_id: str | None = None) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        cur = self._conn.execute(
            "SELECT * FROM equity WHERE run_id=? ORDER BY ts ASC", (run_id or self.run_id,)
        )
        return cur.fetchall()

    def fetch_risk_events(self, run_id: str | None = None) -> list[sqlite3.Row]:
        if self._conn is None:
            return []
        cur = self._conn.execute(
            "SELECT * FROM risk_events WHERE run_id=? ORDER BY ts ASC", (run_id or self.run_id,)
        )
        return cur.fetchall()

    def fetch_run(self, run_id: str | None = None) -> sqlite3.Row | None:
        if self._conn is None:
            return None
        return self._conn.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id or self.run_id,)
        ).fetchone()
