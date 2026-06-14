#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_trend.portfolio import save_portfolio
from quant_trend.portfolio_input import portfolio_from_csv_source, portfolio_from_text


def _read_input_text(args) -> str:
    if args.text:
        return args.text
    if args.text_file:
        return Path(args.text_file).read_text(encoding="utf-8-sig")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("Provide --text, --text-file, --csv, --url, or pipe text into stdin")


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="Local CSV with symbol,shares,avg_cost,thesis_status,conviction columns")
    source.add_argument("--url", help="Online CSV URL, e.g. a published Google Sheet CSV link")
    source.add_argument("--text", help="Natural-language portfolio text")
    source.add_argument("--text-file", help="File containing natural-language portfolio text")
    parser.add_argument("--account-equity", type=float, help="Required for CSV/URL input")
    parser.add_argument("--cash", type=float, default=0.0)
    parser.add_argument("--margin-debit", type=float, default=0.0)
    parser.add_argument("--output", default="config/portfolio.json")
    args = parser.parse_args()

    if args.csv or args.url:
        if args.account_equity is None:
            raise SystemExit("--account-equity is required for --csv/--url input")
        portfolio = portfolio_from_csv_source(args.csv or args.url, args.account_equity, args.cash, args.margin_debit)
    else:
        portfolio = portfolio_from_text(_read_input_text(args))

    save_portfolio(args.output, portfolio)
    print(f"saved {args.output} positions={len(portfolio.positions)} equity={portfolio.account_equity}")
    for symbol, position in sorted(portfolio.positions.items()):
        avg = "" if position.avg_cost is None else f" avg_cost={position.avg_cost}"
        print(f"- {symbol} shares={position.shares}{avg} status={position.thesis_status} conviction={position.conviction}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
