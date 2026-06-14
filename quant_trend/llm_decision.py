import json
import os
import re
import ssl
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def _openai_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except Exception:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _compact_level(level: dict) -> dict:
    return {
        "candidate_id": level.get("candidate_id"),
        "price": level.get("price"),
        "candidate_price": level.get("candidate_price"),
        "source": level.get("source"),
        "category": level.get("category"),
        "tier": level.get("tier"),
        "distance_pct": level.get("distance_pct"),
        "chip_share_pct": level.get("chip_share_pct"),
        "volume_share_pct": level.get("volume_share_pct"),
        "profile_role": level.get("profile_role"),
        "profile_window": level.get("profile_window"),
        "profile_rank": level.get("profile_rank"),
        "confluence_count": level.get("confluence_count"),
        "touch_count": level.get("touch_count"),
        "recency_days": level.get("recency_days"),
        "level_strength_score": level.get("level_strength_score"),
        "within_offset_band": level.get("within_offset_band"),
    }


def _ladder_bounds(order: dict) -> tuple[float, float]:
    context = order.get("limit_context") or {}
    reference = float(context.get("reference_price") or order.get("limit_price") or 0.0)
    offsets = context.get("offset_pct_range") or []
    max_offset = max([float(value) for value in offsets if value is not None] or [0.08])
    max_span = max(0.12, min(0.35, max_offset * 2.2))
    if order.get("side") == "buy":
        return max(0.0001, reference * (1 - max_span)), reference * 0.997
    return reference * 1.003, reference * (1 + max_span)


def _compact_technical(item: dict | None) -> dict:
    if not item:
        return {}
    return {
        "price": item.get("price"),
        "score": item.get("score"),
        "score_range": item.get("score_range"),
        "label": item.get("label"),
        "range_position": item.get("range_position"),
        "volume_ratio20": item.get("volume_ratio20"),
        "supports": [_compact_level(level) for level in (item.get("supports") or [])[:5]],
        "resistances": [_compact_level(level) for level in (item.get("resistances") or [])[:5]],
        "components": item.get("components", []),
        "explanation": item.get("explanation"),
    }


def _compact_prompt_overlay(overlay: dict | None) -> dict:
    if not overlay:
        return {}
    symbols = {}
    for symbol, raw in (overlay.get("symbols") or {}).items():
        if not isinstance(raw, dict):
            continue
        symbols[str(symbol).upper()] = {
            "bias": raw.get("bias"),
            "no_add": raw.get("no_add"),
            "soft_no_add": raw.get("soft_no_add"),
            "no_reduce": raw.get("no_reduce"),
            "thesis_status": raw.get("thesis_status"),
            "trade_plan": raw.get("trade_plan"),
            "target_net_exposure": raw.get("target_net_exposure"),
            "prompt_flags": raw.get("prompt_flags", []),
        }
    return {
        "source": overlay.get("source"),
        "manual_prompt": overlay.get("manual_prompt"),
        "macro_bias": overlay.get("macro_bias"),
        "liquidity_bias": overlay.get("liquidity_bias"),
        "geopolitical_bias": overlay.get("geopolitical_bias"),
        "symbols": symbols,
        "events": (overlay.get("events") or [])[:8],
    }


