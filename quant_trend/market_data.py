import json
import os
import ssl
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    bid: float | None = None
    ask: float | None = None
    asof: str | None = None
    source: str = "unknown"


@dataclass(frozen=True)
class IntradayBar:
    symbol: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    average: float | None = None
    bar_count: int | None = None
    source: str = "unknown"


def _parse_asof(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def quote_age_minutes(quote: Quote, now: datetime | None = None) -> float | None:
    asof = _parse_asof(quote.asof)
    if asof is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - asof).total_seconds() / 60.0)


def load_quotes(path: str | Path) -> dict[str, Quote]:
    with Path(path).open("r", encoding="utf-8") as f:
        payload = json.load(f)

    raw_quotes = payload.get("quotes", payload)
    if isinstance(raw_quotes, list):
        items = raw_quotes
    elif isinstance(raw_quotes, dict):
        items = []
        for symbol, raw in raw_quotes.items():
            if isinstance(raw, dict):
                items.append({"symbol": symbol, **raw})
            else:
                items.append({"symbol": symbol, "price": raw})
    else:
        raise ValueError(f"Unsupported quote file format: {path}")

    quotes: dict[str, Quote] = {}
    for item in items:
        symbol = str(item["symbol"]).upper()
        quotes[symbol] = Quote(
            symbol=symbol,
            price=float(item["price"]),
            bid=float(item["bid"]) if item.get("bid") not in (None, "") else None,
            ask=float(item["ask"]) if item.get("ask") not in (None, "") else None,
            asof=item.get("asof") or payload.get("asof"),
            source=item.get("source") or payload.get("source") or "file",
        )
    return quotes


def save_quotes(path: str | Path, quotes: dict[str, Quote]) -> None:
    output = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "quotes": [asdict(quotes[symbol]) for symbol in sorted(quotes)],
    }
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)


