from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from uuid import uuid4

import duckdb

from app.core.settings import Settings
from app.core.time import iso_now
from app.models import MarketOverview

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    snapshot_id VARCHAR PRIMARY KEY,
    dataset VARCHAR NOT NULL,
    source_timestamp TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL,
    payload_json JSON NOT NULL,
    payload_sha256 VARCHAR NOT NULL
);
CREATE INDEX IF NOT EXISTS market_snapshots_dataset_fetched
ON market_snapshots (dataset, fetched_at DESC);
"""


class MarketCacheRepository:
    def __init__(self, settings: Settings) -> None:
        self._database = settings.analytics_database

    async def initialize(self) -> None:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with duckdb.connect(str(self._database)) as connection:
            connection.execute(SCHEMA)

    async def append(self, overview: MarketOverview, source_timestamp: str | None) -> None:
        payload = overview.model_dump_json()
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        await asyncio.to_thread(self._append_sync, payload, digest, source_timestamp)

    def _append_sync(self, payload: str, digest: str, source_timestamp: str | None) -> None:
        with duckdb.connect(str(self._database)) as connection:
            existing = connection.execute(
                "SELECT 1 FROM market_snapshots WHERE payload_sha256 = ? LIMIT 1",
                [digest],
            ).fetchone()
            if existing:
                return
            connection.execute(
                """
                INSERT INTO market_snapshots
                (snapshot_id, dataset, source_timestamp, fetched_at, payload_json, payload_sha256)
                VALUES (?, 'market_overview', ?, ?, ?, ?)
                """,
                [str(uuid4()), source_timestamp, iso_now(), payload, digest],
            )

    async def latest(self) -> tuple[MarketOverview, str | None, str] | None:
        row = await asyncio.to_thread(self._latest_sync)
        if row is None:
            return None
        payload, source_timestamp, fetched_at = row
        return (
            MarketOverview.model_validate(json.loads(payload)),
            source_timestamp.isoformat() if isinstance(source_timestamp, datetime) else None,
            fetched_at.isoformat() if isinstance(fetched_at, datetime) else str(fetched_at),
        )

    def _latest_sync(self) -> tuple[str, datetime | None, datetime] | None:
        with duckdb.connect(str(self._database), read_only=True) as connection:
            row = connection.execute(
                """
                SELECT CAST(payload_json AS VARCHAR), source_timestamp, fetched_at
                FROM market_snapshots
                WHERE dataset = 'market_overview'
                ORDER BY fetched_at DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return str(row[0]), row[1], row[2]
