#!/usr/bin/env python3
"""Profile fixed CSMAR-legacy statement CSVs as non-PIT staging evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_ID = "Zion-HF/csmar-legacy"
DATASET_REVISION = "70d8d7b4de81323363e100c520c4d029872ed037"
PRIMARY_START = "2016-01-04"
PRIMARY_END = "2025-12-31"
COMPANY_FILE = Path("股票市场系列/股票市场交易/基本数据/公司文件/公司文件.csv")
STATEMENT_FILES = {
    "balance": Path("公司研究系列/财务报表/资产负债表/资产负债表.csv"),
    "income": Path("公司研究系列/财务报表/利润表/利润表.csv"),
    "cashflow_direct": Path("公司研究系列/财务报表/现金流量表(直接法)/现金流量表(直接法).csv"),
}


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


def main() -> int:
    args = parse_args()
    raw_root = args.raw_root.resolve()
    output_dir = args.output_dir.resolve()
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-csmar-statements-profile.json"
    collisions = [path for path in (output_dir, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))

    company_path = raw_root / COMPANY_FILE
    statement_paths = {name: raw_root / relative for name, relative in STATEMENT_FILES.items()}
    all_paths = [company_path, *statement_paths.values()]
    invalid = [path for path in all_paths if not path.is_file() or path.stat().st_size == 0]
    if invalid:
        raise RuntimeError("missing or empty inputs: " + ", ".join(map(str, invalid)))

    con = duckdb.connect()
    con.execute(
        f"""
        CREATE TEMP TABLE company_map AS
        SELECT
          "证券代码" AS provider_symbol,
          CASE
            WHEN TRY_CAST("市场类型" AS INTEGER) IN (1, 32) THEN "证券代码" || '.SH'
            WHEN TRY_CAST("市场类型" AS INTEGER) IN (4, 16) THEN "证券代码" || '.SZ'
          END AS symbol
        FROM read_csv_auto('{quote_path(company_path)}', header=true, all_varchar=true)
        WHERE TRY_CAST("市场类型" AS INTEGER) IN (1, 4, 16, 32)
        """
    )

    profiles: dict[str, dict[str, object]] = {}
    for name, path in statement_paths.items():
        con.execute(
            f"""
            CREATE OR REPLACE TEMP VIEW statement AS
            SELECT m.symbol, s.*
            FROM read_csv_auto('{quote_path(path)}', header=true, all_varchar=true) s
            INNER JOIN company_map m ON m.provider_symbol = s."证券代码"
            """
        )
        values = con.execute(
            f"""
            SELECT
              count(*),
              count(DISTINCT symbol),
              min(TRY_CAST("统计截止日期" AS DATE)),
              max(TRY_CAST("统计截止日期" AS DATE)),
              count(*) FILTER (
                WHERE TRY_CAST("统计截止日期" AS DATE)
                  BETWEEN DATE '{PRIMARY_START}' AND DATE '{PRIMARY_END}'
              ),
              count(DISTINCT symbol) FILTER (
                WHERE TRY_CAST("统计截止日期" AS DATE)
                  BETWEEN DATE '{PRIMARY_START}' AND DATE '{PRIMARY_END}'
              ),
              count(*) FILTER (WHERE TRY_CAST("是否发生差错更正" AS INTEGER) = 1),
              count(*) FILTER (WHERE "差错更正披露日期" IS NOT NULL),
              count(DISTINCT "报表类型")
            FROM statement
            """
        ).fetchone()
        names = (
            "rows",
            "symbols",
            "report_min",
            "report_max",
            "primary_rows",
            "primary_symbols",
            "correction_flag_rows",
            "correction_date_rows",
            "report_types",
        )
        profile = {key: json_value(value) for key, value in zip(names, values, strict=True)}
        duplicate = con.execute(
            """
            SELECT coalesce(sum(row_count - 1), 0), count(*) FILTER (WHERE row_count > 1)
            FROM (
              SELECT symbol, "统计截止日期", "报表类型", count(*) AS row_count
              FROM statement
              GROUP BY 1, 2, 3
            )
            """
        ).fetchone()
        profile["duplicate_rows_beyond_symbol_period_type"] = duplicate[0]
        profile["duplicate_symbol_period_type_keys"] = duplicate[1]
        profiles[name] = profile

    output_dir.mkdir(parents=True, exist_ok=False)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_CSMAR_STATEMENTS_PROFILE",
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
                "name": "company_map",
                "path": str(company_path.relative_to(PROJECT_ROOT)),
                "bytes": company_path.stat().st_size,
                "sha256": sha256_file(company_path),
            },
            *[
                {
                    "name": name,
                    "path": str(path.relative_to(PROJECT_ROOT)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "profile": profiles[name],
                }
                for name, path in statement_paths.items()
            ],
        ],
        "limitations": [
            "The public mirror exposes no dataset license tag; the files remain staging-only.",
            "The tables contain correction flags and correction disclosure dates only.",
            "The tables do not contain the original first-publication date for ordinary reports.",
            "The snapshot does not preserve a complete original-value/restatement lineage.",
            "An external disclosure date may index a report but cannot prove that these values were the values then disclosed.",
        ],
        "blockers_preserved": ["QUALITY_GATE", "POINT_IN_TIME_COVERAGE"],
        "created_at": now(),
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = {
        "schema_version": "1.0.0",
        "event_type": "backtest-csmar-statements-profile",
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
