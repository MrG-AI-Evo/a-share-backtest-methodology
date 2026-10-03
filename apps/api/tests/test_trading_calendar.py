from datetime import datetime
from pathlib import Path

import pytest

from app.core.settings import PROJECT_ROOT, Settings
from app.core.time import SHANGHAI
from app.core.trading_calendar import CalendarCoverageError, load_trading_calendar


def test_calendar_skips_2026_exchange_holidays() -> None:
    calendar = load_trading_calendar(Settings())
    moment = datetime(2026, 2, 13, 18, 0, tzinfo=SHANGHAI)
    assert calendar.next_open(moment).isoformat() == "2026-02-24T09:30:00+08:00"
    assert calendar.is_trading_day(datetime(2026, 8, 25).date()) is True
    assert calendar.is_trading_day(datetime(2026, 10, 5).date()) is False


def test_calendar_fails_closed_outside_coverage(tmp_path: Path) -> None:
    settings = Settings(trading_calendar_file=PROJECT_ROOT / "rules" / "trading-calendar-2026.yaml")
    calendar = load_trading_calendar(settings)
    with pytest.raises(CalendarCoverageError, match="覆盖范围"):
        calendar.next_open(datetime(2026, 12, 31, 18, 0, tzinfo=SHANGHAI))
