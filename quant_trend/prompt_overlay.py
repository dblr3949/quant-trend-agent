import re
from datetime import datetime, timezone


def _segments_for_symbol(prompt: str, symbol: str) -> list[str]:
    pattern = re.compile(rf"(.{{0,30}}\b{re.escape(symbol)}\b.{{0,50}})", flags=re.IGNORECASE)
    return [match.group(1) for match in pattern.finditer(prompt)]


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
        if not item.get("no_add") and not item.get("soft_no_add") and _has_any(text, [r"加仓", r"买", r"提高", r"看好", r"add", r"buy"]):
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
