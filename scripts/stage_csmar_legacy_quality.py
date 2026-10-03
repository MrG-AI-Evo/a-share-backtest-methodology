#!/usr/bin/env python3
"""Stage a fixed public CSMAR-legacy mirror subset for SH/SZ audit.

This is a manual-only, deterministic staging task.  It does not schedule work,
run a backtest, call an LLM, or promote the mirror into the formal point-in-time
lake.  The upstream repository currently publishes no license tag and the
tables do not provide a trustworthy original-publication timestamp, so every
row is explicitly marked non-eligible for formal decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_ID = "Zion-HF/csmar-legacy"
DATASET_REVISION = "70d8d7b4de81323363e100c520c4d029872ed037"
PRIMARY_START = "2016-01-04"
PRIMARY_END = "2025-12-31"

INPUTS = {
    "companies": Path("股票市场系列/股票市场交易/基本数据/公司文件/公司文件.csv"),
    "annual": Path("公司研究系列/上市公司基本信息/上市公司基本信息年度表/上市公司基本信息年度表.csv"),
    "disclosed": Path("公司研究系列/财务指标分析/披露财务指标/披露财务指标.csv"),
    "dividend": Path("公司研究系列/财务指标分析/股利分配/股利分配.csv"),
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def quote_path(path: Path) -> str:
    return str(path).replace("'", "''")


def main() -> int:
    args = parse_args()
    raw_root = args.raw_root.resolve()
    output_dir = args.output_dir.resolve()
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-csmar-legacy-quality-stage.json"
    collisions = [path for path in (output_dir, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))

    input_paths = {name: raw_root / relative for name, relative in INPUTS.items()}
    invalid = [path for path in input_paths.values() if not path.is_file() or path.stat().st_size == 0]
    if invalid:
        raise RuntimeError("missing or empty inputs: " + ", ".join(map(str, invalid)))

    output_dir.mkdir(parents=True, exist_ok=False)
    con = duckdb.connect()
    for name, path in input_paths.items():
        con.execute(
            f"CREATE TEMP VIEW raw_{name} AS SELECT * FROM read_csv_auto('{quote_path(path)}', "
            "header=true, all_varchar=true, ignore_errors=false)"
        )

    con.execute(
        """
        CREATE TEMP TABLE company_map AS
        SELECT
          "证券代码" AS provider_symbol,
          CASE
            WHEN TRY_CAST("市场类型" AS INTEGER) IN (1, 32) THEN "证券代码" || '.SH'
            WHEN TRY_CAST("市场类型" AS INTEGER) IN (4, 16) THEN "证券代码" || '.SZ'
          END AS symbol,
          *
        FROM raw_companies
        WHERE TRY_CAST("市场类型" AS INTEGER) IN (1, 4, 16, 32)
        """
    )

    common_suffix = f""",
      '{DATASET_ID}' AS dataset_id,
      '{DATASET_REVISION}' AS dataset_revision,
      NULL::TIMESTAMP AS known_at,
      'UNKNOWN_NO_ORIGINAL_PUBLICATION_TIMESTAMP' AS point_in_time_status,
      FALSE AS formal_backtest_eligible
    """
    con.execute(
        f"""
        CREATE TEMP TABLE annual_stage AS
        SELECT m.symbol, a.* {common_suffix}
        FROM raw_annual a
        INNER JOIN company_map m ON m.provider_symbol = a."股票代码"
        WHERE TRY_CAST(a."统计截止日期" AS DATE)
              BETWEEN DATE '2016-01-01' AND DATE '{PRIMARY_END}'
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE disclosed_stage AS
        SELECT m.symbol, d.* {common_suffix}
        FROM raw_disclosed d
        INNER JOIN company_map m ON m.provider_symbol = d."股票代码"
        WHERE TRY_CAST(d."统计截止日期" AS DATE)
              BETWEEN DATE '{PRIMARY_START}' AND DATE '{PRIMARY_END}'
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE dividend_stage AS
        SELECT m.symbol, d.* {common_suffix}
        FROM raw_dividend d
        INNER JOIN company_map m ON m.provider_symbol = d."股票代码"
        WHERE TRY_CAST(d."统计截止日期" AS DATE)
              BETWEEN DATE '{PRIMARY_START}' AND DATE '{PRIMARY_END}'
        """
    )
    con.execute(
        f"""
        CREATE TEMP TABLE companies_stage AS
        SELECT * EXCLUDE (provider_symbol, symbol), symbol, provider_symbol {common_suffix}
        FROM company_map
        """
    )

    outputs: dict[str, Path] = {
        "companies": output_dir / "shsz-companies-current-candidate.parquet",
        "annual": output_dir / "shsz-company-annual-candidate.parquet",
        "disclosed": output_dir / "shsz-disclosed-financial-indicators-candidate.parquet",
        "dividend": output_dir / "shsz-dividend-indicators-candidate.parquet",
    }
    for name, path in outputs.items():
        con.execute(
            f"COPY {name}_stage TO '{quote_path(path)}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )

    def stats(table: str, date_field: str | None) -> dict[str, object]:
        date_parts = (
            f", min(TRY_CAST(\"{date_field}\" AS DATE)), max(TRY_CAST(\"{date_field}\" AS DATE))"
            if date_field
            else ""
        )
        values = con.execute(
            f"SELECT count(*), count(DISTINCT symbol){date_parts} FROM {table}_stage"
        ).fetchone()
        result: dict[str, object] = {"rows": values[0], "symbols": values[1]}
        if date_field:
            result["date_min"] = None if values[2] is None else values[2].isoformat()
            result["date_max"] = None if values[3] is None else values[3].isoformat()
        result["exchange_rows"] = {
            row[0]: row[1]
            for row in con.execute(
                f"SELECT right(symbol, 2), count(*) FROM {table}_stage GROUP BY 1 ORDER BY 1"
            ).fetchall()
        }
        return result

    coverage = {
        "companies": stats("companies", None),
        "annual": stats("annual", "统计截止日期"),
        "disclosed": stats("disclosed", "统计截止日期"),
        "dividend": stats("dividend", "统计截止日期"),
    }
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_CSMAR_LEGACY_QUALITY_STAGE",
        "status": "STAGING_ONLY_NOT_FORMAL_BACKTEST_INPUT",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "dataset": {
            "id": DATASET_ID,
            "revision": DATASET_REVISION,
            "license_status": "UNKNOWN_NO_DATASET_LICENSE_TAG",
        },
        "inputs": [
            {
                "name": name,
                "path": str(path.relative_to(PROJECT_ROOT)),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for name, path in input_paths.items()
        ],
        "outputs": [
            {
                "name": name,
                "path": str(path.relative_to(PROJECT_ROOT)),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
                **coverage[name],
            }
            for name, path in outputs.items()
        ],
        "coverage": coverage,
        "limitations": [
            "The public mirror has no dataset license tag; outputs remain staging-only.",
            "The indicator tables do not carry the original first-publication timestamp.",
            "Financial values may reflect later corrections or current snapshots.",
            "Company activity status and ownership fields are current/static unless the annual table proves a dated value.",
            "The annual company table currently ends at 2024-12-31 and cannot prove 2025 year-end state.",
            "Joining a separate disclosure date does not prove that a mirrored value is the originally disclosed value.",
        ],
        "blockers_preserved": ["QUALITY_GATE", "POINT_IN_TIME_COVERAGE"],
        "created_at": now(),
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "schema_version": "1.0.0",
        "event_type": "BACKTEST_CSMAR_LEGACY_QUALITY_STAGED",
        "run_id": args.run_id,
        "status": receipt["status"],
        "receipt": str(receipt_path.relative_to(PROJECT_ROOT)),
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "created_at": receipt["created_at"],
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": args.run_id, "coverage": coverage}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
