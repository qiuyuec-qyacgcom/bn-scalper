"""参数网格搜索（研究工具，不是"圣杯生成器"）。

用法：
    python scripts/grid_search.py --config config/backtest.toml \
        --grid rr=1.8,2.2,2.6 --grid vol_mult=1.1,1.5 --grid stop_atr_mult=1.0,2.0

设计要点：
1. **不落库**（persist=False），避免研究过程污染生产库；
2. 每个组合独立新建 Portfolio/Broker/Risk，无状态共享；
3. 输出按 expectancy（每笔期望 R）排序，而不是收益率 —— 收益率对交易次数敏感，
   期望 R 才是可重复性更强的指标；
4. 明确提示过拟合风险：本工具只做**敏感性分析**，不做最优参数选择。
"""

from __future__ import annotations

import argparse
import copy
import itertools
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scalpx.config import Config, load_config  # noqa: E402
from scalpx.data.feed import build_feed  # noqa: E402
from scalpx.engine.backtest import BacktestEngine  # noqa: E402
from scalpx.strategy.registry import create_strategy  # noqa: E402

# 这些 key 属于 [risk] 段，其余归 [strategy.params]
RISK_KEYS = {
    "risk_per_trade", "max_leverage", "max_notional_pct", "max_daily_loss_pct",
    "max_daily_trades", "min_stop_pct", "max_stop_pct", "max_bars_held",
    "consecutive_loss_cooldown", "consecutive_loss_halt", "cooldown_minutes",
    "max_spread_bps", "symbol_cooldown_bars",
}


def _coerce(raw: str):
    for caster in (int, float):
        try:
            return caster(raw)
        except ValueError:
            continue
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw


def _apply(cfg: Config, combo: dict[str, object]) -> Config:
    strat_params = {k: v for k, v in combo.items() if k not in RISK_KEYS}
    risk_over = {k: v for k, v in combo.items() if k in RISK_KEYS}
    new_cfg = replace(cfg, strategy=replace(cfg.strategy, params={**cfg.strategy.params, **strat_params}))
    if risk_over:
        new_cfg = replace(new_cfg, risk=replace(new_cfg.risk, **risk_over))
    return new_cfg


def run_one(cfg: Config) -> dict[str, float]:
    strategy = create_strategy(cfg.strategy.name, cfg.strategy.params)
    feed = build_feed(cfg)
    engine = BacktestEngine(cfg, strategy, feed, store=None, run_id="grid")
    return engine.run().metrics


def main() -> int:
    ap = argparse.ArgumentParser(description="策略参数网格搜索")
    ap.add_argument("--config", default="config/backtest.toml")
    ap.add_argument("--grid", action="append", required=True, help="key=v1,v2,v3")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()

    base = load_config(args.config)
    base = replace(base, data=replace(base.data, persist=False))

    keys: list[str] = []
    values: list[list[object]] = []
    for spec in args.grid:
        key, raw = spec.split("=", 1)
        keys.append(key.strip())
        values.append([_coerce(v.strip()) for v in raw.split(",")])

    rows: list[tuple[float, dict[str, object], dict[str, float]]] = []
    combos = list(itertools.product(*values))
    print(f"共 {len(combos)} 组参数\n")

    for i, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        cfg = _apply(base, params)
        try:
            metrics = run_one(cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i:>3}] {params} -> 失败: {exc}")
            continue
        rows.append((metrics["avg_r"], params, metrics))
        print(
            f"[{i:>3}] {params} -> 收益 {metrics['total_return']:+.2%}  "
            f"回撤 {metrics['max_drawdown']:.2%}  笔数 {int(metrics['trades']):>4}  "
            f"胜率 {metrics['win_rate']:.1%}  期望R {metrics['avg_r']:+.3f}"
        )

    rows.sort(key=lambda r: r[0], reverse=True)
    print("\n== 按期望 R 排序 ==")
    print(f"{'rank':<5}{'avg_r':>8}{'return':>10}{'mdd':>9}{'trades':>8}{'win%':>8}  params")
    for rank, (score, params, metrics) in enumerate(rows[: args.top], 1):
        print(
            f"{rank:<5}{score:>8.3f}{metrics['total_return']:>10.2%}{metrics['max_drawdown']:>9.2%}"
            f"{int(metrics['trades']):>8}{metrics['win_rate']:>8.1%}  {params}"
        )
    print("\n注意：网格搜索只用于敏感性分析。用同一段数据挑参数 = 过拟合，务必做样本外验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
