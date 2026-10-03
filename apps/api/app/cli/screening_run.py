from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.settings import get_settings
from app.models import UniverseSecurity
from app.repositories.screening import ScreeningRepository
from app.services.screening import ScreeningService


def _load_rows(path: Path) -> list[UniverseSecurity]:
    if path.suffix.lower() == ".json":
        payload: Any = json.loads(path.read_text("utf-8"))
        if isinstance(payload, dict):
            coverage = payload.get("coverage")
            if (
                not isinstance(coverage, dict)
                or coverage.get("formal_screening_eligible") is not True
            ):
                raise RuntimeError("全市场快照未通过正式筛选覆盖硬门")
            payload = payload.get("rows")
        if not isinstance(payload, list):
            raise RuntimeError("筛选输入 JSON 必须是数组或通过硬门的全市场快照")
        return [UniverseSecurity.model_validate(item) for item in payload]
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [UniverseSecurity.model_validate(dict(row)) for row in csv.DictReader(handle)]
    raise RuntimeError("筛选输入仅支持 .json 或 .csv")


def main() -> None:
    parser = argparse.ArgumentParser(description="运行确定性 A 股 300→30 筛选")
    parser.add_argument("input", type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of)
    rows = _load_rows(args.input)
    settings = get_settings()
    run = ScreeningService(settings).run(rows, as_of, args.run_id)
    persisted = ScreeningRepository(settings).append(run)
    print(json.dumps(persisted.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
