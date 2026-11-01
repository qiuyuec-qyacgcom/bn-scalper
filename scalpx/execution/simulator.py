"""模拟撮合器（回测 / 纸上交易）。

建模的要素（都显式可配，不做"乐观假设"）：
1. **intrabar 路径**：把 OHLC 展开为 4 个价格点，按"逆势优先"排序，
   避免"同一根 bar 里先摸到止盈"这种系统性高估收益的假设；
2. **手续费**：maker / taker 分离，止损止盈用 taker；
3. **滑点**：市价单按 slippage_bps 逆向成交；
4. **post-only 拒单**：会立即成交的 GTX 单直接被拒（模拟交易所行为）；
5. **最小下单量**：qty_step / min_qty / min_notional 校验。

未建模（明确记录，便于实盘对账时解释差异）：
- 撮合深度不足导致的 partial fill（默认全额成交）
- 交易所撮合延迟抖动（用 `latency_ms` 只在实盘层体现）
- 资金费率（可选 `funding_bps_per_8h`，由 Portfolio 外部注入）
"""

from __future__ import annotations

import logging
import math
from typing import Iterator, Sequence

from ..config import ExecutionConfig, MarketConfig
from ..models import Bar, Fill, Order, OrderStatus, OrderType, Side, TimeInForce
from .base import Broker, OrderRejected

log = logging.getLogger(__name__)


