#!/usr/bin/env python3
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_trend.backtest import run_backtest
from quant_trend.data import load_symbol
from quant_trend.watchlist import load_watchlist


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol")
    parser.add_argument("--watchlist")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--cash", type=float, default=100000.0)
    parser.add_argument("--output", default="reports/backtest.csv")
    args = parser.parse_args()

    if not args.symbol and not args.watchlist:
        parser.error("use --symbol or --watchlist")

    symbols = [args.symbol] if args.symbol else load_watchlist(args.watchlist)
    rows = []
    for symbol in symbols:
        try:
            bars = load_symbol(symbol, args.data_dir)
            result = run_backtest(symbol, bars, args.cash)
        except FileNotFoundError:
            print(f"skip {symbol}: missing CSV")
            continue
        except ValueError as exc:
            print(f"skip {symbol}: {exc}")
            continue

        rows.append(
            {
                "symbol": symbol,
                "end_value": round(result.end_value, 2),
                "total_return_pct": round(result.total_return * 100, 2),
                "max_drawdown_pct": round(result.max_drawdown * 100, 2),
                "trades": len(result.trades),
            }
        )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.output).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "end_value", "total_return_pct", "max_drawdown_pct", "trades"])
        writer.writeheader()
        writer.writerows(rows)

    for row in rows:
        print(row)
    print(f"saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
