from pathlib import Path

import pytest

from app.backtests.repository import BacktestRepository
from app.backtests.runner import DeterministicHistoryRunner
from tests.test_backtest_runner import day, security


@pytest.mark.asyncio
async def test_completed_run_persists_full_snapshots_and_validates_hash_chain(
    tmp_path: Path,
) -> None:
    run_id = "20260827-formal-fixture-repository"
    runner = DeterministicHistoryRunner(
        run_id,
        "frozen-policy-test",
        run_purpose="FORMAL_BACKTEST",
        raw_data_mode="REAL_POINT_IN_TIME",
        synthetic_raw_observation_count=0,
    )
    result = runner.run(
        [
            day("2020-01-02", security(), month_start=True),
            day("2020-01-03", security()),
        ]
    )
    repository = BacktestRepository(tmp_path / "backtests.sqlite3")
    await repository.initialize()
    saved = await repository.save_completed_run(
        run_id=run_id,
        policy_version="frozen-policy-test",
        scenario="BASE",
        requested_at="2026-08-27T11:00:00+08:00",
        started_at="2026-08-27T11:00:01+08:00",
        finished_at="2026-08-27T11:00:02+08:00",
        data_cutoff="2020-01-03",
        events=result.ledger.events,
        checkpoints=result.checkpoints,
    )
    assert saved.performance_available is True
    assert await repository.audit_chain_valid(run_id) is True
    checkpoints = await repository.checkpoints(run_id)
    assert checkpoints[-1].security_ledgers[0]["symbol"] == "601288"
    assert checkpoints[-1].account_cash_flows[0]["flow_type"] == "CONTRIBUTION"


@pytest.mark.asyncio
async def test_formal_persistence_rejects_fixture_data(tmp_path: Path) -> None:
    runner = DeterministicHistoryRunner("fixture-rejected", "policy")
    result = runner.run(
        [day("2020-01-02", security(), month_start=True), day("2020-01-03", security())]
    )
    repository = BacktestRepository(tmp_path / "backtests.sqlite3")
    await repository.initialize()
    with pytest.raises(ValueError, match="真实点时"):
        await repository.save_completed_run(
            run_id="fixture-rejected",
            policy_version="policy",
            scenario="BASE",
            requested_at="2026-08-27T11:00:00+08:00",
            started_at="2026-08-27T11:00:01+08:00",
            finished_at="2026-08-27T11:00:02+08:00",
            data_cutoff="2020-01-03",
            events=result.ledger.events,
            checkpoints=result.checkpoints,
        )
