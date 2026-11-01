"""执行层：Broker 抽象、模拟撮合、订单管理系统。"""

from __future__ import annotations

from .base import Broker, BrokerError, OrderRejected
from .simulator import SimulatedBroker

__all__ = ["Broker", "BrokerError", "OrderRejected", "SimulatedBroker"]
