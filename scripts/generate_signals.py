#!/usr/bin/env python3
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_trend.data import load_symbol
from quant_trend.strategy import latest_signal
from quant_trend.watchlist import load_watchlist


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watchlist", default="config/watchlists.json")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default="reports/signals.csv")
    args = parser.parse_args()

    rows = []
    for symbol in load_watchlist(args.watchlist):
        try:
            signal = latest_signal(symbol, load_symbol(symbol, args.data_dir))
        except FileNotFoundError:
            print(f"skip {symbol}: missing CSV")
            continue
        except ValueError as exc:
            print(f"skip {symbol}: {exc}")
            continue

        if signal is None:
            print(f"skip {symbol}: not enough history")
            continue

        rows.append(
            {
                "symbol": signal.symbol,
                "date": signal.date.isoformat(),
                "action": signal.action,
                "close": signal.close,
                "score": signal.score,
                "stop": signal.stop,
                "reason": signal.reason,
            }
        )

    rows.sort(key=lambda row: (row["action"] != "buy", -int(row["score"]), row["symbol"]))
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.output).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["symbol", "date", "action", "close", "score", "stop", "reason"])
        writer.writeheader()
        writer.writerows(rows)

    for row in rows:
        print(row)
    print(f"saved {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
