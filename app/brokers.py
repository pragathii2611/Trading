"""Brokers: a paper broker that behaves like Kite, and a live Zerodha Kite broker.

Both expose the same interface and return plain dicts shaped like Kite's API so
the dashboard does not care which one is active.
"""
from __future__ import annotations

import itertools
import logging
import time as _time
from dataclasses import dataclass

from .models import now_ist

log = logging.getLogger(__name__)

ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SL-M")
PRODUCTS = ("MIS", "CNC")
VALIDITIES = ("DAY", "IOC")
OPEN_STATUSES = ("OPEN", "TRIGGER PENDING")


class OrderError(Exception):
    pass


@dataclass
class OrderRequest:
    symbol: str
    side: str  # BUY | SELL
    qty: int
    exchange: str = "NSE"
    order_type: str = "MARKET"
    product: str = "MIS"
    price: float = 0.0
    trigger_price: float = 0.0
    validity: str = "DAY"
    tag: str = "manual"

    def validate(self) -> None:
        self.symbol = self.symbol.strip().upper()
        self.side = self.side.upper()
        if not self.symbol:
            raise OrderError("symbol is required")
        if self.side not in ("BUY", "SELL"):
            raise OrderError("side must be BUY or SELL")
        if int(self.qty) != self.qty or self.qty <= 0:
            raise OrderError("quantity must be a positive whole number")
        self.qty = int(self.qty)
        if self.exchange not in ("NSE", "BSE"):
            raise OrderError("exchange must be NSE or BSE")
        if self.order_type not in ORDER_TYPES:
            raise OrderError(f"order type must be one of {ORDER_TYPES}")
        if self.product not in PRODUCTS:
            raise OrderError(f"product must be one of {PRODUCTS}")
        if self.validity not in VALIDITIES:
            raise OrderError(f"validity must be one of {VALIDITIES}")
        if self.order_type in ("LIMIT", "SL") and self.price <= 0:
            raise OrderError(f"{self.order_type} order needs a price")
        if self.order_type in ("SL", "SL-M") and self.trigger_price <= 0:
            raise OrderError(f"{self.order_type} order needs a trigger price")
        if self.order_type == "SL":
            if self.side == "BUY" and self.price < self.trigger_price:
                raise OrderError("SL BUY: price must be >= trigger price")
            if self.side == "SELL" and self.price > self.trigger_price:
                raise OrderError("SL SELL: price must be <= trigger price")


def zerodha_charges(product: str, side: str, value: float, exchange: str = "NSE") -> float:
    """Approximate Zerodha equity charges (brokerage, STT, exchange, SEBI, stamp, GST)."""
    if product == "MIS":
        brokerage = min(20.0, value * 0.0003)
        stt = value * 0.00025 if side == "SELL" else 0.0
        stamp = value * 0.00003 if side == "BUY" else 0.0
    else:  # CNC delivery
        brokerage = 0.0
        stt = value * 0.001
        stamp = value * 0.00015 if side == "BUY" else 0.0
    txn = value * (0.0000297 if exchange == "NSE" else 0.0000375)
    sebi = value * 0.000001
    gst = 0.18 * (brokerage + txn + sebi)
    return round(brokerage + stt + stamp + txn + sebi + gst, 2)


def _key(exchange: str, symbol: str) -> str:
    return f"{exchange}:{symbol}"


class Broker:
    name = "base"
    supports_funds = False

    def place_order(self, req: OrderRequest) -> dict: ...
    def modify_order(self, order_id: str, **changes) -> dict: ...
    def cancel_order(self, order_id: str) -> dict: ...
    def orders(self) -> list[dict]: ...
    def positions(self, ltp: dict[str, float]) -> list[dict]: ...
    def holdings(self, ltp: dict[str, float]) -> list[dict]: ...
    def funds(self, ltp: dict[str, float]) -> dict: ...
    def net_qty(self, symbol: str, exchange: str, product: str) -> int: ...
    def day_pnl(self, ltp: dict[str, float]) -> float: ...

    def on_prices(self, ltp: dict[str, float]) -> None:
        """Called with fresh prices ({"NSE:INFY": 1800.5}); paper broker matches orders here."""

    def wait_for_fill(self, order_id: str, timeout: float = 10.0) -> dict | None:
        for o in self.orders():
            if o["order_id"] == order_id:
                return o
        return None

    def add_funds(self, amount: float, note: str = "") -> dict:
        raise OrderError("Add funds through Zerodha Kite / Console for a live account")

    def withdraw_funds(self, amount: float, note: str = "") -> dict:
        raise OrderError("Withdraw funds through Zerodha Kite / Console for a live account")

    def new_day(self) -> None:
        pass


