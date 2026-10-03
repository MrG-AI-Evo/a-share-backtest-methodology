from __future__ import annotations

import math


def calculate_sector_heat(change_pct: float, amount_cny: float | None) -> float:
    """Return a deterministic 0-100 display score from change and turnover.

    This is a local presentation metric, not a source field or investment signal.
    """
    turnover_component = math.log10(max((amount_cny or 0) / 100_000_000, 0) + 1) * 12
    return round(min(100, abs(change_pct) * 10 + turnover_component), 1)
