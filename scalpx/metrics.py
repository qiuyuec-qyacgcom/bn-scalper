"""绩效指标。

口径说明（避免"各自为政"的指标口径）：
- 收益率一律基于**权益曲线**，日内聚合后再算夏普/索提诺（1m 级直接年化会虚高）；
- 最大回撤用权益峰谷，未实现盈亏计入（与实盘账户口径一致）；
- 盈亏比、胜率、期望 R 基于**已平仓交易**；
- 年化使用自然日折算（加密 7x24，365 天）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import TradeRecord

DAY_MS = 86_400_000


@dataclass(slots=True)
class EquityPoint:
    ts: int
    equity: float


def _daily_closes(points: Sequence[tuple[int, float]]) -> list[tuple[int, float]]:
    """按 UTC 天取每日最后一条权益。"""
    out: dict[int, float] = {}
    for ts, eq in points:
        day = ts // DAY_MS
        out[day] = eq
    return sorted(out.items())


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _downside_std(xs: Sequence[float]) -> float:
    neg = [x for x in xs if x < 0]
    if len(neg) < 2:
        return 0.0
    return math.sqrt(sum(x * x for x in neg) / len(neg))


def max_drawdown(points: Sequence[tuple[int, float]]) -> tuple[float, float, int]:
    """返回 (最大回撤比例, 回撤金额, 回撤持续 bar 数)。"""
    peak = float("-inf")
    worst_pct = 0.0
    worst_abs = 0.0
    for _, eq in points:
        peak = max(peak, eq)
        if peak <= 0:
            continue
        dd = peak - eq
        if dd > worst_abs:
            worst_abs = dd
            worst_pct = dd / peak
    return worst_pct, worst_abs, 0


def compute_metrics(
    *,
    trades: Sequence[TradeRecord],
    equity_curve: Sequence[tuple[int, float]],
    initial_equity: float,
    total_fees: float = 0.0,
) -> dict[str, float]:
    if not equity_curve:
        equity_curve = [(0, initial_equity)]

    final_equity = equity_curve[-1][1]
    start_ts, end_ts = equity_curve[0][0], equity_curve[-1][0]
    days = max(1.0 / 1440.0, (end_ts - start_ts) / DAY_MS)

    total_return = final_equity / initial_equity - 1.0 if initial_equity > 0 else 0.0
    annualized = (1.0 + total_return) ** (365.0 / days) - 1.0 if total_return > -1 else -1.0

    daily = _daily_closes(equity_curve)
    daily_returns: list[float] = []
    for (_, prev), (_, cur) in zip(daily, daily[1:]):
        if prev > 0:
            daily_returns.append(cur / prev - 1.0)

    d_mean = _mean(daily_returns)
    d_std = _std(daily_returns)
    d_down = _downside_std(daily_returns)
    sharpe = (d_mean / d_std * math.sqrt(365)) if d_std > 0 else 0.0
    sortino = (d_mean / d_down * math.sqrt(365)) if d_down > 0 else 0.0

    mdd_pct, mdd_abs, _ = max_drawdown(equity_curve)
    calmar = (annualized / mdd_pct) if mdd_pct > 0 else 0.0

    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    win_rate = len(wins) / n if n else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    avg_win = _mean([t.pnl for t in wins])
    avg_loss = _mean([t.pnl for t in losses])
    expectancy = _mean([t.pnl for t in trades])
    avg_r = _mean([t.r_multiple for t in trades])
    avg_hold_min = _mean([t.hold_seconds / 60.0 for t in trades])

    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.reason.value] = by_reason.get(t.reason.value, 0) + 1

    return {
        "initial_equity": round(initial_equity, 2),
        "final_equity": round(final_equity, 2),
        "total_return": round(total_return, 6),
        "annualized_return": round(annualized, 6),
        "max_drawdown": round(mdd_pct, 6),
        "max_drawdown_abs": round(mdd_abs, 2),
        "calmar": round(calmar, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "trades": float(n),
        "win_rate": round(win_rate, 6),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else 999.0,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "expectancy": round(expectancy, 4),
        "avg_r": round(avg_r, 4),
        "avg_hold_minutes": round(avg_hold_min, 2),
        "trades_per_day": round(n / days, 2),
        "total_fees": round(total_fees, 2),
        "fee_drag_pct": round(total_fees / initial_equity, 6) if initial_equity > 0 else 0.0,
        **{f"exits_{k}": float(v) for k, v in by_reason.items()},
    }


def format_metrics(metrics: dict[str, float], *, title: str = "") -> str:
    """把指标渲染成等宽文本表（终端友好，不依赖任何第三方库）。"""
    groups: list[tuple[str, list[str]]] = [
        ("收益", ["initial_equity", "final_equity", "total_return", "annualized_return", "total_fees", "fee_drag_pct"]),
        ("风险", ["max_drawdown", "max_drawdown_abs", "sharpe", "sortino", "calmar"]),
        ("交易", ["trades", "trades_per_day", "win_rate", "profit_factor", "expectancy", "avg_r", "avg_hold_minutes", "avg_win", "avg_loss"]),
    ]
    pct_keys = {"total_return", "annualized_return", "max_drawdown", "win_rate", "fee_drag_pct"}
    lines: list[str] = []
    if title:
        lines.append(f"== {title} ==")
    for group, keys in groups:
        present = [k for k in keys if k in metrics]
        if not present:
            continue
        lines.append(f"[{group}]")
        for key in present:
            value = metrics[key]
            if key in pct_keys:
                text = f"{value:.2%}"
            elif abs(value) >= 100:
                text = f"{value:,.2f}"
            else:
                text = f"{value:.4f}"
            lines.append(f"  {key:<24}{text:>14}")
    exits = {k: v for k, v in metrics.items() if k.startswith("exits_")}
    if exits:
        lines.append("[出场分布]")
        for key, value in sorted(exits.items()):
            lines.append(f"  {key:<24}{int(value):>14d}")
    return "\n".join(lines)


def rolling_sharpe(equity: Iterable[tuple[int, float]], window: int = 30) -> list[float]:
    """滚动夏普，用于报表观察策略稳定性衰减。"""
    points = list(equity)
    daily = _daily_closes(points)
    returns: list[float] = []
    for (_, prev), (_, cur) in zip(daily, daily[1:]):
        returns.append(cur / prev - 1.0 if prev > 0 else 0.0)
    out: list[float] = []
    for i in range(len(returns)):
        window_slice = returns[max(0, i - window + 1) : i + 1]
        sd = _std(window_slice)
        out.append(_mean(window_slice) / sd * math.sqrt(365) if sd > 0 and len(window_slice) > 1 else 0.0)
    return out
