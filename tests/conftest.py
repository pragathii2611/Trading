import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from app.data import DataProvider


def make_df(closes, start="2026-01-05 09:15") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({"open": closes, "high": closes * 1.002, "low": closes * 0.998,
                         "close": closes, "volume": 1000}, index=idx)


class FakeProvider(DataProvider):
    """Prices are set directly by the test."""

    name = "fake"

    def __init__(self, prices: dict[str, float], closes: dict[str, list[float]] | None = None):
        self.prices = prices
        self.closes = closes or {}

    def get_ltp(self, symbols, exchange):
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def get_candles(self, symbol, exchange, interval, bars=200):
        return make_df(self.closes.get(symbol, [self.prices.get(symbol, 100.0)] * 100))


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, db_path=str(tmp_path / "t.db"), data_source="simulated",
                    watchlist=["AAA"], starting_capital=100_000, bot_capital=100_000)
