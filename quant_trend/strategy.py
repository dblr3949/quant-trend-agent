from .indicators import average_true_range, rolling_high, simple_moving_average
from .models import Bar, Signal


def trend_signals(symbol: str, bars: list[Bar]) -> list[Signal]:
    if len(bars) < 160:
        return []

    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    sma20 = simple_moving_average(closes, 20)
    sma50 = simple_moving_average(closes, 50)
    sma150 = simple_moving_average(closes, 150)
    vol20 = simple_moving_average(volumes, 20)
    vol60 = simple_moving_average(volumes, 60)
    high60 = rolling_high(closes, 60)
    atr14 = average_true_range(bars, 14)

    signals: list[Signal] = []
    for i, bar in enumerate(bars):
        if any(value is None for value in (sma20[i], sma50[i], sma150[i], vol20[i], vol60[i], high60[i], atr14[i])):
            continue

        score = 0
        reasons: list[str] = []

        if bar.close > sma20[i] > sma50[i] > sma150[i]:  # type: ignore[operator]
            score += 3
            reasons.append("price_above_rising_ma_stack")

        if sma20[i] > sma20[i - 5] and sma50[i] > sma50[i - 10]:  # type: ignore[operator]
            score += 2
            reasons.append("ma_slope_up")

        distance_to_high = (high60[i] - bar.close) / high60[i]  # type: ignore[operator]
        if distance_to_high <= 0.08:
            score += 2
            reasons.append("near_60d_high")

        if vol20[i] > vol60[i]:  # type: ignore[operator]
            score += 1
            reasons.append("volume_expansion")

        if bar.close < sma50[i]:  # type: ignore[operator]
            action = "sell"
            reasons.append("close_below_sma50")
        elif score >= 6:
            action = "buy"
        elif score >= 4:
            action = "watch"
        else:
            action = "hold"

        stop = round(max(sma50[i], bar.close - 2.5 * atr14[i]), 4)  # type: ignore[arg-type]
        signals.append(
            Signal(
                symbol=symbol,
                date=bar.date,
                action=action,
                close=bar.close,
                score=score,
                stop=stop,
                reason=";".join(reasons) if reasons else "no_trend_edge",
            )
        )

    return signals


def latest_signal(symbol: str, bars: list[Bar]) -> Signal | None:
    signals = trend_signals(symbol, bars)
    return signals[-1] if signals else None
