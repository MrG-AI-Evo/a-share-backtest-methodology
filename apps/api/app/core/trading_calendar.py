from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import yaml

from app.core.settings import Settings
from app.core.time import SHANGHAI


class CalendarCoverageError(RuntimeError):
    pass


@dataclass(frozen=True)
class TradingCalendar:
    version: str
    valid_from: date
    valid_until: date
    closed_weekdays: frozenset[date]

    def _ensure_covered(self, value: date) -> None:
        if value < self.valid_from or value > self.valid_until:
            raise CalendarCoverageError(
                f"交易日 {value.isoformat()} 超出日历 {self.version} 覆盖范围"
            )

    def is_trading_day(self, value: date) -> bool:
        self._ensure_covered(value)
        return value.weekday() < 5 and value not in self.closed_weekdays

    def next_trading_day(self, value: date) -> date:
        candidate = value + timedelta(days=1)
        while True:
            self._ensure_covered(candidate)
            if candidate.weekday() < 5 and candidate not in self.closed_weekdays:
                return candidate
            candidate += timedelta(days=1)

    def next_open(self, moment: datetime) -> datetime:
        local = moment.astimezone(SHANGHAI)
        trade_day = self.next_trading_day(local.date())
        return datetime.combine(trade_day, time(9, 30), tzinfo=SHANGHAI)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CalendarCoverageError(f"{name} 必须是对象")
    return value


def load_trading_calendar(settings: Settings) -> TradingCalendar:
    try:
        root = _mapping(
            yaml.safe_load(settings.trading_calendar_file.read_text("utf-8")),
            "calendar",
        )
        version = str(root["calendar_version"])
        valid_from = date.fromisoformat(str(root["valid_from"]))
        valid_until = date.fromisoformat(str(root["valid_until"]))
        raw_closed = root["closed_weekdays"]
        if not isinstance(raw_closed, list):
            raise CalendarCoverageError("closed_weekdays 必须是日期数组")
        closed = frozenset(date.fromisoformat(str(item)) for item in raw_closed)
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise CalendarCoverageError(f"无法加载版本化交易日历: {exc}") from exc
    if any(day.weekday() >= 5 for day in closed):
        raise CalendarCoverageError("closed_weekdays 只记录额外休市的工作日")
    return TradingCalendar(version, valid_from, valid_until, closed)
