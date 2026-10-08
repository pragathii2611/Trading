import numpy as np

from app import indicators as ind
from app.strategies import Ensemble, make_strategy

from .conftest import make_df


def test_rsi_bounds_and_extremes():
    up = make_df(np.linspace(100, 200, 100))
    assert ind.rsi(up["close"]).iloc[-1] == 100
    rng = np.random.default_rng(0)
    noisy = make_df(100 + rng.normal(0, 1, 300).cumsum())
    r = ind.rsi(noisy["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_macd_and_atr_shapes():
    df = make_df(np.linspace(100, 120, 80))
    m = ind.macd(df["close"])
    assert list(m.columns) == ["macd", "signal", "hist"]
    assert m["macd"].iloc[-1] > 0  # rising prices -> positive MACD
    assert ind.atr(df).iloc[-1] > 0


def test_warmup_holds():
    sig = make_strategy("ensemble").evaluate(make_df([100] * 10))
    assert sig.action == "HOLD" and "warming up" in sig.reason


def _breakout(direction: int):
    """Flat range, then an accelerating move."""
    rng = np.random.default_rng(1)
    base = np.concatenate([np.full(100, 100.0), 100 + direction * np.linspace(0, 1, 50) ** 2 * 12])
    return make_df(base + rng.normal(0, 0.2, 150))


def test_ensemble_buys_breakout_and_sells_breakdown():
    assert Ensemble().evaluate(_breakout(+1)).action == "BUY"
    assert Ensemble().evaluate(_breakout(-1)).action == "SELL"


def test_ensemble_blocks_buy_below_ema50():
    closes = list(np.linspace(130, 100, 120)) + list(np.linspace(100, 104, 8))
    sig = Ensemble().evaluate(make_df(closes))
    assert sig.action != "BUY"
