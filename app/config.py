"""Application settings, loaded from environment variables / .env file."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THE_RISKS"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Mode -------------------------------------------------------------
    trading_mode: Literal["paper", "live"] = "paper"
    # Live trading only activates when this equals LIVE_CONFIRM_PHRASE.
    live_trading_confirm: str = ""
    data_source: Literal["simulated", "yahoo", "kite"] = "simulated"

    # --- Universe ---------------------------------------------------------
    exchange: Literal["NSE", "BSE"] = "NSE"
    watchlist: list[str] = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
    candle_interval: Literal["minute", "5minute", "15minute", "60minute", "day"] = "5minute"
    poll_seconds: int = 60
    autostart: bool = False

    # --- Strategy ---------------------------------------------------------
    strategy: Literal["ensemble", "ema_cross", "rsi", "macd"] = "ensemble"
    buy_threshold: float = 0.35
    sell_threshold: float = -0.35

    # --- Capital & risk ---------------------------------------------------
    # Capital the bot is allowed to use (in live mode this caps exposure even if
    # the broker account holds more).
    starting_capital: float = 100_000.0
    risk_per_trade_pct: float = 1.0       # % of equity lost if the stop is hit
    max_position_pct: float = 20.0        # max % of equity in one stock
    max_open_positions: int = 5
    daily_loss_limit_pct: float = 2.0     # halt new entries after this daily loss
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 3.0
    cooldown_minutes: int = 30            # wait after exiting before re-entering
    product: Literal["MIS", "CNC"] = "MIS"  # MIS = intraday, CNC = delivery
    square_off_time: str = "15:15"        # IST; MIS positions closed after this
    enforce_market_hours: bool = True
    slippage_bps: float = 5.0             # paper-trading fill slippage

    # --- Zerodha Kite Connect --------------------------------------------
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_access_token: str = ""

    db_path: str = "trading.db"

    @field_validator("watchlist", mode="before")
    @classmethod
    def _split_watchlist(cls, v):
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return v  # let pydantic parse JSON
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return [s.upper() for s in v]

    @property
    def live_enabled(self) -> bool:
        return self.trading_mode == "live" and self.live_trading_confirm == LIVE_CONFIRM_PHRASE


@lru_cache
def get_settings() -> Settings:
    return Settings()
