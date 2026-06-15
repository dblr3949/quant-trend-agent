import unittest
from unittest.mock import patch

from quant_trend.llm_decision import _apply_gpt5_options, apply_llm_limit_decisions


class LlmDecisionTests(unittest.TestCase):
    def test_gpt5_decision_request_uses_medium_reasoning(self):
        body = _apply_gpt5_options({"model": "gpt-5.5", "temperature": 0.1}, "DECISION", "medium", "low")

        self.assertNotIn("temperature", body)
        self.assertEqual(body["reasoning"]["effort"], "medium")
        self.assertEqual(body["text"]["verbosity"], "low")

    def test_gpt5_decision_request_preserves_structured_output_format(self):
        body = _apply_gpt5_options(
            {"model": "gpt-5.5", "text": {"format": {"type": "json_schema", "name": "x", "schema": {}}}},
            "DECISION",
            "medium",
            "low",
        )

        self.assertEqual(body["text"]["format"]["type"], "json_schema")
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

        def fake_call(compact, model=None):
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

    def test_llm_decision_parse_error_is_reported_with_preview(self):
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
                    "limit_context": {"reference_price": 100.0, "candidate_levels": []},
                }
            ],
        }

        with patch(
            "quant_trend.llm_decision._call_openai_decisions",
            return_value={
                "_openai_model": "gpt-5.5",
                "_openai_usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2, "reasoning_tokens": 0, "total_tokens": 3},
                "_openai_error": "json_parse_error:bad json",
                "_openai_raw_preview": "{\"decisions\":[",
                "decisions": [],
            },
        ):
            result = apply_llm_limit_decisions(plan)

        self.assertEqual(result["source"], "llm_error")
        self.assertIn("json_parse_error", result["error"])
        self.assertIn("decisions", result["raw_preview"])
        self.assertEqual(result["usage"]["total_tokens"], 3)

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

    def test_llm_decision_uses_order_id_when_symbol_has_two_sides(self):
        plan = {
            "portfolio": {},
            "positions": [{"symbol": "MU", "shares": 100}],
            "orders": [
                {
                    "order_id": "range_trade:MU:buy",
                    "symbol": "MU",
                    "side": "buy",
                    "shares": 10,
                    "limit_price": 95.0,
                    "notional": 950.0,
                    "target_trade_value": 1000.0,
                    "limit_context": {
                        "reference_price": 100.0,
                        "candidate_levels": [{"candidate_id": "B1", "candidate_price": 94.0, "source": "低吸支撑"}],
                    },
                },
                {
                    "order_id": "range_trade:MU:sell",
                    "symbol": "MU",
                    "side": "sell",
                    "shares": 10,
                    "limit_price": 105.0,
                    "notional": 1050.0,
                    "target_trade_value": 1000.0,
                    "limit_context": {
                        "reference_price": 100.0,
                        "candidate_levels": [{"candidate_id": "S1", "candidate_price": 108.0, "source": "高抛压力"}],
                    },
                },
            ],
        }

        with patch(
            "quant_trend.llm_decision._call_openai_decisions",
            return_value={
                "decisions": [
                    {
                        "order_id": "range_trade:MU:sell",
                        "symbol": "MU",
                        "side": "sell",
                        "candidate_id": "S1",
                        "rationale": "上方压力更适合卖出",
                    }
                ]
            },
        ):
            result = apply_llm_limit_decisions(plan)

        self.assertEqual(result["applied"][0]["order_id"], "range_trade:MU:sell")
        self.assertEqual(plan["orders"][0]["limit_price"], 95.0)
        self.assertEqual(plan["orders"][1]["limit_price"], 108.0)

    def test_llm_decision_can_make_range_trade_net_directional(self):
        buy_order = {
            "order_id": "range_trade:MU:buy",
            "symbol": "MU",
            "side": "buy",
            "strategy": "range_trade",
            "trade_group_id": "range_trade:MU",
            "pair_role": "low_buy",
            "shares": 3,
            "limit_price": 95.0,
            "notional": 285.0,
            # Budget headroom for 5 shares (5*95=475); the share cap is clamped to
            # target_trade_value, so a net-directional buy must fit inside budget.
            "target_trade_value": 500.0,
            "limit_context": {"reference_price": 100.0, "candidate_levels": []},
        }
        sell_order = {
            "order_id": "range_trade:MU:sell",
            "symbol": "MU",
            "side": "sell",
            "strategy": "range_trade",
            "trade_group_id": "range_trade:MU",
            "pair_role": "high_sell",
            "shares": 3,
            "limit_price": 110.0,
            "notional": 330.0,
            "target_trade_value": 300.0,
            "limit_context": {"reference_price": 100.0, "candidate_levels": []},
        }
        plan = {
            "portfolio": {},
            "positions": [{"symbol": "MU", "shares": 100}],
            "orders": [buy_order, sell_order],
            "trade_groups": [
                {
                    "group_id": "range_trade:MU",
                    "symbol": "MU",
                    "intent": "flat_preferred",
                    "buy_order": buy_order,
                    "sell_order": sell_order,
                    "net_shares_if_all_filled": 0,
                    "net_cash_if_all_filled": 45.0,
                    "estimated_spread_pct": 0.1579,
                }
            ],
        }

        with patch(
            "quant_trend.llm_decision._call_openai_decisions",
            return_value={
                "decisions": [
                    {"order_id": "range_trade:MU:buy", "symbol": "MU", "side": "buy", "target_shares": 5, "rationale": "指标支持多买一点"},
                    {"order_id": "range_trade:MU:sell", "symbol": "MU", "side": "sell", "target_shares": 2, "rationale": "压力位先少卖"},
                ]
            },
        ):
            result = apply_llm_limit_decisions(plan)

        self.assertEqual(len(result["applied"]), 2)
        self.assertEqual(plan["orders"][0]["shares"], 5)
        self.assertEqual(plan["orders"][1]["shares"], 2)
        group = plan["trade_groups"][0]
        self.assertEqual(group["net_shares_if_all_filled"], 3)
        self.assertEqual(group["net_cash_if_all_filled"], -255.0)


if __name__ == "__main__":
    unittest.main()
