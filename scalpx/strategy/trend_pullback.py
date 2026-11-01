"""主策略：趋势回踩确认（Trend Pullback Confirmation）。

适用场景：BTC/ETH 永续 1 分钟级别、持仓 3~15 分钟的超短线。

设计动机（为什么不是纯突破/纯均值回归）：
- 1m 级别的纯突破假信号率极高（插针/扫单），需要"回踩确认"过滤；
- 纯均值回归在趋势日会持续逆势加仓式亏损，需要趋势过滤；
- 因此采用 **趋势方向 + 回踩到均线 + 动量重新转强 + 主动买盘占优** 四重条件，
  只在"趋势中的短暂休息结束"这一时刻入场，天然高胜率、低持仓时间。

多头入场条件（全部满足）：
    E1 趋势：EMA_fast > EMA_slow 且 EMA_trend 斜率 > 0
    E2 波动：ATR% 落在 [atr_min_pct, atr_max_pct]（过滤死盘与瀑布）
    E3 量能：volume > vol_mult * VOL_SMA(20)
    E4 回踩：最近 N 根内最低价触及 EMA_fast + k*ATR 带
    E5 收复：当前收盘 > EMA_fast 且为阳线
    E6 动量：RSI 从 50 以下上穿 50，且未进入超买区
    E7 订单流：taker_buy_ratio > flow_min（数据不支持时按配置跳过）
空头完全镜像。

出场（由策略管理 + 交易所保护单兜底）：
    X1 初始止损：entry -/+ stop_atr_mult * ATR（风控再夹到 [min_stop_pct, max_stop_pct]）
    X2 止盈：entry ± rr * risk
    X3 保本：浮盈 >= be_at_r 时止损推到 entry ± be_offset_r * risk
    X4 移动止损：浮盈 >= trail_at_r 时止损 = close -/+ trail_atr_mult * ATR
    X5 时间止损：持仓 >= max_hold_bars 且 < 0.5R
    X6 结构反转：EMA_fast 反向穿越 EMA_slow
"""

from __future__ import annotations

from typing import Any

from ..models import AdjustStopIntent, Bar, EnterIntent, ExitIntent, ExitReason, Intent, PositionSide, Side
from .base import Strategy, StrategyContext