class SimulatedBroker(Broker):
    def __init__(
        self,
        market_cfg: MarketConfig,
        exec_cfg: ExecutionConfig,
        *,
        pessimistic_intrabar: bool = True,
    ) -> None:
        self.market = market_cfg
        self.cfg = exec_cfg
        self.pessimistic = pessimistic_intrabar
        self._working: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._prices: dict[str, float] = {}
        self._ts = 0
        self._seq = 0
        self._fill_sink = None
        self._persistent_sink = None
        self.stats = {
            "submitted": 0,
            "filled": 0,
            "canceled": 0,
            "rejected": 0,
            "expired": 0,
            "stop_triggered": 0,
            "tp_triggered": 0,
        }

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def reference_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    def current_ts(self) -> int:
        return self._ts

    def set_fill_sink(self, sink) -> None:
        """注册**持久**成交回调。

        一旦注册，任何时点产生的成交（含策略意图触发的市价单）都会同步交给
        OMS，从根本上消除"成交已发生但保护单还没挂"的窗口。
        """
        self._persistent_sink = sink

    def open_orders(self, symbol: str | None = None) -> Sequence[Order]:
        return [
            o
            for o in self._working.values()
            if o.status.is_working and (symbol is None or o.symbol == symbol)
        ]

    def poll_fills(self) -> list[Fill]:
        out = self._fills
        self._fills = []
        return out

    # ------------------------------------------------------------------ #
    # 下单 / 撤单
    # ------------------------------------------------------------------ #
    def submit(self, order: Order) -> Order:
        self.stats["submitted"] += 1
        order.created_ts = order.created_ts or order.ts
        order.updated_ts = order.ts

        if order.client_id in self._working or order.status is not OrderStatus.PENDING_NEW:
            return order  # 幂等：重复提交直接返回现状

        # --- 交易规则校验 ---
        if order.qty < self.market.min_qty - 1e-12:
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"qty<{self.market.min_qty}"
            self.stats["rejected"] += 1
            raise OrderRejected("QTY_TOO_SMALL", order.reject_reason)
        ref = self.reference_price(order.symbol)
        if ref > 0 and order.qty * ref < self.market.min_notional:
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"notional<{self.market.min_notional}"
            self.stats["rejected"] += 1
            raise OrderRejected("NOTIONAL_TOO_SMALL", order.reject_reason)

        # --- 立即可成交的类型 ---
        if order.type is OrderType.MARKET:
            price = self._market_price(order.symbol, order.side)
            self._execute(order, order.qty, price, ts=self._ts, maker=False)
            return order

        if order.type is OrderType.LIMIT and order.tif is TimeInForce.IOC:
            if self._limit_crossed(order, ref):
                self._execute(order, order.qty, order.price or ref, ts=self._ts, maker=False)
            else:
                order.status = OrderStatus.EXPIRED
                self.stats["expired"] += 1
            return order

        if order.type is OrderType.LIMIT and order.tif is TimeInForce.GTX:
            # post-only：若会立即吃单，交易所会直接拒绝
            if self._limit_crossed(order, ref):
                order.status = OrderStatus.REJECTED
                order.reject_reason = "post_only_would_cross"
                self.stats["rejected"] += 1
                raise OrderRejected("POST_ONLY_REJECT", order.reject_reason)

        order.status = OrderStatus.NEW
        self._working[order.client_id] = order
        return order

    def cancel(self, client_id: str) -> bool:
        order = self._working.pop(client_id, None)
        if order is None:
            return False
        order.status = OrderStatus.CANCELED
        order.updated_ts = self._ts
        self.stats["canceled"] += 1
        return True

    # ------------------------------------------------------------------ #
    # 行情推进
    # ------------------------------------------------------------------ #
    def on_bar(self, bar: Bar, on_fill=None) -> None:
        """按 intrabar 路径撮合。成交通过 sink 同步回调给 OMS。"""
        self._prices[bar.symbol] = bar.close
        self._fill_sink = on_fill or self._persistent_sink
        try:
            for price, ts in self._intrabar_path(bar):
                self._ts = ts
                self._match(bar.symbol, price, ts)
        finally:
            self._fill_sink = None
        self._ts = max(self._ts, bar.ts_close)

    def _intrabar_path(self, bar: Bar) -> Iterator[tuple[float, float]]:
        """把 OHLC 展开成带时间戳的价格序列。

        排序规则（pessimistic=True 时）：
        - 该标的存在 SELL 保护单（多单）→ 先走低点，让止损先于止盈触发；
        - 存在 BUY 保护单（空单）→ 先走高点；
        - 否则按 K 线方向 O→C，极值按多空顺/逆序。
        """
        span = max(1, bar.ts_close - bar.ts)
        t = [bar.ts + span * f for f in (0.0, 0.33, 0.66, 0.999)]

        has_sell_stop = any(
            o.symbol == bar.symbol and o.is_protective and o.side is Side.SELL
            for o in self._working.values()
        )
        has_buy_stop = any(
            o.symbol == bar.symbol and o.is_protective and o.side is Side.BUY
            for o in self._working.values()
        )
        if self.pessimistic and has_sell_stop:
            seq = [(bar.open, t[0]), (bar.low, t[1]), (bar.high, t[2]), (bar.close, t[3])]
        elif self.pessimistic and has_buy_stop:
            seq = [(bar.open, t[0]), (bar.high, t[1]), (bar.low, t[2]), (bar.close, t[3])]
        elif bar.is_bull:
            seq = [(bar.open, t[0]), (bar.low, t[1]), (bar.high, t[2]), (bar.close, t[3])]
        else:
            seq = [(bar.open, t[0]), (bar.high, t[1]), (bar.low, t[2]), (bar.close, t[3])]
        return iter(seq)

    def _match(self, symbol: str, price: float, ts: int) -> None:
        for order in list(self._working.values()):
            if order.symbol != symbol or not order.status.is_working:
                continue
            if order.type is OrderType.LIMIT:
                if self._limit_filled(order, price):
                    self._execute(order, order.qty, order.price or price, ts=ts, maker=True)
            elif order.type is OrderType.STOP_MARKET:
                if self._stop_triggered(order, price):
                    fill_px = self._adverse(price, order.side)
                    self._execute(order, order.qty, fill_px, ts=ts, maker=False)
                    self.stats["stop_triggered"] += 1
            elif order.type is OrderType.TAKE_PROFIT_MARKET:
                if self._tp_triggered(order, price):
                    self._execute(order, order.qty, order.stop_price or price, ts=ts, maker=False)
                    self.stats["tp_triggered"] += 1

    # ------------------------------------------------------------------ #
    # 判定
    # ------------------------------------------------------------------ #
    @staticmethod
    def _limit_crossed(order: Order, ref: float) -> bool:
        if ref <= 0 or order.price is None:
            return False
        return price_crosses(ref, order.side, order.price)

    @staticmethod
    def _limit_filled(order: Order, price: float) -> bool:
        if order.price is None:
            return False
        return price <= order.price if order.side is Side.BUY else price >= order.price

    @staticmethod
    def _stop_triggered(order: Order, price: float) -> bool:
        if order.stop_price is None:
            return False
        return price <= order.stop_price if order.side is Side.SELL else price >= order.stop_price

    @staticmethod
    def _tp_triggered(order: Order, price: float) -> bool:
        if order.stop_price is None:
            return False
        return price >= order.stop_price if order.side is Side.SELL else price <= order.stop_price

    # ------------------------------------------------------------------ #
    # 成交
    # ------------------------------------------------------------------ #
    def _execute(self, order: Order, qty: float, price: float, *, ts: int, maker: bool) -> None:
        fee_rate = self.cfg.maker_fee if maker else self.cfg.taker_fee
        fee = abs(price * qty) * fee_rate
        fill = Fill(
            ts=ts,
            order_id=order.client_id,
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            fee=fee,
            is_maker=maker,
            reason=order.tag,
        )
        order.apply_fill(fill)
        sink = self._fill_sink or self._persistent_sink
        if sink is not None:
            sink(fill)  # 同步回调：OMS 可立即挂保护单
        else:
            self._fills.append(fill)
        self._prices[order.symbol] = price
        self._ts = max(self._ts, ts)
        if order.status is OrderStatus.FILLED:
            self._working.pop(order.client_id, None)
            self.stats["filled"] += 1

    def _market_price(self, symbol: str, side: Side) -> float:
        ref = self._prices.get(symbol, 0.0)
        return self._adverse(ref, side) if ref > 0 else 0.0

    def _adverse(self, price: float, side: Side) -> float:
        slip = self.cfg.slippage_bps / 10_000.0
        return price * (1.0 + slip) if side is Side.BUY else price * (1.0 - slip)


def price_crosses(ref: float, side: Side, limit: float) -> bool:
    """参考价是否已经穿过限价（用于 post-only / IOC 判定）。"""
    return ref <= limit if side is Side.BUY else ref >= limit


def round_qty(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step + 1e-12) * step
