import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from .portfolio import Portfolio, Position


def parse_number(value: str) -> float:
    raw = value.strip().replace(",", "").replace("$", "").replace("￥", "").replace("¥", "")
    multiplier = 1.0
    if raw.lower().endswith("k"):
        multiplier = 1000.0
        raw = raw[:-1]
    elif raw.lower().endswith("m"):
        multiplier = 1000000.0
        raw = raw[:-1]
    elif raw.endswith("万"):
        multiplier = 10000.0
        raw = raw[:-1]
    if not raw:
        raise ValueError(f"Bad number: {value}")
    return float(raw) * multiplier


def _clean_key(key: str) -> str:
    return key.strip().lower().replace(" ", "_").replace("-", "_")


def _first_present(row: dict[str, str], names: list[str]) -> str | None:
    normalized = {_clean_key(key): value for key, value in row.items()}
    for name in names:
        value = normalized.get(_clean_key(name))
        if value not in (None, ""):
            return value
    return None


def portfolio_from_csv_rows(
    rows: list[dict[str, str]],
    account_equity: float,
    cash: float = 0.0,
    margin_debit: float = 0.0,
    maintenance_margin: float | None = None,
    excess_liquidity: float | None = None,
    target_gross_hint: float | None = None,
) -> Portfolio:
    positions: dict[str, Position] = {}
    for row in rows:
        symbol = _first_present(row, ["symbol", "ticker", "代码", "股票"])
        shares = _first_present(row, ["shares", "quantity", "qty", "股数", "数量"])
        if not symbol or not shares:
            continue
        avg_cost = _first_present(row, ["avg_cost", "average_cost", "cost", "成本", "成本价"])
        thesis_status = _first_present(row, ["thesis_status", "status", "状态"]) or "intact"
        conviction = _first_present(row, ["conviction", "信心", "confidence"]) or "1.0"
        bucket = _first_present(row, ["bucket", "桶", "分类", "仓位分类"]) or "auto"
        trade_constraint = _first_present(row, ["trade_constraint", "constraint", "约束", "交易约束"]) or "flexible"
        positions[symbol.upper()] = Position(
            symbol=symbol.upper(),
            shares=int(parse_number(shares)),
            avg_cost=parse_number(avg_cost) if avg_cost else None,
            thesis_status=thesis_status,
            conviction=float(parse_number(conviction)),
            bucket=bucket,
            trade_constraint=trade_constraint,
        )
    return Portfolio(
        account_equity=account_equity,
        cash=cash,
        margin_debit=margin_debit,
        maintenance_margin=maintenance_margin,
        excess_liquidity=excess_liquidity,
        target_gross_hint=target_gross_hint,
        positions=positions,
        asof=datetime.now(timezone.utc).isoformat(),
    )


def _read_text(source: str | Path) -> str:
    value = str(source)
    if value.startswith(("http://", "https://")):
        request = Request(value, headers={"User-Agent": "quant-trend-agent/1.0"})
        with urlopen(request, timeout=20) as response:
            return response.read().decode("utf-8-sig")
    return Path(value).read_text(encoding="utf-8-sig")


def portfolio_from_csv_source(
    source: str | Path,
    account_equity: float,
    cash: float = 0.0,
    margin_debit: float = 0.0,
    maintenance_margin: float | None = None,
    excess_liquidity: float | None = None,
    target_gross_hint: float | None = None,
) -> Portfolio:
    text = _read_text(source)
    rows = list(csv.DictReader(text.splitlines()))
    return portfolio_from_csv_rows(rows, account_equity, cash, margin_debit, maintenance_margin, excess_liquidity, target_gross_hint)


def _extract_account_value(text: str, patterns: list[str], required: bool = False) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return parse_number(match.group("value"))
    if required:
        raise ValueError("Missing account equity. Include something like: 净值 100000 or equity 100000.")
    return None


def _chunk_positions(text: str) -> list[tuple[str, str]]:
    raw_matches = list(re.finditer(r"\b(?P<symbol>[A-Za-z]{1,6})\b", text))
    matches = []
    reserved = {"CASH", "EQUITY", "MARGIN", "AVG", "COST", "INTACT", "WATCH", "BROKEN", "SHARES", "QTY"}
    for match in raw_matches:
        symbol = match.group("symbol").upper()
        if symbol not in reserved:
            matches.append(match)
    chunks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        symbol = match.group("symbol").upper()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        chunks.append((symbol, text[match.end() : end]))
    return chunks


