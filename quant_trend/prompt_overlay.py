import json
import os
import re
from datetime import datetime, timezone
from urllib.request import Request

from .llm_decision import (
    _apply_gpt5_options,
    _chat_message_text,
    _extract_chat_usage,
    _extract_json,
    _extract_openai_usage,
    _openai_json_request,
    _openai_ssl_context,
    resolve_llm_target,
)


_BOOL_FIELDS = {"no_add", "soft_no_add", "no_reduce", "soft_no_reduce"}
_THESIS_STATUSES = {"intact", "watch", "broken"}
_TRADE_PLANS = {"range_trade"}
_NET_EXPOSURE_TARGETS = {"flat_required", "flat_preferred", "flexible"}
_EVENT_DIRECTIONS = {"risk_off", "risk_on", "negative", "positive", "bearish", "bullish", "neutral"}
_PROMPT_SYMBOL_EXCLUDES = {
    "AI",
    "API",
    "ATR",
    "BBAE",
    "CPI",
    "CSV",
    "ETF",
    "FOMC",
    "GDP",
    "IPO",
    "JSON",
    "LLM",
    "MACD",
    "MA",
    "NFP",
    "PCE",
    "POC",
    "PROMPT",
    "RSI",
    "USD",
    "VAL",
    "VAH",
    "VIX",
    "VWAP",
}


def _segments_for_symbol(prompt: str, symbol: str) -> list[str]:
    pattern = re.compile(rf"(.{{0,30}}(?<![A-Za-z]){re.escape(symbol)}(?![A-Za-z]).{{0,50}})", flags=re.IGNORECASE)
    return [match.group(1) for match in pattern.finditer(prompt)]


def extract_prompt_symbols(prompt: str) -> set[str]:
    symbols: set[str] = set()
    for match in re.finditer(r"\$([A-Za-z]{1,6})|(?<![A-Za-z])([A-Za-z]{2,6})(?![A-Za-z])", str(prompt or "")):
        raw = match.group(1) or match.group(2) or ""
        symbol = raw.upper().strip()
        if not symbol or symbol in _PROMPT_SYMBOL_EXCLUDES:
            continue
        symbols.add(symbol)
    return symbols


def _has_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _has_range_trade_intent(text: str) -> bool:
    return _has_any(
        text,
        [
            r"做\s*[tT]",
            r"\b[tT]\b",
            r"高抛低吸",
            r"低买高卖",
            r"低位.*买.*高位.*卖",
            r"高位.*卖.*低位.*买",
            r"低位买回.*高位卖",
        ],
    )


def _has_flat_trade_intent(text: str) -> bool:
    return _has_any(
        text,
        [
            r"总持仓不变",
            r"总仓位不变",
            r"净仓位不变",
            r"仓位不变",
            r"持仓不变",
            r"不改变.*仓位",
            r"保持.*仓位",
        ],
    )


def _has_required_flat_trade_intent(text: str) -> bool:
    flat_words = r"(?:总持仓不变|总仓位不变|净仓位不变|仓位不变|持仓不变|不改变.*仓位|保持.*仓位)"
    hard_words = r"(?:必须|严格|绝对|硬性|强制|一定)"
    return _has_any(
        text,
        [
            rf"{hard_words}.{{0,12}}{flat_words}",
            rf"{flat_words}.{{0,12}}{hard_words}",
        ],
    )


