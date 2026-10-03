#!/usr/bin/env python3
"""Normalize the public ifindpy-legacy disclosure calendar as candidate evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


TZ = ZoneInfo("Asia/Shanghai")
REVISION = "be38dbd6797b75a7ff955bd497d39d363dad037d"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report-start", default="2014-01-01")
    parser.add_argument("--known-at-end", default="2025-12-31")
    args = parser.parse_args()
    started_at = datetime.now(TZ)
    root = args.root.resolve()
    source = root / "raw/基础数据/股票/新准则财务报表/股票_新准则财务报表基本信息.csv"
    frame = pd.read_csv(source, dtype=str)
    output = pd.DataFrame(
        {
            "symbol": frame["thscode"],
            "security_name": frame["股票简称"],
            "report_period": pd.to_datetime(frame["报告期"], errors="coerce").dt.date,
            "actual_disclosure_date": pd.to_datetime(
                frame["定期报告实际披露日期"], format="%Y%m%d", errors="coerce"
            ).dt.date,
            "expected_disclosure_date": pd.to_datetime(
                frame["定期报告预计披露日期"], format="%Y%m%d", errors="coerce"
            ).dt.date,
            "statement_format": frame["报表格式"],
        }
    )
    report_start = pd.Timestamp(args.report_start).date()
    known_at_end = pd.Timestamp(args.known_at_end).date()
    output = output[
        output["symbol"].str.endswith((".SH", ".SZ"), na=False)
        & output["report_period"].ge(report_start)
        & output["actual_disclosure_date"].le(known_at_end)
    ].copy()
    output["known_at"] = output["actual_disclosure_date"]
    output["source_id"] = "HF_ZION_IFINDPY_LEGACY_DISCLOSURE_CALENDAR"
    output["source_revision"] = REVISION
    output["retrieved_at"] = started_at.isoformat()
    output["license_status"] = "NO_REPOSITORY_LICENSE_DECLARED"
    output["formal_backtest_eligible"] = False
    output["is_synthetic"] = False
    output.sort_values(["actual_disclosure_date", "symbol", "report_period"], inplace=True)
    duplicate_rows = int(output.duplicated(["symbol", "report_period", "actual_disclosure_date"]).sum())
    output.drop_duplicates(inplace=True)

    output_path = root / "shsz-periodic-report-disclosures-candidate.parquet"
    output.to_parquet(output_path, index=False)
    raw_files = sorted(path for path in (root / "raw").rglob("*.csv"))
    completed_at = datetime.now(TZ)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": "20260911-004800-ifindpy-disclosure-staging",
        "workflow": "BACKTEST_SOURCE_EVIDENCE_COLLECTION",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "input_hash": hashlib.sha256(
            json.dumps(
                {"revision": REVISION, "report_start": args.report_start, "known_at_end": args.known_at_end},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "source": {
            "repository": "https://huggingface.co/datasets/Zion-HF/ifindpy-legacy",
            "revision": REVISION,
            "license_status": "NO_REPOSITORY_LICENSE_DECLARED",
        },
        "raw_records": [
            {
                "path": str(path.relative_to(Path.cwd())),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in raw_files
        ],
        "coverage": {
            "source_rows": len(frame),
            "candidate_rows": len(output),
            "unique_symbols": int(output["symbol"].nunique()),
            "minimum_report_period": str(output["report_period"].min()),
            "maximum_report_period": str(output["report_period"].max()),
            "minimum_known_at": str(output["known_at"].min()),
            "maximum_known_at": str(output["known_at"].max()),
            "duplicate_rows_removed": duplicate_rows,
            "statement_formats": {
                (str(key) if pd.notna(key) else "null"): int(value)
                for key, value in output["statement_format"].value_counts(dropna=False).items()
            },
        },
        "output": {
            "path": str(output_path.relative_to(Path.cwd())),
            "bytes": output_path.stat().st_size,
            "sha256": sha256_file(output_path),
        },
        "formal_assessment": {
            "status": "STAGING_ONLY_CROSSCHECK",
            "useful_for": "Cross-checking periodic-report actual disclosure dates against first-party announcement indexes.",
            "not_sufficient_for": [
                "financial statement values as originally published",
                "revision history",
                "formal use before redistribution/license review",
            ],
            "formal_gate_closed": False,
        },
    }
    (root / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt["coverage"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
