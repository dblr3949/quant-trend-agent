#!/usr/bin/env python3
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_trend.agent import build_trade_plan, load_agent_config, load_json
from quant_trend.market_data import load_quotes
from quant_trend.portfolio import load_portfolio


def _write_orders_csv(path: str | Path, orders: list[dict]) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["symbol", "side", "shares", "limit_price", "notional", "time_in_force", "action", "reason"]
    with file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(orders)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portfolio", default="config/portfolio.json")
    parser.add_argument("--quotes", default="data/live_quotes.json")
    parser.add_argument("--config", default="config/agent_config.json")
    parser.add_argument("--research", default="data/research_overlay.json")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--output", default="reports/agent_plan.json")
    parser.add_argument("--orders-csv", default="reports/agent_orders.csv")
    args = parser.parse_args()

    portfolio = load_portfolio(args.portfolio)
    quotes = load_quotes(args.quotes)
    config = load_agent_config(args.config)
    research = load_json(args.research)

    plan = build_trade_plan(
        portfolio=portfolio,
        quotes=quotes,
        config=config,
        research=research,
        data_dir=args.data_dir,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    _write_orders_csv(args.orders_csv, plan["orders"])

    print(f"regime={plan['regime']['label']} gross={plan['portfolio']['current_gross_exposure']} target={plan['regime']['target_gross_exposure']}")
    if plan["data_warnings"]:
        print("data warnings:")
        for warning in plan["data_warnings"]:
            print(f"- {warning}")
    if plan["orders"]:
        print("orders:")
        for order in plan["orders"]:
            print(f"- {order['side'].upper()} {order['symbol']} {order['shares']} @ {order['limit_price']} ({order['reason']})")
    else:
        print("orders: none")
    print(f"saved {args.output}")
    print(f"saved {args.orders_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