def overlay_from_prompt(prompt: str, symbols: list[str]) -> dict:
    normalized = prompt.strip()
    if not normalized:
        return {}

    overlay: dict = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "source": "manual_prompt",
        "manual_prompt": normalized,
        "symbols": {},
        "events": [],
    }

    cautious = _has_any(
        normalized,
        [
            r"CPI|FOMC|非农|议息|财报前",
            r"不主动加仓|先不加仓|降杠杆|降低杠杆|风险偏好低|保守",
        ],
    )
    if cautious:
        overlay["macro_bias"] = -1
        overlay["events"].append(
            {
                "name": "manual_caution",
                "direction": "risk_off",
                "severity": 1,
                "note": "Derived from manual prompt.",
            }
        )

    global_hard_no_add = _has_any(normalized, [r"全局.*(绝对|禁止|严禁).*加", r"整体.*(绝对|禁止|严禁).*加", r"全局.*hard no add"])
    global_soft_no_add = _has_any(normalized, [r"全局.*不加", r"整体.*不加", r"不主动加仓", r"先不加仓", r"只减不加", r"少加仓", r"控制加仓"])
    global_range_trade = _has_range_trade_intent(normalized)
    global_flat_trade = _has_flat_trade_intent(normalized)
    global_required_flat_trade = _has_required_flat_trade_intent(normalized)

    for symbol in [symbol.upper() for symbol in symbols]:
        text = " ".join(_segments_for_symbol(normalized, symbol))
        if not text and not global_hard_no_add and not global_soft_no_add:
            continue

        item: dict = {}
        notes: list[str] = []
        symbol_range_trade = global_range_trade and bool(text)
        if global_hard_no_add:
            item["no_add"] = True
            item["bias"] = min(float(item.get("bias", 0)), -1.5)
            notes.append("global_hard_no_add")
        elif global_soft_no_add:
            item["soft_no_add"] = True
            item["bias"] = min(float(item.get("bias", 0)), -0.75)
            notes.append("global_soft_no_add")

        hard_no_add_flag = _has_any(text, [r"绝对不加", r"禁止加", r"严禁.*加", r"必须不加", r"hard no add"])
        soft_no_add_flag = _has_any(text, [r"只减不加", r"不加仓", r"不买", r"不主动加", r"少加仓", r"控制加仓", r"no add", r"reduce only"])
        if hard_no_add_flag:
            item["no_add"] = True
            item.pop("soft_no_add", None)
            item["bias"] = min(float(item.get("bias", 0)), -1.5)
            notes.append("symbol_hard_no_add")
        elif soft_no_add_flag:
            item["soft_no_add"] = True
            item["bias"] = min(float(item.get("bias", 0)), -0.75)
            notes.append("symbol_soft_no_add")
        if _has_any(text, [r"不减", r"不卖", r"只加不减", r"no sell", r"no reduce"]):
            item["no_reduce"] = True
            notes.append("symbol_no_reduce")
        if _has_any(text, [r"观察", r"watch", r"存疑", r"弱化"]):
            item["thesis_status"] = "watch"
            notes.append("thesis_watch")
        if _has_any(text, [r"破坏", r"破了", r"broken", r"invalidated"]):
            item["thesis_status"] = "broken"
            item["bias"] = min(float(item.get("bias", 0)), -2)
            notes.append("thesis_broken")
        if not item.get("no_add") and not item.get("soft_no_add") and _has_any(
            text,
            [
                r"建仓",
                r"开仓",
                r"新建.*仓",
                r"买入",
                r"配置",
                r"加仓",
                r"买",
                r"提高",
                r"看好",
                r"add",
                r"buy",
            ],
        ):
            item["bias"] = max(float(item.get("bias", 0)), 1)
            notes.append("symbol_positive")
        if _has_any(text, [r"减仓", r"卖", r"降低", r"降仓", r"reduce", r"sell"]):
            item["bias"] = min(float(item.get("bias", 0)), -1)
            notes.append("symbol_negative")
        if symbol_range_trade or _has_range_trade_intent(text):
            item["trade_plan"] = "range_trade"
            symbol_required_flat = global_required_flat_trade or _has_required_flat_trade_intent(text)
            symbol_flat_preference = global_flat_trade or _has_flat_trade_intent(text)
            if symbol_required_flat:
                item["target_net_exposure"] = "flat_required"
            elif symbol_flat_preference:
                item["target_net_exposure"] = "flat_preferred"
            else:
                item["target_net_exposure"] = "flexible"
            if item["target_net_exposure"] == "flat_required":
                item["bias"] = 0
                notes.append("range_trade_flat_required")
            elif item["target_net_exposure"] == "flat_preferred":
                item["bias"] = 0
                notes.append("range_trade_flat_preferred")
            else:
                notes.append("range_trade")

        if notes:
            item["prompt_flags"] = notes
            overlay["symbols"][symbol] = item

    return overlay


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是", "对", "true "}
    return False


