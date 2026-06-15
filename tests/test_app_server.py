import csv
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from quant_trend.agent import _current_market_snapshot_label
from quant_trend.app_server import DEFAULT_SETTINGS, AgentApp, _latest_expected_us_daily_date
from quant_trend.market_data import Quote


class AppServerTests(unittest.TestCase):
    def test_default_provider_is_massive(self):
        self.assertEqual(DEFAULT_SETTINGS["provider"], "massive")

    def test_fetch_quotes_uses_massive_without_falling_through(self):
        class FakeMassiveClient:
            last_messages = []
            last_symbol_errors = {}

            def __init__(self, *args, **kwargs):
                pass

            def fetch_latest_quotes(self, symbols):
                return {symbol: Quote(symbol, 100.0, source="massive:test") for symbol in symbols}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = AgentApp(root)
            with patch("quant_trend.app_server.MassiveDataClient", FakeMassiveClient):
                with patch("quant_trend.app_server.fetch_yfinance_quotes") as fallback:
                    quotes = app.fetch_quotes("massive", ["MU"], {"massive_rest_url": "http://example.test"})

        self.assertEqual(quotes["MU"].source, "massive:test")
        fallback.assert_not_called()

    def test_refresh_history_updates_stale_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            path = data_dir / "AAOI.csv"
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
                writer.writeheader()
                writer.writerow({"date": "2026-06-10", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 100})

            fresh_rows = [
                {"date": "2026-06-11", "open": 2, "high": 3, "low": 2, "close": 3, "volume": 200},
                {"date": "2026-06-12", "open": 3, "high": 4, "low": 3, "close": 4, "volume": 300},
            ]
            app = AgentApp(root)

            with patch("quant_trend.app_server.fetch_yahoo_chart_daily_rows", return_value=fresh_rows) as fetch:
                warnings = app.refresh_history(["AAOI"], expected_latest=date(2026, 6, 12))

            self.assertEqual(warnings, [])
            fetch.assert_called_once_with("AAOI")
            self.assertIn("2026-06-12", path.read_text(encoding="utf-8"))

    def test_expected_us_daily_date_after_friday_close(self):
        now = datetime.fromisoformat("2026-06-13T05:30:00+00:00")

        self.assertEqual(_latest_expected_us_daily_date(now), date(2026, 6, 12))

    def test_current_market_snapshot_label_uses_new_york_session(self):
        marker = _current_market_snapshot_label("2026-06-12T20:30:00+00:00")

        self.assertEqual(marker["market_date"], "2026-06-12")
        self.assertEqual(marker["session"], "postmarket")
        self.assertIn("盘后", marker["display"])


if __name__ == "__main__":
    unittest.main()
