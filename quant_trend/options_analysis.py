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


def _score_label(value: float, labels: tuple[str, str, str, str]) -> str:
    if value >= 75:
        return labels[3]
    if value >= 50:
        return labels[2]
    if value >= 25:
        return labels[1]
    return labels[0]


def _alert(title: str, detail: str, score: float, *, tone: str = "warn", metric: str | None = None, threshold: str | None = None) -> dict:
    clipped = round(max(0.0, min(100.0, float(score))), 1)
    if clipped >= 75:
        severity = "高"
    elif clipped >= 50:
        severity = "中高"
    elif clipped >= 25:
        severity = "中"
    else:
        severity = "低"
    return {
        "title": title,
        "detail": detail,
        "score": clipped,
        "score_range": _score_meta(clipped, 0, 100),
        "severity": severity,
        "tone": tone,
        "metric": metric,
        "threshold": threshold,
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


def _top_unusual_contracts(items: list[dict], limit: int = 5) -> list[dict]:
    candidates = []
    for item in items:
        volume = float(item.get("volume") or 0.0)
        if volume < 10:
            continue
        open_interest = float(item.get("open_interest") or 0.0)
        volume_oi_ratio = volume / max(open_interest, 1.0)
        short_dte_bonus = 10.0 if int(item.get("dte") or 0) <= 14 else 0.0
        score = min(100.0, volume_oi_ratio * 65.0 + min(volume / 2000.0, 1.0) * 25.0 + short_dte_bonus)
        if score < 18:
            continue
        candidates.append(
            {
                "ticker": item.get("ticker"),
                "type": item.get("type"),
                "expiration": item.get("expiration"),
                "dte": item.get("dte"),
                "strike": item.get("strike"),
                "volume": int(volume),
                "open_interest": int(open_interest),
                "volume_oi_ratio": round(volume_oi_ratio, 4),
                "distance_pct": item.get("distance_pct"),
                "iv": item.get("iv"),
                "spread_pct": None if item.get("spread_pct") is None else round(float(item.get("spread_pct")), 4),
                "score": round(score, 1),
                "score_range": _score_meta(score, 0, 100),
            }
        )
    return sorted(candidates, key=lambda row: (float(row.get("score") or 0.0), int(row.get("volume") or 0)), reverse=True)[:limit]


def _term_structure(expiry_summaries: list[dict]) -> dict:
    rows = [item for item in sorted(expiry_summaries, key=lambda row: int(row.get("dte") or 0)) if item.get("atm_iv") is not None]
    if not rows:
        return {"front_atm_iv": None, "back_atm_iv": None, "front_iv_premium": None}
    front = rows[0]
    back_rows = rows[1:4]
    back_iv = None
    if back_rows:
        values = [float(item["atm_iv"]) for item in back_rows if item.get("atm_iv") is not None]
        back_iv = round(sum(values) / len(values), 4) if values else None
    premium = None
    if back_iv and back_iv > 0:
        premium = round(float(front["atm_iv"]) / back_iv - 1.0, 4)
    return {
        "front_expiration": front.get("expiration"),
        "front_dte": front.get("dte"),
        "front_atm_iv": front.get("atm_iv"),
        "back_atm_iv": back_iv,
        "front_iv_premium": premium,
    }


def _expected_move_pct(atm_iv: float | None, dte: int | float | None) -> float | None:
    iv = _num(atm_iv)
    days = _num(dte)
    if iv is None or days is None or days <= 0:
        return None
    return round(iv * math.sqrt(max(days, 1.0) / 365.0), 4)


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


def _option_risk_and_alerts(summary: dict, unusual_contracts: list[dict], term: dict) -> tuple[dict, dict, list[dict], list[dict], str]:
    alerts: list[dict] = []
    signals: list[dict] = []
    risk_score = 10.0
    anomaly_score = 5.0

    pcr_oi = summary.get("put_call_oi_ratio")
    pcr_volume = summary.get("put_call_volume_ratio")
    atm_iv = summary.get("atm_iv")
    skew = summary.get("skew_25d")
    gex = summary.get("net_gex")
    liquidity = summary.get("liquidity") or {}
    avg_spread = liquidity.get("avg_spread_pct")
    volume_oi_ratio = summary.get("volume_oi_ratio")
    expected_move = summary.get("expected_move_pct")
    front_iv_premium = term.get("front_iv_premium")

    direction_detail = []
    direction_label = "方向中性"
    direction_score = float(summary.get("score") or 0.0)
    if pcr_oi is not None:
        direction_detail.append(f"OI PCR {pcr_oi}")
    if pcr_volume is not None:
        direction_detail.append(f"成交PCR {pcr_volume}")
    if direction_score <= -1.4:
        direction_label = "认沽/保护占优"
    elif direction_score >= 1.4:
        direction_label = "认购占优"
    elif direction_score < -0.5:
        direction_label = "略偏保护"
    elif direction_score > 0.5:
        direction_label = "略偏认购"
    signals.append(
        {
            "name": "方向压力",
            "label": direction_label,
            "score": round(direction_score, 2),
            "score_range": _score_meta(direction_score, -5, 5),
            "detail": "；".join(direction_detail) or "PCR 数据不足",
        }
    )

    if pcr_oi is not None and pcr_oi > 1.25:
        add = 9 if pcr_oi < 2 else 15
        risk_score += add
        alerts.append(_alert("认沽仓位偏重", f"OI PCR {pcr_oi}，保护性/看空仓位高于认购。", 45 + add * 3, tone="danger", metric="OI PCR", threshold=">1.25偏谨慎，>2偏拥挤"))
    elif pcr_oi is not None and pcr_oi < 0.65:
        anomaly_score += 6
        alerts.append(_alert("认购仓位拥挤", f"OI PCR {pcr_oi}，认购未平仓更集中，追涨拥挤度上升。", 40, tone="positive", metric="OI PCR", threshold="<0.65偏认购"))
    if pcr_volume is not None and pcr_volume > 1.35:
        add = 8 if pcr_volume < 2 else 13
        risk_score += add
        anomaly_score += 8
        alerts.append(_alert("当日认沽成交占优", f"成交PCR {pcr_volume}，短线保护/看空成交更活跃。", 48 + add * 3, tone="danger", metric="成交PCR", threshold=">1.35偏保护，>2偏强"))
    elif pcr_volume is not None and pcr_volume < 0.65:
        anomaly_score += 7
        alerts.append(_alert("当日认购成交占优", f"成交PCR {pcr_volume}，短线认购成交更活跃。", 44, tone="positive", metric="成交PCR", threshold="<0.65偏认购"))

    vol_risk_score = 0.0
    if atm_iv is not None:
        if atm_iv >= 1.2:
            vol_risk_score = 86
            risk_score += 16
        elif atm_iv >= 0.8:
            vol_risk_score = 68
            risk_score += 11
        elif atm_iv >= 0.55:
            vol_risk_score = 48
            risk_score += 6
        else:
            vol_risk_score = 25
        detail = f"ATM IV {atm_iv * 100:.1f}%"
        if expected_move is not None:
            detail += f"，焦点到期预期波动约±{expected_move * 100:.1f}%"
        signals.append(
            {
                "name": "波动溢价",
                "label": _score_label(vol_risk_score, ("偏低", "正常", "偏高", "极高")),
                "score": vol_risk_score,
                "score_range": _score_meta(vol_risk_score, 0, 100),
                "detail": detail,
            }
        )
        if vol_risk_score >= 68:
            alerts.append(_alert("隐含波动偏高", detail, vol_risk_score, tone="warn", metric="ATM IV", threshold=">80%偏高，>120%极高"))

    if skew is not None:
        skew_abs_score = min(100.0, abs(float(skew)) * 900.0)
        label = "认沽尾部溢价" if skew > 0.04 else "认购上行溢价" if skew < -0.02 else "偏斜温和"
        signals.append(
            {
                "name": "尾部偏斜",
                "label": label,
                "score": round(skew_abs_score, 1),
                "score_range": _score_meta(skew_abs_score, 0, 100),
                "detail": f"25D Put IV - Call IV = {skew}",
            }
        )
        if skew > 0.04:
            risk_score += 9 if skew < 0.08 else 14
            alerts.append(_alert("下行尾部保护升温", f"25D偏斜 {skew}，认沽波动率高于认购。", 58 if skew < 0.08 else 78, tone="danger", metric="25D偏斜", threshold=">0.04偏保护，>0.08强保护"))
        elif skew < -0.04:
            anomaly_score += 5
            alerts.append(_alert("上行追逐偏强", f"25D偏斜 {skew}，认购波动率相对更贵。", 45, tone="positive", metric="25D偏斜", threshold="<-0.04偏上行追逐"))

    gex_score = 35.0
    if gex is not None:
        if gex < 0:
            risk_score += 12
            anomaly_score += 4
            gex_score = 70
            label = "负GEX放大波动"
            alerts.append(_alert("负GEX波动放大", f"简化GEX {gex:.0f}，价格变动可能更容易被放大。", 70, tone="danger", metric="简化GEX", threshold="<0偏波动放大"))
        else:
            gex_score = 35
            label = "正GEX偏钉扎"
    else:
        label = "GEX不足"
    max_pain = summary.get("max_pain") or {}
    max_pain_distance = max_pain.get("distance_pct")
    if max_pain_distance is not None and abs(float(max_pain_distance)) <= 0.03:
        anomaly_score += 5
        alerts.append(_alert("最大痛点靠近现价", f"最大痛点 {max_pain.get('strike')}，距现价 {max_pain_distance * 100:.1f}%。", 42, tone="info", metric="最大痛点", threshold="距现价3%内"))
    signals.append(
        {
            "name": "GEX/磁吸",
            "label": label,
            "score": gex_score,
            "score_range": _score_meta(gex_score, 0, 100),
            "detail": f"简化GEX {gex:.0f}" if gex is not None else "Greeks 数据不足",
        }
    )

    flow_detail = []
    if volume_oi_ratio is not None:
        flow_detail.append(f"成交/OI {volume_oi_ratio}")
        if volume_oi_ratio > 0.5:
            anomaly_score += 20
            alerts.append(_alert("期权成交/OI显著放大", f"总成交/OI {volume_oi_ratio}，新成交相对未平仓过高。", 82, tone="warn", metric="成交/OI", threshold=">0.25异动，>0.5显著异动"))
        elif volume_oi_ratio > 0.25:
            anomaly_score += 11
            alerts.append(_alert("期权成交/OI偏高", f"总成交/OI {volume_oi_ratio}，短线成交活跃度偏高。", 58, tone="warn", metric="成交/OI", threshold=">0.25异动"))
    if pcr_oi is not None and pcr_volume is not None:
        divergence = abs(math.log((float(pcr_volume) + 0.05) / (float(pcr_oi) + 0.05)))
        if divergence > 0.7:
            anomaly_score += 10
            alerts.append(_alert("当日成交方向偏离存量仓位", f"成交PCR {pcr_volume} vs OI PCR {pcr_oi}，短线资金方向和存量仓位差异大。", min(85, 45 + divergence * 25), tone="warn", metric="PCR背离", threshold="log差>0.7"))
    if unusual_contracts:
        best = unusual_contracts[0]
        anomaly_score += min(22, float(best.get("score") or 0) * 0.25)
        direction = "Call" if best.get("type") == "call" else "Put"
        alerts.append(
            _alert(
                f"{direction}单合约成交异动",
                f"{best.get('expiration')} {direction} {best.get('strike')} 成交/OI {best.get('volume_oi_ratio')}，成交 {best.get('volume')} 张。",
                max(45, float(best.get("score") or 0)),
                tone="positive" if best.get("type") == "call" else "danger",
                metric="单合约成交/OI",
                threshold="成交>=10且成交/OI较高",
            )
        )
    flow_score = min(100.0, max(0.0, anomaly_score))
    signals.append(
        {
            "name": "成交异动",
            "label": _score_label(flow_score, ("平静", "略活跃", "活跃", "显著异动")),
            "score": round(flow_score, 1),
            "score_range": _score_meta(flow_score, 0, 100),
            "detail": "；".join(flow_detail) or "成交/OI不足",
        }
    )

    if front_iv_premium is not None:
        premium_score = min(100.0, abs(float(front_iv_premium)) * 240.0)
        signals.append(
            {
                "name": "期限结构",
                "label": "近月IV倒挂" if front_iv_premium > 0.12 else "远月更贵" if front_iv_premium < -0.12 else "期限温和",
                "score": round(premium_score, 1),
                "score_range": _score_meta(premium_score, 0, 100),
                "detail": f"近月ATM IV相对后续均值 {front_iv_premium * 100:.1f}%",
            }
        )
        if front_iv_premium > 0.15:
            risk_score += 8
            anomaly_score += 9
            alerts.append(_alert("近月IV倒挂", f"近月ATM IV比后续到期高 {front_iv_premium * 100:.1f}%，可能反映近端事件/财报/消息风险。", 62, tone="warn", metric="近月IV溢价", threshold=">15%"))

    if avg_spread is not None:
        liq_score = 20.0
        if avg_spread > 0.25:
            liq_score = 82
            risk_score += 12
        elif avg_spread > 0.12:
            liq_score = 62
            risk_score += 8
        elif avg_spread > 0.06:
            liq_score = 42
            risk_score += 4
        signals.append(
            {
                "name": "流动性",
                "label": _score_label(liq_score, ("较好", "尚可", "偏差", "很差")),
                "score": liq_score,
                "score_range": _score_meta(liq_score, 0, 100),
                "detail": f"平均买卖价差 {avg_spread * 100:.1f}%",
            }
        )
        if liq_score >= 62:
            alerts.append(_alert("期权价差偏宽", f"平均买卖价差 {avg_spread * 100:.1f}%，期权信号噪声和交易成本更高。", liq_score, tone="warn", metric="平均价差", threshold=">12%偏宽，>25%很宽"))

    risk_score = round(max(0.0, min(100.0, risk_score)), 1)
    anomaly_score = round(max(0.0, min(100.0, anomaly_score)), 1)
    risk = {
        "score": risk_score,
        "score_range": _score_meta(risk_score, 0, 100),
        "label": _score_label(risk_score, ("风险较低", "中等风险", "风险偏高", "高风险")),
    }
    anomaly = {
        "score": anomaly_score,
        "score_range": _score_meta(anomaly_score, 0, 100),
        "label": _score_label(anomaly_score, ("暂无明显异动", "轻微异动", "异动偏强", "显著异动")),
    }
    sorted_alerts = sorted(alerts, key=lambda item: float(item.get("score") or 0.0), reverse=True)[:6]
    top_alert = sorted_alerts[0]["title"] if sorted_alerts else "暂无明显期权异动"
    move_text = f"、焦点到期预期±{expected_move * 100:.1f}%" if expected_move is not None else ""
    summary_text = f"{risk['label']}，{anomaly['label']}；{direction_label}{move_text}。主要提示：{top_alert}。"
    return risk, anomaly, sorted_alerts, signals, summary_text


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
    total_oi = call_oi + put_oi
    total_volume = call_volume + put_volume
    quoted_spreads = [float(item["spread_pct"]) for item in clean if item.get("spread_pct") is not None and float(item["spread_pct"]) >= 0]
    term = _term_structure(expiry_summaries)
    unusual_contracts = _top_unusual_contracts(clean)
    summary = {
        "symbol": symbol.upper(),
        "status": "ok",
        "source": "massive:options_snapshot",
        "underlying_price": None if underlying is None else round(float(underlying), 4),
        "contracts": len(clean),
        "expiration_count": len(expirations),
        "total_oi": int(total_oi),
        "total_volume": int(total_volume),
        "volume_oi_ratio": _safe_ratio(total_volume, total_oi),
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
        "expected_move_pct": _expected_move_pct(focus.get("atm_iv"), focus.get("dte")),
        "term_structure": term,
        "top_call_oi": _top_oi(clean, "call"),
        "top_put_oi": _top_oi(clean, "put"),
        "unusual_contracts": unusual_contracts,
        "liquidity": {
            "quoted_contracts": len(quoted_spreads),
            "avg_spread_pct": round(sum(quoted_spreads) / len(quoted_spreads), 4) if quoted_spreads else None,
        },
        "expirations": expiry_summaries[:5],
    }
    score, label, reasons = _score_options(summary)
    risk, anomaly, risk_alerts, signals, symbol_summary = _option_risk_and_alerts(summary, unusual_contracts, term)
    summary["score"] = score
    summary["score_range"] = _score_meta(score, -5, 5)
    summary["label"] = label
    summary["reasons"] = reasons
    summary["risk"] = risk
    summary["anomaly"] = anomaly
    summary["risk_alerts"] = risk_alerts
    summary["signals"] = signals
    summary["symbol_summary"] = symbol_summary
    summary["llm_signal_payload"] = {
        "symbol_summary": symbol_summary,
        "risk": risk,
        "anomaly": anomaly,
        "alerts": risk_alerts,
        "signals": signals,
        "expected_move_pct": summary.get("expected_move_pct"),
        "term_structure": term,
    }
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
    if summary.get("expected_move_pct") is not None:
        parts.append(f"焦点到期预期±{float(summary['expected_move_pct']) * 100:.1f}%")
    summary["explanation"] = "；".join(parts) if parts else "期权链可用，但关键指标较少。"
    return summary
