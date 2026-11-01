"""微观结构层测试：CVD 口径、吸收度、盘口 OBI/微价格、以及降级链。

这一层的 bug 不会让程序崩溃，只会让因子"看起来很合理"但方向是错的，
所以必须用可手算的用例把口径钉死。
"""

from __future__ import annotations

import unittest

from scalpx.microstructure import BookTracker, FlowAggregator, FlowFeatureBuilder
from scalpx.models import Bar, FlowBar


def fb(ts: int, buy: float, sell: float, *, mx: float = 0.0, mn: float = 0.0, n: int = 10) -> FlowBar:
    return FlowBar(symbol="BTCUSDT", ts=ts, buy_vol=buy, sell_vol=sell, trades=n, max_delta=mx, min_delta=mn)


class TestFlowAggregator(unittest.TestCase):
    def test_splits_by_bar_boundary(self) -> None:
        agg = FlowAggregator(bar_ms=60_000, symbol="BTCUSDT")
        # bar 0：两笔主动买、一笔主动卖
        agg.on_trade(1_000, 100.0, 2.0, False)
        agg.on_trade(2_000, 100.0, 1.0, False)
        agg.on_trade(3_000, 100.0, 0.5, True)
        # bar 1：一笔主动买
        agg.on_trade(61_000, 101.0, 3.0, False)

        b0 = agg.finalize(0)
        self.assertIsNotNone(b0)
        assert b0 is not None
        self.assertAlmostEqual(b0.buy_vol, 3.0)
        self.assertAlmostEqual(b0.sell_vol, 0.5)
        self.assertAlmostEqual(b0.delta, 2.5)
        self.assertEqual(b0.trades, 3)

        b1 = agg.finalize(60_000)
        self.assertIsNotNone(b1)
        assert b1 is not None
        self.assertAlmostEqual(b1.buy_vol, 3.0)
        self.assertAlmostEqual(b1.sell_vol, 0.0)

    def test_out_of_order_trade_does_not_corrupt(self) -> None:
        """新 bar 的成交先到，旧 bar 的成交后到——两者都不能串。"""
        agg = FlowAggregator(bar_ms=60_000)
        agg.on_trade(61_000, 100.0, 1.0, False)   # 新 bar 先到
        agg.on_trade(59_000, 100.0, 5.0, False)   # 旧 bar 后到
        self.assertAlmostEqual(agg.finalize(0).buy_vol, 5.0)   # type: ignore[union-attr]
        self.assertAlmostEqual(agg.finalize(60_000).buy_vol, 1.0)  # type: ignore[union-attr]

    def test_intrabar_delta_extremes(self) -> None:
        """先买后卖：max_delta 记录峰值，min_delta 记录谷值。"""
        agg = FlowAggregator(bar_ms=60_000)
        agg.on_trade(1_000, 100.0, 10.0, False)   # +10
        agg.on_trade(2_000, 100.0, 5.0, False)    # +15 (峰值)
        agg.on_trade(3_000, 100.0, 12.0, True)    # -12 -> +3
        b = agg.finalize(0)
        assert b is not None
        self.assertAlmostEqual(b.max_delta, 15.0)
        self.assertAlmostEqual(b.delta, 3.0)
        self.assertGreaterEqual(b.min_delta, 0.0)

    def test_large_trade_bucket(self) -> None:
        agg = FlowAggregator(bar_ms=60_000, large_trade_usd=50_000)
        agg.on_trade(1_000, 60_000, 1.0, False)   # 6万，算大单
        agg.on_trade(2_000, 60_000, 0.1, False)   # 6千，不算
        b = agg.finalize(0)
        assert b is not None
        self.assertAlmostEqual(b.large_vol, 1.0)

    def test_finalize_unknown_bar_returns_none(self) -> None:
        agg = FlowAggregator(bar_ms=60_000)
        self.assertIsNone(agg.finalize(0))
        agg.on_trade(1_000, 100.0, 1.0, False)
        self.assertIsNone(agg.finalize(60_000))


