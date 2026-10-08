"""Trading strategies.

Every strategy scores the latest bar from -1 (strong sell) to +1 (strong buy) and
turns that score into an action using the configured thresholds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from . import indicators as ind

MIN_BARS = 60


@dataclass
class Signal:
    action: str  # "BUY" | "SELL" | "HOLD"
    score: float
    reason: str


def _clip(x: float) -> float:
    if x is None or math.isnan(x):
        return 0.0
    return max(-1.0, min(1.0, x))


class Strategy:
    name = "base"

    def __init__(self, buy_threshold: float = 0.35, sell_threshold: float = -0.35):
        self.buy_threshold = buy_threshold
        self.sell_threshold = sell_threshold

    def score(self, df: pd.DataFrame) -> tuple[float, str]:
        raise NotImplementedError

    def evaluate(self, df: pd.DataFrame) -> Signal:
        if len(df) < MIN_BARS:
            return Signal("HOLD", 0.0, f"warming up ({len(df)}/{MIN_BARS} bars)")
        score, reason = self.score(df)
        score = round(_clip(score), 3)
        if score >= self.buy_threshold:
            action = "BUY"
        elif score <= self.sell_threshold:
            action = "SELL"
        else:
            action = "HOLD"
        return Signal(action, score, reason)


class EmaCross(Strategy):
    """Trend following: fast EMA vs slow EMA, normalised by volatility (ATR)."""

    name = "ema_cross"

    def score(self, df):
        close = df["close"]
        fast, slow = ind.ema(close, 20).iloc[-1], ind.ema(close, 50).iloc[-1]
        vol = ind.atr(df).iloc[-1]
        s = math.tanh((fast - slow) / vol) if vol else 0.0
        trend = "uptrend" if fast > slow else "downtrend"
        return s, f"EMA20 {fast:.2f} vs EMA50 {slow:.2f} ({trend})"


class RsiReversion(Strategy):
    """Mean reversion: oversold -> buy, overbought -> sell."""

    name = "rsi"

    def score(self, df):
        r = ind.rsi(df["close"]).iloc[-1]
        state = "oversold" if r < 30 else "overbought" if r > 70 else "neutral"
        return (50 - r) / 25, f"RSI {r:.1f} ({state})"


class MacdMomentum(Strategy):
    """Momentum: MACD histogram, normalised by ATR."""

    name = "macd"

    def score(self, df):
        m = ind.macd(df["close"])
        hist = m["hist"].iloc[-1]
        vol = ind.atr(df).iloc[-1]
        s = math.tanh(2 * hist / vol) if vol else 0.0
        side = "bullish" if hist > 0 else "bearish"
        return s, f"MACD hist {hist:+.2f} ({side})"


class Ensemble(Strategy):
    """Weighted blend of trend, momentum and mean-reversion.

    Buys are only allowed when price is above the 50 EMA (trade with the trend),
    which filters out many losing counter-trend entries.
    """

    name = "ensemble"
    weights = {"ema_cross": 1.0, "macd": 1.0, "rsi": 0.5}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.parts = [EmaCross(), MacdMomentum(), RsiReversion()]

    def score(self, df):
        total = wsum = 0.0
        reasons = []
        for p in self.parts:
            s, why = p.score(df)
            s = _clip(s)
            w = self.weights[p.name]
            total += w * s
            wsum += w
            reasons.append(why)
        score = total / wsum
        close = df["close"].iloc[-1]
        trend_ok = close > ind.ema(df["close"], 50).iloc[-1]
        if score > 0 and not trend_ok:
            score = min(score, self.buy_threshold - 0.01)
            reasons.append("buy blocked: price below EMA50")
        return score, "; ".join(reasons)


STRATEGIES = {c.name: c for c in (EmaCross, RsiReversion, MacdMomentum, Ensemble)}


def make_strategy(name: str, buy_threshold: float = 0.35, sell_threshold: float = -0.35) -> Strategy:
    return STRATEGIES[name](buy_threshold, sell_threshold)
