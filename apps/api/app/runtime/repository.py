from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import aiosqlite

from app.runtime.models import DeterministicException, DeterministicTaskRun, TaskId

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
CREATE TABLE IF NOT EXISTS deterministic_task_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    as_of TEXT NOT NULL,
    data_cutoff TEXT,
    runtime_version TEXT NOT NULL,
    ruleset_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    artifact_path TEXT,
    exception_package_path TEXT
);
CREATE INDEX IF NOT EXISTS deterministic_task_runs_task_idx
ON deterministic_task_runs(task_id, requested_at DESC);
CREATE TABLE IF NOT EXISTS deterministic_task_audit_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES deterministic_task_runs(run_id),
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_event_hash TEXT,
    event_hash TEXT NOT NULL UNIQUE,
    UNIQUE(run_id, sequence)
);
"""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DeterministicRuntimeRepository:
    def __init__(self, database: Path) -> None:
        self.database = database

    async def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def save(self, task_run: DeterministicTaskRun) -> None:
        payload = task_run.model_dump(mode="json")
        async with aiosqlite.connect(self.database) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                await db.execute(
                    """
                    INSERT INTO deterministic_task_runs(
                        run_id, task_id, status, requested_at, finished_at, as_of, data_cutoff,
                        runtime_version, ruleset_version, policy_version, input_hash, payload_json,
                        artifact_path, exception_package_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_run.run_id,
                        task_run.task_id,
                        task_run.status,
                        task_run.requested_at,
                        task_run.finished_at,
                        task_run.as_of,
                        task_run.data_cutoff,
                        task_run.runtime_version,
                        task_run.ruleset_version,
                        task_run.policy_version,
                        task_run.input_hash,
                        _json(payload),
                        task_run.artifact_path,
                        task_run.exception_package_path,
                    ),
                )
                core = {
                    "run_id": task_run.run_id,
                    "sequence": 1,
                    "event_type": "DETERMINISTIC_TASK_FINISHED",
                    "occurred_at": task_run.finished_at,
                    "payload": {
                        "task_id": task_run.task_id,
                        "status": task_run.status,
                        "input_hash": task_run.input_hash,
                        "artifact_hash": task_run.artifact_hash,
                        "blockers": task_run.blockers,
                        "exception_count": len(task_run.exceptions),
                    },
                    "prev_event_hash": None,
                }
                event_hash = hashlib.sha256(_json(core).encode()).hexdigest()
                await db.execute(
                    """
                    INSERT INTO deterministic_task_audit_events(
                        event_id, run_id, sequence, event_type, occurred_at, payload_json,
                        prev_event_hash, event_hash
                    ) VALUES (?, ?, 1, ?, ?, ?, NULL, ?)
                    """,
                    (
                        f"{task_run.run_id}-audit-000001",
                        task_run.run_id,
                        core["event_type"],
                        task_run.finished_at,
                        _json(core["payload"]),
                        event_hash,
                    ),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def latest_by_task(self, task_id: TaskId) -> DeterministicTaskRun | None:
        async with aiosqlite.connect(self.database) as db:
            row = await (
                await db.execute(
                    "SELECT payload_json FROM deterministic_task_runs WHERE task_id = ? "
                    "ORDER BY requested_at DESC LIMIT 1",
                    (task_id,),
                )
            ).fetchone()
        return self._from_payload(row[0]) if row else None

    async def list_runs(self, limit: int = 100) -> list[DeterministicTaskRun]:
        async with aiosqlite.connect(self.database) as db:
            rows = await (
                await db.execute(
                    "SELECT payload_json FROM deterministic_task_runs "
                    "ORDER BY requested_at DESC LIMIT ?",
                    (limit,),
                )
            ).fetchall()
        return [self._from_payload(row[0]) for row in rows]

    async def get(self, run_id: str) -> DeterministicTaskRun | None:
        async with aiosqlite.connect(self.database) as db:
            row = await (
                await db.execute(
                    "SELECT payload_json FROM deterministic_task_runs WHERE run_id = ?", (run_id,)
                )
            ).fetchone()
        return self._from_payload(row[0]) if row else None

    @staticmethod
    def _from_payload(raw: str) -> DeterministicTaskRun:
        payload: dict[str, Any] = json.loads(raw)
        payload["exceptions"] = [DeterministicException(**item) for item in payload["exceptions"]]
        return DeterministicTaskRun(**payload)
