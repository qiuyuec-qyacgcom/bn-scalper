"""订单管理系统（OMS）。

职责边界（与 engine / risk / portfolio 的分工）：
- **engine** 决定"什么时候调用"（bar 事件、成交事件、对账事件）；
- **risk** 决定"能不能做、做多大"；
- **OMS** 决定"怎么做"：下单类型、撤单、重试、保护单、成交归属、异常兜底；
- **portfolio** 只记账，不理解订单语义。

三条不可妥协的规则：
1. **成交后必须立刻挂保护单**（fail-closed）。挂不上就市价平仓，绝不裸奔。
2. **幂等**：同一个意图重试复用 client_order_id，交易所侧不会重复成交。
3. **出场优先撤保护单**，避免保护单在平仓后反向开仓（reduceOnly 之外的二道保险）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from ..config import ExecutionConfig, MarketConfig, RiskConfig
from ..models import (
    Bar,
    ExitReason,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    Side,
    TimeInForce,
    TradeRecord,
)
from ..portfolio import Portfolio
from ..risk.manager import RiskManager, new_client_order_id
from ..risk.sizing import ceil_tick, floor_tick, round_tick
from .base import Broker, BrokerError, OrderRejected

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Tracked:
    """OMS 侧的订单元信息（不污染 Order 模型）。"""

    order: Order
    kind: str  # entry | stop | take_profit | exit
    deadline_ts: int
    attempts: int = 0
    reason: ExitReason = ExitReason.MANUAL


@dataclass(slots=True)
class OMSStats:
    entries_attempted: int = 0
    entries_filled: int = 0
    entries_expired: int = 0
    entries_rejected: int = 0
    exits: int = 0
    protections_placed: int = 0
    protections_failed: int = 0
    forced_flatten: int = 0
    reconcile_fixes: int = 0
    unmatched_fills: int = 0


class OrderManager:
    def __init__(
        self,
        broker: Broker,
        portfolio: Portfolio,
        exec_cfg: ExecutionConfig,
        market_cfg: MarketConfig,
        risk: RiskManager,
        *,
        store=None,
        risk_cfg: RiskConfig | None = None,
        bar_ms: int = 60_000,
    ) -> None:
        self.broker = broker
        self.portfolio = portfolio
        self.cfg = exec_cfg
        self.market = market_cfg
        self.risk = risk
        self.store = store
        self.risk_cfg = risk_cfg
        self.bar_ms = bar_ms
        self.tracked: dict[str, _Tracked] = {}
        self._pending_exits: set[str] = set()
        self.stats = OMSStats()
        self._seq = 0
        self._timeout_ms = max(exec_cfg.entry_timeout_ms, bar_ms)

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _next_id(self, prefix: str, symbol: str, ts: int) -> str:
        self._seq += 1
        return new_client_order_id(prefix, symbol, ts, self._seq)

    def _record_order(self, order: Order) -> None:
        if self.store is not None:
            self.store.save_order(order)

    def _record_fill(self, fill: Fill) -> None:
        if self.store is not None:
            self.store.save_fill(fill)

    def _record_trade(self, trade: TradeRecord) -> None:
        if self.store is not None:
            self.store.save_trade(trade)

    def _register(
        self,
        order: Order,
        *,
        kind: str,
        deadline_ts: int,
        attempts: int = 0,
        reason: ExitReason = ExitReason.MANUAL,
    ) -> None:
        self.tracked[order.client_id] = _Tracked(
            order=order, kind=kind, deadline_ts=deadline_ts, attempts=attempts, reason=reason
        )

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return list(self.broker.open_orders(symbol))

    def _protection_orders(self, symbol: str) -> list[Order]:
        return [o for o in self.broker.open_orders(symbol) if o.is_protective]

    # ------------------------------------------------------------------ #
    # 入场
    # ------------------------------------------------------------------ #
    def submit_entry(
        self,
        *,
        symbol: str,
        side: Side,
        qty: float,
        ts: int,
        stop_price: float,
        take_profit_price: float,
        ref_price: float,
        tag: str = "",
    ) -> bool:
        self.stats.entries_attempted += 1
        order = self._build_entry_order(
            symbol=symbol, side=side, qty=qty, ts=ts, ref_price=ref_price, tag=tag
        )
        order.meta["stop_price"] = stop_price
        order.meta["take_profit_price"] = take_profit_price
        # 先登记再提交：市价单可能在 submit() 内部同步成交并回调 OMS，
        # 若登记晚一步，成交会被当成 unknown，导致保护单缺失。
        self._register(order, kind="entry", deadline_ts=ts + self._timeout_ms, attempts=0)
        try:
            submitted = self.broker.submit(order)
        except OrderRejected as exc:
            self.tracked.pop(order.client_id, None)
            self.stats.entries_rejected += 1
            self._record_order(order)
            if exc.code == "POST_ONLY_REJECT":
                # maker 挂不上（会立即吃单）→ 直接降级为 taker，不算"异常拒单"
                log.debug("post-only 被拒，降级市价: %s", symbol)
                return self._submit_market_entry(
                    symbol=symbol, side=side, qty=qty, ts=ts,
                    stop_price=stop_price, take_profit_price=take_profit_price,
                    tag=tag, attempts=1,
                )
            self.risk.on_order_rejected(ts, exc.code, self.portfolio.equity())
            log.warning("入场被拒 %s: %s", order.client_id, exc)
            return False
        except BrokerError as exc:
            self.tracked.pop(order.client_id, None)
            self.risk.on_order_rejected(ts, "BROKER_ERROR", self.portfolio.equity())
            log.error("入场失败 %s: %s", order.client_id, exc)
            return False

        self._record_order(submitted)
        if submitted.status is OrderStatus.REJECTED:
            self.tracked.pop(order.client_id, None)
            self.risk.on_order_rejected(ts, "REJECTED", self.portfolio.equity())
            return False
        self.risk.on_order_accepted()
        return True

    def _submit_market_entry(
        self,
        *,
        symbol: str,
        side: Side,
        qty: float,
        ts: int,
        stop_price: float,
        take_profit_price: float,
        tag: str,
        attempts: int,
    ) -> bool:
        order = Order(
            client_id=self._next_id("E", symbol, ts),
            symbol=symbol,
            side=side,
            type=OrderType.MARKET,
            qty=qty,
            ts=ts,
            tif=TimeInForce.IOC,
            tag=tag or "entry",
            created_ts=ts,
            meta={"stop_price": stop_price, "take_profit_price": take_profit_price},
        )
        self._register(order, kind="entry", deadline_ts=ts + self._timeout_ms, attempts=attempts)
        try:
            submitted = self.broker.submit(order)
        except (OrderRejected, BrokerError) as exc:
            self.tracked.pop(order.client_id, None)
            self.risk.on_order_rejected(ts, getattr(exc, "code", "MARKET_FAIL"), self.portfolio.equity())
            log.error("市价入场失败 %s: %s", symbol, exc)
            return False
        self._record_order(submitted)
        self.risk.on_order_accepted()
        return True

    def _build_entry_order(
        self, *, symbol: str, side: Side, qty: float, ts: int, ref_price: float, tag: str
    ) -> Order:
        mode = self.cfg.entry_order_type
        if mode == "market":
            return Order(
                client_id=self._next_id("E", symbol, ts),
                symbol=symbol,
                side=side,
                type=OrderType.MARKET,
                qty=qty,
                ts=ts,
                tif=TimeInForce.IOC,
                tag=tag or "entry",
                created_ts=ts,
            )
        # maker 优先：必须挂在同侧最优价**之外**，否则 post-only 一定被拒。
        # 因此至少让出一个 tick，再叠加可配的 bps 偏移。
        offset = max(ref_price * self.cfg.entry_offset_bps / 10_000.0, self.market.price_tick)
        limit = ref_price - offset if side is Side.BUY else ref_price + offset
        limit = floor_tick(limit, self.market.price_tick) if side is Side.BUY else ceil_tick(
            limit, self.market.price_tick
        )
        return Order(
            client_id=self._next_id("E", symbol, ts),
            symbol=symbol,
            side=side,
            type=OrderType.LIMIT,
            qty=qty,
            ts=ts,
            price=limit,
            tif=TimeInForce.GTX if mode == "limit_post_only" else TimeInForce.IOC,
            tag=tag or "entry",
            created_ts=ts,
        )

    def _place_protection(self, pos: Position) -> bool:
        side = pos.side.exit_side
        ts = pos.entry_ts
        placed = 0
        for kind, trigger, otype in (
            ("stop", pos.stop_price, OrderType.STOP_MARKET),
            ("take_profit", pos.take_profit_price, OrderType.TAKE_PROFIT_MARKET),
        ):
            if trigger <= 0:
                continue
            order = Order(
                client_id=self._next_id("X", pos.symbol, ts),
                symbol=pos.symbol,
                side=side,
                type=otype,
                qty=pos.qty,
                ts=ts,
                stop_price=round_tick(trigger, self.market.price_tick),
                reduce_only=True,
                close_position=True,
                tag=kind,
                created_ts=ts,
            )
            self._register(order, kind=kind, deadline_ts=ts + self.cfg.stop_place_deadline_ms)
            try:
                submitted = self.broker.submit(order)
                self._record_order(submitted)
                if submitted.status is OrderStatus.REJECTED:
                    self.tracked.pop(order.client_id, None)
                    continue
                placed += 1
            except (OrderRejected, BrokerError) as exc:
                self.tracked.pop(order.client_id, None)
                log.error("保护单 %s 失败 %s: %s", kind, pos.symbol, exc)
        self.stats.protections_placed += placed
        # 止损是硬要求；止盈缺失只降级不否决
        has_stop = any(o.type is OrderType.STOP_MARKET for o in self._protection_orders(pos.symbol))
        return has_stop

    # ------------------------------------------------------------------ #
    # 出场
    # ------------------------------------------------------------------ #
    def submit_exit(self, *, symbol: str, ts: int, reason: ExitReason, price: float | None = None) -> bool:
        pos = self.portfolio.position(symbol)
        if pos is None:
            return False
        self._cancel_symbol_orders(symbol, keep_entry=False)
        order = Order(
            client_id=self._next_id("E", symbol, ts),
            symbol=symbol,
            side=pos.side.exit_side,
            type=OrderType.MARKET,
            qty=pos.qty,
            ts=ts,
            tif=TimeInForce.IOC,
            reduce_only=True,
            tag=f"exit:{reason.value}",
            created_ts=ts,
        )
        self._register(order, kind="exit", deadline_ts=ts, reason=reason)
        self._pending_exits.add(symbol)
        try:
            submitted = self.broker.submit(order)
        except (OrderRejected, BrokerError) as exc:
            self.tracked.pop(order.client_id, None)
            log.error("平仓失败 %s: %s", symbol, exc)
            # 兜底：直接改账本价格强平，避免持仓悬空（实盘由对账任务处理）
            self._force_flatten(pos, ts=ts, reason=reason)
            return False
        self._record_order(submitted)
        self.stats.exits += 1
        return True

    def adjust_stop(self, *, symbol: str, ts: int, new_stop: float) -> bool:
        pos = self.portfolio.position(symbol)
        if pos is None:
            return False
        if not self.portfolio.update_stop(symbol, new_stop):
            return False
        # 撤旧挂新：先撤原止损，再按新价挂出
        for order in self._protection_orders(symbol):
            if order.type is OrderType.STOP_MARKET:
                self.broker.cancel(order.client_id)
                self.tracked.pop(order.client_id, None)
        new_order = Order(
            client_id=self._next_id("X", symbol, ts),
            symbol=symbol,
            side=pos.side.exit_side,
            type=OrderType.STOP_MARKET,
            qty=pos.qty,
            ts=ts,
            stop_price=round_tick(new_stop, self.market.price_tick),
            reduce_only=True,
            close_position=True,
            tag="stop",
            created_ts=ts,
        )
        self._register(new_order, kind="stop", deadline_ts=ts)
        try:
            submitted = self.broker.submit(new_order)
        except (OrderRejected, BrokerError) as exc:
            self.tracked.pop(new_order.client_id, None)
            log.error("移动止损失败 %s: %s", symbol, exc)
            # 旧止损已撤 → 必须补回旧价，否则裸奔
            fallback = Order(
                client_id=self._next_id("X", symbol, ts),
                symbol=symbol,
                side=pos.side.exit_side,
                type=OrderType.STOP_MARKET,
                qty=pos.qty,
                ts=ts,
                stop_price=round_tick(pos.stop_price, self.market.price_tick),
                reduce_only=True,
                close_position=True,
                tag="stop",
                created_ts=ts,
            )
            self._register(fallback, kind="stop", deadline_ts=ts)
            try:
                self.broker.submit(fallback)
            except (OrderRejected, BrokerError):
                self.tracked.pop(fallback.client_id, None)
                self._force_flatten(pos, ts=ts, reason=ExitReason.RISK_LIQUIDATION)
            return False
        self._record_order(submitted)
        return True

    def _cancel_symbol_orders(self, symbol: str, *, keep_entry: bool = False) -> None:
        for order in list(self.broker.open_orders(symbol)):
            tracked = self.tracked.get(order.client_id)
            if keep_entry and tracked is not None and tracked.kind == "entry":
                continue
            self.broker.cancel(order.client_id)
            self.tracked.pop(order.client_id, None)

    def _force_flatten(self, pos: Position, *, ts: int, reason: ExitReason) -> None:
        """最后一道防线：不走撮合，直接按参考价改账本。"""
        self.stats.forced_flatten += 1
        price = self.broker.reference_price(pos.symbol) or pos.entry_price
        fill = Fill(
            ts=ts,
            order_id="force-flatten",
            symbol=pos.symbol,
            side=pos.side.exit_side,
            qty=pos.qty,
            price=price,
            fee=abs(price * pos.qty) * self.cfg.taker_fee,
            is_maker=False,
            reason=reason.value,
        )
        trade = self.portfolio.on_fill(fill, reason=reason)
        self._record_fill(fill)
        self._pending_exits.discard(pos.symbol)
        if trade is not None:
            self._record_trade(trade)
            self.risk.on_trade_closed(trade, ts, self.portfolio.equity())
        self._cancel_symbol_orders(pos.symbol)

    # ------------------------------------------------------------------ #
    # 事件推进
    # ------------------------------------------------------------------ #
    def handle_fill(self, fill: Fill, now: int) -> list[TradeRecord]:
        """同步成交回调入口（回测 intrabar / 实盘用户数据流都会走这里）。

        为什么必须同步：入场成交后要**在同一根 bar 内**挂上保护单，
        否则盘中反向插针无法被止损，回测会系统性低估尾部风险。
        """
        return self._handle_fill(fill, now)

    def drain(self, now: int) -> list[TradeRecord]:
        """消费 Broker 队列里的成交（实盘/无 sink 场景使用）。"""
        trades: list[TradeRecord] = []
        for fill in self.broker.poll_fills():
            trades.extend(self._handle_fill(fill, now))
        return trades

    def advance(self, bar: Bar) -> list[TradeRecord]:
        """消费成交、处理超时、校验保护单。返回本次平仓产生的交易。"""
        now = bar.ts_close

        # 1) 成交归属
        trades = self.drain(now)

        # 2) 入场超时撤单 / 升级为 taker
        self._sweep_entry_timeouts(now)

        # 3) 保护单兜底校验（正在平仓的标的不重复处理，避免二次平仓）
        for symbol, pos in list(self.portfolio.positions.items()):
            if symbol in self._pending_exits:
                continue
            if not self._protection_orders(symbol):
                if pos.stop_price > 0:
                    log.warning("%s 保护单丢失，补挂", symbol)
                    if not self._place_protection(pos):
                        self._force_flatten(pos, ts=now, reason=ExitReason.RISK_LIQUIDATION)
                else:
                    self._force_flatten(pos, ts=now, reason=ExitReason.RISK_LIQUIDATION)

        # 4) 硬性持仓时长上限（风控兜底，策略可能忘记）
        if self.risk_cfg is not None:
            for symbol, pos in list(self.portfolio.positions.items()):
                if pos.bars_held >= self.risk_cfg.max_bars_held and symbol not in self._pending_exits:
                    self.submit_exit(symbol=symbol, ts=now, reason=ExitReason.TIME_STOP)

        # 5) 本步内新产生的成交（如市价平仓）立即归属
        trades.extend(self.drain(now))
        return trades

    def _handle_fill(self, fill: Fill, now: int) -> list[TradeRecord]:
        tracked = self.tracked.get(fill.order_id)
        kind = tracked.kind if tracked else "unknown"
        reason = _reason_for(kind, tracked)

        trade = self.portfolio.on_fill(fill, reason=reason)
        self._record_fill(fill)
        self.risk.on_order_accepted()

        if kind == "entry":
            # 部分成交也立刻挂保护单（数量用实际持仓）
            stop = 0.0
            tp = 0.0
            if tracked is not None:
                stop = float(tracked.order.meta.get("stop_price", 0.0))
                tp = float(tracked.order.meta.get("take_profit_price", 0.0))
            if tracked is not None and tracked.order.status is OrderStatus.FILLED:
                self.stats.entries_filled += 1
                self.tracked.pop(fill.order_id, None)
            elif tracked is None:
                # 成交找不到归属：说明订单登记与提交的顺序被破坏，属于严重 bug
                log.error("成交 %s 无归属订单（%s），按未知成交处理", fill.order_id, fill.symbol)
                self.stats.unmatched_fills += 1
            self._after_entry_fill_tracked(fill, stop, tp, now)

        elif kind == "exit":
            self.tracked.pop(fill.order_id, None)
            self._pending_exits.discard(fill.symbol)
            self._cancel_symbol_orders(fill.symbol)

        elif kind in ("stop", "take_profit"):
            self.tracked.pop(fill.order_id, None)
            self._pending_exits.discard(fill.symbol)
            # 一侧触发 → 撤销另一侧，防止反向开仓
            self._cancel_symbol_orders(fill.symbol)

        if trade is not None:
            self._record_trade(trade)
            self.risk.on_trade_closed(trade, now, self.portfolio.equity())
        return [trade] if trade is not None else []

    def _after_entry_fill_tracked(self, fill: Fill, stop: float, tp: float, now: int) -> None:
        pos = self.portfolio.position(fill.symbol)
        if pos is None:
            return
        if stop > 0:
            pos.stop_price = round_tick(stop, self.market.price_tick)
            pos.initial_stop = pos.stop_price
        if tp > 0:
            pos.take_profit_price = round_tick(tp, self.market.price_tick)
        if not self._protection_orders(fill.symbol):
            if not self._place_protection(pos):
                self._force_flatten(pos, ts=now, reason=ExitReason.RISK_LIQUIDATION)

    def _sweep_entry_timeouts(self, now: int) -> None:
        for client_id, tracked in list(self.tracked.items()):
            if tracked.kind != "entry" or now < tracked.deadline_ts:
                continue
            order_symbol = tracked.order.symbol
            still_open = any(o.client_id == client_id for o in self.broker.open_orders(order_symbol))
            if not still_open:
                self.tracked.pop(client_id, None)
                continue
            self.broker.cancel(client_id)
            self.tracked.pop(client_id, None)
            self.stats.entries_expired += 1
            if tracked.attempts >= self.cfg.entry_max_retries:
                log.info("入场单 %s 超时放弃（已重试 %d 次）", client_id, tracked.attempts)
                continue
            # maker 未成交 → 降级吃单，保证"想成交就成交"
            log.info("入场单 %s 超时，降级为市价重试（第 %d 次）", client_id, tracked.attempts + 1)
            self._submit_market_entry(
                symbol=order_symbol,
                side=tracked.order.side,
                qty=tracked.order.qty,
                ts=now,
                stop_price=float(tracked.order.meta.get("stop_price", 0.0)),
                take_profit_price=float(tracked.order.meta.get("take_profit_price", 0.0)),
                tag=tracked.order.tag,
                attempts=tracked.attempts + 1,
            )

    # ------------------------------------------------------------------ #
    # 收尾
    # ------------------------------------------------------------------ #
    def flatten_all(self, ts: int, prices: dict[str, float], reason: ExitReason) -> list[TradeRecord]:
        out: list[TradeRecord] = []
        for symbol in list(self.portfolio.positions):
            self._cancel_symbol_orders(symbol)
            out.extend(self.portfolio.close_all(ts, prices, reason))
        for trade in out:
            self._record_trade(trade)
        return out

    def describe(self) -> dict[str, int]:
        return {
            "entries_attempted": self.stats.entries_attempted,
            "entries_filled": self.stats.entries_filled,
            "entries_expired": self.stats.entries_expired,
            "entries_rejected": self.stats.entries_rejected,
            "exits": self.stats.exits,
            "protections_placed": self.stats.protections_placed,
            "protections_failed": self.stats.protections_failed,
            "forced_flatten": self.stats.forced_flatten,
        }


def _reason_for(kind: str, tracked: _Tracked | None) -> ExitReason:
    if kind == "stop":
        return ExitReason.STOP_LOSS
    if kind == "take_profit":
        return ExitReason.TAKE_PROFIT
    if kind == "exit":
        return tracked.reason if tracked else ExitReason.MANUAL
    return ExitReason.MANUAL
