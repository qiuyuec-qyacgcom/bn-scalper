"""订单流：从逐笔成交到 CVD。

两个组件，职责严格分开：

- `FlowAggregator`：**实时**把 aggTrade 流按 bar 聚合为 `FlowBar`
  （主动买量、主动卖量、成交笔数、bar 内累计 delta 的极值）。
- `FlowFeatureBuilder`：把一串 `FlowBar` 变成策略可用的 `FlowSnapshot`
  （含滚动 CVD、CVD 斜率、吸收度）。回测与实盘**都走这一个类**。

关键口径（决定了因子是否可信）：
1. 主动方向只看 `is_buyer_maker`：taker 是买方 → 主动性买；反之卖。
2. `max_delta`/`min_delta` 必须是 **bar 内按时间顺序累计** 后的极值，
   不能对整根 bar 求 delta 再取极值（那样恒等于 delta 本身）。
3. CVD 是**跨 bar 累计**的量，只用于看斜率；绝对值无意义（起点任意）。
"""

from __future__ import annotations

import logging
from collections import deque

from ..models import FlowBar, FlowSnapshot

log = logging.getLogger(__name__)


class _Bucket:
    """单根 bar 的累加器。"""

    __slots__ = ("buy", "sell", "trades", "cum", "mx", "mn", "large")

    def __init__(self) -> None:
        self.buy = 0.0
        self.sell = 0.0
        self.trades = 0
        self.cum = 0.0
        self.mx = 0.0
        self.mn = 0.0
        self.large = 0.0


class FlowAggregator:
    """把逐笔成交按 bar 边界聚合成 `FlowBar`。

    **多 bucket 设计**：kline 与 aggTrade 是两条独立 WS 流，收盘事件与逐笔成交
    的先后顺序并不保证（新 bar 的第一笔成交可能早于上一根 bar 的收盘事件到达）。
    因此不能只维护"当前 bucket"，而是保留最近几个 bucket，由 `finalize(bar_ts)`
    按 bar 时间戳精确取走——这样既不会串 bar，也不会丢边界成交。
    """

    __slots__ = ("bar_ms", "large_trade_usd", "_symbol", "_buckets", "_late", "_keep")

    MAX_BUCKETS = 4

    def __init__(
        self,
        *,
        bar_ms: int = 60_000,
        large_trade_usd: float = 50_000.0,
        symbol: str = "",
    ) -> None:
        self.bar_ms = bar_ms
        self.large_trade_usd = large_trade_usd
        self._symbol = symbol
        self._buckets: dict[int, _Bucket] = {}
        self._late = 0
        self._keep = self.MAX_BUCKETS

    @property
    def late_trades(self) -> int:
        """晚到（bucket 已被清理）而被丢弃的成交数——用于监控数据质量。"""
        return self._late

    def on_trade(self, ts: int, price: float, qty: float, is_buyer_maker: bool) -> None:
        if qty <= 0:
            return
        bucket_ts = ts - (ts % self.bar_ms)
        bucket = self._buckets.get(bucket_ts)
        if bucket is None:
            newest = max(self._buckets) if self._buckets else bucket_ts
            # 允许回填"仍在保留窗口内"的旧 bar（kline 收盘事件常晚于下一根的首笔成交），
            # 只有超出窗口的才是真·迟到，必须丢弃而不是污染
            if self._buckets and bucket_ts < newest - (self._keep - 1) * self.bar_ms:
                self._late += 1
                return
            bucket = _Bucket()
            self._buckets[bucket_ts] = bucket
            self._evict(newest=max(newest, bucket_ts))

        if is_buyer_maker:      # taker 是卖方 → 主动卖
            bucket.sell += qty
            signed = -qty
        else:                   # taker 是买方 → 主动买
            bucket.buy += qty
            signed = qty
        bucket.trades += 1
        bucket.cum += signed
        bucket.mx = max(bucket.mx, bucket.cum)
        bucket.mn = min(bucket.mn, bucket.cum)
        if price * qty >= self.large_trade_usd:
            bucket.large += qty

    def _evict(self, *, newest: int) -> None:
        horizon = newest - (self._keep - 1) * self.bar_ms
        for ts in [t for t in self._buckets if t < horizon]:
            self._buckets.pop(ts, None)

    def finalize(self, bar_ts: int) -> FlowBar | None:
        """取走指定 bar 的聚合结果；该 bar 无成交时返回 None。"""
        bucket = self._buckets.pop(bar_ts, None)
        if bucket is None or bucket.trades == 0:
            return None
        return FlowBar(
            symbol=self._symbol,
            ts=bar_ts,
            buy_vol=bucket.buy,
            sell_vol=bucket.sell,
            trades=bucket.trades,
            max_delta=bucket.mx,
            min_delta=bucket.mn,
            large_vol=bucket.large,
        )

    def pending(self) -> int:
        return len(self._buckets)


class FlowFeatureBuilder:
    """把 `FlowBar` 序列变成带滚动特征的 `FlowSnapshot`。

    回测用文件/DB 里的 `FlowBar` 驱动；实盘用 `FlowAggregator.finalize()` 的输出驱动。
    两条路径产出的 `FlowSnapshot` 完全同构。
    """

    __slots__ = ("window", "_cvd", "_hist", "_n")

    def __init__(self, window: int = 20) -> None:
        if window < 2:
            raise ValueError("cvd_window 必须 >= 2")
        self.window = window
        self._cvd = 0.0
        # (delta, volume) 环形缓冲，用于算斜率与归一化
        self._hist: deque[tuple[float, float]] = deque(maxlen=window)
        self._n = 0

    @property
    def cvd(self) -> float:
        return self._cvd

    def update(self, bar: FlowBar) -> FlowSnapshot:
        delta = bar.delta
        volume = bar.volume
        self._cvd += delta
        self._hist.append((delta, volume))
        self._n += 1

        # CVD 斜率：窗口内净 delta / 窗口内总量 → 归一化到 [-1, 1]，可跨品种比较
        win_delta = sum(d for d, _ in self._hist)
        win_vol = sum(v for _, v in self._hist)
        slope = (win_delta / win_vol) if win_vol > 0 else 0.0

        # 吸收度：bar 内 delta 从最高点回落的比例。
        # 0 = 收盘仍在最高推动力上；1 = 完全回吐（买盘被吸收后反转）。
        span = bar.max_delta - bar.min_delta
        absorption = 0.0
        if span > 1e-12:
            absorption = min(1.0, max(0.0, (bar.max_delta - delta) / span))

        return FlowSnapshot(
            symbol=bar.symbol,
            ts=bar.ts,
            buy_vol=bar.buy_vol,
            sell_vol=bar.sell_vol,
            trades=bar.trades,
            delta=delta,
            delta_ratio=(delta / volume) if volume > 0 else 0.0,
            cvd=self._cvd,
            cvd_slope=slope,
            absorption=absorption,
            max_delta=bar.max_delta,
            min_delta=bar.min_delta,
            large_ratio=(bar.large_vol / volume) if volume > 0 else 0.0,
        )

    def reset(self) -> None:
        self._cvd = 0.0
        self._hist.clear()
        self._n = 0
