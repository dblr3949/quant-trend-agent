import os
import unittest
from unittest.mock import patch

from quant_trend.plan_summary import _apply_gpt5_options, build_executive_summary


class PlanSummaryTests(unittest.TestCase):
    def test_gpt5_summary_request_uses_medium_reasoning(self):
        body = _apply_gpt5_options({"model": "gpt-5.5", "temperature": 0.2}, "SUMMARY", "medium", "medium")

        self.assertNotIn("temperature", body)
        self.assertEqual(body["reasoning"]["effort"], "medium")
        self.assertEqual(body["text"]["verbosity"], "medium")

    def test_fallback_summary_is_short_and_per_symbol(self):
        old_key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            plan = {
                "regime": {"label": "risk_off", "score": -3.2, "score_range": {"min": -10, "max": 10, "percentile": 34}},
                "portfolio": {"current_gross_exposure": 1.8},
                "orders": [{"symbol": "MU", "side": "sell", "shares": 10, "limit_price": 100}],
                "technical_analysis": {
                    "MU": {
                        "score": -1.5,
                        "score_range": {"min": -6, "max": 6, "percentile": 37.5},
                        "supports": [{"price": 95, "source": "近10日日线支撑"}],
                        "resistances": [{"price": 105, "source": "近10日日线压力"}],
                    }
                },
                "positions": [
                    {"symbol": "MU", "current_weight": 0.5, "target_weight": 0.3, "action": "reduce", "reason": "trend_buy;price_volume_score:-1.5;intraday_down"},
                    {"symbol": "MRVL", "current_weight": 0.2, "target_weight": 0.25, "action": "hold", "reason": "inside_rebalance_band"},
                ],
            }

            summary = build_executive_summary(plan)

            self.assertEqual(summary["source"], "local_fallback")
            self.assertIn("MU", summary["text"])
            self.assertIn("量价", summary["text"])
            self.assertIn("-6~+6", summary["text"])
            self.assertIn("尺位", summary["text"])
            self.assertIn("MRVL", summary["text"])
            self.assertEqual(len(summary["paragraphs"]), 2)
        finally:
            if old_key:
                os.environ["OPENAI_API_KEY"] = old_key

    def test_llm_summary_is_filled_when_symbols_are_missing(self):
        old_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        try:
            plan = {
                "regime": {"label": "neutral", "score": 0},
                "portfolio": {"current_gross_exposure": 1.2},
                "orders": [],
                "technical_analysis": {
                    "MU": {"score": 2, "supports": [{"price": 90}], "resistances": [{"price": 110}]},
                    "MRVL": {"score": 1, "supports": [{"price": 70}], "resistances": [{"price": 80}]},
                },
                "positions": [
                    {"symbol": "MU", "current_weight": 0.3, "target_weight": 0.3, "action": "hold", "reason": "inside_rebalance_band"},
                    {"symbol": "MRVL", "current_weight": 0.2, "target_weight": 0.2, "action": "hold", "reason": "inside_rebalance_band"},
                ],
            }

            with patch("quant_trend.plan_summary._call_openai_summary", return_value="MU：保持，量价偏强。"):
                summary = build_executive_summary(plan)

            self.assertEqual(summary["source"], "llm_with_local_fill")
            self.assertIn("MU", summary["text"])
            self.assertIn("MRVL", summary["text"])
        finally:
            if old_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_key

    def test_llm_summary_with_denominator_scores_uses_fallback(self):
        old_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-openai-key"
        try:
            plan = {
                "regime": {"label": "neutral", "score": 0, "score_range": {"min": -10, "max": 10, "percentile": 50}},
                "portfolio": {"current_gross_exposure": 1.2},
                "orders": [],
                "technical_analysis": {
                    "MU": {"score": 2, "score_range": {"min": -6, "max": 6, "percentile": 67}, "supports": [], "resistances": []},
                },
                "positions": [
                    {
                        "symbol": "MU",
                        "current_weight": 0.3,
                        "target_weight": 0.3,
                        "action": "hold",
                        "reason": "inside_rebalance_band",
                    },
                ],
            }

            with patch("quant_trend.plan_summary._call_openai_summary", return_value="MU：量价评分2/6（分位67%）。"):
                summary = build_executive_summary(plan)

            self.assertEqual(summary["source"], "local_fallback")
            self.assertIn("-6~+6", summary["text"])
            self.assertNotIn("2/6", summary["text"])
        finally:
            if old_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_key


if __name__ == "__main__":
    unittest.main()
