"""端到端 + 架构约束测试。

架构测试的意义：解耦不是靠"约定"，而是靠 CI 拦住错误依赖。
一旦有人在 strategy 里 import broker，测试立刻失败。
"""

from __future__ import annotations

import ast
import dataclasses
import math
import random
import unittest
from pathlib import Path

from scalpx.config import Config, DataConfig, MarketConfig, RiskConfig, StrategyConfig
from scalpx.data.feed import InMemoryFeed
from scalpx.engine.backtest import BacktestEngine
from scalpx.models import Bar, ExitReason
from scalpx.strategy.registry import available, create_strategy

ROOT = Path(__file__).resolve().parents[1] / "scalpx"


def synth_bars(n: int = 6000, seed: int = 42, start: float = 30_000.0) -> list[Bar]:
    """带趋势与震荡切换的合成行情，用于端到端测试（不依赖磁盘文件）。"""
    rng = random.Random(seed)
    bars: list[Bar] = []
    price = start
    drift = 0.00002
    for i in range(n):
        if i % 900 == 0:
            drift = rng.choice([0.00006, -0.00006, 0.0])
        ret = rng.gauss(drift, 0.0006)
        o = price
        c = max(1.0, o * (1 + ret))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.0004)))
        low = min(o, c) * (1 - abs(rng.gauss(0, 0.0004)))
        vol = max(1.0, rng.gauss(20, 5))
        bars.append(
            Bar(
                symbol="BTCUSDT",
                ts=1_700_000_000_000 + i * 60_000,
                open=o, high=h, low=low, close=c,
                volume=vol, quote_volume=vol * c, trades=int(vol), taker_buy_base=vol * 0.55,
            )
        )
        price = c
    return bars


def make_config(**overrides) -> Config:
    cfg = Config(
        market=MarketConfig(symbols=("BTCUSDT",), price_tick=0.1, qty_step=0.001, min_notional=5.0),
        data=DataConfig(source="csv", persist=False, warmup_bars=320),
        strategy=StrategyConfig(name="trend_pullback", params={}),
        risk=RiskConfig(risk_per_trade=0.0025, min_stop_pct=0.002, max_stop_pct=0.008),
    )
    for key, value in overrides.items():
        cfg = dataclasses.replace(cfg, **{key: value})
    return cfg


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bars = synth_bars()
        cls.cfg = make_config()
        strategy = create_strategy("trend_pullback", cls.cfg.strategy.params)
        engine = BacktestEngine(cls.cfg, strategy, InMemoryFeed(cls.bars), store=None, run_id="test")
        cls.result = engine.run()

    def test_produces_trades(self) -> None:
        self.assertGreater(len(self.result.trades), 5, "端到端链路没有产生交易，说明信号/执行/风控有断裂")

    def test_equity_curve_complete(self) -> None:
        self.assertGreaterEqual(len(self.result.equity_curve), len(self.bars) - 5)
        self.assertTrue(all(math.isfinite(eq) for _, eq in self.result.equity_curve))

    def test_no_forced_flatten(self) -> None:
        """forced_flatten > 0 意味着出现过"持仓无保护单"的严重异常。"""
        self.assertEqual(self.result.oms_stats["forced_flatten"], 0, self.result.oms_stats)

    def test_all_positions_closed_at_end(self) -> None:
        self.assertGreaterEqual(self.result.oms_stats["entries_filled"], len(self.result.trades))
        # 期末不应残留未平仓（run() 会强平收尾）
        self.assertTrue(all(t.exit_ts > 0 for t in self.result.trades))

    def test_trade_accounting_consistency(self) -> None:
        for t in self.result.trades:
            self.assertGreater(t.qty, 0)
            self.assertGreater(t.entry_price, 0)
            self.assertGreaterEqual(t.fees, 0)
            expected_sign = 1 if t.side.value == "long" else -1
            gross = (t.exit_price - t.entry_price) * t.qty * expected_sign
            self.assertAlmostEqual(t.pnl, gross - t.fees, delta=1e-6)

    def test_metrics_sane(self) -> None:
        m = self.result.metrics
        self.assertLessEqual(m["max_drawdown"], 1.0)
        self.assertGreaterEqual(m["win_rate"], 0.0)
        self.assertLessEqual(m["win_rate"], 1.0)
        self.assertAlmostEqual(
            m["final_equity"], self.cfg.backtest.initial_equity * (1 + m["total_return"]), delta=0.05
        )

    def test_risk_never_exceeded_leverage(self) -> None:
        m = self.result.metrics
        self.assertLess(m["max_drawdown"], 0.5, "回撤超过 50%，风控显然失效")

    def test_exit_reasons_are_known(self) -> None:
        for t in self.result.trades:
            self.assertIsInstance(t.reason, ExitReason)


class TestStrategyRegistry(unittest.TestCase):
    def test_both_strategies_registered(self) -> None:
        self.assertIn("trend_pullback", available())
        self.assertIn("donchian", available())

    def test_pluggable_second_strategy_runs(self) -> None:
        cfg = make_config(strategy=StrategyConfig(name="donchian", params={}))
        bars = synth_bars(n=3000, seed=7)
        engine = BacktestEngine(cfg, create_strategy("donchian", {}), InMemoryFeed(bars), store=None, run_id="t2")
        result = engine.run()
        self.assertIn("total_return", result.metrics)
        self.assertEqual(result.oms_stats["forced_flatten"], 0)

    def test_unknown_strategy_raises(self) -> None:
        with self.assertRaises(KeyError):
            create_strategy("does_not_exist", {})


class TestArchitecture(unittest.TestCase):
    """静态检查依赖方向，防止分层腐化。"""

    FORBIDDEN = {
        "strategy": {"scalpx.risk", "scalpx.execution", "scalpx.engine", "scalpx.cli", "scalpx.data"},
        "risk": {"scalpx.execution", "scalpx.engine", "scalpx.cli"},
        "models": {"scalpx"},
        "execution": {"scalpx.engine", "scalpx.cli"},
    }

    def _imports(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:  # 相对导入，还原成绝对
                    base = "scalpx"
                    parts = path.relative_to(ROOT).with_suffix("").parts[:-1]
                    prefix = ".".join((base, *parts))
                    module = f"{prefix}.{module}" if module else prefix
                out.add(module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    out.add(alias.name)
        return out

    def test_layer_dependencies(self) -> None:
        violations: list[str] = []
        for layer, forbidden in self.FORBIDDEN.items():
            for py in (ROOT / layer).rglob("*.py"):
                for module in self._imports(py):
                    if module in forbidden or any(module.startswith(f + ".") for f in forbidden):
                        violations.append(f"{py.relative_to(ROOT)} -> {module}")
        self.assertEqual(violations, [], f"依赖方向违规: {violations}")

    def test_strategy_does_not_touch_broker_or_store(self) -> None:
        """用 AST 取标识符而非全文匹配，避免文档字符串误报。"""
        banned = {"Broker", "SimulatedBroker", "OrderManager", "Store", "RiskManager", "BacktestEngine"}
        violations: list[str] = []
        for py in (ROOT / "strategy").glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in banned:
                    violations.append(f"{py.name}: {node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in banned:
                    violations.append(f"{py.name}: .{node.attr}")
        self.assertEqual(violations, [], f"策略层不应直接接触执行/存储层: {violations}")


if __name__ == "__main__":
    unittest.main()
