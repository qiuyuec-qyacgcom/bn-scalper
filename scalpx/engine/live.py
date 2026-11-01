"""实盘 / 纸上交易引擎。

与回测引擎的差异只有三处（其余代码 100% 复用）：
1. 事件来源：`AsyncBarFeed` 而不是文件回放；
2. Broker：`BinanceBroker`（真实下单）或 `PaperBroker`（模拟撮合）；
3. 增加了实盘特有的三个守护：用户数据流守护、定时对账、kill switch 文件。

进程模型：单进程单事件循环。策略/风控都是同步的轻量计算，放在循环里执行，
不引入线程池，避免状态竞争（这是交易系统最常见的隐性 bug 来源）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any

from ..config import Config
from ..data.binance_rest import BinanceRest
from ..data.feed import AsyncBarFeed, build_async_feed
from ..data.store import Store
from ..execution.base import BrokerError, OrderRejected
from ..execution.binance_broker import BinanceBroker, PaperBroker
from ..execution.oms import OrderManager
from ..indicators import IndicatorSet
from ..metrics import compute_metrics
from ..microstructure import BookTracker, FlowAggregator, FlowFeatureBuilder
from ..models import (
    AdjustStopIntent,
    Bar,
    EnterIntent,
    ExitIntent,
    ExitReason,
    now_ms,
    ts_to_iso,
)
from ..portfolio import Portfolio
from ..risk.manager import RiskContext, RiskManager
from ..strategy.base import StrategyContext
from ..strategy.registry import create_strategy

log = logging.getLogger(__name__)


class LiveEngine:
    def __init__(self, cfg: Config, *, send_real_orders: bool = False) -> None:
        self.cfg = cfg
        self.send_real_orders = send_real_orders
        self.mode = "live" if send_real_orders else "paper"
        self.run_id = f"{self.mode}-{cfg.run.name}-{now_ms()}"
        self.bar_ms = cfg.market.bar_ms

        self.client = BinanceRest(
            base_url=cfg.exchange.base_url,
            api_key=cfg.exchange.api_key,
            api_secret=cfg.exchange.api_secret,
            recv_window_ms=cfg.exchange.recv_window_ms,
        )
        if send_real_orders:
            self.broker = BinanceBroker(self.client, cfg.market, cfg.execution)
            if not (cfg.exchange.api_key and cfg.exchange.api_secret):
                raise RuntimeError(
                    f"实盘需要 {cfg.exchange.api_key_env} / {cfg.exchange.api_secret_env} 环境变量"
                )
        else:
            self.broker = PaperBroker(cfg.market, cfg.execution)

        self.portfolio = Portfolio(initial_equity=cfg.backtest.initial_equity)
        self.risk = RiskManager(
            cfg.risk, cfg.market, bar_ms=self.bar_ms, exec_cfg=cfg.execution
        )
        self.store = Store(cfg.data.sqlite_path, run_id=self.run_id, persist=cfg.data.persist)
        self.oms = OrderManager(
            self.broker, self.portfolio, cfg.execution, cfg.market, self.risk,
            store=self.store if cfg.data.persist else None,
            risk_cfg=cfg.risk, bar_ms=self.bar_ms,
        )
        self.strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)

        self._indicators: dict[str, IndicatorSet] = {}
        self._history: dict[str, deque[Bar]] = {}
        self._equity_curve: list[tuple[int, float]] = []

        # --- 微观结构 ---
        ms = cfg.microstructure
        self._flow_aggs: dict[str, FlowAggregator] = {}
        self._flow_builders: dict[str, FlowFeatureBuilder] = {}
        self._book = BookTracker(
            depth_bps=ms.depth_bps,
            levels=ms.obi_levels,
            max_age_ms=ms.max_book_age_ms,
            tick=cfg.market.price_tick,
            stack_ratio=ms.stack_ratio,
            stack_range=ms.stack_range,
            min_level_qty=ms.min_level_qty,
            max_bad_ratio=ms.max_bad_ratio,
        )
        # 只有当策略**确实依赖盘口**时才用质量熔断去停它（否则会误伤纯 OHLCV 策略）
        self._book_gate = bool(
            ms.use_order_book and getattr(self.strategy, "uses_order_book", False)
            and ms.gate_on_book_quality
        )
        self._pending_flow: list = []
        self._last_book_persist_ms = 0
        self._micro_enabled = ms.enabled and (ms.use_agg_trades or ms.use_order_book)
        self._feed: AsyncBarFeed | None = None
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._kill_file = Path("data/KILL")

        # 纸上交易：成交同步回流 OMS
        if isinstance(self.broker, PaperBroker):
            self.broker.set_fill_sink(lambda f: self.oms.handle_fill(f, self.broker.current_ts()))

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def run_forever(self) -> int:
        try:
            asyncio.run(self.run())
        except KeyboardInterrupt:
            log.warning("收到中断信号，正在优雅退出")
        return 0

    async def run(self) -> None:
        self._running = True
        log.info(
            "启动 %s 模式 run_id=%s strategy=%s symbols=%s feed=%s",
            self.mode, self.run_id, self.strategy.name, self.cfg.market.symbols, self.cfg.exchange.feed,
        )
        self.store.register_run(
            mode=self.mode,
            strategy=self.strategy.name,
            symbols=self.cfg.market.symbols,
            interval=self.cfg.market.interval,
            started_ts=now_ms(),
            config={"strategy": self.strategy.describe(), "symbols": list(self.cfg.market.symbols)},
        )
        await self._preflight()

        self._feed = build_async_feed(self.cfg, self.client)
        if hasattr(self._feed, "set_micro_handlers"):
            self._feed.set_micro_handlers(on_trade=self._on_trade, on_depth=self._on_depth)
            log.info(
                "微观结构已接入：aggTrade=%s depth20=%s (depth_bps=%.1f, cvd_window=%d)",
                self.cfg.microstructure.use_agg_trades,
                self.cfg.microstructure.use_order_book,
                self.cfg.microstructure.depth_bps,
                self.cfg.microstructure.cvd_window,
            )
        if self.send_real_orders and self.cfg.exchange.user_stream:
            self._tasks.append(asyncio.create_task(self._user_stream_loop(), name="user-stream"))
        self._tasks.append(asyncio.create_task(self._reconcile_loop(), name="reconcile"))
        self._tasks.append(asyncio.create_task(self._housekeeping_loop(), name="housekeeping"))

        try:
            async for bar in self._feed.stream():
                if not self._running:
                    break
                self._on_bar(bar)
        finally:
            await self._shutdown()

    async def _preflight(self) -> None:
        """启动自检：只读接口，不做任何交易动作。"""
        try:
            await asyncio.to_thread(self.client.sync_time)
        except Exception as exc:  # noqa: BLE001
            log.warning("时间同步失败（可能无网络）: %s", exc)

        if self.send_real_orders:
            try:
                balance = await asyncio.to_thread(self.client.balance)
                total = sum(float(b.get("balance", 0)) for b in balance)
                log.info("账户可用余额（USDT 计价）: %.2f", total)
                if total <= 0:
                    raise RuntimeError("账户余额为 0，拒绝启动")
            except Exception as exc:  # noqa: BLE001
                log.error("实盘预检失败: %s", exc)
                raise
            for symbol in self.cfg.market.symbols:
                try:
                    await asyncio.to_thread(self.client.set_margin_type, symbol, "ISOLATED")
                    await asyncio.to_thread(self.client.set_leverage, symbol, int(self.cfg.risk.max_leverage))
                except Exception as exc:  # noqa: BLE001
                    log.warning("设置 %s 杠杆/保证金模式失败: %s", symbol, exc)

    async def _shutdown(self) -> None:
        log.warning("正在收尾：撤销挂单并平掉持仓")
        self._running = False
        try:
            for order in list(self.broker.open_orders()):
                self.broker.cancel(order.client_id)
            prices = {s: self.broker.reference_price(s) or p.entry_price for s, p in self.portfolio.positions.items()}
            if prices:
                self.oms.flatten_all(now_ms(), prices, ExitReason.KILL_SWITCH)
        except Exception as exc:  # noqa: BLE001
            log.error("收尾失败，请人工检查持仓: %s", exc)

        for task in self._tasks:
            task.cancel()
        if self._feed is not None:
            await self._feed.close()

        metrics = compute_metrics(
            trades=self.portfolio.trades,
            equity_curve=self._equity_curve or [(now_ms(), self.cfg.backtest.initial_equity)],
            initial_equity=self.cfg.backtest.initial_equity,
            total_fees=self.portfolio.total_fees,
        )
        self.store.save_state("metrics", metrics, now_ms())
        self.store.finish_run("stopped", metrics, now_ms())
        self.store.close()
        log.info("已退出。交易 %d 笔，权益 %.2f", len(self.portfolio.trades), metrics["final_equity"])

    # ------------------------------------------------------------------ #
    # 微观结构：逐笔成交流 / 盘口
    # ------------------------------------------------------------------ #
    def _on_trade(self, symbol: str, ts: int, price: float, qty: float, is_buyer_maker: bool) -> None:
        """aggTrade 回调（WS 线程内同步执行，只做累加，不做 IO）。"""
        agg = self._flow_aggs.get(symbol)
        if agg is None:
            agg = FlowAggregator(
                bar_ms=self.bar_ms,
                large_trade_usd=self.cfg.microstructure.large_trade_usd,
                symbol=symbol,
            )
            self._flow_aggs[symbol] = agg
        agg.on_trade(ts, price, qty, is_buyer_maker)

    def _on_depth(self, symbol: str, ts: int, bids, asks):
        """depth20@100ms 回调：直接由 BookTracker 计算 OBI/微价格。"""
        return self._book.apply(symbol, ts, bids, asks)

    def _micro(self, symbol: str, bar: Bar):
        """bar 收盘时取走订单流与盘口（与回测同构）。"""
        flow = None
        agg = self._flow_aggs.get(symbol)
        if agg is not None:
            raw = agg.finalize(bar.ts)
            if raw is not None:
                builder = self._flow_builders.get(symbol)
                if builder is None:
                    builder = FlowFeatureBuilder(window=self.cfg.microstructure.cvd_window)
                    self._flow_builders[symbol] = builder
                flow = builder.update(raw)
                if self.cfg.microstructure.persist_flow and self.cfg.data.persist:
                    self._pending_flow.append(raw)

        book = self._book.get(symbol, bar.ts_close)
        return flow, book

    # ------------------------------------------------------------------ #
    # 主事件处理（与回测同构）
    # ------------------------------------------------------------------ #
    def _on_bar(self, bar: Bar) -> None:
        symbol = bar.symbol
        if isinstance(self.broker, PaperBroker):
            self.broker.on_bar(bar)
        else:
            self.broker.set_price(symbol, bar.close)

        indicators, history = self._ensure_symbol(symbol)
        snapshot = indicators.update(bar)
        history.append(bar)
        flow, book = self._micro(symbol, bar)

        # 行情落库（批量提交由 housekeeping 负责）
        if self.cfg.data.persist:
            self.store.save_bars(self.cfg.market.interval, [bar])

        self.portfolio.mark(symbol, bar.close)
        pos = self.portfolio.position(symbol)
        if pos is not None:
            pos.bars_held += 1
            pos.update_extremes(bar.high)
            pos.update_extremes(bar.low)

        # 盘口质量 -> 风控熔断（只在策略确实依赖盘口时生效）
        if self._book_gate:
            q = self._book.quality(symbol, bar.ts_close)
            self.risk.notify_book_quality(bar.ts_close, symbol, q.ok, q.reason)

        equity = self.portfolio.equity()
        self.risk.on_bar(bar.ts_close, equity)
        self._drain_risk_events(bar.ts_close)

        if snapshot is not None and len(history) >= self.strategy.warmup_bars:
            ctx = StrategyContext(
                bar=bar, snapshot=snapshot, history=history, indicators=indicators,
                position=pos, equity=equity, ts=bar.ts_close, book=book, flow=flow,
            )
            for intent in self.strategy.on_bar(ctx):
                self._dispatch(intent, bar, equity, snapshot, book)

        self.oms.advance(bar)
        self._drain_risk_events(bar.ts_close)

        self._equity_curve.append((bar.ts_close, self.portfolio.equity()))
        if self.cfg.data.persist:
            self.store.save_equity(
                ts=bar.ts_close,
                equity=self.portfolio.equity(),
                cash=self.portfolio.cash,
                unrealized=self.portfolio.unrealized(),
                exposure=self.portfolio.exposure(),
                drawdown=self.portfolio.drawdown(),
            )

    def _dispatch(self, intent, bar: Bar, equity: float, snapshot, book=None) -> None:
        if isinstance(intent, EnterIntent):
            prices = self.portfolio.prices
            prices[bar.symbol] = bar.close
            spread = float(book.spread_bps) if book is not None else self.cfg.backtest.assumed_spread_bps
            untrusted = False
            reason = ""
            if self._book_gate:
                q = self._book.quality(bar.symbol, bar.ts_close)
                untrusted = not q.ok
                reason = q.reason
            ctx = RiskContext(
                ts=bar.ts_close, equity=equity, positions=dict(self.portfolio.positions),
                prices=prices, spread_bps=spread, bar=bar, atr=snapshot.atr,
                book_untrusted=untrusted, book_reason=reason,
            )
            decision = self.risk.pre_trade(intent, ctx)
            if not decision.approved:
                if self.cfg.data.persist:
                    self.store.save_signal(
                        ts=bar.ts_close, symbol=intent.symbol, action="rejected",
                        side=intent.side.value, price=intent.ref_price, reason=intent.reason,
                        meta={"code": decision.code},
                    )
                return
            self.oms.submit_entry(
                symbol=intent.symbol, side=intent.side, qty=decision.qty, ts=bar.ts_close,
                stop_price=decision.stop_price, take_profit_price=decision.take_profit_price,
                ref_price=intent.ref_price, tag=intent.reason,
            )
            if self.cfg.data.persist:
                self.store.save_signal(
                    ts=bar.ts_close, symbol=intent.symbol, action="entry", side=intent.side.value,
                    price=intent.ref_price, stop_price=decision.stop_price,
                    tp_price=decision.take_profit_price, reason=intent.reason,
                    meta={"qty": decision.qty},
                )
        elif isinstance(intent, ExitIntent):
            self.oms.submit_exit(symbol=intent.symbol, ts=bar.ts_close, reason=intent.reason)
        elif isinstance(intent, AdjustStopIntent):
            self.oms.adjust_stop(symbol=intent.symbol, ts=bar.ts_close, new_stop=intent.new_stop)

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

    def _drain_risk_events(self, ts: int) -> None:
        for ev in self.risk.drain_events():
            if self.cfg.data.persist:
                self.store.save_risk_event(
                    ts=int(ev.get("ts", ts)), level=str(ev.get("level", "INFO")),
                    code=str(ev.get("code", "")), message=str(ev.get("message", "")),
                    meta=ev.get("meta", {}),  # type: ignore[arg-type]
                )

    # ------------------------------------------------------------------ #
    # 守护任务
    # ------------------------------------------------------------------ #
    async def _user_stream_loop(self) -> None:
        """用户数据流：订单/账户更新。断线后重建 listenKey 并触发对账。"""
        try:
            import websockets
        except ImportError:
            log.error("实盘需要 `pip install websockets` 才能接收订单回报，已停止")
            self._running = False
            return

        backoff = 1.0
        while self._running:
            listen_key = ""
            try:
                listen_key = await asyncio.to_thread(self.client.create_listen_key)
                url = f"{self.cfg.exchange.ws_url}/ws/{listen_key}"
                async with websockets.connect(url, ping_interval=15, ping_timeout=20) as ws:
                    log.info("用户数据流已连接")
                    backoff = 1.0
                    async for raw in ws:
                        try:
                            self.broker.on_user_event(json.loads(raw))
                        except (json.JSONDecodeError, KeyError, TypeError) as exc:
                            log.warning("解析用户事件失败: %s", exc)
                        # 成交立即交给 OMS，缩短「成交→挂保护单」窗口
                        for fill in self.broker.poll_fills():
                            self.oms.handle_fill(fill, now_ms())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("用户数据流断开: %s，%.1fs 后重连", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                if listen_key:
                    try:
                        await asyncio.to_thread(self.client.close_listen_key, listen_key)
                    except Exception:  # noqa: BLE001
                        pass

    async def _reconcile_loop(self) -> None:
        interval = max(5, self.cfg.execution.reconcile_interval_s)
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("对账异常: %s", exc)

    async def _reconcile_once(self) -> None:
        if not self.send_real_orders:
            return
        exch = await asyncio.to_thread(self.broker.fetch_positions)
        local = {s: p for s, p in self.portfolio.positions.items()}

        # 1) 交易所有仓、本地无仓 → 未知敞口，立即市价平掉（fail-closed）
        for symbol, qty in exch.items():
            if symbol not in local:
                log.critical("检测到本地未记录的持仓 %s=%.6f，立即平仓", symbol, qty)
                side = "SELL" if qty > 0 else "BUY"
                try:
                    await asyncio.to_thread(
                        self.client.new_order,
                        symbol=symbol, side=side, type="MARKET", quantity=abs(qty),
                        reduceOnly="true", newClientOrderId=f"rec-{now_ms()}",
                    )
                except Exception as exc:  # noqa: BLE001
                    log.critical("平掉未知持仓失败 %s: %s", symbol, exc)
                self.risk.halt(now_ms(), "RECONCILE_UNKNOWN_POSITION", f"{symbol} 存在未知持仓")
                self.oms.stats.reconcile_fixes += 1

        # 2) 本地有仓、交易所无仓 → 本地账本陈旧
        for symbol in local:
            if symbol not in exch:
                log.error("本地持仓 %s 在交易所已不存在，强制平账", symbol)
                pos = local[symbol]
                self.oms._force_flatten(pos, ts=now_ms(), reason=ExitReason.RISK_LIQUIDATION)
                self.oms.stats.reconcile_fixes += 1

        # 3) 有仓但缺保护单 → 补挂或强平
        for symbol, pos in self.portfolio.positions.items():
            open_orders = await asyncio.to_thread(self.broker.fetch_open_orders, symbol)
            has_stop = any(o.get("type") in ("STOP_MARKET", "STOP") for o in open_orders)
            if not has_stop:
                log.error("%s 在交易所无止损单，尝试补挂", symbol)
                if not self.oms._place_protection(pos):
                    self.oms._force_flatten(pos, ts=now_ms(), reason=ExitReason.RISK_LIQUIDATION)
                self.oms.stats.reconcile_fixes += 1

    async def _housekeeping_loop(self) -> None:
        """批量提交、kill switch、心跳日志。"""
        while self._running:
            await asyncio.sleep(5)
            try:
                if self._kill_file.exists():
                    log.critical("检测到 kill switch 文件 %s，停止交易并平仓", self._kill_file)
                    self.risk.halt(now_ms(), "KILL_SWITCH", "kill switch 文件存在")
                    prices = {
                        s: self.broker.reference_price(s) or p.entry_price
                        for s, p in self.portfolio.positions.items()
                    }
                    if prices:
                        self.oms.flatten_all(now_ms(), prices, ExitReason.KILL_SWITCH)
                    self._running = False
                    break

                # 成交流 / 盘口批量落库（避免高频单条写入）
                if self._pending_flow:
                    self.store.save_bar_flow(self._pending_flow)
                    self._pending_flow.clear()

                now = now_ms()
                ms = self.cfg.microstructure
                if ms.persist_book and now - self._last_book_persist_ms >= ms.persist_book_interval_ms:
                    snaps = [
                        s for sym in self.cfg.market.symbols
                        if (s := self._book.raw(sym)) is not None
                    ]
                    if snaps:
                        self.store.save_book_snapshots(snaps)
                    self._last_book_persist_ms = now

                self.store.commit()
                snap = self.portfolio.snapshot(self.portfolio.prices)
                self.store.save_state(
                    "heartbeat",
                    {
                        "equity": round(snap["equity"], 2),
                        "positions": snap["open_positions"],
                        "drawdown": round(snap["drawdown"], 4),
                        "risk": self.risk.describe(),
                        "oms": self.oms.describe(),
                    },
                    now_ms(),
                )
                log.info(
                    "心跳 equity=%.2f pos=%d dd=%.2f%% trades=%d halted=%s",
                    snap["equity"], int(snap["open_positions"]), snap["drawdown"] * 100,
                    len(self.portfolio.trades), self.risk.state.halted,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("housekeeping 异常: %s", exc)
