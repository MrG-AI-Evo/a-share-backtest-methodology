from __future__ import annotations

import argparse
import json
from uuid import uuid4

from app.core.settings import get_settings
from app.core.time import iso_now
from app.models import ScreeningInvalidation
from app.repositories.screening import ScreeningRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="追加筛选 run 失效记录；不删除或覆盖原产物")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--replacement-run-id", default=None)
    args = parser.parse_args()
    invalidated_at = iso_now()
    item = ScreeningInvalidation(
        invalidation_id=(
            f"{invalidated_at[:19].replace(':', '').replace('-', '')}-"
            f"screening-invalidation-{uuid4().hex[:8]}"
        ),
        run_id=args.run_id,
        invalidated_at=invalidated_at,
        reason=args.reason,
        replacement_run_id=args.replacement_run_id,
    )
    ScreeningRepository(get_settings()).invalidate(item)
    print(json.dumps(item.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
