"""Broker 抽象。

回测与实盘的唯一差异点：一个由 `SimulatedBroker` 实现，一个由
`BinanceBroker` 实现。上层（OMS / engine）只依赖这个接口。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from ..models import Bar, Fill, Order


class BrokerError(RuntimeError):
    """通用执行异常。"""


class OrderRejected(BrokerError):
    """交易所/模拟器确定性拒单（不可重试）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Broker(ABC):
    """订单执行接口。

    契约：
    - `submit` 必须返回**最新状态**的 Order（可能已 FILLED）；
    - 成交只通过 `poll_fills()` 交付，保证上层只有一个成交入口；
    - 幂等：同一 `client_id` 重复提交不得产生第二笔成交。
    """

    @abstractmethod
    def submit(self, order: Order) -> Order: ...

    @abstractmethod
    def cancel(self, client_id: str) -> bool: ...

    @abstractmethod
    def open_orders(self, symbol: str | None = None) -> Sequence[Order]: ...

    @abstractmethod
    def poll_fills(self) -> list[Fill]: ...

    @abstractmethod
    def reference_price(self, symbol: str) -> float: ...

    def on_bar(self, bar: Bar, on_fill: "object | None" = None) -> None:
        """回测推进；实盘实现为空（由 WS 推送驱动）。

        `on_fill` 为同步成交回调：撮合器在 intrabar 路径上每产生一笔成交就
        立即回调，使 OMS 能在同一根 bar 的剩余路径前挂上保护单。
        """
        return None

    def close(self) -> None:
        return None
