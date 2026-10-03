from datetime import datetime, timedelta

from app.core.time import SHANGHAI
from app.models import PriceBar
from app.services.indicators import calculate_indicators


def test_indicators_are_computed_by_deterministic_code() -> None:
    start = datetime(2026, 1, 1, 15, tzinfo=SHANGHAI)
    bars = [
        PriceBar(
            timestamp=(start + timedelta(days=index)).isoformat(),
            open=10 + index,
            high=11 + index,
            low=9 + index,
            close=10 + index,
            volume_shares=100_000,
        )
        for index in range(65)
    ]
    calculated = calculate_indicators(bars)
    assert calculated[3].ma5 is None
    assert calculated[4].ma5 == 12
    assert calculated[-1].ma60 is not None
    assert calculated[-1].rsi14 == 100
    assert calculated[-1].boll_upper is not None
    assert calculated[-1].macd_hist is not None
