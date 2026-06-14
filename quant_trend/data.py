import csv
from datetime import datetime
from pathlib import Path

from .models import Bar


REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def parse_date(value: str):
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unsupported date format: {value}")


def load_bars(path: str | Path) -> list[Bar]:
    path = Path(path)
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path} missing columns: {', '.join(missing)}")

        bars: list[Bar] = []
        for row in reader:
            try:
                bars.append(
                    Bar(
                        date=parse_date(row["date"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Bad row in {path}: {row}") from exc

    bars.sort(key=lambda bar: bar.date)
    return bars


def symbol_to_path(symbol: str, data_dir: str | Path = "data") -> Path:
    return Path(data_dir) / f"{symbol}.csv"


def load_symbol(symbol: str, data_dir: str | Path = "data") -> list[Bar]:
    return load_bars(symbol_to_path(symbol, data_dir))
