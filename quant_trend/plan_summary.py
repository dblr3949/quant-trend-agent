import json
import os
import re
import ssl
from datetime import datetime, timezone
from urllib.request import Request, urlopen


def _openai_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except Exception:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _pct(value) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.1f}%"


def _fmt_score_number(value, signed: bool = False) -> str:
    number = float(value)
    if number.is_integer():
        text = str(int(number))
    else:
        text = f"{number:.2f}".rstrip("0").rstrip(".")
    if signed and number > 0:
        return f"+{text}"
    return text


def _score_percentile(value, minimum: float, maximum: float) -> float:
    if maximum <= minimum:
        maximum = minimum + 1.0
    position = (float(value) - minimum) / (maximum - minimum)
    return max(0.0, min(100.0, position * 100.0))


def _score_text(value, meta: dict | None = None, minimum: float = -6, maximum: float = 6) -> str:
    if value is None:
        return "-"
    meta = meta or {}
    min_value = float(meta.get("min", minimum))
    max_value = float(meta.get("max", maximum))
    unit = str(meta.get("unit") or "")
    percentile = meta.get("percentile")
    if percentile is None:
        percentile = _score_percentile(float(value), min_value, max_value)
    score = _fmt_score_number(value, signed=True)
    low = _fmt_score_number(min_value, signed=True)
    high = _fmt_score_number(max_value, signed=True)
    return f"{score}{unit}/{low}{unit}~{high}{unit}（尺位{float(percentile):.0f}%）"


def _action_text(position: dict) -> str:
    action = position.get("action")
    if action == "add":
        return "建议加仓"
    if action == "reduce":
        return "建议减仓"
    return "暂不动作"


def _reason_text(raw: str) -> str:
    parts = [part for part in str(raw or "").split(";") if part]
    mapping = {
        "trend_buy": "日线趋势强",
        "trend_watch": "趋势观察",
        "trend_sell": "趋势转弱",
        "trend_hold": "趋势一般",
        "below_sma50": "低于50日线",
        "below_trend_stop": "跌破趋势止损",
        "intraday_strong_up": "日内强势",
        "intraday_up": "日内偏强",
        "intraday_mixed": "日内震荡",
        "intraday_down": "日内偏弱",
        "intraday_strong_down": "日内明显走弱",
        "inside_rebalance_band": "仓位差额不大",
        "buy_blocked": "买入被预算/风控拦截",
        "bucket_core": "核心桶",
        "bucket_satellite": "卫星桶",
        "bucket_watch": "观察桶",
        "bucket_trim": "清理桶",
    }
    result = []
    for part in parts:
        if part.startswith("research_bias:"):
            result.append("研究/提示偏置")
        elif part.startswith("price_volume_score:"):
            value = part.split(":", 1)[1]
            try:
                result.append(f"量价分{_score_text(float(value), minimum=-6, maximum=6)}")
            except ValueError:
                result.append(f"量价分{value}")
        elif part.startswith("prompt_soft") or part.startswith("constraint_soft"):
            result.append("软约束参与判断")
        else:
            result.append(mapping.get(part, part))
    return "、".join(result[:4])


def _market_proxy_text(plan: dict) -> str:
    analyses = plan.get("market_technical_analysis", {}) or {}
    parts = []
    for symbol in ("SPY", "SMH", "SOXX", "VIXY"):
        item = analyses.get(symbol)
        if not item:
            continue
        parts.append(f"{symbol}{_score_text(item.get('score'), item.get('score_range'), -6, 6)}")
    return "，".join(parts)


