"""回测引擎（事件驱动，逐 bar 推进）。

**与实盘共用同一套** Strategy / RiskManager / OrderManager / Portfolio，
唯一替换的是 Clock 与 Broker（SimulatedBroker）。这是回测可信度的前提。

每根已收盘 bar 的处理顺序（顺序本身决定是否引入未来函数）：

    1. broker.on_bar(bar)  —— 用本 bar 的 intrabar 路径撮合上一根挂出的订单；
       成交通过同步回调进入 OMS（入场即可在 bar 内挂上保护单）
    2. oms.advance(bar)    —— 入场超时撤单/降级、保护单兜底、持仓时长硬上限
    3. 指标更新（只吃已收盘 bar）+ 持仓状态推进（bars_held / MFE / MAE）
    4. portfolio.mark(close)
    5. risk.on_bar(ts, equity) —— 日切、日亏熔断
    6. strategy.on_bar(ctx)    —— 产出意图
    7. 意图 → 风控闸门 → OMS 下单（下一根 bar 才会成交）
    8. 记录权益点

第 7 步刻意不在本 bar 内成交：信号基于 close，最快也只能在下一根开盘成交，
这是最保守也最接近真实的假设。
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..config import Config
from ..data.feed import BarFeed
from ..data.flow_source import (
    BookSource,
    CsvFlowSource,
    EmptyFlowSource,
    FlowSource,
    StoreBookSource,
)
from ..data.store import Store
from ..execution.oms import OrderManager
from ..execution.simulator import SimulatedBroker
from ..indicators import IndicatorSet, Snapshot
from ..metrics import compute_metrics
from ..microstructure import FlowFeatureBuilder
from ..models import (
    AdjustStopIntent,
    Bar,
    EnterIntent,
    ExitIntent,
    ExitReason,
    Fill,
    Order,
    Side,
    TradeRecord,
    now_ms,
    ts_to_iso,
)
from ..portfolio import Portfolio
from ..risk.manager import RiskContext, RiskManager
from ..strategy.base import Strategy, StrategyContext

log = logging.getLogger(__name__)


@dataclass(slots=True)
class BacktestResult:
    run_id: str
    metrics: dict[str, float]
    trades: list[TradeRecord]
    equity_curve: list[tuple[int, float]]
    risk_events: list[dict] = field(default_factory=list)
    oms_stats: dict[str, int] = field(default_factory=dict)
    broker_stats: dict[str, int] = field(default_factory=dict)
    bars_processed: int = 0
    signals_generated: int = 0
    rejected_signals: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)
    micro_stats: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        from ..metrics import format_metrics

        return format_metrics(self.metrics, title=f"回测结果 {self.run_id}")


class BacktestEngine:
    def __init__(
        self,
        cfg: Config,
        strategy: Strategy,
        feed: BarFeed,
        *,
        store: Store | None = None,
        run_id: str | None = None,
        flow_sources: dict[str, FlowSource] | None = None,
        book_sources: dict[str, BookSource] | None = None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.feed = feed
        self.run_id = run_id or f"bt-{now_ms()}"
        self.store = store
        self.bar_ms = cfg.market.bar_ms

        # 微观结构数据（可选）：缺失时策略自动降级到 taker 占比代理
        self.flow_sources = flow_sources if flow_sources is not None else self._default_flow_sources()
        self.book_sources = book_sources if book_sources is not None else self._default_book_sources()
        self._flow_builders: dict[str, FlowFeatureBuilder] = {}
        self._micro_bars_with_flow = 0
        self._micro_bars_with_book = 0

        self.portfolio = Portfolio(initial_equity=cfg.backtest.initial_equity)
        self.broker = SimulatedBroker(
            cfg.market, cfg.execution, pessimistic_intrabar=cfg.backtest.pessimistic_intrabar
        )
        self.risk = RiskManager(
            cfg.risk, cfg.market, bar_ms=self.bar_ms, exec_cfg=cfg.execution
        )
        self.oms = OrderManager(
            self.broker,
            self.portfolio,
            cfg.execution,
            cfg.market,
            self.risk,
            store=store,
            risk_cfg=cfg.risk,
            bar_ms=self.bar_ms,
        )

        # 成交即回调：任何时点成交都立即交给 OMS，杜绝"有仓无保护单"的窗口
        self.broker.set_fill_sink(lambda fill: self.oms.handle_fill(fill, self.broker.current_ts()))

        self._indicators: dict[str, IndicatorSet] = {}
        self._history: dict[str, deque[Bar]] = {}
        self._equity_curve: list[tuple[int, float]] = []
        self._signals = 0
        self._rejected = 0
        self._reject_reasons: dict[str, int] = {}
        self._bars = 0

    # ------------------------------------------------------------------ #
    # 微观结构数据源
    # ------------------------------------------------------------------ #
    def _default_flow_sources(self) -> dict[str, FlowSource]:
        ms = self.cfg.microstructure
        if not (ms.enabled and ms.use_agg_trades):
            return {}
        return {
            sym: CsvFlowSource(self.cfg.data.csv_dir, sym, self.cfg.market.interval)
            for sym in self.cfg.market.symbols
        }

    def _default_book_sources(self) -> dict[str, BookSource]:
        ms = self.cfg.microstructure
        if not (ms.enabled and ms.use_order_book):
            return {}
        # 公开历史数据没有 L2，只有"实盘录制后回放"这一条路径
        path = Path(self.cfg.data.sqlite_path)
        if not path.exists():
            return {}
        book_store = Store(path, run_id="book-feed", persist=False)
        return {sym: StoreBookSource(book_store, sym) for sym in self.cfg.market.symbols}

    def _micro(self, symbol: str, bar: Bar):
        """取本 bar 的订单流特征与盘口快照，取不到就返回 None（不伪造）。"""
        flow = None
        src = self.flow_sources.get(symbol)
        if src is not None:
            raw = src.get(symbol, bar.ts)
            if raw is not None:
                builder = self._flow_builders.get(symbol)
                if builder is None:
                    builder = FlowFeatureBuilder(window=self.cfg.microstructure.cvd_window)
                    self._flow_builders[symbol] = builder
                flow = builder.update(raw)
                self._micro_bars_with_flow += 1

        book = None
        bsrc = self.book_sources.get(symbol)
        if bsrc is not None:
            snap = bsrc.get(symbol, bar.ts_close)
            if snap is not None:
                book = snap
                self._micro_bars_with_book += 1
        return flow, book

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _ensure_symbol(self, symbol: str) -> tuple[IndicatorSet, deque[Bar]]:
        if symbol not in self._indicators:
            p = self.strategy.params
            self._indicators[symbol] = IndicatorSet(
                ema_fast=int(p.get("ema_fast", 21)),
                ema_slow=int(p.get("ema_slow", 55)),
                ema_trend=int(p.get("ema_trend", 144)),
                trend_slope_lookback=int(p.get("trend_slope_lookback", 20)),
                rsi_period=int(p.get("rsi_period", 14)),
                atr_period=int(p.get("atr_period", 14)),
                vol_period=int(p.get("vol_period", 20)),
            )
            self._history[symbol] = deque(maxlen=max(400, self.strategy.warmup_bars + 50))
        return self._indicators[symbol], self._history[symbol]

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #
    def run(self) -> BacktestResult:
        log.info("回测开始 run_id=%s strategy=%s symbols=%s", self.run_id, self.strategy.name, self.cfg.market.symbols)
        if self.store is not None:
            self.store.register_run(
                mode="backtest",
                strategy=self.strategy.name,
                symbols=self.cfg.market.symbols,
                interval=self.cfg.market.interval,
                started_ts=now_ms(),
                config=self._config_dict(),
            )

        warmup_required = max(self.strategy.warmup_bars, self.cfg.data.warmup_bars)

        for bar in self.feed:
            self._bars += 1
            self._on_bar(bar, warmup_required)

        # 收尾：以最后价格强平，避免"未平仓浮盈"美化结果
        if self.portfolio.positions:
            prices = {s: self.broker.reference_price(s) or p.entry_price for s, p in self.portfolio.positions.items()}
            self.oms.flatten_all(self._last_ts(), prices, ExitReason.END_OF_DATA)

        metrics = compute_metrics(
            trades=self.portfolio.trades,
            equity_curve=self._equity_curve or [(now_ms(), self.cfg.backtest.initial_equity)],
            initial_equity=self.cfg.backtest.initial_equity,
            total_fees=self.portfolio.total_fees,
        )
        result = BacktestResult(
            run_id=self.run_id,
            metrics=metrics,
            trades=list(self.portfolio.trades),
            equity_curve=list(self._equity_curve),
            risk_events=self.risk.drain_events(),
            oms_stats=self.oms.describe(),
            broker_stats=dict(self.broker.stats),
            bars_processed=self._bars,
            signals_generated=self._signals,
            rejected_signals=self._rejected,
            reject_reasons=dict(self._reject_reasons),
            micro_stats={
                "bars_with_flow": self._micro_bars_with_flow,
                "bars_with_book": self._micro_bars_with_book,
                "flow_sources": len(self.flow_sources),
                "book_sources": len(self.book_sources),
            },
        )

        if self.store is not None:
            for ev in result.risk_events:
                self.store.save_risk_event(
                    ts=int(ev.get("ts", 0)),
                    level=str(ev.get("level", "INFO")),
                    code=str(ev.get("code", "")),
                    message=str(ev.get("message", "")),
                    meta=ev.get("meta", {}),  # type: ignore[arg-type]
                )
            self.store.save_state("metrics", metrics, now_ms())
            self.store.finish_run("finished", metrics, now_ms())
            self.store.commit()

        log.info("回测结束：%d 笔交易，收益 %.2f%%，最大回撤 %.2f%%",
                 len(result.trades), metrics["total_return"] * 100, metrics["max_drawdown"] * 100)
        return result

    def _on_bar(self, bar: Bar, warmup_required: int) -> None:
        symbol = bar.symbol
        indicators, history = self._ensure_symbol(symbol)

        # 1) 撮合（成交同步回调 OMS，见 __init__ 中注册的 sink）
        self.broker.on_bar(bar)

        # 2) 超时撤单 / 保护单兜底 / 成交归属（交易由 OMS 负责落库，此处不重复写）
        self.oms.advance(bar)

        # 3) 指标 + 持仓状态
        snapshot = indicators.update(bar)
        history.append(bar)
        pos = self.portfolio.position(symbol)
        if pos is not None:
            pos.bars_held += 1
            pos.update_extremes(bar.high)
            pos.update_extremes(bar.low)

        # 4) 估值
        self.portfolio.mark(symbol, bar.close)
        equity = self.portfolio.equity()

        # 5) 风控日切
        self.risk.on_bar(bar.ts_close, equity)

        # 6) 策略
        if snapshot is not None and len(history) >= warmup_required:
            flow, book = self._micro(symbol, bar)
            ctx = StrategyContext(
                bar=bar,
                snapshot=snapshot,
                history=history,
                indicators=indicators,
                position=pos,
                equity=equity,
                ts=bar.ts_close,
                flow=flow,
                book=book,
            )
            for intent in self.strategy.on_bar(ctx):
                self._dispatch(intent, snapshot, bar, equity)

        # 7) 权益点
        self._equity_curve.append((bar.ts_close, self.portfolio.equity()))
        if self.store is not None:
            self.store.save_equity(
                ts=bar.ts_close,
                equity=self.portfolio.equity(),
                cash=self.portfolio.cash,
                unrealized=self.portfolio.unrealized(),
                exposure=self.portfolio.exposure(),
                drawdown=self.portfolio.drawdown(),
            )
            self.store.save_position_snapshot(self.portfolio.position(symbol), bar.ts_close)

    # ------------------------------------------------------------------ #
    # 意图分发
    # ------------------------------------------------------------------ #
    def _dispatch(self, intent, snapshot: Snapshot, bar: Bar, equity: float) -> None:
        if isinstance(intent, EnterIntent):
            self._handle_entry(intent, snapshot, bar, equity)
        elif isinstance(intent, ExitIntent):
            self._handle_exit(intent, bar)
        elif isinstance(intent, AdjustStopIntent):
            self.oms.adjust_stop(symbol=intent.symbol, ts=bar.ts_close, new_stop=intent.new_stop)
        else:  # pragma: no cover
            log.warning("未知意图类型: %r", intent)

    def _handle_entry(self, intent: EnterIntent, snapshot: Snapshot, bar: Bar, equity: float) -> None:
        self._signals += 1
        prices = self.portfolio.prices
        prices[bar.symbol] = bar.close
        ctx = RiskContext(
            ts=bar.ts_close,
            equity=equity,
            positions=dict(self.portfolio.positions),
            prices=prices,
            spread_bps=self.cfg.backtest.assumed_spread_bps,
            bar=bar,
            atr=snapshot.atr,
        )
        decision = self.risk.pre_trade(intent, ctx)
        if not decision.approved:
            self._rejected += 1
            self._reject_reasons[decision.code] = self._reject_reasons.get(decision.code, 0) + 1
            if self.store is not None:
                self.store.save_signal(
                    ts=bar.ts_close, symbol=intent.symbol, action="rejected",
                    side=intent.side.value, price=intent.ref_price,
                    stop_price=intent.stop_price, tp_price=intent.take_profit_price,
                    reason=intent.reason, meta={"code": decision.code, "msg": decision.message},
                )
            return

        ok = self.oms.submit_entry(
            symbol=intent.symbol,
            side=intent.side,
            qty=decision.qty,
            ts=bar.ts_close,
            stop_price=decision.stop_price,
            take_profit_price=decision.take_profit_price,
            ref_price=intent.ref_price,
            tag=intent.reason,
        )
        if self.store is not None:
            self.store.save_signal(
                ts=bar.ts_close, symbol=intent.symbol,
                action="entry" if ok else "entry_failed",
                side=intent.side.value, price=intent.ref_price,
                stop_price=decision.stop_price, tp_price=decision.take_profit_price,
                reason=intent.reason,
                meta={"qty": decision.qty, "risk": decision.sizing.risk_amount if decision.sizing else 0},
            )

    def _handle_exit(self, intent: ExitIntent, bar: Bar) -> None:
        self.oms.submit_exit(
            symbol=intent.symbol, ts=bar.ts_close, reason=intent.reason, price=intent.ref_price
        )
        if self.store is not None:
            self.store.save_signal(
                ts=bar.ts_close, symbol=intent.symbol, action="exit",
                price=intent.ref_price, reason=intent.reason.value,
            )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _last_ts(self) -> int:
        return self._equity_curve[-1][0] if self._equity_curve else now_ms()

    def _config_dict(self) -> dict:
        return {
            "run": self.cfg.run.name,
            "mode": "backtest",
            "symbols": list(self.cfg.market.symbols),
            "interval": self.cfg.market.interval,
            "strategy": self.strategy.describe(),
            "risk": {
                "risk_per_trade": self.cfg.risk.risk_per_trade,
                "max_leverage": self.cfg.risk.max_leverage,
                "max_daily_loss_pct": self.cfg.risk.max_daily_loss_pct,
            },
            "execution": {
                "entry_order_type": self.cfg.execution.entry_order_type,
                "maker_fee": self.cfg.execution.maker_fee,
                "taker_fee": self.cfg.execution.taker_fee,
                "slippage_bps": self.cfg.execution.slippage_bps,
            },
            "data": {
                "source": self.cfg.data.source,
                "start": ts_to_iso(self.cfg.data.start_ms),
                "end": ts_to_iso(self.cfg.data.end_ms),
            },
        }
