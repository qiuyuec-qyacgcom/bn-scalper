"""策略基类与上下文。

约定（非常重要）：
1. 策略**只能**读取 `StrategyContext`，不允许访问 Store / Broker / RiskManager，
   因此策略天然可回测、可实盘、可单测。
2. 策略输出的 `Intent` 不含仓位大小 —— 仓位由风控决定。
3. 策略只能看到 **已收盘** 的 bar，`ctx.bar` 一定 closed=True。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..indicators import IndicatorSet, Snapshot
from ..models import Bar, FlowSnapshot, Intent, OrderBookSnapshot, Position


@dataclass(slots=True)
class StrategyContext:
    bar: Bar
    snapshot: Snapshot
    history: deque[Bar]
    indicators: IndicatorSet
    position: Position | None
    equity: float
    ts: int
    book: OrderBookSnapshot | None = None
    flow: FlowSnapshot | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return self.bar.symbol

    def bars(self, n: int) -> list[Bar]:
        n = max(1, min(n, len(self.history)))
        return list(self.history)[-n:]

    @property
    def price(self) -> float:
        return self.bar.close


class Strategy(ABC):
    """所有策略的父类。

    子类实现 `on_bar`；可选实现 `warmup_bars`（覆盖指标所需最小样本）。
    """

    name: str = "base"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = {**self.default_params(), **(params or {})}
        self.validate_params()

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {}

    def validate_params(self) -> None:
        """参数自检，非法直接抛错（避免带病上线）。"""
        return None

    @property
    def warmup_bars(self) -> int:
        return 300

    # ------------------------------------------------------------------ #
    # 交易逻辑
    # ------------------------------------------------------------------ #
    @abstractmethod
    def on_bar(self, ctx: StrategyContext) -> list[Intent]:
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def p(self, key: str) -> Any:
        return self.params[key]

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "params": self.params}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__} {self.params}>"
