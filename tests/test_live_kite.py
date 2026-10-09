"""Live mode against a fake Zerodha Kite that mimics the real API's shapes and rules."""
import itertools
from datetime import datetime, timedelta

import numpy as np
import pytest
from kiteconnect import exceptions as kex

from app.brokers import OrderRequest
from app.config import LIVE_CONFIRM_PHRASE, Settings
from app.engine import Engine
from app.models import IST

MON_11AM = datetime(2026, 10, 5, 11, 0, tzinfo=IST)


class FakeKite:
    VARIETY_REGULAR = "regular"
    instances: list["FakeKite"] = []

    def __init__(self, api_key, proxies=None, **_):
        self.api_key, self.proxies = api_key, proxies
        self.token = None
        self.expired = False
        self.prices = {"NSE:AAA": 100.0}
        self.cash = 100_000.0
        self.orders_: dict[str, list[dict]] = {}
        self.trades_: list[dict] = []
        self.pos: dict[tuple, dict] = {}
        self.ids = itertools.count(1)
        self.placed: list[dict] = []
        FakeKite.instances.append(self)

    # --- session ---
    def login_url(self):
        return f"https://kite.zerodha.com/connect/login?api_key={self.api_key}&v=3"

    def generate_session(self, request_token, api_secret):
        assert request_token == "req-tok" and api_secret == "secret"
        return {"access_token": "acc-tok"}

    def set_access_token(self, t):
        self.token = self.access_token = t

    def _auth(self):
        if self.expired or not self.token:
            raise kex.TokenException("Incorrect `api_key` or `access_token`.")

    # --- market data ---
    def instruments(self, exchange):
        return [{"tradingsymbol": "AAA", "instrument_token": 123}]

    def historical_data(self, token, frm, to, interval):
        self._auth()
        closes = np.concatenate([np.full(100, 88.0), 88 + np.linspace(0, 1, 50) ** 2 * 12])
        closes = closes + np.random.default_rng(1).normal(0, 0.2, 150)
        t0 = datetime(2026, 10, 5, 9, 15, tzinfo=IST)
        return [{"date": t0 + timedelta(minutes=5 * i), "open": c, "high": c * 1.002, "low": c * 0.998,
                 "close": c, "volume": 1000} for i, c in enumerate(closes)]

    def ltp(self, *instruments):
        self._auth()
        ins = instruments[0] if instruments and isinstance(instruments[0], list) else list(instruments)
        return {i: {"instrument_token": 123, "last_price": self.prices[i]} for i in ins if i in self.prices}

    # --- orders ---
    def place_order(self, variety, exchange, tradingsymbol, transaction_type, quantity, product, order_type,
                    price=None, validity=None, trigger_price=None, tag=None, market_protection=None, **_):
        self._auth()
        if order_type in ("MARKET", "SL-M") and not market_protection:
            raise kex.InputException("Market orders require market protection.")
        oid = f"2510{next(self.ids):08d}"
        o = {"order_id": oid, "order_timestamp": datetime.now(), "tradingsymbol": tradingsymbol,
             "exchange": exchange, "transaction_type": transaction_type, "quantity": quantity,
             "filled_quantity": 0, "order_type": order_type, "product": product, "price": price or 0,
             "trigger_price": trigger_price or 0, "validity": validity, "tag": tag, "status": "OPEN",
             "average_price": 0, "status_message": None}
        self.placed.append(dict(o, market_protection=market_protection))
        px = self.prices[f"{exchange}:{tradingsymbol}"]
        if order_type == "MARKET" or (order_type == "LIMIT" and (
                (transaction_type == "BUY" and px <= price) or (transaction_type == "SELL" and px >= price))):
            self._fill(o, px)
        self.orders_[oid] = [dict(o, status="PUT ORDER REQ RECEIVED"), o]
        return oid

    def _fill(self, o, px):
        o.update(status="COMPLETE", filled_quantity=o["quantity"], average_price=px)
        sign = 1 if o["transaction_type"] == "BUY" else -1
        key = (o["exchange"], o["tradingsymbol"], o["product"])
        p = self.pos.setdefault(key, {"quantity": 0, "average_price": 0.0, "realised": 0.0})
        q = p["quantity"]
        dq = sign * o["quantity"]
        if q == 0 or (q > 0) == (dq > 0):
            p["average_price"] = (p["average_price"] * abs(q) + px * abs(dq)) / (abs(q) + abs(dq))
        else:
            p["realised"] += min(abs(q), abs(dq)) * (px - p["average_price"]) * (1 if q > 0 else -1)
        p["quantity"] = q + dq
        self.trades_.append({"trade_id": str(len(self.trades_) + 1), "order_id": o["order_id"],
                             "fill_timestamp": datetime.now(), "tradingsymbol": o["tradingsymbol"],
                             "exchange": o["exchange"], "transaction_type": o["transaction_type"],
                             "quantity": o["quantity"], "average_price": px, "product": o["product"]})

    def modify_order(self, variety, order_id, **kw):
        self._auth()
        self.orders_[order_id][-1].update({k: v for k, v in kw.items() if k in ("quantity", "price", "trigger_price")})
        return order_id

    def cancel_order(self, variety, order_id):
        self._auth()
        self.orders_[order_id][-1]["status"] = "CANCELLED"
        return order_id

    def orders(self):
        self._auth()
        return [h[-1] for h in self.orders_.values()]

    def order_history(self, order_id):
        self._auth()
        return self.orders_[order_id]

    def trades(self):
        self._auth()
        return list(self.trades_)

    # --- portfolio ---
    def positions(self):
        self._auth()
        rows = []
        for (ex, sym, prod), p in self.pos.items():
            ltp = self.prices[f"{ex}:{sym}"]
            unreal = (ltp - p["average_price"]) * p["quantity"]
            rows.append({"tradingsymbol": sym, "exchange": ex, "product": prod, "quantity": p["quantity"],
                         "average_price": p["average_price"], "last_price": ltp, "realised": p["realised"],
                         "unrealised": unreal, "pnl": p["realised"] + unreal, "m2m": p["realised"] + unreal,
                         "buy_quantity": 0, "sell_quantity": 0})
        return {"net": rows, "day": rows}

    def holdings(self):
        self._auth()
        return []

    def margins(self, segment=None):
        self._auth()
        return {"net": self.cash, "available": {"cash": self.cash, "live_balance": self.cash},
                "utilised": {"debits": 0, "m2m_realised": 0, "m2m_unrealised": 0}}

    def profile(self):
        self._auth()
        return {"user_id": "AB1234", "user_name": "Test User"}


