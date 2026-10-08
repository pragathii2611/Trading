import numpy as np
from fastapi.testclient import TestClient

from app.brokers import OrderRequest
from app.engine import Engine
from app.main import create_app

from .conftest import FakeProvider


def engine_with(settings, prices, closes=None):
    e = Engine(settings)
    e.data = FakeProvider(prices, closes)
    e.step(force_strategy=True)
    return e


def test_auto_protect_cuts_a_losing_manual_trade(settings):
    e = engine_with(settings, {"AAA": 100.0})
    e.place_manual(OrderRequest("AAA", "BUY", 10), auto_protect=True)
    assert "NSE:AAA:MIS" in e.managed
    e.data.prices["AAA"] = 98.0  # below the 1.5% stop
    e.step()
    assert e.broker.net_qty("AAA", "NSE", "MIS") == 0
    assert "NSE:AAA:MIS" not in e.managed
    assert any("stop-loss" in ev["message"] for ev in e.events)


def test_bot_buys_on_signal_and_halts_on_daily_loss(settings):
    base = np.concatenate([np.full(100, 88.0), 88 + np.linspace(0, 1, 50) ** 2 * 12])
    up = list(base + np.random.default_rng(1).normal(0, 0.2, 150))
    e = engine_with(settings, {"AAA": 100.0}, {"AAA": up})
    e.start_bot()
    e.step(force_strategy=True)
    assert e.broker.net_qty("AAA", "NSE", "MIS") > 0, e.signals
    # simulate earlier losses today pushing the day beyond the 2% loss limit
    e.broker.realized_today = -2_500
    e.step()
    assert e.halted and "loss limit" in e.halted
    assert e.broker.net_qty("AAA", "NSE", "MIS") == 0  # bot position closed


def test_kill_switch_flattens_everything(settings):
    e = engine_with(settings, {"AAA": 100.0})
    e.place_manual(OrderRequest("AAA", "SELL", 5))
    e.place_manual(OrderRequest("AAA", "BUY", 5, order_type="LIMIT", price=50))
    e.kill()
    assert e.broker.net_qty("AAA", "NSE", "MIS") == 0
    assert all(o["status"] not in ("OPEN", "TRIGGER PENDING") for o in e.broker.orders())
    try:
        e.place_manual(OrderRequest("AAA", "BUY", 1))
        raise AssertionError("orders must be blocked while killed")
    except Exception as ex:
        assert "Kill switch" in str(ex)


def test_state_survives_restart(settings):
    e = engine_with(settings, {"AAA": 100.0})
    e.add_funds(25_000)
    e.place_manual(OrderRequest("AAA", "BUY", 10), auto_protect=True)
    e2 = Engine(settings)
    assert e2.broker.net_qty("AAA", "NSE", "MIS") == 10
    assert "NSE:AAA:MIS" in e2.managed
    assert e2.broker.cash > 120_000


def test_api_flow(settings):
    e = engine_with(settings, {"AAA": 100.0})
    with TestClient(create_app(e, start_loop=False)) as c:
        assert c.get("/api/status").json()["mode"] == "paper"
        r = c.post("/api/orders", json={"symbol": "aaa", "side": "BUY", "qty": 3, "auto_protect": True})
        assert r.status_code == 200 and r.json()["status"] == "COMPLETE"
        assert c.post("/api/orders", json={"symbol": "AAA", "side": "BUY", "qty": 1, "order_type": "LIMIT"}).status_code == 400
        assert c.post("/api/funds/add", json={"amount": 1000}).status_code == 200
        assert c.patch("/api/settings", json={"stop_loss_pct": 1.0}).json()["stop_loss_pct"] == 1.0
        assert c.patch("/api/settings", json={"trading_mode": "live"}).status_code == 400
        assert c.post("/api/positions/exit", json={"symbol": "AAA"}).json()["status"] == "COMPLETE"
        assert len(c.get("/api/trades").json()) == 2
        assert c.get("/").status_code == 200


def test_password_protection(settings):
    settings.app_password = "s3cret"
    e = engine_with(settings, {"AAA": 100.0})
    with TestClient(create_app(e, start_loop=False)) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", auth=("me", "wrong")).status_code == 401
        assert c.get("/api/status", auth=("me", "s3cret")).status_code == 200
        assert c.get("/api/status").status_code == 200  # session cookie remembered