class TestFlowFeatures(unittest.TestCase):
    def test_delta_ratio_and_cvd_accumulate(self) -> None:
        b = FlowFeatureBuilder(window=3)
        s1 = b.update(fb(0, 60.0, 40.0))
        self.assertAlmostEqual(s1.delta_ratio, 0.2)          # (60-40)/100
        self.assertAlmostEqual(s1.cvd, 20.0)
        s2 = b.update(fb(60_000, 30.0, 50.0))
        self.assertAlmostEqual(s2.cvd, 0.0)                  # 20 + (-20)
        self.assertAlmostEqual(s2.delta_ratio, -0.25)

    def test_cvd_slope_is_normalised(self) -> None:
        b = FlowFeatureBuilder(window=2)
        b.update(fb(0, 60.0, 40.0))      # delta +20, vol 100
        s = b.update(fb(60_000, 60.0, 40.0))   # delta +20, vol 100
        # 窗口内净 delta 40 / 总量 200 = 0.2
        self.assertAlmostEqual(s.cvd_slope, 0.2)

    def test_absorption_measures_giveback(self) -> None:
        b = FlowFeatureBuilder(window=5)
        # 冲到 +30 后回落到 +5：吸收度 = (30-5)/35 ≈ 0.714
        s = b.update(fb(0, 55.0, 50.0, mx=30.0, mn=-5.0))
        self.assertAlmostEqual(s.absorption, 25.0 / 35.0, places=4)

    def test_absorption_zero_at_peak(self) -> None:
        b = FlowFeatureBuilder(window=5)
        s = b.update(fb(0, 60.0, 40.0, mx=20.0, mn=0.0))
        self.assertAlmostEqual(s.absorption, 0.0)

    def test_no_data_snapshot(self) -> None:
        b = FlowFeatureBuilder()
        s = b.update(FlowBar(symbol="BTCUSDT", ts=0, buy_vol=0, sell_vol=0, trades=0))
        self.assertFalse(s.has_data)


class TestBookTracker(unittest.TestCase):
    def setUp(self) -> None:
        self.tracker = BookTracker(depth_bps=5.0, levels=20, max_age_ms=2000)
        self.bids = [(100.0, 5.0), (99.99, 10.0), (99.90, 20.0)]   # 最后一档超出 5bps(≈0.05)
        self.asks = [(100.01, 4.0), (100.02, 8.0), (100.10, 30.0)]

    def test_imbalance_uses_band_and_decay(self) -> None:
        snap = self.tracker.apply("BTCUSDT", 1_000, self.bids, self.asks)
        assert snap is not None
        self.assertEqual(snap.bid_px, 100.0)
        self.assertEqual(snap.ask_px, 100.01)
        # 买盘更厚 -> 正失衡
        self.assertGreater(snap.imbalance, 0.0)
        self.assertLess(snap.imbalance, 1.0)
        # 99.90 超出 ±5bps 带宽，不应计入
        self.assertLess(snap.bid_depth, 5.0 + 10.0 + 20.0)

    def test_microprice_leans_to_thin_side(self) -> None:
        """卖盘薄（4）买盘厚（5）→ 微价格偏向卖一，即价格上行压力。"""
        snap = self.tracker.apply("BTCUSDT", 1_000, self.bids, self.asks)
        assert snap is not None
        mid = (100.0 + 100.01) / 2
        self.assertGreater(snap.microprice, mid)
        self.assertGreater(snap.microprice_bias_bps, 0.0)

    def test_staleness_blocks_read(self) -> None:
        self.tracker.apply("BTCUSDT", 1_000, self.bids, self.asks)
        self.assertIsNotNone(self.tracker.get("BTCUSDT", 2_000))
        self.assertIsNone(self.tracker.get("BTCUSDT", 10_000))  # 超过 2s

    def test_crossed_book_rejected(self) -> None:
        snap = self.tracker.apply("BTCUSDT", 1_000, [(100.5, 1.0)], [(100.0, 1.0)])
        self.assertIsNone(snap)
        self.assertEqual(self.tracker.stats["stale_drops"], 1)

    def test_out_of_order_snapshot_ignored(self) -> None:
        self.tracker.apply("BTCUSDT", 2_000, self.bids, self.asks)
        old = self.tracker.apply("BTCUSDT", 1_000, [(99.0, 1.0)], [(99.1, 1.0)])
        assert old is not None
        self.assertEqual(old.ts, 2_000)  # 保留较新的快照
        self.assertEqual(self.tracker.stats["out_of_order"], 1)


