"""Market data providers: simulated (offline demo), Yahoo Finance (free, delayed), Kite (real-time)."""
from __future__ import annotations

import logging
import random
from datetime import timedelta

import pandas as pd

from .models import now_ist

log = logging.getLogger(__name__)

COLUMNS = ["open", "high", "low", "close", "volume"]

# Rough reference prices so the simulator looks realistic.
SEED_PRICES = {
    "RELIANCE": 2900, "TCS": 4100, "INFY": 1800, "HDFCBANK": 1650, "ICICIBANK": 1250,
    "SBIN": 820, "ITC": 470, "LT": 3600, "AXISBANK": 1150, "BHARTIARTL": 1550,
}
INTERVAL_MINUTES = {"minute": 1, "5minute": 5, "15minute": 15, "60minute": 60, "day": 1440}


class DataProvider:
    name = "base"
    realtime = False

    def get_candles(self, symbol: str, exchange: str, interval: str, bars: int = 200) -> pd.DataFrame:
        raise NotImplementedError

    def get_ltp(self, symbols: list[str], exchange: str) -> dict[str, float]:
        raise NotImplementedError

    def tick(self) -> None:
        """Advance time (simulator only)."""


class SimulatedProvider(DataProvider):
    """Random-walk prices with drift regimes, so strategies have trends to find.

    Every `tick()` moves prices; every `ticks_per_bar` ticks a new candle starts.
    """

    name = "simulated"
    realtime = True

    def __init__(self, interval: str = "5minute", ticks_per_bar: int = 6, seed: int | None = None):
        self.rng = random.Random(seed)
        self.interval = timedelta(minutes=INTERVAL_MINUTES.get(interval, 5))
        self.ticks_per_bar = ticks_per_bar
        self._tick_count = 0
        self.bars: dict[str, list[list[float]]] = {}
        self.times: dict[str, list] = {}
        self.drift: dict[str, float] = {}

    def _ensure(self, symbol: str, history: int = 1500):
        if symbol in self.bars:
            return
        price = SEED_PRICES.get(symbol, self.rng.uniform(200, 3000))
        self.drift[symbol] = 0.0
        rows, times = [], []
        t0 = now_ist().replace(second=0, microsecond=0) - self.interval * history
        for i in range(history):
            o = price
            path = [self._step(symbol, o) for _ in range(self.ticks_per_bar)]
            price = path[-1]
            rows.append([o, max(o, *path), min(o, *path), price, self.rng.randint(5_000, 50_000)])
            times.append(t0 + self.interval * i)
        self.bars[symbol], self.times[symbol] = rows, times
        self._start_bar(symbol)

    def _step(self, symbol: str, price: float) -> float:
        if self.rng.random() < 0.02:  # occasionally switch trend regime
            self.drift[symbol] = self.rng.choice([-1, 0, 0, 1]) * 0.0006
        ret = self.drift[symbol] + self.rng.gauss(0, 0.0015)
        return round(max(1.0, price * (1 + ret)), 2)

    def _start_bar(self, symbol: str):
        last = self.bars[symbol][-1][3]
        self.bars[symbol].append([last, last, last, last, 0])
        self.times[symbol].append(self.times[symbol][-1] + self.interval)

    def tick(self):
        self._tick_count += 1
        new_bar = self._tick_count % self.ticks_per_bar == 0
        for sym, rows in self.bars.items():
            bar = rows[-1]
            p = self._step(sym, bar[3])
            bar[1], bar[2], bar[3] = max(bar[1], p), min(bar[2], p), p
            bar[4] += self.rng.randint(500, 8_000)
            if new_bar:
                self._start_bar(sym)
                if len(rows) > 600:
                    del rows[:100]
                    del self.times[sym][:100]

    def get_candles(self, symbol, exchange, interval, bars=200):
        self._ensure(symbol)
        df = pd.DataFrame(self.bars[symbol][-bars:], columns=COLUMNS,
                          index=pd.DatetimeIndex(self.times[symbol][-bars:], name="time"))
        return df

    def get_ltp(self, symbols, exchange):
        out = {}
        for s in symbols:
            self._ensure(s)
            out[s] = self.bars[s][-1][3]
        return out


