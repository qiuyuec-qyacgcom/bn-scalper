"""核心单元测试：指标、仓位计算、账务。

原则：**只测不变量（invariant），不测具体数值**。具体数值会随参数调整而失效，
不变量（如"权益 = 现金 + 浮盈""仓位风险 ≈ 预算"）才是系统正确性的保证。
"""

from __future__ import annotations

import math
import unittest

from scalpx.config import ExecutionConfig, MarketConfig, RiskConfig
from scalpx.indicators import Atr, Ema, IndicatorSet, Rsi, Sma
from scalpx.models import Bar, Fill, Side
from scalpx.portfolio import Portfolio
from scalpx.risk.costs import cost_to_risk_ratio, net_rr, round_trip_cost
from scalpx.risk.sizing import size_position


def make_bar(i: int, close: float, *, high: float | None = None, low: float | None = None, vol: float = 10.0) -> Bar:
    return Bar(
        symbol="BTCUSDT",
        ts=1_700_000_000_000 + i * 60_000,
        open=close,
        high=high if high is not None else close * 1.001,
        low=low if low is not None else close * 0.999,
        close=close,
        volume=vol,
        taker_buy_base=vol * 0.55,
    )


class TestIndicators(unittest.TestCase):
    def test_sma_matches_manual(self) -> None:
        sma = Sma(3)
        values = [1.0, 2.0, 3.0, 4.0]
        out = [sma.update(v) for v in values]
        self.assertIsNone(out[0])
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)

    def test_ema_seeded_by_sma(self) -> None:
        ema = Ema(4)
        for v in [1, 2, 3, 4]:
            ema.update(float(v))
        # 第 4 个样本后种子完成，值应为 SMA=2.5
        self.assertAlmostEqual(ema.value, 2.5)
        ema.update(5.0)
        self.assertAlmostEqual(ema.value, 2.5 + (2 / 5) * (5.0 - 2.5))

    def test_rsi_bounds(self) -> None:
        rsi = Rsi(14)
        for i in range(60):
            rsi.update(100.0 + i)  # 单边上涨
        self.assertIsNotNone(rsi.value)
        self.assertGreater(rsi.value, 95.0)
        self.assertLessEqual(rsi.value, 100.0)

    def test_atr_positive_and_wilder(self) -> None:
        atr = Atr(5)
        value = None
        for i in range(30):
            value = atr.update_bar(make_bar(i, 100.0 + (i % 3)))
        self.assertIsNotNone(value)
        self.assertGreater(value, 0.0)

    def test_indicator_set_returns_none_until_warm(self) -> None:
        s = IndicatorSet(ema_fast=3, ema_slow=5, ema_trend=9, trend_slope_lookback=2, rsi_period=3, atr_period=3, vol_period=3)
        price = 100.0
        first_ready_at = None
        for i in range(40):
            price *= 1.0005
            snap = s.update(make_bar(i, price))
            if snap is not None and first_ready_at is None:
                first_ready_at = i
                self.assertGreater(snap.atr, 0)
                self.assertGreater(snap.ema_fast, 0)
        self.assertIsNotNone(first_ready_at)
        self.assertGreaterEqual(first_ready_at, 8)


