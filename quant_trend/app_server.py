import base64
import csv
import copy
import hmac
import json
import os
import threading
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import unquote, urlparse

from .agent import _score_meta, build_trade_plan, load_agent_config, load_json
from .llm_decision import apply_llm_limit_decisions
from .market_data import (
    AlpacaDataClient,
    IBKRDataClient,
    IntradayBar,
    MassiveDataClient,
    _is_vix_index_symbol,
    fetch_yahoo_chart_daily_rows,
    fetch_yahoo_chart_intraday_rows,
    fetch_yahoo_chart_quotes,
    fetch_yfinance_quotes,
    load_quotes,
    save_quotes,
)
from .portfolio import Portfolio, Position, portfolio_from_dict, portfolio_to_dict, save_portfolio
from .portfolio_input import portfolio_from_text
from .plan_summary import build_executive_summary
from .prompt_overlay import merge_research_overlays, overlay_from_prompt
from .user_store import SESSION_DAYS, UserStore, auth_mode_enabled

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


APP_STATE = "state/agent_app_state.json"
RUNS_DIR = "reports/agent_runs"
RUN_ID_LENGTH = 22
DEFAULT_SETTINGS = {
    "provider": "massive",
    "llm_model": "qwen3.7-max",
    "refresh_history": True,
    "schedule_enabled": False,
    "massive_rest_url": "http://44.219.45.87:8081",
    "massive_ws_url": "ws://44.219.45.87:8080/ws",
    "massive_timeout": 10.0,
    "ibkr_host": "127.0.0.1",
    "ibkr_port": 4002,
    "ibkr_client_id": 81,
    "ibkr_market_data_type": 1,
    "ibkr_timeout": 8.0,
    "fetch_intraday": True,
    "intraday_bar_size": "1 min",
    "intraday_duration": "1 D",
    "intraday_use_rth": False,
    "timezone": "Asia/Shanghai",
    "schedule_times": [
        {"label": "premarket", "time": "21:05"},
        {"label": "postmarket", "time": "05:10"},
    ],
}


def _bar_size_to_yahoo_interval(bar_size: str) -> str:
    normalized = str(bar_size or "").strip().lower()
    if normalized.startswith("1 "):
        return "1m"
    if normalized.startswith("15 "):
        return "15m"
    if normalized.startswith("30 "):
        return "30m"
    return "5m"


def _storage_root(root: Path) -> Path:
    raw = os.getenv("APP_DATA_DIR")
    return Path(raw).expanduser().resolve() if raw else root


