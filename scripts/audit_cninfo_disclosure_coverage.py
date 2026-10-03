#!/usr/bin/env python3
"""Manually collect a bounded CNInfo annual-report announcement evidence sample.

This is an evidence-only, foreground command.  It does not download a market
history, calculate a financial ratio, change a quality threshold, write the
formal lake, or run a backtest.  Its only purpose is to prove whether the
public CNInfo announcement index carries a publication date that can later be
used as a point-in-time ``known_at`` input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "CNINFO_DISCLOSURE_INDEX_VIA_AKSHARE"
DEFAULT_SYMBOLS = ("600000", "000001", "600519", "000002")


def now() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_digest(path: Path) -> str:
    return digest(path.read_bytes())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--start-date", default="2016-01-04")
    parser.add_argument("--end-date", default="2025-12-31")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--audit-dir", type=Path, default=PROJECT_ROOT / "data" / "audit")
    return parser.parse_args()


def ensure_new(*paths: Path) -> None:
    collisions = [str(path) for path in paths if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable audit artifacts: " + ", ".join(collisions))


def as_date(value: object) -> str | None:
    if pd.isna(value):
        return None
    return pd.Timestamp(value).date().isoformat()


def normalize(symbol: str, frame: pd.DataFrame, start: str, end: str, retrieved_at: str) -> list[dict[str, Any]]:
    expected = {"代码", "简称", "公告标题", "公告时间", "公告链接"}
    missing = sorted(expected - set(frame.columns))
    if missing:
        raise RuntimeError(f"{symbol} CNInfo response missing columns: {', '.join(missing)}")
    records: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        published = as_date(row["公告时间"])
        if published is None or not (start <= published <= end):
            continue
        records.append(
            {
                "symbol": symbol,
                "issuer_name": None if pd.isna(row["简称"]) else str(row["简称"]),
                "report_title": None if pd.isna(row["公告标题"]) else str(row["公告标题"]),
                "known_at_candidate": published,
                "announcement_uri": None if pd.isna(row["公告链接"]) else str(row["公告链接"]),
                "source_id": SOURCE_ID,
                "source_uri": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
                "retrieved_at": retrieved_at,
                "formal_backtest_eligible": False,
            }
        )
    return records


def main() -> int:
    args = parse_args()
    start = args.start_date.replace("-", "")
    end = args.end_date.replace("-", "")
    if args.start_date > args.end_date:
        raise RuntimeError("start-date must not be after end-date")
    if any(not item.isdigit() or len(item) != 6 for item in args.symbols):
        raise RuntimeError("symbols must be six-digit A-share codes")

    output_dir = (args.output_dir or PROJECT_ROOT / "data" / "backtests" / "staging" / f"cninfo-disclosure-{args.run_id}").resolve()
    raw_path = output_dir / "raw-responses.json"
    report_path = output_dir / "coverage-audit.json"
    audit_path = args.audit_dir.resolve() / f"{args.run_id}-cninfo-disclosure-coverage-audit.json"
    ensure_new(output_dir, raw_path, report_path, audit_path)

    retrieved_at = now()
    input_payload = {
        "source_id": SOURCE_ID,
        "symbols": args.symbols,
        "category": "年报",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "script_sha256": file_digest(Path(__file__).resolve()),
    }
    raw: dict[str, list[dict[str, Any]]] = {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    for symbol in args.symbols:
        try:
            frame = ak.stock_zh_a_disclosure_report_cninfo(
                symbol=symbol, market="沪深京", category="年报", start_date=start, end_date=end
            )
            raw[symbol] = json.loads(frame.to_json(orient="records", force_ascii=False, date_format="iso"))
            normalized[symbol] = normalize(symbol, frame, args.start_date, args.end_date, retrieved_at)
        except Exception as error:
            errors.append({"symbol": symbol, "error": f"{type(error).__name__}: {error}"})

    output_dir.mkdir(parents=True, exist_ok=False)
    args.audit_dir.resolve().mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_SOURCE_COVERAGE_AUDIT",
        "status": "CANDIDATE_SOURCE_SUCCESS_NOT_FORMAL" if not errors else "PARTIAL_SOURCE_COVERAGE_NOT_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "source": {"source_id": SOURCE_ID, "source_url": "https://www.cninfo.com.cn/", "adapter": "akshare.stock_zh_a_disclosure_report_cninfo", "retrieved_at": retrieved_at},
        "input": {**input_payload, "input_sha256": digest(json.dumps(input_payload, sort_keys=True).encode("utf-8"))},
        "summary": {"symbols_requested": len(args.symbols), "symbols_succeeded": len(normalized), "symbols_failed": len(errors), "raw_record_count": sum(len(records) for records in raw.values()), "annual_report_announcement_count": sum(len(records) for records in normalized.values()), "records_with_known_at_candidate": sum(sum(record["known_at_candidate"] is not None for record in records) for records in normalized.values()), "errors": errors},
        "samples": {symbol: records[:4] for symbol, records in normalized.items()},
        "formal_backtest_eligibility": {"eligible": False, "blockers_preserved": ["QUALITY_GATE", "POINT_IN_TIME_COVERAGE"], "reasons": ["Bounded publication-index sample only; no full-universe coverage.", "A published annual-report index does not itself provide the numerical financial facts, audit opinion, or frozen cross-industry quality thresholds.", "The linked statutory filing and any amendments must be preserved and reconciled before formal use."]},
        "artifacts": {"raw_responses": str(raw_path.relative_to(PROJECT_ROOT)), "raw_responses_sha256": file_digest(raw_path)},
        "completed_at": now(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(json.dumps({"schema_version": "1.0.0", "event_type": "BACKTEST_DISCLOSURE_INDEX_AUDITED", "run_id": args.run_id, "status": report["status"], "source_id": SOURCE_ID, "input_sha256": report["input"]["input_sha256"], "report": str(report_path.relative_to(PROJECT_ROOT)), "created_at": report["completed_at"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
