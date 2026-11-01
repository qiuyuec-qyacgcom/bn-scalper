"""增量技术指标。

为什么不用 pandas / talib：
1. 实盘需要 **O(1) 增量更新**，pandas 全量重算在 1m 高频下会造成抖动；
2. 回测与实盘必须用 **同一份指标实现**，否则回测-实盘一致性无法保证；
3. 零外部依赖，方便在交易所同机部署。

所有指标遵循 `update(x) -> float | None`：未攒够样本时返回 None，
调用方必须显式处理 None（这是防止用"半成品指标"交易的第一道防线）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

from .models import Bar


class Indicator:
    """指标基类。"""

    __slots__ = ("ready",)

    def __init__(self) -> None:
        self.ready = False

    def update(self, value: float) -> float | None:  # pragma: no cover - 抽象
        raise NotImplementedError

    @property
    def value(self) -> float | None:  # pragma: no cover - 抽象
        raise NotImplementedError


class Sma(Indicator):
    __slots__ = ("period", "_buf", "_sum", "_value")

    def __init__(self, period: int) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period 必须 >= 1")
        self.period = period
        self._buf: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        self._buf.append(value)
        self._sum = sum(self._buf)
        if len(self._buf) == self.period:
            self._value = self._sum / self.period
            self.ready = True
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


class Ema(Indicator):
    """指数移动平均。前 period 个样本用 SMA 播种，保证与主流实现一致。"""

    __slots__ = ("period", "alpha", "_seed", "_n", "_value")

    def __init__(self, period: int) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period 必须 >= 1")
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self._seed: list[float] = []
        self._n = 0
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        self._n += 1
        if self._value is None:
            self._seed.append(value)
            if len(self._seed) >= self.period:
                self._value = sum(self._seed) / len(self._seed)
                self._seed.clear()
                self.ready = True
            return self._value
        self._value = self.alpha * value + (1.0 - self.alpha) * self._value
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


class Delay(Indicator):
    """N 期前的值，用于斜率 / 变化率。"""

    __slots__ = ("period", "_buf")

    def __init__(self, period: int) -> None:
        super().__init__()
        self.period = period
        self._buf: deque[float] = deque(maxlen=period + 1)

    def update(self, value: float) -> float | None:
        self._buf.append(value)
        out = None
        if len(self._buf) == self.period + 1:
            out = self._buf[0]
            self.ready = True
        return out

    @property
    def value(self) -> float | None:
        if len(self._buf) == self.period + 1:
            return self._buf[0]
        return None


class Atr(Indicator):
    """Wilder ATR（真实波幅均值），超短线止损的核心尺度。"""

    __slots__ = ("period", "_prev_close", "_seed", "_n", "_value")

    def __init__(self, period: int = 14) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period 必须 >= 1")
        self.period = period
        self._prev_close: float | None = None
        self._seed: list[float] = []
        self._n = 0
        self._value: float | None = None

    def update_bar(self, bar: Bar) -> float | None:
        tr = bar.high - bar.low
        if self._prev_close is not None:
            tr = max(tr, abs(bar.high - self._prev_close), abs(bar.low - self._prev_close))
        self._prev_close = bar.close
        self._n += 1
        if self._value is None:
            self._seed.append(tr)
            if len(self._seed) >= self.period:
                self._value = sum(self._seed) / len(self._seed)
                self._seed.clear()
                self.ready = True
            return self._value
        self._value = (self._value * (self.period - 1) + tr) / self.period
        return self._value

    def update(self, value: float) -> float | None:  # pragma: no cover - 仅供一致性测试
        raise NotImplementedError("ATR 需要 Bar，请使用 update_bar")

    @property
    def value(self) -> float | None:
        return self._value


class Rsi(Indicator):
    """Wilder RSI。"""

    __slots__ = ("period", "_prev", "_avg_gain", "_avg_loss", "_seed_g", "_seed_l", "_value")

    def __init__(self, period: int = 14) -> None:
        super().__init__()
        if period < 2:
            raise ValueError("period 必须 >= 2")
        self.period = period
        self._prev: float | None = None
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._seed_g: list[float] = []
        self._seed_l: list[float] = []
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        prev = self._prev
        self._prev = value
        if prev is None:
            return None
        change = value - prev
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        if self._avg_gain is None:
            self._seed_g.append(gain)
            self._seed_l.append(loss)
            if len(self._seed_g) >= self.period:
                self._avg_gain = sum(self._seed_g) / self.period
                self._avg_loss = sum(self._seed_l) / self.period
                self._seed_g.clear()
                self._seed_l.clear()
                self.ready = True
                self._value = self._compute()
            return self._value
        assert self._avg_loss is not None
        self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
        self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
        self._value = self._compute()
        return self._value

    def _compute(self) -> float:
        assert self._avg_gain is not None and self._avg_loss is not None
        if self._avg_loss == 0:
            return 100.0 if self._avg_gain > 0 else 50.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    @property
    def value(self) -> float | None:
        return self._value


class RollingExtreme(Indicator):
    """滚动极值（不含当前 bar 时可作为突破通道）。"""

    __slots__ = ("period", "kind", "_buf")

    def __init__(self, period: int, kind: str = "max") -> None:
        super().__init__()
        if kind not in ("max", "min"):
            raise ValueError("kind 必须是 max/min")
        self.period = period
        self.kind = kind
        self._buf: deque[float] = deque(maxlen=period)

    def update(self, value: float) -> float | None:
        self._buf.append(value)
        if len(self._buf) < self.period:
            return None
        self.ready = True
        return max(self._buf) if self.kind == "max" else min(self._buf)

    @property
    def value(self) -> float | None:
        if len(self._buf) < self.period:
            return None
        return max(self._buf) if self.kind == "max" else min(self._buf)


# --------------------------------------------------------------------------- #
# 指标集合：一次遍历 bar 计算出策略需要的全部特征
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Snapshot:
    """单根已收盘 bar 的特征快照，策略只读这个对象。"""

    symbol: str
    ts: int
    close: float
    atr: float
    ema_fast: float
    ema_slow: float
    ema_trend: float
    trend_slope_pct: float
    rsi: float
    rsi_prev: float
    vol_sma: float
    volume_ratio: float
    atr_pct: float
    taker_buy_ratio: float | None
    bars_seen: int

    @property
    def trend_up(self) -> bool:
        return self.ema_fast > self.ema_slow and self.trend_slope_pct > 0

    @property
    def trend_down(self) -> bool:
        return self.ema_fast < self.ema_slow and self.trend_slope_pct < 0


class IndicatorSet:
    """为一组 bar 维护所有指标状态。"""

    def __init__(
        self,
        *,
        ema_fast: int = 21,
        ema_slow: int = 55,
        ema_trend: int = 144,
        trend_slope_lookback: int = 20,
        rsi_period: int = 14,
        atr_period: int = 14,
        vol_period: int = 20,
    ) -> None:
        self.ema_fast = Ema(ema_fast)
        self.ema_slow = Ema(ema_slow)
        self.ema_trend = Ema(ema_trend)
        self._trend_delay = Delay(trend_slope_lookback)
        self.rsi = Rsi(rsi_period)
        self.atr = Atr(atr_period)
        self.vol_sma = Sma(vol_period)
        self._bars = 0
        self._prev_rsi: float | None = None
        self._last: Snapshot | None = None
        self._required = max(ema_trend + trend_slope_lookback, ema_slow, rsi_period + 1, atr_period + 1, vol_period)

    @property
    def required_bars(self) -> int:
        return self._required

    @property
    def last(self) -> Snapshot | None:
        return self._last

    def update(self, bar: Bar) -> Snapshot | None:
        self._bars += 1
        ema_fast = self.ema_fast.update(bar.close)
        ema_slow = self.ema_slow.update(bar.close)
        ema_trend = self.ema_trend.update(bar.close)
        trend_delay = self._trend_delay.update(ema_trend if ema_trend is not None else bar.close) if ema_trend is not None else None
        rsi = self.rsi.update(bar.close)
        atr = self.atr.update_bar(bar)
        vol_sma = self.vol_sma.update(bar.volume)

        if None in (ema_fast, ema_slow, ema_trend, rsi, atr, vol_sma):
            if rsi is not None:
                self._prev_rsi = rsi
            return None

        assert ema_fast is not None and ema_slow is not None and ema_trend is not None
        assert rsi is not None and atr is not None and vol_sma is not None

        prev_rsi = self._prev_rsi if self._prev_rsi is not None else rsi
        self._prev_rsi = rsi

        slope_pct = 0.0
        if trend_delay is not None and trend_delay > 0:
            slope_pct = (ema_trend - trend_delay) / trend_delay

        snap = Snapshot(
            symbol=bar.symbol,
            ts=bar.ts,
            close=bar.close,
            atr=atr,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            ema_trend=ema_trend,
            trend_slope_pct=slope_pct,
            rsi=rsi,
            rsi_prev=prev_rsi,
            vol_sma=vol_sma,
            volume_ratio=(bar.volume / vol_sma) if vol_sma > 0 else 0.0,
            atr_pct=(atr / bar.close) if bar.close > 0 else 0.0,
            taker_buy_ratio=bar.taker_buy_ratio,
            bars_seen=self._bars,
        )
        self._last = snap
        return snap


def max_drawdown(equity: Iterable[float]) -> tuple[float, float]:
    """返回 (最大回撤比例, 最大回撤绝对额)。"""
    peak = float("-inf")
    worst_pct = 0.0
    worst_abs = 0.0
    for value in equity:
        if value > peak:
            peak = value
        if peak <= 0:
            continue
        dd_abs = peak - value
        dd_pct = dd_abs / peak
        if dd_pct > worst_pct:
            worst_pct = dd_pct
            worst_abs = dd_abs
    return worst_pct, worst_abs
