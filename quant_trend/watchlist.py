import json
from pathlib import Path


def load_watchlist(path: str | Path) -> list[str]:
    with Path(path).open("r", encoding="utf-8") as f:
        data = json.load(f)
    symbols: list[str] = []
    for market_symbols in data.values():
        symbols.extend(market_symbols)
    return symbols
