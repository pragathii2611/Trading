"""Trading engine: price loop, strategy loop, auto-trading, protective exits, kill switch."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from datetime import datetime

from .brokers import OPEN_STATUSES, KiteBroker, OrderError, OrderRequest, PaperBroker
from .config import Settings
from .data import DataProvider, make_provider
from .models import Position, now_ist
from .risk import RiskManager
from .store import Store
from .strategies import make_strategy

log = logging.getLogger(__name__)

# Settings that may be changed from the dashboard at runtime.
EDITABLE = {
    "watchlist", "strategy", "buy_threshold", "sell_threshold", "bot_capital", "risk_per_trade_pct",
    "max_position_pct", "max_open_positions", "daily_loss_limit_pct", "stop_loss_pct",
    "take_profit_pct", "cooldown_minutes", "breakeven_trigger_pct", "trailing_stop_pct",
    "early_exit_on_weak_signal", "weak_signal_threshold", "max_hold_minutes",
    "daily_profit_target_pct", "profit_giveback_pct", "no_new_entries_after", "square_off_time",
    "poll_seconds",
}


def _pkey(exchange: str, symbol: str, product: str) -> str:
    return f"{exchange}:{symbol}:{product}"


class Engine:
    def __init__(self, settings: Settings, store: Store | None = None):
        self.store = store or Store(settings.db_path)
        overrides = self.store.get("settings_overrides", {})
        self.s = self._validated(settings, overrides) if overrides else settings
        self.lock = threading.RLock()
        self.events: deque[dict] = deque(maxlen=200)
        self.kite = None
        self.broker = None
        self.data: DataProvider | None = None
        self.running = False            # bot auto-trading on/off
        self.kill_switch = False
        self.halted: str | None = None  # day-level halt reason
        self.managed: dict[str, Position] = {}       # positions under automatic protection
        self.pending_protect: dict[str, dict] = {}   # order_id -> protection params
        self.last_exit: dict[str, datetime] = {}
        self.signals: dict[str, dict] = {}
        self.ltp: dict[str, float] = {}
        self.extra_symbols: set[str] = set()         # "NSE:SBIN" traded manually
        self.day = None
        self.start_equity = 0.0
        self.peak_day_pnl = 0.0
        self.snapshot: dict = {}
        self._recorded = 0
        self._last_strategy = 0.0
        self._last_equity = -1e9
        self._task: asyncio.Task | None = None
        self._build()

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _validated(base: Settings, changes: dict) -> Settings:
        merged = {**base.model_dump(), **{k: v for k, v in changes.items() if k in EDITABLE}}
        return type(base).model_validate(merged)

    @property
    def mode(self) -> str:
        return "live" if self.s.live_enabled else "paper"

    def _build(self) -> None:
        s = self.s
        if s.trading_mode == "live" and not s.live_enabled:
            self.event("warn", "TRADING_MODE=live ignored: set LIVE_TRADING_CONFIRM to enable real orders. "
                               "Running in PAPER mode.")
        token = s.kite_access_token or self._stored_kite_token()
        if s.kite_api_key:
            from kiteconnect import KiteConnect

            self.kite = KiteConnect(api_key=s.kite_api_key)
            if token:
                self.kite.set_access_token(token)
        kite_ready = self.kite is not None and bool(token)

        source = s.data_source
        if source == "kite" and not kite_ready:
            self.event("warn", "Kite data needs a login; using simulated prices until you log in.")
            source = "simulated"
        self.data = make_provider(source, s.candle_interval, self.kite if kite_ready else None)
        self.data_source = source
        if source == "simulated":
            s.enforce_market_hours = False  # simulated market never closes
        self.risk = RiskManager(s)
        self.strategy = make_strategy(s.strategy, s.buy_threshold, s.sell_threshold)

        if self.mode == "live":
            self.broker = KiteBroker(self.kite) if kite_ready else None
            if not kite_ready:
                self.event("warn", "LIVE mode: log in to Kite from the dashboard before trading.")
        else:
            self.broker = PaperBroker(0, s.slippage_bps, s.mis_leverage)
            st = self.store.get("paper_state")
            if st:
                self.broker.load_state(st)
                self._recorded = len(self.broker.trades)
            else:
                self.broker.add_funds(s.starting_capital, "opening balance")
            managed = self.store.get("paper_managed", {})
            for k, p in managed.items():
                p["opened_at"] = datetime.fromisoformat(p["opened_at"])
                self.managed[k] = Position(**p)

    def _stored_kite_token(self) -> str:
        t = self.store.get("kite_token")
        if t and t.get("date") == now_ist().date().isoformat():
            return t["token"]
        return ""

    # ------------------------------------------------------------------ utils
    def event(self, level: str, msg: str) -> None:
        ts = now_ist().isoformat(timespec="seconds")
        self.events.appendleft({"time": ts, "level": level, "message": msg})
        getattr(log, "warning" if level == "warn" else level if level in ("info", "error") else "info")(msg)
        try:
            self.store.add_event(ts, level, msg)
        except Exception:  # pragma: no cover
            pass

    def _require_broker(self):
        if self.broker is None:
            raise OrderError("Not connected to Kite. Log in to Kite first.")
        return self.broker

    def tracked(self) -> set[str]:
        keys = {f"{self.s.exchange}:{sym}" for sym in self.s.watchlist} | set(self.extra_symbols)
        for p in self.managed.values():
            keys.add(f"{p.exchange}:{p.symbol}")
        return keys

    # ------------------------------------------------------------------ loop
    async def run(self) -> None:
        self.event("info", f"Engine started in {self.mode.upper()} mode, data: {self.data_source}")
        if self.s.autostart:
            self.start_bot()
        while True:
            started = time.monotonic()
            try:
                await asyncio.to_thread(self.step)
            except Exception as e:  # keep the loop alive no matter what
                log.exception("engine step failed")
                self.event("error", f"engine step failed: {e}")
            interval = self.s.quote_seconds if self.data_source != "yahoo" else max(20, self.s.quote_seconds)
            await asyncio.sleep(max(0.5, interval - (time.monotonic() - started)))

    def step(self, now: datetime | None = None, force_strategy: bool = False) -> None:
        now = now or now_ist()
        with self.lock:
            self._roll_day(now)
            self.data.tick()
            self._refresh_prices()
            if self.broker is not None:
                self.broker.on_prices(self.ltp)
            self._record_trades()
            if force_strategy or time.monotonic() - self._last_strategy >= self.s.poll_seconds:
                self._last_strategy = time.monotonic()
                self._compute_signals()
            if self.broker is not None:
                self._check_pending_protect(now)
                self._manage_exits(now)
                self._day_guard(now)
                if self.running and not self.kill_switch and not self.halted:
                    self._entries(now)
                self._record_trades()
            self._persist()
            self._snapshot(now)

    def _roll_day(self, now: datetime) -> None:
        if self.day == now.date():
            return
        if self.day is not None:
            if self.broker is not None:
                self.broker.new_day()
            self._recorded = 0
            self.event("info", "New trading day: daily limits reset")
        self.day = now.date()
        self.halted = None
        self.peak_day_pnl = 0.0
        self.last_exit.clear()
        if self.broker is not None:
            try:
                self.start_equity = float(self.broker.funds(self.ltp)["equity"])
            except Exception:
                self.start_equity = self.s.starting_capital

    def _refresh_prices(self) -> None:
        by_ex: dict[str, list[str]] = {}
        for key in self.tracked():
            ex, sym = key.split(":", 1)
            by_ex.setdefault(ex, []).append(sym)
        for ex, syms in by_ex.items():
            try:
                for sym, px in self.data.get_ltp(syms, ex).items():
                    self.ltp[f"{ex}:{sym}"] = px
            except Exception as e:
                self.event("warn", f"price fetch failed for {ex}: {e}")

    def _record_trades(self) -> None:
        if isinstance(self.broker, PaperBroker):
            new = self.broker.trades[self._recorded:]
            for t in new:
                self.store.add_trade(t, self.mode)
            self._recorded = len(self.broker.trades)

    def _compute_signals(self) -> None:
        for sym in self.s.watchlist:
            try:
                df = self.data.get_candles(sym, self.s.exchange, self.s.candle_interval, 200)
                sig = self.strategy.evaluate(df)
            except Exception as e:
                self.event("warn", f"signal failed for {sym}: {e}")
                continue
            self.signals[sym] = {"symbol": sym, "action": sig.action, "score": sig.score,
                                 "reason": sig.reason, "time": now_ist().isoformat(timespec="seconds")}

    # ------------------------------------------------------------------ trading
    def _bot_positions(self) -> dict[str, Position]:
        return {p.symbol: p for p in self.managed.values() if p.source == "bot"}

    def _entries(self, now: datetime) -> None:
        bot = self._bot_positions()
        for sym, sig in self.signals.items():
            if sig["action"] != "BUY":
                continue
            block = self.risk.entry_block_reason(sym, now, bot, self.last_exit, self.halted)
            if block:
                continue
            px = self.ltp.get(f"{self.s.exchange}:{sym}")
            if not px:
                continue
            funds = self.broker.funds(self.ltp)
            bot_used = sum(p.qty * p.entry_price for p in bot.values())
            bot_equity = min(self.s.bot_capital, funds["equity"])
            cash = min(funds["available"], self.s.bot_capital - bot_used)
            qty = self.risk.position_size(bot_equity, cash, px)
            if qty < 1:
                self.event("info", f"BUY {sym} skipped: not enough bot capital")
                continue
            o = self._place(OrderRequest(sym, "BUY", qty, self.s.exchange, "MARKET", "MIS", tag="bot"),
                            wait=True)
            if o and o.get("status") == "COMPLETE":
                self._protect(sym, self.s.exchange, "MIS", 1, qty, o["average_price"], now, "bot")
                bot = self._bot_positions()
                self.event("trade", f"BOT BUY {qty} {sym} @ {o['average_price']:.2f} — {sig['reason']}")
            elif o:
                self.event("warn", f"BOT BUY {sym} not filled: {o.get('status')} {o.get('status_message', '')}")

    def _protect(self, sym, exchange, product, side, qty, price, now, source,
                 stop_pct=None, target_pct=None) -> Position:
        stop, target = self.risk.initial_stops(price, side, stop_pct, target_pct)
        key = _pkey(exchange, sym, product)
        existing = self.managed.get(key)
        if existing and existing.side == side:  # averaging into a protected position
            total = existing.qty + qty
            existing.entry_price = round((existing.entry_price * existing.qty + price * qty) / total, 2)
            existing.qty = total
            return existing
        pos = Position(sym, qty, price, stop, target, now, side=side, product=product, source=source,
                       exchange=exchange)
        self.managed[key] = pos
        return pos

    def _check_pending_protect(self, now: datetime) -> None:
        if not self.pending_protect:
            return
        orders = {o["order_id"]: o for o in self.broker.orders()}
        for oid, prm in list(self.pending_protect.items()):
            o = orders.get(oid)
            if o is None:
                continue
            if o["status"] == "COMPLETE":
                side = 1 if o["side"] == "BUY" else -1
                self._protect(o["symbol"], o["exchange"], o["product"], side, o["qty"], o["average_price"],
                              now, "manual", prm.get("stop_loss_pct"), prm.get("target_pct"))
                self.event("info", f"Auto-protect active on {o['symbol']} ({o['side']} {o['qty']})")
                del self.pending_protect[oid]
            elif o["status"] not in OPEN_STATUSES:
                del self.pending_protect[oid]

    def _manage_exits(self, now: datetime) -> None:
        if not self.managed:
            if isinstance(self.broker, PaperBroker) and self.risk.past_square_off(now):
                self._square_off_all_mis("intraday square-off")
            return
        actual = {_pkey(p["exchange"], p["symbol"], p["product"]): p["qty"] for p in self.broker.positions(self.ltp)}
        for key, pos in list(self.managed.items()):
            qty = actual.get(key, 0)
            if qty == 0 or (qty > 0) != (pos.side > 0):
                self.managed.pop(key)  # closed outside the bot
                continue
            pos.qty = abs(qty)
            px = self.ltp.get(f"{pos.exchange}:{pos.symbol}")
            if not px:
                continue
            sig = self.signals.get(pos.symbol) if pos.exchange == self.s.exchange else None
            reason = self.risk.exit_reason(pos, px, sig["score"] if sig else None, now)
            if reason:
                self._close(key, pos, reason)
        if isinstance(self.broker, PaperBroker) and self.risk.past_square_off(now):
            self._square_off_all_mis("intraday square-off")

    def _close(self, key: str, pos: Position, reason: str) -> None:
        side = "SELL" if pos.side == 1 else "BUY"
        o = self._place(OrderRequest(pos.symbol, side, pos.qty, pos.exchange, "MARKET", pos.product,
                                     tag="bot-exit" if pos.source == "bot" else "protect-exit"), wait=True)
        if o and o.get("status") == "COMPLETE":
            pnl = (o["average_price"] - pos.entry_price) * pos.qty * pos.side
            self.managed.pop(key, None)
            self.last_exit[pos.symbol] = now_ist()
            self.event("trade", f"EXIT {pos.symbol} {side} {pos.qty} @ {o['average_price']:.2f} "
                                f"(P&L {pnl:+,.2f}) — {reason}")
        elif o:
            self.event("error", f"EXIT {pos.symbol} failed: {o.get('status')} {o.get('status_message', '')}")

    def _square_off_all_mis(self, reason: str) -> None:
        for p in self.broker.positions(self.ltp):
            if p["product"] == "MIS" and p["qty"]:
                side = "SELL" if p["qty"] > 0 else "BUY"
                o = self._place(OrderRequest(p["symbol"], side, abs(p["qty"]), p["exchange"], "MARKET", "MIS",
                                             tag="square-off"), wait=True)
                self.managed.pop(_pkey(p["exchange"], p["symbol"], "MIS"), None)
                if o and o.get("status") == "COMPLETE":
                    self.event("trade", f"{reason}: {side} {abs(p['qty'])} {p['symbol']} @ {o['average_price']:.2f}")

    def _day_guard(self, now: datetime) -> None:
        if self.halted:
            return
        pnl = self.broker.day_pnl(self.ltp)
        self.peak_day_pnl = max(self.peak_day_pnl, pnl)
        reason = self.risk.day_halt_reason(pnl, self.peak_day_pnl, self.start_equity)
        if reason:
            self.halted = reason
            self.event("warn", f"Bot halted for today — {reason}. Closing bot positions.")
            for key, pos in list(self.managed.items()):
                if pos.source == "bot":
                    self._close(key, pos, reason)

    def _place(self, req: OrderRequest, wait: bool = False) -> dict | None:
        try:
            o = self.broker.place_order(req)
        except OrderError as e:
            self.event("error", f"order rejected ({req.side} {req.qty} {req.symbol}): {e}")
            return None
        if wait and o.get("status") not in ("COMPLETE", "REJECTED", "CANCELLED"):
            o = self.broker.wait_for_fill(o["order_id"], timeout=10) or o
        return o

    # ------------------------------------------------------------------ actions (API)
    def start_bot(self) -> None:
        with self.lock:
            if self.kill_switch:
                raise OrderError("Kill switch is on. Reset it before starting the bot.")
            self._require_broker()
            self.running = True
            self.event("info", "Auto-trading bot STARTED")

    def stop_bot(self) -> None:
        with self.lock:
            self.running = False
            self.event("info", "Auto-trading bot STOPPED (open positions stay protected)")

    def kill(self) -> None:
        with self.lock:
            self.running = False
            self.kill_switch = True
            self.event("warn", "KILL SWITCH: cancelling open orders and closing all intraday positions")
            if self.broker is None:
                return
            for o in self.broker.orders():
                if o["status"] in OPEN_STATUSES:
                    try:
                        self.broker.cancel_order(o["order_id"])
                    except OrderError as e:
                        self.event("error", f"cancel {o['order_id']} failed: {e}")
            self._square_off_all_mis("kill switch")
            self.managed.clear()
            self._record_trades()
            self._persist()

    def reset_kill(self) -> None:
        with self.lock:
            self.kill_switch = False
            self.event("info", "Kill switch reset")

    def place_manual(self, req: OrderRequest, auto_protect: bool = False,
                     stop_loss_pct: float | None = None, target_pct: float | None = None) -> dict:
        with self.lock:
            broker = self._require_broker()
            if self.kill_switch:
                raise OrderError("Kill switch is on. Reset it to place orders.")
            req.tag = "manual"
            req.validate()
            key = f"{req.exchange}:{req.symbol}"
            self.extra_symbols.add(key)
            if key not in self.ltp:
                self._refresh_prices()
                broker.on_prices(self.ltp)
            o = broker.place_order(req)
            self.event("order", f"{req.side} {req.qty} {req.symbol} {req.order_type} {req.product} → {o.get('status')}"
                                + (f" ({o['status_message']})" if o.get("status_message") else ""))
            if auto_protect and o.get("status") not in ("REJECTED", "CANCELLED"):
                self.pending_protect[o["order_id"]] = {"stop_loss_pct": stop_loss_pct, "target_pct": target_pct}
                self._check_pending_protect(now_ist())
            self._record_trades()
            self._persist()
            self._snapshot(now_ist())
            return o

    def modify_order(self, order_id: str, **changes) -> dict:
        with self.lock:
            o = self._require_broker().modify_order(order_id, **changes)
            self.event("order", f"modified {order_id} → {o.get('status')}")
            self._record_trades()
            self._persist()
            return o

    def cancel_order(self, order_id: str) -> dict:
        with self.lock:
            o = self._require_broker().cancel_order(order_id)
            self.pending_protect.pop(order_id, None)
            self.event("order", f"cancelled {order_id}")
            self._persist()
            return o

    def exit_position(self, symbol: str, exchange: str, product: str) -> dict:
        with self.lock:
            broker = self._require_broker()
            qty = broker.net_qty(symbol, exchange, product)
            if not qty:
                raise OrderError("no open position")
            side = "SELL" if qty > 0 else "BUY"
            o = self._place(OrderRequest(symbol, side, abs(qty), exchange, "MARKET", product, tag="manual-exit"),
                            wait=True)
            if o is None:
                raise OrderError("exit order rejected")
            self.managed.pop(_pkey(exchange, symbol, product), None)
            self.last_exit[symbol] = now_ist()
            self.event("trade", f"Manual exit {side} {abs(qty)} {symbol} → {o.get('status')}")
            self._record_trades()
            self._persist()
            return o

    def set_protection(self, symbol: str, exchange: str, product: str, enabled: bool,
                       stop_loss_pct: float | None = None, target_pct: float | None = None) -> None:
        with self.lock:
            key = _pkey(exchange, symbol, product)
            if not enabled:
                self.managed.pop(key, None)
                self.event("info", f"Auto-protect removed from {symbol}")
                return
            qty = self._require_broker().net_qty(symbol, exchange, product)
            if not qty:
                raise OrderError("no open position to protect")
            px = self.ltp.get(f"{exchange}:{symbol}")
            avg = next((p["avg_price"] for p in self.broker.positions(self.ltp)
                        if p["symbol"] == symbol and p["exchange"] == exchange and p["product"] == product), px)
            self.managed.pop(key, None)
            self._protect(symbol, exchange, product, 1 if qty > 0 else -1, abs(qty), avg, now_ist(), "manual",
                          stop_loss_pct, target_pct)
            self.event("info", f"Auto-protect enabled on {symbol}")
            self._persist()

    def add_funds(self, amount: float, note: str = "") -> dict:
        with self.lock:
            e = self._require_broker().add_funds(amount, note)
            self.event("info", f"Added ₹{amount:,.2f} to account")
            self._persist()
            return e

    def withdraw_funds(self, amount: float, note: str = "") -> dict:
        with self.lock:
            e = self._require_broker().withdraw_funds(amount, note)
            self.event("info", f"Withdrew ₹{amount:,.2f} from account")
            self._persist()
            return e

    def update_settings(self, changes: dict) -> dict:
        with self.lock:
            bad = set(changes) - EDITABLE
            if bad:
                raise ValueError(f"not editable: {', '.join(sorted(bad))}")
            new = self._validated(self.s, changes)
            new.enforce_market_hours = self.s.enforce_market_hours
            overrides = {**self.store.get("settings_overrides", {}), **changes}
            self.s = new
            self.risk = RiskManager(new)
            self.strategy = make_strategy(new.strategy, new.buy_threshold, new.sell_threshold)
            self.signals = {k: v for k, v in self.signals.items() if k in new.watchlist}
            self.store.put("settings_overrides", overrides)
            self.event("info", f"Settings updated: {', '.join(sorted(changes))}")
            self._last_strategy = 0.0
            return self.public_settings()

    def kite_login(self, request_token: str) -> None:
        with self.lock:
            if self.kite is None or not self.s.kite_api_secret:
                raise OrderError("Set KITE_API_KEY and KITE_API_SECRET in .env first")
            data = self.kite.generate_session(request_token, api_secret=self.s.kite_api_secret)
            self.store.put("kite_token", {"token": data["access_token"], "date": now_ist().date().isoformat()})
            self.event("info", "Logged in to Kite")
            self._build()

    # ------------------------------------------------------------------ views
    def public_settings(self) -> dict:
        d = self.s.model_dump()
        return {k: d[k] for k in sorted(EDITABLE)} | {
            "exchange": d["exchange"], "product": d["product"], "candle_interval": d["candle_interval"],
            "data_source": self.data_source, "mode": self.mode, "kite_configured": self.kite is not None,
        }

    def _persist(self) -> None:
        if isinstance(self.broker, PaperBroker):
            self.store.put("paper_state", self.broker.to_state())
            self.store.put("paper_managed", {k: p.to_dict() for k, p in self.managed.items()})

    def _snapshot(self, now: datetime) -> None:
        b = self.broker
        funds = b.funds(self.ltp) if b else {}
        positions = b.positions(self.ltp) if b else []
        for p in positions:
            m = self.managed.get(_pkey(p["exchange"], p["symbol"], p["product"]))
            p["protected"] = bool(m)
            if m:
                p.update(stop_price=m.stop_price, target_price=m.target_price, source=m.source)
        day_pnl = b.day_pnl(self.ltp) if b else 0.0
        if b and self.day == now.date() and time.monotonic() - self._last_equity >= 60:
            self._last_equity = time.monotonic()
            self.store.add_equity(now.isoformat(timespec="seconds"), funds.get("equity", 0), day_pnl, self.mode)
        watch = []
        for sym in self.s.watchlist:
            sig = self.signals.get(sym, {})
            watch.append({"symbol": sym, "exchange": self.s.exchange,
                          "ltp": self.ltp.get(f"{self.s.exchange}:{sym}"), **sig})
        self.snapshot = {
            "time": now.isoformat(timespec="seconds"), "mode": self.mode, "data_source": self.data_source,
            "connected": b is not None, "running": self.running, "kill_switch": self.kill_switch,
            "halted": self.halted, "market_open": self.risk.market_open(now),
            "funds": {k: v for k, v in funds.items() if k != "ledger"}, "day_pnl": day_pnl,
            "peak_day_pnl": round(self.peak_day_pnl, 2), "start_equity": self.start_equity,
            "positions": positions, "watchlist": watch,
            "open_orders": sum(1 for o in b.orders() if o["status"] in OPEN_STATUSES) if b else 0,
            "events": list(self.events)[:30],
        }