def _compact_plan(plan: dict) -> dict:
    positions = {item.get("symbol"): item for item in plan.get("positions", [])}
    technical = plan.get("technical_analysis", {}) or {}
    market_technical = plan.get("market_technical_analysis", {}) or {}
    orders = []
    for order in plan.get("orders", []):
        context = order.get("limit_context") or {}
        candidates = [_compact_level(level) for level in (context.get("candidate_levels") or [])[:8]]
        symbol = order.get("symbol")
        orders.append(
            {
                "order_id": order.get("order_id"),
                "symbol": symbol,
                "side": order.get("side"),
                "strategy": order.get("strategy"),
                "trade_group_id": order.get("trade_group_id"),
                "pair_role": order.get("pair_role"),
                "current_limit_price": order.get("limit_price"),
                "target_trade_value": order.get("target_trade_value") or order.get("notional"),
                "current_shares": order.get("shares"),
                "reference_price": context.get("reference_price"),
                "ladder_price_bounds": _ladder_bounds(order),
                "offset_pct_range": context.get("offset_pct_range"),
                "selected_source": context.get("selected_source"),
                "limit_basis": order.get("limit_basis"),
                "position": positions.get(symbol, {}),
                "technical": _compact_technical(technical.get(symbol)),
                "candidate_levels": candidates,
            }
        )
    return {
        "asof": plan.get("asof"),
        "prompt": plan.get("run", {}).get("prompt") or "",
        "prompt_overlay": _compact_prompt_overlay(plan.get("research_overlay")),
        "decision_context": plan.get("decision_context", []),
        "portfolio": plan.get("portfolio", {}),
        "regime": plan.get("regime", {}),
        "market_structure": plan.get("market_structure", {}),
        "market_technical_analysis": {
            symbol: _compact_technical(market_technical.get(symbol))
            for symbol in ("SPY", "SMH", "SOXX", "VIXY")
            if symbol in market_technical
        },
        "orders": orders,
        "trade_groups": [
            {
                "group_id": group.get("group_id"),
                "symbol": group.get("symbol"),
                "intent": group.get("intent"),
                "current_price": group.get("current_price"),
                "shares_held": group.get("shares_held"),
                "net_shares_if_all_filled": group.get("net_shares_if_all_filled"),
                "buy_order_id": (group.get("buy_order") or {}).get("order_id"),
                "sell_order_id": (group.get("sell_order") or {}).get("order_id"),
            }
            for group in plan.get("trade_groups", [])
        ],
        "instructions": (
            "每张订单以 order_id 唯一识别；primary candidate_id 只能从 candidate_levels 中选择。"
            "如果 candidate_levels 为空，primary candidate_id 返回 null。"
            "必须读取 prompt 与 prompt_overlay；明确禁止/绝对类约束是硬约束，普通偏好是软约束，"
            "可在技术面、指数环境或风控证据很强时给出中文理由反驳。"
            "做T/range_trade 里 flat_preferred 只是净仓位偏好，不是硬配平；你可根据指标和prompt用 target_shares 调整买卖腿股数，"
            "使组合净加仓、净减仓或净持平。只有 flat_required 才应尽量保持两腿全成交净股数为0。"
            "reference_ladder 是给用户参考的2-3档分层价，可以由你根据量价/指数/筹码占比自主定价格和幅度，"
            "但必须在 ladder_price_bounds 内，买入价低于现价、卖出价高于现价。"
        ),
    }


def _extract_json(text: str) -> dict:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def _apply_gpt5_options(body: dict, kind: str, default_effort: str, default_verbosity: str) -> dict:
    model = str(body.get("model") or "")
    if not model.startswith("gpt-5"):
        return body
    effort = os.getenv(f"OPENAI_{kind}_REASONING_EFFORT") or os.getenv("OPENAI_REASONING_EFFORT") or default_effort
    verbosity = os.getenv(f"OPENAI_{kind}_VERBOSITY") or os.getenv("OPENAI_VERBOSITY") or default_verbosity
    body.pop("temperature", None)
    body["reasoning"] = {"effort": effort}
    text_options = body.get("text") if isinstance(body.get("text"), dict) else {}
    body["text"] = {**text_options, "verbosity": verbosity}
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


def _openai_json_request(request: Request, timeout: float, context: ssl.SSLContext) -> dict:
    retry_delays = [float(item) for item in os.getenv("OPENAI_RETRY_429_SECONDS", "20,60").split(",") if item.strip()]
    attempts = len(retry_delays) + 1
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=timeout, context=context) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:2000]
            if exc.code == 429 and attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            raise RuntimeError(f"OpenAI HTTP {exc.code}: {body or exc.reason}") from exc


