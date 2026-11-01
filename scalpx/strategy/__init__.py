"""策略层：只产出意图（Intent），不决定仓位。

新增策略：在本目录加一个文件，用 `@register("name")` 装饰类，
然后在 TOML 里把 `[strategy] name` 指向它，无需改引擎。
"""

from __future__ import annotations

from .base import Strategy, StrategyContext
from .donchian import DonchianBreakoutStrategy
from .registry import available, create_strategy, register
from .trend_pullback import TrendPullbackStrategy

# 导入即注册：新增策略只需在这里加一行
register("trend_pullback")(TrendPullbackStrategy)
register("donchian")(DonchianBreakoutStrategy)

__all__ = [
    "Strategy",
    "StrategyContext",
    "TrendPullbackStrategy",
    "DonchianBreakoutStrategy",
    "register",
    "create_strategy",
    "available",
]
