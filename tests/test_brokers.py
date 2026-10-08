import pytest

from app.brokers import OrderError, OrderRequest, PaperBroker, zerodha_charges


@pytest.fixture
def b():
    br = PaperBroker(100_000, slippage_bps=0)
    br.on_prices({"NSE:AAA": 100.0})
    return br


def req(side="BUY", qty=10, **kw):
    return OrderRequest("AAA", side, qty, **kw)


def test_market_buy_fills_with_charges(b):
    o = b.place_order(req())
    assert o["status"] == "COMPLETE" and o["average_price"] == 100.0
    assert b.net_qty("AAA", "NSE", "MIS") == 10
    assert b.funds()["cash"] == pytest.approx(100_000 - zerodha_charges("MIS", "BUY", 1000))


def test_round_trip_realizes_pnl(b):
    b.place_order(req())
    b.on_prices({"NSE:AAA": 110.0})
    b.place_order(req("SELL"))
    assert b.net_qty("AAA", "NSE", "MIS") == 0
    assert b.realized_today == pytest.approx(100.0)
    assert b.day_pnl() == pytest.approx(100.0 - b.charges_today)


def test_limit_order_waits_then_fills(b):
    o = b.place_order(req(order_type="LIMIT", price=95))
    assert o["status"] == "OPEN"
    b.on_prices({"NSE:AAA": 96.0})
    assert b.orders()[0]["status"] == "OPEN"
    b.on_prices({"NSE:AAA": 94.0})
    o = b.orders()[0]
    assert o["status"] == "COMPLETE" and o["average_price"] == 94.0


def test_sl_m_triggers(b):
    b.place_order(req())
    o = b.place_order(req("SELL", order_type="SL-M", trigger_price=97))
    assert o["status"] == "TRIGGER PENDING"
    b.on_prices({"NSE:AAA": 96.5})
    assert b.net_qty("AAA", "NSE", "MIS") == 0


def test_sl_limit_validation():
    with pytest.raises(OrderError):
        req("BUY", order_type="SL", price=90, trigger_price=100).validate()


def test_ioc_cancels_if_unfilled(b):
    o = b.place_order(req(order_type="LIMIT", price=90, validity="IOC"))
    assert o["status"] == "CANCELLED"


def test_modify_and_cancel(b):
    o = b.place_order(req(order_type="LIMIT", price=90))
    m = b.modify_order(o["order_id"], price=101)
    assert m["status"] == "COMPLETE"
    o2 = b.place_order(req(order_type="LIMIT", price=50))
    assert b.cancel_order(o2["order_id"])["status"] == "CANCELLED"
    with pytest.raises(OrderError):
        b.cancel_order(o2["order_id"])


def test_insufficient_funds_rejected(b):
    o = b.place_order(req(qty=5000))
    assert o["status"] == "REJECTED" and "insufficient" in o["status_message"]


def test_cnc_sell_requires_holdings(b):
    assert b.place_order(req("SELL", product="CNC"))["status"] == "REJECTED"
    b.place_order(req(product="CNC"))
    assert b.holdings()[0]["qty"] == 10
    assert b.place_order(req("SELL", product="CNC"))["status"] == "COMPLETE"


def test_intraday_short(b):
    b.place_order(req("SELL"))
    assert b.net_qty("AAA", "NSE", "MIS") == -10
    b.on_prices({"NSE:AAA": 95.0})
    b.place_order(req("BUY"))
    assert b.realized_today == pytest.approx(50.0)


def test_funds_add_withdraw(b):
    b.add_funds(50_000)
    assert b.funds()["cash"] == 150_000
    b.withdraw_funds(20_000)
    assert b.funds()["cash"] == 130_000
    with pytest.raises(OrderError):
        b.withdraw_funds(10**9)
    with pytest.raises(OrderError):
        b.add_funds(-5)


def test_state_roundtrip(b):
    b.place_order(req())
    b2 = PaperBroker()
    b2.load_state(b.to_state())
    assert b2.net_qty("AAA", "NSE", "MIS") == 10 and b2.cash == b.cash
