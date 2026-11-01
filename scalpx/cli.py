"""命令行入口。

    python -m scalpx backtest --config config/backtest.toml
    python -m scalpx download --symbol BTCUSDT --interval 1m --start 2024-01-01 --end 2024-02-01
    python -m scalpx paper    --config config/paper.toml
    python -m scalpx live     --config config/live.toml --i-understand-live-risk
    python -m scalpx report   --run-id bt-xxx
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .data.feed import build_feed
from .data.store import Store
from .engine.backtest import BacktestEngine
from .models import now_ms
from .reporting import write_report
from .strategy.registry import available, create_strategy


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# --------------------------------------------------------------------------- #
# backtest
# --------------------------------------------------------------------------- #
def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.param:
        overrides = _parse_params(args.param)
        cfg = _with_strategy_params(cfg, overrides)
        print(f"参数覆盖: {overrides}")
    _setup_logging(args.log_level or cfg.run.log_level)
    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
    feed = build_feed(cfg)

    run_id = args.run_id or f"bt-{cfg.run.name}-{now_ms()}"
    store = Store(cfg.data.sqlite_path, run_id=run_id, persist=cfg.data.persist)
    try:
        engine = BacktestEngine(cfg, strategy, feed, store=store if cfg.data.persist else None, run_id=run_id)
        result = engine.run()
    finally:
        store.close()

    print(result.summary())
    print(f"\n信号 {result.signals_generated} 个，被风控拦截 {result.rejected_signals} 个")
    if result.reject_reasons:
        top = sorted(result.reject_reasons.items(), key=lambda kv: -kv[1])[:6]
        print("拦截原因 Top: " + ", ".join(f"{k}×{v}" for k, v in top))
    print(f"OMS: {result.oms_stats}")
    if result.micro_stats:
        m = result.micro_stats
        bars = max(1, result.bars_processed)
        print(
            f"微观结构: 有订单流 {m.get('bars_with_flow', 0)}/{bars} bar "
            f"({m.get('bars_with_flow', 0) / bars:.1%})，"
            f"有盘口 {m.get('bars_with_book', 0)} bar，"
            f"数据源 flow={m.get('flow_sources', 0)} book={m.get('book_sources', 0)}"
        )

    out = write_report(
        cfg.run.output_dir,
        run_id=run_id,
        metrics=result.metrics,
        trades=result.trades,
        equity_curve=result.equity_curve,
        extra={
            "strategy": strategy.name,
            "params": str(strategy.params),
            "bars": result.bars_processed,
            "signals": result.signals_generated,
            "rejected": result.rejected_signals,
            "oms": result.oms_stats,
            "broker": result.broker_stats,
        },
    )
    print(f"报告已写入: {out}")
    return 0


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def cmd_download(args: argparse.Namespace) -> int:
    from .data.binance_rest import BinanceRest
    from .models import ts_to_iso

    _setup_logging("INFO")
    cfg = load_config(args.config) if args.config else None
    base_url = args.base_url or (cfg.exchange.base_url if cfg else "https://fapi.binance.com")
    client = BinanceRest(base_url=base_url)

    symbols = args.symbol.split(",") if args.symbol else list(cfg.market.symbols if cfg else ["BTCUSDT"])
    interval = args.interval or (cfg.market.interval if cfg else "1m")
    start_ms = _parse_date(args.start) if args.start else (cfg.data.start_ms if cfg else None)
    end_ms = _parse_date(args.end) if args.end else (cfg.data.end_ms if cfg else None)
    if start_ms is None or end_ms is None:
        print("必须提供 --start/--end 或在配置文件中指定 data.start/end")
        return 2

    out_dir = Path(args.out or (cfg.data.csv_dir if cfg else "data/raw"))
    out_dir.mkdir(parents=True, exist_ok=True)
    bar_ms = 60_000

    for symbol in symbols:
        bars = client.download_klines(symbol, interval, start_ms, end_ms, bar_ms=bar_ms)
        path = out_dir / f"{symbol}_{interval}.csv"
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["ts", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_base"]
            )
            for b in bars:
                writer.writerow([b.ts, b.open, b.high, b.low, b.close, b.volume, b.quote_volume, b.trades, b.taker_buy_base])
        print(f"{symbol}: {len(bars)} 根 {ts_to_iso(start_ms)} ~ {ts_to_iso(end_ms)} -> {path}")
    return 0


def cmd_download_flow(args: argparse.Namespace) -> int:
    """下载逐笔成交并**在下载过程中聚合成 per-bar 订单流**（不落地原始逐笔）。"""
    from .data.binance_rest import BinanceRest
    from .data.flow_source import write_flow_csv

    _setup_logging("INFO")
    cfg = load_config(args.config) if args.config else None
    base_url = args.base_url or (cfg.exchange.base_url if cfg else "https://fapi.binance.com")
    client = BinanceRest(base_url=base_url)

    symbols = args.symbol.split(",") if args.symbol else list(cfg.market.symbols if cfg else ["BTCUSDT"])
    interval = args.interval or (cfg.market.interval if cfg else "1m")
    bar_ms = {"m": 60_000, "s": 1000, "h": 3_600_000}.get(interval[-1], 60_000) * int(interval[:-1])
    start_ms = _parse_date(args.start) if args.start else (cfg.data.start_ms if cfg else None)
    end_ms = _parse_date(args.end) if args.end else (cfg.data.end_ms if cfg else None)
    if start_ms is None or end_ms is None:
        print("必须提供 --start/--end 或在配置里指定 data.start/end")
        return 2

    out_dir = Path(args.out or (cfg.data.csv_dir if cfg else "data/raw"))
    out_dir.mkdir(parents=True, exist_ok=True)
    store = None
    if args.persist_db:
        store = Store(args.persist_db, run_id="flow-download")

    try:
        for symbol in symbols:
            bars = client.download_flow(
                symbol, interval, start_ms, end_ms, bar_ms=bar_ms,
                large_trade_usd=cfg.microstructure.large_trade_usd if cfg else 50_000.0,
            )
            path = out_dir / f"{symbol}_{interval}_flow.csv"
            write_flow_csv(path, bars)
            if store is not None:
                store.save_bar_flow(bars)
                store.commit()
            total_buy = sum(b.buy_vol for b in bars)
            total_sell = sum(b.sell_vol for b in bars)
            print(
                f"{symbol}: {len(bars)} 根 bar 订单流 -> {path}"
                f"（主动买 {total_buy:.1f} / 主动卖 {total_sell:.1f}）"
            )
    finally:
        if store is not None:
            store.close()
    return 0


# --------------------------------------------------------------------------- #
# paper / live
# --------------------------------------------------------------------------- #
def cmd_paper(args: argparse.Namespace) -> int:
    cfg = load_config(args.config).with_mode("paper")
    _setup_logging(args.log_level or cfg.run.log_level)
    from .engine.live import LiveEngine

    engine = LiveEngine(cfg, send_real_orders=False)
    return engine.run_forever()


def cmd_live(args: argparse.Namespace) -> int:
    # 先做"人类确认"闸门，再读配置：避免误触时先报密钥缺失、掩盖真实意图
    if not args.i_understand_live_risk:
        print(
            "拒绝启动：实盘模式会使用真实资金。\n"
            "确认已理解风险后，追加参数 --i-understand-live-risk 再执行。",
            file=sys.stderr,
        )
        return 3
    cfg = load_config(args.config)
    _setup_logging(args.log_level or cfg.run.log_level)
    from .engine.live import LiveEngine

    engine = LiveEngine(cfg, send_real_orders=True)
    return engine.run_forever()


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def cmd_report(args: argparse.Namespace) -> int:
    from .metrics import format_metrics

    cfg = load_config(args.config) if args.config else None
    db = args.db or (cfg.data.sqlite_path if cfg else "data/scalpx.db")
    store = Store(db, run_id=args.run_id or "", persist=True)
    try:
        run = store.fetch_run(args.run_id)
        if run is None:
            print(f"未找到运行记录: {args.run_id}")
            return 1
        import json

        metrics = json.loads(run["metrics_json"] or "{}")
        print(f"运行 {run['run_id']}  mode={run['mode']}  strategy={run['strategy']}  status={run['status']}")
        print(format_metrics(metrics))
        trades = store.fetch_trades(run["run_id"])
        print(f"\n成交笔数: {len(trades)}")
        reasons: dict[str, int] = {}
        for t in trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<20}{v:>6}")
    finally:
        store.close()
    return 0


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def _parse_date(value: str) -> int:
    from .models import iso_to_ts

    return iso_to_ts(value)


def _parse_params(items: list[str]) -> dict:
    """`--param rr=2.2 --param max_hold_bars=20` -> {"rr": 2.2, ...}"""
    import json

    out: dict = {}
    for item in items:
        if "=" not in item:
            raise ConfigError(f"--param 需要 key=value 形式，收到 {item!r}")
        key, raw = item.split("=", 1)
        try:
            out[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            out[key.strip()] = raw
    return out


def _with_strategy_params(cfg, overrides: dict):
    from dataclasses import replace

    params = {**cfg.strategy.params, **overrides}
    return replace(cfg, strategy=replace(cfg.strategy, params=params))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scalpx", description="币安永续超短线交易系统")
    parser.add_argument("--version", action="version", version="scalpx 1.0.0")
    sub = parser.add_subparsers(dest="command", required=True)

    p_bt = sub.add_parser("backtest", help="运行历史回测")
    p_bt.add_argument("--config", default="config/backtest.toml")
    p_bt.add_argument("--run-id", default=None)
    p_bt.add_argument("--log-level", default=None)
    p_bt.add_argument(
        "--param", action="append", metavar="KEY=VALUE",
        help="覆盖策略参数，可重复，例如 --param rr=2.2 --param vol_mult=1.4",
    )
    p_bt.set_defaults(func=cmd_backtest)

    p_dl = sub.add_parser("download", help="下载币安历史 K 线到 CSV")
    p_dl.add_argument("--config", default=None)
    p_dl.add_argument("--symbol", default=None, help="逗号分隔，如 BTCUSDT,ETHUSDT")
    p_dl.add_argument("--interval", default=None)
    p_dl.add_argument("--start", default=None)
    p_dl.add_argument("--end", default=None)
    p_dl.add_argument("--out", default=None)
    p_dl.add_argument("--base-url", default=None)
    p_dl.set_defaults(func=cmd_download)

    p_df = sub.add_parser("download-flow", help="下载逐笔成交并聚合成 per-bar 订单流（CVD 数据源）")
    p_df.add_argument("--config", default=None)
    p_df.add_argument("--symbol", default=None)
    p_df.add_argument("--interval", default=None)
    p_df.add_argument("--start", default=None)
    p_df.add_argument("--end", default=None)
    p_df.add_argument("--out", default=None)
    p_df.add_argument("--base-url", default=None)
    p_df.add_argument("--persist-db", default=None, help="同时写入 SQLite 的 bar_flow 表")
    p_df.set_defaults(func=cmd_download_flow)

    p_paper = sub.add_parser("paper", help="纸上交易（真实行情 + 模拟成交）")
    p_paper.add_argument("--config", default="config/paper.toml")
    p_paper.add_argument("--log-level", default=None)
    p_paper.set_defaults(func=cmd_paper)

    p_live = sub.add_parser("live", help="实盘交易（需显式确认）")
    p_live.add_argument("--config", default="config/live.toml")
    p_live.add_argument("--log-level", default=None)
    p_live.add_argument("--i-understand-live-risk", action="store_true")
    p_live.set_defaults(func=cmd_live)

    p_rep = sub.add_parser("report", help="查看已落库的回测/实盘结果")
    p_rep.add_argument("--run-id", default=None)
    p_rep.add_argument("--config", default=None)
    p_rep.add_argument("--db", default=None)
    p_rep.set_defaults(func=cmd_report)

    p_strat = sub.add_parser("strategies", help="列出已注册策略")
    p_strat.set_defaults(func=lambda a: (print("\n".join(available())), 0)[1])

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"文件缺失: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
