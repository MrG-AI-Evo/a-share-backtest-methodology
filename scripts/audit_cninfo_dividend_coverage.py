#!/usr/bin/env python3
"""Manually collect a bounded CNInfo dividend evidence sample through AKShare.

The resulting JSON is immutable source evidence, not a formal backtest import.
It intentionally avoids any full-market loop, scheduler, account update, or
performance calculation.  A formal import still requires full-universe
coverage, statutory-document checks, and a point-in-time manifest.
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
SOURCE_ID = "CNINFO_DIVIDEND_VIA_AKSHARE"
DEFAULT_SYMBOLS = ("600000", "000001", "600519", "000002")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def iso_or_none(value: object) -> str | None:
    if pd.isna(value):
        return None
    return pd.Timestamp(value).date().isoformat()


def number_or_none(value: object) -> float | None:
    if pd.isna(value):
        return None
    return float(value)


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


def normalize(symbol: str, frame: pd.DataFrame, start: str, end: str, retrieved_at: str) -> list[dict[str, Any]]:
    required = {"实施方案公告日期", "分红类型", "派息比例", "股权登记日", "除权日", "派息日", "报告时间"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise RuntimeError(f"{symbol} CNInfo response missing columns: {', '.join(missing)}")
    records: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        ex_date = iso_or_none(row["除权日"])
        if ex_date is None or not (start <= ex_date <= end):
            continue
        cash_per_ten = number_or_none(row["派息比例"])
        records.append(
            {
                "symbol": symbol,
                "effective_date": ex_date,
                "known_at_candidate": iso_or_none(row["实施方案公告日期"]),
                "record_date": iso_or_none(row["股权登记日"]),
                "pay_date": iso_or_none(row["派息日"]),
                "cash_per_ten_shares_before_tax": cash_per_ten,
                "cash_per_share_before_tax_candidate": None if cash_per_ten is None else cash_per_ten / 10.0,
                "dividend_type": None if pd.isna(row["分红类型"]) else str(row["分红类型"]),
                "report_period_label": None if pd.isna(row["报告时间"]) else str(row["报告时间"]),
                "implementation_description": None if pd.isna(row.get("实施方案分红说明")) else str(row["实施方案分红说明"]),
                "source_id": SOURCE_ID,
                "source_uri": f"https://webapi.cninfo.com.cn/#/company?companyid={symbol}",
                "retrieved_at": retrieved_at,
                "formal_backtest_eligible": False,
            }
        )
    return records


def main() -> int:
    args = parse_args()
    if args.start_date > args.end_date:
        raise RuntimeError("start-date must not be after end-date")
    if any(not symbol.isdigit() or len(symbol) != 6 for symbol in args.symbols):
        raise RuntimeError("symbols must be six-digit A-share codes")
    output_dir = (args.output_dir or PROJECT_ROOT / "data" / "backtests" / "staging" / f"cninfo-dividend-{args.run_id}").resolve()
    raw_path = output_dir / "raw-responses.json"
    report_path = output_dir / "coverage-audit.json"
    audit_path = args.audit_dir.resolve() / f"{args.run_id}-cninfo-dividend-coverage-audit.json"
    ensure_new(output_dir, raw_path, report_path, audit_path)
    retrieved_at = utc_now()
    input_payload = {
        "source_id": SOURCE_ID,
        "symbols": args.symbols,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "script_sha256": sha256_file(Path(__file__).resolve()),
    }
    raw: dict[str, list[dict[str, Any]]] = {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []
    for symbol in args.symbols:
        try:
            frame = ak.stock_dividend_cninfo(symbol=symbol)
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
        "source": {
            "source_id": SOURCE_ID,
            "source_url": "https://webapi.cninfo.com.cn/",
            "adapter": "akshare.stock_dividend_cninfo",
            "retrieved_at": retrieved_at,
        },
        "input": {**input_payload, "input_sha256": sha256_bytes(json.dumps(input_payload, sort_keys=True).encode("utf-8"))},
        "summary": {
            "symbols_requested": len(args.symbols),
            "symbols_succeeded": len(normalized),
            "symbols_failed": len(errors),
            "raw_record_count": sum(len(records) for records in raw.values()),
            "primary_window_ex_date_record_count": sum(len(records) for records in normalized.values()),
            "records_with_known_at_candidate": sum(sum(record["known_at_candidate"] is not None for record in records) for records in normalized.values()),
            "errors": errors,
        },
        "samples": {symbol: records[:3] for symbol, records in normalized.items()},
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["POINT_IN_TIME_COVERAGE", "QUALITY_GATE", "CD_HISTORY", "FEE_TAX_RULES"],
            "reasons": [
                "This is a bounded source sample, not a full-market event coverage report.",
                "Implementation announcement dates are candidates for known_at and still need statutory-document verification.",
                "Corporate actions other than cash dividends and delisting settlement are not closed by this source sample.",
            ],
        },
        "artifacts": {"raw_responses": str(raw_path.relative_to(PROJECT_ROOT)), "raw_responses_sha256": sha256_file(raw_path)},
        "completed_at": utc_now(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(json.dumps({"schema_version": "1.0.0", "event_type": "BACKTEST_SOURCE_COVERAGE_AUDITED", "run_id": args.run_id, "status": report["status"], "source_id": SOURCE_ID, "input_sha256": report["input"]["input_sha256"], "report": str(report_path.relative_to(PROJECT_ROOT)), "created_at": report["completed_at"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
