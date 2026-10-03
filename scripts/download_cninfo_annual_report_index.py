#!/usr/bin/env python3
"""Download the official CNInfo annual-report announcement index for SH/SZ.

This one-shot, foreground, manual-only collector queries the public CNInfo
announcement index by calendar month. It stores raw page responses, normalized
metadata and PDF URLs, but does not download filing bodies, promote data, alter
rules, schedule work, or run a backtest.
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import hashlib
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
SOURCE_ID = "CNINFO_OFFICIAL_ANNUAL_REPORT_INDEX"
CATEGORY = "category_ndbg_szsh"
PAGE_SIZE = 30
SHANGHAI = ZoneInfo("Asia/Shanghai")
_THREAD = threading.local()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def session() -> requests.Session:
    value = getattr(_THREAD, "session", None)
    if value is None:
        value = requests.Session()
        value.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36",
                "Referer": "https://www.cninfo.com.cn/",
                "Accept": "application/json,text/plain,*/*",
            }
        )
        _THREAD.session = value
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date", default="2015-01-01")
    parser.add_argument("--end-date", default="2025-12-31")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--limit-months", type=int)
    parser.add_argument("--window-unit", choices=("month", "day"), default="month")
    return parser.parse_args()


def month_windows(start: str, end: str) -> list[tuple[str, str]]:
    start_dt = datetime.fromisoformat(start).date()
    end_dt = datetime.fromisoformat(end).date()
    if start_dt > end_dt:
        raise RuntimeError("start-date must not be after end-date")
    windows: list[tuple[str, str]] = []
    year, month = start_dt.year, start_dt.month
    while (year, month) <= (end_dt.year, end_dt.month):
        first = datetime(year, month, 1).date()
        last = datetime(year, month, calendar.monthrange(year, month)[1]).date()
        windows.append((max(first, start_dt).isoformat(), min(last, end_dt).isoformat()))
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return windows


def day_windows(start: str, end: str) -> list[tuple[str, str]]:
    start_dt = datetime.fromisoformat(start).date()
    end_dt = datetime.fromisoformat(end).date()
    if start_dt > end_dt:
        raise RuntimeError("start-date must not be after end-date")
    windows: list[tuple[str, str]] = []
    current = start_dt
    while current <= end_dt:
        value = current.isoformat()
        windows.append((value, value))
        current += timedelta(days=1)
    return windows


def fetch_page(
    start: str,
    end: str,
    page: int,
    timeout: float,
    max_attempts: int,
    plate: str,
    sort_type: str,
) -> dict[str, Any]:
    payload = {
        "pageNum": str(page),
        "pageSize": str(PAGE_SIZE),
        "column": "szse",
        "tabName": "fulltext",
        "plate": plate,
        "stock": "",
        "searchkey": "",
        "secid": "",
        "category": CATEGORY,
        "trade": "",
        "seDate": f"{start}~{end}",
        "sortName": "time",
        "sortType": sort_type,
        "isHLtitle": "true",
    }
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            response = session().post(API_URL, data=payload, timeout=timeout)
            body_sha = hashlib.sha256(response.content).hexdigest()
            response.raise_for_status()
            body = response.json()
            announcements = body.get("announcements") or []
            if not isinstance(announcements, list):
                raise RuntimeError("announcements is not a list")
            return {
                "ok": True,
                "page": page,
                "plate": plate,
                "sort_type": sort_type,
                "attempts": attempt,
                "retrieved_at": now(),
                "http_status": response.status_code,
                "response_sha256": body_sha,
                "errors": errors,
                "body": body,
            }
        except Exception as error:
            errors.append(f"attempt={attempt} {type(error).__name__}: {error}")
            if attempt < max_attempts:
                time.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))
    return {
        "ok": False,
        "page": page,
        "plate": plate,
        "sort_type": sort_type,
        "attempts": max_attempts,
        "retrieved_at": now(),
        "errors": errors,
    }


def fetch_window(window: tuple[str, str], timeout: float, max_attempts: int) -> dict[str, Any]:
    start, end = window
    pages: list[dict[str, Any]] = []
    expected_total = 0
    errors: list[str] = []
    unique_ids: set[tuple[str, str]] = set()
    for plate in ("sh", "sz"):
        first = fetch_page(start, end, 1, timeout, max_attempts, plate, "desc")
        pages.append(first)
        if not first["ok"]:
            errors.extend(first["errors"])
            continue
        total = int(first["body"].get("totalAnnouncement") or 0)
        expected_total += total
        expected_pages = math.ceil(total / PAGE_SIZE)
        for page_number in range(2, min(expected_pages, 100) + 1):
            page = fetch_page(start, end, page_number, timeout, max_attempts, plate, "desc")
            pages.append(page)
            if not page["ok"]:
                errors.extend(page["errors"])
                break
        if expected_pages > 100:
            for page_number in range(1, 101):
                page = fetch_page(start, end, page_number, timeout, max_attempts, plate, "asc")
                pages.append(page)
                if not page["ok"]:
                    errors.extend(page["errors"])
                    break
    for page in pages:
        if not page["ok"]:
            continue
        for item in page["body"].get("announcements") or []:
            unique_ids.add((str(item.get("secCode") or ""), str(item.get("announcementId") or "")))
    delivered = len(unique_ids)
    if delivered != expected_total:
        errors.append(f"coverage mismatch expected={expected_total} unique_delivered={delivered}")
    return {
        "window": window,
        "ok": not errors,
        "total_announcement": expected_total,
        "delivered": delivered,
        "pages": pages,
        "errors": errors,
    }


def normalize_announcement(raw: dict[str, Any], retrieved_at: str, response_sha256: str) -> dict[str, Any]:
    timestamp_ms = raw.get("announcementTime")
    published = None
    if isinstance(timestamp_ms, (int, float)):
        published_dt = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(SHANGHAI)
        published_date = published_dt.date().isoformat()
        published = f"{published_date}T23:59:59+08:00"
    adjunct = raw.get("adjunctUrl")
    pdf_url = f"https://static.cninfo.com.cn/{str(adjunct).lstrip('/')}" if adjunct else None
    code = str(raw.get("secCode") or "")
    return {
        "announcement_id": raw.get("announcementId"),
        "symbol": code,
        "exchange": "SSE" if code.startswith("6") else "SZSE" if code.startswith(("0", "3")) else "OUT_OF_SCOPE",
        "issuer_name": raw.get("secName"),
        "org_id": raw.get("orgId"),
        "title": raw.get("announcementTitle"),
        "short_title": raw.get("shortTitle"),
        "announcement_time_raw_ms": timestamp_ms,
        "known_at_conservative": published,
        "known_at_precision": "DATE_END_OF_DAY",
        "adjunct_url": adjunct,
        "pdf_url": pdf_url,
        "adjunct_type": raw.get("adjunctType"),
        "adjunct_size_kib_candidate": raw.get("adjunctSize"),
        "announcement_type": raw.get("announcementType"),
        "column_id": raw.get("columnId"),
        "page_column": raw.get("pageColumn"),
        "associate_announcement": raw.get("associateAnnouncement"),
        "source_id": SOURCE_ID,
        "source_uri": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        "retrieved_at": retrieved_at,
        "response_sha256": response_sha256,
        "formal_backtest_eligible": False,
        "is_synthetic": False,
    }


def main() -> int:
    args = parse_args()
    if not 1 <= args.workers <= 8:
        raise RuntimeError("workers must be between 1 and 8")
    windows = day_windows(args.start_date, args.end_date) if args.window_unit == "day" else month_windows(args.start_date, args.end_date)
    if args.limit_months:
        windows = windows[: args.limit_months]
    output_dir = args.output_dir.resolve()
    raw_path = output_dir / "raw-pages.jsonl"
    normalized_path = output_dir / "annual-report-announcements.jsonl"
    parquet_path = output_dir / "annual-report-announcements.parquet"
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-cninfo-annual-report-index.json"
    collisions = [path for path in (output_dir, raw_path, normalized_path, parquet_path, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))

    descriptor = {
        "endpoint": API_URL,
        "category": CATEGORY,
        "page_size": PAGE_SIZE,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "windows": windows,
        "window_unit": args.window_unit,
        "plates": ["sh", "sz"],
        "deep_page_policy": "TIME_DESC_100_PLUS_TIME_ASC_100_WHEN_PLATE_EXCEEDS_3000",
        "workers": args.workers,
        "script_sha256": sha256_file(Path(__file__).resolve()),
    }
    input_sha = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    started_at = now()
    started_monotonic = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=False)
    failures: list[dict[str, Any]] = []
    pages_written = 0
    rows_written = 0
    with raw_path.open("w", encoding="utf-8") as raw_handle, normalized_path.open("w", encoding="utf-8") as data_handle:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                window: pool.submit(fetch_window, window, args.timeout_seconds, args.max_attempts)
                for window in windows
            }
            for index, window in enumerate(windows, start=1):
                result = futures[window].result()
                if not result["ok"]:
                    failures.append({"window": window, "errors": result["errors"]})
                for page in result["pages"]:
                    raw_handle.write(
                        json.dumps({"window": window, **page}, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
                    pages_written += 1
                    if not page["ok"]:
                        continue
                    for item in page["body"].get("announcements") or []:
                        row = normalize_announcement(item, page["retrieved_at"], page["response_sha256"])
                        if row["exchange"] not in {"SSE", "SZSE"}:
                            continue
                        data_handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                        rows_written += 1
                raw_handle.flush()
                data_handle.flush()
                print(
                    json.dumps(
                        {"windows": index, "total_windows": len(windows), "pages": pages_written, "rows": rows_written, "failures": len(failures)},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    profile: dict[str, Any]
    if rows_written:
        con = duckdb.connect()
        source = str(normalized_path).replace("'", "''")
        target = str(parquet_path).replace("'", "''")
        con.execute(
            f"""
            COPY (
              SELECT * EXCLUDE (rn)
              FROM (
                SELECT *, row_number() OVER (
                  PARTITION BY announcement_id ORDER BY retrieved_at, response_sha256
                ) AS rn
                FROM read_json_auto(
                  '{source}', format='newline_delimited', union_by_name=true,
                  maximum_object_size=16777216
                )
              ) WHERE rn = 1
            ) TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
            """
        )
        values = con.execute(
            f"""
            SELECT count(*), count(DISTINCT announcement_id), count(DISTINCT symbol),
                   CAST(min(TRY_CAST(known_at_conservative AS TIMESTAMPTZ)) AS VARCHAR),
                   CAST(max(TRY_CAST(known_at_conservative AS TIMESTAMPTZ)) AS VARCHAR),
                   count(*) FILTER (WHERE title LIKE '%摘要%'),
                   count(*) FILTER (WHERE title LIKE '%已取消%' OR title LIKE '%取消%'),
                   count(*) FILTER (WHERE title LIKE '%英文%'),
                   count(*) FILTER (
                     WHERE title NOT LIKE '%摘要%' AND title NOT LIKE '%英文%'
                       AND title NOT LIKE '%取消%' AND adjunct_type = 'PDF'
                   ),
                   count(*) FILTER (WHERE known_at_conservative IS NULL),
                   count(*) FILTER (WHERE pdf_url IS NULL)
            FROM read_parquet('{target}')
            """
        ).fetchone()
        names = (
            "rows", "unique_announcement_ids", "symbols", "known_at_min", "known_at_max",
            "summary_rows", "cancelled_rows", "english_rows", "full_report_candidates",
            "missing_known_at", "missing_pdf_url",
        )
        profile = {
            name: value.isoformat() if hasattr(value, "isoformat") else value
            for name, value in zip(names, values, strict=True)
        }
    else:
        profile = {"rows": 0, "symbols": 0}

    completed_at = now()
    artifacts = []
    for path in (raw_path, normalized_path, parquet_path):
        if path.exists():
            artifacts.append({"path": str(path.relative_to(PROJECT_ROOT)), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_OFFICIAL_DISCLOSURE_INDEX_ACQUISITION",
        "status": "STAGING_SOURCE_SUCCESS_NOT_FORMAL" if not failures else "PARTIAL_STAGING_SOURCE_NOT_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "source": {
            "source_id": SOURCE_ID,
            "endpoint": API_URL,
            "search_page": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
            "retrieval_type": "PUBLIC_NO_KEY_HTTP_POST",
        },
        "input": {**descriptor, "input_sha256": input_sha},
        "summary": {
            "windows_requested": len(windows),
            "pages_written": pages_written,
            "rows_before_dedup": rows_written,
            "windows_failed": len(failures),
            "failures": failures,
            "profile": profile,
        },
        "point_in_time_policy": {
            "known_at_candidate": "announcement calendar date at 23:59:59 Asia/Shanghai",
            "reason": "The public index does not expose an exact intraday publication time; end-of-day availability is conservative for next-session decisions.",
        },
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["QUALITY_GATE", "POINT_IN_TIME_COVERAGE"],
            "reasons": [
                "This artifact is an announcement index; filing bodies and amendments are not yet downloaded or reconciled.",
                "Quality thresholds and industry-specific effective rules are not yet frozen.",
                "The index must be joined to numerical observations with a deterministic revision policy.",
            ],
        },
        "artifacts": artifacts,
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
        "started_at": started_at,
        "completed_at": completed_at,
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "schema_version": "1.0.0",
        "event_type": "backtest-cninfo-annual-report-index-acquired",
        "run_id": args.run_id,
        "status": receipt["status"],
        "scheduler_status": receipt["scheduler_status"],
        "input_sha256": input_sha,
        "artifact": {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(receipt_path)},
        "blockers_preserved": receipt["formal_backtest_eligibility"]["blockers_preserved"],
        "created_at": completed_at,
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
