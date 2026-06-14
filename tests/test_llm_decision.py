import unittest
from unittest.mock import patch

from quant_trend.llm_decision import _apply_gpt5_options, apply_llm_limit_decisions


class LlmDecisionTests(unittest.TestCase):
    def test_gpt5_decision_request_uses_xhigh_reasoning(self):
        body = _apply_gpt5_options({"model": "gpt-5.5", "temperature": 0.1}, "DECISION", "xhigh", "low")

        self.assertNotIn("temperature", body)
        self.assertEqual(body["reasoning"]["effort"], "xhigh")
        self.assertEqual(body["text"]["verbosity"], "low")

    def test_prompt_context_is_sent_to_decision_llm(self):
        plan = {
            "portfolio": {"current_gross_exposure": 1.6},
            "run": {"prompt": "MU 不主动卖，INTC 只在深回撤加仓"},
            "research_overlay": {
                "source": "manual_prompt",
                "manual_prompt": "MU 不主动卖，INTC 只在深回撤加仓",
                "macro_bias": -1,
                "symbols": {
                    "MU": {"no_reduce": True, "prompt_flags": ["symbol_no_reduce"]},
                    "INTC": {"soft_no_add": True, "bias": -0.75, "prompt_flags": ["symbol_soft_no_add"]},
                },
                "events": [{"name": "manual_caution", "direction": "risk_off"}],
            },
            "decision_context": [{"name": "你的 prompt 与持仓约束", "status": "本轮有输入"}],
            "positions": [{"symbol": "INTC", "shares": 0}],
            "orders": [
                {
                    "symbol": "INTC",
                    "side": "buy",
                    "shares": 10,
                    "limit_price": 95.0,
                    "notional": 950.0,
                    "target_trade_value": 1000.0,
                    "limit_context": {
                        "reference_price": 100.0,
                        "candidate_levels": [{"candidate_id": "C1", "candidate_price": 96.0, "source": "20日POC"}],
                    },
                }
            ],
        }
        captured = {}

        def fake_call(compact):
            captured.update(compact)
            return {"decisions": []}

        with patch("quant_trend.llm_decision._call_openai_decisions", side_effect=fake_call):
            apply_llm_limit_decisions(plan)

        self.assertEqual(captured["prompt"], "MU 不主动卖，INTC 只在深回撤加仓")
        self.assertEqual(captured["prompt_overlay"]["manual_prompt"], "MU 不主动卖，INTC 只在深回撤加仓")
        self.assertTrue(captured["prompt_overlay"]["symbols"]["MU"]["no_reduce"])
        self.assertTrue(captured["prompt_overlay"]["symbols"]["INTC"]["soft_no_add"])
        self.assertIn("必须读取 prompt", captured["instructions"])
        self.assertEqual(captured["decision_context"][0]["status"], "本轮有输入")

    def test_llm_decision_can_only_apply_known_candidate(self):
        plan = {
            "portfolio": {},
            "positions": [{"symbol": "MU", "shares": 100}],
            "orders": [
                {
                    "symbol": "MU",
                    "side": "buy",
                    "shares": 10,
                    "limit_price": 95.0,
                    "notional": 950.0,
                    "target_trade_value": 1000.0,
                    "limit_context": {
                        "reference_price": 100.0,
                        "candidate_levels": [
                            {"candidate_id": "C1", "candidate_price": 96.0, "source": "20日POC成交最密集价"},
                            {"candidate_id": "C2", "candidate_price": 92.0, "source": "60日价值区下沿VAL"},
                        ],
                    },
                }
            ],
        }

        with patch(
            "quant_trend.llm_decision._call_openai_decisions",
            return_value={
                "decisions": [
                    {
                        "symbol": "MU",
                        "candidate_id": "C2",
                        "rationale": "更低且有结构承接",
                        "reference_ladder": [
                            {"label": "浅回撤", "price": 96.5, "allocation_pct": 0.4, "rationale": "先试探"},
                            {"label": "主支撑", "price": 92.5, "allocation_pct": 0.35, "rationale": "接近结构支撑"},
                        ],
                    }
                ]
            },
        ):
            result = apply_llm_limit_decisions(plan)

        self.assertEqual(result["source"], "llm_candidate_selector")
        self.assertEqual(plan["orders"][0]["limit_price"], 92.0)
        self.assertEqual(plan["orders"][0]["shares"], 10)
        self.assertIn("LLM选择候选C2", plan["orders"][0]["limit_basis"])
        self.assertEqual(len(plan["orders"][0]["llm_reference_ladder"]), 2)
        self.assertEqual(plan["orders"][0]["llm_reference_ladder"][0]["price"], 96.5)
        self.assertTrue(plan["orders"][0]["llm_reference_ladder"][0]["reference_only"])
        self.assertEqual(len(result["ladders"]), 1)

    def test_unknown_candidate_is_ignored(self):
        plan = {
            "portfolio": {},
            "positions": [{"symbol": "MU", "shares": 100}],
            "orders": [
                {
                    "symbol": "MU",
                    "side": "buy",
                    "shares": 10,
                    "limit_price": 95.0,
                    "notional": 950.0,
                    "target_trade_value": 1000.0,
                    "limit_context": {
                        "reference_price": 100.0,
                        "candidate_levels": [{"candidate_id": "C1", "candidate_price": 96.0, "source": "20日POC"}],
                    },
                }
            ],
        }

        with patch(
            "quant_trend.llm_decision._call_openai_decisions",
            return_value={"decisions": [{"symbol": "MU", "candidate_id": "C9", "rationale": "不可用"}]},
        ):
            result = apply_llm_limit_decisions(plan)

        self.assertEqual(result["applied"], [])
        self.assertEqual(plan["orders"][0]["limit_price"], 95.0)
        self.assertIn("llm_reference_ladder", plan["orders"][0])


if __name__ == "__main__":
    unittest.main()