def _parse_position(symbol: str, chunk: str) -> Position | None:
    shares_match = re.search(r"(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?(?:万|[kKmM])?)\s*(?:股|shares?|sh|qty|数量)?", chunk)
    if not shares_match:
        return None

    avg_cost = None
    cost_match = re.search(
        r"(?:成本|成本价|均价|avg(?:_cost)?|cost|@)\s*[:：=]?\s*\$?(?P<value>\d+(?:,\d{3})*(?:\.\d+)?)",
        chunk,
        flags=re.IGNORECASE,
    )
    if cost_match:
        avg_cost = parse_number(cost_match.group("value"))

    thesis_status = "intact"
    if re.search(r"\b(broken|invalidated)\b|破|坏", chunk, flags=re.IGNORECASE):
        thesis_status = "broken"
    elif re.search(r"\b(watch|questioned|weakening)\b|观察|存疑", chunk, flags=re.IGNORECASE):
        thesis_status = "watch"

    conviction = 1.0
    conviction_match = re.search(r"(?:conviction|confidence|信心)\s*[:：=]?\s*(?P<value>\d+(?:\.\d+)?)", chunk, flags=re.IGNORECASE)
    if conviction_match:
        conviction = float(parse_number(conviction_match.group("value")))

    bucket = "auto"
    if re.search(r"\bcore\b|核心|压舱", chunk, flags=re.IGNORECASE):
        bucket = "core"
    elif re.search(r"\bsatellite\b|卫星", chunk, flags=re.IGNORECASE):
        bucket = "satellite"
    elif re.search(r"\btrim\b|清理|出清|减仓桶", chunk, flags=re.IGNORECASE):
        bucket = "trim"
    elif re.search(r"\bwatch\b|观察桶", chunk, flags=re.IGNORECASE):
        bucket = "watch"

    trade_constraint = "flexible"
    if re.search(r"只减不加|不加仓|不买|reduce only|no add", chunk, flags=re.IGNORECASE):
        trade_constraint = "soft_no_add"
    if re.search(r"不减|不卖|不主动卖|no sell|no reduce", chunk, flags=re.IGNORECASE):
        trade_constraint = "soft_no_reduce"
    if re.search(r"尽量不动|少动|hold preferred|prefer hold", chunk, flags=re.IGNORECASE):
        trade_constraint = "prefer_hold"

    return Position(
        symbol=symbol,
        shares=int(parse_number(shares_match.group("value"))),
        avg_cost=avg_cost,
        thesis_status=thesis_status,
        conviction=conviction,
        bucket=bucket,
        trade_constraint=trade_constraint,
    )


def portfolio_from_text(text: str) -> Portfolio:
    normalized = text.replace("，", ",").replace("；", ";").replace("：", ":")
    account_equity = _extract_account_value(
        normalized,
        [
            r"(?:account[_ ]?equity|equity|net liquidation|netliq|净值|账户净值|权益|总权益)\s*[:：=]?\s*(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?(?:万|[kKmM])?)",
            r"(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?(?:万|[kKmM])?)\s*(?:净值|账户净值|权益|总权益)",
        ],
        required=True,
    )
    cash = _extract_account_value(
        normalized,
        [
            r"(?:cash|现金|可用现金)\s*[:：=]?\s*(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?(?:万|[kKmM])?)",
        ],
    )
    margin_debit = _extract_account_value(
        normalized,
        [
            r"(?:margin[_ ]?debit|margin|融资|借款)\s*[:：=]?\s*(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?(?:万|[kKmM])?)",
        ],
    )
    maintenance_margin = _extract_account_value(
        normalized,
        [
            r"(?:maintenance[_ ]?margin|维持保证金|维持保证金要求|强平线|强平阈值)\s*[:：=]?\s*(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?(?:万|[kKmM])?)",
        ],
    )
    excess_liquidity = _extract_account_value(
        normalized,
        [
            r"(?:excess[_ ]?liquidity|excess|保证金余量|超额流动性|流动性余量)\s*[:：=]?\s*(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?(?:万|[kKmM])?)",
        ],
    )
    target_gross_hint = _extract_account_value(
        normalized,
        [
            r"(?:target[_ ]?gross|target[_ ]?leverage|目标杠杆|目标gross|目标仓位杠杆)\s*[:：=]?\s*(?P<value>-?\d+(?:,\d{3})*(?:\.\d+)?)(?:x|倍)?",
        ],
    )

    positions: dict[str, Position] = {}
    for symbol, chunk in _chunk_positions(normalized):
        position = _parse_position(symbol, chunk)
        if position:
            positions[symbol] = position

    if not positions:
        raise ValueError("Missing positions. Include lines like: MU 300股 成本115 intact.")

    return Portfolio(
        account_equity=float(account_equity or 0),
        cash=float(cash or 0),
        margin_debit=float(margin_debit or 0),
        maintenance_margin=None if maintenance_margin is None else float(maintenance_margin),
        excess_liquidity=None if excess_liquidity is None else float(excess_liquidity),
        target_gross_hint=None if target_gross_hint is None else float(target_gross_hint),
        positions=positions,
        asof=datetime.now(timezone.utc).isoformat(),
    )
