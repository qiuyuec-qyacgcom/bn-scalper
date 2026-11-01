"""行情接入层：回测用文件/DB 回放，实盘用 WS 或 REST 轮询。

统一抽象：
- 回测（同步）：`BarFeed.__iter__` 逐根产出已收盘 bar；
- 实盘（异步）：`AsyncBarFeed.stream()` 产出 `Bar`。

两种形态刻意分开，避免为了"统一"而在回测里引入事件循环开销。
差异只存在于**接线**层（engine/backtest.py vs engine/live.py），
策略 / 风控 / 撮合代码完全共用。
"""

from __future__ import annotations

import asyncio
import csv
import logging
from pathlib import Path
from typing import AsyncIterator, Iterator, Protocol, Sequence, runtime_checkable

from ..models import Bar
from .store import Store

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 同步回测源
# --------------------------------------------------------------------------- #
@runtime_checkable
class BarFeed(Protocol):
    def __iter__(self) -> Iterator[Bar]: ...


class InMemoryFeed:
    def __init__(self, bars: Sequence[Bar]) -> None:
        self._bars = list(bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self._bars)


class CsvBarFeed:
    """从 `{csv_dir}/{symbol}_{interval}.csv` 读取 K 线。

    兼容两种表头：
    - 简化表头：ts,open,high,low,close,volume[,quote_volume,trades,taker_buy_base]
    - 币安原始 kline CSV：open_time,open,high,low,close,volume,close_time,quote_volume,trades,taker_buy_base,...
    """

    def __init__(
        self,
        csv_dir: str | Path,
        symbol: str,
        interval: str = "1m",
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> None:
        self.path = Path(csv_dir) / f"{symbol}_{interval}.csv"
        self.symbol = symbol
        self.interval = interval
        self.start_ms = start_ms
        self.end_ms = end_ms

    def __iter__(self) -> Iterator[Bar]:
        if not self.path.exists():
            raise FileNotFoundError(
                f"行情文件不存在: {self.path}；可先执行 `python -m scalpx download` 或 `python scripts/make_sample_data.py`"
            )
        with self.path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                return
            idx = _resolve_columns(header)
            for row in reader:
                if not row:
                    continue
                try:
                    ts = _parse_ts(row[idx["ts"]])
                    bar = Bar(
                        symbol=self.symbol,
                        ts=ts,
                        open=float(row[idx["open"]]),
                        high=float(row[idx["high"]]),
                        low=float(row[idx["low"]]),
                        close=float(row[idx["close"]]),
                        volume=float(row[idx["volume"]]),
                        quote_volume=_opt_float(row, idx.get("quote_volume")),
                        trades=int(float(row[idx["trades"]])) if "trades" in idx else 0,
                        taker_buy_base=_opt_float(row, idx.get("taker_buy_base")),
                    )
                except (ValueError, IndexError) as exc:
                    log.debug("跳过异常行 %s: %s", row[:3], exc)
                    continue
                if self.start_ms is not None and bar.ts < self.start_ms:
                    continue
                if self.end_ms is not None and bar.ts >= self.end_ms:
                    break
                yield bar


class StoreBarFeed:
    """从 SQLite 回放，适合"先下载一次，反复回测"。"""

    def __init__(
        self,
        store: Store,
        symbol: str,
        interval: str = "1m",
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> None:
        self.store = store
        self.symbol = symbol
        self.interval = interval
        self.start_ms = start_ms or 0
        self.end_ms = end_ms or 2**62

    def __iter__(self) -> Iterator[Bar]:
        yield from self.store.load_bars(self.symbol, self.interval, self.start_ms, self.end_ms)


class MultiSymbolFeed:
    """多标的时间序合并（按 bar.ts 升序），保证跨品种决策顺序一致。"""

    def __init__(self, feeds: dict[str, BarFeed]) -> None:
        self.feeds = feeds

    def __iter__(self) -> Iterator[Bar]:
        iters = {sym: iter(feed) for sym, feed in self.feeds.items()}
        heads: dict[str, Bar | None] = {sym: next(it, None) for sym, it in iters.items()}
        while True:
            available = {sym: b for sym, b in heads.items() if b is not None}
            if not available:
                return
            sym = min(available, key=lambda s: (available[s].ts, s))
            bar = available[sym]
            assert bar is not None
            yield bar
            heads[sym] = next(iters[sym], None)


def build_feed(cfg, symbols: Sequence[str] | None = None) -> BarFeed:
    """根据配置装配回测数据源。"""
    symbols = tuple(symbols or cfg.market.symbols)
    feeds: dict[str, BarFeed] = {}
    for sym in symbols:
        if cfg.data.source == "csv":
            feeds[sym] = CsvBarFeed(
                cfg.data.csv_dir, sym, cfg.market.interval, start_ms=cfg.data.start_ms, end_ms=cfg.data.end_ms
            )
        elif cfg.data.source == "sqlite":
            store = Store(cfg.data.sqlite_path, run_id="feed", persist=False)
            feeds[sym] = StoreBarFeed(
                store, sym, cfg.market.interval, start_ms=cfg.data.start_ms, end_ms=cfg.data.end_ms
            )
        elif cfg.data.source == "binance_rest":
            from .binance_rest import BinanceRest

            client = BinanceRest(base_url=cfg.exchange.base_url)
            bars = client.download_klines(
                sym, cfg.market.interval, cfg.data.start_ms, cfg.data.end_ms, bar_ms=cfg.market.bar_ms
            )
            feeds[sym] = InMemoryFeed(bars)
        else:  # pragma: no cover - 已在 config 校验
            raise ValueError(cfg.data.source)
    return feeds[symbols[0]] if len(symbols) == 1 else MultiSymbolFeed(feeds)


# --------------------------------------------------------------------------- #
# 异步实盘源
# --------------------------------------------------------------------------- #
class AsyncBarFeed:
    """实盘行情协议基类。"""

    def __init__(self, symbols: Sequence[str], interval: str) -> None:
        self.symbols = tuple(symbols)
        self.interval = interval
        self._running = False

    async def stream(self) -> AsyncIterator[Bar]:  # pragma: no cover - 抽象
        raise NotImplementedError
        yield  # type: ignore[unreachable]

    async def close(self) -> None:
        self._running = False

    async def book(self, symbol: str):  # pragma: no cover - 可选能力
        """盘口快照（可选）。不支持时返回 None。"""
        return None


class RestPollBarFeed(AsyncBarFeed):
    """无第三方依赖的降级方案：定时轮询 REST K 线，只在 bar 收盘后推送。

    缺点：延迟 1~3s，无盘口。用途是让 **paper 模式零依赖可跑通**，
    生产环境应使用 `WsBarFeed`。
    """

    def __init__(
        self,
        client,
        symbols: Sequence[str],
        interval: str,
        bar_ms: int,
        *,
        poll_interval_ms: int = 2000,
    ) -> None:
        super().__init__(symbols, interval)
        self.client = client
        self.bar_ms = bar_ms
        self.poll_interval_ms = poll_interval_ms
        self._last_closed: dict[str, int] = {}

    async def stream(self) -> AsyncIterator[Bar]:
        self._running = True
        # 预热：先取最近 limit 根，最后一种丢弃（可能未收盘）
        for sym in self.symbols:
            try:
                bars = await asyncio.to_thread(self.client.klines, sym, self.interval, limit=300)
            except Exception as exc:  # noqa: BLE001
                log.error("预热失败 %s: %s", sym, exc)
                continue
            if bars:
                self._last_closed[sym] = bars[-2].ts if len(bars) >= 2 else bars[-1].ts
        while self._running:
            for sym in self.symbols:
                try:
                    bars = await asyncio.to_thread(self.client.klines, sym, self.interval, limit=3)
                except Exception as exc:  # noqa: BLE001
                    log.warning("轮询失败 %s: %s", sym, exc)
                    continue
                last = self._last_closed.get(sym, 0)
                for bar in bars[:-1]:  # 丢弃最后一根未收盘
                    if bar.ts > last:
                        self._last_closed[sym] = bar.ts
                        yield bar
            await asyncio.sleep(self.poll_interval_ms / 1000.0)


class WsBarFeed(AsyncBarFeed):
    """WebSocket 实时行情（需要 `pip install websockets`）。

    订阅三路：
    - `kline_<interval>`：已收盘 bar（策略主时钟）
    - `depth20@100ms`：前 20 档全量快照（OBI / 微价格）
    - `aggTrade`：逐笔主动成交（真实 CVD）

    断线重连使用指数退避，重连后由引擎触发对账与盘口重建。
    行情解析与业务加工严格分离：feed 只解析，OBI/CVD 由 `microstructure/` 计算。
    """

    def __init__(
        self,
        ws_base: str,
        symbols: Sequence[str],
        interval: str,
        *,
        depth: bool = True,
        agg_trades: bool = True,
        max_backoff_s: float = 30.0,
    ) -> None:
        super().__init__(symbols, interval)
        self.ws_base = ws_base.rstrip("/")
        self.depth = depth
        self.agg_trades = agg_trades
        self.max_backoff_s = max_backoff_s
        self._books: dict[str, object] = {}
        self._queue: asyncio.Queue[Bar] = asyncio.Queue(maxsize=2000)
        self._task: asyncio.Task[None] | None = None
        self._ws = None
        self.reconnects = 0
        # 微观结构回调：由 LiveEngine 注入，feed 只负责解析与分发
        self.on_trade = None
        self.on_depth = None
        self.micro_events = {"trade": 0, "depth": 0, "malformed": 0}

    def set_micro_handlers(self, *, on_trade=None, on_depth=None) -> None:
        self.on_trade = on_trade
        self.on_depth = on_depth

    def _url(self) -> str:
        streams = [f"{s.lower()}@kline_{self.interval}" for s in self.symbols]
        if self.depth:
            streams += [f"{s.lower()}@depth20@100ms" for s in self.symbols]
        if self.agg_trades:
            streams += [f"{s.lower()}@aggTrade" for s in self.symbols]
        return f"{self.ws_base}/stream?streams={'/'.join(streams)}"

    async def book(self, symbol: str):
        return self._books.get(symbol)

    async def _run(self) -> None:
        try:
            import websockets  # 延迟导入：纯回测环境无需安装
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("WS 模式需要 `pip install websockets`，或把 exchange.feed 设为 rest_poll") from exc

        from ..models import OrderBookSnapshot

        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(self._url(), ping_interval=20, ping_timeout=20, max_queue=1000) as ws:
                    self._ws = ws
                    backoff = 1.0
                    log.info("行情 WS 已连接: %s", self._url())
                    async for raw in ws:
                        import json

                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        event = data.get("e")
                        if event == "kline":
                            k = data["k"]
                            if not k.get("x"):
                                continue
                            bar = Bar(
                                symbol=str(k["s"]),
                                ts=int(k["t"]),
                                open=float(k["o"]),
                                high=float(k["h"]),
                                low=float(k["l"]),
                                close=float(k["c"]),
                                volume=float(k["v"]),
                                quote_volume=float(k["q"]),
                                trades=int(k["n"]),
                                taker_buy_base=float(k["V"]),
                            )
                            await self._queue.put(bar)
                        elif event == "depthUpdate" and "b" in data:
                            bids = [(float(p), float(q)) for p, q in data["b"][:20]]
                            asks = [(float(p), float(q)) for p, q in data["a"][:20]]
                            symbol = str(data.get("s", ""))
                            ts = int(data.get("E", 0))
                            self.micro_events["depth"] += 1
                            if self.on_depth is not None:
                                # 交给 BookTracker 统一计算 OBI/微价格，feed 不做业务加工
                                snap = self.on_depth(symbol, ts, bids, asks)
                                if snap is not None:
                                    self._books[symbol] = snap
                            elif bids and asks:
                                self._books[symbol] = OrderBookSnapshot(
                                    symbol=symbol,
                                    ts=ts,
                                    bid_px=bids[0][0],
                                    ask_px=asks[0][0],
                                    bid_qty=bids[0][1],
                                    ask_qty=asks[0][1],
                                )
                        elif event == "aggTrade":
                            self.micro_events["trade"] += 1
                            if self.on_trade is not None:
                                self.on_trade(
                                    str(data["s"]),
                                    int(data["T"]),
                                    float(data["p"]),
                                    float(data["q"]),
                                    bool(data["m"]),
                                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.reconnects += 1
                log.warning("行情 WS 断开（第 %d 次）: %s，%.1fs 后重连", self.reconnects, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(self.max_backoff_s, backoff * 2)

    async def stream(self) -> AsyncIterator[Bar]:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="ws-feed")
        while self._running:
            bar = await self._queue.get()
            yield bar

    async def close(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def build_async_feed(cfg, client) -> AsyncBarFeed:
    if cfg.exchange.feed == "ws":
        ms = cfg.microstructure
        return WsBarFeed(
            cfg.exchange.ws_url,
            cfg.market.symbols,
            cfg.market.interval,
            depth=ms.enabled and ms.use_order_book,
            agg_trades=ms.enabled and ms.use_agg_trades,
        )
    return RestPollBarFeed(
        client,
        cfg.market.symbols,
        cfg.market.interval,
        bar_ms=cfg.market.bar_ms,
        poll_interval_ms=cfg.exchange.poll_interval_ms,
    )


# --------------------------------------------------------------------------- #
# CSV 表头解析
# --------------------------------------------------------------------------- #
def _resolve_columns(header: Sequence[str]) -> dict[str, int]:
    lower = [h.strip().lower() for h in header]
    mapping: dict[str, int] = {}

    aliases = {
        "ts": ("ts", "open_time", "timestamp", "time", "date", "datetime", "opentime"),
        "open": ("open", "o"),
        "high": ("high", "h"),
        "low": ("low", "l"),
        "close": ("close", "c"),
        "volume": ("volume", "v", "vol"),
        "quote_volume": ("quote_volume", "quotevol", "qv", "quote asset volume"),
        "trades": ("trades", "count", "n"),
        "taker_buy_base": ("taker_buy_base", "takerbuybaseassetvolume", "taker_buy_volume", "vb"),
    }
    for key, names in aliases.items():
        for name in names:
            if name in lower:
                mapping[key] = lower.index(name)
                break
    for required in ("ts", "open", "high", "low", "close", "volume"):
        if required not in mapping:
            raise ValueError(f"CSV 缺少列 {required}，实际表头: {header}")
    return mapping


def _parse_ts(value: str) -> int:
    value = value.strip()
    if not value:
        raise ValueError("空时间戳")
    try:
        ts = int(float(value))
    except ValueError:
        from ..models import iso_to_ts

        return iso_to_ts(value)
    # 秒 / 毫秒 / 微秒 自适应
    if ts < 10_000_000_000:
        return ts * 1000
    if ts > 10_000_000_000_000:
        return ts // 1000
    return ts


def _opt_float(row: Sequence[str], idx: int | None) -> float:
    if idx is None or idx >= len(row):
        return 0.0
    try:
        return float(row[idx])
    except (TypeError, ValueError):
        return 0.0
