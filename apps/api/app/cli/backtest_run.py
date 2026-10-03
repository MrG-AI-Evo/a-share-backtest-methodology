from __future__ import annotations

import argparse
import asyncio
import json

from app.backtests.historical import HistoricalDataRepository
from app.backtests.repository import BacktestRepository
from app.backtests.service import BacktestService
from app.core.settings import get_settings


async def _run(run_id: str, scenario: str) -> dict[str, object]:
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
    run = await service.execute_formal(run_id, scenario)
    return run.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="执行正式十年回测；任一策略或数据门未关闭时仅保存阻断记录"
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", choices=("OPTIMISTIC", "BASE", "STRESS"), default="BASE")
    args = parser.parse_args()
    payload = asyncio.run(_run(args.run_id, args.scenario))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
