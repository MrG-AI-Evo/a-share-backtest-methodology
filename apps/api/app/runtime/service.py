from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from app.backtests.repository import BacktestRepository
from app.backtests.service import BacktestService
from app.core.policy import load_policy, load_ruleset_meta
from app.core.settings import PROJECT_ROOT, Settings
from app.core.time import SHANGHAI, iso_now
from app.repositories.paper import PaperRepository
from app.repositories.screening import ScreeningRepository
from app.runtime.models import (
    AnnualPnl,
    DeterministicException,
    DeterministicTaskRun,
    RuntimeStatus,
    RuntimeTaskDefinition,
    TaskId,
)
from app.runtime.repository import DeterministicRuntimeRepository

TASK_IDS: tuple[TaskId, ...] = (
    "DATA_UPDATE_VALIDATE",
    "DAILY_EXIT_CHECK",
    "MONTHLY_CANDIDATE_SCREEN",
    "NEXT_OPEN_PAPER_FILL",
    "CORPORATE_ACTIONS_LEDGER",
    "ANNUAL_PNL_SNAPSHOT",
    "EXCEPTION_EVIDENCE_PACKAGE",
)
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{11,159}$")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} 必须是对象")
    return cast(dict[str, Any], value)


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_once(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(serialized.encode()).hexdigest()


def _validate_schema(payload: object, schema_name: str) -> None:
    schema = _object(
        json.loads((PROJECT_ROOT / "schemas" / schema_name).read_text("utf-8")), schema_name
    )
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda item: list(item.path),
    )
    if errors:
        where = ".".join(str(item) for item in errors[0].path) or "root"
        raise RuntimeError(f"运行产物不符合 {schema_name}: {where}: {errors[0].message}")


def _as_of(value: str | None) -> str:
    if value is None:
        return iso_now()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("as_of 必须包含时区，例如 2026-09-09T15:00:00+08:00")
    return parsed.astimezone(SHANGHAI).isoformat(timespec="seconds")


def calculate_annual_pnl(checkpoints: Sequence[object]) -> list[AnnualPnl]:
    """Use only cumulative ledger fields; attribution is display-only and never re-added."""
    rows: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        dump = (
            checkpoint.model_dump(mode="json") if hasattr(checkpoint, "model_dump") else checkpoint
        )
        if not isinstance(dump, dict):
            continue
        account = dump.get("account_summary")
        if isinstance(account, dict):
            rows.append({"as_of": dump.get("as_of"), "account": account})
    rows.sort(key=lambda item: str(item["as_of"]))
    by_year: dict[int, dict[str, Any]] = {}
    for row in rows:
        as_of = str(row["as_of"])
        try:
            year = datetime.fromisoformat(as_of).year
        except ValueError:
            continue
        by_year[year] = row

    prior_assets = 0.0
    prior_contributions = 0.0
    prior_withdrawals = 0.0
    prior_dividend_gross = 0.0
    prior_dividend_tax = 0.0
    prior_buy_costs = 0.0
    prior_sell_costs = 0.0
    result: list[AnnualPnl] = []
    for year, row in sorted(by_year.items()):
        account = cast(dict[str, Any], row["account"])
        end_assets = float(account.get("total_assets_cny", 0.0))
        cumulative_in = float(account.get("cumulative_external_contributions_cny", 0.0))
        cumulative_out = float(account.get("cumulative_external_withdrawals_cny", 0.0))
        annual_in = cumulative_in - prior_contributions
        annual_out = cumulative_out - prior_withdrawals
        cumulative_dividend_gross = float(account.get("cumulative_dividend_gross_cny", 0.0))
        cumulative_dividend_tax = float(account.get("cumulative_dividend_tax_cny", 0.0))
        cumulative_buy_costs = float(account.get("cumulative_buy_transaction_costs_cny", 0.0))
        cumulative_sell_costs = float(account.get("cumulative_sell_transaction_costs_cny", 0.0))
        attribution = {
            "dividend_gross_cny": cumulative_dividend_gross - prior_dividend_gross,
            "dividend_tax_cny": cumulative_dividend_tax - prior_dividend_tax,
            "buy_transaction_costs_cny": cumulative_buy_costs - prior_buy_costs,
            "sell_transaction_costs_cny": cumulative_sell_costs - prior_sell_costs,
            "slippage_cny": None,
        }
        result.append(
            AnnualPnl(
                year=year,
                year_start_total_assets_cny=prior_assets,
                year_end_total_assets_cny=end_assets,
                annual_external_contributions_cny=annual_in,
                annual_external_withdrawals_cny=annual_out,
                cumulative_external_contributions_cny=cumulative_in,
                cumulative_external_withdrawals_cny=cumulative_out,
                annual_net_pnl_cny=end_assets + annual_out - prior_assets - annual_in,
                cumulative_pnl_cny=end_assets + cumulative_out - cumulative_in,
                attribution=attribution,
            )
        )
        prior_assets = end_assets
        prior_contributions = cumulative_in
        prior_withdrawals = cumulative_out
        prior_dividend_gross = cumulative_dividend_gross
        prior_dividend_tax = cumulative_dividend_tax
        prior_buy_costs = cumulative_buy_costs
        prior_sell_costs = cumulative_sell_costs
    return result


