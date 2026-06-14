import unittest
from datetime import date, timedelta

from quant_trend.backtest import run_backtest
from quant_trend.models import Bar
from quant_trend.strategy import latest_signal


def make_trend_bars(days=220):
    start = date(2025, 1, 1)
    bars = []
    price = 20.0
    for i in range(days):
        price *= 1.006
        bars.append(Bar(start + timedelta(days=i), price * 0.99, price * 1.02, price * 0.98, price, 1000000 + i * 2000))
    return bars


class StrategyTests(unittest.TestCase):
    def test_latest_signal_detects_uptrend(self):
        signal = latest_signal("TEST", make_trend_bars())
        self.assertIsNotNone(signal)
        self.assertIn(signal.action, {"buy", "watch"})
        self.assertGreaterEqual(signal.score, 4)

    def test_backtest_runs(self):
        result = run_backtest("TEST", make_trend_bars(), start_cash=100000)
        self.assertEqual(result.symbol, "TEST")
        self.assertGreater(result.end_value, 0)


if __name__ == "__main__":
    unittest.main()
