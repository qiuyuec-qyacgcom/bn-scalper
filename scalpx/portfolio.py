"""组合与账务：仓位、已实现/未实现盈亏、权益曲线。

模型简化（与币安 U 本位永续一致的部分）：
- 逐仓（isolated），单标的单向持仓（one-way mode）；
- 费用在下单成交时立即从现金扣除；
- equity = 已实现现金 + 未实现盈亏；
- 不建模保证金率/强平（由风控层的杠杆与止损上限保证远离强平）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .models import ExitReason, Fill, Position, PositionSide, TradeRecord

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Portfolio:
    initial_equity: float = 10_000.0
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[TradeRecord] = field(default_factory=list)
    total_fees: float = 0.0
    realized_pnl: float = 0.0
    peak_equity: float = 0.0
    _last_prices: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cash == 0.0:
            self.cash = self.initial_equity
        if self.peak_equity == 0.0:
            self.peak_equity = self.initial_equity

    # ------------------------------------------------------------------ #
    # 估值
    # ------------------------------------------------------------------ #
    def mark(self, symbol: str, price: float) -> None:
        if price > 0:
            self._last_prices[symbol] = price

    def mark_many(self, prices: dict[str, float]) -> None:
        for sym, px in prices.items():
            self.mark(sym, px)

    def unrealized(self, prices: dict[str, float] | None = None) -> float:
        prices = prices or self._last_prices
        total = 0.0
        for sym, pos in self.positions.items():
            px = prices.get(sym, pos.entry_price)
            total += pos.unrealized(px)
        return total

    def equity(self, prices: dict[str, float] | None = None) -> float:
        return self.cash + self.unrealized(prices)

    def exposure(self, prices: dict[str, float] | None = None) -> float:
        prices = prices or self._last_prices
        return sum(abs(p.qty * prices.get(sym, p.entry_price)) for sym, p in self.positions.items())

    def drawdown(self, prices: dict[str, float] | None = None) -> float:
        eq = self.equity(prices)
        self.peak_equity = max(self.peak_equity, eq)
        return 0.0 if self.peak_equity <= 0 else 1.0 - eq / self.peak_equity

    def position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    @property
    def prices(self) -> dict[str, float]:
        """最近一次标记价格（只读副本）。"""
        return dict(self._last_prices)

    @property
    def open_positions(self) -> int:
        return len(self.positions)

    def unrealized_r(self) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            px = self._last_prices.get(sym, pos.entry_price)
            total += pos.unrealized_r(px)
        return total

    # ------------------------------------------------------------------ #
    # 成交处理
    # ------------------------------------------------------------------ #
    def on_fill(self, fill: Fill, *, reason: ExitReason = ExitReason.MANUAL) -> TradeRecord | None:
        """处理一笔成交，返回因该成交而 **完全平仓** 的交易记录。"""
        symbol = fill.symbol
        pos = self.positions.get(symbol)

        if pos is None:
            self._open(fill)
            self.peak_equity = max(self.peak_equity, self.equity())
            return None

        if PositionSide.from_side(fill.side) is pos.side:
            # 同向加仓：系统默认禁止（风控层拦截），此处保留能力以防手动干预
            total = pos.qty + fill.qty
            pos.entry_price = (pos.entry_price * pos.qty + fill.price * fill.qty) / total
            pos.qty = total
            pos.entry_fee += fill.fee
            self.cash -= fill.fee
            self.total_fees += fill.fee
            self.peak_equity = max(self.peak_equity, self.equity())
            return None

        close_qty = min(fill.qty, pos.qty)
        sign = pos.side.sign
        gross = (fill.price - pos.entry_price) * close_qty * sign
        entry_fee_share = pos.entry_fee * (close_qty / pos.qty) if pos.qty > 0 else 0.0
        exit_fee_share = fill.fee * (close_qty / fill.qty) if fill.qty > 0 else 0.0
        fees = entry_fee_share + exit_fee_share

        self.cash += gross - exit_fee_share
        self.total_fees += exit_fee_share
        self.realized_pnl += gross

        remaining = pos.qty - close_qty
        trade: TradeRecord | None = None
        if remaining <= pos.qty * 1e-9:
            risk_per_unit = abs(pos.entry_price - pos.initial_stop) if pos.initial_stop else 0.0
            risk = risk_per_unit * close_qty
            trade = TradeRecord(
                symbol=symbol,
                side=pos.side,
                qty=close_qty,
                entry_price=pos.entry_price,
                exit_price=fill.price,
                entry_ts=pos.entry_ts,
                exit_ts=fill.ts,
                pnl=gross - fees,
                fees=fees,
                r_multiple=(gross - fees) / risk if risk > 0 else 0.0,
                reason=reason,
                bars_held=pos.bars_held,
                entry_reason=str(pos.tags.get("entry_reason", "")),
            )
            self.trades.append(trade)
            self.positions.pop(symbol, None)
        else:
            pos.qty = remaining
            pos.entry_fee = max(0.0, pos.entry_fee - entry_fee_share)

        # 反向成交超出持仓 → 溢出部分开新仓（反手）
        overflow = fill.qty - close_qty
        if overflow > 1e-12:
            self._open(
                Fill(
                    ts=fill.ts,
                    order_id=fill.order_id,
                    symbol=symbol,
                    side=fill.side,
                    qty=overflow,
                    price=fill.price,
                    fee=0.0,
                    is_maker=fill.is_maker,
                )
            )

        self.peak_equity = max(self.peak_equity, self.equity())
        return trade

    def _open(self, fill: Fill) -> None:
        self.positions[fill.symbol] = Position(
            symbol=fill.symbol,
            side=PositionSide.from_side(fill.side),
            qty=fill.qty,
            entry_price=fill.price,
            entry_ts=fill.ts,
            stop_price=0.0,
            take_profit_price=0.0,
            initial_stop=0.0,
            entry_fee=fill.fee,
            entry_order_id=fill.order_id,
        )
        self.cash -= fill.fee
        self.total_fees += fill.fee
        self.mark(fill.symbol, fill.price)

    # ------------------------------------------------------------------ #
    # 保护单 / 移动止损
    # ------------------------------------------------------------------ #
    def attach_protection(self, symbol: str, stop_price: float, take_profit: float) -> None:
        pos = self.positions.get(symbol)
        if pos is None:
            return
        pos.stop_price = stop_price
        pos.take_profit_price = take_profit
        if pos.initial_stop == 0.0:
            pos.initial_stop = stop_price

    def update_stop(self, symbol: str, new_stop: float) -> bool:
        """移动止损只允许朝有利方向推进（单调收紧）。"""
        pos = self.positions.get(symbol)
        if pos is None:
            return False
        if pos.side is PositionSide.LONG:
            if new_stop <= pos.stop_price:
                return False
        else:
            if new_stop >= pos.stop_price:
                return False
        pos.stop_price = new_stop
        return True

    def close_all(self, ts: int, prices: dict[str, float], reason: ExitReason) -> list[TradeRecord]:
        """以给定价格强制平掉全部持仓（回测收尾 / kill switch）。"""
        out: list[TradeRecord] = []
        for sym, pos in list(self.positions.items()):
            px = prices.get(sym, pos.entry_price)
            trade = self.on_fill(
                Fill(
                    ts=ts,
                    order_id="force_close",
                    symbol=sym,
                    side=pos.side.exit_side,
                    qty=pos.qty,
                    price=px,
                    fee=abs(px * pos.qty) * 0.0004,
                    is_maker=False,
                ),
                reason=reason,
            )
            if trade is not None:
                out.append(trade)
        return out

    # ------------------------------------------------------------------ #
    # 快照
    # ------------------------------------------------------------------ #
    def snapshot(self, prices: dict[str, float] | None = None) -> dict[str, float]:
        prices = prices or self._last_prices
        return {
            "equity": self.equity(prices),
            "cash": self.cash,
            "unrealized": self.unrealized(prices),
            "exposure": self.exposure(prices),
            "drawdown": self.drawdown(prices),
            "open_positions": float(self.open_positions),
            "realized_pnl": self.realized_pnl,
            "total_fees": self.total_fees,
            "trades": float(len(self.trades)),
        }
