"""执行层测试：撮合语义、post-only、止损触发、OMS 保护单、风控闸门。

这些测试守护的是**最容易在实盘亏钱的行为**：
- 保护单是否真的挂上；
- 止损是否在逆势时先于止盈触发；
- 风控是否正确熔断。
"""

from __future__ import annotations

import unittest

from scalpx.config import ExecutionConfig, MarketConfig, RiskConfig
from scalpx.execution.base import OrderRejected
from scalpx.execution.oms import OrderManager
from scalpx.execution.simulator import SimulatedBroker
from scalpx.models import (
    Bar,
    EnterIntent,
    ExitReason,
    Order,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    Side,
    TimeInForce,
)
from scalpx.portfolio import Portfolio
from scalpx.risk.manager import RiskContext, RiskManager


def bar(ts: int, o: float, h: float, l: float, c: float, symbol: str = "BTCUSDT") -> Bar:
    return Bar(symbol=symbol, ts=ts, open=o, high=h, low=l, close=c, volume=100.0, taker_buy_base=55.0)


MARKET = MarketConfig(symbols=("BTCUSDT",), price_tick=0.1, qty_step=0.001, min_qty=0.001, min_notional=5.0)
EXEC = ExecutionConfig(maker_fee=0.0002, taker_fee=0.0004, slippage_bps=0.0)


class TestSimulator(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = SimulatedBroker(MARKET, EXEC)
        self.broker.on_bar(bar(0, 100, 101, 99, 100))

    def test_market_order_fills_immediately(self) -> None:
        order = Order("m1", "BTCUSDT", Side.BUY, OrderType.MARKET, 1.0, 0, tif=TimeInForce.IOC)
        self.broker.submit(order)
        self.assertIs(order.status, OrderStatus.FILLED)
        fills = self.broker.poll_fills()
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].fee, 100 * 1.0 * 0.0004)

    def test_post_only_rejected_when_crossing(self) -> None:
        order = Order("p1", "BTCUSDT", Side.BUY, OrderType.LIMIT, 1.0, 0, price=100.0, tif=TimeInForce.GTX)
        with self.assertRaises(OrderRejected) as ctx:
            self.broker.submit(order)
        self.assertEqual(ctx.exception.code, "POST_ONLY_REJECT")

    def test_limit_rests_then_fills_on_pullback(self) -> None:
        order = Order("l1", "BTCUSDT", Side.BUY, OrderType.LIMIT, 1.0, 0, price=98.0, tif=TimeInForce.GTX)
        self.broker.submit(order)
        self.assertIs(order.status, OrderStatus.NEW)
        # 价格没有回到 98，不成交
        self.broker.on_bar(bar(60_000, 100, 101, 99.0, 100.5))
        self.assertEqual(len(self.broker.poll_fills()), 0)
        # 回到 97.5，成交
        self.broker.on_bar(bar(120_000, 100.5, 100.6, 97.5, 98.2))
        fills = self.broker.poll_fills()
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].price, 98.0)
        self.assertTrue(fills[0].is_maker)

    def test_stop_triggers_before_take_profit_in_pessimistic_mode(self) -> None:
        """同一根 bar 同时包含止损价与止盈价时，必须假设先触发止损。"""
        broker = SimulatedBroker(MARKET, EXEC, pessimistic_intrabar=True)
        broker.on_bar(bar(0, 100, 100.5, 99.5, 100))
        stop = Order("s1", "BTCUSDT", Side.SELL, OrderType.STOP_MARKET, 1.0, 0, stop_price=99.0, reduce_only=True)
        tp = Order("t1", "BTCUSDT", Side.SELL, OrderType.TAKE_PROFIT_MARKET, 1.0, 0, stop_price=101.0, reduce_only=True)
        broker.submit(stop)
        broker.submit(tp)
        broker.on_bar(bar(60_000, 100, 102, 98, 100))
        fills = broker.poll_fills()
        self.assertGreaterEqual(len(fills), 1)
        self.assertEqual(fills[0].order_id, "s1", "逆向路径必须优先触发止损")
        self.assertLessEqual(fills[0].price, 99.0)

    def test_duplicate_client_id_is_idempotent(self) -> None:
        o1 = Order("dup", "BTCUSDT", Side.BUY, OrderType.LIMIT, 1.0, 0, price=97.0, tif=TimeInForce.GTX)
        o2 = Order("dup", "BTCUSDT", Side.BUY, OrderType.LIMIT, 1.0, 0, price=97.0, tif=TimeInForce.GTX)
        self.broker.submit(o1)
        self.broker.submit(o2)
        self.assertEqual(len(self.broker.open_orders()), 1)


