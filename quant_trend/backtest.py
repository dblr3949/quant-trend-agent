from dataclasses import dataclass

from .models import Bar, Trade
from .strategy import trend_signals


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    start_cash: float
    end_value: float
    total_return: float
    max_drawdown: float
    trades: list[Trade]


def _position_size(cash: float, entry: float, stop: float | None, risk_pct: float, max_position_pct: float) -> int:
    if entry <= 0:
        return 0

    max_position_value = cash * max_position_pct
    by_cash = int(max_position_value // entry)

    if stop is None or stop >= entry:
        return by_cash

    risk_cash = cash * risk_pct
    by_risk = int(risk_cash // (entry - stop))
    return max(0, min(by_cash, by_risk))


def run_backtest(
    symbol: str,
    bars: list[Bar],
    start_cash: float = 100000.0,
    risk_pct: float = 0.01,
    max_position_pct: float = 0.25,
) -> BacktestResult:
    signal_by_date = {signal.date: signal for signal in trend_signals(symbol, bars)}
    cash = start_cash
    shares = 0
    entry_stop: float | None = None
    trades: list[Trade] = []
    equity_curve: list[float] = []
    peak = start_cash
    max_drawdown = 0.0

    for bar in bars:
        signal = signal_by_date.get(bar.date)
        value = cash + shares * bar.close
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, (value - peak) / peak)
        equity_curve.append(value)

        if signal is None:
            continue

        should_exit = shares > 0 and (signal.action == "sell" or (entry_stop is not None and bar.close <= entry_stop))
        if should_exit:
            cash += shares * bar.close
            trades.append(Trade(bar.date, symbol, "sell", bar.close, shares, cash, signal.reason))
            shares = 0
            entry_stop = None
            continue

        if shares == 0 and signal.action == "buy":
            size = _position_size(cash, bar.close, signal.stop, risk_pct, max_position_pct)
            if size > 0:
                shares = size
                cash -= shares * bar.close
                entry_stop = signal.stop
                trades.append(Trade(bar.date, symbol, "buy", bar.close, shares, cash, signal.reason))

    end_value = cash + (shares * bars[-1].close if bars else 0.0)
    return BacktestResult(
        symbol=symbol,
        start_cash=start_cash,
        end_value=end_value,
        total_return=(end_value / start_cash - 1.0) if start_cash else 0.0,
        max_drawdown=max_drawdown,
        trades=trades,
    )