def _bounded_float(value, minimum: float, maximum: float) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(minimum, min(maximum, number))


def _clean_text(value, limit: int = 180) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:limit]


def _clean_flags(value) -> list[str]:
    if not isinstance(value, list):
        return []
    flags = []
    for item in value[:12]:
        text = _clean_text(item, 80)
        if text and text not in flags:
            flags.append(text)
    return flags


def _sanitize_llm_symbol(raw: dict) -> dict:
    item: dict = {}
    if not isinstance(raw, dict):
        return item

    for field in _BOOL_FIELDS:
        # False values are intentionally omitted so LLM output cannot erase a
        # deterministic rule parser hard constraint during merge.
        if _truthy(raw.get(field)):
            item[field] = True

    bias = _bounded_float(raw.get("bias"), -3.0, 3.0)
    if bias is not None:
        item["bias"] = bias

    thesis_status = str(raw.get("thesis_status") or "").strip().lower()
    if thesis_status in _THESIS_STATUSES:
        item["thesis_status"] = thesis_status

    trade_plan = str(raw.get("trade_plan") or "").strip().lower()
    if trade_plan in _TRADE_PLANS:
        item["trade_plan"] = trade_plan

    target_net_exposure = str(raw.get("target_net_exposure") or "").strip().lower()
    if target_net_exposure in _NET_EXPOSURE_TARGETS:
        item["target_net_exposure"] = target_net_exposure

    for field in ("buy_condition", "sell_condition", "risk_trigger", "explanation", "evidence"):
        text = _clean_text(raw.get(field))
        if text:
            item[field] = text

    flags = _clean_flags(raw.get("prompt_flags"))
    if flags:
        item["prompt_flags"] = flags
    return item


def _sanitize_llm_overlay(raw: dict, prompt: str, symbols: list[str]) -> dict:
    allowed_symbols = {symbol.upper() for symbol in symbols}
    overlay: dict = {
        "asof": datetime.now(timezone.utc).isoformat(),
        "source": "manual_prompt_llm",
        "manual_prompt": prompt.strip(),
        "symbols": {},
        "events": [],
    }
    if not isinstance(raw, dict):
        return overlay

    for key in ("macro_bias", "liquidity_bias", "geopolitical_bias"):
        value = _bounded_float(raw.get(key), -3.0, 3.0)
        if value is not None:
            overlay[key] = value

    symbols_payload = raw.get("symbols") or {}
    if isinstance(symbols_payload, list):
        symbols_payload = {
            str(item.get("symbol", "")).upper(): item
            for item in symbols_payload
            if isinstance(item, dict) and item.get("symbol")
        }
    if isinstance(symbols_payload, dict):
        for symbol, item in symbols_payload.items():
            normalized = str(symbol or "").upper().strip()
            if normalized not in allowed_symbols:
                continue
            cleaned = _sanitize_llm_symbol(item)
            if cleaned:
                overlay["symbols"][normalized] = cleaned

    for raw_event in (raw.get("events") or [])[:8]:
        if not isinstance(raw_event, dict):
            continue
        name = _clean_text(raw_event.get("name"), 80)
        if not name:
            continue
        direction = str(raw_event.get("direction") or "neutral").strip().lower()
        if direction not in _EVENT_DIRECTIONS:
            direction = "neutral"
        event = {
            "name": name,
            "direction": direction,
            "severity": _bounded_float(raw_event.get("severity"), 0.0, 3.0) or 0.0,
        }
        note = _clean_text(raw_event.get("note"))
        if note:
            event["note"] = note
        expires = _clean_text(raw_event.get("expires"), 32)
        if expires:
            event["expires"] = expires
        overlay["events"].append(event)

    return overlay


