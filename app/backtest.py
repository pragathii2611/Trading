"""Bar-by-bar backtest using the same strategy and exit rules as the live bot."""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from .brokers import zerodha_charges
from .config import Settings
from .models import Position
from .risk import RiskManager
from .strategies import MIN_BARS, make_strategy


def run_backtest(df: pd.DataFrame, s: Settings, strategy: str | None = None, capital: float | None = None) -> dict:
    s = s.model_copy(update={"enforce_market_hours": False})
    risk = RiskManager(s)
    strat = make_strategy(strategy or s.strategy, s.buy_threshold, s.sell_threshold)
    cash = equity = capital or s.bot_capital
    pos: Position | None = None
    trades, curve = [], []
    cooldown_until = -1
    bar_minutes = 5
    if len(df) > 1:
        bar_minutes = max(1, int((df.index[1] - df.index[0]).total_seconds() // 60))

    def close(i, price, reason):
        nonlocal cash, pos
        value = price * pos.qty
        fee = zerodha_charges("MIS", "SELL", value)
        pnl = (price - pos.entry_price) * pos.qty - fee - pos_entry_fee
        cash += value - fee
        trades.append({"entry_time": str(pos.opened_at), "exit_time": str(df.index[i]), "qty": pos.qty,
                       "entry": round(pos.entry_price, 2), "exit": round(price, 2), "pnl": round(pnl, 2),
                       "reason": reason})
        pos = None

    pending_buy = False
    pos_entry_fee = 0.0
    for i in range(MIN_BARS, len(df)):
        bar = df.iloc[i]
        ts = df.index[i]
        if pending_buy and pos is None:
            price = float(bar.open)
            qty = risk.position_size(equity, cash, price)
            if qty >= 1:
                stop, target = risk.initial_stops(price)
                pos_entry_fee = zerodha_charges("MIS", "BUY", price * qty)
                cash -= price * qty + pos_entry_fee
                pos = Position("BT", qty, price, stop, target, ts)
        pending_buy = False

        if pos is not None:
            # Conservative: assume the stop is hit before the target within a bar.
            if bar.low <= pos.stop_price:
                close(i, min(float(bar.open), pos.stop_price), "stop")
            elif bar.high >= pos.target_price:
                close(i, max(float(bar.open), pos.target_price), "target")
            else:
                sig = strat.evaluate(df.iloc[: i + 1])
                now = ts if isinstance(ts, pd.Timestamp) else pos.opened_at + timedelta(minutes=bar_minutes * i)
                risk.update_trailing(pos, float(bar.high))
                reason = risk.exit_reason(pos, float(bar.close), sig.score, now)
                if reason is None and sig.action == "SELL":
                    reason = "sell signal"
                if reason:
                    close(i, float(bar.close), reason)
            if pos is None:
                cooldown_until = i + max(1, s.cooldown_minutes // bar_minutes)
        elif i > cooldown_until:
            if strat.evaluate(df.iloc[: i + 1]).action == "BUY":
                pending_buy = True

        equity = cash + (pos.qty * float(bar.close) if pos else 0.0)
        curve.append({"time": str(ts), "equity": round(equity, 2)})

    if pos is not None:
        close(len(df) - 1, float(df.iloc[-1].close), "end of data")
        curve[-1]["equity"] = round(cash, 2)

    start = capital or s.bot_capital
    eq = pd.Series([c["equity"] for c in curve]) if curve else pd.Series([start])
    dd = ((eq - eq.cummax()) / eq.cummax()).min() * 100
    wins = [t for t in trades if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    return {
        "strategy": strat.name,
        "bars": len(df),
        "start_capital": start,
        "end_equity": round(float(eq.iloc[-1]), 2),
        "return_pct": round((float(eq.iloc[-1]) / start - 1) * 100, 2),
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(float(dd), 2),
        "trade_list": trades[-200:],
        "equity_curve": curve[:: max(1, len(curve) // 500)],
    }
