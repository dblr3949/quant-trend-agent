import csv
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from quant_trend.agent import (
    DEFAULT_CONFIG,
    TechnicalSnapshot,
    _anchored_vwap_levels_from_daily,
    _daily_anchor_candidates,
    _display_levels,
    _limit_price,
    build_snapshot,
    build_trade_plan,
    summarize_intraday_bars,
)
from quant_trend.market_data import IntradayBar, Quote
from quant_trend.models import Bar
from quant_trend.portfolio import Portfolio, Position
from quant_trend.prompt_overlay import overlay_from_prompt


def write_bars(directory: str, symbol: str, start_price: float, drift: float = 0.004):
    path = Path(directory) / f"{symbol}.csv"
    rows = []
    price = start_price
    start = date(2025, 1, 1)
    for i in range(220):
        price *= 1 + drift
        rows.append(
            {
                "date": (start + timedelta(days=i)).isoformat(),
                "open": price * 0.99,
                "high": price * 1.02,
                "low": price * 0.98,
                "close": price,
                "volume": 1000000 + i * 1000,
            }
        )
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)
    return price


class AgentTests(unittest.TestCase):
    def test_ibkr_close_tick_uses_latest_daily_close_when_conflicting(self):
        bars = [Bar(date(2026, 6, 12), 971.81, 1012.62, 960.19, 981.61, 40785200)]
        quote = Quote("MU", 995.87, asof="2026-06-13T06:00:00+00:00", source="ibkr:live:close")

        snapshot = build_snapshot("MU", bars, quote)

        self.assertEqual(snapshot.price, 981.61)
        self.assertEqual(snapshot.source, "daily_close:stale_close_ignored")

    def test_massive_prev_close_quote_falls_back_to_daily_close(self):
        bars = [Bar(date(2026, 6, 12), 971.81, 1012.62, 960.19, 981.61, 40785200)]
        quote = Quote("MU", 950.0, asof=None, source="massive:snapshot:prev_close")

        snapshot = build_snapshot("MU", bars, quote)

        # A prevDay close is always stale vs. the current session: use the daily close.
        self.assertEqual(snapshot.price, 981.61)
        self.assertEqual(snapshot.source, "daily_close:stale_close_ignored")
        self.assertIsNone(snapshot.quote_age_minutes)

    def test_build_trade_plan_creates_sized_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = {}
            for symbol, price in {"MU": 100, "AAOI": 20, "INTC": 30, "LITE": 60, "MRVL": 75, "SPY": 500, "SMH": 250, "SOXX": 220}.items():
                latest[symbol] = write_bars(tmp, symbol, price)
            latest["^VIX"] = write_bars(tmp, "^VIX", 16, drift=0.0)

            asof = datetime.now(timezone.utc).isoformat()
            quotes = {
                "MU": Quote("MU", latest["MU"], asof=asof, source="test"),
                "AAOI": Quote("AAOI", latest["AAOI"], asof=asof, source="test"),
                "INTC": Quote("INTC", latest["INTC"], asof=asof, source="test"),
                "LITE": Quote("LITE", latest["LITE"], asof=asof, source="test"),
                "MRVL": Quote("MRVL", latest["MRVL"], asof=asof, source="test"),
                "SPY": Quote("SPY", latest["SPY"], asof=asof, source="test"),
                "SMH": Quote("SMH", latest["SMH"], asof=asof, source="test"),
                "SOXX": Quote("SOXX", latest["SOXX"], asof=asof, source="test"),
                "^VIX": Quote("^VIX", latest["^VIX"], asof=asof, source="test"),
            }
            portfolio = Portfolio(
                account_equity=100000,
                cash=-10000,
                margin_debit=10000,
                positions={"MU": Position("MU", 100, 100), "MRVL": Position("MRVL", 50, 75)},
            )

            plan = build_trade_plan(portfolio, quotes, DEFAULT_CONFIG, {}, tmp)

            self.assertIn(plan["regime"]["label"], {"risk_on", "neutral"})
            self.assertGreaterEqual(len(plan["positions"]), 5)
            self.assertTrue(all(order["shares"] > 0 and order["limit_price"] > 0 for order in plan["orders"]))
            self.assertIn("MU", plan["technical_analysis"])
            self.assertIn("supports", plan["technical_analysis"]["MU"])
            self.assertIn("resistances", plan["technical_analysis"]["MU"])
            self.assertIn("risk_adjusted_momentum", plan["technical_analysis"]["MU"])
            self.assertIn("range_volatility", plan["technical_analysis"]["MU"])
            self.assertIn("order_flow", plan["technical_analysis"]["MU"])
            self.assertIn("SPY", plan["market_technical_analysis"])
            self.assertNotIn("^VIX", plan["market_technical_analysis"])
            self.assertIn("^VIX", plan["volatility_analysis"])
            self.assertIn("percentile_252", plan["volatility_analysis"]["^VIX"])
            self.assertIn("market_structure", plan)
            self.assertTrue(any(item.get("direction") == "volatility_risk" for item in plan["market_structure"]["components"]))
            self.assertIsNotNone(plan["positions"][0].get("price_volume_score"))
            self.assertIn("score_range", plan["regime"])
            self.assertIn("score_range", plan["technical_analysis"]["MU"])
            self.assertIn("score_range", plan["technical_analysis"]["MU"]["components"][0])
            self.assertIn("score_range", plan["market_structure"])
            displayed_levels = plan["technical_analysis"]["MU"]["supports"] + plan["technical_analysis"]["MU"]["resistances"]
            self.assertTrue(any("level_strength_score" in level for level in displayed_levels))
            self.assertTrue(any(level.get("profile_role") for level in displayed_levels))
            self.assertTrue(any(order["limit_context"].get("candidate_levels") for order in plan["orders"]))

    def test_anchored_vwap_detects_event_anchors(self):
        bars = []
        start = date(2026, 1, 1)
        price = 100.0
        for i in range(45):
            volume = 1_000_000
            open_price = price * 0.995
            close = price * 1.002
            if i == 30:
                open_price = price * 1.06
                close = open_price * 1.06
                volume = 2_600_000
            high = max(open_price, close) * 1.01
            low = min(open_price, close) * 0.99
            bars.append(Bar(start + timedelta(days=i), open_price, high, low, close, volume))
            price = close

        anchors = _daily_anchor_candidates(bars)
        anchor_types = {item["type"] for item in anchors}
        self.assertIn("gap_up", anchor_types)
        self.assertIn("volume_shock_up", anchor_types)

        levels = _anchored_vwap_levels_from_daily(bars, "buy", 200)
        self.assertTrue(any(level.get("profile_role") == "anchored_vwap" for level in levels))
        self.assertTrue(any(level.get("anchor_date") for level in levels))

    def test_stale_quotes_block_add_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            for symbol, price in {"MU": 100, "SPY": 500, "SMH": 250, "SOXX": 220, "^VIX": 16}.items():
                write_bars(tmp, symbol, price)
            stale = "2020-01-01T14:30:00+00:00"
            quotes = {
                "MU": Quote("MU", 150, asof=stale, source="test"),
                "SPY": Quote("SPY", 700, asof=stale, source="test"),
                "SMH": Quote("SMH", 350, asof=stale, source="test"),
                "SOXX": Quote("SOXX", 300, asof=stale, source="test"),
                "^VIX": Quote("^VIX", 16, asof=stale, source="test"),
            }
            portfolio = Portfolio(account_equity=100000, cash=100000, positions={})
            config = {**DEFAULT_CONFIG, "symbols": ["MU"], "base_target_weights": {"MU": 1.0}}

            plan = build_trade_plan(portfolio, quotes, config, {}, tmp)

            self.assertEqual(plan["orders"], [])
            self.assertIn("quote_stale", plan["positions"][0]["reason"])

    def test_hard_prompt_no_add_blocks_buy_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = write_bars(tmp, "MU", 100)
            for symbol, price in {"SPY": 500, "SMH": 250, "SOXX": 220, "^VIX": 16}.items():
                write_bars(tmp, symbol, price)
            asof = datetime.now(timezone.utc).isoformat()
            quotes = {
                "MU": Quote("MU", latest, asof=asof, source="test"),
                "SPY": Quote("SPY", 500, asof=asof, source="test"),
                "SMH": Quote("SMH", 250, asof=asof, source="test"),
                "SOXX": Quote("SOXX", 220, asof=asof, source="test"),
                "^VIX": Quote("^VIX", 16, asof=asof, source="test"),
            }
            portfolio = Portfolio(account_equity=100000, cash=100000, positions={})
            config = {**DEFAULT_CONFIG, "symbols": ["MU"], "base_target_weights": {"MU": 1.0}}
            research = overlay_from_prompt("MU 绝对不加仓", ["MU"])

            plan = build_trade_plan(portfolio, quotes, config, research, tmp)

            self.assertEqual(plan["orders"], [])
            self.assertIn("prompt_no_add", plan["positions"][0]["reason"])

    def test_soft_prompt_no_add_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = write_bars(tmp, "MU", 100)
            for symbol, price in {"SPY": 500, "SMH": 250, "SOXX": 220, "^VIX": 16}.items():
                write_bars(tmp, symbol, price)
            asof = datetime.now(timezone.utc).isoformat()
            quotes = {
                "MU": Quote("MU", latest, asof=asof, source="test"),
                "SPY": Quote("SPY", 700, asof=asof, source="test"),
                "SMH": Quote("SMH", 350, asof=asof, source="test"),
                "SOXX": Quote("SOXX", 300, asof=asof, source="test"),
                "^VIX": Quote("^VIX", 16, asof=asof, source="test"),
            }
            intraday = {
                "MU": [
                    IntradayBar("MU", "2026-06-11T13:30:00+00:00", 100, 101, 99, 100, 1000, 100),
                    IntradayBar("MU", "2026-06-11T14:00:00+00:00", 100, 106, 100, 106, 1000, 104),
                ]
            }
            portfolio = Portfolio(account_equity=100000, cash=100000, positions={})
            config = {**DEFAULT_CONFIG, "symbols": ["MU"], "base_target_weights": {"MU": 1.0}}
            research = overlay_from_prompt("MU 只减不加", ["MU"])

            plan = build_trade_plan(portfolio, quotes, config, research, tmp, intraday_bars=intraday, intraday_bar_size="30 mins")

            self.assertTrue(plan["orders"])
            self.assertIn("prompt_soft_no_add_overridden", plan["positions"][0]["reason"])

    def test_prompt_no_add_is_soft_by_default(self):
        research = overlay_from_prompt("CPI 前不主动加仓，INTC 只减不加", ["INTC"])

        self.assertTrue(research["symbols"]["INTC"]["soft_no_add"])
        self.assertFalse(research["symbols"]["INTC"].get("no_add", False))
        self.assertLessEqual(research["symbols"]["INTC"]["bias"], 0)

    def test_prompt_range_trade_creates_soft_flat_buy_and_sell_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = write_bars(tmp, "MU", 100)
            for symbol, price in {"SPY": 500, "SMH": 250, "SOXX": 220, "^VIX": 16}.items():
                write_bars(tmp, symbol, price)
            asof = datetime.now(timezone.utc).isoformat()
            quotes = {
                "MU": Quote("MU", latest, asof=asof, source="test"),
                "SPY": Quote("SPY", 700, asof=asof, source="test"),
                "SMH": Quote("SMH", 350, asof=asof, source="test"),
                "SOXX": Quote("SOXX", 300, asof=asof, source="test"),
                "^VIX": Quote("^VIX", 16, asof=asof, source="test"),
            }
            portfolio = Portfolio(account_equity=100000, cash=100000, positions={"MU": Position("MU", 100, 100)})
            config = {**DEFAULT_CONFIG, "symbols": ["MU"], "base_target_weights": {"MU": 1.0}}
            research = overlay_from_prompt("MU 做T，低位买回，高位卖出，总持仓不变", ["MU"])

            plan = build_trade_plan(portfolio, quotes, config, research, tmp)

            self.assertEqual(research["symbols"]["MU"]["trade_plan"], "range_trade")
            self.assertEqual(research["symbols"]["MU"]["target_net_exposure"], "flat_preferred")
            self.assertEqual(len(plan["trade_groups"]), 1)
            group = plan["trade_groups"][0]
            self.assertEqual(group["symbol"], "MU")
            self.assertEqual(group["intent"], "flat_preferred")
            self.assertIsNotNone(group["buy_order"])
            self.assertIsNotNone(group["sell_order"])
            self.assertLess(group["buy_order"]["limit_price"], group["current_price"])
            self.assertGreater(group["sell_order"]["limit_price"], group["current_price"])
            range_orders = [order for order in plan["orders"] if order.get("strategy") == "range_trade" and order.get("symbol") == "MU"]
            self.assertEqual({order["side"] for order in range_orders}, {"buy", "sell"})

    def test_prompt_range_trade_required_flat_still_pairs_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = write_bars(tmp, "MU", 100)
            for symbol, price in {"SPY": 500, "SMH": 250, "SOXX": 220, "^VIX": 16}.items():
                write_bars(tmp, symbol, price)
            asof = datetime.now(timezone.utc).isoformat()
            quotes = {
                "MU": Quote("MU", latest, asof=asof, source="test"),
                "SPY": Quote("SPY", 700, asof=asof, source="test"),
                "SMH": Quote("SMH", 350, asof=asof, source="test"),
                "SOXX": Quote("SOXX", 300, asof=asof, source="test"),
                "^VIX": Quote("^VIX", 16, asof=asof, source="test"),
            }
            portfolio = Portfolio(account_equity=100000, cash=100000, positions={"MU": Position("MU", 100, 100)})
            config = {**DEFAULT_CONFIG, "symbols": ["MU"], "base_target_weights": {"MU": 1.0}}
            research = overlay_from_prompt("MU 做T，低位买回，高位卖出，必须总持仓不变", ["MU"])

            plan = build_trade_plan(portfolio, quotes, config, research, tmp)

            self.assertEqual(research["symbols"]["MU"]["target_net_exposure"], "flat_required")
            group = plan["trade_groups"][0]
            self.assertEqual(group["intent"], "flat_required")
            self.assertEqual(group["net_shares_if_all_filled"], 0)
            self.assertEqual(group["buy_order"]["shares"], group["sell_order"]["shares"])

    def test_intraday_summary_scores_recent_strength(self):
        bars = [
            IntradayBar("MU", "2026-06-11T13:30:00+00:00", 100, 101, 99, 100, 1000, 100),
            IntradayBar("MU", "2026-06-11T13:35:00+00:00", 100, 103, 100, 102, 1200, 101.5),
            IntradayBar("MU", "2026-06-11T13:40:00+00:00", 102, 106, 101, 105, 1300, 104),
        ]

        summary = summarize_intraday_bars("MU", bars, "5 mins")

        self.assertIsNotNone(summary)
        self.assertGreater(summary["score"], 0)
        self.assertIn("score_range", summary)
        self.assertEqual(summary["label"], "strong_up")
        self.assertEqual(summary["bar_count"], 3)

    def test_limit_price_prefers_patient_higher_sell_when_only_intraday_resistance(self):
        snapshot = TechnicalSnapshot(
            symbol="MU",
            price=100,
            source="test",
            quote_age_minutes=0,
            bid=None,
            ask=None,
            close=99,
            sma20=98,
            sma50=96,
            sma150=90,
            atr14=10,
            high60=120,
            trend_action="buy",
            trend_score=6,
            trend_stop=92,
            trend_reason="test",
        )
        intraday = [
            IntradayBar("MU", "2026-06-11T13:30:00+00:00", 99.8, 100.2, 99.6, 100.0, 1000, 99.9),
            IntradayBar("MU", "2026-06-11T13:35:00+00:00", 100.0, 100.42, 99.9, 100.1, 1500, 100.25),
        ]

        price, context = _limit_price("sell", snapshot, DEFAULT_CONFIG, intraday_bars=intraday)

        self.assertGreater(price, 100.42)
        self.assertEqual(context["selected_source"], "历史结构卖出溢价")
        self.assertEqual(context["method"], "patient_historical_structure_with_intraday_check")

    def test_low_margin_cushion_blocks_new_buys(self):
        with tempfile.TemporaryDirectory() as tmp:
            latest = write_bars(tmp, "MU", 100)
            for symbol, price in {"SPY": 500, "SMH": 250, "SOXX": 220, "^VIX": 16}.items():
                write_bars(tmp, symbol, price)
            asof = datetime.now(timezone.utc).isoformat()
            quotes = {
                "MU": Quote("MU", latest, asof=asof, source="test"),
                "SPY": Quote("SPY", 700, asof=asof, source="test"),
                "SMH": Quote("SMH", 350, asof=asof, source="test"),
                "SOXX": Quote("SOXX", 300, asof=asof, source="test"),
                "^VIX": Quote("^VIX", 16, asof=asof, source="test"),
            }
            portfolio = Portfolio(account_equity=100000, cash=100000, maintenance_margin=80000, positions={})
            config = {**DEFAULT_CONFIG, "symbols": ["MU"], "base_target_weights": {"MU": 1.0}}

            plan = build_trade_plan(portfolio, quotes, config, {}, tmp)

            self.assertEqual(plan["orders"], [])
            self.assertEqual(plan["portfolio"]["margin_buy_budget"], 0.0)
            self.assertEqual(plan["portfolio"]["margin_cushion"], 20000.0)
            self.assertIn("buy_blocked", plan["positions"][0]["reason"])

    def test_limit_price_prefers_patient_lower_buy_when_only_intraday_support(self):
        snapshot = TechnicalSnapshot(
            symbol="MU",
            price=100,
            source="test",
            quote_age_minutes=0,
            bid=None,
            ask=None,
            close=99,
            sma20=99.6,
            sma50=96,
            sma150=90,
            atr14=10,
            high60=120,
            trend_action="buy",
            trend_score=6,
            trend_stop=92,
            trend_reason="test",
        )
        intraday = [
            IntradayBar("MU", "2026-06-11T13:30:00+00:00", 100.2, 100.5, 99.52, 100.0, 1000, 99.8),
            IntradayBar("MU", "2026-06-11T13:35:00+00:00", 100.0, 100.1, 99.62, 99.9, 3000, 99.75),
        ]

        price, context = _limit_price("buy", snapshot, DEFAULT_CONFIG, intraday_bars=intraday)

        self.assertLess(price, 99.75)
        self.assertEqual(context["selected_source"], "50日均线")
        self.assertEqual(context["method"], "patient_historical_structure_with_intraday_check")

    def test_display_levels_prioritize_historical_over_intraday(self):
        levels = [
            {"price": 99.9, "source": "当日VWAP", "category": "intraday", "distance_pct": -0.001},
            {"price": 99.8, "source": "近30分钟支撑", "category": "intraday", "distance_pct": -0.002},
            {"price": 96.0, "source": "90日成交密集区下沿", "category": "volume_profile", "distance_pct": -0.04},
            {"price": 94.0, "source": "90日摆动前低", "category": "swing", "distance_pct": -0.06},
        ]

        displayed = _display_levels(levels, "buy", 100, limit=3)

        self.assertNotEqual(displayed[0]["category"], "intraday")
        self.assertIn(displayed[0]["category"], {"volume_profile", "swing"})
        self.assertLessEqual(sum(1 for item in displayed if item["category"] == "intraday"), 1)


if __name__ == "__main__":
    unittest.main()
