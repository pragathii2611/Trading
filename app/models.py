from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


@dataclass
class Position:
    symbol: str
    qty: int
    entry_price: float
    stop_price: float
    target_price: float
    opened_at: datetime
    side: int = 1            # 1 = long, -1 = short
    product: str = "MIS"
    peak_price: float = 0.0  # best price seen in the trade's favour
    source: str = "bot"      # "bot" or "manual" (auto-protected manual trade)
    exchange: str = "NSE"

    def __post_init__(self):
        if not self.peak_price:
            self.peak_price = self.entry_price

    def unrealized(self, price: float) -> float:
        return (price - self.entry_price) * self.qty * self.side

    def gain_pct(self, price: float) -> float:
        return (price / self.entry_price - 1) * 100 * self.side

    def to_dict(self, price: float | None = None) -> dict:
        d = asdict(self)
        d["opened_at"] = self.opened_at.isoformat()
        if price is not None:
            d["ltp"] = price
            d["unrealized"] = round(self.unrealized(price), 2)
            d["unrealized_pct"] = round(self.gain_pct(price), 2)
        return d


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: str  # BUY | SELL
    qty: int
    price: float
    charges: float = 0.0
    ts: datetime = field(default_factory=now_ist)
