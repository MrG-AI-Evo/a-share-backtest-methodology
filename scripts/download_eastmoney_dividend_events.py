#!/usr/bin/env python3
"""Manually stage Eastmoney dividend/corporate-action rows with raw evidence.

This collector never writes to the formal backtest lake.  The returned amount is
the provider's latest view of a plan, so rows remain candidate evidence until a
revision-history/PIT audit proves the value was known on PLAN_NOTICE_DATE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests


API_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
TZ = ZoneInfo("Asia/Shanghai")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_json(session: requests.Session, params: dict[str, str], retries: int) -> dict[str, Any]:
    error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = session.get(API_URL, params=params, timeout=45)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") == 9201 and payload.get("message") == "返回数据为空":
                return {
                    "success": True,
                    "code": 0,
                    "message": "normalized provider empty result",
                    "provider_empty_code": 9201,
                    "result": {"pages": 0, "count": 0, "data": []},
                }
            if payload.get("success") is not True or payload.get("code") != 0:
                raise RuntimeError(f"provider error: code={payload.get('code')} message={payload.get('message')}")
            return payload
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"request failed after {retries + 1} attempts: {error}")


def quarter_ends(start_year: int, end_year: int) -> list[str]:
    suffixes = ("03-31", "06-30", "09-30", "12-31")
    return [f"{year}-{suffix}" for year in range(start_year, end_year + 1) for suffix in suffixes]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-report-year", type=int, default=2014)
    parser.add_argument("--end-report-year", type=int, default=2025)
    parser.add_argument("--ex-start", default="2016-01-04")
    parser.add_argument("--ex-end", default="2025-12-31")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--request-delay-seconds", type=float, default=0.35)
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()

    started_at = datetime.now(TZ)
    root = args.output_root.resolve()
    raw_root = root / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    request_contract = {
        "api_url": API_URL,
        "report_name": "RPT_SHAREBONUS_DET",
        "report_dates": quarter_ends(args.start_report_year, args.end_report_year),
        "ex_window": [args.ex_start, args.ex_end],
        "page_size": args.page_size,
        "markets": ["SH", "SZ"],
        "scheduler_status": "DISABLED_MANUAL_ONLY",
    }
    input_hash = hashlib.sha256(
        json.dumps(request_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    session = requests.Session()
    session.headers.update({"User-Agent": "A-share-backtest-audit/1.0 (manual deterministic collector)"})
    all_rows: list[dict[str, Any]] = []
    raw_records: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    for report_date in request_contract["report_dates"]:
        base_params = {
            "sortColumns": "PLAN_NOTICE_DATE",
            "sortTypes": "-1",
            "pageSize": str(args.page_size),
            "pageNumber": "1",
            "reportName": "RPT_SHAREBONUS_DET",
            "columns": "ALL",
            "quoteColumns": "",
            "source": "WEB",
            "client": "WEB",
            "filter": f"(REPORT_DATE='{report_date}')",
        }
        try:
            first = request_json(session, base_params, args.retries)
            result = first.get("result") or {}
            pages = int(result.get("pages") or 0)
            payloads = [first]
            for page in range(2, pages + 1):
                time.sleep(args.request_delay_seconds)
                params = dict(base_params, pageNumber=str(page))
                payloads.append(request_json(session, params, args.retries))
            report_rows: list[dict[str, Any]] = []
            for payload in payloads:
                report_rows.extend(((payload.get("result") or {}).get("data") or []))
            raw_path = raw_root / f"report-date-{report_date}.json"
            raw_path.write_text(
                json.dumps({"request": base_params, "pages": payloads}, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            raw_records.append(
                {
                    "report_date": report_date,
                    "pages": pages,
                    "rows": len(report_rows),
                    "path": str(raw_path.relative_to(Path.cwd())),
                    "bytes": raw_path.stat().st_size,
                    "sha256": sha256_file(raw_path),
                }
            )
            all_rows.extend(report_rows)
            print(f"{report_date}: pages={pages} rows={len(report_rows)}", flush=True)
            time.sleep(args.request_delay_seconds)
        except Exception as exc:  # keep per-period failure evidence
            failures.append({"report_date": report_date, "error": f"{type(exc).__name__}: {exc}"})
            print(f"{report_date}: FAILED {exc}", flush=True)

    frame = pd.DataFrame(all_rows)
    if frame.empty:
        normalized = frame
    else:
        wanted = [
            "SECUCODE", "SECURITY_CODE", "SECURITY_NAME_ABBR", "ORG_CODE",
            "REPORT_DATE", "PLAN_NOTICE_DATE", "NOTICE_DATE", "PUBLISH_DATE",
            "EQUITY_RECORD_DATE", "EX_DIVIDEND_DATE", "PRETAX_BONUS_RMB",
            "BONUS_RATIO", "IT_RATIO", "BONUS_IT_RATIO", "ASSIGN_PROGRESS",
            "IMPL_PLAN_PROFILE", "MARKET_TYPE",
        ]
        normalized = frame.reindex(columns=wanted).copy()
        normalized.columns = [column.lower() for column in normalized.columns]
        for column in (
            "report_date", "plan_notice_date", "notice_date", "publish_date",
            "equity_record_date", "ex_dividend_date",
        ):
            normalized[column] = pd.to_datetime(normalized[column], errors="coerce").dt.date
        for column in ("pretax_bonus_rmb", "bonus_ratio", "it_ratio", "bonus_it_ratio"):
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        normalized = normalized[
            normalized["secucode"].str.endswith((".SH", ".SZ"), na=False)
            & normalized["ex_dividend_date"].between(
                pd.Timestamp(args.ex_start).date(), pd.Timestamp(args.ex_end).date(), inclusive="both"
            )
        ].copy()
        normalized["source_id"] = "EASTMONEY_RPT_SHAREBONUS_DET"
        normalized["source_uri"] = API_URL
        normalized["retrieved_at"] = started_at.isoformat()
        normalized["known_at_candidate"] = normalized["plan_notice_date"]
        normalized["known_at_status"] = "CANDIDATE_REQUIRES_REVISION_HISTORY_AUDIT"
        normalized["formal_backtest_eligible"] = False
        normalized["is_synthetic"] = False
        normalized.sort_values(["ex_dividend_date", "secucode", "report_date"], inplace=True)
        normalized.drop_duplicates(inplace=True)

    parquet_path = root / "shsz-dividend-events-candidate.parquet"
    normalized.to_parquet(parquet_path, index=False)
    completed_at = datetime.now(TZ)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": root.name,
        "workflow": "BACKTEST_SOURCE_EVIDENCE_COLLECTION",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": (completed_at - started_at).total_seconds(),
        "input_hash": input_hash,
        "request_contract": request_contract,
        "raw_records": raw_records,
        "failures": failures,
        "output": {
            "path": str(parquet_path.relative_to(Path.cwd())),
            "bytes": parquet_path.stat().st_size,
            "sha256": sha256_file(parquet_path),
            "rows": len(normalized),
            "unique_symbols": int(normalized["secucode"].nunique()) if not normalized.empty else 0,
            "minimum_ex_date": str(normalized["ex_dividend_date"].min()) if not normalized.empty else None,
            "maximum_ex_date": str(normalized["ex_dividend_date"].max()) if not normalized.empty else None,
        },
        "formal_assessment": {
            "status": "STAGING_ONLY_NOT_FORMAL_BACKTEST_INPUT",
            "reason": "Provider returns its latest plan view; historical revisions must be reconstructed before PLAN_NOTICE_DATE can be treated as a strict point-in-time known_at for the final amount.",
            "formal_gate_closed": False,
        },
    }
    (root / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt["output"], ensure_ascii=False, indent=2), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
