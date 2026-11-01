"""报表输出：终端摘要 + 落盘文件（metrics.json / trades.csv / equity.csv / report.txt）。

不引入 pandas / matplotlib：交易系统在服务器上跑，能少一个依赖就少一个故障点。
需要画图时，导出的 CSV 可直接喂给任意分析工具。
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Sequence

from .metrics import format_metrics
from .models import TradeRecord, ts_to_iso


def write_report(
    out_dir: str | Path,
    *,
    run_id: str,
    metrics: dict[str, float],
    trades: Sequence[TradeRecord],
    equity_curve: Sequence[tuple[int, float]],
    extra: dict | None = None,
) -> Path:
    path = Path(out_dir) / run_id
    path.mkdir(parents=True, exist_ok=True)

    (path / "metrics.json").write_text(
        json.dumps({"run_id": run_id, **metrics, **(extra or {})}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (path / "trades.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["symbol", "side", "qty", "entry_ts", "exit_ts", "entry_price", "exit_price",
             "pnl", "fees", "r_multiple", "bars_held", "reason", "entry_reason"]
        )
        for t in trades:
            writer.writerow(
                [t.symbol, t.side.value, f"{t.qty:.6f}", ts_to_iso(t.entry_ts), ts_to_iso(t.exit_ts),
                 f"{t.entry_price:.4f}", f"{t.exit_price:.4f}", f"{t.pnl:.4f}", f"{t.fees:.4f}",
                 f"{t.r_multiple:.3f}", t.bars_held, t.reason.value, t.entry_reason]
            )

    with (path / "equity.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "iso", "equity"])
        for ts, eq in equity_curve:
            writer.writerow([ts, ts_to_iso(ts), f"{eq:.4f}"])

    report = [format_metrics(metrics, title=f"运行 {run_id}")]
    if extra:
        report.append("[运行信息]")
        for key, value in extra.items():
            report.append(f"  {key:<24}{value}")
    if trades:
        report.append("[最近 10 笔交易]")
        report.append(f"  {'时间':<20}{'标的':<10}{'方向':<6}{'盈亏':>10}{'R':>8}  出场原因")
        for t in trades[-10:]:
            report.append(
                f"  {ts_to_iso(t.exit_ts):<20}{t.symbol:<10}{t.side.value:<6}"
                f"{t.pnl:>10.2f}{t.r_multiple:>8.2f}  {t.reason.value}"
            )
    (path / "report.txt").write_text("\n".join(report), encoding="utf-8")
    return path