class TestRiskGates(unittest.TestCase):
    def setUp(self) -> None:
        self.risk = RiskManager(
            RiskConfig(max_daily_loss_pct=0.02, risk_per_trade=0.002, min_stop_pct=0.002, max_stop_pct=0.01),
            MARKET,
        )
        self.ctx = RiskContext(
            ts=1_700_000_000_000,
            equity=10_000,
            positions={},
            prices={"BTCUSDT": 60_000},
            spread_bps=1.0,
            bar=bar(1_700_000_000_000, 60_000, 60_100, 59_900, 60_000),
            atr=100,
        )

    def _intent(self, side: Side = Side.BUY) -> EnterIntent:
        sign = 1 if side is Side.BUY else -1
        return EnterIntent(
            symbol="BTCUSDT", side=side, ts=self.ctx.ts, ref_price=60_000,
            stop_price=60_000 - sign * 300, take_profit_price=60_000 + sign * 450,
        )

    def test_approves_normal_trade(self) -> None:
        d = self.risk.pre_trade(self._intent(), self.ctx)
        self.assertTrue(d.approved, d.message)
        self.assertGreater(d.qty, 0)

    def test_daily_loss_halts(self) -> None:
        self.risk.on_bar(self.ctx.ts, 10_000)
        self.risk.on_bar(self.ctx.ts + 60_000, 9_700)  # -3% 超过 2% 上限
        self.assertTrue(self.risk.state.halted)
        d = self.risk.pre_trade(self._intent(), self.ctx)
        self.assertFalse(d.approved)
        self.assertEqual(d.code, "RISK_HALTED")

    def test_halt_resets_next_day(self) -> None:
        ts = self.ctx.ts
        self.risk.on_bar(ts, 10_000)
        self.risk.on_bar(ts + 60_000, 9_700)
        self.assertTrue(self.risk.state.halted)
        self.risk.on_bar(ts + 86_400_000, 9_700)
        self.assertFalse(self.risk.state.halted)

    def test_existing_position_blocks_same_symbol(self) -> None:
        self.ctx.positions["BTCUSDT"] = Position(
            symbol="BTCUSDT", side=PositionSide.LONG, qty=0.1, entry_price=60_000,
            entry_ts=self.ctx.ts, stop_price=59_700, take_profit_price=60_450, initial_stop=59_700,
        )
        d = self.risk.pre_trade(self._intent(), self.ctx)
        self.assertFalse(d.approved)
        self.assertEqual(d.code, "SYMBOL_POSITION_EXISTS")

    def test_wide_spread_rejected(self) -> None:
        self.ctx.spread_bps = 99.0
        d = self.risk.pre_trade(self._intent(), self.ctx)
        self.assertFalse(d.approved)
        self.assertEqual(d.code, "SPREAD_TOO_WIDE")


class TestOMSProtection(unittest.TestCase):
    def setUp(self) -> None:
        self.portfolio = Portfolio(initial_equity=10_000)
        self.broker = SimulatedBroker(MARKET, EXEC)
        self.risk = RiskManager(RiskConfig(min_stop_pct=0.002, max_stop_pct=0.01), MARKET)
        self.oms = OrderManager(self.broker, self.portfolio, EXEC, MARKET, self.risk, bar_ms=60_000)
        self.broker.set_fill_sink(lambda f: self.oms.handle_fill(f, self.broker.current_ts()))
        self.broker.on_bar(bar(0, 60_000, 60_050, 59_950, 60_000))

    def _enter(self) -> bool:
        """挂 maker 单 → 用一根回踩 bar 促成成交。"""
        ok = self.oms.submit_entry(
            symbol="BTCUSDT", side=Side.BUY, qty=0.01, ts=60_000,
            stop_price=59_700, take_profit_price=60_450, ref_price=60_000, tag="test",
        )
        self.broker.on_bar(bar(60_000, 60_000, 60_010, 59_900, 60_000))
        return ok

    def test_entry_fill_places_stop_and_tp(self) -> None:
        self.assertTrue(self._enter())
        self.assertEqual(self.portfolio.open_positions, 1)
        protections = [o for o in self.broker.open_orders() if o.is_protective]
        self.assertEqual(len(protections), 2)
        kinds = {o.type for o in protections}
        self.assertIn(OrderType.STOP_MARKET, kinds)
        self.assertIn(OrderType.TAKE_PROFIT_MARKET, kinds)
        pos = self.portfolio.position("BTCUSDT")
        assert pos is not None
        self.assertGreater(pos.stop_price, 0)

    def test_protection_fill_cancels_sibling(self) -> None:
        self._enter()
        # 价格击穿止损
        self.broker.on_bar(bar(120_000, 60_000, 60_010, 59_500, 59_600))
        self.assertEqual(self.portfolio.open_positions, 0)
        self.assertEqual([o for o in self.broker.open_orders() if o.is_protective], [])
        trades = self.portfolio.trades
        self.assertEqual(len(trades), 1)
        self.assertIs(trades[0].reason, ExitReason.STOP_LOSS)
        self.assertLess(trades[0].pnl, 0)

    def test_exit_closes_and_flattens(self) -> None:
        self._enter()
        self.oms.submit_exit(symbol="BTCUSDT", ts=120_000, reason=ExitReason.TIME_STOP)
        self.assertEqual(self.portfolio.open_positions, 0)
        self.assertEqual(len(self.portfolio.trades), 1)
        self.assertIs(self.portfolio.trades[0].reason, ExitReason.TIME_STOP)

    def test_missing_stop_forces_flatten(self) -> None:
        self._enter()
        for order in list(self.broker.open_orders()):
            self.broker.cancel(order.client_id)
        pos = self.portfolio.position("BTCUSDT")
        assert pos is not None
        pos.stop_price = 0.0  # 模拟极端异常：保护信息丢失
        self.broker.on_bar(bar(120_000, 60_000, 60_050, 59_950, 60_000))
        self.oms.advance(bar(120_000, 60_000, 60_050, 59_950, 60_000))
        self.assertEqual(self.portfolio.open_positions, 0, "无保护单的持仓必须被强平")


if __name__ == "__main__":
    unittest.main()