class TestStackedImbalance(unittest.TestCase):
    """堆叠失衡：连续多档同向才算"价格墙"，单档失衡只能是噪声。"""

    def _ladder(self, bid_qty: float, ask_qty: float, levels: int = 4):
        bids = [(round(100.0 - i * 0.1, 2), bid_qty) for i in range(levels)]
        asks = [(round(100.1 + i * 0.1, 2), ask_qty) for i in range(levels)]
        return bids, asks

    def test_detects_bid_wall(self) -> None:
        from scalpx.microstructure import compute_stacked_imbalance

        bids, asks = self._ladder(100.0, 1.0)
        stack = compute_stacked_imbalance(bids, asks, tick=0.1, ratio=3.0, stack_range=3)
        self.assertGreaterEqual(stack.bid_levels, 3)
        self.assertEqual(stack.ask_levels, 0)
        self.assertAlmostEqual(stack.score, 1.0)
        self.assertTrue(stack.has_stack)

    def test_detects_ask_wall_mirrored(self) -> None:
        from scalpx.microstructure import compute_stacked_imbalance

        bids, asks = self._ladder(1.0, 100.0)
        stack = compute_stacked_imbalance(bids, asks, tick=0.1, ratio=3.0, stack_range=3)
        self.assertGreaterEqual(stack.ask_levels, 3)
        self.assertEqual(stack.bid_levels, 0)
        self.assertAlmostEqual(stack.score, -1.0)

    def test_single_level_is_not_a_stack(self) -> None:
        from scalpx.microstructure import compute_stacked_imbalance

        bids = [(100.0, 1000.0), (99.9, 1.0), (99.8, 1.0), (99.7, 1.0)]
        asks = [(100.1, 1.0), (100.2, 1.0), (100.3, 1.0), (100.4, 1000.0)]
        stack = compute_stacked_imbalance(bids, asks, tick=0.1, ratio=3.0, stack_range=3)
        self.assertEqual(stack.bid_levels, 0)   # 只有 1 档，达不到 stack_range
        self.assertEqual(stack.ask_levels, 0)

    def test_min_level_qty_filters_noise(self) -> None:
        from scalpx.microstructure import compute_stacked_imbalance

        bids, asks = self._ladder(2.0, 0.5)   # 量都很小
        stack = compute_stacked_imbalance(
            bids, asks, tick=0.1, ratio=3.0, stack_range=3, min_level_qty=10.0
        )
        self.assertEqual(stack.bid_levels, 0)

    def test_balanced_book_has_no_stack(self) -> None:
        from scalpx.microstructure import compute_stacked_imbalance

        bids, asks = self._ladder(10.0, 10.0)
        stack = compute_stacked_imbalance(bids, asks, tick=0.1, ratio=3.0, stack_range=3)
        self.assertAlmostEqual(stack.score, 0.0)

    def test_tracker_attaches_stack_to_snapshot(self) -> None:
        tracker = BookTracker(tick=0.1, stack_ratio=3.0, stack_range=3)
        bids, asks = self._ladder(100.0, 1.0)
        snap = tracker.apply("BTCUSDT", 1_000, bids, asks)
        assert snap is not None
        self.assertGreaterEqual(snap.bid_stack_levels, 3)
        self.assertAlmostEqual(snap.stack_score, 1.0)
        self.assertTrue(tracker.stack("BTCUSDT").has_stack)  # type: ignore[union-attr]


