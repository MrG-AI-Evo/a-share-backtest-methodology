from __future__ import annotations

import argparse
import asyncio
import json

from app.backtests.historical import HistoricalDataRepository
from app.backtests.repository import BacktestRepository
from app.backtests.service import BacktestService
from app.core.settings import get_settings


async def _run(run_id: str) -> dict[str, object]:
    settings = get_settings()
    service = BacktestService(
        settings,
        BacktestRepository(settings.backtest_database),
        HistoricalDataRepository(
            settings.backtest_analytics_database,
            settings.backtest_parquet_dir,
        ),
    )
    await service.initialize()
    run = await service.preflight(run_id)
    return run.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="只执行正式回测就绪预检；若有阻塞则追加阻断运行，不生成绩效"
    )
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    payload = asyncio.run(_run(args.run_id))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
