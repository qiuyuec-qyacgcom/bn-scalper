"""交易成本模型（滑点 + 手续费 + 点差）。

为什么单独成模块：超短线的盈亏几乎由成本决定。把成本算清楚，才能回答两个问题——
1. 这笔交易的目标价，扣除成本后还剩多少净收益？
2. 止损距离这么窄，名义仓位会不会大到让手续费吃掉半个 R？

这三个数字（往返成本、成本占风险比、净盈亏比）是超短线能否成立的**硬约束**，
必须在事前风控里判定，而不是事后复盘时才发现。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ExecutionConfig


@dataclass(slots=True)
class CostBreakdown:
    """一次完整往返（开仓 + 平仓）的成本，单位为**占名义价值（名义仓位价值）的比例**。"""

    entry_fee: float
    exit_fee: float
    slippage: float
    spread: float

    @property
    def total(self) -> float:
        return self.entry_fee + self.exit_fee + self.slippage + self.spread

    def as_bps(self) -> float:
        return self.total * 10_000

    def __str__(self) -> str:  # pragma: no cover - 仅日志
        return (
            f"cost={self.total * 1e4:.2f}bps "
            f"(fee {self.entry_fee * 1e4:.2f}+{self.exit_fee * 1e4:.2f}, "
            f"slip {self.slippage * 1e4:.2f}, spread {self.spread * 1e4:.2f})"
        )


def round_trip_cost(
    exec_cfg: ExecutionConfig,
    *,
    entry_is_maker: bool = True,
    exit_is_maker: bool = False,
    spread_bps: float | None = None,
) -> CostBreakdown:
    """估算一次往返成本。

    默认假设：**maker 进、taker 出**（止损必然是 taker，止盈才可能 maker）。
    滑点只作用在 taker 腿上——maker 单以挂单价成交，超额滑点表现为"不成交"而非"成交更差"。
    """
    entry_fee = exec_cfg.maker_fee if entry_is_maker else exec_cfg.taker_fee
    exit_fee = exec_cfg.maker_fee if exit_is_maker else exec_cfg.taker_fee

    slip_bps = exec_cfg.slippage_bps
    slippage = (0.0 if entry_is_maker else slip_bps / 10_000.0)
    slippage += (0.0 if exit_is_maker else slip_bps / 10_000.0)

    # 点差成本：maker 入场时是"赚点差"（挂在被动侧），此处保守按 0 计；
    # taker 入场才需要跨点差，按半个点差计。
    spread = 0.0
    if spread_bps is not None:
        spread = (spread_bps / 10_000.0) * (0.0 if entry_is_maker else 0.5)

    return CostBreakdown(
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        slippage=slippage,
        spread=spread,
    )


def cost_to_risk_ratio(cost_pct: float, stop_pct: float) -> float:
    """成本占单笔风险的比例。

    这是超短线最重要的一个比值：
        往返成本 / 止损距离
    因为它等于「手续费吃掉了多少个 R」——比值 0.4 意味着每笔交易一开始就亏 0.4R。

    典型值参考（maker 进 + taker 出，约 7bps 往返）：
        止损 0.15% → 0.47R   几乎不可能盈利
        止损 0.25% → 0.28R
        止损 0.40% → 0.18R   可接受
        止损 0.80% → 0.09R
    """
    if stop_pct <= 0:
        return float("inf")
    return cost_pct / stop_pct


def net_rr(*, take_profit_pct: float, stop_pct: float, cost_pct: float) -> float:
    """扣除成本后的实际盈亏比。

    毛盈亏比 = tp/stop，但成本对两侧都收费：
        净盈利 = tp - cost
        净亏损 = stop + cost
    所以**实际盈亏比永远低于设计值**，且止损越窄差距越大。
    """
    if stop_pct <= 0:
        return 0.0
    net_win = take_profit_pct - cost_pct
    net_loss = stop_pct + cost_pct
    if net_loss <= 0:
        return float("inf")
    return net_win / net_loss
