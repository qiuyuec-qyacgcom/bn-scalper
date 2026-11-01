"""币安 UM 永续实盘 Broker。

与 `SimulatedBroker` 的差异（这是唯一需要分别实现的地方）：
- 下单/撤单走签名 REST；
- 成交**不能**同步拿到，必须由用户数据流（`ORDER_TRADE_UPDATE`）回填；
- 必须处理：时间戳漂移(-1021)、限频(-1003)、post-only 拒单(-5022)、
  精度错误(-1111/-4014)、余额不足(-2019)、reduceOnly 冲突(-2022/-2021)。

关键设计：
1. **幂等**：`newClientOrderId` 由 OMS 生成并在重试时复用，网络超时后
   先 `query_order` 确认是否已成交，绝不盲目重下。
2. **状态以交易所为准**：本地 Order 只是缓存，`reconcile()` 定期对齐。
3. **fail-closed**：任何"结果未知"的情况都先查单，查不到就按未成交处理并
   由 OMS 的保护单兜底逻辑收尾。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Sequence

from ..config import ExecutionConfig, MarketConfig
from ..models import Fill, Order, OrderStatus, OrderType, Side, TimeInForce, now_ms
from .base import Broker, BrokerError, OrderRejected
from .simulator import SimulatedBroker

log = logging.getLogger(__name__)


# 币安错误码 → 语义分类
_RETRYABLE_CODES = {-1021, -1003, -1001, -1000, -1007}
_REJECT_CODES = {
    -2010: "INSUFFICIENT_MARGIN",
    -2019: "MARGIN_NOT_ENOUGH",
    -2022: "REDUCE_ONLY_REJECT",
    -2021: "WOULD_TRIGGER_IMMEDIATELY",
    -5022: "POST_ONLY_REJECT",
    -4164: "NOTIONAL_TOO_SMALL",
    -1111: "PRECISION_ERROR",
    -4014: "PRICE_FILTER_FAIL",
    -4131: "PERCENT_PRICE_FAIL",
}

_STATUS_MAP = {
    "NEW": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}


class BinanceBroker(Broker):
    def __init__(
        self,
        client,
        market_cfg: MarketConfig,
        exec_cfg: ExecutionConfig,
        *,
        max_retries: int = 2,
    ) -> None:
        self.client = client
        self.market = market_cfg
        self.cfg = exec_cfg
        self.max_retries = max_retries
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._prices: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.stats = {"submitted": 0, "filled": 0, "canceled": 0, "rejected": 0, "unknown": 0}

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def reference_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    def set_price(self, symbol: str, price: float) -> None:
        if price > 0:
            self._prices[symbol] = price

    def open_orders(self, symbol: str | None = None) -> Sequence[Order]:
        return [o for o in self._orders.values() if o.status.is_working and (symbol is None or o.symbol == symbol)]

    def poll_fills(self) -> list[Fill]:
        out = self._fills
        self._fills = []
        return out

    # ------------------------------------------------------------------ #
    # 下单
    # ------------------------------------------------------------------ #
    def submit(self, order: Order) -> Order:
        if order.client_id in self._orders and self._orders[order.client_id].status.is_working:
            return self._orders[order.client_id]  # 幂等

        params = self._to_exchange_params(order)
        self._orders[order.client_id] = order
        order.created_ts = order.created_ts or now_ms()
        try:
            resp = self.client.new_order(**params)
        except Exception as exc:  # noqa: BLE001 - 统一转换
            return self._handle_submit_error(order, exc, params)

        self._apply_resp(order, resp)
        self.stats["submitted"] += 1
        if order.status.is_terminal and order.client_id in self._orders:
            self._orders.pop(order.client_id, None)
        return order

    def _to_exchange_params(self, order: Order) -> dict[str, Any]:
        base: dict[str, Any] = {
            "symbol": order.symbol,
            "side": order.side.value.upper(),
            "newClientOrderId": order.client_id,
        }
        if order.type is OrderType.MARKET:
            base["type"] = "MARKET"
        elif order.type is OrderType.LIMIT:
            base["type"] = "LIMIT"
            base["price"] = order.price
            base["timeInForce"] = {
                TimeInForce.GTC: "GTC",
                TimeInForce.IOC: "IOC",
                TimeInForce.FOK: "FOK",
                TimeInForce.GTX: "GTX",
            }[order.tif]
        elif order.type is OrderType.STOP_MARKET:
            base["type"] = "STOP_MARKET"
            base["stopPrice"] = order.stop_price
            base["workingType"] = "MARK_PRICE"
        elif order.type is OrderType.TAKE_PROFIT_MARKET:
            base["type"] = "TAKE_PROFIT_MARKET"
            base["stopPrice"] = order.stop_price
            base["workingType"] = "MARK_PRICE"
        else:  # pragma: no cover
            raise BrokerError(f"不支持的订单类型 {order.type}")

        if order.reduce_only:
            base["reduceOnly"] = "true"
        if order.close_position:
            base["closePosition"] = "true"
        else:
            base["quantity"] = order.qty
        return base

    def _handle_submit_error(self, order: Order, exc: Exception, params: dict[str, Any]) -> Order:
        code = getattr(exc, "code", None)
        if code == -1021:
            try:
                self.client.sync_time()
            except Exception:  # noqa: BLE001
                pass
        if code in _RETRYABLE_CODES:
            # 结果未知：先查单，避免重复下单
            confirmed = self._query_after_error(order)
            if confirmed is not None:
                return confirmed
            raise BrokerError(f"下单结果未知，已查单确认未成交: {exc}") from exc

        if code in _REJECT_CODES:
            order.status = OrderStatus.REJECTED
            order.reject_reason = _REJECT_CODES[code]
            self.stats["rejected"] += 1
            raise OrderRejected(_REJECT_CODES[code], str(exc)) from exc

        order.status = OrderStatus.REJECTED
        order.reject_reason = str(exc)
        self.stats["rejected"] += 1
        raise OrderRejected("UNKNOWN_REJECT", str(exc)) from exc

    def _query_after_error(self, order: Order) -> Order | None:
        try:
            resp = self.client.query_order(order.symbol, origClientOrderId=order.client_id)
        except Exception:  # noqa: BLE001 - 查不到说明确实没下单
            return None
        log.warning("订单 %s 实际存在于交易所，按查单结果同步", order.client_id)
        self._apply_resp(order, resp)
        if order.status.is_terminal:
            self._orders.pop(order.client_id, None)
        return order

    def cancel(self, client_id: str) -> bool:
        order = self._orders.get(client_id)
        if order is None:
            return False
        try:
            self.client.cancel_order(order.symbol, origClientOrderId=client_id)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", None)
            if code == -2011:  # Unknown order：说明已经不在挂单簿
                order.status = OrderStatus.CANCELED
                self._orders.pop(client_id, None)
                return True
            log.error("撤单失败 %s: %s", client_id, exc)
            return False
        order.status = OrderStatus.CANCELED
        order.updated_ts = now_ms()
        self._orders.pop(client_id, None)
        self.stats["canceled"] += 1
        return True

    # ------------------------------------------------------------------ #
    # 响应解析
    # ------------------------------------------------------------------ #
    def _apply_resp(self, order: Order, resp: dict[str, Any]) -> None:
        order.exchange_id = str(resp.get("orderId", order.exchange_id or ""))
        status = _STATUS_MAP.get(str(resp.get("status", "")).upper())
        if status is not None:
            order.status = status
        executed = float(resp.get("executedQty", 0) or 0)
        avg = float(resp.get("avgPrice", 0) or 0)
        if executed > 0 and avg > 0 and executed > order.filled_qty + 1e-12:
            delta = executed - order.filled_qty
            fill = Fill(
                ts=int(resp.get("updateTime", now_ms())),
                order_id=order.client_id,
                symbol=order.symbol,
                side=order.side,
                qty=delta,
                price=avg,
                fee=abs(delta * avg) * self.cfg.taker_fee,
                is_maker=False,
                reason=order.tag,
            )
            order.apply_fill(fill)
            self._fills.append(fill)
            if order.status is OrderStatus.FILLED:
                self.stats["filled"] += 1
        order.updated_ts = now_ms()

    def on_user_event(self, event: dict[str, Any]) -> None:
        """用户数据流入口：`ORDER_TRADE_UPDATE` / `ACCOUNT_UPDATE`。"""
        kind = str(event.get("e", ""))
        if kind == "ORDER_TRADE_UPDATE":
            self._on_order_update(event.get("o", {}))
        elif kind == "ACCOUNT_UPDATE":
            acc = event.get("a", {})
            for item in acc.get("P", []) or []:
                symbol = str(item.get("s", ""))
                entry = float(item.get("ep", 0) or 0)
                if entry > 0:
                    self.set_price(symbol, entry)
        elif kind == "listenKeyExpired":
            log.warning("listenKey 过期，需要重建用户数据流")

    def _on_order_update(self, o: dict[str, Any]) -> None:
        client_id = str(o.get("c", ""))
        order = self._orders.get(client_id)
        status = _STATUS_MAP.get(str(o.get("X", "")).upper())
        last_qty = float(o.get("l", 0) or 0)
        last_px = float(o.get("L", 0) or 0)

        if order is None:
            # 未知订单（重启后/手动下单）：仍然把成交交给上层，避免账实不符
            if last_qty > 0 and last_px > 0:
                self._fills.append(
                    Fill(
                        ts=int(o.get("T", now_ms())),
                        order_id=client_id,
                        symbol=str(o.get("s", "")),
                        side=Side(str(o.get("S", "")).lower()),
                        qty=last_qty,
                        price=last_px,
                        fee=float(o.get("n", 0) or 0),
                        is_maker=bool(o.get("m", False)),
                        reason="unknown",
                    )
                )
            return

        if last_qty > 0 and last_px > 0:
            fill = Fill(
                ts=int(o.get("T", now_ms())),
                order_id=client_id,
                symbol=order.symbol,
                side=order.side,
                qty=last_qty,
                price=last_px,
                fee=abs(float(o.get("n", 0) or 0)),
                is_maker=bool(o.get("m", False)),
                reason=order.tag,
            )
            order.apply_fill(fill)
            self._fills.append(fill)
            self.stats["filled"] += 1

        if status is not None:
            order.status = status
        if order.status.is_terminal:
            self._orders.pop(client_id, None)

    # ------------------------------------------------------------------ #
    # 对账
    # ------------------------------------------------------------------ #
    def fetch_positions(self) -> dict[str, float]:
        """返回 {symbol: signed_qty}（多头正、空头负）。"""
        out: dict[str, float] = {}
        for row in self.client.positions():
            qty = float(row.get("positionAmt", 0) or 0)
            if abs(qty) > 1e-12:
                out[str(row["symbol"])] = qty
        return out

    def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return list(self.client.open_orders(symbol))

    def close(self) -> None:
        return None


class PaperBroker(Broker):
    """纸上交易：真实行情 + 模拟撮合。

    直接复用 `SimulatedBroker`，只是把"行情推进"的职责交给实盘引擎。
    """

    def __init__(self, market_cfg: MarketConfig, exec_cfg: ExecutionConfig) -> None:
        self._sim = SimulatedBroker(market_cfg, exec_cfg)

    def submit(self, order: Order) -> Order:
        return self._sim.submit(order)

    def cancel(self, client_id: str) -> bool:
        return self._sim.cancel(client_id)

    def open_orders(self, symbol: str | None = None) -> Sequence[Order]:
        return self._sim.open_orders(symbol)

    def poll_fills(self) -> list[Fill]:
        return self._sim.poll_fills()

    def reference_price(self, symbol: str) -> float:
        return self._sim.reference_price(symbol)

    def set_price(self, symbol: str, price: float) -> None:
        self._sim._prices[symbol] = price

    def current_ts(self) -> int:
        return self._sim.current_ts()

    def set_fill_sink(self, sink) -> None:
        self._sim.set_fill_sink(sink)

    def on_bar(self, bar, on_fill=None) -> None:
        self._sim.on_bar(bar, on_fill)

    @property
    def stats(self) -> dict[str, int]:
        return self._sim.stats
