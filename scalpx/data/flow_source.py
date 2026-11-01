"""订单流/盘口的数据源：把"落库的历史"变成回测可用的逐 bar 特征。

设计要点：
- 回测**不做**逐笔重放（几十 GB 的 aggTrade 无法高效回放），
  而是回放**已按 bar 聚合**的 `FlowBar`——聚合逻辑与实盘是同一个 `FlowAggregator`。
- 缺失数据时返回 None，由策略显式降级，绝不伪造 0 值。
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterable, Iterator

from ..models import FlowBar
from .store import Store

log = logging.getLogger(__name__)

FLOW_COLUMNS = ["ts", "buy_vol", "sell_vol", "trades", "max_delta", "min_delta", "large_vol"]


class FlowSource:
    """按 (symbol, bar_ts) 提供 `FlowBar`。"""

    def get(self, symbol: str, ts: int) -> FlowBar | None:  # pragma: no cover - 抽象
        raise NotImplementedError


class EmptyFlowSource(FlowSource):
    """无订单流数据（纯 OHLCV 场景）。"""

    def get(self, symbol: str, ts: int) -> FlowBar | None:
        return None


class CsvFlowSource(FlowSource):
    """读取 `{csv_dir}/{symbol}_{interval}_flow.csv`。"""

    def __init__(self, csv_dir: str | Path, symbol: str, interval: str = "1m") -> None:
        self.path = Path(csv_dir) / f"{symbol}_{interval}_flow.csv"
        self.symbol = symbol
        self._rows: dict[int, FlowBar] = {}
        self._loaded = False

    def _load(self) -> None:
        self._loaded = True
        if not self.path.exists():
            log.info("未找到订单流文件 %s，将降级为 taker 占比代理", self.path)
            return
        with self.path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    ts = int(float(row["ts"]))
                    self._rows[ts] = FlowBar(
                        symbol=self.symbol,
                        ts=ts,
                        buy_vol=float(row.get("buy_vol", 0) or 0),
                        sell_vol=float(row.get("sell_vol", 0) or 0),
                        trades=int(float(row.get("trades", 0) or 0)),
                        max_delta=float(row.get("max_delta", 0) or 0),
                        min_delta=float(row.get("min_delta", 0) or 0),
                        large_vol=float(row.get("large_vol", 0) or 0),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
        log.info("已加载订单流 %s：%d 根 bar", self.path.name, len(self._rows))

    def get(self, symbol: str, ts: int) -> FlowBar | None:
        if not self._loaded:
            self._load()
        return self._rows.get(ts)


class StoreFlowSource(FlowSource):
    """从 SQLite 的 `bar_flow` 表读取（`download` 落库后的回放路径）。"""

    def __init__(self, store: Store, symbol: str) -> None:
        self.store = store
        self.symbol = symbol
        self._cache: dict[int, FlowBar] = {}
        self._loaded = False

    def _load(self) -> None:
        self._loaded = True
        self._cache = self.store.load_bar_flow(self.symbol)
        log.info("已从 DB 加载 %s 的订单流 %d 根", self.symbol, len(self._cache))

    def get(self, symbol: str, ts: int) -> FlowBar | None:
        if not self._loaded:
            self._load()
        return self._cache.get(ts)


class BookSource:
    """回测用的盘口回放（来自实盘录制）。默认空实现。"""

    def get(self, symbol: str, ts: int) -> "object | None":
        return None


class StoreBookSource(BookSource):
    """从 `book_snapshot` 表回放，取 <= ts 的最近一条（容忍采样间隔）。"""

    def __init__(self, store: Store, symbol: str, *, max_gap_ms: int = 3000) -> None:
        self.store = store
        self.symbol = symbol
        self.max_gap_ms = max_gap_ms
        self._rows: dict[int, object] = {}
        self._loaded = False
        self._keys: list[int] = []

    def _load(self) -> None:
        self._loaded = True
        self._rows = self.store.load_book_snapshots(self.symbol)
        self._keys = sorted(self._rows)
        log.info("已从 DB 加载 %s 的盘口快照 %d 条", self.symbol, len(self._keys))

    def get(self, symbol: str, ts: int):
        if not self._loaded:
            self._load()
        if not self._keys:
            return None
        import bisect

        i = bisect.bisect_right(self._keys, ts) - 1
        if i < 0:
            return None
        key = self._keys[i]
        if ts - key > self.max_gap_ms:
            return None
        return self._rows[key]


def iter_flow_rows(path: str | Path) -> Iterator[dict[str, float]]:
    """流式读取 aggTrade→FlowBar 的中间产物（供脚本/测试使用）。"""
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            yield row


def write_flow_csv(path: str | Path, bars: Iterable[FlowBar]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(FLOW_COLUMNS)
        for b in bars:
            writer.writerow([b.ts, b.buy_vol, b.sell_vol, b.trades, b.max_delta, b.min_delta, b.large_vol])
            n += 1
    return n
