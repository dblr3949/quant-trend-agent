#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quant_trend.env_loader import load_env_file
from quant_trend.market_data import AlpacaDataClient, IBKRDataClient, MassiveDataClient, fetch_yfinance_quotes, save_quotes
from quant_trend.watchlist import load_watchlist


load_env_file(ROOT / "config" / "openai.env")


def _parse_symbols(value: str | None, watchlist: str | None) -> list[str]:
    symbols: list[str] = []
    if value:
        symbols.extend(symbol.strip().upper() for symbol in value.split(",") if symbol.strip())
    if watchlist:
        symbols.extend(symbol.upper() for symbol in load_watchlist(watchlist))
    seen = set()
    result = []
    for symbol in symbols:
        if symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["massive", "alpaca", "ibkr", "yfinance"], default="massive")
    parser.add_argument("--symbols", help="Comma-separated symbols, e.g. MU,AAOI,SPY,SMH,SOXX,^VIX")
    parser.add_argument("--watchlist")
    parser.add_argument("--feed", help="Alpaca feed, usually sip for consolidated data or iex for basic testing")
    parser.add_argument("--ibkr-host", default=None, help="IBKR TWS/Gateway host, default IBKR_HOST or 127.0.0.1")
    parser.add_argument("--ibkr-port", type=int, default=None, help="IBKR TWS/Gateway port, paper often 7497, live often 7496")
    parser.add_argument("--ibkr-client-id", type=int, default=None, help="IBKR API client id, default IBKR_CLIENT_ID or 81")
    parser.add_argument(
        "--ibkr-market-data-type",
        type=int,
        default=None,
        help="1 live, 2 frozen, 3 delayed, 4 delayed frozen; default IBKR_MARKET_DATA_TYPE or 1",
    )
    parser.add_argument("--ibkr-timeout", type=float, default=8.0, help="Seconds to wait for IBKR snapshot quotes")
    parser.add_argument("--massive-rest-url", default=None, help="Massive/Polygon proxy REST URL, default MASSIVE_REST_URL")
    parser.add_argument("--massive-timeout", type=float, default=10.0, help="Seconds to wait for Massive REST requests")
    parser.add_argument("--output", default="data/live_quotes.json")
    args = parser.parse_args()

    symbols = _parse_symbols(args.symbols, args.watchlist)
    if not symbols:
        raise SystemExit("Provide --symbols or --watchlist")

    if args.provider == "massive":
        client = MassiveDataClient(base_url=args.massive_rest_url, timeout=args.massive_timeout)
        quotes = client.fetch_latest_quotes(symbols)
        if client.last_symbol_errors:
            print("massive symbol errors:")
            for symbol, errors in sorted(client.last_symbol_errors.items()):
                print(f"- {symbol}: {'; '.join(errors)}")
    elif args.provider == "alpaca":
        quotes = AlpacaDataClient(feed=args.feed).fetch_latest_quotes(symbols)
    elif args.provider == "ibkr":
        client = IBKRDataClient(
            host=args.ibkr_host,
            port=args.ibkr_port,
            client_id=args.ibkr_client_id,
            market_data_type=args.ibkr_market_data_type,
            timeout=args.ibkr_timeout,
        )
        quotes = client.fetch_latest_quotes(symbols)
        if client.last_messages:
            print("ibkr messages:")
            for message in client.last_messages:
                print(f"- {message}")
        if client.last_symbol_errors:
            print("ibkr symbol errors:")
            for symbol, errors in sorted(client.last_symbol_errors.items()):
                print(f"- {symbol}: {'; '.join(errors)}")
    else:
        quotes = fetch_yfinance_quotes(symbols)

    missing = sorted(set(symbols) - set(quotes))
    save_quotes(args.output, quotes)
    print(f"saved {args.output} quotes={len(quotes)}")
    if missing:
        print(f"missing quotes: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