class TestBookQuality(unittest.TestCase):
    """盘口质量熔断：丢包/乱序/过期比例过高时，盘口必须被判定为不可信。"""

    def setUp(self) -> None:
        self.tracker = BookTracker(tick=0.1, max_age_ms=2_000, max_bad_ratio=0.4)
        self.bids = [(100.0, 5.0), (99.9, 10.0)]
        self.asks = [(100.1, 4.0), (100.2, 8.0)]

    def test_healthy_book_is_trusted(self) -> None:
        self.tracker.apply("BTCUSDT", 1_000, self.bids, self.asks)
        q = self.tracker.quality("BTCUSDT", 1_500)
        self.assertTrue(q.ok)
        self.assertEqual(q.reason, "OK")
        self.assertIsNotNone(self.tracker.get("BTCUSDT", 1_500))

    def test_stale_book_is_not_trusted(self) -> None:
        self.tracker.apply("BTCUSDT", 1_000, self.bids, self.asks)
        q = self.tracker.quality("BTCUSDT", 10_000)
        self.assertFalse(q.ok)
        self.assertEqual(q.reason, "STALE")
        self.assertIsNone(self.tracker.get("BTCUSDT", 10_000))

    def test_high_reject_rate_triggers_untrusted(self) -> None:
        self.tracker.apply("BTCUSDT", 1_000, self.bids, self.asks)
        for i in range(25):  # 连续交叉盘（异常快照）
            self.tracker.apply("BTCUSDT", 1_100 + i, [(100.5, 1.0)], [(100.0, 1.0)])
        q = self.tracker.quality("BTCUSDT", 2_000)
        self.assertFalse(q.ok)
        self.assertEqual(q.reason, "HIGH_REJECT_RATE")
        self.assertIsNone(self.tracker.get("BTCUSDT", 2_000))

    def test_no_data_before_first_snapshot(self) -> None:
        q = self.tracker.quality("BTCUSDT", 1_000)
        self.assertFalse(q.ok)
        self.assertEqual(q.reason, "NO_DATA")

    def test_describe_exposes_quality_fields(self) -> None:
        self.tracker.apply("BTCUSDT", 1_000, self.bids, self.asks)
        desc = self.tracker.describe("BTCUSDT", 1_200)
        for key in ("ok", "reason", "age_ms", "bad_ratio", "bid_stack", "stack_score"):
            self.assertIn(key, desc)


