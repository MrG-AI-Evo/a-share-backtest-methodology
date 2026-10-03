#!/usr/bin/env python3
"""Audit financial quality-field coverage and official filing-date matches.

This manual deterministic task joins the staged EastMoney F10 current-view
indicator capture to the official CNInfo annual-report index. It does not
promote data, set quality thresholds, schedule work, or run a backtest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FINANCIAL = PROJECT_ROOT / "data/backtests/staging/20260911-030000-eastmoney-mainfinadata-full/mainfinadata-current-view.parquet"
DEFAULT_FINANCIAL_RECEIPT = DEFAULT_FINANCIAL.parent / "receipt.json"
DEFAULT_CNINFO = PROJECT_ROOT / "data/backtests/staging/20260911-041500-cninfo-annual-normalized/annual-report-announcements-deduplicated.parquet"
DEFAULT_CNINFO_RECEIPT = DEFAULT_CNINFO.parent / "receipt.json"
PRIMARY_END = "2025-12-31"


FIELDS_BY_TYPE = {
    "通用": ["ROEJQ", "PARENTNETPROFIT", "KCFJCXSYJLR", "NETCASH_OPERATE_PK", "ZCFZL"],
    "银行": ["PARENTNETPROFIT", "NEWCAPITALADER", "HXYJBCZL", "NONPERLOAN", "BLDKBBL"],
    "保险": ["PARENTNETPROFIT", "SOLVENCY_AR"],
    "证券": ["PARENTNETPROFIT", "JZB", "JZC", "JZBJZC", "RISK_COVERAGE", "CAPITAL_LEVERAGE_RATIO", "NET_CAPITAL_LIABILITIES"],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quote(path: Path) -> str:
    return str(path).replace("'", "''")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--financial", type=Path, default=DEFAULT_FINANCIAL)
    parser.add_argument("--financial-receipt", type=Path, default=DEFAULT_FINANCIAL_RECEIPT)
    parser.add_argument("--cninfo", type=Path, default=DEFAULT_CNINFO)
    parser.add_argument("--cninfo-receipt", type=Path, default=DEFAULT_CNINFO_RECEIPT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    inputs = [args.financial.resolve(), args.financial_receipt.resolve(), args.cninfo.resolve(), args.cninfo_receipt.resolve()]
    invalid = [path for path in inputs if not path.is_file() or path.stat().st_size == 0]
    if invalid:
        raise RuntimeError("missing or empty inputs: " + ", ".join(map(str, invalid)))
    output_dir = args.output_dir.resolve()
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-financial-quality-coverage.json"
    collisions = [path for path in (output_dir, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))

    con = duckdb.connect()
    financial = f"read_parquet('{quote(inputs[0])}')"
    cninfo = f"read_parquet('{quote(inputs[2])}')"
    con.execute(
        f"""
        CREATE TEMP VIEW annual_financial AS
        SELECT *
        FROM {financial}
        WHERE REPORT_TYPE = '年报'
          AND TRY_CAST(REPORT_DATE AS DATE) BETWEEN DATE '2015-01-01' AND DATE '2024-12-31'
        """
    )
    con.execute(
        f"""
        CREATE TEMP VIEW full_annual_filings AS
        SELECT DISTINCT symbol, CAST(to_timestamp(announcement_time_raw_ms / 1000.0) AS DATE) AS announcement_date
        FROM {cninfo}
        WHERE title NOT LIKE '%摘要%' AND title NOT LIKE '%取消%' AND title NOT LIKE '%英文%'
          AND adjunct_type = 'PDF'
        """
    )
    overall_values = con.execute(
        f"""
        SELECT count(*) AS rows,
               count(DISTINCT SECUCODE) AS symbols,
               count(DISTINCT SECUCODE || ':' || CAST(REPORT_DATE AS VARCHAR)) AS symbol_periods,
               count(*) FILTER (WHERE _known_at_conservative IS NULL) AS missing_conservative_known_at,
               count(*) FILTER (WHERE TRY_CAST(UPDATE_DATE AS TIMESTAMP) > TRY_CAST(NOTICE_DATE AS TIMESTAMP)) AS updated_after_notice,
               count(*) FILTER (WHERE TRY_CAST(_known_at_conservative AS TIMESTAMP) <= TIMESTAMP '{PRIMARY_END} 23:59:59') AS conservatively_known_by_primary_end,
               count(*) FILTER (
                 WHERE EXISTS (
                   SELECT 1 FROM full_annual_filings c
                   WHERE c.symbol = annual_financial.SECURITY_CODE
                     AND c.announcement_date = TRY_CAST(annual_financial.NOTICE_DATE AS DATE)
                 )
               ) AS exact_cninfo_notice_date_matches
        FROM annual_financial
        """
    ).fetchone()
    overall_names = (
        "rows", "symbols", "symbol_periods", "missing_conservative_known_at", "updated_after_notice",
        "conservatively_known_by_primary_end", "exact_cninfo_notice_date_matches",
    )
    overall = dict(zip(overall_names, overall_values, strict=True))
    overall["exact_cninfo_notice_date_match_rate"] = (
        overall["exact_cninfo_notice_date_matches"] / overall["rows"] if overall["rows"] else None
    )

    available_columns = {row[0] for row in con.execute("DESCRIBE SELECT * FROM annual_financial").fetchall()}
    coverage: dict[str, dict[str, object]] = {}
    for org_type, fields in FIELDS_BY_TYPE.items():
        selected = [field for field in fields if field in available_columns]
        expressions = ", ".join(
            f"count(*) FILTER (WHERE {field} IS NOT NULL) AS {field}" for field in selected
        )
        values = con.execute(
            f"""
            SELECT count(*) AS rows, count(DISTINCT SECUCODE) AS symbols{', ' if expressions else ''}{expressions}
            FROM annual_financial WHERE ORG_TYPE = ?
            """,
            [org_type],
        ).fetchone()
        names = ["rows", "symbols", *selected]
        record = dict(zip(names, values, strict=True))
        rows = int(record["rows"])
        record["field_coverage_rates"] = {
            field: (record[field] / rows if rows else None) for field in selected
        }
        coverage[org_type] = record

    year_values = con.execute(
        """
        SELECT year(TRY_CAST(REPORT_DATE AS DATE)) AS report_year,
               count(*) AS rows,
               count(DISTINCT SECUCODE) AS symbols,
               count(*) FILTER (WHERE TRY_CAST(_known_at_conservative AS TIMESTAMP) <= TIMESTAMP '2025-12-31 23:59:59') AS conservatively_known_by_end,
               count(*) FILTER (
                 WHERE EXISTS (
                   SELECT 1 FROM full_annual_filings c
                   WHERE c.symbol = annual_financial.SECURITY_CODE
                     AND c.announcement_date = TRY_CAST(annual_financial.NOTICE_DATE AS DATE)
                 )
               ) AS exact_cninfo_matches
        FROM annual_financial GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    by_year = [
        {"report_year": row[0], "rows": row[1], "symbols": row[2], "conservatively_known_by_primary_end": row[3], "exact_cninfo_notice_date_matches": row[4]}
        for row in year_values
    ]

    input_record = {
        "files": [
            {"path": str(path.relative_to(PROJECT_ROOT)), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
            for path in inputs
        ],
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "candidate_known_at": "max(NOTICE_DATE, UPDATE_DATE)",
        "official_match": "SECURITY_CODE + NOTICE_DATE == CNInfo full annual report announcement date",
    }
    input_sha = hashlib.sha256(json.dumps(input_record, sort_keys=True).encode()).hexdigest()
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_FINANCIAL_QUALITY_COVERAGE_AUDIT",
        "status": "STAGING_CANDIDATE_COVERAGE_AUDITED_NOT_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "input": {**input_record, "input_sha256": input_sha},
        "overall": overall,
        "industry_profile_coverage": coverage,
        "by_report_year": by_year,
        "interpretation": [
            "The public F10 capture supplies general, bank, insurer and broker indicator fields across the requested universe.",
            "The exact CNInfo match measures whether the F10 original NOTICE_DATE coincides with an official full annual-report announcement date.",
            "Using max(NOTICE_DATE, UPDATE_DATE) is conservative against future leakage, but does not reconstruct values before the latest visible update.",
        ],
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["QUALITY_GATE", "POINT_IN_TIME_COVERAGE"],
            "reasons": [
                "Numeric threshold values and effective dates are not frozen.",
                "The current-view endpoint does not expose all historical revision bodies.",
                "Rows without conservative known_at or official filing-date match must fail closed.",
                "CNInfo filing bodies, audit opinions, going-concern flags and correction lineage remain to be extracted or separately evidenced.",
            ],
        },
        "created_at": now(),
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "schema_version": "1.0.0",
        "event_type": "backtest-financial-quality-coverage-audited",
        "run_id": args.run_id,
        "status": receipt["status"],
        "scheduler_status": receipt["scheduler_status"],
        "input_sha256": input_sha,
        "artifact": {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(receipt_path)},
        "blockers_preserved": receipt["formal_backtest_eligibility"]["blockers_preserved"],
        "created_at": receipt["created_at"],
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
