"""运行引擎：回测与实盘共用策略/风控/撮合语义，只替换 Clock 与 Broker。"""

from __future__ import annotations

from .backtest import BacktestEngine, BacktestResult

__all__ = ["BacktestEngine", "BacktestResult"]