def _merge_prompt_parse(rule_overlay: dict, llm_overlay: dict, parser_meta: dict) -> dict:
    merged = merge_research_overlays(rule_overlay, llm_overlay)
    merged["manual_prompt"] = (llm_overlay or rule_overlay or {}).get("manual_prompt") or (rule_overlay or {}).get("manual_prompt")
    merged["source"] = (llm_overlay or rule_overlay or {}).get("source") or "manual_prompt"
    merged["prompt_parser"] = parser_meta

    rule_symbols = (rule_overlay or {}).get("symbols") or {}
    llm_symbols = (llm_overlay or {}).get("symbols") or {}
    for symbol in set(rule_symbols) | set(llm_symbols):
        target = merged.setdefault("symbols", {}).setdefault(symbol, {})
        rule_item = rule_symbols.get(symbol) if isinstance(rule_symbols.get(symbol), dict) else {}
        llm_item = llm_symbols.get(symbol) if isinstance(llm_symbols.get(symbol), dict) else {}

        for field in _BOOL_FIELDS:
            if rule_item.get(field) or llm_item.get(field):
                target[field] = True

        if rule_item.get("target_net_exposure") == "flat_required":
            target["target_net_exposure"] = "flat_required"

        flags = []
        for source in (rule_item, llm_item):
            for flag in source.get("prompt_flags", []) if isinstance(source, dict) else []:
                if flag not in flags:
                    flags.append(flag)
        if flags:
            target["prompt_flags"] = flags[:16]
    return merged


