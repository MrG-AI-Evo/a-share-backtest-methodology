from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.models import StrictModel

TaskId = Literal[
    "DATA_UPDATE_VALIDATE",
    "DAILY_EXIT_CHECK",
    "MONTHLY_CANDIDATE_SCREEN",
    "NEXT_OPEN_PAPER_FILL",
    "CORPORATE_ACTIONS_LEDGER",
    "ANNUAL_PNL_SNAPSHOT",
    "EXCEPTION_EVIDENCE_PACKAGE",
]
TaskStatus = Literal["COMPLETE", "BLOCKED", "FAILED"]
ExceptionSeverity = Literal["INFO", "WARNING", "BLOCKER", "CRITICAL"]


class RuntimeTaskDefinition(StrictModel):
    task_id: TaskId
    enabled: Literal[False] = False
    manual_command_allowed: Literal[True] = True
    cadence: str
    trigger: str
    description: str
    latest_run: DeterministicTaskRun | None = None


class DeterministicException(StrictModel):
    code: str
    severity: ExceptionSeverity
    message: str
    evidence_refs: list[str] = Field(default_factory=list)
    suggested_human_action: str


class DeterministicTaskRun(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    task_id: TaskId
    execution_mode: Literal["MANUAL_COMMAND"] = "MANUAL_COMMAND"
    status: TaskStatus
    requested_at: str
    finished_at: str
    execution_duration_ms: int = Field(ge=0)
    as_of: str
    data_cutoff: str | None = None
    runtime_version: str
    ruleset_version: str
    policy_version: str
    input_hash: str
    counts: dict[str, int | float | None] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    exceptions: list[DeterministicException] = Field(default_factory=list)
    summary: str
    artifact_hash: str
    artifact_path: str | None = None
    exception_package_path: str | None = None


class AnnualPnl(StrictModel):
    year: int
    year_start_total_assets_cny: float
    year_end_total_assets_cny: float
    annual_external_contributions_cny: float
    annual_external_withdrawals_cny: float
    cumulative_external_contributions_cny: float
    cumulative_external_withdrawals_cny: float
    annual_net_pnl_cny: float
    cumulative_pnl_cny: float
    attribution: dict[str, float | None] = Field(default_factory=dict)


class RuntimeStatus(StrictModel):
    runtime_id: str
    runtime_version: str
    status: Literal["DISABLED"]
    execution_mode: Literal["MANUAL_ONLY"]
    scheduler_enabled: Literal[False] = False
    llm_api_enabled: Literal[False] = False
    web_ai_chat_enabled: Literal[False] = False
    real_broker_enabled: Literal[False] = False
    automatic_trading_enabled: Literal[False] = False
    activation_gate: dict[str, object]
    tasks: list[RuntimeTaskDefinition]
    annual_pnl: list[AnnualPnl] = Field(default_factory=list)
    current_blockers: list[str] = Field(default_factory=list)
    checked_at: str


RuntimeTaskDefinition.model_rebuild()
