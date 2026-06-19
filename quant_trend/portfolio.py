import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Position:
    symbol: str
    shares: int
    avg_cost: float | None = None
    thesis_status: str = "intact"
    conviction: float = 1.0
    bucket: str = "auto"
    trade_constraint: str = "flexible"


@dataclass(frozen=True)
class Portfolio:
    account_equity: float
    cash: float
    positions: dict[str, Position]
    margin_debit: float = 0.0
    maintenance_margin: float | None = None
    excess_liquidity: float | None = None
    target_gross_hint: float | None = None
    asof: str | None = None
    cash_input_missing: bool = False
    margin_debit_input_missing: bool = False
    maintenance_margin_input_missing: bool = False


def load_portfolio(path: str | Path) -> Portfolio:
    with Path(path).open("r", encoding="utf-8") as f:
        payload = json.load(f)

    return portfolio_from_dict(payload)


def portfolio_from_dict(payload: dict) -> Portfolio:
    raw_positions = payload.get("positions", {})
    if isinstance(raw_positions, list):
        items = raw_positions
    else:
        items = [{"symbol": symbol, **raw} for symbol, raw in raw_positions.items()]

    positions: dict[str, Position] = {}
    for raw in items:
        symbol = str(raw["symbol"]).upper()
        positions[symbol] = Position(
            symbol=symbol,
            shares=int(raw["shares"]),
            avg_cost=float(raw["avg_cost"]) if raw.get("avg_cost") not in (None, "") else None,
            thesis_status=str(raw.get("thesis_status", "intact")),
            conviction=float(raw.get("conviction", 1.0)),
            bucket=str(raw.get("bucket", "auto")),
            trade_constraint=str(raw.get("trade_constraint", "flexible")),
        )

    cash_missing = payload.get("cash") in (None, "") or "cash" not in payload
    margin_debit_missing = payload.get("margin_debit") in (None, "") or "margin_debit" not in payload
    maintenance_margin_missing = payload.get("maintenance_margin") in (None, "") or "maintenance_margin" not in payload

    return Portfolio(
        account_equity=float(payload["account_equity"]),
        cash=0.0 if cash_missing else float(payload.get("cash", 0.0)),
        margin_debit=0.0 if margin_debit_missing else float(payload.get("margin_debit", 0.0)),
        maintenance_margin=float(payload["maintenance_margin"]) if payload.get("maintenance_margin") not in (None, "") else None,
        excess_liquidity=float(payload["excess_liquidity"]) if payload.get("excess_liquidity") not in (None, "") else None,
        target_gross_hint=float(payload["target_gross_hint"]) if payload.get("target_gross_hint") not in (None, "") else None,
        positions=positions,
        asof=payload.get("asof"),
        cash_input_missing=bool(payload.get("cash_input_missing", cash_missing)),
        margin_debit_input_missing=bool(payload.get("margin_debit_input_missing", margin_debit_missing)),
        maintenance_margin_input_missing=bool(payload.get("maintenance_margin_input_missing", maintenance_margin_missing)),
    )


def portfolio_to_dict(portfolio: Portfolio) -> dict:
    return {
        "asof": portfolio.asof,
        "account_equity": portfolio.account_equity,
        "cash": None if portfolio.cash_input_missing else portfolio.cash,
        "margin_debit": None if portfolio.margin_debit_input_missing else portfolio.margin_debit,
        "maintenance_margin": None if portfolio.maintenance_margin_input_missing else portfolio.maintenance_margin,
        "excess_liquidity": portfolio.excess_liquidity,
        "target_gross_hint": portfolio.target_gross_hint,
        "cash_input_missing": portfolio.cash_input_missing,
        "margin_debit_input_missing": portfolio.margin_debit_input_missing,
        "maintenance_margin_input_missing": portfolio.maintenance_margin_input_missing,
        "positions": {
            symbol: {
                key: value
                for key, value in asdict(position).items()
                if key != "symbol" and value is not None
            }
            for symbol, position in sorted(portfolio.positions.items())
        },
    }


def save_portfolio(path: str | Path, portfolio: Portfolio) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("w", encoding="utf-8") as f:
        json.dump(portfolio_to_dict(portfolio), f, indent=2, ensure_ascii=False)