class TestExitImprovements(unittest.TestCase):
    """出场逻辑两处优化：保本价计入成本、订单流反转离场。"""

    def _pos(self, *, entry=100.0, stop=99.0, side=None, bars_held=1):
        from scalpx.models import Position, PositionSide

        side = side or PositionSide.LONG
        return Position(
            symbol="BTCUSDT", side=side, qty=1.0, entry_price=entry, entry_ts=0,
            stop_price=stop, take_profit_price=entry + 2, initial_stop=stop,
            bars_held=bars_held,
        )

    def _ctx(self, *, price, position, flow=None, extras=None):
        from collections import deque

        from scalpx.indicators import IndicatorSet, Snapshot
        from scalpx.models import Bar
        from scalpx.strategy.base import StrategyContext

        bar = Bar(symbol="BTCUSDT", ts=0, open=price, high=price, low=price, close=price, volume=10)
        snap = Snapshot(
            symbol="BTCUSDT", ts=0, close=price, atr=1.0, ema_fast=99.0, ema_slow=98.0,
            ema_trend=97.0, trend_slope_pct=0.001, rsi=55.0, rsi_prev=45.0,
            vol_sma=5.0, volume_ratio=1.5, atr_pct=0.01, taker_buy_ratio=None, bars_seen=500,
        )
        return StrategyContext(
            bar=bar, snapshot=snap, history=deque([bar]), indicators=IndicatorSet(),
            position=position, equity=10_000, ts=0, flow=flow, extras=extras or {},
        )

    def _strategy(self, **params):
        from scalpx.strategy.trend_pullback import TrendPullbackStrategy

        return TrendPullbackStrategy(params)

    # ---------------- 保本价必须覆盖成本 ----------------
    def test_breakeven_includes_cost_buffer(self) -> None:
        from scalpx.models import AdjustStopIntent

        s = self._strategy(be_at_r=1.0, be_offset_r=0.01, be_cost_buffer=True, trail_at_r=99)
        ctx = self._ctx(price=101.0, position=self._pos(), extras={"cost_bps": 7.0})
        intents = s._manage(ctx)
        adjust = [i for i in intents if isinstance(i, AdjustStopIntent)]
        self.assertEqual(len(adjust), 1)
        # 0.01R(0.01) 与 7bps(0.07) 取大 -> 100.07
        self.assertAlmostEqual(adjust[0].new_stop, 100.07, places=4)

    def test_breakeven_offset_wins_when_larger(self) -> None:
        from scalpx.models import AdjustStopIntent

        s = self._strategy(be_at_r=1.0, be_offset_r=0.1, be_cost_buffer=True, trail_at_r=99)
        ctx = self._ctx(price=101.0, position=self._pos(), extras={"cost_bps": 7.0})
        adjust = [i for i in s._manage(ctx) if isinstance(i, AdjustStopIntent)]
        self.assertAlmostEqual(adjust[0].new_stop, 100.1, places=4)

    def test_breakeven_without_buffer_uses_raw_offset(self) -> None:
        from scalpx.models import AdjustStopIntent

        s = self._strategy(be_at_r=1.0, be_offset_r=0.01, be_cost_buffer=False, trail_at_r=99)
        ctx = self._ctx(price=101.0, position=self._pos(), extras={"cost_bps": 7.0})
        adjust = [i for i in s._manage(ctx) if isinstance(i, AdjustStopIntent)]
        self.assertAlmostEqual(adjust[0].new_stop, 100.01, places=4)

    def test_short_breakeven_is_mirrored(self) -> None:
        from scalpx.models import AdjustStopIntent, PositionSide

        s = self._strategy(be_at_r=1.0, be_offset_r=0.01, be_cost_buffer=True, trail_at_r=99)
        pos = self._pos(entry=100.0, stop=101.0, side=PositionSide.SHORT)
        ctx = self._ctx(price=99.0, position=pos, extras={"cost_bps": 7.0})
        adjust = [i for i in s._manage(ctx) if isinstance(i, AdjustStopIntent)]
        self.assertAlmostEqual(adjust[0].new_stop, 99.93, places=4)

    # ---------------- 订单流反转离场 ----------------
    def _flow(self, delta_ratio):
        from scalpx.models import FlowSnapshot

        return FlowSnapshot(
            symbol="BTCUSDT", ts=0, buy_vol=50, sell_vol=50, trades=100, delta=delta_ratio * 100,
            delta_ratio=delta_ratio, cvd=0.0, cvd_slope=0.0, absorption=0.1,
        )

    def test_flow_reversal_exits_long(self) -> None:
        from scalpx.models import ExitReason, ExitIntent

        s = self._strategy(flow_exit_delta=0.35, trend_flip_exit=False)
        ctx = self._ctx(price=100.5, position=self._pos(), flow=self._flow(-0.5))
        exits = [i for i in s._manage(ctx) if isinstance(i, ExitIntent)]
        self.assertEqual(len(exits), 1)
        self.assertIs(exits[0].reason, ExitReason.FLOW_REVERSAL)

    def test_flow_reversal_mirrored_for_short(self) -> None:
        from scalpx.models import ExitIntent, PositionSide

        s = self._strategy(flow_exit_delta=0.35, trend_flip_exit=False)
        pos = self._pos(entry=100.0, stop=101.0, side=PositionSide.SHORT)
        ctx = self._ctx(price=99.5, position=pos, flow=self._flow(0.5))
        exits = [i for i in s._manage(ctx) if isinstance(i, ExitIntent)]
        self.assertEqual(len(exits), 1)

    def test_flow_reversal_off_by_default(self) -> None:
        from scalpx.models import ExitIntent

        s = self._strategy()
        ctx = self._ctx(price=100.5, position=self._pos(), flow=self._flow(-0.9))
        self.assertEqual([i for i in s._manage(ctx) if isinstance(i, ExitIntent)], [])

    def test_flow_reversal_yields_to_trailing_zone(self) -> None:
        """已经进入移动止损区间时，交给移动止损，不抢跑。"""
        from scalpx.models import ExitIntent

        s = self._strategy(flow_exit_delta=0.35, trend_flip_exit=False, trail_at_r=1.5)
        ctx = self._ctx(price=102.0, position=self._pos(), flow=self._flow(-0.9))
        self.assertEqual([i for i in s._manage(ctx) if isinstance(i, ExitIntent)], [])

    def test_no_flow_data_is_ignored(self) -> None:
        from scalpx.models import ExitIntent

        s = self._strategy(flow_exit_delta=0.35, trend_flip_exit=False)
        ctx = self._ctx(price=100.5, position=self._pos(), flow=None)
        self.assertEqual([i for i in s._manage(ctx) if isinstance(i, ExitIntent)], [])