def _call_openai_decisions(compact: dict) -> dict | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not compact.get("orders"):
        return None
    model = os.getenv("OPENAI_DECISION_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5.5")
    system = (
        "你是美股半导体仓位管理的点位复核助手。"
        "每张单的主执行 candidate_id 只能从 candidate_levels 里选择；若 candidate_levels 为空，candidate_id 返回 null。"
        "另外你必须为每张单输出 2到3 档 reference_ladder，作为用户参考价梯；价梯价格可由你自行决定，不必等于候选价，"
        "但必须在输入的 ladder_price_bounds 内，且买入价低于现价、卖出价高于现价。"
        "必须同时考虑个股量价、筹码/成交占比、支撑压力力度、SPY/SMH/SOXX/VIXY、杠杆和保证金。"
        "必须读取用户本轮 prompt、prompt_overlay 和 decision_context；明确禁止/绝对类约束不可违反，普通偏好可被强证据反驳但要说明。"
        "如果订单属于 range_trade/做T，同一标的的买腿和卖腿不必机械相等；"
        "flat_preferred 表示用户偏好净仓位接近不变，但你可以根据指标与prompt决定净加仓、净减仓或净持平；"
        "flat_required 才表示尽量保持两腿全成交净股数为0。"
        "买单目标是争取超额收益，优先更低且有结构承接的候选；卖单目标是更高减仓，但不能选择明显脱离可成交结构的孤立远点。"
        "你可以为每张订单输出 target_shares 调整主建议股数；买入股数受预算风控上限保护，卖出股数不能超过持仓。"
        "reference_ladder 每档需给 label、price、allocation_pct、rationale。allocation_pct 为该单目标交易金额的比例，2-3档合计不超过1。"
        "只返回 JSON，不要 Markdown。格式："
        "{\"decisions\":[{\"order_id\":\"rebalance:MU:buy\",\"symbol\":\"MU\",\"side\":\"buy\",\"candidate_id\":\"C1\",\"target_shares\":8,\"rationale\":\"中文理由，60字内\","
        "\"reference_ladder\":[{\"label\":\"第一档\",\"price\":92.5,\"allocation_pct\":0.4,\"rationale\":\"中文理由\"}]}]}"
    )
    decision_schema = {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "order_id": {"type": "string"},
                        "symbol": {"type": "string"},
                        "side": {"type": "string", "enum": ["buy", "sell"]},
                        "candidate_id": {"type": ["string", "null"]},
                        "target_shares": {"type": ["integer", "null"]},
                        "rationale": {"type": "string"},
                        "reference_ladder": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "price": {"type": "number"},
                                    "allocation_pct": {"type": "number"},
                                    "rationale": {"type": "string"},
                                },
                                "required": ["label", "price", "allocation_pct", "rationale"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["order_id", "symbol", "side", "candidate_id", "target_shares", "rationale", "reference_ladder"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["decisions"],
        "additionalProperties": False,
    }
    max_output_tokens = int(os.getenv("OPENAI_DECISION_MAX_OUTPUT_TOKENS", "12000"))
    body = _apply_gpt5_options({
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)},
        ],
        "temperature": 0.1,
        "max_output_tokens": max_output_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "limit_decisions",
                "schema": decision_schema,
                "strict": True,
            }
        },
    }, "DECISION", "medium", "low")
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    timeout = float(os.getenv("OPENAI_DECISION_TIMEOUT_SECONDS", "120"))
    payload = _openai_json_request(request, timeout, _openai_ssl_context())
    text = str(payload.get("output_text") or "")
    if not text:
        chunks = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(str(content["text"]))
        text = "\n".join(chunks)
    if not text:
        return {
            "_openai_model": model,
            "_openai_usage": _extract_openai_usage(payload),
            "_openai_error": "empty_output",
            "decisions": [],
        }
    usage = _extract_openai_usage(payload)
    if payload.get("status") == "incomplete":
        return {
            "_openai_model": model,
            "_openai_usage": usage,
            "_openai_error": f"incomplete:{(payload.get('incomplete_details') or {}).get('reason')}",
            "_openai_raw_preview": text[:1200],
            "decisions": [],
        }
    try:
        parsed = _extract_json(text)
    except Exception as exc:
        return {
            "_openai_model": model,
            "_openai_usage": usage,
            "_openai_error": f"json_parse_error:{exc}",
            "_openai_raw_preview": text[:1200],
            "decisions": [],
        }
    parsed["_openai_model"] = model
    parsed["_openai_usage"] = usage
    return parsed


