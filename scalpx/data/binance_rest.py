"""币安 REST 客户端（纯标准库实现，零第三方依赖）。

为什么手写而不用 ccxt / python-binance：
- 降低依赖面与供应链风险（交易系统只引入必要代码）；
- 只实现用到的 8 个端点，行为可审计；
- 签名、时间同步、重试、限速策略完全可控。

端点覆盖：kline / exchangeInfo / 下单 / 撤单 / 查单 / 持仓 / 挂单 / 账户 / listenKey。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..models import Bar, now_ms

log = logging.getLogger(__name__)


class BinanceAPIError(RuntimeError):
    """币安返回的业务错误（HTTP 200 但 code != 0，或 4xx）。"""

    def __init__(self, status: int, code: int, message: str, endpoint: str) -> None:
        super().__init__(f"[{endpoint}] HTTP {status} code={code} msg={message}")
        self.status = status
        self.code = code
        self.message = message
        self.endpoint = endpoint

    @property
    def is_retryable(self) -> bool:
        # -1021 时间戳错 / -1003 限频 / 5xx 可退避重试；其余视为确定性拒绝
        return self.code in (-1021, -1003, -1001, -1000) or self.status >= 500

    @property
    def is_rejected(self) -> bool:
        return self.code in (-2010, -1013, -4131, -4164, -1102, -2019)


class NetworkError(RuntimeError):
    """网络层失败（超时/连接重置），属于 **结果未知** 状态，必须查单确认。"""


@dataclass(slots=True)
class RateLimiter:
    """轻量令牌桶，防止触发 -1003 封禁。"""

    capacity: int = 20
    refill_per_s: float = 8.0
    _tokens: float = 20.0
    _last: float = 0.0

    def acquire(self, weight: int = 1) -> None:
        now = time.monotonic()
        if self._last == 0.0:
            self._last = now
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_s)
        if self._tokens < weight:
            sleep_s = (weight - self._tokens) / self.refill_per_s
            time.sleep(max(0.0, sleep_s))
            self._tokens = weight
        self._tokens -= weight


class BinanceRest:
    """同步 REST 客户端。异步引擎通过 `asyncio.to_thread` 调用。"""

    def __init__(
        self,
        *,
        base_url: str = "https://fapi.binance.com",
        api_key: str = "",
        api_secret: str = "",
        recv_window_ms: int = 5000,
        timeout_s: float = 10.0,
        max_retries: int = 3,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window_ms = recv_window_ms
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.limiter = limiter or RateLimiter()
        self._time_offset_ms = 0
        self._time_synced = False

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #
    def sync_time(self) -> int:
        """用 /fapi/v1/time 校正本地时钟，避免 -1021。"""
        local_before = now_ms()
        data = self._request("GET", "/fapi/v1/time", signed=False)
        local_after = now_ms()
        server = int(data["serverTime"])
        self._time_offset_ms = server - (local_before + local_after) // 2
        self._time_synced = True
        log.info("时间同步完成，本地偏移 %d ms", self._time_offset_ms)
        return self._time_offset_ms

    def _signature(self, query: str) -> str:
        return hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
    ) -> Any:
        params = dict(params or {})
        if signed:
            if not self.api_key or not self.api_secret:
                raise RuntimeError("缺少 API 凭证，无法调用签名端点")
            if not self._time_synced:
                self.sync_time()
            params["timestamp"] = now_ms() + self._time_offset_ms
            params["recvWindow"] = self.recv_window_ms
            query = urllib.parse.urlencode(params, doseq=True)
            query += "&signature=" + self._signature(query)
        else:
            query = urllib.parse.urlencode(params, doseq=True)

        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        body = None
        if method in ("POST", "PUT", "DELETE"):
            body = query.encode()
            url = f"{self.base_url}{path}"
            if signed:
                url = f"{self.base_url}{path}?{query}"

        headers = {"User-Agent": "scalpx/1.0", "X-MBX-APIKEY": self.api_key}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.limiter.acquire()
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = {"code": -1, "msg": raw[:200]}
                err = BinanceAPIError(exc.code, int(payload.get("code", -1)), str(payload.get("msg", "")), path)
                if err.code == -1021 and attempt <= 2:
                    self.sync_time()
                    continue
                if err.is_retryable and attempt < self.max_retries:
                    time.sleep(0.25 * 2 ** (attempt - 1))
                    last_exc = err
                    continue
                raise err from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt < self.max_retries:
                    time.sleep(0.25 * 2 ** (attempt - 1))
                    last_exc = exc
                    continue
                raise NetworkError(f"{method} {path} 网络失败: {exc}") from exc
        raise NetworkError(f"{method} {path} 重试耗尽: {last_exc}")

    # ------------------------------------------------------------------ #
    # 公共行情
    # ------------------------------------------------------------------ #
    def klines(
        self,
        symbol: str,
        interval: str = "1m",
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1500,
    ) -> list[Bar]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": min(limit, 1500)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        raw = self._request("GET", "/fapi/v1/klines", params)
        return [
            Bar(
                symbol=symbol,
                ts=int(k[0]),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5]),
                quote_volume=float(k[7]),
                trades=int(k[8]),
                taker_buy_base=float(k[9]),
                closed=True,
            )
            for k in raw
        ]

    def download_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        *,
        bar_ms: int = 60_000,
        progress: bool = True,
    ) -> list[Bar]:
        """分页拉取历史 K 线，自动去重、补洞检测。"""
        out: list[Bar] = []
        cursor = start_ms
        while cursor < end_ms:
            batch = self.klines(symbol, interval, start_ms=cursor, end_ms=end_ms, limit=1500)
            if not batch:
                break
            out.extend(b for b in batch if b.ts < end_ms)
            cursor = batch[-1].ts + bar_ms
            if progress:
                log.info("%s 已下载 %d 根，游标 %d", symbol, len(out), cursor)
            if len(batch) < 1500:
                break
        # 去重 + 排序
        seen: dict[int, Bar] = {b.ts: b for b in out}
        bars = [seen[k] for k in sorted(seen)]
        gaps = sum(1 for a, b in zip(bars, bars[1:]) if b.ts - a.ts > bar_ms)
        if gaps:
            log.warning("%s 检测到 %d 个缺口（可能停牌/维护）", symbol, gaps)
        return bars

    def agg_trades(
        self,
        symbol: str,
        *,
        from_id: int | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """逐笔聚合成交。`from_id` 与 `startTime/endTime` 二选一。"""
        params: dict[str, Any] = {"symbol": symbol, "limit": min(limit, 1000)}
        if from_id is not None:
            params["fromId"] = from_id
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return self._request("GET", "/fapi/v1/aggTrades", params)

    def download_flow(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        *,
        bar_ms: int = 60_000,
        large_trade_usd: float = 50_000.0,
        max_batches: int = 100_000,
        progress: bool = True,
    ) -> list:
        """把逐笔成交**直接聚合**成按 bar 的成交流，不落地原始逐笔。

        为什么不在本地保存原始 aggTrade：BTCUSDT 每天约 200~500 万条，
        30 天就是 1 亿+ 行、数十 GB，而策略真正需要的只是每根 bar 的 6 个聚合量。
        聚合在下载过程中完成，磁盘成本降低 3 个数量级。
        """
        from ..microstructure import FlowAggregator

        agg = FlowAggregator(
            bar_ms=bar_ms, large_trade_usd=large_trade_usd, symbol=symbol
        )
        out = []
        cursor_ts = start_ms
        batches = 0
        while cursor_ts < end_ms and batches < max_batches:
            batch = self.agg_trades(symbol, start_ms=cursor_ts, limit=1000)
            batches += 1
            if not batch:
                # 该时间窗没有成交（休市/低流动性），直接跳到下一个 bar
                cursor_ts += bar_ms * 5
                continue
            for t in batch:
                ts = int(t["T"])
                if ts < start_ms:
                    continue
                if ts >= end_ms:
                    cursor_ts = end_ms
                    break
                agg.on_trade(ts, float(t["p"]), float(t["q"]), bool(t["m"]))
            last_ts = int(batch[-1]["T"])
            # 收走已完结的 bucket（保留最后一个未收盘的）
            closed_upto = (last_ts // bar_ms) * bar_ms
            for ts in sorted(agg._buckets):  # noqa: SLF001 - 内部聚合状态，仅下载器使用
                if ts < closed_upto:
                    bar = agg.finalize(ts)
                    if bar is not None:
                        out.append(bar)
            if last_ts < cursor_ts:
                cursor_ts += bar_ms
            else:
                cursor_ts = last_ts + 1
            if progress and batches % 200 == 0:
                log.info("%s 已聚合 %d 根 bar，游标 %d", symbol, len(out), cursor_ts)
            if len(batch) < 1000 and last_ts >= end_ms:
                break
        return out

    def exchange_info(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def symbol_filters(self, symbol: str) -> dict[str, Any]:
        info = self.exchange_info()
        for item in info.get("symbols", []):
            if item.get("symbol") == symbol:
                filters = {f["filterType"]: f for f in item.get("filters", [])}
                return {
                    "price_tick": float(filters.get("PRICE_FILTER", {}).get("tickSize", 0.01)),
                    "qty_step": float(filters.get("LOT_SIZE", {}).get("stepSize", 0.001)),
                    "min_qty": float(filters.get("LOT_SIZE", {}).get("minQty", 0.001)),
                    "min_notional": float(filters.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
                    "price_precision": int(item.get("pricePrecision", 2)),
                    "qty_precision": int(item.get("quantityPrecision", 3)),
                }
        raise ValueError(f"交易对不存在: {symbol}")

    # ------------------------------------------------------------------ #
    # 交易
    # ------------------------------------------------------------------ #
    def new_order(self, **params: Any) -> dict[str, Any]:
        for key in ("price", "stopPrice", "quantity"):
            if key in params and params[key] is not None:
                params[key] = f"{float(params[key]):.10f}".rstrip("0").rstrip(".")
        return self._request("POST", "/fapi/v1/order", params, signed=True)

    def cancel_order(self, symbol: str, **params: Any) -> dict[str, Any]:
        params["symbol"] = symbol
        return self._request("DELETE", "/fapi/v1/order", params, signed=True)

    def cancel_all(self, symbol: str) -> Any:
        return self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)

    def query_order(self, symbol: str, **params: Any) -> dict[str, Any]:
        params["symbol"] = symbol
        return self._request("GET", "/fapi/v1/order", params, signed=True)

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/openOrders", params, signed=True)

    def positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": symbol} if symbol else {}
        rows = self._request("GET", "/fapi/v2/positionRisk", params, signed=True)
        return [r for r in rows if float(r.get("positionAmt", 0)) != 0.0]

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v2/account", signed=True)

    def balance(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/fapi/v2/balance", signed=True)
        return [r for r in data if float(r.get("balance", 0)) > 0]

    def set_leverage(self, symbol: str, leverage: int) -> Any:
        return self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> Any:
        try:
            return self._request(
                "POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type}, signed=True
            )
        except BinanceAPIError as exc:
            if exc.code == -4046:  # 已经是该模式
                return {"code": 0}
            raise

    # ------------------------------------------------------------------ #
    # 用户数据流
    # ------------------------------------------------------------------ #
    def create_listen_key(self) -> str:
        data = self._request("POST", "/fapi/v1/listenKey", signed=True)
        return str(data["listenKey"])

    def keepalive_listen_key(self, listen_key: str) -> Any:
        return self._request("PUT", "/fapi/v1/listenKey", {"listenKey": listen_key}, signed=True)

    def close_listen_key(self, listen_key: str) -> Any:
        return self._request("DELETE", "/fapi/v1/listenKey", {"listenKey": listen_key}, signed=True)
