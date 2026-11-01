"""配置加载与校验。

设计原则：
1. **单一来源**：所有可调参数都在 TOML 中，代码里不出现魔法数字。
2. **显式校验**：非法值在启动期直接失败（fail-fast），而不是在盘中。
3. **结构化**：拆成 run/market/data/strategy/risk/execution/backtest/exchange
   多个 dataclass，各模块只拿到自己那一段，避免"全局配置"耦合。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .models import iso_to_ts


class ConfigError(ValueError):
    """配置非法。"""


# --------------------------------------------------------------------------- #
# 各配置段
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RunConfig:
    name: str = "scalpx"
    mode: str = "backtest"  # backtest | paper | live
    output_dir: str = "runs"
    log_level: str = "INFO"
    seed: int = 7

    def validate(self) -> None:
        if self.mode not in ("backtest", "paper", "live"):
            raise ConfigError(f"run.mode 必须是 backtest/paper/live，收到 {self.mode!r}")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ConfigError(f"run.log_level 非法: {self.log_level!r}")


@dataclass(frozen=True, slots=True)
class MarketConfig:
    symbols: tuple[str, ...] = ("BTCUSDT",)
    interval: str = "1m"
    market_type: str = "futures"  # futures | spot
    contract_size: float = 1.0
    price_tick: float = 0.1
    qty_step: float = 0.001
    min_qty: float = 0.001
    min_notional: float = 5.0

    @property
    def bar_ms(self) -> int:
        unit = self.interval[-1]
        n = int(self.interval[:-1])
        return n * {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]

    def validate(self) -> None:
        if not self.symbols:
            raise ConfigError("market.symbols 不能为空")
        if self.interval[-1] not in ("s", "m", "h", "d"):
            raise ConfigError(f"market.interval 非法: {self.interval!r}")
        if self.price_tick <= 0 or self.qty_step <= 0:
            raise ConfigError("market.price_tick / qty_step 必须为正")


@dataclass(frozen=True, slots=True)
class DataConfig:
    source: str = "csv"  # csv | sqlite | binance_rest
    csv_dir: str = "data/raw"
    sqlite_path: str = "data/scalpx.db"
    start: str = "2024-01-01"
    end: str = "2024-02-01"
    warmup_bars: int = 300
    persist: bool = True  # 是否把行情/订单落库

    @property
    def start_ms(self) -> int:
        return iso_to_ts(self.start)

    @property
    def end_ms(self) -> int:
        return iso_to_ts(self.end)

    def validate(self) -> None:
        if self.source not in ("csv", "sqlite", "binance_rest"):
            raise ConfigError(f"data.source 非法: {self.source!r}")
        if self.warmup_bars < 1:
            raise ConfigError("data.warmup_bars 必须 >= 1")
        if self.start_ms >= self.end_ms:
            raise ConfigError("data.start 必须早于 data.end")


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    name: str = "trend_pullback"
    params: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.name:
            raise ConfigError("strategy.name 不能为空")


@dataclass(frozen=True, slots=True)
class RiskConfig:
    # 单笔风险（占权益比例）
    risk_per_trade: float = 0.0025
    max_positions: int = 2
    max_positions_per_symbol: int = 1
    max_leverage: float = 3.0
    max_notional_pct: float = 3.0  # 名义价值 / 权益 上限
    # 单日熔断
    max_daily_loss_pct: float = 0.025
    max_daily_trades: int = 40
    # 连亏冷却
    consecutive_loss_cooldown: int = 3
    cooldown_minutes: int = 30
    consecutive_loss_halt: int = 5
    # 其它
    min_stop_pct: float = 0.0040
    max_stop_pct: float = 0.008
    max_bars_held: int = 15
    max_spread_bps: float = 4.0
    symbol_cooldown_bars: int = 3
    # 相关性：同向持仓的风险折算系数
    correlated_risk_factor: float = 1.5
    # 成本闸门：往返成本 / 止损距离 的上限（超短线最关键的护栏）
    # 0.35 意味着"手续费最多吃掉 0.35R"；配合 min_stop_pct=0.4% 时实际约 0.18R。
    # 设 0 表示关闭该闸门。
    max_cost_ratio: float = 0.35

    def validate(self) -> None:
        if not 0 < self.risk_per_trade <= 0.02:
            raise ConfigError("risk.risk_per_trade 必须在 (0, 0.02] 内")
        if self.max_leverage < 1:
            raise ConfigError("risk.max_leverage 必须 >= 1")
        if self.max_positions < 1:
            raise ConfigError("risk.max_positions 必须 >= 1")
        if not 0 < self.max_daily_loss_pct < 1:
            raise ConfigError("risk.max_daily_loss_pct 必须在 (0, 1) 内")
        if self.min_stop_pct <= 0 or self.max_stop_pct < self.min_stop_pct:
            raise ConfigError("risk.min_stop_pct / max_stop_pct 关系非法")
        if self.max_bars_held < 1:
            raise ConfigError("risk.max_bars_held 必须 >= 1")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    entry_order_type: str = "limit_post_only"  # market | limit_post_only | limit_ioc
    entry_timeout_ms: int = 3_000
    entry_max_retries: int = 2
    entry_offset_bps: float = 0.0  # 挂单相对盘口偏移
    maker_fee: float = 0.0002
    taker_fee: float = 0.0004
    slippage_bps: float = 1.0
    latency_ms: int = 120
    use_exchange_stops: bool = True  # 交易所原生止损/止盈
    stop_place_deadline_ms: int = 800  # 成交后必须在此时间内挂上保护单
    reconcile_interval_s: int = 30
    max_order_reject_streak: int = 3
    tick_size_for_price: float = 0.1

    def validate(self) -> None:
        if self.entry_order_type not in ("market", "limit_post_only", "limit_ioc"):
            raise ConfigError(f"execution.entry_order_type 非法: {self.entry_order_type!r}")
        if self.entry_max_retries < 0:
            raise ConfigError("execution.entry_max_retries 不能为负")
        if self.entry_timeout_ms < 100:
            raise ConfigError("execution.entry_timeout_ms 必须 >= 100")
        if not 0 <= self.maker_fee < 0.01 or not 0 <= self.taker_fee < 0.01:
            raise ConfigError("execution 手续费率非法")


@dataclass(frozen=True, slots=True)
class MicrostructureConfig:
    """微观结构数据的采集与使用口径。

    注意：这里只放**数据管道**相关的参数；进场的阈值（delta_ratio / obi）属于策略参数，
    因为它们直接定义"什么算合格信号"。
    """

    enabled: bool = True
    use_agg_trades: bool = True      # 订阅 aggTrade 计算真实 CVD
    use_order_book: bool = True      # 订阅 depth20@100ms 计算 OBI
    depth_bps: float = 5.0           # 盘口带宽（bps），用于计算深度失衡
    obi_levels: int = 20             # 盘口档数
    cvd_window: int = 20             # CVD 斜率窗口（bar 数）
    max_book_age_ms: int = 2_000     # 盘口超过该时长视为不可用
    large_trade_usd: float = 50_000.0  # 大单阈值（USD），用于 large_ratio
    # 堆叠失衡
    stack_ratio: float = 3.0         # 对角线失衡阈值（bid/ask >= 3 记为买盘主导）
    stack_range: int = 3             # 连续多少档同向才算堆叠
    min_level_qty: float = 0.0       # 单价位最小量（低于该值视为噪声）
    # 盘口质量熔断
    max_bad_ratio: float = 0.5       # 滚动窗口内异常比例上限（超过即判定盘口不可信）
    gate_on_book_quality: bool = True  # 盘口不可信时是否熔断入场（BOOK_UNTRUSTED）
    persist_flow: bool = True        # 实盘是否把成交流聚合结果落库
    persist_book: bool = False       # 实盘是否录制盘口（默认关，避免写放大）
    persist_book_interval_ms: int = 1_000

    def validate(self) -> None:
        if self.depth_bps <= 0:
            raise ConfigError("microstructure.depth_bps 必须为正")
        if not 1 <= self.obi_levels <= 500:
            raise ConfigError("microstructure.obi_levels 必须在 [1, 500]")
        if self.cvd_window < 2:
            raise ConfigError("microstructure.cvd_window 必须 >= 2")
        if self.max_book_age_ms < 100:
            raise ConfigError("microstructure.max_book_age_ms 必须 >= 100")
        if self.stack_ratio <= 1.0:
            raise ConfigError("microstructure.stack_ratio 必须 > 1")
        if self.stack_range < 2:
            raise ConfigError("microstructure.stack_range 必须 >= 2")
        if not 0.0 <= self.max_bad_ratio <= 1.0:
            raise ConfigError("microstructure.max_bad_ratio 必须在 [0,1]")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_equity: float = 10_000.0
    intrabar_path: str = "ohlc"  # ohlc | ohlc_pessimistic
    pessimistic_intrabar: bool = True
    funding_bps_per_8h: float = 0.0
    warmup_bars: int | None = None
    assumed_spread_bps: float = 1.5  # 历史数据无盘口，用固定点差代理风控的点差闸门

    def validate(self) -> None:
        if self.initial_equity <= 0:
            raise ConfigError("backtest.initial_equity 必须为正")
        if self.assumed_spread_bps < 0:
            raise ConfigError("backtest.assumed_spread_bps 不能为负")


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    name: str = "binance"
    base_url: str = "https://fapi.binance.com"
    ws_url: str = "wss://fstream.binance.com"
    api_key_env: str = "BINANCE_API_KEY"
    api_secret_env: str = "BINANCE_API_SECRET"
    testnet: bool = True
    recv_window_ms: int = 5000
    feed: str = "ws"  # ws | rest_poll
    poll_interval_ms: int = 2_000
    user_stream: bool = True

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    @property
    def api_secret(self) -> str:
        return os.environ.get(self.api_secret_env, "")

    def validate(self) -> None:
        if self.feed not in ("ws", "rest_poll"):
            raise ConfigError(f"exchange.feed 非法: {self.feed!r}")
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError("exchange.base_url 必须以 http(s):// 开头")


@dataclass(frozen=True, slots=True)
class Config:
    run: RunConfig = field(default_factory=RunConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    data: DataConfig = field(default_factory=DataConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    microstructure: MicrostructureConfig = field(default_factory=MicrostructureConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "Config":
        self.run.validate()
        self.market.validate()
        self.data.validate()
        self.strategy.validate()
        self.risk.validate()
        self.execution.validate()
        self.microstructure.validate()
        self.backtest.validate()
        self.exchange.validate()
        if self.run.mode == "live" and not self.exchange.api_key:
            raise ConfigError(
                f"实盘模式需要设置环境变量 {self.exchange.api_key_env} / "
                f"{self.exchange.api_secret_env}"
            )
        return self

    def with_mode(self, mode: str) -> "Config":
        return replace(self, run=replace(self.run, mode=mode))


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #
def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"配置段 [{name}] 必须是表")
    return value


def _build(cls: type, data: dict[str, Any], section: str):
    """按 dataclass 字段过滤未知键，未知键直接报错（防止拼写错误静默失效）。"""
    allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"[{section}] 存在未知配置项: {sorted(unknown)}")
    converted = dict(data)
    if cls is MarketConfig and "symbols" in converted:
        converted["symbols"] = tuple(str(s).upper() for s in converted["symbols"])
    try:
        return cls(**converted)
    except TypeError as exc:  # pragma: no cover - 防御性
        raise ConfigError(f"[{section}] 构造失败: {exc}") from exc


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"配置文件不存在: {p}")
    with p.open("rb") as fh:
        raw = tomllib.load(fh)

    strategy_raw = _section(raw, "strategy")
    strategy_params = strategy_raw.pop("params", {})
    if not isinstance(strategy_params, dict):
        raise ConfigError("[strategy.params] 必须是表")

    cfg = Config(
        run=_build(RunConfig, _section(raw, "run"), "run"),
        market=_build(MarketConfig, _section(raw, "market"), "market"),
        data=_build(DataConfig, _section(raw, "data"), "data"),
        strategy=_build(
            StrategyConfig,
            {**strategy_raw, "params": strategy_params},
            "strategy",
        ),
        risk=_build(RiskConfig, _section(raw, "risk"), "risk"),
        execution=_build(ExecutionConfig, _section(raw, "execution"), "execution"),
        microstructure=_build(
            MicrostructureConfig, _section(raw, "microstructure"), "microstructure"
        ),
        backtest=_build(BacktestConfig, _section(raw, "backtest"), "backtest"),
        exchange=_build(ExchangeConfig, _section(raw, "exchange"), "exchange"),
        raw=raw,
    )
    return cfg.validate()
