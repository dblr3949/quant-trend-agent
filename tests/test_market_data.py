import unittest
from pathlib import Path

from quant_trend.market_data import _IbkrTickerState, _ibkr_bar_timestamp


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


if __name__ == "__main__":
    unittest.main()