class PaperBroker(Broker):
    name = "paper"
    supports_funds = True

    def __init__(self, starting_capital: float = 0.0, slippage_bps: float = 5.0, mis_leverage: float = 1.0):
        self.slippage = slippage_bps / 10_000
        self.mis_leverage = max(1.0, mis_leverage)
        self.cash = 0.0
        self.ledger: list[dict] = []
        self.pos: dict[str, dict] = {}       # "NSE:INFY:MIS" -> position row
        self._orders: dict[str, dict] = {}
        self.trades: list[dict] = []          # today's executions (also persisted by the store)
        self.realized_today = 0.0
        self.charges_today = 0.0
        self.ltp: dict[str, float] = {}
        self._ids = itertools.count(1)
        if starting_capital:
            self.add_funds(starting_capital, "opening balance")

    # ---- persistence -----------------------------------------------------
    def to_state(self) -> dict:
        return {"cash": self.cash, "ledger": self.ledger, "pos": self.pos, "orders": self._orders,
                "realized_today": self.realized_today, "charges_today": self.charges_today,
                "trades": self.trades, "next_id": next(self._ids)}

    def load_state(self, st: dict) -> None:
        self.cash = st["cash"]
        self.ledger = st["ledger"]
        self.pos = st["pos"]
        self._orders = st["orders"]
        self.realized_today = st.get("realized_today", 0.0)
        self.charges_today = st.get("charges_today", 0.0)
        self.trades = st.get("trades", [])
        self._ids = itertools.count(st.get("next_id", 1))

    # ---- funds -----------------------------------------------------------
    def add_funds(self, amount: float, note: str = "") -> dict:
        if amount <= 0:
            raise OrderError("amount must be positive")
        self.cash += amount
        entry = {"time": now_ist().isoformat(), "type": "DEPOSIT", "amount": round(amount, 2),
                 "note": note, "balance": round(self.cash, 2)}
        self.ledger.append(entry)
        return entry

    def withdraw_funds(self, amount: float, note: str = "") -> dict:
        if amount <= 0:
            raise OrderError("amount must be positive")
        avail = self.funds(self.ltp)["available"]
        if amount > avail:
            raise OrderError(f"only ₹{avail:,.2f} is available to withdraw")
        self.cash -= amount
        entry = {"time": now_ist().isoformat(), "type": "WITHDRAW", "amount": round(amount, 2),
                 "note": note, "balance": round(self.cash, 2)}
        self.ledger.append(entry)
        return entry

    def _lev(self, product: str) -> float:
        return self.mis_leverage if product == "MIS" else 1.0

    def _price(self, exchange: str, symbol: str, ltp: dict | None = None) -> float | None:
        return (ltp or self.ltp).get(_key(exchange, symbol), self.ltp.get(_key(exchange, symbol)))

    def funds(self, ltp=None) -> dict:
        ltp = ltp or self.ltp
        used = unreal = 0.0
        for p in self.pos.values():
            if p["qty"]:
                used += abs(p["qty"]) * p["avg_price"] / self._lev(p["product"])
                px = self._price(p["exchange"], p["symbol"], ltp) or p["avg_price"]
                unreal += (px - p["avg_price"]) * p["qty"]
        available = self.cash - used + min(unreal, 0.0)
        return {"cash": round(self.cash, 2), "used_margin": round(used, 2),
                "available": round(available, 2), "unrealized": round(unreal, 2),
                "equity": round(self.cash + unreal, 2), "realized_today": round(self.realized_today, 2),
                "charges_today": round(self.charges_today, 2), "ledger": self.ledger[-50:]}

    # ---- orders ----------------------------------------------------------
    def place_order(self, req: OrderRequest) -> dict:
        req.validate()
        oid = f"P{now_ist():%y%m%d}{next(self._ids):05d}"
        o = {
            "order_id": oid, "time": now_ist().isoformat(), "symbol": req.symbol,
            "exchange": req.exchange, "side": req.side, "qty": req.qty, "filled_qty": 0,
            "order_type": req.order_type, "product": req.product, "price": req.price,
            "trigger_price": req.trigger_price, "validity": req.validity, "tag": req.tag,
            "status": "TRIGGER PENDING" if req.order_type in ("SL", "SL-M") else "OPEN",
            "average_price": 0.0, "status_message": "",
        }
        self._orders[oid] = o
        if req.product == "CNC" and req.side == "SELL":
            held = self.net_qty(req.symbol, req.exchange, "CNC")
            if held < req.qty:
                return self._reject(o, f"CNC sell needs holdings; you hold {held}")
        if self._price(req.exchange, req.symbol) is None and req.order_type in ("MARKET", "SL-M"):
            return self._reject(o, "no market price available yet for this symbol")
        self._try_match(o)
        if o["validity"] == "IOC" and o["status"] in OPEN_STATUSES:
            o["status"], o["status_message"] = "CANCELLED", "IOC not filled immediately"
        return dict(o)

    def _reject(self, o: dict, msg: str) -> dict:
        o["status"], o["status_message"] = "REJECTED", msg
        return dict(o)

    def modify_order(self, order_id: str, **changes) -> dict:
        o = self._orders.get(order_id)
        if not o:
            raise OrderError("order not found")
        if o["status"] not in OPEN_STATUSES:
            raise OrderError(f"cannot modify a {o['status']} order")
        trial = OrderRequest(o["symbol"], o["side"], o["qty"], o["exchange"], o["order_type"], o["product"],
                             o["price"], o["trigger_price"], o["validity"], o["tag"])
        for k in ("qty", "price", "trigger_price", "order_type"):
            if changes.get(k) is not None:
                setattr(trial, k, changes[k])
        trial.validate()
        o.update(qty=trial.qty, price=trial.price, trigger_price=trial.trigger_price, order_type=trial.order_type)
        if trial.order_type in ("MARKET", "LIMIT"):
            o["status"] = "OPEN"
        self._try_match(o)
        return dict(o)

    def cancel_order(self, order_id: str) -> dict:
        o = self._orders.get(order_id)
        if not o:
            raise OrderError("order not found")
        if o["status"] not in OPEN_STATUSES:
            raise OrderError(f"cannot cancel a {o['status']} order")
        o["status"], o["status_message"] = "CANCELLED", "cancelled by user"
        return dict(o)

    def orders(self) -> list[dict]:
        return sorted((dict(o) for o in self._orders.values()), key=lambda o: o["time"], reverse=True)

    def on_prices(self, ltp: dict[str, float]) -> None:
        self.ltp.update(ltp)
        for o in list(self._orders.values()):
            if o["status"] in OPEN_STATUSES:
                self._try_match(o)

    def _try_match(self, o: dict) -> None:
        px = self._price(o["exchange"], o["symbol"])
        if px is None:
            return
        buy = o["side"] == "BUY"
        if o["status"] == "TRIGGER PENDING":
            hit = px >= o["trigger_price"] if buy else px <= o["trigger_price"]
            if not hit:
                return
            o["status"] = "OPEN"
        kind = "MARKET" if o["order_type"] in ("MARKET", "SL-M") else "LIMIT"
        if kind == "MARKET":
            fill = px * (1 + self.slippage) if buy else px * (1 - self.slippage)
        else:
            if (buy and px > o["price"]) or (not buy and px < o["price"]):
                return
            fill = min(px, o["price"]) if buy else max(px, o["price"])
        self._fill(o, round(fill, 2))

    def _fill(self, o: dict, price: float) -> None:
        key = f"{o['exchange']}:{o['symbol']}:{o['product']}"
        p = self.pos.setdefault(key, {"symbol": o["symbol"], "exchange": o["exchange"], "product": o["product"],
                                      "qty": 0, "avg_price": 0.0, "realized": 0.0,
                                      "buy_qty": 0, "sell_qty": 0, "charges": 0.0})
        dq = o["qty"] if o["side"] == "BUY" else -o["qty"]
        q, a = p["qty"], p["avg_price"]
        # margin check for the part of the fill that increases exposure
        increasing = abs(dq) if (q == 0 or (q > 0) == (dq > 0)) else max(0, abs(dq) - abs(q))
        need = increasing * price / self._lev(o["product"])
        if need > self.funds()["available"] + 1e-6:
            o["status"] = "REJECTED"
            o["status_message"] = f"insufficient funds: need ₹{need:,.2f}"
            return
        charges = zerodha_charges(o["product"], o["side"], price * o["qty"], o["exchange"])
        realized = 0.0
        if q == 0 or (q > 0) == (dq > 0):
            p["avg_price"] = (a * abs(q) + price * abs(dq)) / (abs(q) + abs(dq))
        else:
            closing = min(abs(q), abs(dq))
            realized = closing * (price - a) * (1 if q > 0 else -1)
            if abs(dq) > abs(q):
                p["avg_price"] = price  # position flipped
        p["qty"] = q + dq
        if p["qty"] == 0:
            p["avg_price"] = 0.0
        p["realized"] += realized
        p["charges"] += charges
        p["buy_qty" if dq > 0 else "sell_qty"] += abs(dq)
        self.cash += realized - charges
        self.realized_today += realized
        self.charges_today += charges
        o.update(status="COMPLETE", filled_qty=o["qty"], average_price=price, status_message="")
        self.trades.append({"order_id": o["order_id"], "time": now_ist().isoformat(), "symbol": o["symbol"],
                            "exchange": o["exchange"], "side": o["side"], "qty": o["qty"], "price": price,
                            "product": o["product"], "charges": charges, "realized": round(realized, 2),
                            "tag": o["tag"]})

    # ---- portfolio -------------------------------------------------------
    def net_qty(self, symbol, exchange, product) -> int:
        return self.pos.get(f"{exchange}:{symbol}:{product}", {}).get("qty", 0)

    def positions(self, ltp=None) -> list[dict]:
        out = []
        for p in self.pos.values():
            if not p["qty"] and not p["buy_qty"] and not p["sell_qty"]:
                continue  # nothing today
            px = self._price(p["exchange"], p["symbol"], ltp) or p["avg_price"]
            unreal = (px - p["avg_price"]) * p["qty"] if p["qty"] else 0.0
            out.append({**p, "ltp": px, "unrealized": round(unreal, 2),
                        "pnl": round(p["realized"] + unreal, 2), "realized": round(p["realized"], 2),
                        "avg_price": round(p["avg_price"], 2)})
        return out

    def holdings(self, ltp=None) -> list[dict]:
        out = []
        for p in self.pos.values():
            if p["product"] == "CNC" and p["qty"] > 0:
                px = self._price(p["exchange"], p["symbol"], ltp) or p["avg_price"]
                out.append({"symbol": p["symbol"], "exchange": p["exchange"], "qty": p["qty"],
                            "avg_price": round(p["avg_price"], 2), "ltp": px,
                            "value": round(px * p["qty"], 2),
                            "pnl": round((px - p["avg_price"]) * p["qty"], 2)})
        return out

    def day_pnl(self, ltp=None) -> float:
        unreal = sum(
            ((self._price(p["exchange"], p["symbol"], ltp) or p["avg_price"]) - p["avg_price"]) * p["qty"]
            for p in self.pos.values() if p["product"] == "MIS"
        )
        return round(self.realized_today + unreal - self.charges_today, 2)

    def new_day(self) -> None:
        for o in self._orders.values():
            if o["status"] in OPEN_STATUSES:
                o["status"], o["status_message"] = "CANCELLED", "expired at end of day"
        self._orders = {k: o for k, o in self._orders.items() if o["status"] in OPEN_STATUSES}
        for k in list(self.pos):
            p = self.pos[k]
            if p["qty"] == 0:
                del self.pos[k]
            else:
                p.update(realized=0.0, buy_qty=0, sell_qty=0, charges=0.0)
        self.realized_today = self.charges_today = 0.0
        self.trades = []


