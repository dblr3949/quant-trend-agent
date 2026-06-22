import unittest
from datetime import date

from quant_trend.options_analysis import analyze_option_chain


def option_row(contract_type, strike, expiration, oi, volume=0, iv=0.5, delta=None, gamma=0.01, bid=1.0, ask=1.2):
    return {
        "details": {
            "contract_type": contract_type,
            "expiration_date": expiration,
            "strike_price": strike,
            "ticker": f"O:MU260626{contract_type[0].upper()}{int(strike * 1000):08d}",
        },
        "open_interest": oi,
        "implied_volatility": iv,
        "day": {"volume": volume},
        "greeks": {"delta": delta, "gamma": gamma},
        "last_quote": {"bid": bid, "ask": ask, "midpoint": (bid + ask) / 2},
        "underlying_asset": {"price": 100},
    }


class OptionsAnalysisTests(unittest.TestCase):
    def test_analyze_option_chain_summarizes_core_metrics(self):
        chain = [
            option_row("call", 95, "2026-06-26", 100, volume=20, iv=0.45, delta=0.65),
            option_row("call", 105, "2026-06-26", 300, volume=60, iv=0.48, delta=0.35),
            option_row("put", 95, "2026-06-26", 150, volume=30, iv=0.52, delta=-0.35),
            option_row("put", 105, "2026-06-26", 50, volume=10, iv=0.50, delta=-0.65),
        ]

        result = analyze_option_chain("MU", chain, underlying_price=100, today=date(2026, 6, 22))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["contracts"], 4)
        self.assertEqual(result["call_oi"], 400)
        self.assertEqual(result["put_oi"], 200)
        self.assertEqual(result["put_call_oi_ratio"], 0.5)
        self.assertEqual(result["focus_expiration"], "2026-06-26")
        self.assertEqual(result["top_call_oi"][0]["strike"], 105)
        self.assertEqual(result["top_put_oi"][0]["strike"], 95)
        self.assertIsNotNone(result["max_pain"])
        self.assertIn("risk", result)
        self.assertIn("anomaly", result)
        self.assertIn("signals", result)
        self.assertIn("symbol_summary", result)
        self.assertIn("PCR", result["explanation"])

    def test_empty_option_chain_is_marked_no_data(self):
        result = analyze_option_chain("MU", [], underlying_price=100, today=date(2026, 6, 22))

        self.assertEqual(result["status"], "no_data")
        self.assertEqual(result["contracts"], 0)


if __name__ == "__main__":
    unittest.main()