def _urlopen_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    context = None
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        context = ssl.create_default_context()
    with urlopen(request, timeout=20, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_yahoo_chart(symbol: str, range_value: str = "2y", interval: str = "1d") -> dict:
    encoded = quote(symbol, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={range_value}&interval={interval}&events=history"
    payload = _urlopen_json(url)
    result = payload.get("chart", {}).get("result") or []
    if not result:
        error = payload.get("chart", {}).get("error")
        raise RuntimeError(f"Yahoo chart returned no data for {symbol}: {error}")
    return result[0]


def fetch_yahoo_chart_daily_rows(symbol: str, range_value: str = "2y") -> list[dict[str, float | str]]:
    result = fetch_yahoo_chart(symbol, range_value=range_value, interval="1d")
    timestamps = result.get("timestamp") or []
    quote_payload = (result.get("indicators", {}).get("quote") or [{}])[0]
    rows = []
    for index, timestamp in enumerate(timestamps):
        try:
            open_price = quote_payload.get("open", [])[index]
            high = quote_payload.get("high", [])[index]
            low = quote_payload.get("low", [])[index]
            close = quote_payload.get("close", [])[index]
            volume = quote_payload.get("volume", [])[index]
        except IndexError:
            continue
        if None in (open_price, high, low, close, volume):
            continue
        rows.append(
            {
                "date": datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat(),
                "open": float(open_price),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(volume),
            }
        )
    return rows


def fetch_yahoo_chart_intraday_rows(symbol: str, range_value: str = "1d", interval: str = "5m") -> list[IntradayBar]:
    result = fetch_yahoo_chart(symbol, range_value=range_value, interval=interval)
    timestamps = result.get("timestamp") or []
    quote_payload = (result.get("indicators", {}).get("quote") or [{}])[0]
    rows: list[IntradayBar] = []
    for index, timestamp in enumerate(timestamps):
        try:
            open_price = quote_payload.get("open", [])[index]
            high = quote_payload.get("high", [])[index]
            low = quote_payload.get("low", [])[index]
            close = quote_payload.get("close", [])[index]
            volume = quote_payload.get("volume", [])[index]
        except IndexError:
            continue
        if None in (open_price, high, low, close):
            continue
        rows.append(
            IntradayBar(
                symbol=symbol.upper(),
                timestamp=datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                open=float(open_price),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=float(volume or 0),
                source=f"yahoo_chart:{interval}",
            )
        )
    return rows


def fetch_yahoo_chart_quotes(symbols: list[str]) -> dict[str, Quote]:
    quotes: dict[str, Quote] = {}
    for symbol in symbols:
        result = fetch_yahoo_chart(symbol, range_value="5d", interval="1d")
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            rows = fetch_yahoo_chart_daily_rows(symbol, range_value="5d")
            if not rows:
                continue
            price = rows[-1]["close"]
        quotes[symbol.upper()] = Quote(
            symbol=symbol.upper(),
            price=float(price),
            asof=None,
            source="yahoo_chart",
        )
    return quotes


def _float_or_none(value) -> float | None:
    if value in (None, "", "NaN"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _polygon_timestamp_to_datetime(value) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1_000_000_000_000_000:
        number = number / 1_000_000_000
    elif number > 1_000_000_000_000:
        number = number / 1000
    try:
        return datetime.fromtimestamp(number, timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _polygon_timestamp_to_iso(value) -> str | None:
    parsed = _polygon_timestamp_to_datetime(value)
    return parsed.isoformat() if parsed else None


def _polygon_bar_date(value) -> str:
    parsed = _polygon_timestamp_to_datetime(value)
    if not parsed:
        return str(value)[:10]
    return parsed.date().isoformat()


def _polygon_market_date(value) -> str:
    parsed = _polygon_timestamp_to_datetime(value)
    if not parsed:
        return str(value)[:10]
    if ZoneInfo is None:
        return parsed.date().isoformat()
    return parsed.astimezone(ZoneInfo("America/New_York")).date().isoformat()


def _bar_size_to_polygon(bar_size: str) -> tuple[int, str]:
    normalized = str(bar_size or "").strip().lower()
    mapping = {
        "1 min": (1, "minute"),
        "1 mins": (1, "minute"),
        "1 minute": (1, "minute"),
        "5 min": (5, "minute"),
        "5 mins": (5, "minute"),
        "5 minutes": (5, "minute"),
        "15 min": (15, "minute"),
        "15 mins": (15, "minute"),
        "15 minutes": (15, "minute"),
        "30 min": (30, "minute"),
        "30 mins": (30, "minute"),
        "30 minutes": (30, "minute"),
    }
    return mapping.get(normalized, (5, "minute"))


def _duration_to_calendar_days(duration: str) -> int:
    match = str(duration or "1 D").strip().upper().split()
    if len(match) >= 2:
        try:
            amount = int(float(match[0]))
        except ValueError:
            amount = 1
        unit = match[1][0]
        if unit == "W":
            return max(1, amount * 7)
        if unit == "M":
            return max(1, amount * 31)
        if unit == "Y":
            return max(1, amount * 366)
        return max(1, amount)
    return 1


class MassiveDataClient:
    """Massive/Polygon proxy REST client for market data.

    The proxy follows Polygon REST paths and authenticates with X-Proxy-Key.
    This app uses REST for on-demand runs instead of opening a persistent
    WebSocket so it does not occupy the single WebSocket connection allowed by
    the standard plan.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 10.0,
    ):
        self.api_key = api_key or os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_PROXY_KEY")
        self.base_url = (base_url or os.getenv("MASSIVE_REST_URL") or "http://44.219.45.87:8081").rstrip("/")
        self.timeout = timeout
        self.last_messages: list[str] = []
        self.last_symbol_errors: dict[str, list[str]] = {}
        if not self.api_key:
            raise ValueError("Missing MASSIVE_API_KEY for Massive/Polygon market data")

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> dict:
        query = f"?{urlencode(params or {})}" if params else ""
        url = f"{self.base_url}{path}{query}"
        request = Request(
            url,
            headers={
                "User-Agent": "quant-trend-agent/1.0",
                "X-Proxy-Key": self.api_key,
                "X-API-KEY": self.api_key,
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _snapshot_quote(self, symbol: str, payload: dict) -> Quote | None:
        ticker = payload.get("ticker") or payload.get("results") or payload
        if not isinstance(ticker, dict):
            return None
        last_trade = ticker.get("lastTrade") or ticker.get("last_trade") or ticker.get("lastTradeDetails") or {}
        last_quote = ticker.get("lastQuote") or ticker.get("last_quote") or {}
        day = ticker.get("day") or {}
        minute = ticker.get("min") or ticker.get("minute") or {}
        prev_day = ticker.get("prevDay") or ticker.get("prev_day") or {}
        bid = _float_or_none(last_quote.get("p") or last_quote.get("bid") or last_quote.get("bid_price"))
        ask = _float_or_none(last_quote.get("P") or last_quote.get("ask") or last_quote.get("ask_price"))
        midpoint = (bid + ask) / 2.0 if bid and ask else None
        candidates = [
            ("last", _float_or_none(last_trade.get("p") or last_trade.get("price"))),
            ("day", _float_or_none(day.get("c") or day.get("close"))),
            ("minute", _float_or_none(minute.get("c") or minute.get("close"))),
            ("midpoint", midpoint),
            ("ask", ask),
            ("bid", bid),
            ("prev_close", _float_or_none(prev_day.get("c") or prev_day.get("close"))),
        ]
        price_kind = "unknown"
        price = None
        for candidate_kind, candidate_price in candidates:
            if candidate_price is not None:
                price_kind = candidate_kind
                price = candidate_price
                break
        if price is None:
            return None
        return Quote(
            symbol=symbol.upper(),
            price=float(price),
            bid=bid,
            ask=ask,
            asof=datetime.now(timezone.utc).isoformat(),
            source=f"massive:snapshot:{price_kind}",
        )

    def _last_trade_quote(self, symbol: str) -> Quote | None:
        trade_payload = self._get_json(f"/v2/last/trade/{quote(symbol.upper(), safe='')}")
        trade = trade_payload.get("results") or trade_payload.get("last") or {}
        price = _float_or_none(trade.get("p") or trade.get("price"))
        if price is None:
            return None
        return Quote(
            symbol=symbol.upper(),
            price=price,
            asof=datetime.now(timezone.utc).isoformat(),
            source="massive:last_trade",
        )

    def fetch_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        self.last_messages = []
        self.last_symbol_errors = {}
        for symbol in [item.upper() for item in symbols]:
            try:
                payload = self._get_json(f"/v2/snapshot/locale/us/markets/stocks/tickers/{quote(symbol, safe='')}")
                quote_item = self._snapshot_quote(symbol, payload)
                if quote_item is None:
                    quote_item = self._last_trade_quote(symbol)
                if quote_item is not None:
                    quotes[symbol] = quote_item
                else:
                    self.last_symbol_errors[symbol] = ["Massive snapshot returned no usable price"]
            except Exception as exc:
                try:
                    quote_item = self._last_trade_quote(symbol)
                    if quote_item is not None:
                        quotes[symbol] = quote_item
                        continue
                except Exception as fallback_exc:
                    self.last_symbol_errors[symbol] = [f"snapshot: {exc}", f"last_trade: {fallback_exc}"]
                    continue
                self.last_symbol_errors[symbol] = [str(exc)]
        return quotes

    def fetch_daily_bars(self, symbol: str, start: str, end: str | None = None) -> list[dict[str, float | str]]:
        end_value = end or datetime.now(timezone.utc).date().isoformat()
        payload = self._get_json(
            f"/v2/aggs/ticker/{quote(symbol.upper(), safe='')}/range/1/day/{start}/{end_value}",
            {"adjusted": "true", "sort": "asc", "limit": "50000"},
        )
        rows = []
        for raw in payload.get("results") or []:
            if not all(key in raw for key in ("o", "h", "l", "c")):
                continue
            rows.append(
                {
                    "date": _polygon_bar_date(raw.get("t")),
                    "open": float(raw["o"]),
                    "high": float(raw["h"]),
                    "low": float(raw["l"]),
                    "close": float(raw["c"]),
                    "volume": float(raw.get("v") or 0),
                }
            )
        return rows

    def fetch_intraday_bars(
        self,
        symbols: list[str],
        duration: str = "1 D",
        bar_size: str = "5 mins",
    ) -> dict[str, list[IntradayBar]]:
        multiplier, timespan = _bar_size_to_polygon(bar_size)
        calendar_days = max(5, _duration_to_calendar_days(duration) + 4)
        end_date = datetime.now(timezone.utc).date()
        start_date = end_date - timedelta(days=calendar_days)
        result: dict[str, list[IntradayBar]] = {}
        self.last_symbol_errors = {}
        for symbol in [item.upper() for item in symbols]:
            try:
                payload = self._get_json(
                    f"/v2/aggs/ticker/{quote(symbol, safe='')}/range/{multiplier}/{timespan}/{start_date.isoformat()}/{end_date.isoformat()}",
                    {"adjusted": "true", "sort": "asc", "limit": "50000"},
                )
                bar_pairs = []
                for raw in payload.get("results") or []:
                    if not all(key in raw for key in ("o", "h", "l", "c")):
                        continue
                    bar_pairs.append(
                        (
                            IntradayBar(
                                symbol=symbol,
                                timestamp=_polygon_timestamp_to_iso(raw.get("t")) or str(raw.get("t")),
                                open=float(raw["o"]),
                                high=float(raw["h"]),
                                low=float(raw["l"]),
                                close=float(raw["c"]),
                                volume=float(raw.get("v") or 0),
                                average=_float_or_none(raw.get("vw")),
                                bar_count=int(raw["n"]) if raw.get("n") is not None else None,
                                source=f"massive:{multiplier}:{timespan}",
                            ),
                            raw,
                        )
                    )
                if bar_pairs:
                    latest_day = max(_polygon_market_date(raw.get("t")) for _, raw in bar_pairs if raw.get("t") is not None)
                    result[symbol] = [
                        bar
                        for bar, raw in bar_pairs
                        if raw.get("t") is not None and _polygon_market_date(raw.get("t")) == latest_day
                    ]
                else:
                    self.last_symbol_errors[symbol] = ["Massive minute aggregates returned no bars"]
            except Exception as exc:
                self.last_symbol_errors[symbol] = [str(exc)]
        return result


class AlpacaDataClient:
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        feed: str | None = None,
        base_url: str = "https://data.alpaca.markets",
    ):
        self.api_key = api_key or os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
        self.api_secret = api_secret or os.getenv("ALPACA_API_SECRET") or os.getenv("APCA_API_SECRET_KEY")
        self.feed = feed or os.getenv("ALPACA_DATA_FEED", "sip")
        self.base_url = base_url.rstrip("/")
        if not self.api_key or not self.api_secret:
            raise ValueError("Missing ALPACA_API_KEY/ALPACA_API_SECRET for market data")

    def _get_json(self, path: str, params: dict[str, str]) -> dict:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.api_secret,
            },
        )
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    def fetch_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        normalized = [symbol.upper() for symbol in symbols]
        payload = self._get_json(
            "/v2/stocks/quotes/latest",
            {"symbols": ",".join(normalized), "feed": self.feed},
        )
        raw_quotes = payload.get("quotes", {})
        quotes: dict[str, Quote] = {}
        for symbol in normalized:
            raw = raw_quotes.get(symbol)
            if not raw:
                continue
            bid = float(raw["bp"]) if raw.get("bp") not in (None, 0, "0") else None
            ask = float(raw["ap"]) if raw.get("ap") not in (None, 0, "0") else None
            if bid and ask:
                price = (bid + ask) / 2.0
            else:
                price = ask or bid
            if price is None:
                continue
            quotes[symbol] = Quote(
                symbol=symbol,
                price=float(price),
                bid=bid,
                ask=ask,
                asof=raw.get("t"),
                source=f"alpaca:{self.feed}",
            )
        return quotes

    def fetch_daily_bars(self, symbol: str, start: str, end: str | None = None) -> list[dict[str, float | str]]:
        params = {
            "symbols": symbol.upper(),
            "timeframe": "1Day",
            "start": start,
            "adjustment": "raw",
            "feed": self.feed,
            "limit": "10000",
            "sort": "asc",
        }
        if end:
            params["end"] = end
        payload = self._get_json("/v2/stocks/bars", params)
        rows = []
        for raw in payload.get("bars", {}).get(symbol.upper(), []):
            rows.append(
                {
                    "date": str(raw["t"])[:10],
                    "open": float(raw["o"]),
                    "high": float(raw["h"]),
                    "low": float(raw["l"]),
                    "close": float(raw["c"]),
                    "volume": float(raw["v"]),
                }
            )
        return rows


class _IbkrTickerState:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bid: float | None = None
        self.ask: float | None = None
        self.last: float | None = None
        self.close: float | None = None
        self.asof: str | None = None
        self.market_data_type: int | None = None
        self.ended = False
        self.errors: list[str] = []

    def set_price(self, tick_type: int, price: float) -> None:
        if price <= 0:
            return
        if tick_type in {1, 66}:
            self.bid = price
        elif tick_type in {2, 67}:
            self.ask = price
        elif tick_type in {4, 68}:
            self.last = price
        elif tick_type in {9, 75}:
            self.close = price
        else:
            return
        self.asof = datetime.now(timezone.utc).isoformat()

    def to_quote(self) -> Quote | None:
        midpoint = (self.bid + self.ask) / 2.0 if self.bid and self.ask else None
        if self.last is not None:
            price = self.last
            price_kind = "last"
        elif midpoint is not None:
            price = midpoint
            price_kind = "midpoint"
        elif self.ask is not None:
            price = self.ask
            price_kind = "ask"
        elif self.bid is not None:
            price = self.bid
            price_kind = "bid"
        else:
            price = self.close
            price_kind = "close"
        if price is None:
            return None
        label = {
            1: "live",
            2: "frozen",
            3: "delayed",
            4: "delayed_frozen",
        }.get(self.market_data_type or 1, str(self.market_data_type or 1))
        return Quote(
            symbol=self.symbol,
            price=float(price),
            bid=self.bid,
            ask=self.ask,
            asof=self.asof or datetime.now(timezone.utc).isoformat(),
            source=f"ibkr:{label}:{price_kind}",
        )


class _IbkrHistoricalState:
    def __init__(self, symbol: str, source: str):
        self.symbol = symbol
        self.source = source
        self.bars: list[IntradayBar] = []
        self.ended = False
        self.errors: list[str] = []

    def add_bar(self, bar) -> None:
        timestamp = _ibkr_bar_timestamp(bar.date)
        average = None if getattr(bar, "average", None) in (None, -1) else float(bar.average)
        bar_count = None if getattr(bar, "barCount", None) in (None, -1) else int(bar.barCount)
        self.bars.append(
            IntradayBar(
                symbol=self.symbol,
                timestamp=timestamp,
                open=float(bar.open),
                high=float(bar.high),
                low=float(bar.low),
                close=float(bar.close),
                volume=float(getattr(bar, "volume", 0) or 0),
                average=average,
                bar_count=bar_count,
                source=self.source,
            )
        )


def _load_ibapi():
    try:
        from ibapi.client import EClient
        from ibapi.common import TickerId
        from ibapi.contract import Contract
        from ibapi.wrapper import EWrapper
    except ImportError as exc:
        raise SystemExit("Please install ibapi first: python3 -m pip install ibapi") from exc
    return EClient, EWrapper, Contract, TickerId


def _ibkr_contract_for_symbol(symbol: str, Contract, exchange: str, currency: str):
    contract = Contract()
    if symbol == "^VIX":
        contract.symbol = "VIX"
        contract.secType = "IND"
        contract.exchange = "CBOE"
        contract.currency = currency
    else:
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = exchange
        contract.currency = currency
    return contract


def _ibkr_bar_timestamp(value) -> str:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    if text.isdigit():
        return datetime.fromtimestamp(int(text), timezone.utc).isoformat()
    return text


class IBKRDataClient:
    """Market-data-only IBKR TWS/Gateway client.

    This client intentionally uses only market-data APIs:
    reqMktData/cancelMktData and reqHistoricalData/cancelHistoricalData. It
    does not request account, position, order, execution, or portfolio state.
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        client_id: int | None = None,
        market_data_type: int | None = None,
        timeout: float = 8.0,
        exchange: str = "SMART",
        currency: str = "USD",
    ):
        self.host = host or os.getenv("IBKR_HOST", "127.0.0.1")
        self.port = int(port or os.getenv("IBKR_PORT", "7497"))
        self.client_id = int(client_id or os.getenv("IBKR_CLIENT_ID", "81"))
        self.market_data_type = int(market_data_type or os.getenv("IBKR_MARKET_DATA_TYPE", "1"))
        self.timeout = timeout
        self.exchange = exchange
        self.currency = currency
        self.last_messages: list[str] = []
        self.last_symbol_errors: dict[str, list[str]] = {}

    def fetch_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        EClient, EWrapper, Contract, _ = _load_ibapi()

        class App(EWrapper, EClient):
            def __init__(self):
                EClient.__init__(self, self)
                self.ready = threading.Event()
                self.lock = threading.Lock()
                self.tickers: dict[int, _IbkrTickerState] = {}
                self.symbol_to_req_id: dict[str, int] = {}
                self.messages: list[str] = []

            def nextValidId(self, orderId: int):
                self.ready.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
                with self.lock:
                    message = f"{errorCode}: {errorString}"
                    if reqId in self.tickers:
                        self.tickers[reqId].errors.append(message)
                    else:
                        self.messages.append(message)

            def marketDataType(self, reqId: int, marketDataType: int):
                with self.lock:
                    if reqId in self.tickers:
                        self.tickers[reqId].market_data_type = marketDataType

            def tickPrice(self, reqId, tickType, price, attrib):
                with self.lock:
                    if reqId in self.tickers:
                        self.tickers[reqId].set_price(int(tickType), float(price))

            def tickSnapshotEnd(self, reqId: int):
                with self.lock:
                    if reqId in self.tickers:
                        self.tickers[reqId].ended = True

        quotes: dict[str, Quote] = {}
        app = App()
        thread: threading.Thread | None = None
        try:
            app.connect(self.host, self.port, self.client_id)
            thread = threading.Thread(target=app.run, daemon=True)
            thread.start()

            if not app.ready.wait(timeout=self.timeout):
                raise TimeoutError(f"Timed out connecting to IBKR TWS/Gateway at {self.host}:{self.port}")

            app.reqMarketDataType(self.market_data_type)

            normalized = [symbol.upper() for symbol in symbols]
            for offset, symbol in enumerate(normalized, start=1):
                contract = _ibkr_contract_for_symbol(symbol, Contract, self.exchange, self.currency)
                req_id = 1000 + offset
                with app.lock:
                    app.tickers[req_id] = _IbkrTickerState(symbol)
                    app.symbol_to_req_id[symbol] = req_id
                app.reqMktData(req_id, contract, "", True, False, [])

            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                with app.lock:
                    if all(state.ended or state.to_quote() is not None for state in app.tickers.values()):
                        break
                time.sleep(0.1)

            with app.lock:
                for _, state in app.tickers.items():
                    quote = state.to_quote()
                    if quote is not None:
                        quotes[state.symbol] = quote
                    if state.errors:
                        self.last_symbol_errors[state.symbol] = list(state.errors)
                self.last_messages = list(app.messages)
            return quotes
        finally:
            try:
                with app.lock:
                    req_ids = list(app.tickers)
                for req_id in req_ids:
                    try:
                        app.cancelMktData(req_id)
                    except Exception:
                        pass
                with app.lock:
                    self.last_messages = list(app.messages)
                    self.last_symbol_errors = {
                        state.symbol: list(state.errors)
                        for state in app.tickers.values()
                        if state.errors
                    }
            finally:
                try:
                    app.disconnect()
                finally:
                    if thread is not None:
                        thread.join(timeout=2.0)

    def fetch_intraday_bars(
        self,
        symbols: list[str],
        duration: str = "1 D",
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        timeout: float | None = None,
    ) -> dict[str, list[IntradayBar]]:
        EClient, EWrapper, Contract, _ = _load_ibapi()

        class App(EWrapper, EClient):
            def __init__(self):
                EClient.__init__(self, self)
                self.ready = threading.Event()
                self.lock = threading.Lock()
                self.historical: dict[int, _IbkrHistoricalState] = {}
                self.messages: list[str] = []

            def nextValidId(self, orderId: int):
                self.ready.set()

            def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
                with self.lock:
                    message = f"{errorCode}: {errorString}"
                    if reqId in self.historical:
                        self.historical[reqId].errors.append(message)
                    else:
                        self.messages.append(message)

            def historicalData(self, reqId, bar):
                with self.lock:
                    if reqId in self.historical:
                        self.historical[reqId].add_bar(bar)

            def historicalDataEnd(self, reqId: int, start: str, end: str):
                with self.lock:
                    if reqId in self.historical:
                        self.historical[reqId].ended = True

        timeout_value = float(timeout if timeout is not None else max(self.timeout, 12.0))
        result: dict[str, list[IntradayBar]] = {}
        app = App()
        thread: threading.Thread | None = None
        try:
            app.connect(self.host, self.port, self.client_id)
            thread = threading.Thread(target=app.run, daemon=True)
            thread.start()

            if not app.ready.wait(timeout=timeout_value):
                raise TimeoutError(f"Timed out connecting to IBKR TWS/Gateway at {self.host}:{self.port}")

            normalized = [symbol.upper() for symbol in symbols]
            source = f"ibkr:historical:{bar_size}"
            for offset, symbol in enumerate(normalized, start=1):
                contract = _ibkr_contract_for_symbol(symbol, Contract, self.exchange, self.currency)
                req_id = 2000 + offset
                with app.lock:
                    app.historical[req_id] = _IbkrHistoricalState(symbol, source)
                app.reqHistoricalData(
                    req_id,
                    contract,
                    "",
                    duration,
                    bar_size,
                    what_to_show,
                    1 if use_rth else 0,
                    2,
                    False,
                    [],
                )

            deadline = time.monotonic() + timeout_value
            while time.monotonic() < deadline:
                with app.lock:
                    if all(state.ended for state in app.historical.values()):
                        break
                time.sleep(0.15)

            with app.lock:
                for state in app.historical.values():
                    if state.bars:
                        result[state.symbol] = list(state.bars)
                    if state.errors:
                        self.last_symbol_errors[state.symbol] = list(state.errors)
                self.last_messages = list(app.messages)
            return result
        finally:
            try:
                with app.lock:
                    req_ids = list(app.historical)
                for req_id in req_ids:
                    try:
                        app.cancelHistoricalData(req_id)
                    except Exception:
                        pass
                with app.lock:
                    self.last_messages = list(app.messages)
                    self.last_symbol_errors = {
                        state.symbol: list(state.errors)
                        for state in app.historical.values()
                        if state.errors
                    }
            finally:
                try:
                    app.disconnect()
                finally:
                    if thread is not None:
                        thread.join(timeout=2.0)


def fetch_yfinance_quotes(symbols: list[str]) -> dict[str, Quote]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise SystemExit("Please install yfinance first: python3 -m pip install yfinance") from exc

    quotes: dict[str, Quote] = {}
    asof = datetime.now(timezone.utc).isoformat()
    for symbol in symbols:
        ticker = yf.Ticker(symbol)
        price = None
        try:
            fast_info = ticker.fast_info
            price = fast_info.get("last_price") if hasattr(fast_info, "get") else fast_info["last_price"]
        except Exception:
            price = None
        if price is None:
            history = ticker.history(period="1d")
            if not history.empty:
                price = float(history["Close"].iloc[-1])
        if price is None:
            continue
        quotes[symbol.upper()] = Quote(symbol=symbol.upper(), price=float(price), asof=asof, source="yfinance")
    return quotes
