from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import aiosqlite

from app.backtests.models import (
    BacktestAuditEvent,
    BacktestBlocker,
    BacktestCheckpoint,
    BacktestRunSummary,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    purpose TEXT NOT NULL,
    scenario TEXT NOT NULL,
    segment TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    data_cutoff TEXT,
    blockers_json TEXT NOT NULL,
    performance_available INTEGER NOT NULL CHECK (performance_available IN (0, 1)),
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS backtest_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id),
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    known_at TEXT NOT NULL,
    symbol TEXT,
    payload_json TEXT NOT NULL,
    prev_event_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE,
    UNIQUE(run_id, sequence)
);
CREATE TABLE IF NOT EXISTS backtest_checkpoints (
    run_id TEXT NOT NULL REFERENCES backtest_runs(run_id),
    sequence INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    account_summary_json TEXT NOT NULL,
    performance_json TEXT NOT NULL,
    reconciliation_status TEXT NOT NULL,
    data_quality_status TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(run_id, sequence)
);
"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class BacktestRepository:
    def __init__(self, database: Path) -> None:
        self.database = database

    async def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database) as db:
            await db.executescript(SCHEMA)
            cursor = await db.execute("PRAGMA table_info(backtest_checkpoints)")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if "snapshot_json" not in columns:
                await db.execute(
                    "ALTER TABLE backtest_checkpoints ADD COLUMN "
                    "snapshot_json TEXT NOT NULL DEFAULT '{}'"
                )
            await db.commit()

    async def create_blocked_run(
        self,
        *,
        run_id: str,
        policy_version: str,
        requested_at: str,
        blockers: list[BacktestBlocker],
        summary: str,
    ) -> BacktestRunSummary:
        if not blockers:
            raise ValueError("阻断运行必须至少包含一个 blocker")
        async with aiosqlite.connect(self.database) as db:
            await db.execute(
                """
                INSERT INTO backtest_runs(
                    run_id, policy_version, purpose, scenario, segment, status,
                    requested_at, started_at, finished_at, data_cutoff, blockers_json,
                    performance_available, summary, created_at
                ) VALUES (?, ?, 'READINESS_PREFLIGHT', 'BASE', 'PRIMARY_TEN_YEAR',
                          'BLOCKED', ?, NULL, ?, NULL, ?, 0, ?, ?)
                """,
                (
                    run_id,
                    policy_version,
                    requested_at,
                    requested_at,
                    _json([item.model_dump(mode="json") for item in blockers]),
                    summary,
                    requested_at,
                ),
            )
            await self._append_event_with_db(
                db,
                run_id=run_id,
                event_type="FORMAL_BACKTEST_BLOCKED",
                occurred_at=requested_at,
                known_at=requested_at,
                symbol=None,
                payload={
                    "blocker_ids": [item.blocker_id for item in blockers],
                    "performance_generated": False,
                    "reason": summary,
                },
            )
            await db.commit()
        run = await self.get_run(run_id)
        assert run is not None
        return run

    async def save_completed_run(
        self,
        *,
        run_id: str,
        policy_version: str,
        scenario: str,
        requested_at: str,
        started_at: str,
        finished_at: str,
        data_cutoff: str,
        events: list[dict[str, object]],
        checkpoints: list[dict[str, object]],
    ) -> BacktestRunSummary:
        if not checkpoints:
            raise ValueError("正式回测必须包含检查点")
        if any(
            cast(dict[str, object], item["reconciliation"])["status"] != "PASS"
            for item in checkpoints
        ):
            raise ValueError("账本闭合失败，禁止保存正式绩效")
        for checkpoint in checkpoints:
            data_quality = cast(dict[str, object], checkpoint["data_quality"])
            if (
                data_quality.get("raw_data_mode") != "REAL_POINT_IN_TIME"
                or data_quality.get("synthetic_raw_observation_count") != 0
                or data_quality.get("time_travel_check") != "PASS"
                or data_quality.get("ledger_check") != "PASS"
            ):
                raise ValueError("正式检查点未通过真实点时、无合成、时间穿越或账本门")
        previous_hash: str | None = None
        for expected_sequence, event in enumerate(events, start=1):
            core = {
                key: event[key]
                for key in (
                    "run_id",
                    "sequence",
                    "event_type",
                    "occurred_at",
                    "known_at",
                    "symbol",
                    "payload",
                    "prev_event_hash",
                )
            }
            expected_hash = hashlib.sha256(
                f"{previous_hash or ''}{_json(core)}".encode()
            ).hexdigest()
            if (
                event["run_id"] != run_id
                or event["sequence"] != expected_sequence
                or event.get("prev_event_hash") != previous_hash
                or event.get("event_hash") != expected_hash
            ):
                raise ValueError("正式审计事件哈希链失败")
            previous_hash = expected_hash
        final_performance = cast(dict[str, object], checkpoints[-1]["performance"])
        if final_performance.get("calculation_status") != "COMPLETE":
            raise ValueError("绩效计算未完成，禁止保存正式运行")
        async with aiosqlite.connect(self.database) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO backtest_runs(
                        run_id, policy_version, purpose, scenario, segment, status,
                        requested_at, started_at, finished_at, data_cutoff, blockers_json,
                        performance_available, summary, created_at
                    ) VALUES (?, ?, 'FORMAL_BACKTEST', ?, 'PRIMARY_TEN_YEAR', 'COMPLETE',
                              ?, ?, ?, ?, '[]', 1, ?, ?)
                    """,
                    (
                        run_id,
                        policy_version,
                        scenario,
                        requested_at,
                        started_at,
                        finished_at,
                        data_cutoff,
                        "正式十年回测已完成；绩效来自真实点时数据和闭合账本。",
                        requested_at,
                    ),
                )
                for event in events:
                    await db.execute(
                        """
                        INSERT INTO backtest_events(
                            event_id, run_id, sequence, event_type, occurred_at, known_at,
                            symbol, payload_json, prev_event_hash, event_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event["event_id"],
                            run_id,
                            event["sequence"],
                            event["event_type"],
                            event["occurred_at"],
                            event["known_at"],
                            event.get("symbol"),
                            _json(event["payload"]),
                            event.get("prev_event_hash"),
                            event["event_hash"],
                        ),
                    )
                for sequence, checkpoint in enumerate(checkpoints, start=1):
                    payload_hash = hashlib.sha256(_json(checkpoint).encode()).hexdigest()
                    reconciliation = cast(dict[str, object], checkpoint["reconciliation"])
                    data_quality = cast(dict[str, object], checkpoint["data_quality"])
                    await db.execute(
                        """
                        INSERT INTO backtest_checkpoints(
                            run_id, sequence, as_of, account_summary_json,
                            performance_json, reconciliation_status,
                            data_quality_status, payload_sha256, snapshot_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            sequence,
                            checkpoint["as_of"],
                            _json(checkpoint["account_summary"]),
                            _json(checkpoint["performance"]),
                            reconciliation["status"],
                            data_quality["status"],
                            payload_hash,
                            _json(checkpoint),
                        ),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        run = await self.get_run(run_id)
        assert run is not None
        return run

    async def _append_event_with_db(
        self,
        db: aiosqlite.Connection,
        *,
        run_id: str,
        event_type: str,
        occurred_at: str,
        known_at: str,
        symbol: str | None,
        payload: dict[str, object],
    ) -> None:
        cursor = await db.execute(
            """
            SELECT sequence, event_hash FROM backtest_events
            WHERE run_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (run_id,),
        )
        previous = await cursor.fetchone()
        sequence = int(previous[0]) + 1 if previous else 1
        prev_hash = str(previous[1]) if previous else None
        core = {
            "run_id": run_id,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "known_at": known_at,
            "symbol": symbol,
            "payload": payload,
            "prev_event_hash": prev_hash,
        }
        event_hash = hashlib.sha256(f"{prev_hash or ''}{_json(core)}".encode()).hexdigest()
        event_id = f"{run_id}-event-{sequence:06d}"
        await db.execute(
            """
            INSERT INTO backtest_events(
                event_id, run_id, sequence, event_type, occurred_at, known_at,
                symbol, payload_json, prev_event_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                sequence,
                event_type,
                occurred_at,
                known_at,
                symbol,
                _json(payload),
                prev_hash,
                event_hash,
            ),
        )

    async def list_runs(self) -> list[BacktestRunSummary]:
        async with aiosqlite.connect(self.database) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM backtest_runs ORDER BY requested_at DESC")
            rows = await cursor.fetchall()
        return [self._run_from_row(dict(row)) for row in rows]

    async def get_run(self, run_id: str) -> BacktestRunSummary | None:
        async with aiosqlite.connect(self.database) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,))
            row = await cursor.fetchone()
        return self._run_from_row(dict(row)) if row else None

    async def blockers(self, run_id: str) -> list[BacktestBlocker]:
        async with aiosqlite.connect(self.database) as db:
            cursor = await db.execute(
                "SELECT blockers_json FROM backtest_runs WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
        if row is None:
            return []
        payload = cast(list[dict[str, object]], json.loads(str(row[0])))
        return [BacktestBlocker.model_validate(item) for item in payload]

    async def events(self, run_id: str) -> list[BacktestAuditEvent]:
        async with aiosqlite.connect(self.database) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM backtest_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            )
            rows = await cursor.fetchall()
        return [
            BacktestAuditEvent(
                event_id=str(row["event_id"]),
                run_id=str(row["run_id"]),
                sequence=int(row["sequence"]),
                event_type=str(row["event_type"]),
                occurred_at=str(row["occurred_at"]),
                known_at=str(row["known_at"]),
                symbol=str(row["symbol"]) if row["symbol"] else None,
                payload=cast(dict[str, object], json.loads(str(row["payload_json"]))),
                prev_event_hash=str(row["prev_event_hash"]) if row["prev_event_hash"] else None,
                event_hash=str(row["event_hash"]),
            )
            for row in rows
        ]

    async def event_count(self, run_id: str) -> int:
        async with aiosqlite.connect(self.database) as db:
            cursor = await db.execute(
                "SELECT count(*) FROM backtest_events WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def events_page(
        self, run_id: str, *, offset: int, limit: int
    ) -> list[BacktestAuditEvent]:
        async with aiosqlite.connect(self.database) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM backtest_events WHERE run_id = ?
                ORDER BY sequence LIMIT ? OFFSET ?
                """,
                (run_id, limit, offset),
            )
            rows = await cursor.fetchall()
        return [
            BacktestAuditEvent(
                event_id=str(row["event_id"]),
                run_id=str(row["run_id"]),
                sequence=int(row["sequence"]),
                event_type=str(row["event_type"]),
                occurred_at=str(row["occurred_at"]),
                known_at=str(row["known_at"]),
                symbol=str(row["symbol"]) if row["symbol"] else None,
                payload=cast(dict[str, object], json.loads(str(row["payload_json"]))),
                prev_event_hash=str(row["prev_event_hash"]) if row["prev_event_hash"] else None,
                event_hash=str(row["event_hash"]),
            )
            for row in rows
        ]

    async def audit_chain_valid(self, run_id: str) -> bool:
        events = await self.events(run_id)
        previous_hash: str | None = None
        for event in events:
            core = {
                "run_id": run_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at,
                "known_at": event.known_at,
                "symbol": event.symbol,
                "payload": event.payload,
                "prev_event_hash": event.prev_event_hash,
            }
            expected = hashlib.sha256(
                f"{previous_hash or ''}{_json(core)}".encode()
            ).hexdigest()
            if event.prev_event_hash != previous_hash or event.event_hash != expected:
                return False
            previous_hash = event.event_hash
        return True

    async def checkpoints(self, run_id: str) -> list[BacktestCheckpoint]:
        async with aiosqlite.connect(self.database) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM backtest_checkpoints WHERE run_id = ? ORDER BY sequence", (run_id,)
            )
            rows = await cursor.fetchall()
        result: list[BacktestCheckpoint] = []
        for row in rows:
            snapshot = cast(dict[str, object], json.loads(str(row["snapshot_json"])))
            security_ledgers = cast(list[dict[str, object]], snapshot.get("security_ledgers", []))
            cash_flows = cast(list[dict[str, object]], snapshot.get("account_cash_flows", []))
            result.append(BacktestCheckpoint(
                sequence=int(row["sequence"]),
                as_of=str(row["as_of"]),
                account_summary=cast(
                    dict[str, object], json.loads(str(row["account_summary_json"]))
                ),
                performance=cast(dict[str, object], json.loads(str(row["performance_json"]))),
                reconciliation_status=cast(Any, row["reconciliation_status"]),
                data_quality_status=cast(Any, row["data_quality_status"]),
                security_ledgers=security_ledgers,
                account_cash_flows=cash_flows,
            ))
        return result

    async def checkpoint_count(self, run_id: str) -> int:
        async with aiosqlite.connect(self.database) as db:
            cursor = await db.execute(
                "SELECT count(*) FROM backtest_checkpoints WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def latest_checkpoint(self, run_id: str) -> BacktestCheckpoint | None:
        async with aiosqlite.connect(self.database) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM backtest_checkpoints WHERE run_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (run_id,),
            )
            row = await cursor.fetchone()
        return self._checkpoint_from_row(row) if row else None

    async def checkpoints_page(
        self, run_id: str, *, offset: int, limit: int
    ) -> list[BacktestCheckpoint]:
        async with aiosqlite.connect(self.database) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM backtest_checkpoints WHERE run_id = ?
                ORDER BY sequence LIMIT ? OFFSET ?
                """,
                (run_id, limit, offset),
            )
            rows = await cursor.fetchall()
        return [self._checkpoint_from_row(row) for row in rows]

    @staticmethod
    def _checkpoint_from_row(row: aiosqlite.Row) -> BacktestCheckpoint:
        snapshot = cast(dict[str, object], json.loads(str(row["snapshot_json"])))
        security_ledgers = cast(list[dict[str, object]], snapshot.get("security_ledgers", []))
        cash_flows = cast(list[dict[str, object]], snapshot.get("account_cash_flows", []))
        return BacktestCheckpoint(
            sequence=int(row["sequence"]),
            as_of=str(row["as_of"]),
            account_summary=cast(
                dict[str, object], json.loads(str(row["account_summary_json"]))
            ),
            performance=cast(dict[str, object], json.loads(str(row["performance_json"]))),
            reconciliation_status=cast(Any, row["reconciliation_status"]),
            data_quality_status=cast(Any, row["data_quality_status"]),
            security_ledgers=security_ledgers,
            account_cash_flows=cash_flows,
        )

    @staticmethod
    def _run_from_row(row: dict[str, Any]) -> BacktestRunSummary:
        blockers = cast(list[object], json.loads(str(row["blockers_json"])))
        return BacktestRunSummary(
            run_id=str(row["run_id"]),
            policy_version=str(row["policy_version"]),
            purpose=cast(Any, row["purpose"]),
            scenario=cast(Any, row["scenario"]),
            segment=cast(Any, row["segment"]),
            status=cast(Any, row["status"]),
            requested_at=str(row["requested_at"]),
            started_at=str(row["started_at"]) if row["started_at"] else None,
            finished_at=str(row["finished_at"]) if row["finished_at"] else None,
            data_cutoff=str(row["data_cutoff"]) if row["data_cutoff"] else None,
            blocker_count=len(blockers),
            performance_available=bool(row["performance_available"]),
            summary=str(row["summary"]),
        )
