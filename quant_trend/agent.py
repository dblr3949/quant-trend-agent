import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Callable

from .data import load_symbol
from .indicators import average_true_range, rolling_high, simple_moving_average
from .market_data import IntradayBar, Quote, quote_age_minutes
from .models import Bar
from .portfolio import Portfolio
from .strategy import latest_signal

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


@dataclass(frozen=True)
class TechnicalSnapshot:
    symbol: str
    price: float
    source: str
    quote_age_minutes: float | None
    close: float | None
    sma20: float | None
    sma50: float | None
    sma150: float | None
    atr14: float | None
    high60: float | None
    trend_action: str
    trend_score: int
    trend_stop: float | None
    trend_reason: str
    quote_asof: str | None = None


DEFAULT_CONFIG = {
    "symbols": ["MU", "AAOI", "INTC", "LITE", "MRVL"],
    "market_proxies": ["SPY", "SMH", "SOXX", "^VIX"],
    "base_target_weights": {
        "MU": 0.42,
        "MRVL": 0.30,
        "LITE": 0.24,
        "AAOI": 0.22,
        "INTC": 0.14,
    },
    "risk": {
        "max_gross_exposure": 2.0,
        "risk_on_gross_exposure": 1.65,
        "neutral_gross_exposure": 1.25,
        "risk_off_gross_exposure": 0.85,
        "max_symbol_weight": 0.48,
        "max_noncore_symbol_weight": 0.28,
        "min_trade_value": 500.0,
        "rebalance_band_pct": 0.015,
        "max_quote_age_minutes": 20.0,
        "max_quote_age_minutes_extended": 180.0,
        "max_quote_age_minutes_closed": 0.0,
        "limit_offset_bps": 15.0,
        "max_limit_offset_bps": 80.0,
        "min_margin_cushion": 50000.0,
        "margin_buy_power_haircut": 0.5,
        "stress_drop_pct": 0.05,
        "target_hint_upside_cap_risk_off": 0.25,
        "target_hint_upside_cap_neutral": 0.35,
        "target_hint_upside_cap_risk_on": 0.50,
    },
    "core_symbols": ["MU", "MRVL", "LITE"],
}


def _score_meta(value: float | int | None, minimum: float, maximum: float, unit: str = "") -> dict:
    if maximum <= minimum:
        maximum = minimum + 1.0
    raw = 0.0 if value is None else float(value)
    position = (raw - minimum) / (maximum - minimum)
    clipped = max(0.0, min(1.0, position))
    return {
        "min": minimum,
        "max": maximum,
        "unit": unit,
        "percentile": round(clipped * 100.0, 1),
        "clipped": position != clipped,
    }


def _is_volatility_proxy(symbol: str) -> bool:
    return "VIX" in str(symbol or "").upper()


def _is_true_vix_index(symbol: str) -> bool:
    return str(symbol or "").strip().upper() in {"^VIX", "VIX", "I:VIX"}


def _vix_level_contribution(symbol: str, price: float) -> tuple[float, str, str]:
    if price >= 30:
        return -4.0, "panic_vix", "VIX >= 30，波动率进入恐慌区，风险资产降权。"
    if price >= 25:
        return -2.0, "elevated_vix", "VIX >= 25，波动率偏高，风险偏好收缩。"
    if price <= 18:
        return 2.0, "calm_vix", "VIX <= 18，波动率低位，风险偏好加分。"
    if price <= 22:
        return 1.0, "normal_vix", "VIX <= 22，波动率正常偏低，轻度加分。"
    return 0.0, "middle_vix", f"{symbol} 位于 22~25 中性区间。"


