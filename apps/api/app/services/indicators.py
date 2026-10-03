from __future__ import annotations

import math
from collections.abc import Iterable

from app.models import PriceBar


def _sma(values: list[float], window: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        if index >= window - 1:
            result[index] = running / window
    return result


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _rsi(values: list[float], window: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= window:
        return result
    gains = [max(values[index] - values[index - 1], 0) for index in range(1, len(values))]
    losses = [max(values[index - 1] - values[index], 0) for index in range(1, len(values))]
    average_gain = sum(gains[:window]) / window
    average_loss = sum(losses[:window]) / window

    def current_value() -> float:
        if average_loss == 0:
            return 100.0 if average_gain > 0 else 50.0
        relative_strength = average_gain / average_loss
        return 100 - (100 / (1 + relative_strength))

    result[window] = current_value()
    for index in range(window + 1, len(values)):
        average_gain = ((average_gain * (window - 1)) + gains[index - 1]) / window
        average_loss = ((average_loss * (window - 1)) + losses[index - 1]) / window
        result[index] = current_value()
    return result


def _bollinger(
    values: list[float],
    window: int = 20,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    middle = _sma(values, window)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for index in range(window - 1, len(values)):
        sample = values[index - window + 1 : index + 1]
        mean = middle[index]
        if mean is None:
            continue
        deviation = math.sqrt(sum((item - mean) ** 2 for item in sample) / window)
        upper[index] = mean + 2 * deviation
        lower[index] = mean - 2 * deviation
    return middle, upper, lower


def _rounded(value: float | None) -> float | None:
    return round(value, 6) if value is not None and math.isfinite(value) else None


def calculate_indicators(bars: Iterable[PriceBar]) -> list[PriceBar]:
    source = list(bars)
    closes = [bar.close for bar in source]
    ma5, ma10, ma20, ma60 = (_sma(closes, window) for window in (5, 10, 20, 60))
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = [fast - slow for fast, slow in zip(ema12, ema26, strict=True)]
    dea = _ema(dif, 9)
    histogram = [(dif_value - dea_value) * 2 for dif_value, dea_value in zip(dif, dea, strict=True)]
    rsi14 = _rsi(closes)
    boll_mid, boll_upper, boll_lower = _bollinger(closes)

    result: list[PriceBar] = []
    for index, bar in enumerate(source):
        result.append(
            bar.model_copy(
                update={
                    "ma5": _rounded(ma5[index]),
                    "ma10": _rounded(ma10[index]),
                    "ma20": _rounded(ma20[index]),
                    "ma60": _rounded(ma60[index]),
                    "macd_dif": _rounded(dif[index]),
                    "macd_dea": _rounded(dea[index]),
                    "macd_hist": _rounded(histogram[index]),
                    "rsi14": _rounded(rsi14[index]),
                    "boll_mid": _rounded(boll_mid[index]),
                    "boll_upper": _rounded(boll_upper[index]),
                    "boll_lower": _rounded(boll_lower[index]),
                }
            )
        )
    return result
