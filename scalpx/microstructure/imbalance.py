"""堆叠失衡（stacked imbalance）。

单一价位的失衡很容易是噪声（一笔冰山单就能造出来）。真正有信息量的是
**连续多个档位被同一方向主导**——它代表一段"价格墙"，超短线的回踩/突破
是否成立，几乎完全取决于这段墙在不在。

两种口径的区别（这是最容易搞错的地方）：

- **成交剖面**（freqtrade `orderflow.py`）：把逐笔成交按价格分箱，比较
  `bid[p]` 与 `ask[p+tick]`（沿价格轴 shift），因为成交记录里每个价位都可能
  同时有主动买和主动卖。
- **盘口阶梯**（本项目）：买卖两边天然不重叠，且远离中间价的档位可能一侧为空。
  若仍按价格轴对角线比较，会出现"对手方为空 ⇒ 比率无穷大 ⇒ 误判为失衡"。
  因此盘口用**按档位配对**（第 i 档买 vs 第 i 档卖，即距中间价等距的两档），
  这才是 L2 深度的正确失衡度量。

两种口径的结论都叫"堆叠"：连续 `stack_range` 档以上同向主导。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class StackedImbalance:
    """一次盘口扫描的结果。"""

    bid_levels: int = 0
    ask_levels: int = 0
    bid_prices: tuple[float, ...] = ()
    ask_prices: tuple[float, ...] = ()
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    levels_used: int = 0
    tick: float = 0.0
    dominant_prices: tuple[float, ...] = field(default_factory=tuple)

    @property
    def score(self) -> float:
        """[-1, 1]：+1 全是买盘堆叠，-1 全是卖盘堆叠，0 表示无堆叠或势均。"""
        total = self.bid_levels + self.ask_levels
        if total <= 0:
            return 0.0
        return (self.bid_levels - self.ask_levels) / total

    @property
    def has_stack(self) -> bool:
        return self.bid_levels > 0 or self.ask_levels > 0

    def describe(self) -> str:
        return (
            f"stack={self.score:+.2f} bid×{self.bid_levels} ask×{self.ask_levels} "
            f"(levels={self.levels_used})"
        )


def _scan(signs: list[tuple[int, int, float, float]], prices: list[float], side: int, stack_range: int):
    """扫描连续同向区间，返回 (最大连续长度, 起点价格列表, 累计量)。"""
    best = 0
    starts: list[float] = []
    volume = 0.0
    run = 0
    run_start_idx = 0
    run_vol = 0.0

    def _close() -> None:
        nonlocal best, volume
        if run >= stack_range:
            starts.append(round(prices[run_start_idx], 10))
            volume += run_vol
            best = max(best, run)

    for idx, s, bid_qty, ask_qty in signs:
        if s == side:
            if run == 0:
                run_start_idx = idx
                run_vol = 0.0
            run += 1
            run_vol += bid_qty if side > 0 else ask_qty
        else:
            _close()
            run = 0
    _close()
    return best, tuple(starts), volume


def compute_stacked_imbalance(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    tick: float,
    ratio: float = 3.0,
    min_level_qty: float = 0.0,
    stack_range: int = 3,
    max_levels: int = 20,
) -> StackedImbalance:
    """按档位配对扫描盘口，找出连续的同向失衡区间。

    参数
    ----
    tick          : 价格最小变动单位（仅用于对齐/记录，不参与配对）
    ratio         : 失衡阈值，`bid/ask >= ratio` 记为买盘主导（反之卖盘）
    min_level_qty : 单档最小总量，低于该值视为噪声，判为中性
    stack_range   : 至少连续多少档同向才算"堆叠"
    max_levels    : 最多扫描多少档（受订阅档数限制）
    """
    if not bids or not asks or stack_range < 1:
        return StackedImbalance(tick=tick)

    n = min(len(bids), len(asks), max_levels)
    signs: list[tuple[int, int, float, float]] = []
    bid_prices: list[float] = []
    ask_prices: list[float] = []

    for i in range(n):
        bp, bq = bids[i]
        ap, aq = asks[i]
        bid_prices.append(bp)
        ask_prices.append(ap)
        if bq <= 0 or aq <= 0:
            side = 0
        elif bq + aq < min_level_qty:
            side = 0
        elif bq / aq >= ratio:
            side = 1
        elif aq / bq >= ratio:
            side = -1
        else:
            side = 0
        signs.append((i, side, bq, aq))

    bid_levels, bid_starts, bid_vol = _scan(signs, bid_prices, 1, stack_range)
    ask_levels, ask_starts, ask_vol = _scan(signs, ask_prices, -1, stack_range)

    return StackedImbalance(
        bid_levels=bid_levels,
        ask_levels=ask_levels,
        bid_prices=bid_starts,
        ask_prices=ask_starts,
        bid_volume=bid_vol,
        ask_volume=ask_vol,
        levels_used=n,
        tick=tick,
        dominant_prices=bid_starts if bid_levels >= ask_levels else ask_starts,
    )
