#!/usr/bin/env python3
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_trend.market_data import AlpacaDataClient


def _row_value(row, column: str):
    value = row[column]
    if hasattr(value, "iloc"):
        return value.iloc[0]
    return value


def download_us(symbol: str, start: str, end: str | None, provider: str = "yfinance", feed: str | None = None):
    if provider == "alpaca":
        return AlpacaDataClient(feed=feed).fetch_daily_bars(symbol, start, end)

    try:
        import yfinance as yf
    except ImportError as exc:
        raise SystemExit("Please install yfinance first: python3 -m pip install yfinance") from exc

    frame = yf.download(symbol, start=start, end=end, auto_adjust=False, progress=False)
    rows = []
    for date, row in frame.iterrows():
        rows.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "open": float(_row_value(row, "Open")),
                "high": float(_row_value(row, "High")),
                "low": float(_row_value(row, "Low")),
                "close": float(_row_value(row, "Close")),
                "volume": float(_row_value(row, "Volume")),
            }
        )
    return rows


def download_cn(symbol: str, start: str, end: str | None):
    try:
        import akshare as ak
    except ImportError as exc:
        raise SystemExit("Please install akshare first: python3 -m pip install akshare") from exc

    raw_symbol = symbol.split(".")[0]
    frame = ak.stock_zh_a_hist(symbol=raw_symbol, period="daily", start_date=start.replace("-", ""), end_date=(end or "").replace("-", ""), adjust="qfq")
    rows = []
    for _, row in frame.iterrows():
        rows.append(
            {
                "date": str(row["日期"]),
                "open": float(row["开盘"]),
                "high": float(row["最高"]),
                "low": float(row["最低"]),
                "close": float(row["收盘"]),
                "volume": float(row["成交量"]),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", choices=["us", "cn"], required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--provider", choices=["yfinance", "alpaca"], default="yfinance")
    parser.add_argument("--feed", help="Alpaca feed, usually sip or iex")
    args = parser.parse_args()

    rows = download_us(args.symbol, args.start, args.end, args.provider, args.feed) if args.market == "us" else download_cn(args.symbol, args.start, args.end)
    output_symbol = args.symbol if args.market == "us" else (args.symbol if "." in args.symbol else f"{args.symbol}.SH")
    output = Path(args.data_dir) / f"{output_symbol}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"saved {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