class KiteBroker(Broker):
    """Live trading through Zerodha Kite Connect. Every call here moves real money."""

    name = "kite"

    def __init__(self, kite):
        self.kite = kite

    def place_order(self, req: OrderRequest) -> dict:
        req.validate()
        k = self.kite
        params = dict(variety=k.VARIETY_REGULAR, exchange=req.exchange, tradingsymbol=req.symbol,
                      transaction_type=req.side, quantity=req.qty, product=req.product,
                      order_type=req.order_type, validity=req.validity, tag=req.tag[:20])
        if req.order_type in ("LIMIT", "SL"):
            params["price"] = req.price
        if req.order_type in ("SL", "SL-M"):
            params["trigger_price"] = req.trigger_price
        try:
            oid = k.place_order(**params)
        except Exception as e:  # kiteconnect raises typed exceptions; surface the message
            raise OrderError(str(e)) from e
        return self.wait_for_fill(oid, timeout=0) or {"order_id": oid, "status": "OPEN"}

    def modify_order(self, order_id: str, **changes) -> dict:
        params = {k: v for k, v in changes.items() if v is not None}
        if "qty" in params:
            params["quantity"] = params.pop("qty")
        try:
            self.kite.modify_order(self.kite.VARIETY_REGULAR, order_id, **params)
        except Exception as e:
            raise OrderError(str(e)) from e
        return self.wait_for_fill(order_id, timeout=0) or {"order_id": order_id}

    def cancel_order(self, order_id: str) -> dict:
        try:
            self.kite.cancel_order(self.kite.VARIETY_REGULAR, order_id)
        except Exception as e:
            raise OrderError(str(e)) from e
        return {"order_id": order_id, "status": "CANCELLED"}

    @staticmethod
    def _order(o: dict) -> dict:
        return {"order_id": o["order_id"], "time": str(o.get("order_timestamp") or ""),
                "symbol": o["tradingsymbol"], "exchange": o["exchange"], "side": o["transaction_type"],
                "qty": o["quantity"], "filled_qty": o.get("filled_quantity", 0),
                "order_type": o["order_type"], "product": o["product"], "price": o.get("price") or 0,
                "trigger_price": o.get("trigger_price") or 0, "validity": o.get("validity", "DAY"),
                "tag": o.get("tag") or "", "status": o["status"],
                "average_price": o.get("average_price") or 0, "status_message": o.get("status_message") or ""}

    def orders(self) -> list[dict]:
        return [self._order(o) for o in reversed(self.kite.orders())]

    def wait_for_fill(self, order_id: str, timeout: float = 10.0) -> dict | None:
        deadline = _time.time() + timeout
        while True:
            hist = self.kite.order_history(order_id)
            last = self._order(hist[-1]) if hist else None
            if last is None or last["status"] not in OPEN_STATUSES + ("PUT ORDER REQ RECEIVED", "VALIDATION PENDING"):
                return last
            if _time.time() >= deadline:
                return last
            _time.sleep(0.5)

    def positions(self, ltp=None) -> list[dict]:
        return [{"symbol": p["tradingsymbol"], "exchange": p["exchange"], "product": p["product"],
                 "qty": p["quantity"], "avg_price": p["average_price"], "ltp": p["last_price"],
                 "realized": p.get("realised", 0), "unrealized": p.get("unrealised", 0), "pnl": p["pnl"],
                 "buy_qty": p.get("buy_quantity", 0), "sell_qty": p.get("sell_quantity", 0)}
                for p in self.kite.positions()["net"]]

    def holdings(self, ltp=None) -> list[dict]:
        return [{"symbol": h["tradingsymbol"], "exchange": h["exchange"], "qty": h["quantity"],
                 "avg_price": h["average_price"], "ltp": h["last_price"],
                 "value": round(h["last_price"] * h["quantity"], 2), "pnl": h["pnl"]}
                for h in self.kite.holdings()]

    def funds(self, ltp=None) -> dict:
        m = self.kite.margins("equity")
        return {"cash": m["available"].get("cash", 0), "used_margin": m["utilised"].get("debits", 0),
                "available": m["available"].get("live_balance", m.get("net", 0)),
                "unrealized": m["utilised"].get("m2m_unrealised", 0), "equity": m.get("net", 0),
                "realized_today": m["utilised"].get("m2m_realised", 0), "charges_today": 0, "ledger": []}

    def net_qty(self, symbol, exchange, product) -> int:
        for p in self.kite.positions()["net"]:
            if p["tradingsymbol"] == symbol and p["exchange"] == exchange and p["product"] == product:
                return p["quantity"]
        return 0

    def day_pnl(self, ltp=None) -> float:
        return round(sum(p["pnl"] for p in self.kite.positions()["day"]), 2)