@pytest.fixture
def live(tmp_path, monkeypatch):
    import kiteconnect

    FakeKite.instances.clear()
    monkeypatch.setattr(kiteconnect, "KiteConnect", FakeKite)
    s = Settings(_env_file=None, db_path=str(tmp_path / "live.db"), trading_mode="live",
                 live_trading_confirm=LIVE_CONFIRM_PHRASE, data_source="kite", kite_api_key="key",
                 kite_api_secret="secret", watchlist=["AAA"], bot_capital=50_000,
                 enforce_market_hours=False)
    e = Engine(s)
    assert e.mode == "live" and e.broker is None  # not logged in yet
    e.kite_login("req-tok")
    assert e.broker is not None and e.data_source == "kite"
    return e


def kite(e) -> FakeKite:
    return e.kite


def test_market_orders_carry_market_protection(live):
    o = live.place_manual(OrderRequest("AAA", "BUY", 10), auto_protect=True)
    assert o["status"] == "COMPLETE"
    assert kite(live).placed[-1]["market_protection"] == -1


def test_live_stop_loss_exit_goes_through(live):
    live.step(now=MON_11AM)
    live.place_manual(OrderRequest("AAA", "BUY", 10), auto_protect=True)
    kite(live).prices["NSE:AAA"] = 98.0
    live.step(now=MON_11AM)
    assert live.broker.net_qty("AAA", "NSE", "MIS") == 0
    sell = kite(live).placed[-1]
    assert sell["transaction_type"] == "SELL" and sell["market_protection"] == -1
    assert any("stop-loss" in ev["message"] for ev in live.events)


def test_daily_loss_limit_works_after_late_login(live):
    # Day starts before login, so starting equity must be picked up after login.
    live.step(now=MON_11AM)
    assert live.start_equity == 100_000
    live.place_manual(OrderRequest("AAA", "BUY", 100), auto_protect=False)
    kite(live).prices["NSE:AAA"] = 97.5  # -250 * ... => -2.5% of 100k is 2500
    kite(live).pos[("NSE", "AAA", "MIS")]["realised"] = -2_400
    live.step(now=MON_11AM)
    assert live.halted and "loss limit" in live.halted


def test_bot_trades_live_and_records_trades(live):
    live.start_bot()
    live.step(now=MON_11AM, force_strategy=True)
    assert live.broker.net_qty("AAA", "NSE", "MIS") > 0
    live._record_trades(force=True)
    trades = live.store.trades()
    assert trades and trades[0]["mode"] == "live" and trades[0]["side"] == "BUY"
    live._record_trades(force=True)
    assert len(live.store.trades()) == len(trades)  # no duplicates


def test_protection_survives_restart_in_live(live):
    live.place_manual(OrderRequest("AAA", "BUY", 10), auto_protect=True)
    live.step(now=MON_11AM)
    e2 = Engine(live.s)
    assert "NSE:AAA:MIS" in e2.managed


def test_token_expiry_stops_trading_and_asks_for_login(live):
    import asyncio

    kite(live).expired = True
    live.s.quote_seconds = 1

    async def one_loop():
        task = asyncio.create_task(live.run())
        await asyncio.sleep(0.3)
        task.cancel()

    asyncio.run(one_loop())
    assert live.broker is None
    assert any("session expired" in ev["message"] for ev in live.events)


def test_order_value_cap(live):
    live.s.max_order_value = 5_000
    with pytest.raises(Exception, match="above your limit"):
        live.place_manual(OrderRequest("AAA", "BUY", 100))
    assert live.place_manual(OrderRequest("AAA", "BUY", 10))["status"] == "COMPLETE"


def test_kite_proxy_is_used(tmp_path, monkeypatch):
    import kiteconnect

    monkeypatch.setattr(kiteconnect, "KiteConnect", FakeKite)
    s = Settings(_env_file=None, db_path=str(tmp_path / "p.db"), kite_api_key="k",
                 kite_proxy_url="http://u:p@proxy:8080")
    e = Engine(s)
    assert e.kite.proxies == {"http": "http://u:p@proxy:8080", "https": "http://u:p@proxy:8080"}


def test_preflight_checklist(live, monkeypatch):
    monkeypatch.setattr(Engine, "outbound_ip", lambda self: "203.0.113.7")
    checks = {c["name"]: c for c in live.preflight()}
    assert checks["Kite login"]["ok"] is True and "AB1234" in checks["Kite login"]["detail"]
    assert "203.0.113.7" in checks["Static IP"]["detail"]
    assert checks["Funds"]["ok"] is True
    assert checks["Live mode"]["ok"] is True
    assert checks["App password"]["ok"] is False  # no password in this test