def _side_price_valid(side: str, price: float, reference: float, bounds: tuple[float, float]) -> bool:
    low, high = bounds
    if price < low or price > high:
        return False
    if side == "buy":
        return price < reference
    return price > reference


def _sanitize_ladder(order: dict, raw_ladder: object, position_shares: int) -> list[dict]:
    if not isinstance(raw_ladder, list):
        raw_ladder = []
    side = str(order.get("side") or "")
    context = order.get("limit_context") or {}
    reference = float(context.get("reference_price") or order.get("limit_price") or 0.0)
    bounds = _ladder_bounds(order)
    target_value = float(order.get("target_trade_value") or order.get("notional") or 0.0)
    fallback_allocations = [0.4, 0.35, 0.25]
    used_value = 0.0
    remaining_shares = position_shares
    result = []

    for index, raw in enumerate(raw_ladder[:3]):
        if not isinstance(raw, dict):
            continue
        try:
            price = float(raw.get("price"))
        except (TypeError, ValueError):
            continue
        if not _side_price_valid(side, price, reference, bounds):
            continue
        try:
            allocation = float(raw.get("allocation_pct"))
        except (TypeError, ValueError):
            allocation = fallback_allocations[min(index, len(fallback_allocations) - 1)]
        allocation = max(0.05, min(0.7, allocation))
        if target_value and used_value + allocation * target_value > target_value:
            allocation = max(0.0, (target_value - used_value) / target_value)
        if allocation <= 0:
            continue
        notional = target_value * allocation
        shares = int(notional // price) if price > 0 else 0
        if side == "sell":
            shares = min(shares, remaining_shares)
            remaining_shares -= shares
        if shares <= 0:
            continue
        rounded_price = round(price, 4 if price < 1 else 2)
        used_value += allocation * target_value
        result.append(
            {
                "label": str(raw.get("label") or f"第{len(result) + 1}档"),
                "original_label": str(raw.get("label") or f"第{len(result) + 1}档"),
                "price": rounded_price,
                "distance_pct": round((rounded_price / reference) - 1.0, 5) if reference else None,
                "allocation_pct": round(allocation, 4),
                "shares": shares,
                "notional": round(shares * rounded_price, 2),
                "rationale": str(raw.get("rationale") or "")[:120],
                "source": "llm",
                "reference_only": True,
            }
        )
    result = sorted(result, key=lambda item: float(item.get("price") or 0.0), reverse=side == "buy")
    for index, item in enumerate(result, start=1):
        item["label"] = f"第{index}档"
    return result


def _decision_target_shares(order: dict, raw_shares: object, position_shares: int) -> int | None:
    if raw_shares is None:
        return None
    try:
        shares = int(float(raw_shares))
    except (TypeError, ValueError):
        return None
    if shares <= 0:
        return None
    price = float(order.get("limit_price") or 0.0)
    if price <= 0:
        return None
    if order.get("side") == "sell":
        return min(shares, max(0, int(position_shares)))
    target_value = float(order.get("target_trade_value") or order.get("notional") or 0.0)
    current_value = float(order.get("notional") or 0.0)
    cap_multiplier = max(1.0, float(os.getenv("OPENAI_DECISION_SHARE_CAP_MULTIPLIER", "2.0")))
    cap_value = max(target_value, current_value, price) * cap_multiplier
    max_shares = max(1, int(cap_value // price))
    return min(shares, max_shares)


def _refresh_trade_groups(plan: dict) -> None:
    orders = plan.get("orders") or []
    for group in plan.get("trade_groups") or []:
        group_id = group.get("group_id")
        buy_order = next((order for order in orders if order.get("trade_group_id") == group_id and order.get("side") == "buy"), None)
        sell_order = next((order for order in orders if order.get("trade_group_id") == group_id and order.get("side") == "sell"), None)
        group["buy_order"] = buy_order
        group["sell_order"] = sell_order
        buy_shares = int((buy_order or {}).get("shares") or 0)
        sell_shares = int((sell_order or {}).get("shares") or 0)
        buy_notional = float((buy_order or {}).get("notional") or 0.0)
        sell_notional = float((sell_order or {}).get("notional") or 0.0)
        group["net_shares_if_all_filled"] = buy_shares - sell_shares
        group["net_cash_if_all_filled"] = round(sell_notional - buy_notional, 2)
        if buy_order and sell_order and float(buy_order.get("limit_price") or 0.0) > 0:
            group["estimated_spread_pct"] = round(float(sell_order.get("limit_price") or 0.0) / float(buy_order.get("limit_price") or 1.0) - 1.0, 4)
        else:
            group["estimated_spread_pct"] = None


def _fallback_ladder(order: dict, position_shares: int) -> list[dict]:
    context = order.get("limit_context") or {}
    candidates = context.get("candidate_levels") or []
    side = str(order.get("side") or "")
    bounds = _ladder_bounds(order)
    reference = float(context.get("reference_price") or order.get("limit_price") or 0.0)
    if side == "buy":
        ordered = sorted(candidates, key=lambda item: float(item.get("candidate_price") or item.get("price") or 0.0), reverse=True)
    else:
        ordered = sorted(candidates, key=lambda item: float(item.get("candidate_price") or item.get("price") or 0.0))
    raw = []
    for candidate in ordered:
        if len(raw) >= 3:
            break
        price = candidate.get("candidate_price") or candidate.get("price")
        if not price:
            continue
        price = float(price)
        if not _side_price_valid(side, price, reference, bounds):
            continue
        raw.append(
            {
                "label": f"第{len(raw) + 1}档",
                "price": price,
                "allocation_pct": [0.4, 0.35, 0.25][min(len(raw), 2)],
                "rationale": f"参考{candidate.get('source', '候选结构位')}",
            }
        )
    return _sanitize_ladder(order, raw, position_shares)


def apply_llm_limit_decisions(plan: dict) -> dict | None:
    compact = _compact_plan(plan)
    if not compact.get("orders"):
        return None
    try:
        payload = _call_openai_decisions(compact)
    except Exception as exc:
        return {
            "asof": datetime.now(timezone.utc).isoformat(),
            "source": "llm_error",
            "error": str(exc),
            "applied": [],
        }
    if not payload:
        return None

    usage = payload.pop("_openai_usage", None) if isinstance(payload, dict) else None
    model = payload.pop("_openai_model", None) if isinstance(payload, dict) else None
    error = payload.pop("_openai_error", None) if isinstance(payload, dict) else None
    raw_preview = payload.pop("_openai_raw_preview", None) if isinstance(payload, dict) else None
    decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(decisions, list):
        return {
            "asof": datetime.now(timezone.utc).isoformat(),
            "source": "llm_invalid",
            "model": model,
            "usage": usage,
            "applied": [],
        }
    if error and not decisions:
        source = "llm_empty_output" if error == "empty_output" else "llm_error"
        return {
            "asof": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "model": model,
            "usage": usage,
            "error": error,
            "raw_preview": raw_preview,
            "applied": [],
        }

    positions = {item.get("symbol"): item for item in plan.get("positions", [])}
    orders_by_id = {item.get("order_id"): item for item in plan.get("orders", []) if item.get("order_id")}
    applied = []
    ladders = []
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        order_id = str(decision.get("order_id") or "")
        symbol = str(decision.get("symbol") or "").upper()
        side = str(decision.get("side") or "").lower()
        candidate_id = str(decision.get("candidate_id") or "")
        order = orders_by_id.get(order_id)
        if not order:
            order = next((item for item in plan.get("orders", []) if item.get("symbol") == symbol and (not side or item.get("side") == side)), None)
        if not order:
            continue
        order_id = str(order.get("order_id") or order_id)
        position_shares = int((positions.get(symbol) or {}).get("shares") or order.get("shares") or 0)
        context = order.get("limit_context") or {}
        candidates = {str(level.get("candidate_id")): level for level in context.get("candidate_levels", [])}
        candidate = candidates.get(candidate_id)
        old_price = float(order.get("limit_price") or 0.0)
        old_shares = int(order.get("shares") or 0)
        price_changed = False
        share_changed = False
        if candidate:
            new_price = candidate.get("candidate_price") or candidate.get("price")
            if new_price:
                new_price = float(new_price)
                reference = float(context.get("reference_price") or 0.0)
                invalid_side = (order.get("side") == "buy" and reference and new_price >= reference) or (order.get("side") == "sell" and reference and new_price <= reference)
                target_value = float(order.get("target_trade_value") or order.get("notional") or 0.0)
                shares = int(target_value // new_price) if new_price > 0 else 0
                if order.get("side") == "sell":
                    shares = min(shares, position_shares)
                if not invalid_side and shares > 0:
                    order["limit_price"] = round(new_price, 4 if new_price < 1 else 2)
                    order["shares"] = shares
                    order["notional"] = round(shares * order["limit_price"], 2)
                    order["limit_basis"] = f"LLM选择候选{candidate_id}：{candidate.get('source')}；{decision.get('rationale', '')}"
                    order["llm_limit_decision"] = {
                        "candidate_id": candidate_id,
                        "old_limit_price": old_price,
                        "new_limit_price": order["limit_price"],
                        "rationale": decision.get("rationale", ""),
                        "candidate": candidate,
                    }
                    context["llm_selected_candidate_id"] = candidate_id
                    context["llm_selected_rationale"] = decision.get("rationale", "")
                    price_changed = True
        target_shares = _decision_target_shares(order, decision.get("target_shares"), position_shares)
        if target_shares is not None and target_shares > 0 and target_shares != int(order.get("shares") or 0):
            order["shares"] = target_shares
            order["notional"] = round(target_shares * float(order.get("limit_price") or 0.0), 2)
            order["llm_share_decision"] = {
                "old_shares": old_shares,
                "new_shares": target_shares,
                "requested_shares": decision.get("target_shares"),
                "rationale": decision.get("rationale", ""),
            }
            share_changed = True
        if price_changed or share_changed:
            applied.append(
                {
                    "symbol": symbol,
                    "order_id": order_id,
                    "side": order.get("side"),
                    "candidate_id": candidate_id or None,
                    "old_limit_price": old_price,
                    "new_limit_price": order.get("limit_price"),
                    "old_shares": old_shares,
                    "new_shares": order.get("shares"),
                    "rationale": decision.get("rationale", ""),
                }
            )
        ladder = _sanitize_ladder(order, decision.get("reference_ladder"), position_shares)
        if not ladder:
            ladder = _fallback_ladder(order, position_shares)
        if ladder:
            order["llm_reference_ladder"] = ladder
            ladders.append({"symbol": symbol, "order_id": order.get("order_id"), "side": order.get("side"), "levels": ladder})

    for order in plan.get("orders", []):
        if order.get("llm_reference_ladder"):
            continue
        symbol = str(order.get("symbol") or "").upper()
        position_shares = int((positions.get(symbol) or {}).get("shares") or order.get("shares") or 0)
        ladder = _fallback_ladder(order, position_shares)
        if ladder:
            for item in ladder:
                item["source"] = "candidate_fallback"
            order["llm_reference_ladder"] = ladder
            ladders.append({"symbol": symbol, "order_id": order.get("order_id"), "side": order.get("side"), "levels": ladder})

    if applied:
        _refresh_trade_groups(plan)
        portfolio = plan.get("portfolio") or {}
        planned_buy = sum(float(order.get("notional") or 0.0) for order in plan.get("orders", []) if order.get("side") == "buy")
        planned_sell = sum(float(order.get("notional") or 0.0) for order in plan.get("orders", []) if order.get("side") == "sell")
        portfolio["planned_buy_notional"] = round(planned_buy, 2)
        portfolio["planned_sell_notional"] = round(planned_sell, 2)
        if portfolio.get("margin_cushion") is not None:
            haircut = 0.5
            after_buys = float(portfolio.get("margin_cushion") or 0.0) - planned_buy * haircut
            portfolio["estimated_margin_cushion_after_buys"] = round(after_buys, 2)

    return {
        "asof": datetime.now(timezone.utc).isoformat(),
        "source": "llm_candidate_selector",
        "model": model,
        "usage": usage,
        "applied": applied,
        "ladders": ladders,
    }
