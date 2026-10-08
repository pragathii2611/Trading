"""Risk management: position sizing, entry checks, and loss-cutting exits."""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta

from .config import Settings
from .models import Position

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def _t(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


class RiskManager:
    def __init__(self, s: Settings):
        self.s = s

    # ---- clock -----------------------------------------------------------
    def market_open(self, now: datetime) -> bool:
        if not self.s.enforce_market_hours:
            return True
        return now.weekday() < 5 and MARKET_OPEN <= now.time() < MARKET_CLOSE

    def past_entry_cutoff(self, now: datetime) -> bool:
        return self.s.enforce_market_hours and now.time() >= _t(self.s.no_new_entries_after)

    def past_square_off(self, now: datetime) -> bool:
        return (
            self.s.enforce_market_hours
            and self.s.product == "MIS"
            and now.time() >= _t(self.s.square_off_time)
        )

    # ---- sizing ----------------------------------------------------------
    def position_size(self, equity: float, cash: float, price: float) -> int:
        """Size so that hitting the stop loses ~risk_per_trade_pct of equity."""
        if price <= 0:
            return 0
        risk_amount = equity * self.s.risk_per_trade_pct / 100
        per_share_risk = price * self.s.stop_loss_pct / 100
        by_risk = risk_amount / per_share_risk
        by_cap = equity * self.s.max_position_pct / 100 / price
        by_cash = cash * 0.98 / price  # leave room for charges
        return max(0, math.floor(min(by_risk, by_cap, by_cash)))

    def initial_stops(
        self, price: float, side: int = 1, stop_pct: float | None = None, target_pct: float | None = None
    ) -> tuple[float, float]:
        sl = self.s.stop_loss_pct if stop_pct is None else stop_pct
        tp = self.s.take_profit_pct if target_pct is None else target_pct
        return (
            round(price * (1 - side * sl / 100), 2),
            round(price * (1 + side * tp / 100), 2),
        )

    # ---- day-level guard -------------------------------------------------
    def day_halt_reason(self, day_pnl: float, peak_day_pnl: float, start_equity: float) -> str | None:
        """Stop trading for the day on a loss limit or after giving back profits."""
        if start_equity <= 0:
            return None
        if day_pnl <= -start_equity * self.s.daily_loss_limit_pct / 100:
            return f"daily loss limit hit ({day_pnl:,.0f})"
        target = start_equity * self.s.daily_profit_target_pct / 100
        if peak_day_pnl >= target:
            floor = peak_day_pnl * (1 - self.s.profit_giveback_pct / 100)
            if day_pnl <= floor:
                return f"profit lock: day P&L fell from peak {peak_day_pnl:,.0f} to {day_pnl:,.0f}"
        return None

    # ---- entries ---------------------------------------------------------
    def entry_block_reason(
        self,
        symbol: str,
        now: datetime,
        positions: dict[str, Position],
        last_exit: dict[str, datetime],
        halted: str | None,
    ) -> str | None:
        if halted:
            return f"halted: {halted}"
        if not self.market_open(now):
            return "market closed"
        if self.past_entry_cutoff(now):
            return f"no new entries after {self.s.no_new_entries_after}"
        if symbol in positions:
            return "already holding"
        if len(positions) >= self.s.max_open_positions:
            return "max open positions"
        ex = last_exit.get(symbol)
        if ex and now - ex < timedelta(minutes=self.s.cooldown_minutes):
            return "cooldown after recent exit"
        return None

    # ---- exits -----------------------------------------------------------
    def update_trailing(self, pos: Position, price: float) -> None:
        """Ratchet the stop in the trade's favour: breakeven first, then trail the
        best price. The stop never moves against the trade."""
        side = pos.side
        pos.peak_price = max(pos.peak_price, price) if side == 1 else min(pos.peak_price, price)
        if pos.gain_pct(pos.peak_price) >= self.s.breakeven_trigger_pct:
            # Breakeven slightly beyond entry so charges are covered.
            be = pos.entry_price * (1 + side * 0.001)
            trail = pos.peak_price * (1 - side * self.s.trailing_stop_pct / 100)
            pick = max if side == 1 else min
            pos.stop_price = round(pick(pos.stop_price, be, trail), 2)

    def exit_reason(self, pos: Position, price: float, score: float | None, now: datetime) -> str | None:
        self.update_trailing(pos, price)
        side = pos.side
        if (price - pos.stop_price) * side <= 0:
            if (pos.stop_price - pos.entry_price) * side >= 0:
                return f"trailing/breakeven stop {pos.stop_price}"
            return f"stop-loss {pos.stop_price}"
        if (price - pos.target_price) * side >= 0:
            return f"take-profit {pos.target_price}"
        gain = pos.gain_pct(price)
        bearish = score is not None and score * side <= self.s.weak_signal_threshold
        if self.s.early_exit_on_weak_signal and gain < 0 and bearish:
            return f"early exit: losing and signal turned against trade ({score:+.2f})"
        if (
            self.s.max_hold_minutes
            and now - pos.opened_at >= timedelta(minutes=self.s.max_hold_minutes)
            and gain <= 0.2
        ):
            return f"stale trade after {self.s.max_hold_minutes} min"
        if pos.product == "MIS" and self.past_square_off(now):
            return f"intraday square-off {self.s.square_off_time}"
        return None