class TestSizing(unittest.TestCase):
    def setUp(self) -> None:
        self.market = MarketConfig(symbols=("BTCUSDT",), price_tick=0.1, qty_step=0.001, min_qty=0.001, min_notional=5.0)
        self.risk = RiskConfig(risk_per_trade=0.0025, min_stop_pct=0.002, max_stop_pct=0.01)

    def test_risk_budget_respected(self) -> None:
        res = size_position(
            equity=10_000, entry=60_000, stop=59_400, take_profit=60_900,
            side_is_long=True, risk_cfg=self.risk, market_cfg=self.market,
        )
        self.assertTrue(res.ok)
        # 实际风险（qty * 止损距离）应接近预算 25 USD（受 step 取整影响）
        self.assertLessEqual(res.risk_amount, 10_000 * 0.0025 + 1e-6)
        self.assertGreater(res.risk_amount, 10_000 * 0.0025 * 0.9)

    def test_stop_clamped_and_rr_preserved(self) -> None:
        # 止损距离只有 0.02%（远小于 min_stop_pct=0.2%），应被拉宽
        res = size_position(
            equity=10_000, entry=60_000, stop=59_988, take_profit=60_030,
            side_is_long=True, risk_cfg=self.risk, market_cfg=self.market,
        )
        self.assertTrue(res.ok)
        stop_dist = abs(res.stop_price - 60_000)
        self.assertGreaterEqual(stop_dist, 60_000 * 0.002 * 0.999)
        # 盈亏比应保持原设计 1.5（30 / 20 的目标 vs 实际止损距离）
        planned_rr = abs(60_030 - 60_000) / abs(59_988 - 60_000)
        actual_rr = abs(res.take_profit_price - 60_000) / stop_dist
        self.assertAlmostEqual(actual_rr, planned_rr, delta=0.05)

    def test_min_notional_rejects(self) -> None:
        res = size_position(
            equity=10, entry=60_000, stop=59_000, take_profit=61_000,
            side_is_long=True, risk_cfg=self.risk, market_cfg=self.market,
        )
        self.assertFalse(res.ok)
        self.assertIn(res.code, ("SIZE_TOO_SMALL", "NOTIONAL_TOO_SMALL"))

    def test_short_side_symmetry(self) -> None:
        res = size_position(
            equity=10_000, entry=60_000, stop=60_600, take_profit=59_100,
            side_is_long=False, risk_cfg=self.risk, market_cfg=self.market,
        )
        self.assertTrue(res.ok)
        self.assertGreater(res.stop_price, 60_000)
        self.assertLess(res.take_profit_price, 60_000)


class TestCosts(unittest.TestCase):
    """成本模型是超短线的核心约束，必须锁死其口径。"""

    def setUp(self) -> None:
        self.exec_cfg = ExecutionConfig(maker_fee=0.0002, taker_fee=0.0004, slippage_bps=1.0)

    def test_maker_in_taker_out_cost(self) -> None:
        c = round_trip_cost(self.exec_cfg, entry_is_maker=True, exit_is_maker=False)
        # 2bps(maker 进) + 4bps(taker 出) + 1bp(出场滑点) = 7bps
        self.assertAlmostEqual(c.total, 0.0007, places=9)
        self.assertAlmostEqual(c.as_bps(), 7.0, places=6)

    def test_maker_out_has_no_slippage(self) -> None:
        c = round_trip_cost(self.exec_cfg, entry_is_maker=True, exit_is_maker=True)
        self.assertAlmostEqual(c.total, 0.0004, places=9)  # 2bps + 2bps，无滑点

    def test_cost_ratio_grows_as_stop_tightens(self) -> None:
        cost = 0.0007
        self.assertAlmostEqual(cost_to_risk_ratio(cost, 0.0080), 0.0875, places=4)
        self.assertAlmostEqual(cost_to_risk_ratio(cost, 0.0040), 0.175, places=4)
        self.assertAlmostEqual(cost_to_risk_ratio(cost, 0.0015), 0.4667, places=3)
        self.assertEqual(cost_to_risk_ratio(cost, 0.0), float("inf"))

    def test_net_rr_below_design_rr(self) -> None:
        """设计 1.5 的盈亏比，扣成本后必然低于 1.5。"""
        raw_rr = 0.006 / 0.004
        net = net_rr(take_profit_pct=0.006, stop_pct=0.004, cost_pct=0.0007)
        self.assertLess(net, raw_rr)
        self.assertAlmostEqual(net, (0.006 - 0.0007) / (0.004 + 0.0007), places=6)