def _compact_plan(plan: dict) -> dict:
    orders_by_symbol: dict[str, list[dict]] = {}
    for order in plan.get("orders", []):
        orders_by_symbol.setdefault(str(order.get("symbol") or ""), []).append(order)
    technical = plan.get("technical_analysis", {}) or {}
    market_technical = plan.get("market_technical_analysis", {}) or {}
    regime = plan.get("regime", {}) or {}
    market_structure = plan.get("market_structure", {}) or {}
    return {
        "asof": plan.get("asof"),
        "run": plan.get("run", {}),
        "portfolio": plan.get("portfolio", {}),
        "regime": {**regime, "score_text": _score_text(regime.get("score"), regime.get("score_range"), -10, 10)},
        "market_structure": {
            **market_structure,
            "score_text": _score_text(market_structure.get("score"), market_structure.get("score_range"), -6, 6),
        },
        "market_technical_analysis": {
            symbol: {
                **item,
                "score_text": _score_text(item.get("score"), item.get("score_range"), -6, 6),
            }
            for symbol, item in market_technical.items()
        },
        "scorecard": plan.get("research_process", {}).get("scorecard", {}),
        "llm_limit_decisions": plan.get("llm_limit_decisions", {}),
        "orders": plan.get("orders", []),
        "trade_groups": plan.get("trade_groups", []),
        "sources": plan.get("research_process", {}).get("sources", []),
        "decision_context": plan.get("decision_context") or plan.get("research_process", {}).get("decision_factors", []),
        "positions": [
            {
                "symbol": item.get("symbol"),
                "shares": item.get("shares"),
                "price": item.get("price"),
                "current_weight": item.get("current_weight"),
                "target_weight": item.get("target_weight"),
                "delta_value": item.get("delta_value"),
                "action": item.get("action"),
                "reason": item.get("reason"),
                "trend_score": item.get("trend_score"),
                "trend_score_range": item.get("trend_score_range"),
                "trend_score_text": _score_text(item.get("trend_score"), item.get("trend_score_range"), 0, 8),
                "price_volume_score": item.get("price_volume_score"),
                "price_volume_score_range": item.get("price_volume_score_range"),
                "price_volume_score_text": _score_text(item.get("price_volume_score"), item.get("price_volume_score_range"), -6, 6),
                "intraday_score": item.get("intraday_score"),
                "intraday_score_range": item.get("intraday_score_range"),
                "intraday_score_text": _score_text(item.get("intraday_score"), item.get("intraday_score_range"), -5, 5),
                "bucket": item.get("bucket"),
                "trade_constraint": item.get("trade_constraint"),
                "orders": orders_by_symbol.get(str(item.get("symbol") or ""), []),
                "price_volume": {
                    **(technical.get(item.get("symbol"), {}) or {}),
                    "score_text": _score_text(
                        (technical.get(item.get("symbol"), {}) or {}).get("score"),
                        (technical.get(item.get("symbol"), {}) or {}).get("score_range"),
                        -6,
                        6,
                    ),
                },
            }
            for item in plan.get("positions", [])
        ],
        "data_warnings": plan.get("data_warnings", []),
        "prompt": plan.get("run", {}).get("prompt") or "",
        "research_overlay": plan.get("research_overlay", {}),
        "score_format": "所有分数必须按“分数 / 下限~上限 · 尺位xx%”解释；禁止写成 8.9/20、3/8、1.4/6、-2/5 这类分母式格式。尺位为评分尺上的线性位置，不是历史样本分位。",
    }


def _fallback_summary(plan: dict) -> dict:
    regime = plan.get("regime", {})
    portfolio = plan.get("portfolio", {})
    orders_by_symbol: dict[str, list[dict]] = {}
    for order in plan.get("orders", []):
        orders_by_symbol.setdefault(str(order.get("symbol") or ""), []).append(order)
    positions = sorted(plan.get("positions", []), key=lambda item: item.get("symbol", ""))
    raw_lines = []
    for item in positions:
        symbol = item.get("symbol", "-")
        symbol_orders = orders_by_symbol.get(str(symbol), [])
        order_text = ""
        if symbol_orders:
            parts = []
            for order in symbol_orders:
                side = "买" if order.get("side") == "buy" else "卖"
                parts.append(f"{side}{order.get('shares')}股@{order.get('limit_price')}")
            order_text = "，" + "；".join(parts)
        technical = plan.get("technical_analysis", {}).get(symbol, {})
        tech_text = ""
        if technical:
            supports = technical.get("supports") or []
            resistances = technical.get("resistances") or []
            support_text = f"支撑{supports[0].get('price')}" if supports else ""
            resistance_text = f"压力{resistances[0].get('price')}" if resistances else ""
            tech_score = _score_text(technical.get("score"), technical.get("score_range"), -6, 6)
            tech_parts = [part for part in [f"量价{tech_score}", support_text, resistance_text] if part]
            tech_text = "；" + "、".join(tech_parts) if tech_parts else ""
        text = (
            f"{symbol}：现价{item.get('price', '-')}，{_action_text(item)}{order_text}；"
            f"权重{_pct(item.get('current_weight'))}->{_pct(item.get('target_weight'))}；"
            f"{_reason_text(item.get('reason')) or '仓位接近目标'}{tech_text}。"
        )
        raw_lines.append({"symbol": symbol, "text": text})

    market_text = _market_proxy_text(plan)
    prefix = (
        f"整体：市场{regime.get('label', '-')}/{_score_text(regime.get('score'), regime.get('score_range'), -10, 10)}，"
        f"当前杠杆{float(portfolio.get('current_gross_exposure') or 0):.2f}x"
        f"{'，指数：' + market_text if market_text else ''}。"
    )
    paragraphs = [{"symbol": item["symbol"], "text": item["text"]} for item in raw_lines]
    text = prefix + "\n" + "\n".join(item["text"] for item in paragraphs)
    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "source": "local_fallback",
        "text": text,
        "paragraphs": paragraphs,
    }


def _symbols_in_plan(compact: dict) -> list[str]:
    return [str(item.get("symbol", "")).upper() for item in compact.get("positions", []) if item.get("symbol")]


def _clip_paragraphs(text: str, limit: int | None = None) -> str:
    paragraphs = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not paragraphs:
        return str(text or "")
    if limit is None:
        return "\n".join(paragraphs)
    selected: list[str] = []
    total = 0
    for line in paragraphs:
        addition = len(line) + (1 if selected else 0)
        if total + addition > limit and selected:
            break
        selected.append(line)
        total += addition
        if total >= limit:
            break
    return "\n".join(selected)[:limit]


def _missing_symbols(text: str, symbols: list[str]) -> list[str]:
    upper = str(text or "").upper()
    return [symbol for symbol in symbols if symbol and symbol not in upper]


