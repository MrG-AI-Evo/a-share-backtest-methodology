from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.core.settings import get_settings
from app.repositories.screening import ScreeningRepository
from app.repositories.universe import UniverseRepository
from app.services.screening import ScreeningService
from app.services.universe_collection import UniverseCollectionService


def main() -> None:
    parser = argparse.ArgumentParser(description="采集真实三交易所 A 股快照并可执行 300→30")
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--history-limit", type=int, default=160)
    parser.add_argument("--max-history-requests", type=int, default=520)
    parser.add_argument("--screen", action="store_true")
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of)
    settings = get_settings()
    snapshot = UniverseCollectionService(settings).collect(
        as_of,
        args.run_id,
        history_limit=args.history_limit,
        max_history_requests=args.max_history_requests,
    )
    persisted = UniverseRepository(settings).append(snapshot)
    screening_run_id: str | None = None
    if args.screen:
        if not persisted.coverage.formal_screening_eligible:
            raise SystemExit("全市场覆盖或可筛选数量未通过硬门，拒绝生成正式 300→30")
        screening_run_id = args.run_id.replace("universe", "screening")
        run = ScreeningService(settings).run(persisted.rows, as_of, screening_run_id)
        ScreeningRepository(settings).append(run)
    print(
        json.dumps(
            {
                "run_id": persisted.run_id,
                "artifact_path": persisted.artifact_path,
                "as_of": persisted.as_of,
                "coverage": persisted.coverage.model_dump(mode="json"),
                "warnings": persisted.warnings,
                "screening_run_id": screening_run_id,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
