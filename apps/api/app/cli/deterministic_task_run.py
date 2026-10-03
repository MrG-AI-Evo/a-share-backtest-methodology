from __future__ import annotations

import argparse
import asyncio
import json

from app.backtests.historical import HistoricalDataRepository
from app.backtests.repository import BacktestRepository
from app.backtests.service import BacktestService
from app.core.settings import get_settings
from app.repositories.paper import PaperRepository
from app.repositories.screening import ScreeningRepository
from app.runtime.models import TaskId
from app.runtime.repository import DeterministicRuntimeRepository
from app.runtime.service import TASK_IDS, DeterministicRuntimeService


async def _run(task_id: TaskId, run_id: str, as_of: str | None) -> dict[str, object]:
    settings = get_settings()
    backtest_repository = BacktestRepository(settings.backtest_database)
    backtests = BacktestService(
        settings,
        backtest_repository,
        HistoricalDataRepository(
            settings.backtest_analytics_database, settings.backtest_parquet_dir
        ),
    )
    paper = PaperRepository(settings)
    await paper.initialize()
    await backtests.initialize()
    service = DeterministicRuntimeService(
        settings,
        DeterministicRuntimeRepository(settings.deterministic_runtime_database),
        backtests,
        backtest_repository,
        paper,
        ScreeningRepository(settings),
    )
    await service.initialize()
    return (await service.run_manual(task_id, run_id, as_of)).model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="手动运行一项确定性本地任务；不启用定时器、不调用LLM、不连接券商。"
    )
    parser.add_argument("--task", choices=TASK_IDS, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--as-of", default=None, help="带时区ISO时间；省略时使用本地当前时间")
    args = parser.parse_args()
    result = asyncio.run(_run(args.task, args.run_id, args.as_of))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result["status"] != "COMPLETE":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
