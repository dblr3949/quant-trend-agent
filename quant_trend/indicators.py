from .models import Bar


def simple_moving_average(values: list[float], window: int) -> list[float | None]:
    if window <= 0:
        raise ValueError("window must be positive")

    result: list[float | None] = []
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= window:
            running -= values[i - window]
        result.append(running / window if i + 1 >= window else None)
    return result


def rolling_high(values: list[float], window: int) -> list[float | None]:
    result: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < window:
            result.append(None)
            continue
        result.append(max(values[i + 1 - window : i + 1]))
    return result


def average_true_range(bars: list[Bar], window: int = 14) -> list[float | None]:
    true_ranges: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            true_ranges.append(bar.high - bar.low)
            continue
        prev_close = bars[i - 1].close
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close)))
    return simple_moving_average(true_ranges, window)