def load_json(path: str | Path | None) -> dict:
    if not path:
        return {}
    file = Path(path)
    if not file.exists():
        return {}
    with file.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_agent_config(path: str | Path | None) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    override = load_json(path)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def _last(values: list[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def build_snapshot(symbol: str, bars: list[Bar], quote: Quote | None = None) -> TechnicalSnapshot:
    close = bars[-1].close if bars else None
    price = quote.price if quote else close
    if price is None:
        raise ValueError(f"{symbol}: no quote or historical close available")
    source = quote.source if quote else "daily_close"
    quote_age = quote_age_minutes(quote) if quote else None
    quote_asof = quote.asof if quote else None
    quote_source = str(quote.source) if quote else ""
    is_close_like = quote_source.endswith((":close", ":prev_close"))
    if quote and close is not None and is_close_like:
        quote_distance = abs((float(quote.price) / close) - 1.0) if close else 0.0
        # A prevDay close is always stale relative to the current session; an
        # IBKR close tick is only ignored when it disagrees with the daily close.
        if quote_source.endswith(":prev_close") or quote_distance >= 0.0005:
            price = close
            source = "daily_close:stale_close_ignored"
            quote_age = None
            quote_asof = None

    closes = [bar.close for bar in bars]
    sma20 = _last(simple_moving_average(closes, 20)) if bars else None
    sma50 = _last(simple_moving_average(closes, 50)) if bars else None
    sma150 = _last(simple_moving_average(closes, 150)) if bars else None
    atr14 = _last(average_true_range(bars, 14)) if bars else None
    high60 = _last(rolling_high(closes, 60)) if bars else None
    signal = latest_signal(symbol, bars) if bars else None

    return TechnicalSnapshot(
        symbol=symbol,
        price=float(price),
        source=source,
        quote_age_minutes=quote_age,
        close=close,
        sma20=sma20,
        sma50=sma50,
        sma150=sma150,
        atr14=atr14,
        high60=high60,
        trend_action=signal.action if signal else "hold",
        trend_score=signal.score if signal else 0,
        trend_stop=signal.stop if signal else None,
        trend_reason=signal.reason if signal else "insufficient_history",
        quote_asof=quote_asof,
    )


def _is_active_event(raw: dict, today: date) -> bool:
    expires = raw.get("expires")
    if not expires:
        return True
    try:
        return date.fromisoformat(str(expires)[:10]) >= today
    except ValueError:
        return True


def _bias_from_research(research: dict, symbol: str | None = None) -> float:
    if symbol:
        symbols = research.get("symbols", {})
        raw = symbols.get(symbol, symbols.get(symbol.upper(), {}))
        if isinstance(raw, dict):
            return float(raw.get("bias", raw.get("score", 0.0)))
        if raw not in (None, ""):
            return float(raw)
        raw_bias = research.get("symbol_bias", {}).get(symbol, research.get("symbol_bias", {}).get(symbol.upper(), 0.0))
        return float(raw_bias or 0.0)
    return float(research.get("macro_bias", 0.0)) + float(research.get("liquidity_bias", 0.0)) + float(research.get("geopolitical_bias", 0.0))


def _bar_lookback_count(bar_size: str, minutes: int = 30) -> int:
    raw = str(bar_size or "5 mins").lower()
    number = ""
    for char in raw:
        if char.isdigit():
            number += char
        elif number:
            break
    value = int(number or "5")
    if "hour" in raw:
        value *= 60
    return max(1, round(minutes / max(1, value)))


def summarize_intraday_bars(symbol: str, bars: list[IntradayBar], bar_size: str = "5 mins") -> dict | None:
    valid = [bar for bar in bars if bar.open > 0 and bar.high > 0 and bar.low > 0 and bar.close > 0]
    if not valid:
        return None

    first = valid[0]
    last = valid[-1]
    high = max(bar.high for bar in valid)
    low = min(bar.low for bar in valid)
    volume = sum(max(0.0, bar.volume) for bar in valid)
    if volume > 0:
        weighted = sum(((bar.average if bar.average and bar.average > 0 else bar.close) * max(0.0, bar.volume)) for bar in valid)
        vwap = weighted / volume
    else:
        vwap = None

    from_open = (last.close / first.open) - 1.0
    from_vwap = ((last.close / vwap) - 1.0) if vwap else None
    range_position = (last.close - low) / (high - low) if high > low else 0.5
    lookback = _bar_lookback_count(bar_size)
    prior = valid[-lookback - 1] if len(valid) > lookback else valid[0]
    last_30m = (last.close / prior.close) - 1.0 if prior.close else 0.0

    score = 0
    if from_open >= 0.015:
        score += 2
    elif from_open >= 0.004:
        score += 1
    elif from_open <= -0.015:
        score -= 2
    elif from_open <= -0.004:
        score -= 1

    if last_30m >= 0.006:
        score += 1
    elif last_30m <= -0.006:
        score -= 1

    if range_position >= 0.75:
        score += 1
    elif range_position <= 0.25:
        score -= 1

    if from_vwap is not None:
        if from_vwap >= 0.004:
            score += 1
        elif from_vwap <= -0.004:
            score -= 1

    score = max(-5, min(5, score))
    if score >= 3:
        label = "strong_up"
    elif score >= 1:
        label = "up"
    elif score <= -3:
        label = "strong_down"
    elif score <= -1:
        label = "down"
    else:
        label = "mixed"

    return {
        "symbol": symbol.upper(),
        "bar_count": len(valid),
        "bar_size": bar_size,
        "source": last.source,
        "first_timestamp": first.timestamp,
        "last_timestamp": last.timestamp,
        "open": round(first.open, 4),
        "high": round(high, 4),
        "low": round(low, 4),
        "close": round(last.close, 4),
        "volume": round(volume, 2),
        "vwap": round(vwap, 4) if vwap else None,
        "from_open_pct": round(from_open, 5),
        "from_vwap_pct": round(from_vwap, 5) if from_vwap is not None else None,
        "last_30m_pct": round(last_30m, 5),
        "range_position": round(range_position, 4),
        "score": score,
        "score_range": _score_meta(score, -5, 5),
        "label": label,
        "recent_bars": [asdict(bar) for bar in valid[-12:]],
    }


def _intraday_reason(summary: dict | None) -> tuple[float, str | None]:
    if not summary:
        return 1.0, None
    score = float(summary.get("score", 0))
    if score >= 3:
        return 1.12, "intraday_strong_up"
    if score >= 1:
        return 1.05, "intraday_up"
    if score <= -3:
        return 0.75, "intraday_strong_down"
    if score <= -1:
        return 0.88, "intraday_down"
    return 1.0, "intraday_mixed"


def classify_market_regime(snapshots: dict[str, TechnicalSnapshot], config: dict, research: dict, intraday: dict[str, dict] | None = None) -> dict:
    score = 0.0
    reasons: list[str] = []
    components: list[dict] = []
    proxies = [symbol.upper() for symbol in config.get("market_proxies", [])]
    intraday = intraday or {}

    for symbol in proxies:
        snap = snapshots.get(symbol)
        if not snap:
            reasons.append(f"{symbol}:missing")
            components.append({"symbol": symbol, "role": "missing", "score": 0.0, "contributions": [], "missing": True})
            continue
        component_score = 0.0
        component = {
            "symbol": symbol,
            "role": "volatility_index" if _is_volatility_proxy(symbol) else "risk_asset",
            "price": round(snap.price, 4),
            "source": snap.source,
            "quote_asof": snap.quote_asof,
            "quote_age_minutes": None if snap.quote_age_minutes is None else round(snap.quote_age_minutes, 2),
            "sma20": round(snap.sma20, 4) if snap.sma20 else None,
            "sma50": round(snap.sma50, 4) if snap.sma50 else None,
            "contributions": [],
        }
        if _is_volatility_proxy(symbol):
            level_score, level_reason, detail = _vix_level_contribution(symbol, snap.price)
            if not _is_true_vix_index(symbol):
                detail = detail.replace("VIX", "VIX 代理")
            score += level_score
            component_score += level_score
            reasons.append(f"{symbol}:{level_reason}")
            component["contributions"].append(
                {
                    "name": "VIX绝对水平" if _is_true_vix_index(symbol) else "波动率代理水平",
                    "score": round(level_score, 2),
                    "score_range": _score_meta(level_score, -4, 2),
                    "reason": level_reason,
                    "detail": detail,
                    "reference": ">=30 恐慌；25~30 偏高；22~25 中性；18~22 正常偏低；<=18 低波动。",
                }
            )
            day = intraday.get(symbol)
            if day:
                intraday_score = max(-1.5, min(1.5, float(day.get("score", 0)) * -0.35))
                if intraday_score:
                    score += intraday_score
                    component_score += intraday_score
                    reasons.append(f"{symbol}:intraday_inverse_{day.get('label', 'mixed')}:{intraday_score:+.1f}")
                component["intraday"] = {
                    "label": day.get("label"),
                    "score": day.get("score"),
                    "score_range": day.get("score_range"),
                    "regime_contribution": round(intraday_score, 2),
                    "from_open_pct": day.get("from_open_pct"),
                    "from_vwap_pct": day.get("from_vwap_pct"),
                    "last_30m_pct": day.get("last_30m_pct"),
                    "range_position": day.get("range_position"),
                }
                component["contributions"].append(
                    {
                        "name": "日内反向趋势",
                        "score": round(intraday_score, 2),
                        "score_range": _score_meta(intraday_score, -1.5, 1.5),
                        "reason": f"intraday_inverse_{day.get('label', 'mixed')}",
                        "detail": "VIX 日内走强代表风险偏好下降；VIX 日内走弱代表风险偏好改善。",
                        "reference": "分钟线原始分 -5~+5，乘以 -0.35 后封顶 ±1.5。",
                    }
                )
            component["score"] = round(component_score, 2)
            component["score_range"] = _score_meta(component_score, -6, 4)
            components.append(component)
            continue

        if snap.sma20 and snap.price > snap.sma20:
            contribution = 1.0
            reasons.append(f"{symbol}:above_sma20")
            detail = "现价站上20日均线，短中期风险资产趋势加分。"
        else:
            contribution = -1.0
            reasons.append(f"{symbol}:below_sma20")
            detail = "现价低于20日均线，短线趋势转弱扣分。"
        score += contribution
        component_score += contribution
        component["contributions"].append(
            {
                "name": "20日均线",
                "score": contribution,
                "score_range": _score_meta(contribution, -1, 1),
                "reason": "above_sma20" if contribution > 0 else "below_sma20",
                "detail": detail,
                "reference": "站上 +1；跌破 -1。",
            }
        )

        if snap.sma50 and snap.price > snap.sma50:
            contribution = 1.0
            reasons.append(f"{symbol}:above_sma50")
            detail = "现价站上50日均线，中期趋势加分。"
        else:
            contribution = -2.0
            reasons.append(f"{symbol}:below_sma50")
            detail = "现价跌破50日均线，中期趋势风险更高，扣分更重。"
        score += contribution
        component_score += contribution
        component["contributions"].append(
            {
                "name": "50日均线",
                "score": contribution,
                "score_range": _score_meta(contribution, -2, 1),
                "reason": "above_sma50" if contribution > 0 else "below_sma50",
                "detail": detail,
                "reference": "站上 +1；跌破 -2。",
            }
        )

        day = intraday.get(symbol)
        if day:
            intraday_score = max(-1.5, min(1.5, float(day.get("score", 0)) * 0.35))
            if intraday_score:
                score += intraday_score
                component_score += intraday_score
                reasons.append(f"{symbol}:intraday_{day.get('label', 'mixed')}:{intraday_score:+.1f}")
            component["intraday"] = {
                "label": day.get("label"),
                "score": day.get("score"),
                "score_range": day.get("score_range"),
                "regime_contribution": round(intraday_score, 2),
                "from_open_pct": day.get("from_open_pct"),
                "from_vwap_pct": day.get("from_vwap_pct"),
                "last_30m_pct": day.get("last_30m_pct"),
                "range_position": day.get("range_position"),
            }
            component["contributions"].append(
                {
                    "name": "当日分钟线",
                    "score": round(intraday_score, 2),
                    "score_range": _score_meta(intraday_score, -1.5, 1.5),
                    "reason": f"intraday_{day.get('label', 'mixed')}",
                    "detail": "开盘至今、VWAP、近30分钟和日内区间位置合成后，按 0.35 权重进入市场状态。",
                    "reference": "分钟线原始分 -5~+5，乘以 +0.35 后封顶 ±1.5。",
                }
            )
        component["score"] = round(component_score, 2)
        component["score_range"] = _score_meta(component_score, -5, 4)
        components.append(component)

    macro_bias = _bias_from_research(research)
    if macro_bias:
        score += macro_bias
        reasons.append(f"macro_overlay:{macro_bias:+.1f}")

    today = datetime.now(timezone.utc).date()
    for event in research.get("events", []):
        if not isinstance(event, dict) or not _is_active_event(event, today):
            continue
        severity = float(event.get("severity", 0.0))
        direction = str(event.get("direction", "neutral")).lower()
        signed = -severity if direction in {"negative", "risk_off", "bearish"} else severity if direction in {"positive", "risk_on", "bullish"} else 0.0
        if signed:
            score += signed
            reasons.append(f"event:{event.get('name', 'unnamed')}:{signed:+.1f}")

    risk = config["risk"]
    if score >= 4:
        label = "risk_on"
        target_gross = float(risk["risk_on_gross_exposure"])
    elif score <= -2:
        label = "risk_off"
        target_gross = float(risk["risk_off_gross_exposure"])
    else:
        label = "neutral"
        target_gross = float(risk["neutral_gross_exposure"])

    target_gross = min(target_gross, float(risk["max_gross_exposure"]))
    score = round(score, 2)
    return {
        "label": label,
        "score": score,
        "score_range": _score_meta(score, -10, 10),
        "target_gross_exposure": target_gross,
        "reasons": reasons,
        "components": components,
    }


def _thesis_status(portfolio: Portfolio, research: dict, symbol: str) -> str:
    raw = research.get("symbols", {}).get(symbol, {})
    if isinstance(raw, dict) and raw.get("thesis_status"):
        return str(raw["thesis_status"])
    position = portfolio.positions.get(symbol)
    return position.thesis_status if position else "intact"


def _conviction(portfolio: Portfolio, research: dict, symbol: str) -> float:
    raw = research.get("symbols", {}).get(symbol, {})
    if isinstance(raw, dict) and raw.get("conviction") not in (None, ""):
        return float(raw["conviction"])
    position = portfolio.positions.get(symbol)
    return position.conviction if position else 1.0


def _symbol_overlay(research: dict, symbol: str) -> dict:
    raw = research.get("symbols", {}).get(symbol, research.get("symbols", {}).get(symbol.upper(), {}))
    return raw if isinstance(raw, dict) else {}


def _bucket_multiplier(bucket: str, is_core_symbol: bool) -> tuple[float, list[str]]:
    normalized = (bucket or "auto").lower()
    if normalized == "auto":
        return (1.05 if is_core_symbol else 0.9), ["bucket_auto_core" if is_core_symbol else "bucket_auto_satellite"]
    if normalized == "core":
        return 1.12, ["bucket_core"]
    if normalized == "satellite":
        return 0.88, ["bucket_satellite"]
    if normalized == "watch":
        return 0.72, ["bucket_watch"]
    if normalized in {"trim", "exit", "cleanup"}:
        return 0.42, ["bucket_trim"]
    return 1.0, [f"bucket_{normalized}"]


def _constraint_multiplier(trade_constraint: str) -> tuple[float, list[str]]:
    normalized = (trade_constraint or "flexible").lower()
    if normalized == "flexible":
        return 1.0, []
    if normalized == "prefer_hold":
        return 1.02, ["constraint_prefer_hold"]
    if normalized == "soft_no_add":
        return 0.85, ["constraint_soft_no_add"]
    if normalized == "soft_no_reduce":
        return 1.08, ["constraint_soft_no_reduce"]
    if normalized == "reduce_only":
        return 0.7, ["constraint_reduce_only"]
    return 1.0, [f"constraint_{normalized}"]


def _apply_target_gross_hint(auto_target: float, portfolio: Portfolio, regime_label: str, risk: dict) -> tuple[float, list[str]]:
    hint = portfolio.target_gross_hint
    if hint is None or hint <= 0:
        return auto_target, []
    max_gross = float(risk["max_gross_exposure"])
    hint = max(0.0, min(float(hint), max_gross))
    if hint <= auto_target:
        return hint, [f"user_target_gross_conservative:{hint:.2f}"]
    cap_key = {
        "risk_off": "target_hint_upside_cap_risk_off",
        "neutral": "target_hint_upside_cap_neutral",
        "risk_on": "target_hint_upside_cap_risk_on",
    }.get(regime_label, "target_hint_upside_cap_neutral")
    allowed = min(max_gross, auto_target + float(risk.get(cap_key, 0.35)))
    applied = min(hint, allowed)
    if applied < hint:
        return applied, [f"user_target_gross_capped:{hint:.2f}->{applied:.2f}"]
    return applied, [f"user_target_gross_applied:{applied:.2f}"]


def _margin_cushion(portfolio: Portfolio) -> tuple[float | None, str | None]:
    if portfolio.maintenance_margin is not None:
        return portfolio.account_equity - float(portfolio.maintenance_margin), "maintenance_margin"
    if portfolio.excess_liquidity is not None:
        return float(portfolio.excess_liquidity), "legacy_excess_liquidity"
    return None, None


def _margin_buy_budget(portfolio: Portfolio, risk: dict) -> tuple[float | None, list[str]]:
    cushion, source = _margin_cushion(portfolio)
    if cushion is None:
        return None, []
    min_cushion = float(risk.get("min_margin_cushion", 50000.0))
    haircut = max(0.01, float(risk.get("margin_buy_power_haircut", 0.5)))
    surplus = float(cushion) - min_cushion
    if surplus <= 0:
        return 0.0, [f"margin_cushion_below_min:{float(cushion):.0f}<{min_cushion:.0f}"]
    return surplus / haircut, [f"margin_buy_budget:{surplus / haircut:.0f}", f"margin_cushion_source:{source}"]


def _soft_reduce_evidence(
    snapshot: TechnicalSnapshot,
    regime: dict,
    intraday_summary: dict | None,
    research_bias: float,
    current_weight: float,
    target_weight: float,
) -> float:
    score = 0.0
    overweight = max(0.0, current_weight - target_weight)
    score += min(2.0, overweight * 5.0)
    if snapshot.trend_action == "sell":
        score += 2.0
    elif snapshot.trend_action == "hold":
        score += 0.5
    elif snapshot.trend_action == "buy":
        score -= 1.0
    if snapshot.sma50 and snapshot.price < snapshot.sma50:
        score += 1.0
    if snapshot.trend_stop and snapshot.price < snapshot.trend_stop:
        score += 1.5
    if regime.get("label") == "risk_off":
        score += 1.0
    elif regime.get("label") == "risk_on":
        score -= 0.5
    if intraday_summary:
        score += max(-1.0, min(1.0, -float(intraday_summary.get("score", 0)) * 0.25))
    if research_bias:
        score += max(-1.0, min(1.0, -research_bias * 0.35))
    return round(score, 2)


def _symbol_multiplier(
    snapshot: TechnicalSnapshot,
    thesis_status: str,
    conviction: float,
    research_bias: float,
    intraday_summary: dict | None = None,
) -> tuple[float, list[str]]:
    multiplier = 1.0
    reasons: list[str] = []

    if snapshot.trend_action == "buy":
        multiplier *= 1.15
        reasons.append("trend_buy")
    elif snapshot.trend_action == "watch":
        multiplier *= 1.0
        reasons.append("trend_watch")
    elif snapshot.trend_action == "sell":
        multiplier *= 0.35
        reasons.append("trend_sell")
    else:
        multiplier *= 0.8
        reasons.append("trend_hold")

    if snapshot.sma50 and snapshot.price < snapshot.sma50:
        multiplier *= 0.55
        reasons.append("below_sma50")
    if snapshot.trend_stop and snapshot.price < snapshot.trend_stop:
        multiplier *= 0.35
        reasons.append("below_trend_stop")

    normalized_status = thesis_status.lower()
    if normalized_status in {"broken", "invalidated"}:
        return 0.0, reasons + ["thesis_broken"]
    if normalized_status in {"watch", "questioned", "weakening"}:
        multiplier *= 0.65
        reasons.append(f"thesis_{normalized_status}")

    multiplier *= max(0.35, min(1.25, 0.65 + 0.35 * conviction))
    if research_bias:
        multiplier *= max(0.4, min(1.6, 1.0 + 0.12 * research_bias))
        reasons.append(f"research_bias:{research_bias:+.1f}")

    intraday_multiplier, intraday_reason = _intraday_reason(intraday_summary)
    multiplier *= intraday_multiplier
    if intraday_reason:
        reasons.append(intraday_reason)

    return multiplier, reasons


def _soft_no_add_evidence(snapshot: TechnicalSnapshot, regime: dict, intraday_summary: dict | None, research_bias: float) -> float:
    score = 0.0
    if snapshot.trend_action == "buy":
        score += 2.0
    elif snapshot.trend_action == "watch":
        score += 0.5
    elif snapshot.trend_action == "sell":
        score -= 2.0
    score += max(-1.0, min(1.0, snapshot.trend_score / 4.0))
    if snapshot.sma20 and snapshot.price > snapshot.sma20:
        score += 0.5
    if snapshot.sma50 and snapshot.price > snapshot.sma50:
        score += 0.75
    if snapshot.trend_stop and snapshot.price < snapshot.trend_stop:
        score -= 1.5
    if regime.get("label") == "risk_on":
        score += 0.75
    elif regime.get("label") == "risk_off":
        score -= 1.0
    if intraday_summary:
        score += max(-1.25, min(1.25, float(intraday_summary.get("score", 0)) * 0.25))
    if research_bias:
        score += max(-1.0, min(1.0, research_bias * 0.35))
    return round(score, 2)


def _round_price(price: float) -> float:
    tick = 0.01 if price >= 1 else 0.0001
    return round(round(price / tick) * tick, 4 if tick < 0.01 else 2)


def _level_category(source: str) -> str:
    if "LVN" in source or "真空" in source:
        return "volume_void"
    if "POC" in source or "HVN" in source or "价值区" in source or "成交密集" in source:
        return "volume_profile"
    if "锚定VWAP" in source or "区间VWAP" in source:
        return "reference"
    if "当日" in source or "VWAP" in source or "30分钟" in source:
        return "intraday"
    if "摆动" in source or "前高" in source or "前低" in source:
        return "swing"
    if "均线" in source or "止损" in source:
        return "reference"
    return "daily_structure"


def _add_level(
    levels: list[dict],
    price: float | None,
    source: str,
    reference_price: float,
    category: str | None = None,
    **extra: object,
) -> None:
    if price is None or price <= 0:
        return
    level = {
        "price": float(price),
        "source": source,
        "category": category or _level_category(source),
        "distance_pct": round((float(price) / reference_price) - 1.0, 5) if reference_price else 0.0,
    }
    level.update({key: value for key, value in extra.items() if value is not None})
    levels.append(level)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _current_market_snapshot_label(asof: str | None) -> dict:
    parsed = _parse_timestamp(asof) or datetime.now(timezone.utc)
    ny_tz = ZoneInfo("America/New_York") if ZoneInfo else timezone.utc
    ny_time = parsed.astimezone(ny_tz)
    clock = ny_time.time()
    is_weekday = ny_time.weekday() < 5
    if is_weekday and dt_time(9, 30) <= clock < dt_time(16, 0):
        session = "regular"
        label = "盘中"
    elif is_weekday and dt_time(4, 0) <= clock < dt_time(9, 30):
        session = "premarket"
        label = "盘前"
    elif is_weekday and dt_time(16, 0) <= clock < dt_time(20, 0):
        session = "postmarket"
        label = "盘后"
    else:
        session = "closed"
        label = "闭市"
    return {
        "timestamp": parsed.isoformat(),
        "market_date": ny_time.date().isoformat(),
        "session": session,
        "session_label": label,
        "display": f"{label} {ny_time.strftime('%m-%d %H:%M')} ET",
    }


def _effective_quote_age_limit(session: str, risk: dict) -> float:
    """Max quote age (minutes) before a quote blocks adds, by trading session.

    Regular hours use the tight limit. Pre/post-market trades are sparse, so a
    last trade can legitimately be older without being "stale"; closed sessions
    disable the age block entirely (value <= 0) so overnight/weekend prep still
    produces suggestions off the last known price.
    """
    base = float(risk.get("max_quote_age_minutes", 20.0))
    if session == "regular":
        return base
    if session in {"premarket", "postmarket"}:
        return float(risk.get("max_quote_age_minutes_extended", base * 9))
    return float(risk.get("max_quote_age_minutes_closed", 0.0))


def _should_append_current_bar(
    last_bar: Bar | None,
    price: float,
    current_volume: float,
    intraday_summary: dict | None,
    session: str,
    quote_source: str,
) -> bool:
    if last_bar is None:
        return True
    if intraday_summary:
        return True
    if session == "closed" and str(quote_source).endswith(":close"):
        return False
    if session in {"regular", "premarket", "postmarket"}:
        return True
    distance = abs((price / last_bar.close) - 1.0) if last_bar.close else 0.0
    return current_volume > 0 or distance >= 0.0005


def _volume_levels_from_daily(bars: list[Bar], side: str, reference_price: float) -> list[dict]:
    recent = bars[-20:]
    if not recent:
        return []
    avg_volume = sum(bar.volume for bar in recent) / len(recent)
    if avg_volume <= 0:
        return []
    high_volume = [bar for bar in recent if bar.volume >= avg_volume * 1.2]
    levels: list[dict] = []
    for bar in high_volume[-5:]:
        if side == "buy":
            _add_level(levels, bar.low, "近20日高量支撑低点", reference_price)
            _add_level(levels, bar.close, "近20日高量成交收盘", reference_price)
        else:
            _add_level(levels, bar.high, "近20日高量压力高点", reference_price)
            _add_level(levels, bar.close, "近20日高量成交收盘", reference_price)
    return levels


def _volume_profile(bars: list[Bar], window: int, value_area_pct: float = 0.70) -> dict | None:
    recent = [bar for bar in bars[-window:] if bar.high > 0 and bar.low > 0 and bar.volume > 0]
    if len(recent) < 20:
        return None
    low = min(bar.low for bar in recent)
    high = max(bar.high for bar in recent)
    if high <= low:
        return None
    bin_count = max(16, min(34, round(len(recent) / 3)))
    bin_size = (high - low) / bin_count
    bins = [
        {
            "index": i,
            "low": low + i * bin_size,
            "high": low + (i + 1) * bin_size,
            "mid": low + (i + 0.5) * bin_size,
            "volume": 0.0,
            "touch_count": 0,
            "last_date": None,
        }
        for i in range(bin_count)
    ]
    for bar in recent:
        start = min(bin_count - 1, max(0, int((bar.low - low) / bin_size)))
        end = min(bin_count - 1, max(0, int((bar.high - low) / bin_size)))
        count = max(1, end - start + 1)
        allocated = bar.volume / count
        for index in range(start, end + 1):
            bins[index]["volume"] += allocated
            bins[index]["touch_count"] += 1
            bins[index]["last_date"] = bar.date

    total_volume = sum(float(item["volume"]) for item in bins)
    if total_volume <= 0:
        return None
    ranked = sorted(bins, key=lambda item: item["volume"], reverse=True)
    for rank, item in enumerate(ranked, start=1):
        item["profile_rank"] = rank

    poc = ranked[0]
    included = {int(poc["index"])}
    accumulated = float(poc["volume"])
    left = int(poc["index"]) - 1
    right = int(poc["index"]) + 1
    while accumulated < total_volume * value_area_pct and (left >= 0 or right < len(bins)):
        left_volume = float(bins[left]["volume"]) if left >= 0 else -1.0
        right_volume = float(bins[right]["volume"]) if right < len(bins) else -1.0
        if right_volume >= left_volume:
            included.add(right)
            accumulated += max(0.0, right_volume)
            right += 1
        else:
            included.add(left)
            accumulated += max(0.0, left_volume)
            left -= 1
    value_bins = [bins[index] for index in sorted(included)]
    vah = value_bins[-1]
    val = value_bins[0]
    return {
        "window": window,
        "low": low,
        "high": high,
        "total_volume": total_volume,
        "bins": bins,
        "ranked": ranked,
        "poc": poc,
        "vah": vah,
        "val": val,
        "value_area_indices": included,
        "value_area_pct": value_area_pct,
    }


def _profile_level_extra(profile: dict, bin_item: dict, role: str) -> dict:
    total = max(1.0, float(profile.get("total_volume") or 0.0))
    bins = profile.get("bins") or []
    index = int(bin_item.get("index", 0))
    zone_volume = 0.0
    for neighbor in range(max(0, index - 1), min(len(bins), index + 2)):
        zone_volume += float(bins[neighbor].get("volume") or 0.0)
    last_date = bin_item.get("last_date")
    last_profile_date = None
    valid_dates = [item.get("last_date") for item in bins if item.get("last_date")]
    if valid_dates:
        last_profile_date = max(valid_dates)
    recency_days = None
    if last_date and last_profile_date:
        recency_days = (last_profile_date - last_date).days
    return {
        "profile_window": int(profile.get("window") or 0),
        "profile_role": role,
        "profile_rank": int(bin_item.get("profile_rank") or 0),
        "profile_bin_low": round(float(bin_item.get("low") or 0.0), 4),
        "profile_bin_high": round(float(bin_item.get("high") or 0.0), 4),
        "volume_share_pct": round(float(bin_item.get("volume") or 0.0) / total, 5),
        "chip_share_pct": round(zone_volume / total, 5),
        "touch_count": int(bin_item.get("touch_count") or 0),
        "recency_days": recency_days,
    }


def _local_minima_bins(profile: dict, limit: int = 3) -> list[dict]:
    bins = profile.get("bins") or []
    if len(bins) < 5:
        return []
    candidates = []
    for index in range(1, len(bins) - 1):
        current = float(bins[index].get("volume") or 0.0)
        if current <= float(bins[index - 1].get("volume") or 0.0) and current <= float(bins[index + 1].get("volume") or 0.0):
            candidates.append(bins[index])
    return sorted(candidates, key=lambda item: float(item.get("volume") or 0.0))[:limit]


def _volume_profile_levels_from_daily(bars: list[Bar], side: str, reference_price: float) -> list[dict]:
    profiles = [profile for window in (20, 60, 90, 126) if (profile := _volume_profile(bars, window))]
    levels: list[dict] = []
    for profile in profiles:
        window = int(profile["window"])
        poc = profile["poc"]
        val = profile["val"]
        vah = profile["vah"]
        top_hvn = [
            item
            for item in profile["ranked"][:6]
            if int(item["index"]) not in {int(poc["index"]), int(val["index"]), int(vah["index"])}
        ][:3]
        low_lvn = _local_minima_bins(profile, 3)
        candidates: list[tuple[float, str, str, dict]] = [
            (float(poc["mid"]), f"{window}日POC成交最密集价", "POC", poc),
        ]
        if side == "buy":
            candidates.append((float(val["low"]), f"{window}日价值区下沿VAL", "VAL", val))
        else:
            candidates.append((float(vah["high"]), f"{window}日价值区上沿VAH", "VAH", vah))
        candidates.extend((float(item["mid"]), f"{window}日HVN高量节点", "HVN", item) for item in top_hvn)
        candidates.extend((float(item["mid"]), f"{window}日LVN低量真空", "LVN", item) for item in low_lvn)

        for price, source, role, item in candidates:
            if side == "buy" and price <= reference_price:
                _add_level(levels, price, source, reference_price, _level_category(source), **_profile_level_extra(profile, item, role))
            elif side == "sell" and price >= reference_price:
                _add_level(levels, price, source, reference_price, _level_category(source), **_profile_level_extra(profile, item, role))
    return levels


def _swing_levels_for_window(bars: list[Bar], side: str, reference_price: float, window: int) -> list[dict]:
    recent = bars[-window:]
    if len(recent) < 9:
        return []
    levels: list[dict] = []
    for index in range(2, len(recent) - 2):
        window = recent[index - 2 : index + 3]
        bar = recent[index]
        if side == "buy" and bar.low == min(item.low for item in window) and bar.low <= reference_price:
            _add_level(levels, bar.low, f"{len(recent)}日摆动前低", reference_price, "swing")
        elif side == "sell" and bar.high == max(item.high for item in window) and bar.high >= reference_price:
            _add_level(levels, bar.high, f"{len(recent)}日摆动前高", reference_price, "swing")
    return levels[-8:]


def _swing_levels_from_daily(bars: list[Bar], side: str, reference_price: float) -> list[dict]:
    levels: list[dict] = []
    for window in (20, 60, 90, 126):
        levels.extend(_swing_levels_for_window(bars, side, reference_price, window))
    return levels


def _anchored_vwap_levels_from_daily(bars: list[Bar], side: str, reference_price: float) -> list[dict]:
    valid = [bar for bar in bars if bar.volume > 0 and bar.high > 0 and bar.low > 0 and bar.close > 0]
    if len(valid) < 20:
        return []
    levels: list[dict] = []
    anchor_indices: list[tuple[int, int, str]] = []
    for window in (20, 60, 90):
        recent = valid[-window:]
        if len(recent) < max(10, window // 2):
            continue
        if side == "buy":
            anchor_bar = min(recent, key=lambda item: item.low)
            label = f"从{window}日低点锚定VWAP"
        else:
            anchor_bar = max(recent, key=lambda item: item.high)
            label = f"从{window}日高点锚定VWAP"
        index = valid.index(anchor_bar)
        anchor_indices.append((index, window, label))
    seen: set[int] = set()
    for index, window, label in anchor_indices:
        if index in seen:
            continue
        seen.add(index)
        rows = valid[index:]
        volume = sum(row.volume for row in rows)
        if volume <= 0:
            continue
        vwap = sum(((row.high + row.low + row.close) / 3.0) * row.volume for row in rows) / volume
        touch_count = sum(1 for row in rows if row.low <= vwap <= row.high)
        recency = (valid[-1].date - rows[-1].date).days if rows else None
        if (side == "buy" and vwap <= reference_price) or (side == "sell" and vwap >= reference_price):
            _add_level(
                levels,
                vwap,
                label,
                reference_price,
                "reference",
                profile_window=window,
                profile_role="anchored_vwap",
                touch_count=touch_count,
                recency_days=recency,
            )
    return levels


def _volume_levels_from_intraday(bars: list[IntradayBar], side: str, reference_price: float) -> list[dict]:
    valid = [bar for bar in bars if bar.volume > 0 and bar.high > 0 and bar.low > 0 and bar.close > 0]
    if not valid:
        return []
    avg_volume = sum(bar.volume for bar in valid) / len(valid)
    if avg_volume <= 0:
        return []
    high_volume = [bar for bar in valid if bar.volume >= avg_volume * 1.35]
    levels: list[dict] = []
    for bar in high_volume[-6:]:
        if side == "buy":
            _add_level(levels, bar.low, "当日高量支撑低点", reference_price)
            _add_level(levels, bar.average or bar.close, "当日高量成交均价", reference_price)
        else:
            _add_level(levels, bar.high, "当日高量压力高点", reference_price)
            _add_level(levels, bar.average or bar.close, "当日高量成交均价", reference_price)
    return levels


def _limit_levels(
    side: str,
    snapshot: TechnicalSnapshot,
    daily_bars: list[Bar],
    intraday_summary: dict | None,
    intraday_bars: list[IntradayBar],
) -> list[dict]:
    price = snapshot.price
    levels: list[dict] = []

    for window in (5, 10, 20, 50, 63, 90, 126):
        recent = daily_bars[-window:]
        if len(recent) < min(window, 5):
            continue
        if side == "buy":
            _add_level(levels, min(bar.low for bar in recent), f"近{window}日日线支撑", price)
        else:
            _add_level(levels, max(bar.high for bar in recent), f"近{window}日日线压力", price)

    if daily_bars:
        previous = daily_bars[-1]
        _add_level(levels, previous.close, "最近日线收盘", price)

    _add_level(levels, snapshot.sma20, "20日均线", price)
    _add_level(levels, snapshot.sma50, "50日均线", price)
    if snapshot.trend_stop:
        _add_level(levels, snapshot.trend_stop, "趋势止损线", price)

    if intraday_summary:
        if side == "buy":
            _add_level(levels, intraday_summary.get("low"), "当日低点", price)
            _add_level(levels, intraday_summary.get("vwap"), "当日VWAP", price)
        else:
            _add_level(levels, intraday_summary.get("high"), "当日高点", price)
            _add_level(levels, intraday_summary.get("vwap"), "当日VWAP", price)

    valid_intraday = [bar for bar in intraday_bars if bar.high > 0 and bar.low > 0]
    if valid_intraday:
        recent_count = min(6, len(valid_intraday))
        recent = valid_intraday[-recent_count:]
        if side == "buy":
            _add_level(levels, min(bar.low for bar in recent), "近30分钟支撑", price)
        else:
            _add_level(levels, max(bar.high for bar in recent), "近30分钟压力", price)

    levels.extend(_volume_levels_from_daily(daily_bars, side, price))
    levels.extend(_volume_profile_levels_from_daily(daily_bars, side, price))
    levels.extend(_swing_levels_from_daily(daily_bars, side, price))
    levels.extend(_anchored_vwap_levels_from_daily(daily_bars, side, price))
    levels.extend(_volume_levels_from_intraday(intraday_bars, side, price))
    return levels


def _dedupe_levels(levels: list[dict], price: float) -> list[dict]:
    seen: set[tuple[float, str]] = set()
    result = []
    for level in sorted(levels, key=lambda item: abs(item["price"] - price)):
        key = (round(level["price"], 2), level["source"])
        if key in seen:
            continue
        seen.add(key)
        result.append(level)
    return result


def _level_weight(level: dict) -> float:
    category = str(level.get("category") or "")
    source = str(level.get("source") or "")
    weight = {
        "volume_profile": 3.2,
        "swing": 2.8,
        "daily_structure": 2.2,
        "reference": 1.7,
        "volume_void": 1.2,
        "intraday": 0.8,
    }.get(category, 1.0)
    if "90日" in source or "126日" in source or "63日" in source:
        weight += 0.6
    if "20日" in source or "50日" in source:
        weight += 0.25
    if "当日" in source or ("VWAP" in source and "锚定" not in source and "区间" not in source) or "30分钟" in source:
        weight -= 0.35
    return weight


def _level_confluence(level: dict, levels: list[dict], price: float) -> int:
    tolerance = max(price * 0.0035, 0.08)
    return sum(1 for other in levels if abs(float(other.get("price", 0)) - float(level.get("price", 0))) <= tolerance)


def _level_tier(level: dict) -> str:
    category = str(level.get("category") or "")
    if category == "intraday":
        return "日内辅助"
    distance = abs(float(level.get("distance_pct", 0.0)))
    if distance <= 0.035:
        return "近端"
    if distance <= 0.12:
        return "主结构"
    return "深水/高抛"


def _touch_stats_for_level(level: dict, bars: list[Bar], reference_price: float) -> tuple[int, int | None]:
    if not bars:
        return int(level.get("touch_count") or 0), level.get("recency_days") if isinstance(level.get("recency_days"), int) else None
    price = float(level.get("price") or 0.0)
    tolerance = max(reference_price * 0.0035, 0.08)
    touched = [bar for bar in bars[-126:] if bar.low - tolerance <= price <= bar.high + tolerance]
    if not touched:
        raw_recency = level.get("recency_days")
        return int(level.get("touch_count") or 0), raw_recency if isinstance(raw_recency, int) else None
    recency_days = (bars[-1].date - touched[-1].date).days
    return max(int(level.get("touch_count") or 0), len(touched)), recency_days


def _level_strength_score(level: dict, levels: list[dict], bars: list[Bar], reference_price: float) -> tuple[float, dict]:
    category = str(level.get("category") or "")
    chip_share = float(level.get("chip_share_pct") or level.get("volume_share_pct") or 0.0)
    confluence = _level_confluence(level, levels, reference_price)
    touch_count, recency_days = _touch_stats_for_level(level, bars, reference_price)
    category_points = {
        "volume_profile": 1.15,
        "swing": 0.95,
        "daily_structure": 0.75,
        "reference": 0.55,
        "volume_void": 0.25,
        "intraday": 0.15,
    }.get(category, 0.45)
    volume_points = min(1.55, chip_share * 18.0)
    confluence_points = min(1.10, max(0, confluence - 1) * 0.28)
    touch_points = min(0.90, touch_count * 0.12)
    if recency_days is None:
        recency_points = 0.0
    elif recency_days <= 7:
        recency_points = 0.55
    elif recency_days <= 21:
        recency_points = 0.38
    elif recency_days <= 63:
        recency_points = 0.18
    else:
        recency_points = 0.05
    if category == "volume_void":
        volume_points *= 0.35
    score = max(0.0, min(5.0, category_points + volume_points + confluence_points + touch_points + recency_points))
    return round(score, 2), {
        "confluence_count": confluence,
        "touch_count": touch_count,
        "recency_days": recency_days,
        "tier": _level_tier(level),
        "level_strength_score": round(score, 2),
        "level_strength_range": _score_meta(score, 0, 5),
    }


def _enrich_levels(levels: list[dict], price: float, bars: list[Bar]) -> list[dict]:
    enriched = []
    for level in levels:
        _, metrics = _level_strength_score(level, levels, bars, price)
        item = {**level, **metrics}
        enriched.append(item)
    return enriched


def _structure_offsets(side: str, snapshot: TechnicalSnapshot, regime: dict | None, market_structure: dict | None = None) -> tuple[float, float, str]:
    atr_pct = (snapshot.atr14 / snapshot.price) if snapshot.atr14 and snapshot.price else 0.025
    label = str((regime or {}).get("label") or "neutral")
    market_score = float((market_structure or {}).get("score") or 0.0)
    if side == "buy":
        min_depth = max(0.012, min(0.045, atr_pct * 0.22))
        max_depth = max(0.045, min(0.18, atr_pct * 1.35))
        if label == "risk_off":
            min_depth += 0.012
            max_depth += 0.035
        elif label == "risk_on":
            min_depth = max(0.008, min_depth - 0.004)
            max_depth = max(0.035, max_depth - 0.015)
        if market_score <= -1.5:
            min_depth += 0.006
            max_depth += 0.02
        elif market_score >= 1.5:
            min_depth = max(0.006, min_depth - 0.003)
            max_depth = max(0.03, max_depth - 0.01)
        return min_depth, max_depth, "历史结构买入折价"
    min_lift = max(0.010, min(0.035, atr_pct * 0.18))
    max_lift = max(0.035, min(0.16, atr_pct * 1.10))
    if label == "risk_off":
        min_lift = max(0.006, min_lift - 0.004)
        max_lift = max(0.028, max_lift - 0.015)
    elif label == "risk_on":
        min_lift += 0.006
        max_lift += 0.025
    if market_score <= -1.5:
        min_lift = max(0.006, min_lift - 0.003)
        max_lift = max(0.025, max_lift - 0.012)
    elif market_score >= 1.5:
        min_lift += 0.004
        max_lift += 0.015
    return min_lift, max_lift, "历史结构卖出溢价"


def _select_structure_level(side: str, levels: list[dict], price: float, min_offset: float, max_offset: float) -> dict | None:
    if side == "buy":
        candidates = [level for level in levels if price * (1 - max_offset) <= level["price"] <= price * (1 - min_offset)]
    else:
        candidates = [level for level in levels if price * (1 + min_offset) <= level["price"] <= price * (1 + max_offset)]
    if not candidates:
        return None
    target_offset = min_offset + (max_offset - min_offset) * 0.42
    scored = []
    for level in candidates:
        distance = abs(float(level["price"]) / price - 1.0)
        confluence = _level_confluence(level, levels, price)
        strength = float(level.get("level_strength_score") or 0.0)
        chip_share = float(level.get("chip_share_pct") or level.get("volume_share_pct") or 0.0)
        score = (
            _level_weight(level)
            + strength * 0.55
            + min(2.5, confluence * 0.35)
            + min(0.9, chip_share * 6.0)
            - abs(distance - target_offset) / max(target_offset, 0.001) * 0.45
        )
        if str(level.get("category")) == "intraday":
            score -= 0.9
        scored.append((score, level))
    return max(scored, key=lambda item: item[0])[1]


def _limit_candidates(side: str, levels: list[dict], price: float, lower_bound: float, upper_bound: float, limit: int = 8) -> list[dict]:
    if side == "buy":
        eligible = [level for level in levels if level.get("price") and level["price"] <= price and lower_bound * 0.96 <= level["price"] <= upper_bound * 1.01]
    else:
        eligible = [level for level in levels if level.get("price") and level["price"] >= price and lower_bound * 0.99 <= level["price"] <= upper_bound * 1.04]
    if not eligible:
        return []

    def candidate_score(level: dict) -> tuple[float, float]:
        distance = abs(float(level.get("distance_pct", 0.0)))
        strength = float(level.get("level_strength_score") or 0.0)
        chip_share = float(level.get("chip_share_pct") or level.get("volume_share_pct") or 0.0)
        inside_band = lower_bound <= float(level["price"]) <= upper_bound
        score = _level_weight(level) + strength * 0.7 + min(1.2, chip_share * 7.5) + (0.8 if inside_band else 0.0)
        if str(level.get("category")) == "intraday":
            score -= 1.0
        return score, -distance

    result = []
    for index, level in enumerate(sorted(eligible, key=candidate_score, reverse=True)[:limit], start=1):
        item = _round_level(level)
        item["candidate_id"] = f"C{index}"
        item["candidate_price"] = _round_price(float(level["price"]))
        item["candidate_score"] = round(candidate_score(level)[0], 2)
        item["within_offset_band"] = lower_bound <= float(level["price"]) <= upper_bound
        result.append(item)
    return result


def _round_level(level: dict) -> dict:
    distance_pct = round(float(level.get("distance_pct", 0.0)), 5)
    rounded = {
        "price": round(float(level.get("price", 0.0)), 4),
        "source": str(level.get("source", "")),
        "category": str(level.get("category", "")),
        "distance_pct": distance_pct,
    }
    optional = {
        "tier": level.get("tier"),
        "profile_window": level.get("profile_window"),
        "profile_role": level.get("profile_role"),
        "profile_rank": level.get("profile_rank"),
        "profile_bin_low": level.get("profile_bin_low"),
        "profile_bin_high": level.get("profile_bin_high"),
        "volume_share_pct": level.get("volume_share_pct"),
        "chip_share_pct": level.get("chip_share_pct"),
        "confluence_count": level.get("confluence_count"),
        "touch_count": level.get("touch_count"),
        "recency_days": level.get("recency_days"),
        "level_strength_score": level.get("level_strength_score"),
        "level_strength_range": level.get("level_strength_range"),
    }
    for key, value in optional.items():
        if value is not None:
            rounded[key] = value
    if str(level.get("profile_role") or "").upper() == "POC":
        distance = abs(distance_pct)
        if distance <= 0.035:
            context = "近端POC"
        elif distance <= 0.15:
            context = "主结构POC"
        else:
            context = "深水/远端POC"
        rounded["poc_side"] = "下方支撑侧" if distance_pct < 0 else "上方压力侧" if distance_pct > 0 else "现价平衡区"
        rounded["poc_context"] = context
    return rounded


def _display_levels(levels: list[dict], side: str, price: float, limit: int = 8) -> list[dict]:
    eligible = [level for level in levels if level.get("price") and (level["price"] <= price if side == "buy" else level["price"] >= price)]
    historical = [level for level in eligible if str(level.get("category")) != "intraday"]
    intraday = [level for level in eligible if str(level.get("category")) == "intraday"]

    def score(level: dict) -> tuple[float, float, float]:
        distance = abs(float(level.get("distance_pct", 0.0)))
        confluence = _level_confluence(level, eligible, price)
        strength = float(level.get("level_strength_score") or 0.0)
        return (_level_weight(level) + strength * 0.55 + min(2.0, confluence * 0.25), -distance, strength)

    near = [level for level in historical if level.get("tier") == "近端"]
    primary = [level for level in historical if level.get("tier") == "主结构"]
    deep = [level for level in historical if level.get("tier") == "深水/高抛"]
    selected: list[dict] = []
    selected.extend(sorted(near, key=score, reverse=True)[:2])
    selected.extend(level for level in sorted(primary, key=score, reverse=True) if level not in selected)
    if len(selected) < max(1, limit - 1):
        selected.extend(level for level in sorted(deep, key=score, reverse=True)[:2] if level not in selected)
    selected = selected[: max(0, limit - 1)]
    if len(selected) < max(0, limit - 1):
        selected.extend(level for level in sorted(historical, key=score, reverse=True) if level not in selected)
        selected = selected[: max(0, limit - 1)]
    poc_levels = [level for level in historical if str(level.get("profile_role") or "").upper() == "POC"]
    if poc_levels:
        sorted_poc = sorted(poc_levels, key=score, reverse=True)
        best_poc = next((level for level in sorted_poc if level.get("tier") != "深水/高抛"), None)
        if best_poc and best_poc not in selected:
            replace_index = None
            for index in range(len(selected) - 1, -1, -1):
                role = str(selected[index].get("profile_role") or "").upper()
                if role not in {"POC", "HVN", "VAL", "VAH"}:
                    replace_index = index
                    break
            if replace_index is None and selected:
                replace_index = len(selected) - 1
            if replace_index is not None:
                selected[replace_index] = best_poc
            else:
                selected.append(best_poc)
        elif not best_poc and len(selected) < max(0, limit - 1):
            selected.append(sorted_poc[0])
    if intraday:
        selected.append(sorted(intraday, key=lambda item: (abs(float(item.get("distance_pct", 0.0))), -float(item.get("level_strength_score") or 0.0)))[0])
    return [_round_level(level) for level in selected[:limit]]


def _score_label(score: float) -> str:
    if score >= 3:
        return "strong"
    if score >= 1:
        return "positive"
    if score <= -3:
        return "weak"
    if score <= -1:
        return "negative"
    return "mixed"


def _price_volume_analysis(
    symbol: str,
    snapshot: TechnicalSnapshot,
    daily_bars: list[Bar],
    intraday_summary: dict | None,
    intraday_bars: list[IntradayBar],
) -> dict:
    price = float(snapshot.price)
    recent = daily_bars[-63:]
    recent20 = daily_bars[-20:]
    support_levels = _enrich_levels(_dedupe_levels(_limit_levels("buy", snapshot, daily_bars, intraday_summary, intraday_bars), price), price, daily_bars)
    resistance_levels = _enrich_levels(_dedupe_levels(_limit_levels("sell", snapshot, daily_bars, intraday_summary, intraday_bars), price), price, daily_bars)
    supports = _display_levels(support_levels, "buy", price, 8)
    resistances = _display_levels(resistance_levels, "sell", price, 8)

    low = min((bar.low for bar in recent), default=price)
    high = max((bar.high for bar in recent), default=price)
    range_position = (price - low) / (high - low) if high > low else 0.5
    avg_volume20 = sum(bar.volume for bar in recent20) / len(recent20) if recent20 else None
    last_bar = daily_bars[-1] if daily_bars else None
    volume_ratio20 = (last_bar.volume / avg_volume20) if last_bar and avg_volume20 and avg_volume20 > 0 else None

    components: list[dict] = []
    score = 0.0

    trend_score = 0.0
    if snapshot.trend_action == "buy":
        trend_score += 1.6
    elif snapshot.trend_action == "watch":
        trend_score += 0.4
    elif snapshot.trend_action == "sell":
        trend_score -= 1.8
    if snapshot.sma20:
        trend_score += 0.7 if price >= snapshot.sma20 else -0.7
    if snapshot.sma50:
        trend_score += 0.9 if price >= snapshot.sma50 else -1.0
    if snapshot.trend_stop and price < snapshot.trend_stop:
        trend_score -= 1.4
    trend_score = max(-3.5, min(3.5, trend_score))
    score += trend_score
    components.append(
        {
            "name": "趋势/均线",
            "score": round(trend_score, 2),
            "score_range": _score_meta(trend_score, -3.5, 3.5),
            "detail": "趋势信号、20/50日均线和趋势止损线。",
        }
    )

    structure_score = 0.0
    nearest_support = supports[0] if supports else None
    nearest_resistance = resistances[0] if resistances else None
    support_distance = abs(float(nearest_support["distance_pct"])) if nearest_support else None
    resistance_distance = abs(float(nearest_resistance["distance_pct"])) if nearest_resistance else None
    if support_distance is not None and support_distance <= 0.025:
        structure_score += 0.8
    if resistance_distance is not None and resistance_distance <= 0.025:
        structure_score -= 0.6
    if 0.35 <= range_position <= 0.78:
        structure_score += 0.5
    elif range_position >= 0.9:
        structure_score -= 0.4
    elif range_position <= 0.18:
        structure_score -= 0.2
    structure_score = max(-2.0, min(2.0, structure_score))
    score += structure_score
    components.append(
        {
            "name": "支撑/压力",
            "score": round(structure_score, 2),
            "score_range": _score_meta(structure_score, -2.0, 2.0),
            "detail": "近5/10/20日高低、均线、VWAP和高量区的近端位置。",
        }
    )

    volume_score = 0.0
    if last_bar and volume_ratio20 is not None:
        day_return = (last_bar.close / last_bar.open - 1.0) if last_bar.open else 0.0
        if volume_ratio20 >= 1.25 and day_return > 0:
            volume_score += 0.9
        elif volume_ratio20 >= 1.25 and day_return < 0:
            volume_score -= 0.9
        elif volume_ratio20 <= 0.7:
            volume_score -= 0.2
    high_volume_levels = [level for level in supports + resistances if "高量" in level.get("source", "")]
    if high_volume_levels:
        volume_score += 0.3
    volume_score = max(-1.5, min(1.5, volume_score))
    score += volume_score
    components.append(
        {
            "name": "量能确认",
            "score": round(volume_score, 2),
            "score_range": _score_meta(volume_score, -1.5, 1.5),
            "detail": "20日量比、放量阳/阴线和高量成交区。",
        }
    )

    day_score = 0.0
    if intraday_summary:
        day_score = max(-1.5, min(1.5, float(intraday_summary.get("score", 0)) * 0.35))
    score += day_score
    components.append(
        {
            "name": "当日趋势",
            "score": round(day_score, 2),
            "score_range": _score_meta(day_score, -1.5, 1.5),
            "detail": "开盘至今、VWAP、近30分钟和日内区间位置。",
        }
    )

    score = round(max(-6.0, min(6.0, score)), 2)
    chart_prices = [price, low, high]
    chart_prices.extend(level["price"] for level in supports[:4])
    chart_prices.extend(level["price"] for level in resistances[:4])
    chart_low = min(chart_prices) if chart_prices else price
    chart_high = max(chart_prices) if chart_prices else price
    if chart_high <= chart_low:
        chart_high = price * 1.03
        chart_low = price * 0.97
    pad = max(price * 0.005, (chart_high - chart_low) * 0.08)

    recent_volume = []
    for bar in daily_bars[-12:]:
        recent_volume.append(
            {
                "date": bar.date.isoformat(),
                "close": round(bar.close, 4),
                "volume": round(bar.volume, 2),
                "up": bar.close >= bar.open,
                "volume_ratio20": round(bar.volume / avg_volume20, 3) if avg_volume20 else None,
            }
        )
    recent_bars = []
    for bar in daily_bars[-60:]:
        recent_bars.append(
            {
                "date": bar.date.isoformat(),
                "open": round(bar.open, 4),
                "high": round(bar.high, 4),
                "low": round(bar.low, 4),
                "close": round(bar.close, 4),
                "volume": round(bar.volume, 2),
                "up": bar.close >= bar.open,
                "volume_ratio20": round(bar.volume / avg_volume20, 3) if avg_volume20 else None,
            }
        )
    current_volume = float(intraday_summary.get("volume") or 0.0) if intraday_summary else 0.0
    base_open = last_bar.close if last_bar else price
    if intraday_summary:
        current_open = float(intraday_summary.get("open") or base_open)
        current_high = max(price, current_open, float(intraday_summary.get("high") or price))
        current_low = min(price, current_open, float(intraday_summary.get("low") or price))
        current_asof = str(intraday_summary.get("last_timestamp") or snapshot.quote_asof or "")
    else:
        current_open = float(base_open)
        current_high = max(price, current_open)
        current_low = min(price, current_open)
        current_asof = snapshot.quote_asof or ""
    current_marker = _current_market_snapshot_label(current_asof)
    append_current = _should_append_current_bar(last_bar, price, current_volume, intraday_summary, current_marker["session"], snapshot.source)
    if append_current:
        recent_volume.append(
            {
                "date": current_marker["display"],
                "timestamp": current_marker["timestamp"],
                "market_date": current_marker["market_date"],
                "session": current_marker["session"],
                "session_label": current_marker["session_label"],
                "close": round(price, 4),
                "volume": round(current_volume, 2),
                "up": price >= current_open,
                "volume_ratio20": round(current_volume / avg_volume20, 3) if avg_volume20 and current_volume > 0 else None,
                "is_current": True,
                "is_complete_daily": False,
            }
        )
    recent_volume = recent_volume[-13:]
    if append_current:
        recent_bars.append(
            {
                "date": current_marker["display"],
                "timestamp": current_marker["timestamp"],
                "market_date": current_marker["market_date"],
                "session": current_marker["session"],
                "session_label": current_marker["session_label"],
                "open": round(current_open, 4),
                "high": round(current_high, 4),
                "low": round(current_low, 4),
                "close": round(price, 4),
                "volume": round(current_volume, 2),
                "up": price >= current_open,
                "volume_ratio20": round(current_volume / avg_volume20, 3) if avg_volume20 and current_volume > 0 else None,
                "is_current": True,
                "is_complete_daily": False,
            }
        )
    recent_bars = recent_bars[-61:]

    explanation_parts = []
    if nearest_support:
        explanation_parts.append(f"近支撑 {nearest_support['price']}({nearest_support['source']})")
    if nearest_resistance:
        explanation_parts.append(f"近压力 {nearest_resistance['price']}({nearest_resistance['source']})")
    if volume_ratio20 is not None:
        explanation_parts.append(f"20日量比 {volume_ratio20:.2f}")
    if intraday_summary:
        explanation_parts.append(f"日内 {intraday_summary.get('label')} {intraday_summary.get('score')}")

    return {
        "symbol": symbol.upper(),
        "price": round(price, 4),
        "score": score,
        "score_range": _score_meta(score, -6.0, 6.0),
        "label": _score_label(score),
        "range_low": round(low, 4),
        "range_high": round(high, 4),
        "range_position": round(range_position, 4),
        "sma20": round(snapshot.sma20, 4) if snapshot.sma20 else None,
        "sma50": round(snapshot.sma50, 4) if snapshot.sma50 else None,
        "trend_stop": round(snapshot.trend_stop, 4) if snapshot.trend_stop else None,
        "volume_ratio20": round(volume_ratio20, 3) if volume_ratio20 is not None else None,
        "avg_volume20": round(avg_volume20, 2) if avg_volume20 is not None else None,
        "supports": supports,
        "resistances": resistances,
        "components": components,
        "recent_volume": recent_volume,
        "recent_bars": recent_bars,
        "current_marker": current_marker,
        "current_marker_appended": append_current,
        "chart_range": {"min": round(chart_low - pad, 4), "max": round(chart_high + pad, 4)},
        "explanation": "；".join(explanation_parts) or "量价数据不足，先按趋势和仓位框架处理。",
    }


def _price_volume_multiplier(analysis: dict | None) -> tuple[float, list[str]]:
    if not analysis:
        return 1.0, []
    score = float(analysis.get("score") or 0.0)
    multiplier = max(0.78, min(1.22, 1.0 + score * 0.035))
    return multiplier, [f"price_volume_score:{score:+.1f}"]


def _market_structure_context(analyses: dict[str, dict], regime: dict) -> dict:
    components = []
    for symbol in sorted(analyses):
        raw_score = float(analyses[symbol].get("score") or 0.0)
        if _is_volatility_proxy(symbol):
            components.append({"symbol": symbol, "score": -raw_score, "raw_score": raw_score, "direction": "inverse_vol"})
        else:
            components.append({"symbol": symbol, "score": raw_score, "raw_score": raw_score, "direction": "risk"})
    if components:
        score = sum(item["score"] for item in components) / len(components)
    else:
        score = float(regime.get("score") or 0.0) * 0.4
    if score >= 2.0:
        label = "market_structure_strong"
    elif score >= 0.75:
        label = "market_structure_positive"
    elif score <= -2.0:
        label = "market_structure_weak"
    elif score <= -0.75:
        label = "market_structure_negative"
    else:
        label = "market_structure_mixed"
    score = round(score, 2)
    return {
        "label": label,
        "score": score,
        "score_range": _score_meta(score, -6, 6),
        "components": components,
        "usage": "SPY/SMH/SOXX 量价分与 ^VIX 反向量价分，用于调节买入折价和卖出溢价。",
    }


def _limit_price(
    side: str,
    snapshot: TechnicalSnapshot,
    config: dict,
    daily_bars: list[Bar] | None = None,
    intraday_summary: dict | None = None,
    intraday_bars: list[IntradayBar] | None = None,
    regime: dict | None = None,
    market_structure: dict | None = None,
) -> tuple[float, dict]:
    atr_pct = (snapshot.atr14 / snapshot.price) if snapshot.atr14 and snapshot.price else 0.02
    min_offset, max_offset, offset_policy = _structure_offsets(side, snapshot, regime, market_structure)
    price = snapshot.price

    daily_bars = daily_bars or []
    intraday_bars = intraday_bars or []
    levels = _enrich_levels(_dedupe_levels(_limit_levels(side, snapshot, daily_bars, intraday_summary, intraday_bars), price), price, daily_bars)
    selected_source = offset_policy
    selected_raw = price * (1 - min_offset if side == "buy" else 1 + min_offset)
    selected = _select_structure_level(side, levels, price, min_offset, max_offset)

    if side == "buy":
        lower_bound = price * (1 - max_offset)
        upper_bound = price * (1 - min_offset)
        if selected:
            selected_raw = selected["price"]
            selected_source = selected["source"]
        else:
            below_band = [level for level in levels if level["price"] < lower_bound and str(level.get("category")) != "intraday"]
            if below_band:
                selected = max(below_band, key=lambda item: _level_weight(item))
                selected_raw = lower_bound
                selected_source = f"{selected['source']}偏远，按历史结构边界"
            else:
                selected_raw = price * (1 - min_offset - (max_offset - min_offset) * 0.35)
        raw_price = max(lower_bound, min(upper_bound, selected_raw))
    else:
        lower_bound = price * (1 + min_offset)
        upper_bound = price * (1 + max_offset)
        if selected:
            selected_raw = selected["price"]
            selected_source = selected["source"]
        else:
            above_band = [level for level in levels if level["price"] > upper_bound and str(level.get("category")) != "intraday"]
            if above_band:
                selected = min(above_band, key=lambda item: _level_weight(item))
                selected_raw = upper_bound
                selected_source = f"{selected['source']}偏远，按历史结构边界"
            else:
                selected_raw = price * (1 + min_offset + (max_offset - min_offset) * 0.35)
        raw_price = max(lower_bound, min(upper_bound, selected_raw))

    historical_nearby = []
    intraday_nearby = []
    for level in levels:
        if side == "buy" and level["price"] <= price:
            target = intraday_nearby if str(level.get("category")) == "intraday" else historical_nearby
            target.append({**level, "price": round(level["price"], 4)})
        elif side == "sell" and level["price"] >= price:
            target = intraday_nearby if str(level.get("category")) == "intraday" else historical_nearby
            target.append({**level, "price": round(level["price"], 4)})
        if len(historical_nearby) >= 5 and len(intraday_nearby) >= 3:
            break

    nearby = historical_nearby[:5] + intraday_nearby[:2]
    candidate_levels = _limit_candidates(side, levels, price, lower_bound, upper_bound)

    rounded = _round_price(raw_price)
    context = {
        "method": "patient_historical_structure_with_intraday_check",
        "reference_price": round(price, 4),
        "limit_price": rounded,
        "selected_source": selected_source,
        "selected_level": round(selected_raw, 4),
        "min_price": round(lower_bound, 4),
        "max_price": round(upper_bound, 4),
        "atr14": round(snapshot.atr14, 4) if snapshot.atr14 else None,
        "atr_pct": round(atr_pct, 5),
        "offset_pct_range": [round(min_offset, 4), round(max_offset, 4)],
        "offset_policy": offset_policy,
        "regime_label": (regime or {}).get("label"),
        "market_structure": market_structure,
        "nearby_levels": nearby,
        "candidate_levels": candidate_levels,
    }
    return rounded, context


def _limit_basis_text(side: str, context: dict) -> str:
    direction = "支撑" if side == "buy" else "压力"
    selected = context.get("selected_source") or direction
    levels = context.get("nearby_levels") or []
    nearby = "，".join(f"{item.get('source')} {item.get('price')}" for item in levels[:3])
    if nearby:
        return f"参考{selected}，历史结构优先、日内量价辅助；附近：{nearby}"
    return f"参考{selected}，按历史结构边界控制追价"


def _order_id(strategy: str, symbol: str, side: str) -> str:
    return f"{strategy}:{symbol.upper()}:{side}"


def _is_range_trade_overlay(overlay: dict) -> bool:
    return str(overlay.get("trade_plan") or "") == "range_trade"


def _is_flat_range_trade_overlay(overlay: dict) -> bool:
    return _is_range_trade_overlay(overlay) and str(overlay.get("target_net_exposure") or "") in {"flat_required", "flat_hard"}


def _range_trade_target(overlay: dict) -> str:
    target = str(overlay.get("target_net_exposure") or "flexible")
    if target == "flat":
        return "flat_preferred"
    if target in {"flat_required", "flat_preferred", "flexible"}:
        return target
    return "flexible"


def _range_trade_value(equity: float, current_value: float, risk: dict) -> float:
    min_value = float(risk.get("range_trade_min_value", risk.get("min_trade_value", 500.0)))
    equity_cap = equity * float(risk.get("range_trade_equity_pct", 0.035))
    position_cap = current_value * float(risk.get("range_trade_position_pct", 0.12)) if current_value > 0 else equity * 0.02
    hard_cap = float(risk.get("range_trade_max_value", max(min_value, equity * 0.05)))
    return max(min_value, min(hard_cap, equity_cap, position_cap))


def _build_order(
    symbol: str,
    side: str,
    shares: int,
    limit_price: float,
    value_to_trade: float,
    action: str,
    reason: str,
    limit_context: dict,
    strategy: str = "rebalance",
    trade_group_id: str | None = None,
    pair_role: str | None = None,
) -> dict:
    order = {
        "order_id": _order_id(strategy, symbol, side),
        "symbol": symbol,
        "side": side,
        "shares": shares,
        "limit_price": limit_price,
        "notional": round(shares * limit_price, 2),
        "target_trade_value": round(value_to_trade, 2),
        "time_in_force": "day",
        "action": action,
        "strategy": strategy,
        "reason": reason,
        "limit_basis": _limit_basis_text(side, limit_context),
        "limit_context": limit_context,
    }
    if trade_group_id:
        order["trade_group_id"] = trade_group_id
    if pair_role:
        order["pair_role"] = pair_role
    return order


def _position_value(portfolio: Portfolio, snapshots: dict[str, TechnicalSnapshot], symbol: str) -> float:
    position = portfolio.positions.get(symbol)
    if not position:
        return 0.0
    snapshot = snapshots.get(symbol)
    if not snapshot:
        return 0.0
    return position.shares * snapshot.price


def build_trade_plan(
    portfolio: Portfolio,
    quotes: dict[str, Quote],
    config: dict | None = None,
    research: dict | None = None,
    data_dir: str | Path = "data",
    history_loader: Callable[[str, str | Path], list[Bar]] = load_symbol,
    intraday_bars: dict[str, list[IntradayBar]] | None = None,
    intraday_bar_size: str = "5 mins",
) -> dict:
    config = config or DEFAULT_CONFIG
    research = research or {}

    symbols = set(symbol.upper() for symbol in config.get("symbols", []))
    symbols.update(portfolio.positions)
    symbols.update(symbol.upper() for symbol in config.get("base_target_weights", {}))
    proxies = set(symbol.upper() for symbol in config.get("market_proxies", []))
    all_symbols = sorted(symbols | proxies)

    snapshots: dict[str, TechnicalSnapshot] = {}
    daily_bars_by_symbol: dict[str, list[Bar]] = {}
    data_warnings: list[str] = []
    for symbol in all_symbols:
        try:
            bars = history_loader(symbol, data_dir)
        except FileNotFoundError:
            bars = []
            data_warnings.append(f"{symbol}: missing historical CSV")
        except ValueError as exc:
            bars = []
            data_warnings.append(f"{symbol}: {exc}")
        daily_bars_by_symbol[symbol] = bars
        try:
            snapshots[symbol] = build_snapshot(symbol, bars, quotes.get(symbol))
            if snapshots[symbol].source == "daily_close:stale_close_ignored":
                data_warnings.append(f"{symbol}: 收盘/昨收报价与最新日线不一致或无实时行情，现价改用最新完整日线收盘价。")
        except ValueError as exc:
            data_warnings.append(str(exc))

    intraday_summaries = {
        symbol.upper(): summary
        for symbol, bars in (intraday_bars or {}).items()
        if (summary := summarize_intraday_bars(symbol.upper(), bars, intraday_bar_size))
    }
    all_technical_analysis = {
        symbol: _price_volume_analysis(
            symbol,
            snapshot,
            daily_bars_by_symbol.get(symbol, []),
            intraday_summaries.get(symbol),
            (intraday_bars or {}).get(symbol, []),
        )
        for symbol, snapshot in snapshots.items()
    }

    regime = classify_market_regime(snapshots, config, research, intraday_summaries)
    technical_analysis = {symbol: all_technical_analysis[symbol] for symbol in sorted(symbols) if symbol in all_technical_analysis}
    market_technical_analysis = {symbol: all_technical_analysis[symbol] for symbol in sorted(proxies) if symbol in all_technical_analysis}
    market_structure = _market_structure_context(market_technical_analysis, regime)
    risk = config["risk"]
    market_session = _current_market_snapshot_label(datetime.now(timezone.utc).isoformat())
    session = market_session["session"]
    max_quote_age = _effective_quote_age_limit(session, risk)
    age_check_enabled = max_quote_age > 0
    market_session = {**market_session, "effective_max_quote_age_minutes": max_quote_age, "quote_age_block_enabled": age_check_enabled}
    equity = portfolio.account_equity
    base_weights = {symbol.upper(): float(value) for symbol, value in config.get("base_target_weights", {}).items()}
    target_gross, target_gross_reasons = _apply_target_gross_hint(float(regime["target_gross_exposure"]), portfolio, regime["label"], risk)
    if target_gross_reasons:
        regime["target_gross_exposure"] = round(target_gross, 4)
        regime["reasons"].extend(target_gross_reasons)
    core_symbols = {symbol.upper() for symbol in config.get("core_symbols", [])}

    raw_targets: dict[str, float] = {}
    target_reasons: dict[str, list[str]] = {}
    for symbol in sorted(symbols):
        snapshot = snapshots.get(symbol)
        if not snapshot:
            raw_targets[symbol] = 0.0
            target_reasons[symbol] = ["no_data"]
            continue
        thesis_status = _thesis_status(portfolio, research, symbol)
        conviction = _conviction(portfolio, research, symbol)
        multiplier, reasons = _symbol_multiplier(snapshot, thesis_status, conviction, _bias_from_research(research, symbol), intraday_summaries.get(symbol))
        price_volume_multiplier, price_volume_reasons = _price_volume_multiplier(technical_analysis.get(symbol))
        multiplier *= price_volume_multiplier
        reasons.extend(price_volume_reasons)
        position = portfolio.positions.get(symbol)
        bucket_multiplier, bucket_reasons = _bucket_multiplier(position.bucket if position else "auto", symbol in core_symbols)
        constraint_multiplier, constraint_reasons = _constraint_multiplier(position.trade_constraint if position else "flexible")
        multiplier *= bucket_multiplier * constraint_multiplier
        reasons.extend(bucket_reasons)
        reasons.extend(constraint_reasons)
        raw_targets[symbol] = base_weights.get(symbol, 0.05) * multiplier
        target_reasons[symbol] = reasons

    raw_sum = sum(raw_targets.values())
    normalized_targets = {
        symbol: (weight / raw_sum * target_gross if raw_sum > 0 else 0.0)
        for symbol, weight in raw_targets.items()
    }

    for symbol, weight in list(normalized_targets.items()):
        cap = float(risk["max_symbol_weight"] if symbol in core_symbols else risk["max_noncore_symbol_weight"])
        normalized_targets[symbol] = min(weight, cap)

    current_values = {symbol: _position_value(portfolio, snapshots, symbol) for symbol in symbols}
    current_gross_value = sum(value for value in current_values.values() if value > 0)
    max_gross_value = equity * float(risk["max_gross_exposure"])
    remaining_buy_value = max(0.0, max_gross_value - current_gross_value)
    margin_buy_budget, margin_reasons = _margin_buy_budget(portfolio, risk)
    if margin_buy_budget is not None:
        remaining_buy_value = min(remaining_buy_value, margin_buy_budget)
    min_trade_value = float(risk["min_trade_value"])
    band_value = equity * float(risk["rebalance_band_pct"])
    threshold_value = max(min_trade_value, band_value)

    positions = []
    orders = []
    for symbol in sorted(symbols):
        snapshot = snapshots.get(symbol)
        if not snapshot:
            continue
        current_value = current_values.get(symbol, 0.0)
        target_weight = normalized_targets.get(symbol, 0.0)
        target_value = target_weight * equity
        delta_value = target_value - current_value
        quote_age = snapshot.quote_age_minutes
        stale_close_fallback = str(snapshot.source).startswith("daily_close")
        quote_stale = stale_close_fallback or (age_check_enabled and quote_age is not None and quote_age > max_quote_age)
        position = portfolio.positions.get(symbol)
        shares_held = position.shares if position else 0
        bucket = position.bucket if position else "auto"
        trade_constraint = position.trade_constraint if position else "flexible"
        reasons = target_reasons.get(symbol, [])[:]
        overlay = _symbol_overlay(research, symbol)
        no_add = bool(overlay.get("no_add"))
        soft_no_add = bool(overlay.get("soft_no_add"))
        no_reduce = bool(overlay.get("no_reduce"))
        portfolio_soft_no_add = trade_constraint in {"soft_no_add", "reduce_only"}
        portfolio_soft_no_reduce = trade_constraint in {"soft_no_reduce", "prefer_hold"}
        research_bias = _bias_from_research(research, symbol)
        intraday_summary = intraday_summaries.get(symbol)
        range_trade_target = _range_trade_target(overlay) if _is_range_trade_overlay(overlay) else "flexible"

        action = "hold"
        side = None
        value_to_trade = 0.0
        if quote_stale:
            reasons.append("quote_stale:close_fallback" if stale_close_fallback else f"quote_stale:{quote_age:.1f}m>{max_quote_age:.0f}m@{session}")
        elif _is_range_trade_overlay(overlay) and current_gross_value <= max_gross_value:
            action = "range_trade"
            if range_trade_target == "flat_required":
                reasons.append("range_trade_flat_required_prompt")
            elif range_trade_target == "flat_preferred":
                reasons.append("range_trade_flat_preferred_prompt")
            else:
                reasons.append("range_trade_prompt")
        elif abs(delta_value) < threshold_value:
            reasons.append("inside_rebalance_band")
        elif delta_value > 0:
            value_to_trade = min(delta_value, remaining_buy_value)
            if no_add:
                reasons.append("prompt_no_add")
            elif value_to_trade >= threshold_value and snapshot.trend_action != "sell":
                if soft_no_add or portfolio_soft_no_add:
                    evidence = _soft_no_add_evidence(snapshot, regime, intraday_summary, research_bias)
                    if evidence < 3.0:
                        label = "prompt_soft_no_add" if soft_no_add else "constraint_soft_no_add"
                        reasons.append(f"{label}:{evidence:+.1f}")
                        value_to_trade = 0.0
                    else:
                        label = "prompt_soft_no_add_overridden" if soft_no_add else "constraint_soft_no_add_overridden"
                        reasons.append(f"{label}:{evidence:+.1f}")
                if value_to_trade > 0:
                    action = "add"
                    side = "buy"
                    remaining_buy_value -= value_to_trade
            else:
                reasons.append("buy_blocked")
        else:
            value_to_trade = abs(delta_value)
            if no_reduce:
                reasons.append("prompt_no_reduce")
            elif portfolio_soft_no_reduce:
                evidence = _soft_reduce_evidence(snapshot, regime, intraday_summary, research_bias, current_value / equity if equity else 0.0, target_weight)
                if evidence < 3.0:
                    reasons.append(f"constraint_soft_no_reduce:{evidence:+.1f}")
                    value_to_trade = 0.0
                elif shares_held > 0:
                    reasons.append(f"constraint_soft_no_reduce_overridden:{evidence:+.1f}")
                    action = "reduce"
                    side = "sell"
                else:
                    reasons.append("no_position_to_reduce")
            elif shares_held > 0:
                action = "reduce"
                side = "sell"
            else:
                reasons.append("no_position_to_reduce")

        order = None
        if side and value_to_trade > 0:
            limit_price, limit_context = _limit_price(
                side,
                snapshot,
                config,
                daily_bars=daily_bars_by_symbol.get(symbol, []),
                intraday_summary=intraday_summary,
                intraday_bars=(intraday_bars or {}).get(symbol, []),
                regime=regime,
                market_structure=market_structure,
            )
            shares = int(value_to_trade // limit_price)
            if side == "sell":
                shares = min(shares, shares_held)
            if shares > 0:
                order = _build_order(symbol, side, shares, limit_price, value_to_trade, action, ";".join(reasons), limit_context)
                orders.append(order)
            else:
                action = "hold"
                reasons.append("shares_round_to_zero")
        else:
            limit_context = None

        positions.append(
            {
                "symbol": symbol,
                "shares": shares_held,
                "bucket": bucket,
                "trade_constraint": trade_constraint,
                "price": round(snapshot.price, 4),
                "source": snapshot.source,
                "quote_age_minutes": None if quote_age is None else round(quote_age, 2),
                "current_value": round(current_value, 2),
                "current_weight": round(current_value / equity, 4) if equity else 0.0,
                "target_weight": round(target_weight, 4),
                "target_value": round(target_value, 2),
                "delta_value": round(delta_value, 2),
                "trend_action": snapshot.trend_action,
                "trend_score": snapshot.trend_score,
                "trend_score_range": _score_meta(snapshot.trend_score, 0, 8),
                "trend_stop": snapshot.trend_stop,
                "price_volume_score": technical_analysis.get(symbol, {}).get("score"),
                "price_volume_score_range": technical_analysis.get(symbol, {}).get("score_range"),
                "price_volume_label": technical_analysis.get(symbol, {}).get("label"),
                "intraday_score": intraday_summary.get("score") if intraday_summary else None,
                "intraday_score_range": intraday_summary.get("score_range") if intraday_summary else None,
                "intraday_label": intraday_summary.get("label") if intraday_summary else None,
                "intraday_from_open_pct": intraday_summary.get("from_open_pct") if intraday_summary else None,
                "intraday_last_30m_pct": intraday_summary.get("last_30m_pct") if intraday_summary else None,
                "action": action,
                "order": order,
                "limit_context": limit_context,
                "reason": ";".join(reasons),
            }
        )

    trade_groups = []
    for symbol in sorted(symbols):
        overlay = _symbol_overlay(research, symbol)
        if not _is_range_trade_overlay(overlay):
            continue
        snapshot = snapshots.get(symbol)
        if not snapshot:
            continue
        quote_age = snapshot.quote_age_minutes
        if str(snapshot.source).startswith("daily_close"):
            data_warnings.append(f"{symbol}: 做T计划跳过，无实时行情，仅有日线收盘兜底价")
            continue
        if age_check_enabled and quote_age is not None and quote_age > max_quote_age:
            data_warnings.append(f"{symbol}: 做T计划跳过，行情时间过旧 {quote_age:.1f}m（{session}限{max_quote_age:.0f}m）")
            continue
        position = portfolio.positions.get(symbol)
        shares_held = position.shares if position else 0
        trade_constraint = position.trade_constraint if position else "flexible"
        no_add = bool(overlay.get("no_add")) or trade_constraint == "reduce_only"
        no_reduce = bool(overlay.get("no_reduce"))
        range_target = _range_trade_target(overlay)
        hard_flat_intent = range_target == "flat_required"
        flat_preference = range_target in {"flat_required", "flat_preferred"}
        current_value = current_values.get(symbol, 0.0)
        trade_value = _range_trade_value(equity, current_value, risk)
        if margin_buy_budget is not None:
            trade_value = min(trade_value, max(0.0, remaining_buy_value))

        buy_order = None
        sell_order = None
        buy_price = sell_price = None
        buy_context = sell_context = None
        if trade_value >= min_trade_value:
            buy_price, buy_context = _limit_price(
                "buy",
                snapshot,
                config,
                daily_bars=daily_bars_by_symbol.get(symbol, []),
                intraday_summary=intraday_summaries.get(symbol),
                intraday_bars=(intraday_bars or {}).get(symbol, []),
                regime=regime,
                market_structure=market_structure,
            )
            sell_price, sell_context = _limit_price(
                "sell",
                snapshot,
                config,
                daily_bars=daily_bars_by_symbol.get(symbol, []),
                intraday_summary=intraday_summaries.get(symbol),
                intraday_bars=(intraday_bars or {}).get(symbol, []),
                regime=regime,
                market_structure=market_structure,
            )

        buy_shares = 0 if no_add or not buy_price else int(trade_value // buy_price)
        sell_shares = 0 if no_reduce or shares_held <= 0 or not sell_price else min(int(trade_value // sell_price), shares_held)
        if flat_preference:
            if buy_shares > 0 and sell_shares == 0 and not no_reduce and shares_held > 0 and sell_price:
                sell_shares = min(buy_shares, shares_held)
            if sell_shares > 0 and buy_shares == 0 and not no_add and buy_price:
                candidate_buy_notional = sell_shares * buy_price
                if margin_buy_budget is None or candidate_buy_notional <= remaining_buy_value:
                    buy_shares = sell_shares
        if hard_flat_intent:
            if buy_shares and sell_shares:
                paired_shares = min(buy_shares, sell_shares)
                buy_shares = paired_shares
                sell_shares = paired_shares

        group_id = f"range_trade:{symbol}"
        group_reason = ["range_trade_prompt"]
        if range_target == "flat_required":
            group_reason.append("range_trade_flat_required_prompt")
        elif range_target == "flat_preferred":
            group_reason.append("range_trade_flat_preferred_prompt")
        if no_add:
            group_reason.append("range_trade_buy_blocked")
        if no_reduce or shares_held <= 0:
            group_reason.append("range_trade_sell_blocked")

        if buy_shares > 0 and buy_price and buy_context:
            buy_order = _build_order(
                symbol,
                "buy",
                buy_shares,
                buy_price,
                buy_shares * buy_price,
                "range_trade",
                ";".join(group_reason + ["range_trade_low_buy"]),
                buy_context,
                strategy="range_trade",
                trade_group_id=group_id,
                pair_role="low_buy",
            )
            orders.append(buy_order)
            remaining_buy_value = max(0.0, remaining_buy_value - float(buy_order.get("notional") or 0.0))
        if sell_shares > 0 and sell_price and sell_context:
            sell_order = _build_order(
                symbol,
                "sell",
                sell_shares,
                sell_price,
                sell_shares * sell_price,
                "range_trade",
                ";".join(group_reason + ["range_trade_high_sell"]),
                sell_context,
                strategy="range_trade",
                trade_group_id=group_id,
                pair_role="high_sell",
            )
            orders.append(sell_order)

        if buy_order or sell_order:
            net_shares = int((buy_order or {}).get("shares") or 0) - int((sell_order or {}).get("shares") or 0)
            net_cash = float((sell_order or {}).get("notional") or 0.0) - float((buy_order or {}).get("notional") or 0.0)
            trade_groups.append(
                {
                    "group_id": group_id,
                    "strategy": "range_trade",
                    "symbol": symbol,
                    "intent": range_target,
                    "title": "做T / 高抛低吸",
                    "current_price": round(snapshot.price, 4),
                    "shares_held": shares_held,
                    "net_shares_if_all_filled": net_shares,
                    "net_cash_if_all_filled": round(net_cash, 2),
                    "estimated_spread_pct": round((float(sell_order["limit_price"]) / float(buy_order["limit_price"]) - 1.0), 4) if buy_order and sell_order else None,
                    "buy_order": buy_order,
                    "sell_order": sell_order,
                    "notes": group_reason,
                }
            )
        elif _is_range_trade_overlay(overlay):
            data_warnings.append(f"{symbol}: 做T计划未生成，金额不足或受硬约束限制。")

    planned_buy_notional = sum(float(order.get("notional", 0.0)) for order in orders if order.get("side") == "buy")
    planned_sell_notional = sum(float(order.get("notional", 0.0)) for order in orders if order.get("side") == "sell")
    min_cushion = float(risk.get("min_margin_cushion", 50000.0))
    haircut = max(0.01, float(risk.get("margin_buy_power_haircut", 0.5)))
    stress_drop = float(risk.get("stress_drop_pct", 0.05))
    margin_cushion, margin_cushion_source = _margin_cushion(portfolio)
    estimated_margin_cushion_after_buys = None
    if margin_cushion is not None:
        estimated_margin_cushion_after_buys = float(margin_cushion) - planned_buy_notional * haircut
        if estimated_margin_cushion_after_buys < min_cushion:
            data_warnings.append(f"维持保证金安全垫压力测试低于安全线: {estimated_margin_cushion_after_buys:.0f} < {min_cushion:.0f}")
    stress_gross_value = max(0.0, (current_gross_value - planned_sell_notional) * (1 - stress_drop) + planned_buy_notional)
    stress_gross_exposure = stress_gross_value / equity if equity else 0.0

    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "portfolio": {
            "account_equity": round(equity, 2),
            "cash": round(portfolio.cash, 2),
            "margin_debit": round(portfolio.margin_debit, 2),
            "maintenance_margin": None if portfolio.maintenance_margin is None else round(float(portfolio.maintenance_margin), 2),
            "excess_liquidity": None if portfolio.excess_liquidity is None else round(float(portfolio.excess_liquidity), 2),
            "margin_cushion": None if margin_cushion is None else round(float(margin_cushion), 2),
            "margin_cushion_source": margin_cushion_source,
            "target_gross_hint": portfolio.target_gross_hint,
            "current_gross_value": round(current_gross_value, 2),
            "current_gross_exposure": round(current_gross_value / equity, 4) if equity else 0.0,
            "max_gross_exposure": float(risk["max_gross_exposure"]),
            "planned_buy_notional": round(planned_buy_notional, 2),
            "planned_sell_notional": round(planned_sell_notional, 2),
            "margin_buy_budget": None if margin_buy_budget is None else round(margin_buy_budget, 2),
            "margin_reasons": margin_reasons,
            "min_margin_cushion": min_cushion,
            "estimated_margin_cushion_after_buys": None if estimated_margin_cushion_after_buys is None else round(estimated_margin_cushion_after_buys, 2),
            "stress_drop_pct": stress_drop,
            "stress_gross_exposure_after_orders": round(stress_gross_exposure, 4),
        },
        "regime": regime,
        "market_session": market_session,
        "orders": orders,
        "trade_groups": trade_groups,
        "positions": positions,
        "data_warnings": data_warnings,
        "research_overlay": research,
        "snapshots": {symbol: asdict(snapshot) for symbol, snapshot in sorted(snapshots.items())},
        "intraday": intraday_summaries,
        "technical_analysis": {symbol: technical_analysis[symbol] for symbol in sorted(technical_analysis)},
        "market_technical_analysis": {symbol: market_technical_analysis[symbol] for symbol in sorted(market_technical_analysis)},
        "market_structure": market_structure,
    }
