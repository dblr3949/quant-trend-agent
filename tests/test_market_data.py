import unittest
from pathlib import Path

from quant_trend.market_data import MassiveDataClient, _IbkrTickerState, _ibkr_bar_timestamp


class FakeMassiveDataClient(MassiveDataClient):
    def __init__(self, responses):
        super().__init__(api_key="test-key", base_url="http://example.test")
        self.responses = responses

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> dict:
        return self.responses[path]


class MarketDataTests(unittest.TestCase):
    def test_ibkr_ticker_state_prefers_last_then_midpoint(self):
        state = _IbkrTickerState("MU")
        state.market_data_type = 1
        state.set_price(1, 100.0)
        state.set_price(2, 100.2)

        quote = state.to_quote()

        self.assertIsNotNone(quote)
        self.assertEqual(quote.price, 100.1)
        self.assertEqual(quote.source, "ibkr:live:midpoint")

        state.set_price(4, 101.0)
        quote = state.to_quote()

        self.assertIsNotNone(quote)
        self.assertEqual(quote.price, 101.0)
        self.assertEqual(quote.source, "ibkr:live:last")

    def test_ibkr_client_does_not_call_account_or_order_apis(self):
        source = Path("quant_trend/market_data.py").read_text(encoding="utf-8")
        forbidden = ["reqPositions", "reqAccountSummary", "reqAccountUpdates", "placeOrder", "reqExecutions"]

        for name in forbidden:
            self.assertNotIn(f".{name}(", source)

    def test_ibkr_daily_bar_date_is_not_treated_as_epoch_seconds(self):
        self.assertEqual(_ibkr_bar_timestamp("20260612"), "2026-06-12")

    def test_massive_snapshot_quote_prefers_last_trade(self):
        client = MassiveDataClient(api_key="test-key", base_url="http://example.test")

        quote = client._snapshot_quote(
            "MU",
            {
                "ticker": {
                    "lastTrade": {"p": 101.25},
                    "lastQuote": {"p": 101.2, "P": 101.3},
                    "day": {"c": 100.0},
                }
            },
        )

        self.assertIsNotNone(quote)
        self.assertEqual(quote.symbol, "MU")
        self.assertEqual(quote.price, 101.25)
        self.assertEqual(quote.bid, 101.2)
        self.assertEqual(quote.ask, 101.3)
        self.assertEqual(quote.source, "massive:snapshot:last")

    def test_massive_daily_bars_parse_polygon_aggregates(self):
        client = FakeMassiveDataClient(
            {
                "/v2/aggs/ticker/MU/range/1/day/2026-06-01/2026-06-02": {
                    "results": [
                        {"t": 1780272000000, "o": 10, "h": 12, "l": 9, "c": 11, "v": 1000},
                        {"t": 1780358400000, "o": 11, "h": 13, "l": 10, "c": 12, "v": 2000},
                    ]
                }
            }
        )

        rows = client.fetch_daily_bars("MU", "2026-06-01", "2026-06-02")

        self.assertEqual(rows[0]["date"], "2026-06-01")
        self.assertEqual(rows[1]["close"], 12.0)
        self.assertEqual(rows[1]["volume"], 2000.0)


if __name__ == "__main__":
    unittest.main()
