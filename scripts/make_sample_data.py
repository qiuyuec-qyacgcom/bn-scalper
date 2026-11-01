"""生成可复现的合成 1m 行情，用于在没有网络/API Key 时跑通回测链路。

刻意构造的三类行情（这是超短线策略最容易翻车的三种环境）：
1) 趋势段：带漂移的随机游走 + 回撤，用来验证趋势跟随；
2) 震荡段：均值回复，用来验证过滤条件是否避免了来回打脸；
3) 脉冲段：突发放量跳变，用来验证止损/熔断/滑点。

数据不是"真实历史"，只用于**验证工程链路正确性**。
真实研究请用 `python -m scalpx download` 拉币安公开 K 线。
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import time
from pathlib import Path


def _intrabar_delta_path(delta: float, volume: float, rng: random.Random, steps: int = 24) -> tuple[float, float]:
    """构造一根 bar 内的累计 delta 路径，返回 (max_delta, min_delta)。

    真实市场里 delta 不会线性走到终点：先冲高再回吐（吸收）是超短线最常见形态。
    这里用带噪声的随机游走，并把终点缩放到实际 delta，保证与 taker_buy 一致。
    """
    if volume <= 0 or steps < 2:
        return max(delta, 0.0), min(delta, 0.0)
    raw = [rng.gauss(delta / steps, volume / steps * 0.35) for _ in range(steps)]
    cum = 0.0
    path = []
    for x in raw:
        cum += x
        path.append(cum)
    # 把路径终点对齐到真实 delta
    end = path[-1]
    scale = 1.0 if abs(end) < 1e-12 else delta / end
    path = [p * scale for p in path]
    return max(max(path), 0.0, delta), min(min(path), 0.0, delta)


def generate(
    symbol: str,
    *,
    start_ms: int,
    days: int,
    seed: int,
    start_price: float,
    bar_vol: float,
) -> tuple[list[list[float]], list[list[float]]]:
    rng = random.Random(seed)
    bars: list[list[float]] = []
    flows: list[list[float]] = []
    price = start_price
    n = days * 24 * 60
    ts = start_ms

    regime = "trend_up"
    regime_left = 0
    drift = 0.0
    vol = bar_vol

    for _ in range(n):
        if regime_left <= 0:
            regime = rng.choices(
                ["trend_up", "trend_down", "range", "shock"], weights=[0.32, 0.28, 0.36, 0.04]
            )[0]
            regime_left = rng.randint(180, 900)
            if regime == "trend_up":
                drift, vol = rng.uniform(0.000015, 0.000060), bar_vol * rng.uniform(0.9, 1.3)
            elif regime == "trend_down":
                drift, vol = -rng.uniform(0.000015, 0.000060), bar_vol * rng.uniform(0.9, 1.3)
            elif regime == "range":
                drift, vol = 0.0, bar_vol * rng.uniform(0.6, 0.9)
            else:
                drift, vol = rng.uniform(-0.0004, 0.0004), bar_vol * rng.uniform(2.5, 5.0)
        regime_left -= 1

        open_px = price
        ret = rng.gauss(drift, vol)
        if regime == "range":
            # 均值回复：把价格拉回起点附近
            ret -= (price / start_price - 1.0) * 0.0012
        close_px = max(1.0, open_px * (1.0 + ret))

        wick = abs(rng.gauss(0, vol * 0.8))
        high_px = max(open_px, close_px) * (1.0 + wick * rng.uniform(0.3, 1.0))
        low_px = min(open_px, close_px) * (1.0 - wick * rng.uniform(0.3, 1.0))

        base_vol = 12.0 * (1.0 + abs(ret) / max(vol, 1e-9))
        volume = max(0.1, rng.lognormvariate(math.log(base_vol), 0.45))
        # 主动买占比与当期收益正相关（真实市场里 taker buy 是顺势的）
        buy_ratio = min(0.85, max(0.15, 0.5 + 0.35 * math.tanh(ret / max(vol, 1e-9)) + rng.gauss(0, 0.06)))
        taker_buy = volume * buy_ratio
        trades = int(max(3, rng.gauss(volume * 0.8, volume * 0.2)))

        bars.append(
            [
                ts,
                round(open_px, 2),
                round(high_px, 2),
                round(low_px, 2),
                round(close_px, 2),
                round(volume, 4),
                round(volume * close_px, 2),
                trades,
                round(taker_buy, 4),
            ]
        )

        sell_vol = max(0.0, volume - taker_buy)
        delta = taker_buy - sell_vol
        max_delta, min_delta = _intrabar_delta_path(delta, volume, rng)
        flows.append(
            [
                ts,
                round(taker_buy, 6),
                round(sell_vol, 6),
                trades,
                round(max_delta, 6),
                round(min_delta, 6),
                round(volume * rng.uniform(0.03, 0.18), 6),
            ]
        )
        price = close_px
        ts += 60_000
    return bars, flows


def write_csv(path: Path, bars: list[list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["ts", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_base"]
        )
        writer.writerows(bars)


def write_flow_csv(path: Path, flows: list[list[float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "buy_vol", "sell_vol", "trades", "max_delta", "min_delta", "large_vol"])
        writer.writerows(flows)


def main() -> None:
    ap = argparse.ArgumentParser(description="生成合成 1m K 线用于回测联调")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--seed", type=int, default=20240101)
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--start", default="2024-01-01")
    args = ap.parse_args()

    start_ms = int(time.mktime(time.strptime(args.start, "%Y-%m-%d")) * 1000) - time.timezone * 1000
    specs = [
        ("BTCUSDT", 65_000.0, 0.00055),
        ("ETHUSDT", 3_200.0, 0.00070),
    ]
    for i, (symbol, price, vol) in enumerate(specs):
        bars, flows = generate(
            symbol, start_ms=start_ms, days=args.days, seed=args.seed + i,
            start_price=price, bar_vol=vol,
        )
        path = Path(args.out) / f"{symbol}_1m.csv"
        write_csv(path, bars)
        flow_path = Path(args.out) / f"{symbol}_1m_flow.csv"
        write_flow_csv(flow_path, flows)
        print(f"{symbol}: {len(bars)} bars -> {path}")
        print(f"{symbol}: {len(flows)} flow rows -> {flow_path}")


if __name__ == "__main__":
    main()