class TestCostGate(unittest.TestCase):
    """止损过窄时，风控必须拦下订单（COST_TOO_HIGH）。"""

    def _run(self, min_stop_pct: float, max_cost_ratio: float):
        from scalpx.risk.manager import RiskContext, RiskManager

        market = MarketConfig(symbols=("BTCUSDT",), price_tick=0.1, qty_step=0.001, min_notional=5.0)
        risk = RiskManager(
            RiskConfig(risk_per_trade=0.0025, min_stop_pct=min_stop_pct, max_stop_pct=0.01,
                       max_cost_ratio=max_cost_ratio),
            market,
            exec_cfg=ExecutionConfig(maker_fee=0.0002, taker_fee=0.0004, slippage_bps=1.0),
        )
        from scalpx.models import EnterIntent, Side

        intent = EnterIntent(
            symbol="BTCUSDT", side=Side.BUY, ts=1_700_000_000_000, ref_price=60_000,
            stop_price=60_000 * (1 - min_stop_pct), take_profit_price=60_000 * (1 + min_stop_pct * 1.5),
        )
        ctx = RiskContext(
            ts=1_700_000_000_000, equity=10_000, positions={}, prices={"BTCUSDT": 60_000},
            spread_bps=1.0, bar=make_bar(0, 60_000), atr=100,
        )
        return risk.pre_trade(intent, ctx)

    def test_tight_stop_rejected_by_cost_gate(self) -> None:
        d = self._run(min_stop_pct=0.0015, max_cost_ratio=0.35)
        self.assertFalse(d.approved)
        self.assertEqual(d.code, "COST_TOO_HIGH")

    def test_normal_stop_passes(self) -> None:
        d = self._run(min_stop_pct=0.0040, max_cost_ratio=0.35)
        self.assertTrue(d.approved, d.message)
        self.assertIn("net_rr=", d.message)

    def test_gate_disabled_by_zero(self) -> None:
        d = self._run(min_stop_pct=0.0015, max_cost_ratio=0.0)
        self.assertTrue(d.approved, d.message)


class TestPortfolio(unittest.TestCase):
    def test_equity_identity(self) -> None:
        pf = Portfolio(initial_equity=1_000)
        ts = 1_700_000_000_000
        pf.on_fill(Fill(ts=ts, order_id="a", symbol="BTCUSDT", side=Side.BUY, qty=0.01, price=50_000, fee=0.2))
        pf.mark("BTCUSDT", 51_000)
        self.assertAlmostEqual(pf.equity(), pf.cash + pf.unrealized(), places=9)
        self.assertAlmostEqual(pf.unrealized(), 10.0)

        trade = pf.on_fill(
            Fill(ts=ts + 60_000, order_id="b", symbol="BTCUSDT", side=Side.SELL, qty=0.01, price=51_000, fee=0.204),
            reason=__import__("scalpx.models", fromlist=["ExitReason"]).ExitReason.TAKE_PROFIT,
        )
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(pf.open_positions, 0)
        self.assertAlmostEqual(trade.pnl, 10.0 - 0.2 - 0.204, places=6)
        self.assertAlmostEqual(pf.equity(), 1_000 + trade.pnl, places=6)

    def test_short_pnl_direction(self) -> None:
        pf = Portfolio(initial_equity=1_000)
        ts = 1_700_000_000_000
        pf.on_fill(Fill(ts=ts, order_id="a", symbol="ETHUSDT", side=Side.SELL, qty=1.0, price=3_000, fee=0.0))
        pf.mark("ETHUSDT", 2_900)
        self.assertAlmostEqual(pf.unrealized(), 100.0)

    def test_stop_only_tightens(self) -> None:
        pf = Portfolio(initial_equity=1_000)
        ts = 1_700_000_000_000
        pf.on_fill(Fill(ts=ts, order_id="a", symbol="BTCUSDT", side=Side.BUY, qty=0.01, price=50_000, fee=0))
        pf.attach_protection("BTCUSDT", 49_000, 52_000)
        self.assertTrue(pf.update_stop("BTCUSDT", 49_500))
        self.assertFalse(pf.update_stop("BTCUSDT", 49_200))  # 不能放宽
        self.assertAlmostEqual(pf.positions["BTCUSDT"].stop_price, 49_500)


if __name__ == "__main__":
    unittest.main()
