from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    return datetime.now(tz=SHANGHAI)


def iso_now() -> str:
    return now_shanghai().isoformat(timespec="seconds")