class YahooProvider(DataProvider):
    """Free Yahoo Finance data (~15 min delayed for NSE/BSE). Good for paper trading."""

    name = "yahoo"
    YF_INTERVAL = {"minute": ("1m", "5d"), "5minute": ("5m", "30d"), "15minute": ("15m", "30d"),
                   "60minute": ("60m", "90d"), "day": ("1d", "2y")}

    def __init__(self):
        import yfinance  # noqa: F401  (fail early if missing)

    @staticmethod
    def _ticker(symbol: str, exchange: str) -> str:
        return f"{symbol}.{'BO' if exchange == 'BSE' else 'NS'}"

    def get_candles(self, symbol, exchange, interval, bars=200):
        import yfinance as yf

        yf_int, period = self.YF_INTERVAL[interval]
        df = yf.download(self._ticker(symbol, exchange), period=period, interval=yf_int,
                         progress=False, auto_adjust=False)
        if df.empty:
            return pd.DataFrame(columns=COLUMNS)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[COLUMNS].dropna()
        df.index = pd.DatetimeIndex(df.index).tz_convert("Asia/Kolkata") if df.index.tz else df.index
        df.index.name = "time"
        return df.tail(bars)

    def get_ltp(self, symbols, exchange):
        import yfinance as yf

        if not symbols:
            return {}
        tickers = [self._ticker(s, exchange) for s in symbols]
        df = yf.download(tickers, period="1d", interval="1m", progress=False, auto_adjust=False,
                         group_by="ticker")
        out = {}
        for s, t in zip(symbols, tickers):
            try:
                col = df[t]["Close"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
                val = col.dropna()
                if len(val):
                    out[s] = round(float(val.iloc[-1]), 2)
            except KeyError:
                log.warning("no quote for %s", t)
        return out


class KiteProvider(DataProvider):
    """Real-time data from Zerodha Kite Connect (needs the historical data add-on for candles)."""

    name = "kite"
    realtime = True

    def __init__(self, kite):
        self.kite = kite
        self._tokens: dict[str, dict[str, int]] = {}

    def _token(self, symbol: str, exchange: str) -> int:
        if exchange not in self._tokens:
            self._tokens[exchange] = {
                i["tradingsymbol"]: i["instrument_token"] for i in self.kite.instruments(exchange)
            }
        return self._tokens[exchange][symbol]

    def get_candles(self, symbol, exchange, interval, bars=200):
        days = max(5, int(bars * INTERVAL_MINUTES[interval] / 375) + 3)
        to = now_ist()
        rows = self.kite.historical_data(self._token(symbol, exchange), to - timedelta(days=days), to, interval)
        df = pd.DataFrame(rows)
        if df.empty:
            return pd.DataFrame(columns=COLUMNS)
        df = df.set_index("date")[COLUMNS]
        df.index.name = "time"
        return df.tail(bars)

    def get_ltp(self, symbols, exchange):
        if not symbols:
            return {}
        q = self.kite.ltp([f"{exchange}:{s}" for s in symbols])
        return {k.split(":", 1)[1]: v["last_price"] for k, v in q.items()}


def make_provider(source: str, interval: str, kite=None) -> DataProvider:
    if source == "simulated":
        return SimulatedProvider(interval)
    if source == "yahoo":
        return YahooProvider()
    if source == "kite":
        if kite is None:
            raise ValueError("Kite data source needs KITE_API_KEY and an access token")
        return KiteProvider(kite)
    raise ValueError(f"unknown data source {source}")


def candles_to_json(df: pd.DataFrame) -> list[dict]:
    out = []
    for ts, r in df.iterrows():
        t = pd.Timestamp(ts)
        if t.tzinfo is not None:
            t = t.tz_convert("Asia/Kolkata").tz_localize(None)
        # lightweight-charts wants unix seconds; shift so IST wall-clock shows on the axis
        out.append({"time": int(t.timestamp()), "open": r.open, "high": r.high,
                    "low": r.low, "close": r.close, "volume": float(r.volume)})
    return out

