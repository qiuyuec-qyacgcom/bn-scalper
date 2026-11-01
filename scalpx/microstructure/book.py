"""盘口：从 `depth20@100ms` 快照构建可交易的 OBI / 微价格 / 堆叠失衡。

为什么不用"把 20 档总量相加"这种粗糙做法：
- 远离中间价的挂单对短期价格几乎没有影响，必须按**带宽**（bps）截断；
- 失衡度要用**深度加权**，且档位越远权重越低；
- 微价格（microprice）比中间价更能反映下一跳方向；
- 单档失衡可能是冰山单噪声，**连续多档同向**（堆叠失衡）才是真正的价格墙。

数据来源是币安 `<symbol>@depth20@100ms`：每 100ms 推送一次**前 20 档全量快照**
（不是增量），因此不需要 UID 拼接，只需处理乱序与过期。

**质量熔断**：盘口是唯一会被"数据质量"直接毁掉的因子（丢包、乱序、跨所延迟）。
本模块维护滚动质量窗口，一旦拒绝率超阈值，`get()` 返回 None，策略自动降级；
引擎可据此触发 `BOOK_UNTRUSTED` 熔断。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

from ..models import OrderBookSnapshot
from .imbalance import StackedImbalance, compute_stacked_imbalance

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BookQuality:
    """盘口可信度快照。"""

    ok: bool
    reason: str = "OK"
    age_ms: int = 0
    updates: int = 0
    stale_ratio: float = 0.0
    out_of_order_ratio: float = 0.0
    bad_ratio: float = 0.0

    def __bool__(self) -> bool:
        return self.ok


@dataclass(slots=True)
class _Outcome:
    """滚动质量窗口的一次采样。"""

    ok: int


class BookTracker:
    """维护每个标的的最新盘口快照，并计算 OBI / 微价格 / 堆叠失衡。"""

    __slots__ = (
        "depth_bps", "levels", "max_age_ms", "tick", "stack_ratio", "stack_range",
        "min_level_qty", "max_bad_ratio", "quality_window",
        "_books", "_outcomes", "_updates", "_stale_drops", "_out_of_order", "_last_stack",
    )

    def __init__(
        self,
        *,
        depth_bps: float = 5.0,
        levels: int = 20,
        max_age_ms: int = 2000,
        tick: float = 0.1,
        stack_ratio: float = 3.0,
        stack_range: int = 3,
        min_level_qty: float = 0.0,
        max_bad_ratio: float = 0.5,
        quality_window: int = 200,
    ) -> None:
        self.depth_bps = depth_bps
        self.levels = levels
        self.max_age_ms = max_age_ms
        self.tick = tick
        self.stack_ratio = stack_ratio
        self.stack_range = stack_range
        self.min_level_qty = min_level_qty
        self.max_bad_ratio = max_bad_ratio
        self.quality_window = quality_window
        self._books: dict[str, OrderBookSnapshot] = {}
        self._outcomes: dict[str, deque[int]] = {}
        self._updates = 0
        self._stale_drops = 0
        self._out_of_order = 0
        self._last_stack: dict[str, StackedImbalance] = {}

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def _record(self, symbol: str, ok: bool) -> None:
        hist = self._outcomes.get(symbol)
        if hist is None:
            hist = deque(maxlen=self.quality_window)
            self._outcomes[symbol] = hist
        hist.append(1 if ok else 0)

    def apply(
        self,
        symbol: str,
        ts: int,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> OrderBookSnapshot | None:
        bids = [(p, q) for p, q in bids if p > 0 and q > 0][: self.levels]
        asks = [(p, q) for p, q in asks if p > 0 and q > 0][: self.levels]
        if not bids or not asks:
            self._stale_drops += 1
            self._record(symbol, False)
            return None

        bids.sort(key=lambda x: -x[0])
        asks.sort(key=lambda x: x[0])
        best_bid, best_bid_qty = bids[0]
        best_ask, best_ask_qty = asks[0]
        if best_bid >= best_ask:  # 交叉盘 = 数据异常
            self._stale_drops += 1
            self._record(symbol, False)
            return None

        prev = self._books.get(symbol)
        if prev is not None and ts < prev.ts:
            self._out_of_order += 1
            self._record(symbol, False)
            return prev

        mid = (best_bid + best_ask) / 2.0
        band = mid * self.depth_bps / 10_000.0
        lo, hi = mid - band, mid + band

        # 深度加权：越靠近中间价的挂单权重越高（线性衰减）
        bid_depth = 0.0
        for px, qty in bids:
            if px < lo:
                break
            weight = 1.0 - min(1.0, abs(mid - px) / band) * 0.5 if band > 0 else 1.0
            bid_depth += qty * weight
        ask_depth = 0.0
        for px, qty in asks:
            if px > hi:
                break
            weight = 1.0 - min(1.0, abs(px - mid) / band) * 0.5 if band > 0 else 1.0
            ask_depth += qty * weight

        total_top = best_bid_qty + best_ask_qty
        microprice = (
            (best_bid * best_ask_qty + best_ask * best_bid_qty) / total_top
            if total_top > 0
            else mid
        )

        stack = compute_stacked_imbalance(
            bids, asks,
            tick=self.tick,
            ratio=self.stack_ratio,
            min_level_qty=self.min_level_qty,
            stack_range=self.stack_range,
            max_levels=self.levels,
        )
        self._last_stack[symbol] = stack

        snap = OrderBookSnapshot(
            symbol=symbol,
            ts=ts,
            bid_px=best_bid,
            ask_px=best_ask,
            bid_qty=best_bid_qty,
            ask_qty=best_ask_qty,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            microprice=microprice,
            levels=min(len(bids), len(asks)),
            bid_stack_levels=stack.bid_levels,
            ask_stack_levels=stack.ask_levels,
            stack_score=stack.score,
        )
        self._books[symbol] = snap
        self._updates += 1
        self._record(symbol, True)
        return snap

    # ------------------------------------------------------------------ #
    # 质量
    # ------------------------------------------------------------------ #
    def quality(self, symbol: str, now_ms: int) -> BookQuality:
        """综合"新鲜度 + 滚动拒绝率"判断盘口是否可用于决策。"""
        snap = self._books.get(symbol)
        hist = self._outcomes.get(symbol) or ()
        updates = len(hist)
        bad = sum(1 for x in hist if x == 0)
        bad_ratio = (bad / updates) if updates else 0.0
        stale_ratio = self._stale_drops / max(1, self._stale_drops + self._updates)
        ooo_ratio = self._out_of_order / max(1, self._updates)

        if snap is None:
            return BookQuality(False, "NO_DATA", 0, updates, stale_ratio, ooo_ratio, bad_ratio)

        age = max(0, now_ms - snap.ts)
        if age > self.max_age_ms:
            return BookQuality(False, "STALE", age, updates, stale_ratio, ooo_ratio, bad_ratio)

        # 至少积累一定样本才判定"劣化"，避免启动瞬间（前几条都失败）误熔断
        if updates >= 20 and bad_ratio > self.max_bad_ratio:
            return BookQuality(
                False, "HIGH_REJECT_RATE", age, updates, stale_ratio, ooo_ratio, bad_ratio
            )
        return BookQuality(True, "OK", age, updates, stale_ratio, ooo_ratio, bad_ratio)

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get(self, symbol: str, now_ms: int) -> OrderBookSnapshot | None:
        """返回**可信且未过期**的快照；否则 None（策略必须显式降级）。"""
        return self._books.get(symbol) if self.quality(symbol, now_ms).ok else None

    def raw(self, symbol: str) -> OrderBookSnapshot | None:
        return self._books.get(symbol)

    def stack(self, symbol: str) -> StackedImbalance | None:
        return self._last_stack.get(symbol)

    @property
    def stats(self) -> dict[str, int]:
        return {
            "updates": self._updates,
            "stale_drops": self._stale_drops,
            "out_of_order": self._out_of_order,
            "tracked_symbols": len(self._books),
        }

    def describe(self, symbol: str, now_ms: int) -> dict[str, float | str | bool]:
        q = self.quality(symbol, now_ms)
        stack = self._last_stack.get(symbol)
        return {
            "ok": q.ok,
            "reason": q.reason,
            "age_ms": q.age_ms,
            "bad_ratio": round(q.bad_ratio, 4),
            "stale_ratio": round(q.stale_ratio, 4),
            "out_of_order_ratio": round(q.out_of_order_ratio, 4),
            "bid_stack": stack.bid_levels if stack else 0,
            "ask_stack": stack.ask_levels if stack else 0,
            "stack_score": round(stack.score, 4) if stack else 0.0,
        }
