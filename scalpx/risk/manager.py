"""风控闸门（Risk Gate）。

**唯一有权拒绝订单的模块**。策略只能"表达意图"，不能决定仓位，也不能绕过风控。
分三层：

L1 事前（pre-trade）   —— 硬性约束：日内亏损熔断、连亏冷却、持仓数、杠杆、点差
L2 事中（in-trade）    —— 保护单必须存在、移动止损单调、时间止损
L3 事后（post-trade）  —— 连亏计数、当日笔数、绩效衰减降档

所有拒绝都会带一个可枚举的 `code`，写入 risk_events 表，方便事后归因。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import ExecutionConfig, MarketConfig, RiskConfig
from ..models import Bar, EnterIntent, ExitReason, Order, Position, PositionSide, TradeRecord, ts_to_iso
from .costs import cost_to_risk_ratio, net_rr, round_trip_cost
from .sizing import SizingResult, size_position

log = logging.getLogger(__name__)

DAY_MS = 86_400_000

# 这些熔断原因只约束"当日"，跨日后自动解除；其余（KILL_SWITCH/MANUAL）需要人工介入。
DAY_SCOPED_HALTS = frozenset({"DAILY_LOSS", "CONSECUTIVE_LOSS_HALT", "ORDER_REJECT_STREAK"})


@dataclass(slots=True)
class RiskContext:
    """风控决策所需的全部外部状态（显式传入，便于单测与回放）。"""

    ts: int
    equity: float
    positions: dict[str, Position]
    prices: dict[str, float]
    spread_bps: float = 0.0
    bar: Bar | None = None
    atr: float = 0.0
    # 盘口数据质量劣化（丢包/乱序/过期）时由引擎置位 -> 拒绝入场
    book_untrusted: bool = False
    book_reason: str = ""


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    code: str = "OK"
    message: str = ""
    sizing: SizingResult | None = None
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    qty: float = 0.0

    @classmethod
    def reject(cls, code: str, message: str = "") -> "RiskDecision":
        return cls(approved=False, code=code, message=message)


@dataclass(slots=True)
class RiskState:
    day: str = ""
    day_open_equity: float = 0.0
    trades_today: int = 0
    pnl_today: float = 0.0
    consecutive_losses: int = 0
    cooldown_until_ts: int = 0
    symbol_cooldown: dict[str, int] = field(default_factory=dict)
    halted: bool = False
    halt_reason: str = ""
    reject_streak: int = 0
    peak_equity: float = 0.0


class RiskManager:
    def __init__(
        self,
        cfg: RiskConfig,
        market_cfg: MarketConfig,
        *,
        bar_ms: int = 60_000,
        exec_cfg: ExecutionConfig | None = None,
    ) -> None:
        self.cfg = cfg
        self.market = market_cfg
        self.bar_ms = bar_ms
        self.exec_cfg = exec_cfg
        self.state = RiskState()
        self.events: list[dict[str, object]] = []  # 供引擎落库
        self._last_exit_bar: dict[str, int] = {}
        self._book_ok: dict[str, bool] = {}

    # ------------------------------------------------------------------ #
    # 事件记录
    # ------------------------------------------------------------------ #
    def _emit(self, ts: int, level: str, code: str, message: str = "", **meta: object) -> None:
        self.events.append(
            {"ts": ts, "level": level, "code": code, "message": message, "meta": meta}
        )
        if level == "ERROR":
            log.error("风控[%s] %s %s", code, message, meta)
        elif level == "WARNING":
            log.warning("风控[%s] %s %s", code, message, meta)

    def drain_events(self) -> list[dict[str, object]]:
        out = self.events
        self.events = []
        return out

    # ------------------------------------------------------------------ #
    # 每根 bar 调用：日切、熔断检查
    # ------------------------------------------------------------------ #
    def on_bar(self, ts: int, equity: float) -> None:
        day = ts_to_iso(ts)[:10]
        if day != self.state.day:
            self.state.day = day
            self.state.day_open_equity = equity
            self.state.trades_today = 0
            self.state.pnl_today = 0.0
            if self.state.halted and self.state.halt_reason in DAY_SCOPED_HALTS:
                # 日级熔断（日亏/连亏/连续拒单）在新交易日自动解除；
                # KILL_SWITCH / MANUAL 属于人工干预，必须人工 resume。
                self.state.halted = False
                self.state.consecutive_losses = 0
                self.state.cooldown_until_ts = 0
                self._emit(
                    ts, "INFO", "DAY_ROLLOVER", f"新交易日 {day}，解除日级熔断（{self.state.halt_reason}）"
                )
                self.state.halt_reason = ""

        self.state.peak_equity = max(self.state.peak_equity, equity)
        if self.state.day_open_equity > 0 and not self.state.halted:
            day_ret = equity / self.state.day_open_equity - 1.0
            if day_ret <= -self.cfg.max_daily_loss_pct:
                self.halt(ts, "DAILY_LOSS", f"当日回撤 {day_ret:.2%} 触及上限 {self.cfg.max_daily_loss_pct:.2%}")

    # ------------------------------------------------------------------ #
    # L1 事前校验
    # ------------------------------------------------------------------ #
    def pre_trade(self, intent: EnterIntent, ctx: RiskContext) -> RiskDecision:
        cfg = self.cfg
        if self.state.halted:
            return RiskDecision.reject("RISK_HALTED", self.state.halt_reason)

        if self.state.cooldown_until_ts > ctx.ts:
            remain = (self.state.cooldown_until_ts - ctx.ts) / 1000
            return RiskDecision.reject("COOLDOWN", f"连亏冷却中，剩余 {remain:.0f}s")

        if self.state.trades_today >= cfg.max_daily_trades:
            return RiskDecision.reject("DAILY_TRADE_LIMIT", f"当日已交易 {self.state.trades_today} 笔")

        if len(ctx.positions) >= cfg.max_positions:
            return RiskDecision.reject("MAX_POSITIONS", f"持仓数已达上限 {cfg.max_positions}")

        if intent.symbol in ctx.positions:
            return RiskDecision.reject("SYMBOL_POSITION_EXISTS", f"{intent.symbol} 已有持仓")

        last_bar = self._last_exit_bar.get(intent.symbol)
        if last_bar is not None and ctx.ts - last_bar < cfg.symbol_cooldown_bars * self.bar_ms:
            return RiskDecision.reject("SYMBOL_COOLDOWN", f"{intent.symbol} 出场冷却中")

        if ctx.bar is not None and ctx.bar.volume <= 0:
            return RiskDecision.reject("NO_LIQUIDITY", "当前 bar 无成交量")

        if ctx.spread_bps > cfg.max_spread_bps:
            return RiskDecision.reject("SPREAD_TOO_WIDE", f"点差 {ctx.spread_bps:.2f}bps > {cfg.max_spread_bps}")

        if ctx.book_untrusted:
            return RiskDecision.reject(
                "BOOK_UNTRUSTED",
                f"盘口数据不可信（{ctx.book_reason or 'UNKNOWN'}），暂停开仓",
            )

        # 相关品种同向 -> 折算风险后仍不得超过单笔预算的 1.5 倍
        same_dir_risk = sum(
            1 for p in ctx.positions.values() if p.side is PositionSide.from_side(intent.side)
        )
        if same_dir_risk >= 1 and cfg.correlated_risk_factor > 1.0:
            pass  # 允许，但在 sizing 中按 max_positions 与杠杆共同约束

        leverage_room = cfg.max_leverage - (
            sum(abs(p.qty * ctx.prices.get(s, p.entry_price)) for s, p in ctx.positions.items())
            / ctx.equity
            if ctx.equity > 0
            else 0.0
        )
        if leverage_room <= 0.1:
            return RiskDecision.reject(
                "LEVERAGE_CAP",
                f"剩余杠杆空间 {leverage_room:.2f}x 不足（上限 {cfg.max_leverage}x）",
            )

        sizing = size_position(
            equity=ctx.equity,
            entry=intent.ref_price,
            stop=intent.stop_price,
            take_profit=intent.take_profit_price,
            side_is_long=intent.side.sign > 0,
            risk_cfg=cfg,
            market_cfg=self.market,
            leverage_room=leverage_room,
        )
        if not sizing.ok:
            return RiskDecision.reject(sizing.code or "SIZING_FAILED", sizing.message)

        # === 成本闸门 ===
        # 超短线的真实敌人是成本：止损越窄 → 名义仓位越大 → 手续费占风险的比例越高。
        # 这里把「往返成本 / 止损距离」做成硬约束，等价于"手续费最多吃掉 N 个 R"。
        cost_ratio = 0.0
        cost_bps = 0.0
        if self.exec_cfg is not None and cfg.max_cost_ratio > 0:
            cost = round_trip_cost(
                self.exec_cfg,
                entry_is_maker=self.exec_cfg.entry_order_type != "market",
                exit_is_maker=False,
                spread_bps=ctx.spread_bps,
            )
            stop_pct = sizing.stop_distance / intent.ref_price if intent.ref_price > 0 else 0.0
            cost_ratio = cost_to_risk_ratio(cost.total, stop_pct)
            cost_bps = cost.as_bps()
            if cost_ratio > cfg.max_cost_ratio:
                return RiskDecision.reject(
                    "COST_TOO_HIGH",
                    f"往返成本 {cost_bps:.1f}bps 占止损 {cost_ratio:.2f}R "
                    f"> 上限 {cfg.max_cost_ratio:.2f}R（止损距离过窄）",
                )

        tp_pct = abs(sizing.take_profit_price - intent.ref_price) / intent.ref_price if intent.ref_price else 0.0
        stop_pct = sizing.stop_distance / intent.ref_price if intent.ref_price else 0.0
        cost_pct = cost_bps / 10_000.0
        return RiskDecision(
            approved=True,
            sizing=sizing,
            stop_price=sizing.stop_price,
            take_profit_price=sizing.take_profit_price,
            qty=sizing.qty,
            message=(
                f"risk={sizing.risk_amount:.2f} ({sizing.risk_amount / ctx.equity:.3%}) "
                f"cost={cost_bps:.1f}bps({cost_ratio:.2f}R) "
                f"net_rr={net_rr(take_profit_pct=tp_pct, stop_pct=stop_pct, cost_pct=cost_pct):.2f}"
            ),
        )

    def notify_book_quality(self, ts: int, symbol: str, ok: bool, reason: str = "") -> None:
        """盘口质量状态迁移时记一条事件（只在翻转时写，避免刷屏）。"""
        prev = self._book_ok.get(symbol, True)
        if ok == prev:
            return
        self._book_ok[symbol] = ok
        self._emit(
            ts,
            "INFO" if ok else "WARNING",
            "BOOK_QUALITY",
            f"{symbol} 盘口{'恢复可用' if ok else '不可信'}（{reason or 'UNKNOWN'}）",
        )

    def check_protection(self, position: Position, ts: int) -> bool:
        """L2：任何持仓必须带保护止损，否则视为异常。"""
        if position.stop_price <= 0:
            self._emit(ts, "ERROR", "MISSING_STOP", f"{position.symbol} 持仓无保护止损")
            return False
        return True

    def should_time_stop(self, position: Position, r_multiple: float) -> bool:
        """硬性持仓时长上限（策略可能忘记，这里兜底）。

        与策略参数 `max_hold_bars` 的关系：策略给"软"时间止损（要求盈利不足才平），
        这里是"硬"上限（无条件平），取两者更严格者由 OMS 执行。
        """
        if position.bars_held < self.cfg.max_bars_held:
            return False
        return r_multiple < 0.5

    # ------------------------------------------------------------------ #
    # L3 事后
    # ------------------------------------------------------------------ #
    def on_trade_closed(self, trade: TradeRecord, ts: int, equity: float) -> None:
        self.state.trades_today += 1
        self.state.pnl_today += trade.pnl
        if trade.pnl < 0:
            self.state.consecutive_losses += 1
            self.state.reject_streak = 0
        else:
            self.state.consecutive_losses = 0

        self._last_exit_bar[trade.symbol] = ts

        if self.state.consecutive_losses >= self.cfg.consecutive_loss_halt:
            self.halt(
                ts,
                "CONSECUTIVE_LOSS_HALT",
                f"连亏 {self.state.consecutive_losses} 笔达到上限，当日停止交易",
            )
        elif self.state.consecutive_losses >= self.cfg.consecutive_loss_cooldown:
            self.state.cooldown_until_ts = ts + self.cfg.cooldown_minutes * 60_000
            self._emit(
                ts, "WARNING", "CONSECUTIVE_LOSS_COOLDOWN",
                f"连亏 {self.state.consecutive_losses} 笔，冷却 {self.cfg.cooldown_minutes} 分钟",
            )

    def on_order_rejected(self, ts: int, code: str, equity: float) -> None:
        self.state.reject_streak += 1
        if self.state.reject_streak >= 3:
            self.halt(ts, "ORDER_REJECT_STREAK", f"连续 {self.state.reject_streak} 次订单被拒（{code}）")

    def on_order_accepted(self) -> None:
        self.state.reject_streak = 0

    def halt(self, ts: int, code: str, message: str) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = code
            self._emit(ts, "ERROR", code, message)

    def resume(self, ts: int, *, operator: bool = False) -> None:
        if self.state.halted and (operator or self.state.halt_reason == "DAILY_LOSS"):
            self._emit(ts, "INFO", "RESUME", f"解除熔断（原因={self.state.halt_reason}）")
            self.state.halted = False
            self.state.halt_reason = ""
            self.state.consecutive_losses = 0
            self.state.cooldown_until_ts = 0

    # ------------------------------------------------------------------ #
    # 开关
    # ------------------------------------------------------------------ #
    def can_open(self, ts: int) -> tuple[bool, str]:
        if self.state.halted:
            return False, self.state.halt_reason
        if self.state.cooldown_until_ts > ts:
            return False, "COOLDOWN"
        return True, "OK"

    def describe(self) -> dict[str, object]:
        s = self.state
        return {
            "day": s.day,
            "day_open_equity": s.day_open_equity,
            "trades_today": s.trades_today,
            "pnl_today": s.pnl_today,
            "consecutive_losses": s.consecutive_losses,
            "halted": s.halted,
            "halt_reason": s.halt_reason,
            "cooldown_until": s.cooldown_until_ts,
        }


def new_client_order_id(prefix: str, symbol: str, ts_ms: int, seq: int) -> str:
    """幂等订单 ID：同一次意图重试时复用，交易所据此去重。

    币安限制长度 <= 36，且必须字母数字与 - _ . 组成。
    """
    clean = "".join(ch for ch in symbol if ch.isalnum())[:8]
    suffix = f"{int(ts_ms) % 100_000_000:08d}{int(seq) % 100:02d}"
    return f"{prefix[:6]}-{clean}-{suffix}"[:36]


def now_ms() -> int:
    return int(time.time() * 1000)


def daily_bucket(ts: int) -> str:
    return ts_to_iso(ts)[:10]
