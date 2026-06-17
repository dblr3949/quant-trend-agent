import unittest
from unittest.mock import patch

from quant_trend.prompt_overlay import overlay_from_prompt_with_llm


class PromptOverlayTests(unittest.TestCase):
    def test_llm_prompt_overlay_merges_with_rule_guardrails(self):
        raw_llm = {
            "symbols": {
                "MU": {
                    "soft_no_reduce": True,
                    "buy_condition": "只在深回撤或结构支撑处买回",
                    "sell_condition": "强势冲高才卖",
                    "prompt_flags": ["llm_soft_no_reduce"],
                },
                "TSLA": {"no_add": True},
            },
            "events": [{"name": "CPI", "direction": "risk_off", "severity": 1, "note": "原文提及", "expires": None}],
            "_llm_model": "qwen3.7-max",
            "_llm_usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 20, "reasoning_tokens": 0, "total_tokens": 30},
        }

        with patch("quant_trend.prompt_overlay._call_llm_prompt_overlay", return_value=raw_llm):
            overlay = overlay_from_prompt_with_llm("MU 绝对不加仓，但不主动卖，CPI 前谨慎", ["MU"], model="qwen3.7-max")

        self.assertEqual(overlay["prompt_parser"]["source"], "llm_with_rules_guardrail")
        self.assertTrue(overlay["symbols"]["MU"]["no_add"])
        self.assertTrue(overlay["symbols"]["MU"]["soft_no_reduce"])
        self.assertEqual(overlay["symbols"]["MU"]["buy_condition"], "只在深回撤或结构支撑处买回")
        self.assertNotIn("TSLA", overlay["symbols"])
        self.assertEqual(overlay["events"][0]["name"], "manual_caution")
        self.assertEqual(overlay["events"][1]["name"], "CPI")

    def test_llm_false_values_cannot_erase_rule_hard_constraints(self):
        raw_llm = {
            "symbols": {"MU": {"no_add": False, "bias": 1.5, "prompt_flags": ["llm_positive"]}},
            "events": [],
            "_llm_model": "qwen3.7-max",
            "_llm_usage": {"total_tokens": 3},
        }

        with patch("quant_trend.prompt_overlay._call_llm_prompt_overlay", return_value=raw_llm):
            overlay = overlay_from_prompt_with_llm("MU 绝对不加仓", ["MU"], model="qwen3.7-max")

        self.assertTrue(overlay["symbols"]["MU"]["no_add"])
        self.assertEqual(overlay["symbols"]["MU"]["bias"], 1.5)

    def test_prompt_overlay_falls_back_to_rules_when_llm_unavailable(self):
        with patch("quant_trend.prompt_overlay._call_llm_prompt_overlay", return_value=None):
            overlay = overlay_from_prompt_with_llm("INTC 只减不加", ["INTC"], model="qwen3.7-max")

        self.assertEqual(overlay["prompt_parser"]["source"], "rules")
        self.assertTrue(overlay["symbols"]["INTC"]["soft_no_add"])


if __name__ == "__main__":
    unittest.main()
