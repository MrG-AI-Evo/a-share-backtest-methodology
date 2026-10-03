from pathlib import Path

import pytest

from app.backtests.historical import HistoricalDataRepository
from app.backtests.repository import BacktestRepository
from app.backtests.service import BacktestService
from app.core.settings import PROJECT_ROOT, Settings


@pytest.mark.asyncio
async def test_readiness_and_preflight_block_without_generating_performance(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        backtest_policy_file=PROJECT_ROOT / "config" / "dividend-hurdle-backtest-v2.yaml",
        backtest_parquet_dir=tmp_path / "parquet",
    )
    repository = BacktestRepository(settings.backtest_database)
    service = BacktestService(
        settings,
        repository,
        HistoricalDataRepository(
            settings.backtest_analytics_database, settings.backtest_parquet_dir
        ),
    )
    await service.initialize()
    readiness = await service.readiness()
    assert readiness.base_policy_sha256_verified is True
    assert readiness.primary_window.start_date == "2016-01-04"
    assert readiness.primary_window.end_date == "2025-12-31"
    assert readiness.extension_window.included_in_primary_performance is False
    assert readiness.formal_backtest_executable is False
    assert any(item.blocker_id == "CD_HISTORY" for item in readiness.blockers)
    assert all(item.row_count == 0 for item in readiness.dataset_coverage)

    run = await service.preflight("20260827-100422-backtest-preflight-test")
    assert run.status == "BLOCKED"
    assert run.performance_available is False
    assert run.blocker_count >= 6
    detail = await service.detail(run.run_id)
    assert detail is not None
    assert detail.checkpoints == []
    assert detail.events[0].event_type == "FORMAL_BACKTEST_BLOCKED"
    assert detail.events[0].payload["performance_generated"] is False