def _call_llm_prompt_overlay(prompt: str, symbols: list[str], model: str | None = None) -> dict | None:
    model = model or os.getenv("OPENAI_PROMPT_MODEL") or os.getenv("OPENAI_MODEL", "deepseek-chat")
    target = resolve_llm_target(model)
    api_key = os.getenv(target["api_key_env"])
    if not api_key:
        return None

    system = (
        "你是交易系统的自然语言约束解析器，只把用户原文解析成 JSON，不做行情判断。"
        "只允许使用输入 symbols 内的标的，忽略其他股票代码；不要编造宏观事实、新闻或价格。"
        "hard 字段 no_add/no_reduce 只能在原文有明确禁止、绝对、严禁、必须不、不要、不能等硬约束时为 true。"
        "不主动、尽量、偏向、可以、希望、只在深回撤等表达必须解析为 soft_no_add 或 soft_no_reduce、condition 或 bias，不要升级成 hard。"
        "做T、高抛低吸、低买高卖解析为 trade_plan=range_trade；总仓位不变解析为 flat_preferred，必须/严格总仓位不变才是 flat_required。"
        "bias 范围 -3 到 +3，正数偏多，负数偏空；无法判断就不要填。只返回 JSON。"
    )
    user_content = json.dumps({"prompt": prompt, "symbols": [symbol.upper() for symbol in symbols]}, ensure_ascii=False)
    schema = {
        "type": "object",
        "properties": {
            "macro_bias": {"type": ["number", "null"]},
            "liquidity_bias": {"type": ["number", "null"]},
            "geopolitical_bias": {"type": ["number", "null"]},
            "symbols": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "bias": {"type": ["number", "null"]},
                        "no_add": {"type": ["boolean", "null"]},
                        "soft_no_add": {"type": ["boolean", "null"]},
                        "no_reduce": {"type": ["boolean", "null"]},
                        "soft_no_reduce": {"type": ["boolean", "null"]},
                        "thesis_status": {"type": ["string", "null"]},
                        "trade_plan": {"type": ["string", "null"]},
                        "target_net_exposure": {"type": ["string", "null"]},
                        "buy_condition": {"type": ["string", "null"]},
                        "sell_condition": {"type": ["string", "null"]},
                        "risk_trigger": {"type": ["string", "null"]},
                        "explanation": {"type": ["string", "null"]},
                        "evidence": {"type": ["string", "null"]},
                        "prompt_flags": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
            },
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "direction": {"type": "string"},
                        "severity": {"type": ["number", "null"]},
                        "note": {"type": ["string", "null"]},
                        "expires": {"type": ["string", "null"]},
                    },
                    "required": ["name", "direction", "severity", "note", "expires"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["symbols", "events", "macro_bias", "liquidity_bias", "geopolitical_bias"],
        "additionalProperties": False,
    }
    max_output_tokens = int(os.getenv("OPENAI_PROMPT_MAX_OUTPUT_TOKENS", "3000"))
    if target["kind"] == "responses":
        body = _apply_gpt5_options(
            {
                "model": model,
                "input": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
                "temperature": 0.0,
                "max_output_tokens": max_output_tokens,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "prompt_overlay",
                        "schema": schema,
                    }
                },
            },
            "PROMPT",
            "low",
            "low",
        )
    else:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_content}],
            "temperature": 0.0,
            "max_tokens": max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        if target.get("provider") == "qwen":
            body["enable_thinking"] = os.getenv("QWEN_PROMPT_ENABLE_THINKING", "false").strip().lower() in {"1", "true", "yes"}
    request = Request(
        target["base_url"] + target["path"],
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    timeout = float(os.getenv("OPENAI_PROMPT_TIMEOUT_SECONDS", "45"))
    payload = _openai_json_request(request, timeout, _openai_ssl_context())
    if target["kind"] == "responses":
        text = str(payload.get("output_text") or "")
        if not text:
            chunks = []
            for item in payload.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") in {"output_text", "text"} and content.get("text"):
                        chunks.append(str(content["text"]))
            text = "\n".join(chunks)
        usage = _extract_openai_usage(payload)
    else:
        text = _chat_message_text(payload)
        usage = _extract_chat_usage(payload)
    if not text:
        return {"_llm_error": "empty_output", "_llm_model": model, "_llm_usage": usage}
    try:
        parsed = _extract_json(text)
    except Exception as exc:
        return {"_llm_error": f"json_parse_error:{exc}", "_llm_model": model, "_llm_usage": usage, "_llm_raw_preview": text[:1000]}
    parsed["_llm_model"] = model
    parsed["_llm_usage"] = usage
    return parsed


def overlay_from_prompt_with_llm(prompt: str, symbols: list[str], model: str | None = None) -> dict:
    rule_overlay = overlay_from_prompt(prompt, symbols)
    if not prompt.strip():
        return {}
    if os.getenv("OPENAI_PROMPT_PARSER_ENABLED", "true").strip().lower() in {"0", "false", "no"}:
        rule_overlay["prompt_parser"] = {"source": "rules", "enabled": False}
        return rule_overlay

    try:
        raw_llm = _call_llm_prompt_overlay(prompt, symbols, model=model)
    except Exception as exc:
        rule_overlay["prompt_parser"] = {"source": "rules_fallback", "error": str(exc)[:300]}
        return rule_overlay

    if not raw_llm:
        rule_overlay["prompt_parser"] = {"source": "rules", "reason": "llm_not_configured"}
        return rule_overlay
    if raw_llm.get("_llm_error"):
        rule_overlay["prompt_parser"] = {
            "source": "rules_fallback",
            "model": raw_llm.get("_llm_model"),
            "error": raw_llm.get("_llm_error"),
            "usage": raw_llm.get("_llm_usage"),
            "raw_preview": raw_llm.get("_llm_raw_preview"),
        }
        return rule_overlay

    usage = raw_llm.pop("_llm_usage", None)
    llm_model = raw_llm.pop("_llm_model", model)
    llm_overlay = _sanitize_llm_overlay(raw_llm, prompt, symbols)
    parser_meta = {
        "source": "llm_with_rules_guardrail",
        "model": llm_model,
        "usage": usage,
        "symbols_parsed": len(llm_overlay.get("symbols", {})),
        "events_parsed": len(llm_overlay.get("events", [])),
    }
    return _merge_prompt_parse(rule_overlay, llm_overlay, parser_meta)


def merge_research_overlays(*overlays: dict) -> dict:
    merged: dict = {"symbols": {}, "events": []}
    for overlay in overlays:
        if not overlay:
            continue
        for key, value in overlay.items():
            if key == "symbols":
                for symbol, raw in value.items():
                    target = merged["symbols"].setdefault(symbol.upper(), {})
                    if isinstance(raw, dict):
                        target.update(raw)
                    else:
                        target["bias"] = raw
            elif key == "events":
                merged["events"].extend(value or [])
            elif isinstance(value, (int, float)):
                merged[key] = float(merged.get(key, 0.0)) + float(value)
            else:
                merged[key] = value
    return merged
