from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any


def _num(value: Any) -> float | None:
    if value in (None, "", "NaN"):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float:
    number = _num(value)
    return number if number is not None and number > 0 else 0.0


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator > 0 else None


def _pct_distance(price: float | None, reference: float | None) -> float | None:
    if price is None or reference is None or reference <= 0:
        return None
    return round((price / reference) - 1.0, 4)


def _parse_expiration(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _score_meta(value: float, minimum: float, maximum: float, unit: str = "") -> dict:
    if maximum <= minimum:
        maximum = minimum + 1.0
    position = (float(value) - minimum) / (maximum - minimum)
    clipped = max(0.0, min(1.0, position))
    return {
        "min": minimum,
        "max": maximum,
        "unit": unit,
        "percentile": round(clipped * 100.0, 1),
        "clipped": position != clipped,
    }


def _contract_mid(raw: dict) -> float | None:
    quote = raw.get("last_quote") or {}
    bid = _num(quote.get("bid") or quote.get("bid_price") or quote.get("bp"))
    ask = _num(quote.get("ask") or quote.get("ask_price") or quote.get("ap"))
    midpoint = _num(quote.get("midpoint"))
    if midpoint is not None and midpoint > 0:
        return midpoint
    if bid and ask:
        return (bid + ask) / 2.0
    trade = raw.get("last_trade") or {}
    day = raw.get("day") or {}
    return _num(trade.get("price") or trade.get("p") or day.get("close") or day.get("c"))


def _spread_pct(raw: dict) -> float | None:
    quote = raw.get("last_quote") or {}
    bid = _num(quote.get("bid") or quote.get("bid_price") or quote.get("bp"))
    ask = _num(quote.get("ask") or quote.get("ask_price") or quote.get("ap"))
    mid = _contract_mid(raw)
    if not bid or not ask or not mid or ask < bid:
        return None
    return (ask - bid) / mid


def _clean_contract(raw: dict, today: date, underlying_price: float | None) -> dict | None:
    details = raw.get("details") or {}
    expiration = _parse_expiration(details.get("expiration_date"))
    strike = _num(details.get("strike_price"))
    contract_type = str(details.get("contract_type") or "").lower()
    if expiration is None or strike is None or contract_type not in {"call", "put"}:
        return None
    day = raw.get("day") or {}
    greeks = raw.get("greeks") or {}
    underlying = raw.get("underlying_asset") or {}
    price = underlying_price or _num(underlying.get("price"))
    return {
        "ticker": details.get("ticker"),
        "type": contract_type,
        "expiration": expiration.isoformat(),
        "dte": max(0, (expiration - today).days),
        "strike": strike,
        "open_interest": _positive(raw.get("open_interest")),
        "volume": _positive(day.get("volume") or day.get("v")),
        "iv": _num(raw.get("implied_volatility")),
        "delta": _num(greeks.get("delta")),
        "gamma": _num(greeks.get("gamma")),
        "theta": _num(greeks.get("theta")),
        "vega": _num(greeks.get("vega")),
        "mid": _contract_mid(raw),
        "spread_pct": _spread_pct(raw),
        "break_even": _num(raw.get("break_even_price")),
        "distance_pct": _pct_distance(strike, price),
    }


def _weighted_average(items: list[dict], key: str, weight_key: str = "open_interest") -> float | None:
    numerator = 0.0
    denominator = 0.0
    for item in items:
        value = _num(item.get(key))
        if value is None:
            continue
        weight = max(_positive(item.get(weight_key)), _positive(item.get("volume")), 1.0)
        numerator += value * weight
        denominator += weight
    return round(numerator / denominator, 4) if denominator else None


def _nearest_contract(items: list[dict], target_delta: float | None, price: float | None, contract_type: str) -> dict | None:
    pool = [item for item in items if item.get("type") == contract_type]
    if not pool:
        return None
    if target_delta is not None and any(item.get("delta") is not None for item in pool):
        return min(pool, key=lambda item: abs(float(item.get("delta") or 0.0) - target_delta))
    if price:
        return min(pool, key=lambda item: abs(float(item.get("strike") or 0.0) - price))
    return max(pool, key=lambda item: float(item.get("open_interest") or 0.0))


def _max_pain(items: list[dict]) -> dict | None:
    strikes = sorted({float(item["strike"]) for item in items if item.get("strike") is not None})
    if not strikes:
        return None
    best_strike = None
    best_payout = None
    for settlement in strikes:
        payout = 0.0
        for item in items:
            strike = float(item.get("strike") or 0.0)
            oi = float(item.get("open_interest") or 0.0)
            if item.get("type") == "call":
                payout += max(0.0, settlement - strike) * oi * 100.0
            elif item.get("type") == "put":
                payout += max(0.0, strike - settlement) * oi * 100.0
        if best_payout is None or payout < best_payout:
            best_strike = settlement
            best_payout = payout
    if best_strike is None:
        return None
    return {"strike": round(best_strike, 4), "estimated_payout": round(float(best_payout or 0.0), 2)}


def _gex(items: list[dict], underlying_price: float | None) -> float | None:
    if not underlying_price or underlying_price <= 0:
        return None
    total = 0.0
    seen = False
    for item in items:
        gamma = _num(item.get("gamma"))
        if gamma is None:
            continue
        sign = 1.0 if item.get("type") == "call" else -1.0
        total += sign * gamma * float(item.get("open_interest") or 0.0) * 100.0 * underlying_price * underlying_price * 0.01
        seen = True
    return round(total, 2) if seen else None


def _top_oi(items: list[dict], contract_type: str, limit: int = 3) -> list[dict]:
    rows = sorted(
        [item for item in items if item.get("type") == contract_type and float(item.get("open_interest") or 0.0) > 0],
        key=lambda item: float(item.get("open_interest") or 0.0),
        reverse=True,
    )
    return [
        {
            "expiration": item.get("expiration"),
            "dte": item.get("dte"),
            "strike": item.get("strike"),
            "open_interest": int(item.get("open_interest") or 0),
            "distance_pct": item.get("distance_pct"),
            "iv": item.get("iv"),
        }
        for item in rows[:limit]
    ]


def _expiration_summary(expiration: str, items: list[dict], underlying_price: float | None, today: date) -> dict:
    call_items = [item for item in items if item.get("type") == "call"]
    put_items = [item for item in items if item.get("type") == "put"]
    call_oi = sum(float(item.get("open_interest") or 0.0) for item in call_items)
    put_oi = sum(float(item.get("open_interest") or 0.0) for item in put_items)
    call_volume = sum(float(item.get("volume") or 0.0) for item in call_items)
    put_volume = sum(float(item.get("volume") or 0.0) for item in put_items)
    max_pain = _max_pain(items)
    if max_pain and underlying_price:
        max_pain["distance_pct"] = _pct_distance(float(max_pain["strike"]), underlying_price)
    atm_call = _nearest_contract(items, 0.50, underlying_price, "call")
    atm_put = _nearest_contract(items, -0.50, underlying_price, "put")
    skew_call = _nearest_contract(items, 0.25, underlying_price, "call")
    skew_put = _nearest_contract(items, -0.25, underlying_price, "put")
    skew = None
    if skew_call and skew_put and skew_call.get("iv") is not None and skew_put.get("iv") is not None:
        skew = round(float(skew_put["iv"]) - float(skew_call["iv"]), 4)
    expiry_date = _parse_expiration(expiration) or today
    return {
        "expiration": expiration,
        "dte": max(0, (expiry_date - today).days),
        "contracts": len(items),
        "call_oi": int(call_oi),
        "put_oi": int(put_oi),
        "put_call_oi_ratio": _safe_ratio(put_oi, call_oi),
        "call_volume": int(call_volume),
        "put_volume": int(put_volume),
        "put_call_volume_ratio": _safe_ratio(put_volume, call_volume),
        "weighted_iv": _weighted_average(items, "iv"),
        "atm_iv": _weighted_average([item for item in (atm_call, atm_put) if item], "iv"),
        "skew_25d": skew,
        "max_pain": max_pain,
        "net_gex": _gex(items, underlying_price),
        "top_call_oi": _top_oi(items, "call", 1)[0] if _top_oi(items, "call", 1) else None,
        "top_put_oi": _top_oi(items, "put", 1)[0] if _top_oi(items, "put", 1) else None,
    }


def _score_options(summary: dict) -> tuple[float, str, list[str]]:
    score = 0.0
    reasons: list[str] = []
    pcr_oi = summary.get("put_call_oi_ratio")
    pcr_volume = summary.get("put_call_volume_ratio")
    skew = summary.get("skew_25d")
    gex = summary.get("net_gex")
    if pcr_oi is not None:
        if pcr_oi < 0.7:
            score += 1.0
            reasons.append("OI PCR偏低，认购仓位更活跃")
        elif pcr_oi > 1.25:
            score -= 1.0
            reasons.append("OI PCR偏高，保护性/看空仓位更重")
    if pcr_volume is not None:
        if pcr_volume < 0.75:
            score += 0.8
            reasons.append("成交PCR偏低，短线认购成交占优")
        elif pcr_volume > 1.35:
            score -= 0.8
            reasons.append("成交PCR偏高，短线认沽成交占优")
    if skew is not None:
        if skew > 0.04:
            score -= 0.7
            reasons.append("25D偏斜偏向认沽，尾部风险溢价抬升")
        elif skew < -0.02:
            score += 0.4
            reasons.append("25D偏斜偏向认购，上行需求更强")
    if gex is not None:
        if gex < 0:
            score -= 0.6
            reasons.append("简化GEX为负，波动放大风险更高")
        elif gex > 0:
            score += 0.3
            reasons.append("简化GEX为正，价格钉扎/缓冲倾向更强")
    score = round(max(-5.0, min(5.0, score)), 2)
    if score >= 1.4:
        label = "期权偏多"
    elif score <= -1.4:
        label = "期权偏空"
    elif score <= -0.5:
        label = "期权偏谨慎"
    elif score >= 0.5:
        label = "期权略偏多"
    else:
        label = "期权中性"
    return score, label, reasons


def analyze_option_chain(
    symbol: str,
    raw_chain: list[dict],
    *,
    underlying_price: float | None = None,
    today: date | None = None,
) -> dict:
    today = today or datetime.now(timezone.utc).date()
    clean = [item for raw in raw_chain if (item := _clean_contract(raw, today, underlying_price))]
    if not clean:
        return {
            "symbol": symbol.upper(),
            "status": "no_data",
            "label": "期权数据不足",
            "contracts": 0,
            "explanation": "Massive/Polygon 未返回可用期权链快照。",
        }
    underlying = underlying_price
    if underlying is None:
        for raw in raw_chain:
            underlying = _num((raw.get("underlying_asset") or {}).get("price"))
            if underlying:
                break

    by_expiration: dict[str, list[dict]] = defaultdict(list)
    for item in clean:
        by_expiration[str(item["expiration"])].append(item)
    expirations = sorted(by_expiration)
    expiry_summaries = [
        _expiration_summary(expiration, by_expiration[expiration], underlying, today)
        for expiration in expirations[:8]
    ]
    focus = max(
        expiry_summaries,
        key=lambda item: int(item.get("call_oi") or 0) + int(item.get("put_oi") or 0),
    )
    all_calls = [item for item in clean if item.get("type") == "call"]
    all_puts = [item for item in clean if item.get("type") == "put"]
    call_oi = sum(float(item.get("open_interest") or 0.0) for item in all_calls)
    put_oi = sum(float(item.get("open_interest") or 0.0) for item in all_puts)
    call_volume = sum(float(item.get("volume") or 0.0) for item in all_calls)
    put_volume = sum(float(item.get("volume") or 0.0) for item in all_puts)
    quoted_spreads = [float(item["spread_pct"]) for item in clean if item.get("spread_pct") is not None and float(item["spread_pct"]) >= 0]
    summary = {
        "symbol": symbol.upper(),
        "status": "ok",
        "source": "massive:options_snapshot",
        "underlying_price": None if underlying is None else round(float(underlying), 4),
        "contracts": len(clean),
        "expiration_count": len(expirations),
        "call_oi": int(call_oi),
        "put_oi": int(put_oi),
        "put_call_oi_ratio": _safe_ratio(put_oi, call_oi),
        "call_volume": int(call_volume),
        "put_volume": int(put_volume),
        "put_call_volume_ratio": _safe_ratio(put_volume, call_volume),
        "weighted_iv": _weighted_average(clean, "iv"),
        "focus_expiration": focus.get("expiration"),
        "focus_dte": focus.get("dte"),
        "atm_iv": focus.get("atm_iv"),
        "skew_25d": focus.get("skew_25d"),
        "max_pain": focus.get("max_pain"),
        "net_gex": focus.get("net_gex"),
        "top_call_oi": _top_oi(clean, "call"),
        "top_put_oi": _top_oi(clean, "put"),
        "liquidity": {
            "quoted_contracts": len(quoted_spreads),
            "avg_spread_pct": round(sum(quoted_spreads) / len(quoted_spreads), 4) if quoted_spreads else None,
        },
        "expirations": expiry_summaries[:5],
    }
    score, label, reasons = _score_options(summary)
    summary["score"] = score
    summary["score_range"] = _score_meta(score, -5, 5)
    summary["label"] = label
    summary["reasons"] = reasons
    parts = []
    if summary["put_call_oi_ratio"] is not None:
        parts.append(f"OI PCR {summary['put_call_oi_ratio']}")
    if summary["put_call_volume_ratio"] is not None:
        parts.append(f"成交PCR {summary['put_call_volume_ratio']}")
    if summary.get("atm_iv") is not None:
        parts.append(f"近月ATM IV {float(summary['atm_iv']) * 100:.1f}%")
    if summary.get("max_pain"):
        parts.append(f"最大痛点 {summary['max_pain']['strike']}")
    if summary.get("net_gex") is not None:
        parts.append(f"简化GEX {summary['net_gex']:.0f}")
    summary["explanation"] = "；".join(parts) if parts else "期权链可用，但关键指标较少。"
    return summary
