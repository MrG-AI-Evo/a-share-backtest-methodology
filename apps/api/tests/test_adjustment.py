from datetime import datetime

from app.core.time import SHANGHAI
from app.models import CorporateAction, PriceBar
from app.services.adjustment import adjust_bars


def test_qfq_and_hfq_use_only_actions_known_by_as_of() -> None:
    bars = [
        PriceBar(
            timestamp="2026-06-01T15:00:00+08:00",
            open=10,
            high=10,
            low=10,
            close=10,
            volume_shares=1000,
        ),
        PriceBar(
            timestamp="2026-06-02T15:00:00+08:00",
            open=9,
            high=9,
            low=9,
            close=9,
            volume_shares=1000,
        ),
    ]
    action = CorporateAction(
        action_id="action-0001",
        symbol="600000",
        action_type="PRICE_FACTOR",
        announced_at="2026-05-20T18:00:00+08:00",
        effective_date="2026-06-02",
        price_factor=0.9,
        source_id="SSE",
        source_uri="https://www.sse.com.cn/fixture",
        retrieved_at="2026-05-20T18:10:00+08:00",
        quality="A",
    )
    as_of = datetime(2026, 6, 2, 15, 0, tzinfo=SHANGHAI)
    qfq = adjust_bars(bars, [action], "qfq", as_of)
    hfq = adjust_bars(bars, [action], "hfq", as_of)
    assert qfq[0].close == 9
    assert qfq[1].close == 9
    assert hfq[0].close == 10
    assert hfq[1].close == 10


def test_future_announcement_never_adjusts_history() -> None:
    bar = PriceBar(
        timestamp="2026-06-01T15:00:00+08:00",
        open=10,
        high=10,
        low=10,
        close=10,
        volume_shares=1000,
    )
    action = CorporateAction(
        action_id="action-0002",
        symbol="600000",
        action_type="PRICE_FACTOR",
        announced_at="2026-06-03T18:00:00+08:00",
        effective_date="2026-06-02",
        price_factor=0.9,
        source_id="SSE",
        source_uri="https://www.sse.com.cn/fixture",
        retrieved_at="2026-06-03T18:10:00+08:00",
        quality="A",
    )
    as_of = datetime(2026, 6, 2, 15, 0, tzinfo=SHANGHAI)
    assert adjust_bars([bar], [action], "qfq", as_of)[0].close == 10
