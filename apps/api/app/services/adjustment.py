from __future__ import annotations

from datetime import datetime
from typing import Literal

from app.models import CorporateAction, PriceBar


class AdjustmentError(RuntimeError):
    pass


def adjust_bars(
    bars: list[PriceBar],
    actions: list[CorporateAction],
    adjustment: Literal["qfq", "hfq", "none"],
    as_of: datetime,
) -> list[PriceBar]:
    if adjustment == "none":
        return bars
    if as_of.tzinfo is None:
        raise AdjustmentError("复权 as_of 必须带时区")
    eligible = [
        action
        for action in actions
        if datetime.fromisoformat(action.announced_at) <= as_of
        and action.effective_date <= as_of.date().isoformat()
    ]
    adjusted: list[PriceBar] = []
    for bar in bars:
        try:
            trade_date = datetime.fromisoformat(bar.timestamp).date().isoformat()
        except ValueError as exc:
            raise AdjustmentError(f"K 线时间无法解析: {bar.timestamp}") from exc
        if adjustment == "qfq":
            factors = [
                action.price_factor for action in eligible if action.effective_date > trade_date
            ]
            multiplier = 1.0
            for factor in factors:
                multiplier *= factor
        else:
            factors = [
                action.price_factor for action in eligible if action.effective_date <= trade_date
            ]
            multiplier = 1.0
            for factor in factors:
                multiplier /= factor
        adjusted.append(
            bar.model_copy(
                update={
                    "open": round(bar.open * multiplier, 6),
                    "high": round(bar.high * multiplier, 6),
                    "low": round(bar.low * multiplier, 6),
                    "close": round(bar.close * multiplier, 6),
                }
            )
        )
    return adjusted
