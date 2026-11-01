"""领域模型：行情、订单、成交、持仓、意图。

本模块是整个系统的最底层，**不允许** import 任何其它业务模块
（config / data / strategy / risk / execution 都不依赖）。这样保证
回测引擎、实盘引擎、撮合器、风控之间只通过稳定的数据结构通信。
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- #
# 基础枚举
# --------------------------------------------------------------------------- #
class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is PositionSide.LONG else -1

    @property
    def entry_side(self) -> Side:
        return Side.BUY if self is PositionSide.LONG else Side.SELL

    @property
    def exit_side(self) -> Side:
        return Side.SELL if self is PositionSide.LONG else Side.BUY

    @classmethod
    def from_side(cls, side: Side) -> "PositionSide":
        return cls.LONG if side is Side.BUY else cls.SHORT


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_MARKET = "stop_market"
    TAKE_PROFIT_MARKET = "take_profit_market"


class TimeInForce(str, Enum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    GTX = "gtx"  # post-only


class OrderStatus(str, Enum):
    PENDING_NEW = "pending_new"
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )

    @property
    def is_working(self) -> bool:
        return self in (
            OrderStatus.PENDING_NEW,
            OrderStatus.NEW,
            OrderStatus.PARTIALLY_FILLED,
        )


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_STOP = "time_stop"
    SIGNAL_FLIP = "signal_flip"
    FLOW_REVERSAL = "flow_reversal"   # 订单流失衡反转（微观结构先于价格结构）
    RISK_LIQUIDATION = "risk_liquidation"
    MANUAL = "manual"
    KILL_SWITCH = "kill_switch"
    END_OF_DATA = "end_of_data"


# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Bar:
    """K 线（只传入已收盘的 bar，避免未来数据泄漏）。"""

    symbol: str
    ts: int  # bar 开盘时间，毫秒 UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0
    trades: int = 0
    taker_buy_base: float = 0.0  # 主动买入成交量，用于订单流代理因子
    closed: bool = True

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return self.close - self.open

    @property
    def is_bull(self) -> bool:
        return self.close >= self.open

    @property
    def taker_buy_ratio(self) -> float | None:
        if self.volume <= 0 or self.taker_buy_base <= 0:
            return None
        return self.taker_buy_base / self.volume

    @property
    def ts_close(self) -> int:
        return self.ts + 60_000


@dataclass(slots=True)
class Tick:
    """逐笔成交（aggTrade）或合成 tick。"""

    symbol: str
    ts: int
    price: float
    qty: float
    is_buyer_maker: bool = False

    @property
    def taker_side(self) -> Side:
        return Side.SELL if self.is_buyer_maker else Side.BUY


@dataclass(slots=True)
class OrderBookSnapshot:
    """盘口快照（前 N 档聚合）。

    `bid_depth`/`ask_depth` 是**带宽内**的累计挂单量（默认 ±5bps），
    与 `bid_qty`/`ask_qty`（仅一档）区分：前者是 OBI 因子，后者是点差计算。
    """

    symbol: str
    ts: int
    bid_px: float
    ask_px: float
    bid_qty: float
    ask_qty: float
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    microprice: float = 0.0
    levels: int = 0
    # 堆叠失衡：连续多少个价位被同一方向主导（见 microstructure/imbalance.py）
    bid_stack_levels: int = 0
    ask_stack_levels: int = 0
    stack_score: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid_px + self.ask_px) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return 0.0 if mid <= 0 else (self.ask_px - self.bid_px) / mid * 10_000

    @property
    def imbalance(self) -> float:
        """订单簿失衡度，范围 [-1, 1]，>0 表示买盘厚。优先用带宽内深度。"""
        bid = self.bid_depth if self.bid_depth > 0 else self.bid_qty
        ask = self.ask_depth if self.ask_depth > 0 else self.ask_qty
        total = bid + ask
        if total <= 0:
            return 0.0
        return (bid - ask) / total

    @property
    def microprice_bias_bps(self) -> float:
        """微价格相对中间价的偏离（bps）。>0 表示买盘更饥渴（上行动能）。"""
        if self.microprice <= 0:
            return 0.0
        mid = self.mid
        return 0.0 if mid <= 0 else (self.microprice - mid) / mid * 10_000

    def is_stale(self, now_ms: int, max_age_ms: int) -> bool:
        return now_ms - self.ts > max_age_ms


# --------------------------------------------------------------------------- #
# 订单流（逐笔成交聚合）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class FlowBar:
    """单根 bar 内的**原始**成交流聚合（由 aggTrade 聚合而来）。

    `max_delta` / `min_delta` 是 bar 内累计 delta 曲线的极值——
    这是"买盘被吸收/衰竭"的直接证据，也是 taker_buy 占比无法表达的时序信息。
    """

    symbol: str
    ts: int
    buy_vol: float
    sell_vol: float
    trades: int = 0
    max_delta: float = 0.0
    min_delta: float = 0.0
    large_vol: float = 0.0

    @property
    def volume(self) -> float:
        return self.buy_vol + self.sell_vol

    @property
    def delta(self) -> float:
        return self.buy_vol - self.sell_vol


@dataclass(slots=True)
class FlowSnapshot:
    """策略可见的订单流特征（原始聚合 + 滚动特征）。"""

    symbol: str
    ts: int
    buy_vol: float
    sell_vol: float
    trades: int
    delta: float
    delta_ratio: float
    cvd: float
    cvd_slope: float
    absorption: float
    max_delta: float = 0.0
    min_delta: float = 0.0
    large_ratio: float = 0.0

    @property
    def volume(self) -> float:
        return self.buy_vol + self.sell_vol

    @property
    def has_data(self) -> bool:
        return self.trades > 0 and self.volume > 0

    def describe(self) -> str:
        return (
            f"d={self.delta_ratio:+.3f} cvd_slope={self.cvd_slope:+.3f} "
            f"absorb={self.absorption:.2f} n={self.trades}"
        )


# --------------------------------------------------------------------------- #
# 交易意图（策略输出，不含仓位大小，仓位由风控决定）
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class EnterIntent:
    symbol: str
    side: Side
    ts: int
    ref_price: float
    stop_price: float
    take_profit_price: float
    reason: str = ""
    confidence: float = 1.0
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExitIntent:
    symbol: str
    ts: int
    reason: ExitReason = ExitReason.MANUAL
    ref_price: float | None = None
    fraction: float = 1.0  # 平仓比例，1.0 = 全平


@dataclass(slots=True)
class AdjustStopIntent:
    symbol: str
    ts: int
    new_stop: float
    reason: ExitReason = ExitReason.TRAILING_STOP


Intent = EnterIntent | ExitIntent | AdjustStopIntent


# --------------------------------------------------------------------------- #
# 订单 / 成交
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Fill:
    ts: int
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float = 0.0
    is_maker: bool = False
    reason: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass(slots=True)
class Order:
    client_id: str
    symbol: str
    side: Side
    type: OrderType
    qty: float
    ts: int
    price: float | None = None
    stop_price: float | None = None
    tif: TimeInForce = TimeInForce.GTC
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled_qty: float = 0.0
    avg_price: float = 0.0
    fee: float = 0.0
    reduce_only: bool = False
    close_position: bool = False
    tag: str = ""
    exchange_id: str | None = None
    created_ts: int = 0
    updated_ts: int = 0
    reject_reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def is_protective(self) -> bool:
        return self.type in (OrderType.STOP_MARKET, OrderType.TAKE_PROFIT_MARKET)

    def apply_fill(self, fill: Fill) -> None:
        total_qty = self.filled_qty + fill.qty
        if total_qty <= 0:
            return
        self.avg_price = (self.avg_price * self.filled_qty + fill.price * fill.qty) / total_qty
        self.filled_qty = total_qty
        self.fee += fill.fee
        self.fills.append(fill)
        self.updated_ts = fill.ts
        self.status = (
            OrderStatus.FILLED if self.remaining <= self.qty * 1e-9 else OrderStatus.PARTIALLY_FILLED
        )


# --------------------------------------------------------------------------- #
# 持仓 / 账户
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Position:
    symbol: str
    side: PositionSide
    qty: float
    entry_price: float
    entry_ts: int
    stop_price: float
    take_profit_price: float
    initial_stop: float
    entry_fee: float = 0.0
    max_favorable_price: float = 0.0
    max_adverse_price: float = 0.0
    breakeven_moved: bool = False
    bars_held: int = 0
    entry_order_id: str = ""
    tags: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.initial_stop)

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.qty * self.side.sign

    def unrealized_r(self, price: float) -> float:
        risk = self.risk_per_unit * self.qty
        return 0.0 if risk <= 0 else self.unrealized(price) / risk

    def update_extremes(self, price: float) -> None:
        if self.max_favorable_price == 0.0:
            self.max_favorable_price = price
            self.max_adverse_price = price
        if self.side is PositionSide.LONG:
            self.max_favorable_price = max(self.max_favorable_price, price)
            self.max_adverse_price = min(self.max_adverse_price, price)
        else:
            self.max_favorable_price = min(self.max_favorable_price, price)
            self.max_adverse_price = max(self.max_adverse_price, price)


@dataclass(slots=True)
class TradeRecord:
    """已平仓交易，用于绩效统计。"""

    symbol: str
    side: PositionSide
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: int
    exit_ts: int
    pnl: float
    fees: float
    r_multiple: float
    reason: ExitReason
    bars_held: int
    entry_reason: str = ""

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def hold_seconds(self) -> float:
        return max(0.0, (self.exit_ts - self.entry_ts) / 1000.0)


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def now_ms() -> int:
    return int(time.time() * 1000)


def ts_to_iso(ts_ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ms / 1000))


def iso_to_ts(value: str) -> int:
    """解析 'YYYY-MM-DD' 或 'YYYY-MM-DDTHH:MM:SSZ' 为毫秒时间戳（统一按 UTC）。"""
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return int(calendar.timegm(time.strptime(value, fmt)) * 1000)
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {value!r}")
