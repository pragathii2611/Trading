from datetime import datetime, timedelta

import pytest

from app.models import IST, Position
from app.risk import RiskManager

MON_11AM = datetime(2026, 10, 5, 11, 0, tzinfo=IST)


@pytest.fixture
def risk(settings):
    settings.enforce_market_hours = True
    return RiskManager(settings)


def pos(side=1, entry=100.0, risk=None, opened=MON_11AM):
    stop, target = risk.initial_stops(entry, side)
    return Position("AAA", 10, entry, stop, target, opened, side=side)


def test_position_size_respects_risk_cap_and_cash(risk):
    # 1% of 100k = 1000 risk; 1.5% stop on 100 = 1.5/share -> 666; capped at 20% = 200 shares
    assert risk.position_size(100_000, 100_000, 100) == 200
    assert risk.position_size(100_000, 5_000, 100) == 49  # limited by cash
    assert risk.position_size(100_000, 100_000, 0) == 0


def test_hard_stop_loss(risk):
    p = pos(risk=risk)
    assert risk.exit_reason(p, 99.0, 0.5, MON_11AM) is None
    assert "stop-loss" in risk.exit_reason(p, 98.4, 0.5, MON_11AM)


def test_breakeven_then_trailing_stop_locks_profit(risk):
    p = pos(risk=risk)
    risk.exit_reason(p, 101.2, 0.5, MON_11AM)  # +1.2% -> breakeven + trail
    assert p.stop_price >= 100.1
    risk.exit_reason(p, 102.5, 0.5, MON_11AM)  # new peak -> trail to ~101.48
    assert p.stop_price == pytest.approx(101.47, abs=0.02)
    reason = risk.exit_reason(p, 101.4, 0.5, MON_11AM)
    assert reason and "trailing" in reason
    # stop never moves down
    before = p.stop_price
    risk.update_trailing(p, 95)
    assert p.stop_price == before


def test_take_profit(risk):
    assert "take-profit" in risk.exit_reason(pos(risk=risk), 103.1, 0.5, MON_11AM)


def test_early_exit_when_losing_and_signal_turns(risk):
    p = pos(risk=risk)
    assert risk.exit_reason(p, 99.5, 0.2, MON_11AM) is None
    assert "early exit" in risk.exit_reason(p, 99.5, -0.3, MON_11AM)
    # a winning trade is not cut on a weak signal
    assert risk.exit_reason(pos(risk=risk), 100.5, -0.3, MON_11AM) is None


def test_stale_trade_and_square_off(risk):
    p = pos(risk=risk)
    assert "stale" in risk.exit_reason(p, 100.1, 0.1, MON_11AM + timedelta(minutes=121))
    p2 = pos(risk=risk, opened=MON_11AM.replace(hour=15, minute=0))
    assert "square-off" in risk.exit_reason(p2, 100.5, 0.1, MON_11AM.replace(hour=15, minute=16))


def test_short_position_rules(risk):
    p = pos(side=-1, risk=risk)
    assert p.stop_price == 101.5 and p.target_price == 97.0
    assert "stop-loss" in risk.exit_reason(p, 101.6, -0.5, MON_11AM)
    p = pos(side=-1, risk=risk)
    risk.exit_reason(p, 98.5, -0.5, MON_11AM)  # +1.5% in favour
    assert p.stop_price <= 99.9
    assert "trailing" in risk.exit_reason(p, 99.6, -0.5, MON_11AM)


def test_day_halt_on_loss_and_profit_giveback(risk):
    assert risk.day_halt_reason(-1000, 0, 100_000) is None
    assert "loss limit" in risk.day_halt_reason(-2000, 0, 100_000)
    assert risk.day_halt_reason(1000, 1000, 100_000) is None          # target not reached yet
    assert risk.day_halt_reason(1400, 2000, 100_000) is None          # gave back 30%
    assert "profit lock" in risk.day_halt_reason(900, 2000, 100_000)  # gave back 55%


def test_entry_blocks(risk):
    now = MON_11AM
    assert risk.entry_block_reason("AAA", now, {}, {}, None) is None
    assert "halted" in risk.entry_block_reason("AAA", now, {}, {}, "loss")
    assert risk.entry_block_reason("AAA", now.replace(hour=14, minute=50), {}, {}, None).startswith("no new")
    assert risk.entry_block_reason("AAA", now + timedelta(days=5), {}, {}, None) == "market closed"  # Saturday
    assert "cooldown" in risk.entry_block_reason("AAA", now, {}, {"AAA": now - timedelta(minutes=5)}, None)
    full = {f"S{i}": None for i in range(5)}
    assert risk.entry_block_reason("AAA", now, full, {}, None) == "max open positions"
