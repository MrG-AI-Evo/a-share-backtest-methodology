from __future__ import annotations

import asyncio
from typing import Any, cast

from app.backtests.frames import HistoricalFrameBuilder
from app.backtests.historical import HistoricalDataRepository
from app.backtests.models import (
    BacktestBlocker,
    BacktestReadiness,
    BacktestRunDetail,
    BacktestRunSummary,
    BacktestWindow,
)
from app.backtests.policy import excluded_symbols, load_backtest_policy
from app.backtests.repository import BacktestRepository
from app.backtests.runner import DeterministicHistoryRunner
from app.core.settings import Settings
from app.core.time import iso_now


def _dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("回测策略节点必须为对象")
    return cast(dict[str, Any], value)


class BacktestService:
    def __init__(
        self,
        settings: Settings,
        repository: BacktestRepository,
        historical: HistoricalDataRepository,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.historical = historical

    async def initialize(self) -> None:
        await self.repository.initialize()
        await self.historical.initialize()

    async def readiness(self) -> BacktestReadiness:
        policy = load_backtest_policy(self.settings)
        windows = _dict(policy["simulation_windows"])
        primary = _dict(windows["primary"])
        extension = _dict(windows["extension"])
        coverage = await self.historical.coverage(
            str(primary["start_date"]), str(primary["end_date"])
        )
        blockers = [
            BacktestBlocker(
                blocker_id=str(item["id"]),
                category=cast(Any, item["category"]),
                status=cast(Any, item["status"]),
                description=str(item["description"]),
                evidence=[],
            )
            for raw in cast(list[object], policy["execution_blockers"])
            for item in [_dict(raw)]
        ]
        for item in coverage:
            if not item.ready_for_primary_window:
                blockers.append(
                    BacktestBlocker(
                        blocker_id=f"DATASET_{item.dataset.upper()}",
                        category="DATA_COVERAGE",
                        status="OPEN",
                        description=(
                            f"{item.dataset} 尚未覆盖主回测窗口，或包含非点时/合成原始观察。"
                        ),
                        evidence=[
                            f"rows={item.row_count}",
                            f"range={item.minimum_effective_date}..{item.maximum_effective_date}",
                            f"point_in_time={item.point_in_time_row_count}",
                            f"synthetic={item.synthetic_row_count}",
                        ],
                    )
                )
        open_blockers = [item for item in blockers if item.status != "CLOSED"]
        local_open = [
            item
            for item in open_blockers
            if item.category in {"LOCAL_ENGINEERING", "USER_DECISION"}
        ]
        return BacktestReadiness(
            policy_id=str(policy["policy_id"]),
            policy_version=str(policy["policy_version"]),
            policy_status=str(policy["status"]),
            base_policy_sha256_verified=True,
            primary_window=BacktestWindow(
                label=str(primary["label"]),
                start_date=str(primary["start_date"]),
                end_date=str(primary["end_date"]),
                included_in_primary_performance=bool(primary["include_in_primary_performance"]),
            ),
            extension_window=BacktestWindow(
                label=str(extension["label"]),
                start_date=str(extension["start_date"]),
                end_date=str(extension["configured_data_cutoff"]),
                included_in_primary_performance=bool(extension["include_in_primary_performance"]),
            ),
            formal_backtest_executable=(
                policy["status"] == "FROZEN_EXECUTABLE" and not open_blockers
            ),
            engineering_ready=not local_open,
            blockers=open_blockers,
            dataset_coverage=coverage,
            safety={
                "real_broker_connected": False,
                "real_orders_enabled": False,
                "llm_api_enabled": False,
                "synthetic_raw_data_allowed_in_formal_run": False,
                "primary_and_extension_results_separated": True,
            },
            checked_at=iso_now(),
        )

    async def list_runs(self) -> list[BacktestRunSummary]:
        return await self.repository.list_runs()

    async def detail(
        self,
        run_id: str,
        *,
        event_offset: int = 0,
        event_limit: int = 100,
        checkpoint_offset: int = 0,
        checkpoint_limit: int = 100,
    ) -> BacktestRunDetail | None:
        run = await self.repository.get_run(run_id)
        if run is None:
            return None
        policy = load_backtest_policy(self.settings)
        event_count = await self.repository.event_count(run_id)
        checkpoint_count = await self.repository.checkpoint_count(run_id)
        return BacktestRunDetail(
            run=run,
            blockers=await self.repository.blockers(run_id),
            checkpoints=await self.repository.checkpoints_page(
                run_id, offset=checkpoint_offset, limit=checkpoint_limit
            ),
            checkpoint_count=checkpoint_count,
            latest_checkpoint=await self.repository.latest_checkpoint(run_id),
            events=await self.repository.events_page(
                run_id, offset=event_offset, limit=event_limit
            ),
            event_count=event_count,
            audit_chain_valid=await self.repository.audit_chain_valid(run_id),
            policy_snapshot=cast(dict[str, object], policy),
        )

    async def preflight(self, run_id: str) -> BacktestRunSummary:
        existing = await self.repository.get_run(run_id)
        if existing is not None:
            return existing
        readiness = await self.readiness()
        if readiness.formal_backtest_executable:
            raise RuntimeError("当前策略已满足正式运行门；请使用正式执行器而不是阻断预检")
        return await self.repository.create_blocked_run(
            run_id=run_id,
            policy_version=readiness.policy_version,
            requested_at=readiness.checked_at,
            blockers=readiness.blockers,
            summary=(
                "正式十年回测未启动：硬阻塞或真实点时数据覆盖未关闭；未生成收益、净值或替代结果。"
            ),
        )

    async def execute_formal(self, run_id: str, scenario: str = "BASE") -> BacktestRunSummary:
        existing = await self.repository.get_run(run_id)
        if existing is not None:
            return existing
        readiness = await self.readiness()
        if not readiness.formal_backtest_executable:
            return await self.repository.create_blocked_run(
                run_id=run_id,
                policy_version=readiness.policy_version,
                requested_at=readiness.checked_at,
                blockers=readiness.blockers,
                summary=(
                    "正式十年回测未启动：执行门或真实点时数据覆盖未关闭；"
                    "未生成收益、净值或替代结果。"
                ),
            )
        if scenario not in {"OPTIMISTIC", "BASE", "STRESS"}:
            raise ValueError("不支持的回测情景")
        started_at = iso_now()
        policy = load_backtest_policy(self.settings)
        exclusions = excluded_symbols(policy)
        frames = await asyncio.to_thread(
            HistoricalFrameBuilder(self.settings.backtest_analytics_database).build,
            readiness.primary_window.start_date,
            readiness.primary_window.end_date,
            exclusions,
        )
        if (
            frames.minimum_trade_date != readiness.primary_window.start_date
            or frames.maximum_trade_date != readiness.primary_window.end_date
        ):
            raise RuntimeError("历史输入帧未精确覆盖主回测首尾交易日")
        runner = DeterministicHistoryRunner(
            run_id,
            readiness.policy_version,
            run_purpose="FORMAL_BACKTEST",
            scenario=cast(Any, scenario),
            raw_data_mode="REAL_POINT_IN_TIME",
            synthetic_raw_observation_count=0,
        )
        result = await asyncio.to_thread(runner.run, frames.days)
        if not result.ledger.audit_chain_ok():
            raise RuntimeError("回测审计事件哈希链失败")
        finished_at = iso_now()
        return await self.repository.save_completed_run(
            run_id=run_id,
            policy_version=readiness.policy_version,
            scenario=scenario,
            requested_at=readiness.checked_at,
            started_at=started_at,
            finished_at=finished_at,
            data_cutoff=frames.maximum_trade_date,
            events=result.ledger.events,
            checkpoints=result.checkpoints,
        )
