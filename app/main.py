"""FastAPI app: REST API + WebSocket for the dashboard."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .backtest import run_backtest
from .brokers import OrderError, OrderRequest
from .config import get_settings
from .data import candles_to_json
from .engine import Engine
from . import indicators as ind

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"


class OrderIn(BaseModel):
    symbol: str
    side: str
    qty: int = Field(gt=0)
    exchange: str = "NSE"
    order_type: str = "MARKET"
    product: str = "MIS"
    price: float = 0.0
    trigger_price: float = 0.0
    validity: str = "DAY"
    auto_protect: bool = False
    stop_loss_pct: float | None = None
    target_pct: float | None = None


class ModifyIn(BaseModel):
    qty: int | None = None
    price: float | None = None
    trigger_price: float | None = None
    order_type: str | None = None


class ExitIn(BaseModel):
    symbol: str
    exchange: str = "NSE"
    product: str = "MIS"


class ProtectIn(ExitIn):
    enabled: bool = True
    stop_loss_pct: float | None = None
    target_pct: float | None = None


class FundsIn(BaseModel):
    amount: float = Field(gt=0)
    note: str = ""


class BacktestIn(BaseModel):
    symbol: str
    exchange: str = "NSE"
    strategy: str | None = None
    capital: float | None = None


def create_app(engine: Engine | None = None, start_loop: bool = True) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = engine or Engine(get_settings())
        task = asyncio.create_task(app.state.engine.run()) if start_loop else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="AI Trader — NSE/BSE", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def _session_token(password: str) -> str:
        return hmac.new(password.encode(), b"ai-trader-session", hashlib.sha256).hexdigest()

    @app.middleware("http")
    async def password_guard(request: Request, call_next):
        password = request.app.state.engine.s.app_password
        if not password or request.url.path.startswith("/static/") or request.url.path == "/api/health":
            return await call_next(request)
        token = _session_token(password)
        if hmac.compare_digest(request.cookies.get("ai_trader", "").encode(), token.encode()):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                given = ""
            if hmac.compare_digest(given.encode(), password.encode()):
                resp = await call_next(request)
                resp.set_cookie("ai_trader", token, httponly=True, samesite="lax", max_age=30 * 86400,
                                secure=request.url.scheme == "https")
                return resp
        return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="AI Trader"'})

    def eng(request: Request) -> Engine:
        return request.app.state.engine

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except (OrderError, ValueError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # ---- status ----------------------------------------------------------
    @app.get("/api/status")
    def status(request: Request):
        e = eng(request)
        if not e.snapshot:
            e.step(force_strategy=True)
        return e.snapshot

    # ---- bot control -----------------------------------------------------
    @app.post("/api/bot/start")
    def bot_start(request: Request):
        guard(eng(request).start_bot)
        return {"ok": True}

    @app.post("/api/bot/stop")
    def bot_stop(request: Request):
        eng(request).stop_bot()
        return {"ok": True}

    @app.post("/api/kill")
    def kill(request: Request):
        eng(request).kill()
        return {"ok": True}

    @app.post("/api/kill/reset")
    def kill_reset(request: Request):
        eng(request).reset_kill()
        return {"ok": True}

    # ---- orders ----------------------------------------------------------

    @app.post("/api/orders")
    def place_order(body: OrderIn, request: Request):
        d = body.model_dump()
        protect = {k: d.pop(k) for k in ("auto_protect", "stop_loss_pct", "target_pct")}
        return guard(eng(request).place_manual, OrderRequest(**d), **protect)


    @app.put("/api/orders/{order_id}")
    def modify_order(order_id: str, body: ModifyIn, request: Request):
        return guard(eng(request).modify_order, order_id, **body.model_dump())

    @app.delete("/api/orders/{order_id}")
    def cancel_order(order_id: str, request: Request):
        return guard(eng(request).cancel_order, order_id)

    @app.get("/api/orders")
    def orders(request: Request):
        e = eng(request)
        return e.broker.orders() if e.broker else []

    # ---- portfolio -------------------------------------------------------
    @app.get("/api/positions")
    def positions(request: Request):
        return eng(request).snapshot.get("positions", [])


    @app.post("/api/positions/exit")
    def exit_position(body: ExitIn, request: Request):
        return guard(eng(request).exit_position, body.symbol, body.exchange, body.product)


    @app.post("/api/positions/protect")
    def protect(body: ProtectIn, request: Request):
        guard(eng(request).set_protection, body.symbol, body.exchange, body.product, body.enabled,
              body.stop_loss_pct, body.target_pct)
        return {"ok": True}

    @app.get("/api/holdings")
    def holdings(request: Request):
        e = eng(request)
        return e.broker.holdings(e.ltp) if e.broker else []

    @app.get("/api/trades")
    def trades(request: Request, limit: int = 200):
        return eng(request).store.trades(limit)

    @app.get("/api/equity")
    def equity(request: Request):
        e = eng(request)
        return e.store.equity(e.mode)

    @app.get("/api/events")
    def events(request: Request):
        return eng(request).store.events(200)

    # ---- funds -----------------------------------------------------------
    @app.get("/api/funds")
    def funds(request: Request):
        e = eng(request)
        if not e.broker:
            return {}
        return {**e.broker.funds(e.ltp), "bot_capital": e.s.bot_capital,
                "can_add_funds": e.broker.supports_funds}


    @app.post("/api/funds/add")
    def add_funds(body: FundsIn, request: Request):
        return guard(eng(request).add_funds, body.amount, body.note)

    @app.post("/api/funds/withdraw")
    def withdraw_funds(body: FundsIn, request: Request):
        return guard(eng(request).withdraw_funds, body.amount, body.note)

    # ---- settings --------------------------------------------------------
    @app.get("/api/settings")
    def get_settings_(request: Request):
        return eng(request).public_settings()

    @app.patch("/api/settings")
    def patch_settings(changes: dict, request: Request):
        try:
            return eng(request).update_settings(changes)
        except Exception as e:
            raise HTTPException(400, str(e)) from e

    # ---- market data -----------------------------------------------------
    @app.get("/api/candles/{symbol}")
    def candles(symbol: str, request: Request, exchange: str = "NSE"):
        e = eng(request)
        df = guard(e.data.get_candles, symbol.upper(), exchange, e.s.candle_interval, 300)
        if df.empty:
            return {"candles": [], "ema20": [], "ema50": []}
        c = candles_to_json(df)
        times = [x["time"] for x in c]
        e20, e50 = ind.ema(df["close"], 20), ind.ema(df["close"], 50)
        return {
            "candles": c,
            "ema20": [{"time": t, "value": round(v, 2)} for t, v in zip(times, e20)],
            "ema50": [{"time": t, "value": round(v, 2)} for t, v in zip(times, e50)],
        }


    @app.post("/api/backtest")
    def backtest(body: BacktestIn, request: Request):
        e = eng(request)
        df = guard(e.data.get_candles, body.symbol.upper(), body.exchange, e.s.candle_interval, 2000)
        if len(df) < 80:
            raise HTTPException(400, "not enough history for a backtest")
        return guard(run_backtest, df, e.s, body.strategy, body.capital)

    # ---- Kite login ------------------------------------------------------
    @app.get("/api/kite/login")
    def kite_login_url(request: Request):
        e = eng(request)
        if e.kite is None:
            raise HTTPException(400, "Set KITE_API_KEY and KITE_API_SECRET in .env first")
        return {"url": e.kite.login_url()}

    @app.get("/kite/callback")
    def kite_callback(request: Request, request_token: str = "", status: str = ""):
        if status != "success" or not request_token:
            raise HTTPException(400, "Kite login failed")
        guard(eng(request).kite_login, request_token)
        return RedirectResponse("/")

    # ---- live updates ----------------------------------------------------
    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        e: Engine = socket.app.state.engine
        if e.s.app_password and not hmac.compare_digest(
            socket.cookies.get("ai_trader", "").encode(), _session_token(e.s.app_password).encode()
        ):
            await socket.close(code=1008)
            return
        await socket.accept()
        last = None
        try:
            while True:
                snap = e.snapshot
                if snap and snap is not last:
                    last = snap
                    await socket.send_json(snap)
                await asyncio.sleep(1)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


app = create_app()