def _has_denominator_score_format(text: str) -> bool:
    return bool(re.search(r"(?<!\d)[+-]?\d+(?:\.\d+)?\s*/\s*(?:20|10|8|6|5|1\.5)(?!\d)", str(text or "")))


def _ensure_symbol_coverage(text: str, fallback: dict, compact: dict) -> tuple[str, str]:
    symbols = _symbols_in_plan(compact)
    clipped = _clip_paragraphs(text)
    missing = _missing_symbols(clipped, symbols)
    if not missing:
        return clipped, "llm"

    fallback_lines = {
        str(item.get("symbol", "")).upper(): str(item.get("text", ""))
        for item in fallback.get("paragraphs", [])
    }
    fills = [fallback_lines[symbol] for symbol in missing if fallback_lines.get(symbol)]
    merged = "\n".join([part for part in [clipped, "\n".join(fills)] if part]).strip()
    if fills and not _missing_symbols(merged, symbols):
        return _clip_paragraphs(merged), "llm_with_local_fill"
    return str(fallback.get("text") or ""), "local_fallback"


def _extract_openai_text(payload: dict) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"])
    chunks = []
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content["text"]))
    return "\n".join(chunks).strip()


def _apply_gpt5_options(body: dict, kind: str, default_effort: str, default_verbosity: str) -> dict:
    model = str(body.get("model") or "")
    if not model.startswith("gpt-5"):
        return body
    effort = os.getenv(f"OPENAI_{kind}_REASONING_EFFORT") or os.getenv("OPENAI_REASONING_EFFORT") or default_effort
    verbosity = os.getenv(f"OPENAI_{kind}_VERBOSITY") or os.getenv("OPENAI_VERBOSITY") or default_verbosity
    body.pop("temperature", None)
    body["reasoning"] = {"effort": effort}
    body["text"] = {"verbosity": verbosity}
    return body


def _extract_openai_usage(payload: dict) -> dict:
    usage = payload.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "cached_input_tokens": int(input_details.get("cached_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(output_details.get("reasoning_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _call_openai_summary(compact: dict) -> dict | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    model = os.getenv("OPENAI_SUMMARY_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5.5")
    timeout = float(os.getenv("OPENAI_SUMMARY_TIMEOUT_SECONDS", "90"))
    system = (
        "你是美股半导体仓位管理助手。只做中文摘要，不给投资保证。"
        "必须基于输入数据，每只股票一段话；必须覆盖输入positions里的全部symbol。"
        "开头必须先写一段整体市场框架，明确覆盖 SPY、SMH、SOXX、VIXY；缺数据就写缺数据。"
        "每段可以较完整，但要分段清楚；不要因为篇幅省略任何持仓股票。"
        "优先解释近期量价技术面：支撑、压力、Volume Profile、POC/VAH/VAL/HVN/LVN、筹码占比、VWAP、高量区、20日量比、日内趋势。"
        "如存在 llm_limit_decisions 或 order.llm_limit_decision，要说明模型选择的候选点位依据。"
        "如存在 order.llm_reference_ladder，只需简短提及有2-3档参考价梯；强调它是参考分层，不是自动下单。"
        "凡写到量价分、日内分、趋势分、市场分或任何评分，必须优先复制输入里的 *_score_text；"
        "必须使用“分数 / 下限~上限 · 尺位xx%”格式；"
        "禁止写成 8.9/20、3/8、1.4/6、-2/5、0.7/1.5 这类分母式格式。"
        "例如“量价 +2 / -6~+6 · 尺位67%”。"
        "每段说清：现价、动作、关键点位、如有挂单则给方向/股数/价格。"
        "仓位/杠杆/保证金和用户约束要纳入判断。宏观、研报、IPO、流动性等若只是手动覆盖或数据不足，只能作为辅助风险提示，不要写成确定事实。"
    )
    body = _apply_gpt5_options({
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_output_tokens": 2200,
    }, "SUMMARY", "low", "medium")
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    context = _openai_ssl_context()
    with urlopen(request, timeout=timeout, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    text = _extract_openai_text(payload)
    if not text:
        return None
    return {
        "model": model,
        "text": text.strip(),
        "usage": _extract_openai_usage(payload),
    }


def build_executive_summary(plan: dict) -> dict:
    fallback = _fallback_summary(plan)
    compact = _compact_plan(plan)
    try:
        result = _call_openai_summary(compact)
    except Exception as exc:
        fallback["error"] = str(exc)
        return fallback
    usage = None
    model = None
    text = result
    if isinstance(result, dict):
        usage = result.get("usage")
        model = result.get("model")
        text = result.get("text")
    if not text:
        return fallback
    if _has_denominator_score_format(text):
        fallback["error"] = "LLM summary used denominator-style score formatting; local formatted fallback was used."
        fallback["model"] = model
        fallback["usage"] = usage
        return fallback
    text, source = _ensure_symbol_coverage(text, fallback, compact)
    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "model": model,
        "usage": usage,
        "text": text,
        "paragraphs": [{"symbol": item.get("symbol"), "text": ""} for item in compact.get("positions", [])],
    }