class TestStrategyDegradation(unittest.TestCase):
    """策略必须在"有数据/无数据/强制模式"三种情况下行为可预测。"""

    def _ctx(self, *, flow=None, book=None, taker_ratio=None):
        from collections import deque

        from scalpx.indicators import IndicatorSet, Snapshot
        from scalpx.models import Bar
        from scalpx.strategy.base import StrategyContext

        bar = Bar(
            symbol="BTCUSDT", ts=0, open=100, high=101, low=99, close=100,
            volume=10, taker_buy_base=(10 * taker_ratio) if taker_ratio is not None else 0.0,
        )
        snap = Snapshot(
            symbol="BTCUSDT", ts=0, close=100.0, atr=1.0, ema_fast=99.0, ema_slow=98.0,
            ema_trend=97.0, trend_slope_pct=0.001, rsi=55.0, rsi_prev=45.0,
            vol_sma=5.0, volume_ratio=1.5, atr_pct=0.01, taker_buy_ratio=taker_ratio, bars_seen=500,
        )
        return StrategyContext(
            bar=bar, snapshot=snap, history=deque([bar]), indicators=IndicatorSet(),
            position=None, equity=10_000, ts=0, flow=flow, book=book,
        )

    def _strategy(self, **params):
        from scalpx.strategy.trend_pullback import TrendPullbackStrategy

        return TrendPullbackStrategy(params)

    def test_agg_trade_mode_rejects_without_data(self) -> None:
        s = self._strategy(flow_mode="agg_trade")
        self.assertFalse(s._microstructure_ok(self._ctx(taker_ratio=0.9), long_side=True))

    def test_auto_falls_back_to_taker_proxy(self) -> None:
        s = self._strategy(flow_mode="auto")
        self.assertTrue(s._microstructure_ok(self._ctx(taker_ratio=0.9), long_side=True))
        self.assertFalse(s._microstructure_ok(self._ctx(taker_ratio=0.4), long_side=True))

    def test_absorption_vetoes_entry(self) -> None:
        s = self._strategy(flow_mode="auto", max_absorption=0.85)
        exhausted = fb(0, 70.0, 30.0, mx=40.0, mn=0.0)
        # delta_ratio=0.4 满足方向，吸收度 =(40-40)/40=0 -> 放行
        ctx = self._ctx(flow=FlowFeatureBuilder().update(exhausted))
        self.assertTrue(s._microstructure_ok(ctx, long_side=True))

        # delta=+10 但峰值 +96：吸收度 =(96-10)/101≈0.85+ -> 判为衰竭
        absorbed = fb(0, 55.0, 45.0, mx=96.0, mn=-5.0)
        ctx2 = self._ctx(flow=FlowFeatureBuilder().update(absorbed))
        self.assertFalse(s._microstructure_ok(ctx2, long_side=True))

    def test_flow_off_always_passes(self) -> None:
        s = self._strategy(flow_mode="off")
        self.assertTrue(s._microstructure_ok(self._ctx(taker_ratio=0.1), long_side=True))

    def test_book_gate_when_obi_required(self) -> None:
        from scalpx.models import OrderBookSnapshot

        s = self._strategy(flow_mode="auto", min_obi=0.1)
        thin_bid = OrderBookSnapshot(
            symbol="BTCUSDT", ts=0, bid_px=100.0, ask_px=100.01,
            bid_qty=1.0, ask_qty=5.0, bid_depth=1.0, ask_depth=5.0, microprice=99.99,
        )
        ctx = self._ctx(taker_ratio=0.9, book=thin_bid)
        self.assertFalse(s._microstructure_ok(ctx, long_side=True))

        thick_bid = OrderBookSnapshot(
            symbol="BTCUSDT", ts=0, bid_px=100.0, ask_px=100.01,
            bid_qty=5.0, ask_qty=1.0, bid_depth=5.0, ask_depth=1.0, microprice=100.02,
        )
        ctx2 = self._ctx(taker_ratio=0.9, book=thick_bid)
        self.assertTrue(s._microstructure_ok(ctx2, long_side=True))

    def test_book_required_but_missing_is_rejected(self) -> None:
        """声明要看盘口却拿不到数据 -> fail-closed，而不是"当作没这回事"。"""
        s = self._strategy(flow_mode="auto", min_obi=0.1)
        self.assertTrue(s.uses_order_book)
        self.assertFalse(s._microstructure_ok(self._ctx(taker_ratio=0.9, book=None), long_side=True))

    def test_book_not_used_passes_without_data(self) -> None:
        s = self._strategy(flow_mode="auto", min_obi=0.0, min_stack_score=-1.0)
        self.assertFalse(s.uses_order_book)
        self.assertTrue(s._microstructure_ok(self._ctx(taker_ratio=0.9, book=None), long_side=True))

    def test_stack_score_gate(self) -> None:
        from scalpx.models import OrderBookSnapshot

        s = self._strategy(flow_mode="auto", min_obi=0.0, min_stack_score=0.5)
        self.assertTrue(s.uses_order_book)
        # 卖盘堆叠 -> 多头被否决
        bearish = OrderBookSnapshot(
            symbol="BTCUSDT", ts=0, bid_px=100.0, ask_px=100.01, bid_qty=1, ask_qty=1,
            bid_depth=1.0, ask_depth=1.0, microprice=100.0, stack_score=-1.0,
        )
        self.assertFalse(s._microstructure_ok(self._ctx(book=bearish), long_side=True))
        # 买盘堆叠 -> 放行
        bullish = OrderBookSnapshot(
            symbol="BTCUSDT", ts=0, bid_px=100.0, ask_px=100.01, bid_qty=1, ask_qty=1,
            bid_depth=1.0, ask_depth=1.0, microprice=100.0, stack_score=1.0,
        )
        self.assertTrue(s._microstructure_ok(self._ctx(book=bullish), long_side=True))