def _looks_like_run_id(value: str) -> bool:
    return (
        len(value) == RUN_ID_LENGTH
        and value.endswith("Z")
        and value[8] == "T"
        and value[:8].isdigit()
        and value[9:21].isdigit()
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _collect_llm_usage(plan: dict) -> dict:
    calls = []
    for name, payload in [
        ("limit_decision", plan.get("llm_limit_decisions") or {}),
        ("executive_summary", plan.get("executive_summary") or {}),
    ]:
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not usage:
            continue
        calls.append(
            {
                "name": name,
                "source": payload.get("source"),
                "model": payload.get("model"),
                "input_tokens": int(usage.get("input_tokens") or 0),
                "cached_input_tokens": int(usage.get("cached_input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            }
        )
    totals = {
        "input_tokens": sum(item["input_tokens"] for item in calls),
        "cached_input_tokens": sum(item["cached_input_tokens"] for item in calls),
        "output_tokens": sum(item["output_tokens"] for item in calls),
        "reasoning_tokens": sum(item["reasoning_tokens"] for item in calls),
        "total_tokens": sum(item["total_tokens"] for item in calls),
    }
    return {"calls": calls, "totals": totals}


def _read_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _symbols_for_run(config: dict, portfolio: Portfolio) -> list[str]:
    symbols = set(symbol.upper() for symbol in config.get("symbols", []))
    symbols.update(symbol.upper() for symbol in config.get("base_target_weights", {}))
    symbols.update(symbol.upper() for symbol in config.get("market_proxies", []))
    symbols.update(portfolio.positions)
    return sorted(symbols)


def _row_value(row, column: str):
    value = row[column]
    if hasattr(value, "iloc"):
        return value.iloc[0]
    return value


def fetch_yfinance_daily_rows(symbol: str, start: str = "2024-01-01") -> list[dict]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("Missing yfinance. Run: python3 -m pip install -r requirements.txt") from exc

    frame = yf.download(symbol, start=start, auto_adjust=False, progress=False)
    rows = []
    for bar_date, row in frame.iterrows():
        rows.append(
            {
                "date": bar_date.strftime("%Y-%m-%d"),
                "open": float(_row_value(row, "Open")),
                "high": float(_row_value(row, "High")),
                "low": float(_row_value(row, "Low")),
                "close": float(_row_value(row, "Close")),
                "volume": float(_row_value(row, "Volume")),
            }
        )
    return rows


def write_daily_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(rows)


def _last_csv_date(path: Path) -> date | None:
    if not path.exists() or path.stat().st_size <= 0:
        return None
    last: date | None = None
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw = str(row.get("date") or "").strip()
            if not raw:
                continue
            try:
                current = date.fromisoformat(raw[:10])
            except ValueError:
                continue
            if last is None or current > last:
                last = current
    return last


def _previous_weekday(day: date) -> date:
    current = day - timedelta(days=1)
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def _latest_expected_us_daily_date(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    tz = ZoneInfo("America/New_York") if ZoneInfo else timezone.utc
    ny_now = now.astimezone(tz)
    ny_day = ny_now.date()
    if ny_day.weekday() >= 5:
        return _previous_weekday(ny_day + timedelta(days=1))
    if ny_now.time() >= dt_time(16, 15):
        return ny_day
    return _previous_weekday(ny_day)


def _is_ibkr_info_message(message: str) -> bool:
    return message.startswith(("2104:", "2106:", "2107:", "2158:", "2176:"))


class AgentApp:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.storage_root = _storage_root(self.root)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.legacy_state_path = self.storage_root / APP_STATE
        self.legacy_runs_dir = self.storage_root / RUNS_DIR
        self.legacy_portfolio_path = self.storage_root / "config/portfolio.json"
        self.state_path = self.legacy_state_path
        self.runs_dir = self.legacy_runs_dir
        self.config_path = self.root / "config/agent_config.json"
        self.portfolio_path = self.legacy_portfolio_path
        self.quotes_path = self.storage_root / "data/live_quotes.json"
        self.research_path = self.storage_root / "data/research_overlay.json"
        self.data_dir = self.storage_root / "data"
        self.web_dir = self.root / "web"
        self.user_store = UserStore(self.storage_root / "state/agent.db") if auth_mode_enabled() else None
        if self.user_store:
            seeded_users = self.user_store.seed_from_env()
            users = seeded_users or self.user_store.list_users()
            if users:
                self.user_store.migrate_legacy(users[0]["id"], self.legacy_state_path, self.legacy_portfolio_path, self.legacy_runs_dir)
            self.user_store.cleanup_sessions()
        self.lock = threading.Lock()
        self.run_lock = threading.Lock()
        self.progress_lock = threading.Lock()
        self.progress = {
            "active": False,
            "status": "idle",
            "run_id": None,
            "kind": None,
            "started_at": None,
            "updated_at": None,
            "steps": [],
            "scorecard": {},
            "error": None,
        }

    def load_state(self, user_id: int | None = None) -> dict:
        if self.user_store and user_id is not None:
            state = self.user_store.load_state(user_id) or {}
        else:
            state = _read_json(self.state_path, {})
        state.setdefault("settings", DEFAULT_SETTINGS.copy())
        state["settings"] = {**DEFAULT_SETTINGS, **state.get("settings", {})}
        state.setdefault("last_schedule_runs", {})
        if not self.user_store and "portfolio" not in state and self.portfolio_path.exists():
            try:
                state["portfolio"] = portfolio_to_dict(portfolio_from_dict(_read_json(self.portfolio_path, {})))
            except Exception:
                pass
        return state

    def save_state(self, state: dict, user_id: int | None = None) -> None:
        if self.user_store and user_id is not None:
            self.user_store.save_state(user_id, state)
            return
        _write_json(self.state_path, state)

    def list_runs(self, limit: int = 50, user_id: int | None = None) -> list[dict]:
        if self.user_store and user_id is not None:
            return self.user_store.list_runs(user_id, limit)
        if not self.runs_dir.exists():
            return []
        items = []
        for path in sorted(self.runs_dir.glob("*.json"), reverse=True):
            if not _looks_like_run_id(path.stem):
                continue
            try:
                run = _read_json(path, {})
            except json.JSONDecodeError:
                continue
            meta = run.get("run", {})
            items.append(
                {
                    "id": meta.get("id") or path.stem,
                    "asof": run.get("asof"),
                    "kind": meta.get("kind", "manual"),
                    "provider": meta.get("provider"),
                    "prompt": meta.get("prompt", ""),
                    "regime": run.get("regime", {}).get("label"),
                    "regime_score": run.get("regime", {}).get("score"),
                    "orders_count": len(run.get("orders", [])),
                    "gross": run.get("portfolio", {}).get("current_gross_exposure"),
                    "path": str(path),
                }
            )
            if len(items) >= limit:
                break
        return items

    def load_run(self, run_id: str, user_id: int | None = None) -> dict:
        safe_id = "".join(ch for ch in run_id if ch.isalnum() or ch in {"_", "-"})
        if self.user_store and user_id is not None:
            run = self.user_store.load_run(user_id, safe_id)
            if run is None:
                raise FileNotFoundError(run_id)
            return run
        path = self.runs_dir / f"{safe_id}.json"
        if not path.exists():
            raise FileNotFoundError(run_id)
        return _read_json(path, {})

    def latest_run(self, user_id: int | None = None) -> dict | None:
        runs = self.list_runs(1, user_id=user_id)
        return self.load_run(runs[0]["id"], user_id=user_id) if runs else None

    def compare_previous(self, portfolio: Portfolio, user_id: int | None = None) -> dict:
        previous = self.latest_run(user_id=user_id)
        if not previous:
            return {"previous_run_id": None, "position_changes": []}
        previous_portfolio = previous.get("input_portfolio", {}).get("positions", {})
        changes = []
        for symbol in sorted(set(previous_portfolio) | set(portfolio.positions)):
            old = int(previous_portfolio.get(symbol, {}).get("shares", 0))
            new = portfolio.positions.get(symbol).shares if symbol in portfolio.positions else 0
            if old != new:
                changes.append({"symbol": symbol, "previous_shares": old, "current_shares": new, "delta_shares": new - old})
        return {
            "previous_run_id": previous.get("run", {}).get("id"),
            "position_changes": changes,
            "previous_orders": previous.get("orders", []),
        }

    def refresh_history(self, symbols: list[str], expected_latest: date | None = None, provider: str | None = None, settings: dict | None = None) -> list[str]:
        warnings = []
        expected_latest = expected_latest or _latest_expected_us_daily_date()
        settings = settings or {}
        massive_client = None
        massive_daily_start = (datetime.now(timezone.utc).date() - timedelta(days=720)).isoformat()
        if provider == "massive":
            try:
                massive_client = MassiveDataClient(
                    base_url=str(settings.get("massive_rest_url") or "http://44.219.45.87:8081"),
                    timeout=float(settings.get("massive_timeout") or 10.0),
                )
            except Exception as exc:
                warnings.append(f"Massive 日线客户端初始化失败，改用 Yahoo Chart: {exc}")
        for symbol in symbols:
            path = self.data_dir / f"{symbol}.csv"
            local_last = _last_csv_date(path)
            if local_last is not None and local_last >= expected_latest:
                continue
            try:
                rows = massive_client.fetch_daily_bars(symbol, massive_daily_start) if massive_client else fetch_yahoo_chart_daily_rows(symbol)
                if not rows and massive_client and _is_vix_index_symbol(symbol):
                    rows = fetch_yahoo_chart_daily_rows(symbol)
                    warnings.append(f"{symbol}: Massive 指数日线为空，已用 Yahoo Chart 兜底。")
                if rows:
                    write_daily_rows(path, rows)
                    fetched_last = _last_csv_date(path)
                    if fetched_last and fetched_last < expected_latest:
                        warnings.append(f"{symbol}: 日线只到 {fetched_last.isoformat()}，预期最近交易日约 {expected_latest.isoformat()}")
                else:
                    source_name = "Massive/Polygon" if massive_client else "Yahoo Chart"
                    warnings.append(f"{symbol}: {source_name} daily history returned no rows")
            except Exception as exc:
                if massive_client and _is_vix_index_symbol(symbol):
                    try:
                        rows = fetch_yahoo_chart_daily_rows(symbol)
                        if rows:
                            write_daily_rows(path, rows)
                            warnings.append(f"{symbol}: Massive 指数日线失败，已用 Yahoo Chart 兜底: {exc}")
                            continue
                    except Exception as fallback_exc:
                        stale = f"，本地只到 {local_last.isoformat()}" if local_last else ""
                        warnings.append(f"{symbol}: VIX 指数日线 Massive/Yahoo 均失败{stale}: {exc}; fallback: {fallback_exc}")
                        continue
                stale = f"，本地只到 {local_last.isoformat()}" if local_last else ""
                warnings.append(f"{symbol}: history refresh failed{stale}: {exc}")
        return warnings

    def fetch_quotes(self, provider: str, symbols: list[str], settings: dict | None = None) -> dict:
        settings = settings or {}
        if provider == "file":
            return load_quotes(self.quotes_path)
        if provider == "massive":
            client = MassiveDataClient(
                base_url=str(settings.get("massive_rest_url") or "http://44.219.45.87:8081"),
                timeout=float(settings.get("massive_timeout") or 10.0),
            )
            quotes = client.fetch_latest_quotes(symbols)
            missing_vix = [symbol for symbol in symbols if _is_vix_index_symbol(symbol) and symbol.upper() not in quotes]
            if missing_vix:
                try:
                    quotes.update(fetch_yahoo_chart_quotes(missing_vix))
                except Exception as exc:
                    client.last_symbol_errors.setdefault("^VIX", []).append(f"Yahoo VIX fallback failed: {exc}")
            if not quotes:
                details = []
                details.extend(client.last_messages)
                for symbol, errors in sorted(client.last_symbol_errors.items()):
                    details.append(f"{symbol}: {'; '.join(errors)}")
                suffix = f" 详情：{' | '.join(details)}" if details else ""
                raise RuntimeError(f"Massive/Polygon 未返回任何行情。请检查 MASSIVE_API_KEY、套餐额度、REST 地址或网络。{suffix}")
        elif provider == "ibkr":
            client = IBKRDataClient(
                host=str(settings.get("ibkr_host") or "127.0.0.1"),
                port=int(settings.get("ibkr_port") or 4002),
                client_id=int(settings.get("ibkr_client_id") or 81),
                market_data_type=int(settings.get("ibkr_market_data_type") or 1),
                timeout=float(settings.get("ibkr_timeout") or 8.0),
            )
            quotes = client.fetch_latest_quotes(symbols)
            if not quotes:
                details = []
                details.extend(client.last_messages)
                for symbol, errors in sorted(client.last_symbol_errors.items()):
                    details.append(f"{symbol}: {'; '.join(errors)}")
                suffix = f" 详情：{' | '.join(details)}" if details else ""
                raise RuntimeError(f"IBKR 未返回任何行情。请检查 Gateway API 设置、端口、行情权限或改用延迟行情。{suffix}")
        elif provider == "alpaca":
            quotes = AlpacaDataClient().fetch_latest_quotes(symbols)
        elif provider == "yahoo_chart":
            quotes = fetch_yahoo_chart_quotes(symbols)
        else:
            quotes = fetch_yfinance_quotes(symbols)
        save_quotes(self.quotes_path, quotes)
        return quotes

    def fetch_intraday_bars(self, provider: str, symbols: list[str], settings: dict | None = None) -> tuple[dict[str, list[IntradayBar]], list[str]]:
        settings = settings or {}
        if not settings.get("fetch_intraday", True):
            return {}, ["当日分钟线已关闭"]
        bar_size = str(settings.get("intraday_bar_size") or "5 mins")
        duration = str(settings.get("intraday_duration") or "1 D")
        warnings: list[str] = []
        if provider == "ibkr":
            client = IBKRDataClient(
                host=str(settings.get("ibkr_host") or "127.0.0.1"),
                port=int(settings.get("ibkr_port") or 4002),
                client_id=int(settings.get("ibkr_client_id") or 81),
                market_data_type=int(settings.get("ibkr_market_data_type") or 1),
                timeout=float(settings.get("ibkr_timeout") or 8.0),
            )
            bars = client.fetch_intraday_bars(
                symbols,
                duration=duration,
                bar_size=bar_size,
                use_rth=bool(settings.get("intraday_use_rth", False)),
                timeout=max(float(settings.get("ibkr_timeout") or 8.0), 12.0),
            )
            for message in client.last_messages:
                if not _is_ibkr_info_message(message):
                    warnings.append(f"IBKR 分钟线提示: {message}")
            for symbol, errors in sorted(client.last_symbol_errors.items()):
                warnings.append(f"{symbol}: IBKR 分钟线错误: {'; '.join(errors)}")
            missing = sorted(set(symbols) - set(bars))
            if missing:
                warnings.append(f"当日分钟线缺失: {', '.join(missing)}")
            return bars, warnings

        if provider == "massive":
            client = MassiveDataClient(
                base_url=str(settings.get("massive_rest_url") or "http://44.219.45.87:8081"),
                timeout=float(settings.get("massive_timeout") or 10.0),
            )
            bars = client.fetch_intraday_bars(symbols, duration=duration, bar_size=bar_size)
            missing_vix = [symbol for symbol in symbols if _is_vix_index_symbol(symbol) and symbol.upper() not in bars]
            if missing_vix:
                interval = _bar_size_to_yahoo_interval(bar_size)
                for symbol in missing_vix:
                    try:
                        rows = fetch_yahoo_chart_intraday_rows(symbol, range_value="1d", interval=interval)
                        if rows:
                            bars[symbol.upper()] = rows
                            warnings.append(f"{symbol}: Massive 指数分钟线缺失，已用 Yahoo Chart {interval} 兜底。")
                    except Exception as exc:
                        warnings.append(f"{symbol}: Yahoo VIX 分钟线兜底失败: {exc}")
            for symbol, errors in sorted(client.last_symbol_errors.items()):
                if _is_vix_index_symbol(symbol) and symbol.upper() in bars:
                    continue
                warnings.append(f"{symbol}: Massive 分钟线错误: {'; '.join(errors)}")
            missing = sorted(set(symbols) - set(bars))
            if missing:
                warnings.append(f"Massive 当日分钟线缺失: {', '.join(missing)}")
            return bars, warnings

        if provider in {"yahoo_chart", "yfinance"}:
            interval = {"1 min": "1m", "5 mins": "5m", "15 mins": "15m", "30 mins": "30m"}.get(bar_size, "5m")
            bars = {}
            for symbol in symbols:
                try:
                    rows = fetch_yahoo_chart_intraday_rows(symbol, range_value="1d", interval=interval)
                    if rows:
                        bars[symbol.upper()] = rows
                except Exception as exc:
                    warnings.append(f"{symbol}: Yahoo 当日分钟线失败: {exc}")
            warnings.append("Yahoo 当日分钟线仅作兜底，不是实盘级行情源。")
            return bars, warnings

        return {}, [f"{provider} 暂未接入当日分钟线"]

    def save_portfolio_payload(self, payload: dict, user_id: int | None = None) -> dict:
        portfolio = portfolio_from_dict(payload)
        data = portfolio_to_dict(portfolio)
        if not self.user_store:
            save_portfolio(self.portfolio_path, portfolio)
        state = self.load_state(user_id=user_id)
        state["portfolio"] = data
        state["updated_at"] = _now_iso()
        self.save_state(state, user_id=user_id)
        return data

    def parse_portfolio_text(self, text: str, user_id: int | None = None) -> dict:
        portfolio = portfolio_from_text(text)
        return self.save_portfolio_payload(portfolio_to_dict(portfolio), user_id=user_id)

    def test_quotes(self, payload: dict, user_id: int | None = None) -> dict:
        state = self.load_state(user_id=user_id)
        settings = {**state.get("settings", {}), **payload.get("settings", {})}
        provider = payload.get("provider") or settings.get("provider", "massive")
        config = load_agent_config(self.config_path)
        symbols = sorted(set(config.get("symbols", [])) | set(config.get("market_proxies", [])) | set(config.get("base_target_weights", {})))
        quotes = self.fetch_quotes(provider, symbols, settings)
        return {
            "provider": provider,
            "symbols": symbols,
            "quotes": [
                {
                    "symbol": quote.symbol,
                    "price": quote.price,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "asof": quote.asof,
                    "source": quote.source,
                }
                for quote in quotes.values()
            ],
            "missing": sorted(set(symbols) - set(quotes)),
        }

    def _start_progress(self, run_id: str, kind: str, prompt: str, user_id: int | None = None) -> None:
        with self.progress_lock:
            self.progress = {
                "active": True,
                "status": "running",
                "run_id": run_id,
                "user_id": user_id,
                "kind": kind,
                "prompt": prompt,
                "started_at": _now_iso(),
                "updated_at": _now_iso(),
                "steps": [],
                "scorecard": {},
                "error": None,
            }

    def _progress_step(self, name: str, detail: str = "", score: dict | None = None, status: str = "done", sources: list[dict] | None = None) -> None:
        with self.progress_lock:
            self.progress.setdefault("steps", []).append(
                {
                    "time": _now_iso(),
                    "name": name,
                    "detail": detail,
                    "score": score or {},
                    "status": status,
                    "sources": sources or [],
                }
            )
            self.progress["updated_at"] = _now_iso()

    def _update_scorecard(self, scorecard: dict) -> None:
        with self.progress_lock:
            self.progress["scorecard"] = scorecard
            self.progress["updated_at"] = _now_iso()

    def _finish_progress(self, status: str, error: str | None = None) -> None:
        with self.progress_lock:
            self.progress["active"] = False
            self.progress["status"] = status
            self.progress["error"] = error
            self.progress["updated_at"] = _now_iso()

    def get_progress(self, user_id: int | None = None) -> dict:
        with self.progress_lock:
            progress = copy.deepcopy(self.progress)
        if self.user_store and user_id is not None and progress.get("user_id") not in (None, user_id):
            return {
                "active": False,
                "status": "idle",
                "run_id": None,
                "kind": None,
                "started_at": None,
                "updated_at": None,
                "steps": [],
                "scorecard": {},
                "error": None,
            }
        return progress

    def _build_scorecard(self, plan: dict, research: dict) -> dict:
        positions = plan.get("positions", [])
        trend_scores = [float(item.get("trend_score") or 0) for item in positions]
        intraday_scores = [float(item.get("intraday_score")) for item in positions if item.get("intraday_score") is not None]
        price_volume_items = list((plan.get("technical_analysis") or {}).values())
        price_volume_scores = [float(item.get("score") or 0) for item in price_volume_items]
        symbol_biases = []
        for raw in research.get("symbols", {}).values():
            if isinstance(raw, dict) and raw.get("bias") not in (None, ""):
                symbol_biases.append(float(raw["bias"]))
        prompt_score = float(research.get("macro_bias", 0.0)) + (sum(symbol_biases) / len(symbol_biases) if symbol_biases else 0.0)
        gross = float(plan.get("portfolio", {}).get("current_gross_exposure") or 0)
        max_gross = float(plan.get("portfolio", {}).get("max_gross_exposure") or 2.0)
        risk_buffer = max_gross - gross
        margin_cushion = plan.get("portfolio", {}).get("margin_cushion")
        min_margin_cushion = plan.get("portfolio", {}).get("min_margin_cushion")
        price_volume_score = round(sum(price_volume_scores) / len(price_volume_scores), 2) if price_volume_scores else 0
        market_score = round(float(plan.get("regime", {}).get("score") or 0), 2)
        technical_score = round(sum(trend_scores) / len(trend_scores), 2) if trend_scores else 0
        intraday_score = round(sum(intraday_scores) / len(intraday_scores), 2) if intraday_scores else 0
        prompt_score = round(prompt_score, 2)
        risk_score = round(risk_buffer, 2)
        return {
            "price_volume": {
                "label": "近期量价点位",
                "score": price_volume_score,
                "score_range": _score_meta(price_volume_score, -6, 6),
                "verdict": "主研究项",
                "detail": "多窗口支撑压力、Volume Profile/POC/VAH/VAL/HVN/LVN、筹码占比、量比和当日趋势。",
                "reference": ">=3 量价共振强；1 到 3 偏强；-1 到 1 震荡；<=-3 明显转弱。力度分为 0~5。",
            },
            "market": {
                "label": "市场/指数环境",
                "score": market_score,
                "score_range": plan.get("regime", {}).get("score_range") or _score_meta(market_score, -10, 10),
                "verdict": plan.get("regime", {}).get("label", "unknown"),
                "detail": "SPY/SMH/SOXX 日线与当日趋势，加 ^VIX 真实波动率指数的绝对值、历史分位、Z-score、5日变化和均值偏离合成。",
                "reference": ">=4 风险偏多；-2 到 4 中性；<=-2 风险收缩。",
            },
            "technical": {
                "label": "趋势/均线",
                "score": technical_score,
                "score_range": _score_meta(technical_score, 0, 8),
                "verdict": "辅助确认",
                "detail": "趋势信号、均线和ATR止损，作为量价点位之外的确认项。",
                "reference": ">=4 强趋势；1 到 4 可持有/观察；<=0 弱势。",
            },
            "intraday": {
                "label": "当日分钟线",
                "score": intraday_score,
                "score_range": _score_meta(intraday_score, -5, 5),
                "verdict": "越高越偏强",
                "detail": "开盘至今、VWAP、近 30 分钟、日内区间位置。",
                "reference": ">=3 强势上行；-1 到 1 震荡；<=-3 强势下行。",
            },
            "prompt": {
                "label": "本次想法/约束",
                "score": prompt_score,
                "score_range": _score_meta(prompt_score, -5, 5),
                "verdict": "负数偏谨慎，正数偏进攻",
                "detail": "自然语言 prompt 解析为软/硬约束和偏置。",
                "reference": "0 中性；<0 降低风险/加仓门槛；>0 提高进攻倾向。",
            },
            "risk": {
                "label": "杠杆/保证金",
                "score": risk_score,
                "score_range": _score_meta(risk_score, -max_gross, max_gross, "x"),
                "verdict": f"当前 {gross:.2f}x / 上限 {max_gross:.2f}x",
                "detail": "结合目标杠杆、维持保证金安全垫和压力测试限制买单。",
                "reference": f"杠杆余量 >0 正常；安全垫建议 >= {min_margin_cushion:.0f}" if min_margin_cushion is not None else "杠杆余量 >0 正常；填维持保证金后会显示安全垫阈值。",
            },
        }

    def _research_sources(self, provider: str, settings: dict, prompt: str, intraday_count: int) -> list[dict]:
        sources = [
            {"name": "BBAE 手动持仓", "type": "portfolio", "usage": "由你在网页表格/自然语言输入，作为唯一持仓来源。"},
            {"name": f"{provider} 实时/快照行情", "type": "market_data", "usage": "用于价格、买卖限价和当前市值计算。"},
            {"name": "Massive/Polygon 日线" if provider == "massive" else "Yahoo Chart 日线", "type": "historical_daily", "usage": "补齐本地日线 CSV，用于近5/10/20日支撑压力、20日量比、高量区、均线和趋势。"},
        ]
        if intraday_count:
            if provider == "ibkr":
                source_name = "IBKR 当日分钟线"
            elif provider == "massive":
                source_name = "Massive/Polygon 当日分钟线"
            else:
                source_name = "Yahoo 当日分钟线兜底"
            sources.append({"name": source_name, "type": "intraday", "usage": "用于开盘至今、VWAP、近 30 分钟趋势和日内区间评分。"})
        if prompt.strip():
            sources.append({"name": "本次自然语言 prompt", "type": "manual_prompt", "usage": "解析为软/硬约束、宏观谨慎偏置和个股偏置。"})
        if self.research_path.exists():
            sources.append({"name": "data/research_overlay.json", "type": "manual_research_overlay", "usage": "本地研究/事件弱覆盖层；数据源未自动化前，只作为辅助偏置，不作为主因。"})
        if provider == "ibkr":
            sources.append({"name": "IBKR 安全边界", "type": "guardrail", "usage": "只调用行情接口，不读账户、持仓、订单、成交，不下单。"})
        if provider == "massive":
            sources.append({"name": "Massive 套餐边界", "type": "guardrail", "usage": "REST 按单标的查询；不做全市场 snapshot、Flat Files 或批量全量请求；^VIX 会优先映射 I:VIX，指数权限不可用时只对 ^VIX 用 Yahoo Chart 兜底。"})
        if os.getenv("OPENAI_API_KEY"):
            sources.append({"name": "OpenAI 候选点位复核", "type": "llm_candidate_selector", "usage": "只在后端生成的候选支撑/压力价中选择，不允许 LLM 编造新价格。"})
        return sources

    def _decision_factors(self, plan: dict, research: dict, prompt: str, provider: str) -> list[dict]:
        portfolio = plan.get("portfolio", {})
        events = [event for event in research.get("events", []) if isinstance(event, dict)]
        if provider == "ibkr":
            provider_detail = "IBKR 仅用于行情；不读账户、持仓、订单、成交，也不会下单。"
        elif provider == "massive":
            provider_detail = "Massive/Polygon 仅用于行情；持仓、保证金和 prompt 只来自网页/本地输入。"
        else:
            provider_detail = "行情源仅用于价格/历史数据；持仓、保证金和 prompt 只来自网页/本地输入。"
        return [
            {
                "name": "近期量价技术面",
                "weight": "主",
                "status": "已纳入订单定价和目标权重微调",
                "detail": "支撑/压力、Volume Profile、筹码占比、VWAP、高量成交区、量比、多周期风险调整动量、区间波动率和日内趋势会进入 LLM 摘要与前端点位图。",
            },
            {
                "name": "仓位/杠杆/维持保证金",
                "weight": "硬约束",
                "status": f"当前杠杆 {float(portfolio.get('current_gross_exposure') or 0):.2f}x",
                "detail": "决定能否新增买单、买单预算和压力测试后的保证金安全垫。",
            },
            {
                "name": "你的 prompt 与持仓约束",
                "weight": "软/硬约束",
                "status": "本轮有输入" if prompt.strip() else "本轮未输入",
                "detail": "普通“不主动加/减”是软约束；明确禁止才硬拦，强证据可以反驳软约束。",
            },
            {
                "name": "宏观/研报/IPO/流动性",
                "weight": "弱覆盖",
                "status": f"手动事件 {len(events)} 条",
                "detail": "当前没有可靠自动数据源时只展示并传给 LLM 做辅助解释，不作为主决策锚。",
            },
            {
                "name": "行情来源边界",
                "weight": "数据质量",
                "status": provider,
                "detail": provider_detail,
            },
        ]

    def run_plan(self, payload: dict, kind: str = "manual", user_id: int | None = None) -> dict:
        if not self.run_lock.acquire(blocking=False):
            raise RuntimeError("已有一轮建议正在生成，请等待完成后再触发。")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        prompt = str(payload.get("prompt") or "")
        self._start_progress(run_id, kind, prompt, user_id=user_id)
        try:
            with self.lock:
                self._progress_step("读取持仓", "读取网页表格里的 BBAE 持仓；不会读取任何券商账户。", {"边界": "只用本地持仓"})
                state = self.load_state(user_id=user_id)
                portfolio_payload = payload.get("portfolio") or state.get("portfolio")
                if not portfolio_payload:
                    raise ValueError("Missing portfolio. Save positions first.")
                portfolio = portfolio_from_dict(portfolio_payload)
                config = load_agent_config(self.config_path)
                symbols = _symbols_for_run(config, portfolio)
                settings = {**state.get("settings", {}), **payload.get("settings", {})}
                provider = payload.get("provider") or settings.get("provider", "massive")
                llm_model = str(payload.get("model") or settings.get("llm_model") or "").strip() or None
                self._progress_step("确认股票池", f"本轮覆盖 {len(symbols)} 个标的：{', '.join(symbols)}。", {"标的数": len(symbols)})

                history_source = "Massive/Polygon" if provider == "massive" else "Yahoo Chart"
                self._progress_step("补齐历史日线", f"检查本地 CSV；缺少或过旧时用 {history_source} 刷新，用于均线/ATR/趋势。", {"状态": "开始"})
                history_warnings = self.refresh_history(symbols, provider=provider, settings=settings) if payload.get("refresh_history", settings.get("refresh_history", True)) else []
                if provider == "yahoo_chart":
                    history_warnings.append("yahoo_chart is a baseline/daily snapshot provider, not a real-time trading feed.")
                self._progress_step("历史日线完成", f"日线检查完成，提示 {len(history_warnings)} 条。", {"提示数": len(history_warnings)})

                self._progress_step("读取实时行情", f"从 {provider} 读取行情快照，用于当前价格和限价。", {"状态": "开始"})
                quotes = self.fetch_quotes(provider, symbols, settings)
                self._progress_step("实时行情完成", f"返回 {len(quotes)}/{len(symbols)} 条行情。", {"返回行情": len(quotes), "缺失": len(set(symbols) - set(quotes))})

                intraday_bars: dict[str, list[IntradayBar]] = {}
                intraday_warnings: list[str] = []
                if settings.get("fetch_intraday", True):
                    self._progress_step(
                        "读取当日分钟线",
                        f"读取 {settings.get('intraday_bar_size', '5 mins')}，覆盖开盘至当前/最近 1 个交易日。",
                        {"状态": "开始"},
                    )
                    intraday_bars, intraday_warnings = self.fetch_intraday_bars(provider, symbols, settings)
                    total_bars = sum(len(rows) for rows in intraday_bars.values())
                    self._progress_step(
                        "当日分钟线完成",
                        f"返回 {len(intraday_bars)} 个标的、{total_bars} 根分钟 bar。",
                        {"有分钟线标的": len(intraday_bars), "分钟bar": total_bars},
                    )
                else:
                    intraday_warnings.append("当日分钟线已关闭")

                self._progress_step("解析本次想法", "把自然语言 prompt 合并进研究覆盖层；普通限制是软约束，明确禁止才硬拦。", {"prompt长度": len(prompt)})
                base_research = load_json(self.research_path)
                prompt_research = overlay_from_prompt(prompt, symbols)
                research = merge_research_overlays(base_research, prompt_research)

                self._progress_step("计算建议", "以近期量价点位为主轴，综合市场环境、当日分钟线、prompt 约束和仓位风险。", {"状态": "开始"})
                plan = build_trade_plan(
                    portfolio=portfolio,
                    quotes=quotes,
                    config=config,
                    research=research,
                    data_dir=self.data_dir,
                    intraday_bars=intraday_bars,
                    intraday_bar_size=str(settings.get("intraday_bar_size") or "5 mins"),
                )
                scorecard = self._build_scorecard(plan, research)
                self._update_scorecard(scorecard)
                price_volume_count = len(plan.get("technical_analysis", {}))
                avg_price_volume = scorecard.get("price_volume", {}).get("score", 0)
                self._progress_step(
                    "量价结构完成",
                    f"生成 {price_volume_count} 个标的的支撑/压力、Volume Profile、多周期动量、区间波动率和点位解释。",
                    {"标的数": price_volume_count, "平均量价分": {"score": avg_price_volume, "score_range": _score_meta(avg_price_volume, -6, 6)}},
                )
                self._progress_step("评分汇总", "生成量价点位、市场、趋势、日内、prompt、杠杆风险评分。", scorecard)

                plan["run"] = {
                    "id": run_id,
                    "kind": kind,
                    "provider": provider,
                    "model": llm_model or os.getenv("OPENAI_MODEL", "qwen3.7-max"),
                    "prompt": prompt,
                    "created_at": _now_iso(),
                }
                plan["input_portfolio"] = portfolio_to_dict(portfolio)
                plan["history_context"] = self.compare_previous(portfolio, user_id=user_id)
                plan["data_warnings"] = history_warnings + intraday_warnings + plan.get("data_warnings", [])
                plan["decision_context"] = self._decision_factors(plan, research, prompt, provider)
                path = self.runs_dir / f"{run_id}.json"
                current_progress = self.get_progress(user_id=user_id)
                plan["research_process"] = {
                    "run_id": run_id,
                    "started_at": current_progress.get("started_at"),
                    "completed_at": None,
                    "steps": current_progress.get("steps", []),
                    "scorecard": scorecard,
                    "sources": self._research_sources(provider, settings, prompt, len(intraday_bars)),
                    "decision_factors": plan["decision_context"],
                }
                self._progress_step("LLM 点位复核", "把候选支撑/压力、筹码占比、多周期动量、区间波动率、指数趋势、VIX波动率风险和风控输入 LLM；主点位只允许从候选价中选择。", {"状态": "开始"})
                llm_limit_decisions = apply_llm_limit_decisions(plan, model=llm_model)
                if llm_limit_decisions:
                    plan["llm_limit_decisions"] = llm_limit_decisions
                    applied_count = len(llm_limit_decisions.get("applied", []))
                    self._progress_step(
                        "LLM 点位复核完成",
                        "LLM 已完成候选点位选择；若无可用选择则保留后端确定性点位。",
                        {"采用点位": {"score": applied_count, "score_range": _score_meta(applied_count, 0, max(1, len(plan.get("orders", []))))}},
                    )
                else:
                    self._progress_step("LLM 点位复核跳过", "未配置 LLM 或本轮没有候选挂单，保留后端确定性点位。", {"采用点位": 0})
                self._progress_step("生成重点总结", "把量价点位、当日趋势、仓位约束和弱覆盖因子交给 LLM，生成 500 字内中文摘要。", {"状态": "开始"})
                plan["executive_summary"] = build_executive_summary(plan, model=llm_model)
                plan["llm_usage"] = _collect_llm_usage(plan)
                self._progress_step("保存记录", f"准备保存到 {path.name}。", {"建议单数": len(plan.get("orders", []))})
                progress = self.get_progress(user_id=user_id)
                plan["research_process"] = {
                    "run_id": run_id,
                    "started_at": progress.get("started_at"),
                    "completed_at": _now_iso(),
                    "steps": progress.get("steps", []),
                    "scorecard": scorecard,
                    "sources": self._research_sources(provider, settings, prompt, len(intraday_bars)),
                    "decision_factors": plan["decision_context"],
                }
                if self.user_store and user_id is not None:
                    self.user_store.save_run(user_id, run_id, plan)
                else:
                    _write_json(path, plan)
                self.save_portfolio_payload(portfolio_to_dict(portfolio), user_id=user_id)
                state = self.load_state(user_id=user_id)
                state["latest_run_id"] = run_id
                state["settings"] = settings
                self.save_state(state, user_id=user_id)
                self._finish_progress("done")
                return plan
        except Exception as exc:
            self._progress_step("生成失败", str(exc), {"错误": 1}, status="failed")
            self._finish_progress("failed", str(exc))
            raise
        finally:
            self.run_lock.release()

    def scheduler_loop(self) -> None:
        while True:
            try:
                if self.user_store:
                    for user in self.user_store.list_users():
                        self._run_scheduled_for_user(user["id"])
                else:
                    self._run_scheduled_for_user(None)
                time.sleep(30)
            except Exception as exc:
                state = self.load_state()
                state["last_scheduler_error"] = {"time": _now_iso(), "error": str(exc)}
                self.save_state(state)
                time.sleep(60)

    def _run_scheduled_for_user(self, user_id: int | None) -> None:
        state = self.load_state(user_id=user_id)
        settings = state.get("settings", {})
        if not settings.get("schedule_enabled") or not state.get("portfolio"):
            return
        tz_name = settings.get("timezone", "Asia/Shanghai")
        tz = ZoneInfo(tz_name) if ZoneInfo else None
        now = datetime.now(tz) if tz else datetime.now()
        today = now.date().isoformat()
        for item in settings.get("schedule_times", []):
            label = item.get("label", "scheduled")
            schedule_time = item.get("time", "")
            key = f"{today}:{label}:{schedule_time}"
            if now.strftime("%H:%M") != schedule_time or key in state.get("last_schedule_runs", {}):
                continue
            plan = self.run_plan({"portfolio": state["portfolio"], "prompt": f"scheduled {label}", "settings": settings}, kind=label, user_id=user_id)
            state = self.load_state(user_id=user_id)
            state.setdefault("last_schedule_runs", {})[key] = plan["run"]["id"]
            self.save_state(state, user_id=user_id)


def make_handler(app: AgentApp):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A003
            return

        def _authorized(self) -> bool:
            if app.user_store:
                return True
            expected_password = os.getenv("APP_PASSWORD")
            if not expected_password:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header.removeprefix("Basic ").strip()).decode("utf-8")
            except Exception:
                return False
            username, separator, password = decoded.partition(":")
            if not separator:
                return False
            expected_username = os.getenv("APP_USERNAME", "agent")
            return hmac.compare_digest(username, expected_username) and hmac.compare_digest(password, expected_password)

        def _cookie(self, name: str) -> str | None:
            raw = self.headers.get("Cookie", "")
            if not raw:
                return None
            try:
                cookie = SimpleCookie(raw)
            except Exception:
                return None
            morsel = cookie.get(name)
            return morsel.value if morsel else None

        def _session_token(self) -> str | None:
            return self._cookie("agent_session")

        def _current_user(self) -> dict | None:
            if not app.user_store:
                return None
            return app.user_store.get_session_user(self._session_token())

        def _cookie_secure_enabled(self) -> bool:
            raw = os.getenv("APP_COOKIE_SECURE", "").strip().lower()
            if raw in {"1", "true", "yes", "on"}:
                return True
            if raw in {"0", "false", "no", "off"}:
                return False
            return self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https"

        def _session_cookie_header(self, token: str) -> str:
            parts = [
                f"agent_session={token}",
                "Path=/",
                f"Max-Age={SESSION_DAYS * 86400}",
                "HttpOnly",
                "SameSite=Lax",
            ]
            if self._cookie_secure_enabled():
                parts.append("Secure")
            return "; ".join(parts)

        def _clear_cookie_header(self) -> str:
            parts = ["agent_session=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax"]
            if self._cookie_secure_enabled():
                parts.append("Secure")
            return "; ".join(parts)

        def _auth_challenge(self):
            body = b"Authentication required"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Semis Position Agent"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200, headers: list[tuple[str, str]] | None = None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            for key, value in headers or []:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, body: bytes, content_type: str = "text/html; charset=utf-8", status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _public_file_response(self, path: str) -> bool:
            if path == "/":
                file = app.web_dir / "index.html"
            elif path.startswith("/static/"):
                file = app.web_dir / path.removeprefix("/static/")
            else:
                return False
            if not file.exists():
                self._json({"error": f"missing {file}"}, 404)
                return True
            content_type = "text/html; charset=utf-8"
            if file.suffix == ".js":
                content_type = "application/javascript; charset=utf-8"
            elif file.suffix == ".css":
                content_type = "text/css; charset=utf-8"
            self._text(file.read_bytes(), content_type)
            return True

        def _require_user(self) -> dict | None:
            user = self._current_user()
            if user:
                return user
            self._json({"error": "unauthorized"}, 401)
            return None

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/healthz":
                    self._json(
                        {
                            "ok": True,
                            "storage_root": str(app.storage_root),
                            "auth": "users" if app.user_store else ("basic" if os.getenv("APP_PASSWORD") else "none"),
                        }
                    )
                    return
                if app.user_store and path == "/api/session":
                    user = self._current_user()
                    self._json({"authenticated": bool(user), "user": user})
                    return
                if app.user_store and self._public_file_response(path):
                    return
                if not self._authorized():
                    self._auth_challenge()
                    return
                if path == "/api/session":
                    self._json({"authenticated": True, "user": None})
                    return
                user = self._require_user() if app.user_store and path.startswith("/api/") else None
                if app.user_store and path.startswith("/api/") and not user:
                    return
                if path == "/api/state":
                    user_id = user["id"] if user else None
                    state = app.load_state(user_id=user_id)
                    latest = app.latest_run(user_id=user_id)
                    self._json({"state": state, "runs": app.list_runs(user_id=user_id), "latest_run": latest})
                    return
                if path == "/api/progress":
                    user_id = user["id"] if user else None
                    self._json({"progress": app.get_progress(user_id=user_id)})
                    return
                if path.startswith("/api/runs/"):
                    user_id = user["id"] if user else None
                    run_id = unquote(path.rsplit("/", 1)[-1])
                    self._json(app.load_run(run_id, user_id=user_id))
                    return
                if self._public_file_response(path):
                    return
                self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                if app.user_store and path == "/api/login":
                    payload = self._read_json()
                    user = app.user_store.authenticate(str(payload.get("username") or ""), str(payload.get("password") or ""))
                    if not user:
                        self._json({"error": "账号或密码不正确"}, 401)
                        return
                    token = app.user_store.create_session(user["id"])
                    self._json({"authenticated": True, "user": user}, headers=[("Set-Cookie", self._session_cookie_header(token))])
                    return
                if app.user_store and path == "/api/logout":
                    app.user_store.delete_session(self._session_token())
                    self._json({"ok": True}, headers=[("Set-Cookie", self._clear_cookie_header())])
                    return
                if not self._authorized():
                    self._auth_challenge()
                    return
                user = self._require_user() if app.user_store and path.startswith("/api/") else None
                if app.user_store and path.startswith("/api/") and not user:
                    return
                user_id = user["id"] if user else None
                payload = self._read_json()
                if path == "/api/portfolio/save":
                    self._json({"portfolio": app.save_portfolio_payload(payload.get("portfolio", payload), user_id=user_id)})
                    return
                if path == "/api/portfolio/parse-text":
                    self._json({"portfolio": app.parse_portfolio_text(str(payload.get("text", "")), user_id=user_id)})
                    return
                if path == "/api/run":
                    self._json({"plan": app.run_plan(payload, kind=str(payload.get("kind", "manual")), user_id=user_id)})
                    return
                if path == "/api/quotes/test":
                    self._json(app.test_quotes(payload, user_id=user_id))
                    return
                if path == "/api/settings/save":
                    state = app.load_state(user_id=user_id)
                    state["settings"] = {**state.get("settings", {}), **payload.get("settings", {})}
                    app.save_state(state, user_id=user_id)
                    self._json({"settings": state["settings"]})
                    return
                self._json({"error": "not found"}, 404)
            except Exception as exc:
                status = 409 if "已有一轮建议正在生成" in str(exc) else 400
                self._json({"error": str(exc)}, status)

    return Handler


def run_server(root: str | Path, host: str = "127.0.0.1", port: int = 8765):
    app = AgentApp(root)
    threading.Thread(target=app.scheduler_loop, daemon=True).start()
    server = ThreadingHTTPServer((host, port), make_handler(app))
    return server
