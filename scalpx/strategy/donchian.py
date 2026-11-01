"""备选策略：通道突破（Donchian Breakout + 波动率过滤）。

存在的意义有两点：
1. 证明 `Strategy` 接口可插拔 —— 只新增一个文件、注册一个名字即可；
2. 提供与主策略**负相关**的信号源，用于组合层分散（趋势回踩在震荡市失效，
   通道突破在趋势加速段表现更好）。

入场：收盘突破近 N 根最高/最低（前一根之前的极值，不含当前 bar，避免自指）
出场：反向通道中轨（N//2）命中 / ATR 止损 / 固定持仓上限
"""

from __future__ import annotations

from typing import Any

from ..models import EnterIntent, ExitIntent, ExitReason, Intent, Side
from .base import Strategy, StrategyContext


class DonchianBreakoutStrategy(Strategy):
    name = "donchian"

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {
            "channel": 30,
            "atr_period": 14,
            "atr_min_pct": 0.0008,
            "atr_max_pct": 0.0080,
            "vol_mult": 1.20,
            "stop_atr_mult": 1.20,
            "rr": 2.00,
            "max_hold_bars": 20,
            "allow_long": True,
            "allow_short": True,
        }

    def validate_params(self) -> None:
        if self.params["channel"] < 5:
            raise ValueError("channel 必须 >= 5")
        if self.params["rr"] <= 0:
            raise ValueError("rr 必须 > 0")

    @property
    def warmup_bars(self) -> int:
        return max(self.params["channel"] + 2, self.params["atr_period"] + 2, 60)

    def on_bar(self, ctx: StrategyContext) -> list[Intent]:
        p, bar, snap = self.params, ctx.bar, ctx.snapshot
        atr = snap.atr
        if atr <= 0:
            return []

        if ctx.position is not None:
            return self._manage(ctx)

        if not (p["atr_min_pct"] <= snap.atr_pct <= p["atr_max_pct"]):
            return []
        if snap.volume_ratio < p["vol_mult"]:
            return []

        hist = ctx.bars(p["channel"] + 1)
        prior = hist[:-1]  # 不含当前 bar
        if len(prior) < p["channel"]:
            return []
        upper = max(b.high for b in prior)
        lower = min(b.low for b in prior)

        if p["allow_long"] and bar.close > upper:
            risk = p["stop_atr_mult"] * atr
            return [
                EnterIntent(
                    symbol=bar.symbol,
                    side=Side.BUY,
                    ts=bar.ts,
                    ref_price=bar.close,
                    stop_price=bar.close - risk,
                    take_profit_price=bar.close + p["rr"] * risk,
                    reason=f"{self.name}:break_up>{upper:.2f}",
                    tags={"atr": atr, "channel_high": upper},
                )
            ]
        if p["allow_short"] and bar.close < lower:
            risk = p["stop_atr_mult"] * atr
            return [
                EnterIntent(
                    symbol=bar.symbol,
                    side=Side.SELL,
                    ts=bar.ts,
                    ref_price=bar.close,
                    stop_price=bar.close + risk,
                    take_profit_price=bar.close - p["rr"] * risk,
                    reason=f"{self.name}:break_down<{lower:.2f}",
                    tags={"atr": atr, "channel_low": lower},
                )
            ]
        return []

    def _manage(self, ctx: StrategyContext) -> list[Intent]:
        pos = ctx.position
        assert pos is not None
        p, bar = self.params, ctx.bar
        if pos.bars_held >= p["max_hold_bars"]:
            return [ExitIntent(symbol=bar.symbol, ts=bar.ts, reason=ExitReason.TIME_STOP, ref_price=bar.close)]

        hist = ctx.bars(p["channel"])
        if len(hist) < p["channel"]:
            return []
        mid = (max(b.high for b in hist) + min(b.low for b in hist)) / 2.0
        from ..models import PositionSide

        if pos.side is PositionSide.LONG and bar.close < mid:
            return [ExitIntent(symbol=bar.symbol, ts=bar.ts, reason=ExitReason.SIGNAL_FLIP, ref_price=bar.close)]
        if pos.side is PositionSide.SHORT and bar.close > mid:
            return [ExitIntent(symbol=bar.symbol, ts=bar.ts, reason=ExitReason.SIGNAL_FLIP, ref_price=bar.close)]
        return []
