"""仓位计算（Sizing）。

核心公式：**风险预算法**
    qty = (equity * risk_per_trade) / |entry - stop|

即"每笔最多亏权益的 0.25%"，止损距离由 ATR 决定，因此波动放大时仓位自动缩小。
再叠加两道硬约束：
1. 名义价值上限 = equity * max_notional_pct（防止低波动时仓位爆炸）；
2. 杠杆上限（名义/权益 <= max_leverage）。

所有数量按交易所 stepSize 向下取整（永不做"多买一点"的冒险）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import MarketConfig, RiskConfig


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step + 1e-12) * step


def round_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 10)


def floor_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(math.floor(price / tick + 1e-12) * tick, 10)


def ceil_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(math.ceil(price / tick - 1e-12) * tick, 10)


@dataclass(slots=True)
class SizingResult:
    ok: bool
    qty: float
    notional: float
    stop_price: float
    take_profit_price: float
    risk_amount: float
    stop_distance: float
    code: str = ""
    message: str = ""


def clamp_stop(
    entry: float,
    stop: float,
    *,
    side_is_long: bool,
    min_stop_pct: float,
    max_stop_pct: float,
    tick: float,
) -> float:
    """把止损距离限制在 [min_stop_pct, max_stop_pct]，并按 tick 对齐（永不放宽止损）。"""
    if entry <= 0:
        return stop
    dist = abs(entry - stop)
    min_dist = entry * min_stop_pct
    max_dist = entry * max_stop_pct
    dist = min(max(dist, min_dist), max_dist)
    raw = entry - dist if side_is_long else entry + dist
    return floor_tick(raw, tick) if side_is_long else ceil_tick(raw, tick)


def size_position(
    *,
    equity: float,
    entry: float,
    stop: float,
    take_profit: float,
    side_is_long: bool,
    risk_cfg: RiskConfig,
    market_cfg: MarketConfig,
    leverage_room: float | None = None,
) -> SizingResult:
    if equity <= 0 or entry <= 0:
        return SizingResult(False, 0.0, 0.0, stop, take_profit, 0.0, 0.0, "BAD_INPUT", "权益或价格非正")

    # 记录策略原本想表达的 R 倍数，止损被夹紧后用它重算止盈，
    # 否则会出现"止损被拉宽、止盈没动"→ 实际盈亏比远低于设计值的问题。
    raw_stop_distance = abs(entry - stop)
    planned_rr = abs(take_profit - entry) / raw_stop_distance if raw_stop_distance > 0 else 0.0

    stop = clamp_stop(
        entry,
        stop,
        side_is_long=side_is_long,
        min_stop_pct=risk_cfg.min_stop_pct,
        max_stop_pct=risk_cfg.max_stop_pct,
        tick=market_cfg.price_tick,
    )
    stop_distance = abs(entry - stop)
    if planned_rr > 0 and stop_distance > 0:
        tp_raw = entry + planned_rr * stop_distance if side_is_long else entry - planned_rr * stop_distance
        take_profit = ceil_tick(tp_raw, market_cfg.price_tick) if side_is_long else floor_tick(
            tp_raw, market_cfg.price_tick
        )
    if stop_distance <= 0:
        return SizingResult(False, 0.0, 0.0, stop, take_profit, 0.0, 0.0, "STOP_TOO_TIGHT", "止损距离为 0")

    risk_amount = equity * risk_cfg.risk_per_trade
    qty_risk = risk_amount / stop_distance

    notional_cap = equity * min(risk_cfg.max_notional_pct, risk_cfg.max_leverage)
    if leverage_room is not None:
        notional_cap = min(notional_cap, equity * max(0.0, leverage_room))
    qty_cap = notional_cap / entry if entry > 0 else 0.0

    qty = round_step(min(qty_risk, qty_cap), market_cfg.qty_step)
    if qty < market_cfg.min_qty:
        return SizingResult(
            False, 0.0, 0.0, stop, take_profit, 0.0, stop_distance,
            "SIZE_TOO_SMALL", f"qty={qty:.8f} < min_qty={market_cfg.min_qty}",
        )
    notional = qty * entry
    if notional < market_cfg.min_notional:
        return SizingResult(
            False, 0.0, notional, stop, take_profit, 0.0, stop_distance,
            "NOTIONAL_TOO_SMALL", f"notional={notional:.2f} < min_notional={market_cfg.min_notional}",
        )

    # 若因 notional 上限导致实际风险小于预算，止损保持原样即可；
    # 这里同步把 TP 按 R 倍数映射到"实际止损距离"，避免 TP 相对过远。
    return SizingResult(
        ok=True,
        qty=qty,
        notional=notional,
        stop_price=stop,
        take_profit_price=take_profit,
        risk_amount=qty * stop_distance,
        stop_distance=stop_distance,
    )


def recompute_targets(
    *, entry: float, stop: float, rr: float, side_is_long: bool, tick: float
) -> tuple[float, float]:
    """由 R 倍数重算止损/止盈（止损固定，止盈 = entry ± rr * risk）。"""
    risk = abs(entry - stop)
    if risk <= 0:
        return stop, entry
    tp_raw = entry + rr * risk if side_is_long else entry - rr * risk
    tp = ceil_tick(tp_raw, tick) if side_is_long else floor_tick(tp_raw, tick)
    return stop, tp
