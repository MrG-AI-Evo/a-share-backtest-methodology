#!/usr/bin/env python3
"""Download CNInfo annual-report index per SH/SZ symbol, manual-only.

Per-symbol queries avoid the public index's unstable deep pagination on annual
report deadline days. The command writes immutable staging evidence and does
not download PDFs, promote data, change rules, schedule work, or run a backtest.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_GLOB = PROJECT_ROOT / (
    "state/cnequity-shsz-2016-2025-local/meta/revisions/data/trading_status/"
    "4fe8622405a046c0b2535a048dee04ba/trade_date=*/part-merged.parquet"
)
STOCK_MAP_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
SEARCH_PAGE = "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search"
SOURCE_ID = "CNINFO_OFFICIAL_ANNUAL_REPORT_INDEX_BY_SYMBOL"
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
    parser.add_argument("--limit-symbols", type=int)
    return parser.parse_args()


def load_symbols(limit: int | None) -> list[str]:
    con = duckdb.connect()
    path = str(STATUS_GLOB).replace("'", "''")
    values = [
        row[0]
        for row in con.execute(
            f"SELECT DISTINCT symbol FROM read_parquet('{path}', hive_partitioning=false) ORDER BY symbol"
        ).fetchall()
    ]
    return values[:limit] if limit else values


def get_stock_map(timeout: float, max_attempts: int) -> tuple[dict[str, str], dict[str, Any]]:
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            response = session().get(STOCK_MAP_URL, timeout=timeout)
            response.raise_for_status()
            body = response.json()
            stock_list = body.get("stockList") or []
            mapping = {
                str(item.get("code")): str(item.get("orgId"))
                for item in stock_list
                if item.get("code") and item.get("orgId")
            }
            return mapping, {
                "retrieved_at": now(),
                "attempts": attempt,
                "http_status": response.status_code,
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "body": body,
                "errors": errors,
            }
        except Exception as error:
            errors.append(f"attempt={attempt} {type(error).__name__}: {error}")
            if attempt < max_attempts:
                time.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))
    raise RuntimeError("CNInfo stock map failed: " + " | ".join(errors))


def fetch_page(
    secucode: str,
    org_id: str,
    start: str,
    end: str,
    page: int,
    sort_type: str,
    timeout: float,
    max_attempts: int,
) -> dict[str, Any]:
    code, suffix = secucode.split(".")
    payload = {
        "pageNum": str(page),
        "pageSize": str(PAGE_SIZE),
        "column": "sse" if suffix == "SH" else "szse",
        "tabName": "fulltext",
        "plate": "sh" if suffix == "SH" else "sz",
        "stock": f"{code},{org_id}",
        "searchkey": "",
        "secid": "",
        "category": CATEGORY,
        "trade": "",
        "seDate": f"{start}~{end}",
        "sortName": "announcementTime",
        "sortType": sort_type,
        "isHLtitle": "false",
    }
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            response = session().post(QUERY_URL, data=payload, timeout=timeout)
            response.raise_for_status()
            body = response.json()
            announcements = body.get("announcements") or []
            if not isinstance(announcements, list):
                raise RuntimeError("announcements is not a list")
            return {
                "ok": True,
                "page": page,
                "sort_type": sort_type,
                "attempts": attempt,
                "retrieved_at": now(),
                "http_status": response.status_code,
                "response_sha256": hashlib.sha256(response.content).hexdigest(),
                "body": body,
                "errors": errors,
            }
        except Exception as error:
            errors.append(f"attempt={attempt} {type(error).__name__}: {error}")
            if attempt < max_attempts:
                time.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))
    return {"ok": False, "page": page, "sort_type": sort_type, "attempts": max_attempts, "retrieved_at": now(), "errors": errors}


def fetch_symbol(
    secucode: str,
    org_id: str,
    start: str,
    end: str,
    timeout: float,
    max_attempts: int,
) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    first = fetch_page(secucode, org_id, start, end, 1, "desc", timeout, max_attempts)
    pages.append(first)
    if not first["ok"]:
        return {"symbol": secucode, "org_id": org_id, "ok": False, "pages": pages, "errors": first["errors"]}
    total = int(first["body"].get("totalAnnouncement") or 0)
    page_count = math.ceil(total / PAGE_SIZE)
    for page_number in range(2, page_count + 1):
        page = fetch_page(secucode, org_id, start, end, page_number, "desc", timeout, max_attempts)
        pages.append(page)
        if not page["ok"]:
            return {"symbol": secucode, "org_id": org_id, "ok": False, "pages": pages, "errors": page["errors"]}
    if page_count > 1:
        for page_number in range(1, page_count + 1):
            page = fetch_page(secucode, org_id, start, end, page_number, "asc", timeout, max_attempts)
            pages.append(page)
            if not page["ok"]:
                return {"symbol": secucode, "org_id": org_id, "ok": False, "pages": pages, "errors": page["errors"]}
    ids = {
        str(item.get("announcementId") or "")
        for page in pages if page["ok"]
        for item in page["body"].get("announcements") or []
        if item.get("announcementId")
    }
    errors = [] if len(ids) == total else [f"coverage mismatch expected={total} unique_delivered={len(ids)}"]
    return {
        "symbol": secucode,
        "org_id": org_id,
        "ok": not errors,
        "total_announcement": total,
        "unique_delivered": len(ids),
        "pages": pages,
        "errors": errors,
    }


def normalize(item: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    code = str(item.get("secCode") or "")
    timestamp_ms = item.get("announcementTime")
    known_at = None
    if isinstance(timestamp_ms, (int, float)):
        published_date = datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(SHANGHAI).date().isoformat()
        known_at = f"{published_date}T23:59:59+08:00"
    adjunct = item.get("adjunctUrl")
    return {
        "announcement_id": item.get("announcementId"),
        "symbol": code,
        "exchange": "SSE" if code.startswith("6") else "SZSE" if code.startswith(("0", "3")) else "OUT_OF_SCOPE",
        "issuer_name": item.get("secName"),
        "org_id": item.get("orgId"),
        "title": item.get("announcementTitle"),
        "short_title": item.get("shortTitle"),
        "announcement_time_raw_ms": timestamp_ms,
        "known_at_conservative": known_at,
        "known_at_precision": "DATE_END_OF_DAY",
        "adjunct_url": adjunct,
        "pdf_url": f"https://static.cninfo.com.cn/{str(adjunct).lstrip('/')}" if adjunct else None,
        "adjunct_type": item.get("adjunctType"),
        "adjunct_size_kib_candidate": item.get("adjunctSize"),
        "announcement_type": item.get("announcementType"),
        "associate_announcement": item.get("associateAnnouncement"),
        "source_id": SOURCE_ID,
        "source_uri": SEARCH_PAGE,
        "retrieved_at": page["retrieved_at"],
        "response_sha256": page["response_sha256"],
        "formal_backtest_eligible": False,
        "is_synthetic": False,
    }


def main() -> int:
    args = parse_args()
    if not 1 <= args.workers <= 8:
        raise RuntimeError("workers must be between 1 and 8")
    symbols = load_symbols(args.limit_symbols)
    stock_map, stock_map_evidence = get_stock_map(args.timeout_seconds, args.max_attempts)
    missing_org = [symbol for symbol in symbols if symbol[:6] not in stock_map]
    if missing_org:
        raise RuntimeError(f"CNInfo stock map missing {len(missing_org)} SH/SZ symbols: {missing_org[:20]}")

    output_dir = args.output_dir.resolve()
    stock_map_path = output_dir / "stock-map.json"
    raw_path = output_dir / "raw-symbol-responses.jsonl"
    normalized_path = output_dir / "annual-report-announcements.jsonl"
    parquet_path = output_dir / "annual-report-announcements.parquet"
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-cninfo-annual-report-index-by-symbol.json"
    collisions = [path for path in (output_dir, stock_map_path, raw_path, normalized_path, parquet_path, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))

    descriptor = {
        "query_url": QUERY_URL,
        "stock_map_url": STOCK_MAP_URL,
        "category": CATEGORY,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "page_size": PAGE_SIZE,
        "symbols": len(symbols),
        "symbols_sha256": hashlib.sha256("\n".join(symbols).encode()).hexdigest(),
        "workers": args.workers,
        "script_sha256": sha256_file(Path(__file__).resolve()),
    }
    input_sha = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    started_at = now()
    started_monotonic = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=False)
    stock_map_path.write_text(json.dumps(stock_map_evidence, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    failures: list[dict[str, Any]] = []
    empty = 0
    rows_written = 0
    with raw_path.open("w", encoding="utf-8") as raw_handle, normalized_path.open("w", encoding="utf-8") as data_handle:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for offset in range(0, len(symbols), args.workers):
                batch = symbols[offset : offset + args.workers]
                futures = {
                    symbol: pool.submit(
                        fetch_symbol, symbol, stock_map[symbol[:6]], args.start_date, args.end_date,
                        args.timeout_seconds, args.max_attempts,
                    )
                    for symbol in batch
                }
                for symbol in batch:
                    result = futures[symbol].result()
                    raw_handle.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
                    if not result["ok"]:
                        failures.append({"symbol": symbol, "errors": result["errors"]})
                    empty += int(result.get("total_announcement") == 0)
                    seen: set[str] = set()
                    for page in result["pages"]:
                        if not page["ok"]:
                            continue
                        for item in page["body"].get("announcements") or []:
                            announcement_id = str(item.get("announcementId") or "")
                            if not announcement_id or announcement_id in seen:
                                continue
                            seen.add(announcement_id)
                            row = normalize(item, page)
                            if row["exchange"] not in {"SSE", "SZSE"}:
                                continue
                            data_handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                            rows_written += 1
                raw_handle.flush()
                data_handle.flush()
                processed = min(offset + len(batch), len(symbols))
                if processed % 100 <= args.workers or processed == len(symbols):
                    print(json.dumps({"processed": processed, "total": len(symbols), "rows": rows_written, "empty": empty, "failures": len(failures)}, ensure_ascii=False), flush=True)

    con = duckdb.connect()
    source = str(normalized_path).replace("'", "''")
    target = str(parquet_path).replace("'", "''")
    con.execute(
        f"""
        COPY (SELECT * FROM read_json_auto('{source}', format='newline_delimited', union_by_name=true))
        TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    )
    values = con.execute(
        f"""
        SELECT count(*), count(DISTINCT announcement_id), count(DISTINCT symbol),
               count(*) FILTER (WHERE title LIKE '%摘要%'),
               count(*) FILTER (WHERE title LIKE '%取消%'),
               count(*) FILTER (WHERE title LIKE '%英文%'),
               count(*) FILTER (WHERE title NOT LIKE '%摘要%' AND title NOT LIKE '%取消%' AND title NOT LIKE '%英文%' AND adjunct_type='PDF'),
               count(*) FILTER (WHERE known_at_conservative IS NULL),
               count(*) FILTER (WHERE pdf_url IS NULL)
        FROM read_parquet('{target}')
        """
    ).fetchone()
    names = ("rows", "unique_announcement_ids", "symbols", "summary_rows", "cancelled_rows", "english_rows", "full_report_candidates", "missing_known_at", "missing_pdf_url")
    profile = dict(zip(names, values, strict=True))
    duplicate_rows = profile["rows"] - profile["unique_announcement_ids"]
    if duplicate_rows:
        failures.append({"error": f"duplicate announcement ids in normalized output: {duplicate_rows}"})

    completed_at = now()
    artifacts = [
        {"path": str(path.relative_to(PROJECT_ROOT)), "bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in (stock_map_path, raw_path, normalized_path, parquet_path)
    ]
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_OFFICIAL_DISCLOSURE_INDEX_ACQUISITION",
        "status": "STAGING_SOURCE_SUCCESS_NOT_FORMAL" if not failures else "PARTIAL_STAGING_SOURCE_NOT_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "source": {"source_id": SOURCE_ID, "query_url": QUERY_URL, "stock_map_url": STOCK_MAP_URL, "search_page": SEARCH_PAGE},
        "input": {**descriptor, "input_sha256": input_sha},
        "summary": {"symbols_requested": len(symbols), "symbols_empty": empty, "symbols_failed": len(failures), "rows": rows_written, "profile": profile, "failures": failures},
        "point_in_time_policy": {"known_at_candidate": "announcement calendar date at 23:59:59 Asia/Shanghai", "reason": "Exact intraday publication time is absent; end-of-day is conservative for next-session use."},
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["QUALITY_GATE", "POINT_IN_TIME_COVERAGE"],
            "reasons": [
                "Filing bodies and correction announcements are not yet reconciled.",
                "Numerical observations need a deterministic revision join to this official index.",
                "Quality thresholds and industry-specific effective rules are not yet frozen.",
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
        "event_type": "backtest-cninfo-annual-report-index-by-symbol-acquired",
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
