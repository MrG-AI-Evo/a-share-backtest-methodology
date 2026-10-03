#!/usr/bin/env python3
"""Manually audit BaoStock historical coverage without importing backtest data.

This foreground-only tool saves an immutable JSON evidence package.  It never
writes a formal backtest dataset, changes a ledger, starts a scheduler, or
calculates performance.  BaoStock is a candidate coverage source, so its
responses remain non-formal until source terms and point-in-time evidence have
been independently checked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import baostock as bs


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "BAOSTOCK_PUBLIC_API"
DEFAULT_DATES = (
    "2016-01-04", "2017-01-03", "2018-01-02", "2019-01-02", "2020-01-02",
    "2021-01-04", "2022-01-04", "2023-01-03", "2024-01-02", "2025-01-02",
)
DEFAULT_SYMBOLS = ("sh.600000", "sz.000001", "sh.600519", "sz.000002")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def rows(response: Any) -> dict[str, Any]:
    """Materialize a BaoStock response while retaining response metadata."""
    values: list[dict[str, str]] = []
    field_names = [str(field) for field in getattr(response, "fields", [])]
    while response.error_code == "0" and response.next():
        values.append(dict(zip(field_names, (str(value) for value in response.get_row_data()), strict=True)))
    return {
        "error_code": str(response.error_code),
        "error_msg": str(response.error_msg),
        "fields": field_names,
        "rows": values,
    }


def is_shsz_common_stock(code: str) -> bool:
    return code.startswith(("sh.6", "sz.0", "sz.3"))


def compact_response(payload: dict[str, Any], *, sample_limit: int = 5) -> dict[str, Any]:
    data_rows = payload["rows"]
    return {
        "error_code": payload["error_code"],
        "error_msg": payload["error_msg"],
        "fields": payload["fields"],
        "row_count": len(data_rows),
        "response_sha256": sha256_bytes(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ),
        "sample_rows": data_rows[:sample_limit],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--dates", nargs="+", default=list(DEFAULT_DATES))
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--start-date", default="2016-01-04")
    parser.add_argument("--end-date", default="2025-12-31")
    parser.add_argument("--socket-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--audit-dir", type=Path, default=PROJECT_ROOT / "data" / "audit")
    return parser.parse_args()


def ensure_new(*paths: Path) -> None:
    collisions = [str(path) for path in paths if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable audit artifacts: " + ", ".join(collisions))


def main() -> int:
    args = parse_args()
    output_dir = (args.output_dir or PROJECT_ROOT / "data" / "backtests" / "staging" / f"baostock-coverage-{args.run_id}").resolve()
    report_path = output_dir / "coverage-audit.json"
    raw_path = output_dir / "raw-responses.json"
    audit_path = args.audit_dir.resolve() / f"{args.run_id}-baostock-coverage-audit.json"
    ensure_new(output_dir, report_path, raw_path, audit_path)
    if args.start_date > args.end_date:
        raise RuntimeError("start-date must not be after end-date")
    if args.socket_timeout_seconds <= 0:
        raise RuntimeError("socket-timeout-seconds must be positive")

    input_payload = {
        "source_id": SOURCE_ID,
        "dates": args.dates,
        "symbols": args.symbols,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "socket_timeout_seconds": args.socket_timeout_seconds,
        "script_sha256": sha256_file(Path(__file__).resolve()),
    }
    started_at = utc_now()
    socket.setdefaulttimeout(args.socket_timeout_seconds)
    login = bs.login()
    if login.error_code != "0":
        output_dir.mkdir(parents=True, exist_ok=False)
        args.audit_dir.resolve().mkdir(parents=True, exist_ok=True)
        report = {
            "schema_version": "1.0.0",
            "run_id": args.run_id,
            "workflow": "BACKTEST_SOURCE_COVERAGE_AUDIT",
            "status": "SOURCE_UNAVAILABLE_NOT_FORMAL",
            "scheduler_status": "DISABLED_MANUAL_ONLY",
            "source": {"source_id": SOURCE_ID, "source_url": "https://www.baostock.com/", "retrieved_at": started_at},
            "input": {**input_payload, "input_sha256": sha256_bytes(json.dumps(input_payload, sort_keys=True).encode("utf-8"))},
            "error": {"error_code": str(login.error_code), "error_msg": str(login.error_msg)},
            "formal_backtest_eligibility": {"eligible": False, "blockers_preserved": ["POINT_IN_TIME_COVERAGE"]},
            "started_at": started_at,
            "completed_at": utc_now(),
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        audit_path.write_text(json.dumps({"schema_version": "1.0.0", "event_type": "BACKTEST_SOURCE_UNAVAILABLE", "run_id": args.run_id, "status": report["status"], "error": report["error"], "report": str(report_path.relative_to(PROJECT_ROOT)), "created_at": report["completed_at"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    raw: dict[str, Any] = {"all_stock": {}, "daily_bars": {}, "dividends": {}, "profit": {}, "stock_basic": {}}
    try:
        for date in args.dates:
            payload = rows(bs.query_all_stock(date))
            raw["all_stock"][date] = payload
        raw["stock_basic"] = rows(bs.query_stock_basic())
        for symbol in args.symbols:
            raw["daily_bars"][symbol] = rows(
                bs.query_history_k_data_plus(
                    symbol,
                    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST",
                    start_date=args.start_date,
                    end_date=args.end_date,
                    frequency="d",
                    adjustflag="3",
                )
            )
            raw["dividends"][symbol] = {
                str(year): rows(bs.query_dividend_data(symbol, year=year, yearType="operate"))
                for year in range(int(args.start_date[:4]), int(args.end_date[:4]) + 1)
            }
            raw["profit"][symbol] = {
                f"{year}Q4": rows(bs.query_profit_data(symbol, year=year, quarter=4))
                for year in range(int(args.start_date[:4]), int(args.end_date[:4]) + 1)
            }
    finally:
        bs.logout()

    output_dir.mkdir(parents=True, exist_ok=False)
    args.audit_dir.resolve().mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    stock_basic_rows = raw["stock_basic"]["rows"]
    report = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_SOURCE_COVERAGE_AUDIT",
        "status": "CANDIDATE_COVERAGE_AUDIT_NOT_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "source": {
            "source_id": SOURCE_ID,
            "source_url": "https://www.baostock.com/",
            "package_version": getattr(bs, "__version__", "UNKNOWN"),
            "retrieved_at": started_at,
        },
        "input": {**input_payload, "input_sha256": sha256_bytes(json.dumps(input_payload, sort_keys=True).encode("utf-8"))},
        "observed_coverage": {
            "universe_snapshots": {
                date: {
                    **compact_response(payload),
                    "shsz_common_stock_candidate_count": sum(
                        is_shsz_common_stock(row.get("code", "")) for row in payload["rows"]
                    ),
                }
                for date, payload in raw["all_stock"].items()
            },
            "security_master": {
                **compact_response(raw["stock_basic"]),
                "listed_stock_rows": sum(row.get("status") == "1" and row.get("type") == "1" for row in stock_basic_rows),
                "delisted_stock_rows": sum(row.get("status") == "0" and row.get("type") == "1" for row in stock_basic_rows),
            },
            "sample_daily_bars": {symbol: compact_response(payload) for symbol, payload in raw["daily_bars"].items()},
            "sample_dividends": {
                symbol: {year: compact_response(payload) for year, payload in by_year.items()}
                for symbol, by_year in raw["dividends"].items()
            },
            "sample_profit": {
                symbol: {period: compact_response(payload) for period, payload in by_period.items()}
                for symbol, by_period in raw["profit"].items()
            },
        },
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["CD_HISTORY", "QUALITY_GATE", "FEE_TAX_RULES", "POINT_IN_TIME_COVERAGE"],
            "reasons": [
                "This audit samples source availability; it is not a complete historical import.",
                "API retrieval timestamp does not itself prove the first-publication known_at of financial facts or corporate actions.",
                "ST, dividend, delisting and corporate-action observations require documented cross-checks against statutory disclosures.",
                "No four-bank certificate-of-deposit history or date-effective fee/tax rules are created by this audit.",
            ],
        },
        "artifacts": {"raw_responses": str(raw_path.relative_to(PROJECT_ROOT)), "raw_responses_sha256": sha256_file(raw_path)},
        "started_at": started_at,
        "completed_at": utc_now(),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "event_type": "BACKTEST_SOURCE_COVERAGE_AUDITED",
                "run_id": args.run_id,
                "status": report["status"],
                "source_id": SOURCE_ID,
                "input_sha256": report["input"]["input_sha256"],
                "report": str(report_path.relative_to(PROJECT_ROOT)),
                "created_at": report["completed_at"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
