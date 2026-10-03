from __future__ import annotations

from datetime import date
from decimal import Decimal


def xirr(
    investor_cash_flows: list[tuple[date, Decimal]],
    *,
    terminal_date: date,
    terminal_equity: Decimal,
) -> float | None:
    flows = [*investor_cash_flows, (terminal_date, terminal_equity)]
    if (
        not flows
        or not any(amount < 0 for _, amount in flows)
        or not any(amount > 0 for _, amount in flows)
    ):
        return None
    start = min(day for day, _ in flows)

    def npv(rate: float) -> float:
        return float(
            sum(
                (
                    float(amount) / ((1 + rate) ** ((day - start).days / 365.0))
                    for day, amount in flows
                ),
                0.0,
            )
        )

    low = -0.9999
    high = 1.0
    low_value = npv(low)
    high_value = npv(high)
    while low_value * high_value > 0 and high < 1_000:
        high *= 2
        high_value = npv(high)
    if low_value * high_value > 0:
        return None
    for _ in range(200):
        middle = (low + high) / 2
        value = npv(middle)
        if abs(value) < 1e-9:
            return middle
        if low_value * value <= 0:
            high = middle
        else:
            low = middle
            low_value = value
    return (low + high) / 2


def link_subperiod_returns(subperiod_returns: list[Decimal]) -> float | None:
    if not subperiod_returns:
        return None
    linked = Decimal("1")
    for period_return in subperiod_returns:
        linked *= Decimal("1") + period_return
    return float(linked - Decimal("1"))
