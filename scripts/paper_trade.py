#!/usr/bin/env python3
import argparse
import csv
import json
from datetime import datetime
from pathlib import Path


DEFAULT_STATE = "state/paper_portfolio.json"


def load_state(path: str):
    file = Path(path)
    if not file.exists():
        return {"cash": 100000.0, "positions": {}, "trades": []}
    with file.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: str, state):
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def buy(state, symbol: str, price: float, shares: int):
    cost = price * shares
    if cost > state["cash"]:
        raise ValueError("not enough simulated cash")
    pos = state["positions"].setdefault(symbol, {"shares": 0, "avg_cost": 0.0})
    old_value = pos["shares"] * pos["avg_cost"]
    pos["shares"] += shares
    pos["avg_cost"] = (old_value + cost) / pos["shares"]
    state["cash"] -= cost
    state["trades"].append({"time": datetime.now().isoformat(timespec="seconds"), "side": "buy", "symbol": symbol, "price": price, "shares": shares})


def sell(state, symbol: str, price: float, shares: int):
    pos = state["positions"].get(symbol)
    if not pos or pos["shares"] < shares:
        raise ValueError("not enough simulated shares")
    pos["shares"] -= shares
    state["cash"] += price * shares
    if pos["shares"] == 0:
        del state["positions"][symbol]
    state["trades"].append({"time": datetime.now().isoformat(timespec="seconds"), "side": "sell", "symbol": symbol, "price": price, "shares": shares})


def print_plan(signals_path: str, state):
    held = set(state["positions"].keys())
    with Path(signals_path).open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["action"] == "buy" and row["symbol"] not in held:
                print(f"BUY WATCH {row['symbol']} close={row['close']} stop={row['stop']} score={row['score']}")
            elif row["action"] == "sell" and row["symbol"] in held:
                print(f"SELL WATCH {row['symbol']} close={row['close']} reason={row['reason']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show")

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--signals", default="reports/signals.csv")

    for name in ("buy", "sell"):
        p = sub.add_parser(name)
        p.add_argument("--symbol", required=True)
        p.add_argument("--price", required=True, type=float)
        p.add_argument("--shares", required=True, type=int)

    args = parser.parse_args()
    state = load_state(args.state)

    if args.command == "show":
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0
    if args.command == "plan":
        print_plan(args.signals, state)
        return 0
    if args.command == "buy":
        buy(state, args.symbol, args.price, args.shares)
    elif args.command == "sell":
        sell(state, args.symbol, args.price, args.shares)

    save_state(args.state, state)
    print(json.dumps(state, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