class TrendPullbackStrategy(Strategy):
    name = "trend_pullback"

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {
            # --- 指标 ---
            "ema_fast": 21,
            "ema_slow": 55,
            "ema_trend": 144,
            "trend_slope_lookback": 20,
            "rsi_period": 14,
            "atr_period": 14,
            "vol_period": 20,
            # --- 入场过滤 ---
            "pullback_lookback": 3,
            "pullback_atr_mult": 0.30,
            "rsi_cross": 50.0,
            "rsi_long_max": 70.0,
            "rsi_short_min": 30.0,
            "vol_mult": 1.10,
            "atr_min_pct": 0.0008,
            "atr_max_pct": 0.0060,
            # --- 订单流 / 盘口（微观结构） ---
            # flow_mode: auto（优先真实CVD，缺失时退化到 taker 占比）/ agg_trade（强制）/
            #            taker_proxy（强制用 taker 占比）/ off（关闭）
            "flow_mode": "auto",
            "min_delta_ratio": 0.04,     # 主动买卖净额 / 总量，等价于 taker 占比 52%
            "min_cvd_slope": 0.0,        # CVD 斜率下限（窗口内净 delta 占比）
            "require_cvd_slope": True,
            "max_absorption": 0.85,      # 买盘被吸收到该程度 -> 视为衰竭，放弃入场
            "min_obi": 0.0,              # 盘口失衡下限（0 = 不启用）
            "min_stack_score": -1.0,     # 堆叠失衡得分下限（-1 = 不启用）
            "require_flow": True,        # 兼容旧配置：false 等价于 flow_mode="off"
            "flow_min_long": 0.52,
            "flow_min_short": 0.48,
            # --- 出场 ---
            "stop_atr_mult": 2.00,
            "rr": 1.50,
            "be_at_r": 1.00,
            "be_offset_r": 0.10,
            # 保本价必须覆盖往返成本，否则"保本出场"其实是净亏损。
            # True 时 be_offset 取 max(be_offset_r * risk, 往返成本 * entry)
            "be_cost_buffer": True,
            "trail_at_r": 1.50,
            "trail_atr_mult": 1.20,
            "max_hold_bars": 30,
            "trend_flip_exit": True,
            # 订单流反转即离场：持仓中主动卖盘（多头）超过该阈值就撤。
            # 0 = 关闭；0.35 表示"反向主动成交占比超过 35%"
            "flow_exit_delta": 0.0,
            # --- 方向 ---
            "allow_long": True,
            "allow_short": True,
        }

    def validate_params(self) -> None:
        p = self.params
        if p["ema_fast"] >= p["ema_slow"]:
            raise ValueError("ema_fast 必须小于 ema_slow")
        if p["ema_slow"] >= p["ema_trend"]:
            raise ValueError("ema_slow 必须小于 ema_trend")
        if p["rr"] <= 0:
            raise ValueError("rr 必须 > 0")
        if p["stop_atr_mult"] <= 0:
            raise ValueError("stop_atr_mult 必须 > 0")
        if not (0 < p["flow_min_short"] < 1 and 0 < p["flow_min_long"] < 1):
            raise ValueError("flow 阈值必须在 (0,1)")
        if p["trail_at_r"] < p["be_at_r"]:
            raise ValueError("trail_at_r 应 >= be_at_r")
        if p["flow_mode"] not in ("auto", "agg_trade", "taker_proxy", "off"):
            raise ValueError(f"flow_mode 非法: {p['flow_mode']!r}")
        if not -1.0 <= p["min_delta_ratio"] <= 1.0:
            raise ValueError("min_delta_ratio 必须在 [-1,1]")
        if not 0.0 <= p["max_absorption"] <= 1.0:
            raise ValueError("max_absorption 必须在 [0,1]")
        if not -1.0 <= p["min_obi"] <= 1.0:
            raise ValueError("min_obi 必须在 [-1,1]")
        if not 0.0 <= p["flow_exit_delta"] <= 1.0:
            raise ValueError("flow_exit_delta 必须在 [0,1]")
        if p["be_offset_r"] < 0:
            raise ValueError("be_offset_r 不能为负")
        if not -1.0 <= p["min_stack_score"] <= 1.0:
            raise ValueError("min_stack_score 必须在 [-1,1]")
        # 兼容旧配置：require_flow=false 等价于关闭订单流过滤
        if not p["require_flow"]:
            self.params["flow_mode"] = "off"

    @property
    def warmup_bars(self) -> int:
        p = self.params
        return max(
            p["ema_trend"] + p["trend_slope_lookback"],
            p["ema_slow"],
            p["rsi_period"] + 2,
            p["atr_period"] + 2,
            p["vol_period"] + 2,
        ) + 5

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def on_bar(self, ctx: StrategyContext) -> list[Intent]:
        if ctx.position is not None:
            return self._manage(ctx)
        return self._look_for_entry(ctx)

    # ------------------------------------------------------------------ #
    # 入场
    # ------------------------------------------------------------------ #
    def _look_for_entry(self, ctx: StrategyContext) -> list[Intent]:
        snap, bar, p = ctx.snapshot, ctx.bar, self.params
        atr = snap.atr
        if atr <= 0:
            return []

        # E2 波动区间
        if not (p["atr_min_pct"] <= snap.atr_pct <= p["atr_max_pct"]):
            return []
        # E3 量能
        if snap.volume_ratio < p["vol_mult"]:
            return []

        recent = ctx.bars(p["pullback_lookback"])
        band = p["pullback_atr_mult"] * atr

        long_ok = (
            p["allow_long"]
            and snap.trend_up                      # E1
            and min(b.low for b in recent) <= snap.ema_fast + band  # E4
            and bar.close > snap.ema_fast          # E5
            and bar.close > bar.open
            and snap.rsi_prev < p["rsi_cross"] <= snap.rsi  # E6
            and snap.rsi < p["rsi_long_max"]
            and self._microstructure_ok(ctx, long_side=True)  # E7
        )
        if long_ok:
            return [self._build_entry(ctx, Side.BUY)]

        short_ok = (
            p["allow_short"]
            and snap.trend_down
            and max(b.high for b in recent) >= snap.ema_fast - band
            and bar.close < snap.ema_fast
            and bar.close < bar.open
            and snap.rsi_prev > p["rsi_cross"] >= snap.rsi
            and snap.rsi > p["rsi_short_min"]
            and self._microstructure_ok(ctx, long_side=False)
        )
        if short_ok:
            return [self._build_entry(ctx, Side.SELL)]

        return []

    def _build_entry(self, ctx: StrategyContext, side: Side) -> EnterIntent:
        p, bar, atr = self.params, ctx.bar, ctx.snapshot.atr
        risk = p["stop_atr_mult"] * atr
        if side is Side.BUY:
            stop = bar.close - risk
            tp = bar.close + p["rr"] * risk
        else:
            stop = bar.close + risk
            tp = bar.close - p["rr"] * risk
        return EnterIntent(
            symbol=bar.symbol,
            side=side,
            ts=bar.ts,
            ref_price=bar.close,
            stop_price=stop,
            take_profit_price=tp,
            reason=self._reason(ctx, side),
            confidence=min(1.0, 0.5 + 0.5 * min(1.0, ctx.snapshot.volume_ratio / 2.0)),
            tags={
                "atr": atr,
                "atr_pct": ctx.snapshot.atr_pct,
                "rsi": ctx.snapshot.rsi,
                "vol_ratio": ctx.snapshot.volume_ratio,
                "flow": ctx.snapshot.taker_buy_ratio,
                "micro_mode": self._active_mode(ctx),
                "delta_ratio": ctx.flow.delta_ratio if ctx.flow else None,
                "cvd_slope": ctx.flow.cvd_slope if ctx.flow else None,
                "absorption": ctx.flow.absorption if ctx.flow else None,
                "obi": ctx.book.imbalance if ctx.book else None,
            },
        )

    def _active_mode(self, ctx: StrategyContext) -> str:
        """记录本次决策实际用到的微观数据来源，便于回测归因。"""
        if ctx.flow is not None and ctx.flow.has_data and self.params["flow_mode"] in ("auto", "agg_trade"):
            return "agg_trade"
        if self.params["flow_mode"] == "agg_trade":
            return "agg_trade_missing"
        if ctx.bar.taker_buy_ratio is not None:
            return "taker_proxy"
        return "none"

    def _reason(self, ctx: StrategyContext, side: Side) -> str:
        s = ctx.snapshot
        micro = ""
        if ctx.flow is not None and ctx.flow.has_data:
            micro = f"|{ctx.flow.describe()}"
        return (
            f"{self.name}:{'long' if side is Side.BUY else 'short'}"
            f"|trend_slope={s.trend_slope_pct:+.4%}"
            f"|rsi={s.rsi_prev:.1f}->{s.rsi:.1f}"
            f"|volx={s.volume_ratio:.2f}"
            f"|atr%={s.atr_pct:.3%}"
            f"{micro}"
        )

    # ------------------------------------------------------------------ #
    # E7 微观结构过滤
    # ------------------------------------------------------------------ #
    def _microstructure_ok(self, ctx: StrategyContext, *, long_side: bool) -> bool:
        """三级降级的订单流/盘口过滤。

        优先级：真实 aggTrade（delta + CVD 斜率 + 吸收度）→ taker 占比代理 → 放行。
        降级链保证：没有微观数据时系统仍可运行，但**有数据时一定用数据**。
        """
        p, bar = self.params, ctx.bar
        mode = p["flow_mode"]
        if mode == "off":
            return True

        # 盘口是与成交流**正交**的过滤器：有没有 CVD 数据都应该生效
        if not self._book_ok(ctx, long_side=long_side):
            return False

        flow = ctx.flow
        if flow is not None and flow.has_data and mode in ("auto", "agg_trade"):
            delta_ratio = flow.delta_ratio
            pressure = delta_ratio if long_side else -delta_ratio
            if pressure < p["min_delta_ratio"]:
                return False
            if p["require_cvd_slope"]:
                slope = flow.cvd_slope
                if (slope < p["min_cvd_slope"]) if long_side else (slope > -p["min_cvd_slope"]):
                    return False
            # 吸收度：主动买盘冲高后被回吐 -> 这是衰竭而非启动
            if flow.absorption > p["max_absorption"]:
                return False
            return True

        if mode == "agg_trade":
            # 强制要求真实订单流：拿不到数据就不交易（fail-closed）
            return False

        # taker_proxy（或无 flow 时的 auto）
        ratio = bar.taker_buy_ratio
        if ratio is None:
            return True  # 完全没有微观数据：降级放行，由信号 meta 记录
        return ratio >= p["flow_min_long"] if long_side else ratio <= p["flow_min_short"]

    @property
    def uses_order_book(self) -> bool:
        """策略是否依赖盘口数据（引擎据此决定是否启用质量熔断）。"""
        p = self.params
        return p["min_obi"] > 0 or p["min_stack_score"] > -1.0

    def _book_ok(self, ctx: StrategyContext, *, long_side: bool) -> bool:
        """盘口过滤：OBI + 微价格 + 堆叠失衡。

        启用（`min_obi > 0` 或 `min_stack_score > -1`）后**没有盘口数据即拒绝入场**——
        这是有意的 fail-closed：既然声明了"要看盘口"，就不该在看不到时假装没这回事。
        """
        p = self.params
        min_obi = p["min_obi"]
        min_stack = p["min_stack_score"]
        if min_obi <= 0 and min_stack <= -1.0:
            return True

        book = ctx.book
        if book is None:
            return False

        if min_obi > 0:
            pressure = book.imbalance if long_side else -book.imbalance
            if pressure < min_obi:
                return False
            # 微价格与方向一致（顺带过滤"盘口厚但价格被压制"的情况）
            bias = book.microprice_bias_bps
            if (bias < 0) if long_side else (bias > 0):
                return False

        if min_stack > -1.0:
            stack = book.stack_score if long_side else -book.stack_score
            if stack < min_stack:
                return False
        return True

    # ------------------------------------------------------------------ #
    # 出场管理
    # ------------------------------------------------------------------ #
    def _manage(self, ctx: StrategyContext) -> list[Intent]:
        pos = ctx.position
        assert pos is not None
        p, bar, atr, snap = self.params, ctx.bar, ctx.snapshot.atr, ctx.snapshot
        price = bar.close
        r = pos.unrealized_r(price)
        out: list[Intent] = []

        # X6 结构反转
        if p["trend_flip_exit"]:
            flipped = (pos.side is PositionSide.LONG and snap.ema_fast < snap.ema_slow) or (
                pos.side is PositionSide.SHORT and snap.ema_fast > snap.ema_slow
            )
            if flipped and r < p["be_at_r"]:
                return [
                    ExitIntent(
                        symbol=bar.symbol,
                        ts=bar.ts,
                        reason=ExitReason.SIGNAL_FLIP,
                        ref_price=price,
                    )
                ]

        # X5 时间止损（由风控策略配置的 max_bars_held 决定）
        if pos.bars_held >= p["max_hold_bars"] and r < 0.5:
            return [
                ExitIntent(
                    symbol=bar.symbol,
                    ts=bar.ts,
                    reason=ExitReason.TIME_STOP,
                    ref_price=price,
                )
            ]

        risk = pos.risk_per_unit
        if risk <= 0:
            return []

        # X3 保本（含成本缓冲）
        if not pos.breakeven_moved and r >= p["be_at_r"]:
            offset = p["be_offset_r"] * risk
            if p["be_cost_buffer"]:
                # 往返手续费 + 滑点折算成价格；否则"保本价"成交后仍是小额亏损
                cost_bps = float(ctx.extras.get("cost_bps", 0.0) or 0.0)
                offset = max(offset, pos.entry_price * cost_bps / 10_000.0)
            if pos.side is PositionSide.LONG:
                new_stop = pos.entry_price + offset
            else:
                new_stop = pos.entry_price - offset
            out.append(
                AdjustStopIntent(
                    symbol=bar.symbol, ts=bar.ts, new_stop=new_stop, reason=ExitReason.TRAILING_STOP
                )
            )

        # X7 订单流反转离场：微观结构先于价格结构变化，用来抢在 EMA 翻转之前走
        if p["flow_exit_delta"] > 0 and ctx.flow is not None and ctx.flow.has_data:
            delta = ctx.flow.delta_ratio
            adverse = -delta if pos.side is PositionSide.LONG else delta
            if adverse >= p["flow_exit_delta"] and r < p["trail_at_r"]:
                return [
                    ExitIntent(
                        symbol=bar.symbol,
                        ts=bar.ts,
                        reason=ExitReason.FLOW_REVERSAL,
                        ref_price=price,
                    )
                ]

        # X4 移动止损
        if r >= p["trail_at_r"] and atr > 0:
            if pos.side is PositionSide.LONG:
                candidate = price - p["trail_atr_mult"] * atr
                if candidate > pos.stop_price:
                    out.append(
                        AdjustStopIntent(
                            symbol=bar.symbol, ts=bar.ts, new_stop=candidate,
                            reason=ExitReason.TRAILING_STOP,
                        )
                    )
            else:
                candidate = price + p["trail_atr_mult"] * atr
                if candidate < pos.stop_price:
                    out.append(
                        AdjustStopIntent(
                            symbol=bar.symbol, ts=bar.ts, new_stop=candidate,
                            reason=ExitReason.TRAILING_STOP,
                        )
                    )

        return out
