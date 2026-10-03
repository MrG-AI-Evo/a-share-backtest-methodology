#!/usr/bin/env python3
"""Profile a fixed public financial-statement snapshot without promoting it.

This manual-only task records immutable hashes and coverage evidence for the
five Parquet tables downloaded from ``langwnwk/stock_finance``.  It does not
schedule work, call a model, mutate the backtest lake, or make the data eligible
for formal point-in-time decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_ID = "langwnwk/stock_finance"
DATASET_REVISION = "3f7846735138bb2761d1b643c91f7265c031ac51"
PRIMARY_START = "2016-01-04"
PRIMARY_END = "2025-12-31"
FILES = (
    "balance_report.parquet",
    "profit_report.parquet",
    "cash_report.parquet",
    "profit_quarter.parquet",
    "cash_quarter.parquet",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def quote_path(path: Path) -> str:
    return str(path).replace("'", "''")


def json_value(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[union-attr]
    return value


def profile(con: duckdb.DuckDBPyConnection, path: Path) -> dict[str, object]:
    source = f"read_parquet('{quote_path(path)}')"
    columns = [row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()]
    required = {"SECUCODE", "REPORT_DATE", "REPORT_TYPE", "NOTICE_DATE", "UPDATE_DATE"}
    missing = sorted(required.difference(columns))
    if missing:
        raise RuntimeError(f"{path.name} missing required columns: {', '.join(missing)}")

    values = con.execute(
        f"""
        SELECT
          count(*) AS rows,
          count(DISTINCT SECUCODE) AS symbols,
          min(TRY_CAST(REPORT_DATE AS DATE)) AS report_min,
          max(TRY_CAST(REPORT_DATE AS DATE)) AS report_max,
          min(TRY_CAST(NOTICE_DATE AS TIMESTAMP)) AS notice_min,
          max(TRY_CAST(NOTICE_DATE AS TIMESTAMP)) AS notice_max,
          count(*) FILTER (WHERE NOTICE_DATE IS NULL) AS notice_null,
          count(*) FILTER (WHERE UPDATE_DATE IS NULL) AS update_null,
          count(*) FILTER (
            WHERE TRY_CAST(REPORT_DATE AS DATE)
              BETWEEN DATE '{PRIMARY_START}' AND DATE '{PRIMARY_END}'
          ) AS primary_rows,
          count(DISTINCT SECUCODE) FILTER (
            WHERE TRY_CAST(REPORT_DATE AS DATE)
              BETWEEN DATE '{PRIMARY_START}' AND DATE '{PRIMARY_END}'
          ) AS primary_symbols,
          count(*) FILTER (
            WHERE TRY_CAST(NOTICE_DATE AS TIMESTAMP) < TRY_CAST(REPORT_DATE AS TIMESTAMP)
          ) AS notice_before_period_end,
          count(*) FILTER (
            WHERE TRY_CAST(UPDATE_DATE AS TIMESTAMP) > TRY_CAST(NOTICE_DATE AS TIMESTAMP)
          ) AS update_after_notice
        FROM {source}
        WHERE right(SECUCODE, 3) IN ('.SH', '.SZ')
        """
    ).fetchone()
    names = (
        "rows",
        "symbols",
        "report_min",
        "report_max",
        "notice_min",
        "notice_max",
        "notice_null",
        "update_null",
        "primary_rows",
        "primary_symbols",
        "notice_before_period_end",
        "update_after_notice",
    )
    result = {name: json_value(value) for name, value in zip(names, values, strict=True)}
    result["exchange_rows"] = {
        row[0]: row[1]
        for row in con.execute(
            f"""
            SELECT right(SECUCODE, 2), count(*)
            FROM {source}
            WHERE right(SECUCODE, 3) IN ('.SH', '.SZ')
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
    }
    duplicate = con.execute(
        f"""
        SELECT coalesce(sum(row_count - 1), 0), count(*) FILTER (WHERE row_count > 1)
        FROM (
          SELECT SECUCODE, REPORT_DATE, REPORT_TYPE, count(*) AS row_count
          FROM {source}
          WHERE right(SECUCODE, 3) IN ('.SH', '.SZ')
          GROUP BY 1, 2, 3
        )
        """
    ).fetchone()
    result["duplicate_rows_beyond_symbol_period_type"] = duplicate[0]
    result["duplicate_symbol_period_type_keys"] = duplicate[1]
    result["columns"] = len(columns)
    result["date_columns"] = [
        name for name in columns if name in {"REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE"}
    ]
    return result


def main() -> int:
    args = parse_args()
    raw_root = args.raw_root.resolve()
    output_dir = args.output_dir.resolve()
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-stock-finance-profile.json"
    collisions = [path for path in (output_dir, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))

    paths = [raw_root / name for name in FILES]
    invalid = [path for path in paths if not path.is_file() or path.stat().st_size == 0]
    if invalid:
        raise RuntimeError("missing or empty inputs: " + ", ".join(map(str, invalid)))

    con = duckdb.connect()
    profiles = {path.name: profile(con, path) for path in paths}
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_STOCK_FINANCE_PROFILE",
        "status": "STAGING_ONLY_NOT_FORMAL_BACKTEST_INPUT",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "dataset": {
            "id": DATASET_ID,
            "revision": DATASET_REVISION,
            "license_status": "UNKNOWN_NO_DATASET_LICENSE_TAG",
        },
        "primary_window": {"start": PRIMARY_START, "end": PRIMARY_END},
        "inputs": [
            {
                "path": str(path.relative_to(PROJECT_ROOT)),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                "profile": profiles[path.name],
            }
            for path in paths
        ],
        "limitations": [
            "The public dataset card names no originating provider or collection method.",
            "The repository exposes no dataset license tag; formal use is not authorized.",
            "NOTICE_DATE and UPDATE_DATE exist, but the card does not define their semantics.",
            "The snapshot does not prove preservation of every originally published value or later restatement lineage.",
            "Rows remain staging-only until source provenance, licensing, and point-in-time semantics are verified.",
        ],
        "blockers_preserved": ["QUALITY_GATE", "POINT_IN_TIME_COVERAGE"],
        "created_at": now(),
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = {
        "schema_version": "1.0.0",
        "event_type": "backtest-stock-finance-profile",
        "run_id": args.run_id,
        "status": receipt["status"],
        "scheduler_status": receipt["scheduler_status"],
        "artifact": {
            "path": str(receipt_path.relative_to(PROJECT_ROOT)),
            "sha256": sha256_file(receipt_path),
        },
        "blockers_preserved": receipt["blockers_preserved"],
        "created_at": now(),
    }
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
