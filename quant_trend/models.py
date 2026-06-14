from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    date: date
    action: str
    close: float
    score: int
    stop: float | None
    reason: str


@dataclass(frozen=True)
class Trade:
    date: date
    symbol: str
    side: str
    price: float
    shares: int
    cash_after: float
    reason: str
