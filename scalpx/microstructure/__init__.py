"""微观结构层：成交流（CVD）与盘口（OBI）。

这一层只做**数据加工**，不做交易决策，也不依赖 strategy / risk / execution。
它同时服务回测与实盘：
- 实盘：`FlowAggregator` 吃 WS aggTrade，`BookTracker` 吃 depth 流；
- 回测：从 `bar_flow` 表读取已聚合的原始量，交给同一个 `FlowFeatureBuilder`
  计算 rolling CVD——保证回测与实盘的特征口径完全一致。
"""

from __future__ import annotations

from .book import BookQuality, BookTracker
from .flow import FlowAggregator, FlowFeatureBuilder
from .imbalance import StackedImbalance, compute_stacked_imbalance

__all__ = [
    "FlowAggregator",
    "FlowFeatureBuilder",
    "BookTracker",
    "BookQuality",
    "StackedImbalance",
    "compute_stacked_imbalance",
]