class DeterministicRuntimeService:
    def __init__(
        self,
        settings: Settings,
        repository: DeterministicRuntimeRepository,
        backtests: BacktestService,
        backtest_repository: BacktestRepository,
        paper_repository: PaperRepository,
        screening_repository: ScreeningRepository,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.backtests = backtests
        self.backtest_repository = backtest_repository
        self.paper_repository = paper_repository
        self.screening_repository = screening_repository

    async def initialize(self) -> None:
        await self.repository.initialize()

    def _config(self) -> dict[str, Any]:
        payload = _object(
            yaml.safe_load(self.settings.deterministic_runtime_config_file.read_text("utf-8")),
            "确定性运行时配置",
        )
        schema = _object(
            json.loads(
                (PROJECT_ROOT / "schemas" / "deterministic-runtime-config.schema.json").read_text(
                    "utf-8"
                )
            ),
            "确定性运行时 Schema",
        )
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
            key=lambda item: list(item.path),
        )
        if errors:
            where = ".".join(str(item) for item in errors[0].path) or "root"
            raise RuntimeError(f"确定性运行时配置不符合 Schema: {where}: {errors[0].message}")
        task_ids = [
            str(_object(item, "task")["id"]) for item in cast(list[object], payload["tasks"])
        ]
        if len(task_ids) != len(TASK_IDS) or set(task_ids) != set(TASK_IDS):
            raise RuntimeError("确定性运行时配置必须且只能声明七个固定任务入口各一次")
        return payload

    async def status(self) -> RuntimeStatus:
        config = self._config()
        readiness = await self.backtests.readiness()
        task_definitions: list[RuntimeTaskDefinition] = []
        for raw in cast(list[object], config["tasks"]):
            task = _object(raw, "task")
            task_id = cast(TaskId, task["id"])
            task_definitions.append(
                RuntimeTaskDefinition(
                    task_id=task_id,
                    enabled=bool(task["enabled"]),
                    manual_command_allowed=bool(task["manual_command_allowed"]),
                    cadence=str(task["cadence"]),
                    trigger=str(task["trigger"]),
                    description=str(task["description"]),
                    latest_run=await self.repository.latest_by_task(task_id),
                )
            )
        annual = await self._annual_from_latest_complete_run()
        safety = _object(config["safety"], "safety")
        return RuntimeStatus(
            runtime_id=str(config["runtime_id"]),
            runtime_version=str(config["runtime_version"]),
            status=str(config["status"]),
            execution_mode=str(config["execution_mode"]),
            scheduler_enabled=bool(safety["scheduler_enabled"]),
            llm_api_enabled=bool(safety["llm_api_enabled"]),
            web_ai_chat_enabled=bool(safety["web_ai_chat_enabled"]),
            real_broker_enabled=bool(safety["real_broker_enabled"]),
            automatic_trading_enabled=bool(safety["automatic_trading_enabled"]),
            activation_gate=cast(dict[str, object], config["activation_gate"]),
            tasks=task_definitions,
            annual_pnl=annual,
            current_blockers=[
                f"{item.blocker_id}: {item.description}" for item in readiness.blockers
            ],
            checked_at=iso_now(),
        )

    async def _annual_from_latest_complete_run(self) -> list[AnnualPnl]:
        complete = [
            run
            for run in await self.backtest_repository.list_runs()
            if run.status == "COMPLETE"
            and run.purpose == "FORMAL_BACKTEST"
            and run.segment == "PRIMARY_TEN_YEAR"
            and run.performance_available
        ]
        if not complete:
            return []
        return calculate_annual_pnl(await self.backtest_repository.checkpoints(complete[0].run_id))

    async def run_manual(
        self, task_id: TaskId, run_id: str, as_of: str | None = None
    ) -> DeterministicTaskRun:
        if task_id not in TASK_IDS:
            raise ValueError(f"不支持的确定性任务: {task_id}")
        if not SAFE_RUN_ID.fullmatch(run_id):
            raise ValueError("run_id 只能使用字母、数字、下划线或连字符，且长度为 12–160")
        config = self._config()
        if config["status"] != "DISABLED" or config["execution_mode"] != "MANUAL_ONLY":
            raise RuntimeError(
                "当前仅允许默认 DISABLED/MANUAL_ONLY 架构；定时启用须另行获得用户授权"
            )
        if await self.repository.get(run_id):
            raise RuntimeError("run_id 已存在，禁止覆盖确定性运行证据")
        started_at = iso_now()
        started_tick = time.monotonic()
        run_as_of = _as_of(as_of)
        policy = load_policy(self.settings)
        ruleset_version, _ = load_ruleset_meta(self.settings)
        readiness = await self.backtests.readiness()
        coverage = readiness.dataset_coverage
        data_cutoff = min(
            (item.maximum_effective_date for item in coverage if item.maximum_effective_date),
            default=None,
        )
        input_hash = _canonical_hash(
            {
                "runtime_config": _file_hash(self.settings.deterministic_runtime_config_file),
                "backtest_policy": _file_hash(self.settings.backtest_policy_file),
                "paper_policy": _file_hash(self.settings.policy_file),
                "rules": _file_hash(self.settings.rule_file),
                "as_of": run_as_of,
                "task_id": task_id,
                "dataset_coverage": [item.model_dump(mode="json") for item in coverage],
            }
        )
        exceptions = self._exceptions(readiness, task_id)
        counts = await self._counts(task_id, coverage)
        annual = await self._annual_from_latest_complete_run()
        if task_id == "ANNUAL_PNL_SNAPSHOT":
            counts["annual_rows"] = len(annual)
            if not annual:
                exceptions.append(
                    DeterministicException(
                        code="ANNUAL_PNL_NO_FORMAL_RUN",
                        severity="BLOCKER",
                        message="没有可审计的正式主回测检查点，年度盈亏保持空值，未生成替代结果。",
                        evidence_refs=["state/backtests.sqlite3"],
                        suggested_human_action="在六项硬门和真实点时数据关闭后，手动运行正式主回测。",
                    )
                )
        status = (
            "COMPLETE"
            if task_id in {"DATA_UPDATE_VALIDATE", "EXCEPTION_EVIDENCE_PACKAGE"}
            else "BLOCKED"
        )
        if any(item.severity in {"BLOCKER", "CRITICAL"} for item in exceptions):
            status = "BLOCKED"
        summary = (
            "已完成确定性来源与覆盖校验；未下载、补造或修改历史数据。"
            if status == "COMPLETE"
            else "任务已 fail closed：未生成模拟成交、账本写入、收益或替代历史结果。"
        )
        package = {
            "schema_version": "1.0.0",
            "package_id": f"{run_id}-exceptions",
            "source_run_id": run_id,
            "created_at": iso_now(),
            "manual_ai_review_only": True,
            "automatic_writeback_allowed": False,
            "items": [item.model_dump(mode="json") for item in exceptions],
        }
        output = _object(config["output"], "output")
        exception_path = PROJECT_ROOT / str(output["exception_package_dir"]) / f"{run_id}.json"
        _validate_schema(package, "exception-evidence-package.schema.json")
        _write_json_once(exception_path, package)
        provisional = {
            "schema_version": "1.0.0",
            "run_id": run_id,
            "task_id": task_id,
            "execution_mode": "MANUAL_COMMAND",
            "status": status,
            "requested_at": started_at,
            "finished_at": iso_now(),
            "execution_duration_ms": int((time.monotonic() - started_tick) * 1000),
            "as_of": run_as_of,
            "data_cutoff": data_cutoff,
            "runtime_version": str(config["runtime_version"]),
            "ruleset_version": ruleset_version,
            "policy_version": policy.version,
            "input_hash": input_hash,
            "counts": counts,
            "blockers": [
                item.code for item in exceptions if item.severity in {"BLOCKER", "CRITICAL"}
            ],
            "exceptions": [item.model_dump(mode="json") for item in exceptions],
            "summary": summary,
            "artifact_hash": "",
            "artifact_path": None,
            "exception_package_path": str(exception_path.relative_to(PROJECT_ROOT)),
        }
        artifact_path = (
            PROJECT_ROOT / str(output["run_artifact_dir"]) / task_id.lower() / f"{run_id}.json"
        )
        provisional["artifact_path"] = str(artifact_path.relative_to(PROJECT_ROOT))
        provisional["artifact_hash"] = _canonical_hash(
            {key: value for key, value in provisional.items() if key != "artifact_hash"}
        )
        _validate_schema(provisional, "deterministic-task-run.schema.json")
        _write_json_once(artifact_path, provisional)
        task_run = DeterministicTaskRun(**provisional)
        await self.repository.save(task_run)
        return task_run

    @staticmethod
    def _exceptions(readiness: Any, task_id: TaskId) -> list[DeterministicException]:
        items: list[DeterministicException] = []
        report_only = task_id in {"DATA_UPDATE_VALIDATE", "EXCEPTION_EVIDENCE_PACKAGE"}
        for blocker in readiness.blockers:
            items.append(
                DeterministicException(
                    code=f"BACKTEST_{blocker.blocker_id}",
                    severity="WARNING" if report_only else "BLOCKER",
                    message=blocker.description,
                    evidence_refs=blocker.evidence,
                    suggested_human_action="补齐真实点时数据或冻结可审计规则；禁止使用模型或假数据补齐。",
                )
            )
        if task_id in {
            "DAILY_EXIT_CHECK",
            "MONTHLY_CANDIDATE_SCREEN",
            "NEXT_OPEN_PAPER_FILL",
            "CORPORATE_ACTIONS_LEDGER",
        }:
            items.append(
                DeterministicException(
                    code="FORMAL_RUNTIME_GATE_CLOSED",
                    severity="BLOCKER",
                    message="回测执行门尚未关闭；本次仅记录确定性阻断，不产生订单、成交或账本写入。",
                    evidence_refs=["config/dividend-hurdle-backtest-v4.yaml"],
                    suggested_human_action="关闭真实点时、利率、质量门和费用规则后，再手动运行对应任务。",
                )
            )
        return items

    async def _counts(self, task_id: TaskId, coverage: list[Any]) -> dict[str, int | float | None]:
        total_rows = sum(item.row_count for item in coverage)
        positions = await self.paper_repository.positions()
        screening = self.screening_repository.latest()
        factor_candidates = len(screening.factor_filter) if screening else 0
        return {
            "datasets_checked": len(coverage),
            "historical_rows_checked": total_rows,
            "stocks_processed": len(positions) if task_id == "DAILY_EXIT_CHECK" else 0,
            "candidates": factor_candidates if task_id == "MONTHLY_CANDIDATE_SCREEN" else 0,
            "buy_count": 0,
            "sell_count": 0,
            "corporate_actions_applied": 0,
            "ledger_updates": 0,
        }
