#!/usr/bin/env python3
"""Normalize and deduplicate a completed CNInfo per-symbol index capture.

This deterministic, manual-only task retains the immutable raw capture and
creates a derived logical index keyed by (symbol, announcement_id).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_dir = args.source_dir.resolve()
    source_parquet = source_dir / "annual-report-announcements.parquet"
    source_receipt = source_dir / "receipt.json"
    output_dir = args.output_dir.resolve()
    output_path = output_dir / "annual-report-announcements-deduplicated.parquet"
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-cninfo-annual-index-normalized.json"
    invalid = [path for path in (source_parquet, source_receipt) if not path.is_file() or path.stat().st_size == 0]
    if invalid:
        raise RuntimeError("missing or empty inputs: " + ", ".join(map(str, invalid)))
    collisions = [path for path in (output_dir, output_path, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))

    source = str(source_parquet).replace("'", "''")
    target = str(output_path).replace("'", "''")
    con = duckdb.connect()
    before = con.execute(
        f"SELECT count(*), count(DISTINCT symbol || ':' || announcement_id) FROM read_parquet('{source}')"
    ).fetchone()
    output_dir.mkdir(parents=True, exist_ok=False)
    con.execute(
        f"""
        COPY (
          SELECT * EXCLUDE (rn)
          FROM (
            SELECT *, row_number() OVER (
              PARTITION BY symbol, announcement_id
              ORDER BY retrieved_at, response_sha256
            ) AS rn
            FROM read_parquet('{source}')
          ) WHERE rn = 1
        ) TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    values = con.execute(
        f"""
        SELECT count(*), count(DISTINCT symbol || ':' || announcement_id),
               count(DISTINCT symbol), min(substr(CAST(known_at_conservative AS VARCHAR), 1, 10)),
               max(substr(CAST(known_at_conservative AS VARCHAR), 1, 10)),
               count(*) FILTER (WHERE title LIKE '%摘要%'),
               count(*) FILTER (WHERE title LIKE '%取消%'),
               count(*) FILTER (WHERE title LIKE '%英文%'),
               count(*) FILTER (WHERE title NOT LIKE '%摘要%' AND title NOT LIKE '%取消%' AND title NOT LIKE '%英文%' AND adjunct_type='PDF')
        FROM read_parquet('{target}')
        """
    ).fetchone()
    names = ("rows", "unique_keys", "symbols", "known_at_min", "known_at_max", "summary_rows", "cancelled_rows", "english_rows", "full_report_candidates")
    profile = dict(zip(names, values, strict=True))
    if profile["rows"] != profile["unique_keys"]:
        raise RuntimeError("logical key deduplication failed")

    source_receipt_data = json.loads(source_receipt.read_text(encoding="utf-8"))
    request_failures = [item for item in source_receipt_data["summary"]["failures"] if "symbol" in item]
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_OFFICIAL_DISCLOSURE_INDEX_NORMALIZATION",
        "status": "STAGING_COVERAGE_COMPLETE_NOT_FORMAL" if not request_failures else "PARTIAL_STAGING_SOURCE_NOT_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "input": {
            "source_receipt": str(source_receipt.relative_to(PROJECT_ROOT)),
            "source_receipt_sha256": sha256_file(source_receipt),
            "source_parquet": str(source_parquet.relative_to(PROJECT_ROOT)),
            "source_parquet_sha256": sha256_file(source_parquet),
            "script_sha256": sha256_file(Path(__file__).resolve()),
        },
        "deduplication": {
            "logical_key": ["symbol", "announcement_id"],
            "rows_before": before[0],
            "unique_keys_before": before[1],
            "duplicate_rows_removed": before[0] - before[1],
            "selection": "earliest retrieved_at then response_sha256",
            "raw_capture_retained": True,
        },
        "profile": profile,
        "request_failures": request_failures,
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["QUALITY_GATE", "POINT_IN_TIME_COVERAGE"],
            "reasons": [
                "This is an official announcement index, not the filing-body revision archive.",
                "Numerical observations and amendment events still need a frozen deterministic join policy.",
            ],
        },
        "output": {
            "path": str(output_path.relative_to(PROJECT_ROOT)),
            "bytes": output_path.stat().st_size,
            "sha256": sha256_file(output_path),
        },
        "created_at": now(),
    }
    input_sha = hashlib.sha256(json.dumps(receipt["input"], sort_keys=True).encode()).hexdigest()
    receipt["input"]["input_sha256"] = input_sha
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "schema_version": "1.0.0",
        "event_type": "backtest-cninfo-annual-index-normalized",
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