class TestBookUntrustedRiskGate(unittest.TestCase):
    def test_untrusted_book_blocks_entry(self) -> None:
        from scalpx.config import MarketConfig, RiskConfig
        from scalpx.models import Bar, EnterIntent, Side
        from scalpx.risk.manager import RiskContext, RiskManager

        risk = RiskManager(RiskConfig(), MarketConfig(symbols=("BTCUSDT",)))
        bar = Bar(symbol="BTCUSDT", ts=0, open=100, high=101, low=99, close=100, volume=10)
        intent = EnterIntent(
            symbol="BTCUSDT", side=Side.BUY, ts=0, ref_price=100,
            stop_price=99.5, take_profit_price=100.8,
        )
        base = dict(ts=0, equity=10_000, positions={}, prices={"BTCUSDT": 100}, spread_bps=1.0, bar=bar, atr=1.0)

        ok = risk.pre_trade(intent, RiskContext(**base))  # type: ignore[arg-type]
        self.assertTrue(ok.approved, ok.message)

        blocked = risk.pre_trade(
            intent, RiskContext(**base, book_untrusted=True, book_reason="HIGH_REJECT_RATE")  # type: ignore[arg-type]
        )
        self.assertFalse(blocked.approved)
        self.assertEqual(blocked.code, "BOOK_UNTRUSTED")
        self.assertIn("HIGH_REJECT_RATE", blocked.message)


if __name__ == "__main__":
    unittest.main()
