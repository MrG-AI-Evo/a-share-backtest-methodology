from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.models import StrictModel

BacktestRunStatus = Literal[
    "PREFLIGHT",
    "BLOCKED",
    "RUNNING",
    "COMPLETE",
    "FAILED",
]


class BacktestWindow(StrictModel):
    label: str
    start_date: str
    end_date: str
    included_in_primary_performance: bool


class BacktestBlocker(StrictModel):
    blocker_id: str
    category: Literal[
        "EXTERNAL_DATA",
        "RULE_AND_POINT_IN_TIME_DATA",
        "LOCAL_ENGINEERING",
        "USER_DECISION",
        "DATA_COVERAGE",
    ]
    status: Literal["OPEN", "IN_PROGRESS", "CLOSED"]
    description: str
    evidence: list[str] = Field(default_factory=list)


class DatasetCoverage(StrictModel):
    dataset: str
    row_count: int = Field(ge=0)
    minimum_effective_date: str | None = None
    maximum_effective_date: str | None = None
    point_in_time_row_count: int = Field(ge=0)
    synthetic_row_count: int = Field(ge=0)
    ready_for_primary_window: bool


class BacktestReadiness(StrictModel):
    policy_id: str
    policy_version: str
    policy_status: str
    base_policy_sha256_verified: bool
    primary_window: BacktestWindow
    extension_window: BacktestWindow
    formal_backtest_executable: bool
    engineering_ready: bool
    blockers: list[BacktestBlocker]
    dataset_coverage: list[DatasetCoverage]
    safety: dict[str, bool]
    checked_at: str


class BacktestRunSummary(StrictModel):
    run_id: str
    policy_version: str
    purpose: Literal["FORMAL_BACKTEST", "MECHANISM_TEST", "READINESS_PREFLIGHT"]
    scenario: Literal["OPTIMISTIC", "BASE", "STRESS"]
    segment: Literal["PRIMARY_TEN_YEAR", "EXTENSION_2026", "MECHANISM_ONLY"]
    status: BacktestRunStatus
    requested_at: str
    started_at: str | None = None
    finished_at: str | None = None
    data_cutoff: str | None = None
    blocker_count: int = Field(ge=0)
    performance_available: bool
    summary: str


class BacktestAuditEvent(StrictModel):
    event_id: str
    run_id: str
    sequence: int = Field(ge=1)
    event_type: str
    occurred_at: str
    known_at: str
    symbol: str | None = None
    payload: dict[str, object]
    prev_event_hash: str | None = None
    event_hash: str


class BacktestCheckpoint(StrictModel):
    sequence: int = Field(ge=1)
    as_of: str
    account_summary: dict[str, object]
    performance: dict[str, object]
    reconciliation_status: Literal["PASS", "FAIL", "NOT_RUN"]
    data_quality_status: Literal["VALID", "STALE", "DEGRADED", "INVALID", "NOT_RUN"]
    security_ledgers: list[dict[str, object]] = Field(default_factory=list)
    account_cash_flows: list[dict[str, object]] = Field(default_factory=list)


class BacktestRunDetail(StrictModel):
    run: BacktestRunSummary
    blockers: list[BacktestBlocker]
    checkpoints: list[BacktestCheckpoint]
    checkpoint_count: int = Field(ge=0)
    latest_checkpoint: BacktestCheckpoint | None = None
    events: list[BacktestAuditEvent]
    event_count: int = Field(ge=0)
    audit_chain_valid: bool
    policy_snapshot: dict[str, object]
