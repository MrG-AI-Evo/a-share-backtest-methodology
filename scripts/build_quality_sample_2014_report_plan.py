#!/usr/bin/env python3
"""Create an immutable CNInfo download plan for 2014 reports of the pilot sample."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "data/backtests/staging/20260911-040500-cninfo-by-symbol-full/annual-report-announcements.parquet"
POLICY = ROOT / "config/dividend-hurdle-quality-sample-11-v2.yaml"
PRICE = ROOT / "data/backtests/staging/nas-daily-20260910-155000-nas-daily-staging/shsz-daily-raw-bars-staging-v1.parquet"


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--policy", type=Path, default=POLICY)
    args = parser.parse_args()
    output = ROOT / "data/backtests/staging" / args.run_id
    audit = ROOT / "data/audit" / f"{args.run_id}-cninfo-2014-report-plan.json"
    if output.exists() or audit.exists():
        raise RuntimeError("immutable output already exists")
    policy_path = args.policy.resolve()
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    symbols = [str(item["symbol"]) for item in policy["scope"]["symbols"]]
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT symbol, issuer_name, announcement_id, known_at_conservative,
               title, pdf_url, adjunct_size_kib_candidate, response_sha256
        FROM read_parquet('{INDEX}')
        WHERE symbol IN (SELECT * FROM UNNEST(?))
          AND regexp_matches(title, '2014年年度报告')
          AND NOT regexp_matches(title, '摘要|英文|取消')
          AND pdf_url IS NOT NULL
        ORDER BY symbol, known_at_conservative, announcement_id
        """,
        [symbols],
    ).fetchall()
    listed_at_start = {
        str(row[0])
        for row in con.execute(
            f"SELECT symbol FROM read_parquet('{PRICE}') GROUP BY symbol HAVING min(effective_date) <= DATE '2016-01-04'"
        ).fetchall()
    }
    expected = sorted(set(symbols) & listed_at_start)
    delivered = sorted({str(row[0]) for row in rows})
    announcement_ids = [str(row[2]) for row in rows]
    if delivered != expected or len(announcement_ids) != len(set(announcement_ids)):
        raise RuntimeError(f"2014 annual-report plan mismatch: expected={expected} delivered={delivered} rows={len(rows)}")
    output.mkdir(parents=True, exist_ok=False)
    plan = output / "cninfo-2014-annual-report-download-plan.jsonl"
    with plan.open("x", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({
                "symbol": row[0], "issuer_name": row[1], "announcement_id": row[2],
                "known_at": row[3].isoformat(), "title": row[4], "pdf_url": row[5],
                "size_kib_candidate": row[6], "index_response_sha256": row[7],
            }, ensure_ascii=False) + "\n")
    created_at = datetime.now(UTC).isoformat()
    receipt = {
        "schema_version": "1.0.0", "run_id": args.run_id,
        "workflow": "BACKTEST_QUALITY_SAMPLE_2014_CNINFO_PLAN",
        "status": "DOWNLOAD_PLAN_READY_NOT_FORMAL_INPUT",
        "scheduler_status": "DISABLED_MANUAL_ONLY", "created_at": created_at,
        "summary": {"planned": len(rows), "symbols": delivered, "not_applicable_pre_listing": ["600919", "601298"]},
        "inputs": [
            {"path": str(INDEX.relative_to(ROOT)), "sha256": sha256(INDEX)},
            {"path": str(policy_path.relative_to(ROOT)), "sha256": sha256(policy_path)},
        ],
        "output": {"path": str(plan.relative_to(ROOT)), "sha256": sha256(plan)},
    }
    receipt_path = output / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text(json.dumps({
        "schema_version": "1.0.0", "event_type": "BACKTEST_QUALITY_SAMPLE_2014_CNINFO_PLAN_CREATED",
        "run_id": args.run_id, "status": receipt["status"], "scheduler_status": "DISABLED_MANUAL_ONLY",
        "receipt": {"path": str(receipt_path.relative_to(ROOT)), "sha256": sha256(receipt_path)}, "created_at": created_at,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
