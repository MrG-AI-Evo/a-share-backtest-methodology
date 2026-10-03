from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.backtests.historical import HistoricalDataRepository
from app.core.settings import get_settings


async def _run(manifest: Path) -> dict[str, object]:
    settings = get_settings()
    repository = HistoricalDataRepository(
        settings.backtest_analytics_database,
        settings.backtest_parquet_dir,
    )
    await repository.initialize()
    return await repository.import_manifest(manifest)


def main() -> None:
    parser = argparse.ArgumentParser(description="导入经哈希与点时字段校验的回测 Parquet")
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args.manifest)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
