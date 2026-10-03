from __future__ import annotations

import asyncio
from datetime import datetime

import duckdb

from app.core.settings import Settings
from app.core.time import iso_now
from app.models import PriceBar

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_bars (
    symbol VARCHAR NOT NULL,
    period VARCHAR NOT NULL,
    adjustment VARCHAR NOT NULL,
    minute_interval INTEGER NOT NULL DEFAULT 0,
    bar_time TIMESTAMPTZ NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume_shares DOUBLE NOT NULL,
    amount_cny DOUBLE,
    source VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (symbol, period, adjustment, minute_interval, bar_time)
);
"""


class BarCacheRepository:
    def __init__(self, settings: Settings) -> None:
        self._database = settings.analytics_database

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with duckdb.connect(str(self._database)) as connection:
            connection.execute(SCHEMA)

    async def upsert(
        self,
        symbol: str,
        period: str,
        adjustment: str,
        minute_interval: int,
        bars: list[PriceBar],
        source: str,
    ) -> None:
        if bars:
            await asyncio.to_thread(
                self._upsert_sync,
                symbol,
                period,
                adjustment,
                minute_interval,
                bars,
                source,
            )

    def _upsert_sync(
        self,
        symbol: str,
        period: str,
        adjustment: str,
        minute_interval: int,
        bars: list[PriceBar],
        source: str,
    ) -> None:
        rows = [
            (
                symbol,
                period,
                adjustment,
                minute_interval,
                bar.timestamp,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume_shares,
                bar.amount_cny,
                source,
                iso_now(),
            )
            for bar in bars
        ]
        with duckdb.connect(str(self._database)) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO price_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    async def load(
        self,
        symbol: str,
        period: str,
        adjustment: str,
        minute_interval: int,
        limit: int,
    ) -> tuple[list[PriceBar], str] | None:
        rowset = await asyncio.to_thread(
            self._load_sync,
            symbol,
            period,
            adjustment,
            minute_interval,
            limit,
        )
        if not rowset:
            return None
        bars = [
            PriceBar(
                timestamp=row[0].isoformat() if isinstance(row[0], datetime) else str(row[0]),
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                volume_shares=row[5],
                amount_cny=row[6],
            )
            for row in reversed(rowset)
        ]
        fetched_at = rowset[0][7]
        return bars, fetched_at.isoformat() if isinstance(fetched_at, datetime) else str(fetched_at)

    def _load_sync(
        self,
        symbol: str,
        period: str,
        adjustment: str,
        minute_interval: int,
        limit: int,
    ) -> list[tuple[datetime, float, float, float, float, float, float | None, datetime]]:
        with duckdb.connect(str(self._database), read_only=True) as connection:
            return connection.execute(
                """
                SELECT bar_time, open, high, low, close, volume_shares, amount_cny, fetched_at
                FROM price_bars
                WHERE symbol = ? AND period = ? AND adjustment = ? AND minute_interval = ?
                ORDER BY bar_time DESC
                LIMIT ?
                """,
                [symbol, period, adjustment, minute_interval, limit],
            ).fetchall()
