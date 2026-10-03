#!/usr/bin/env python3
"""Download EastMoney F10 main financial indicators for the SH/SZ universe.

The command is intentionally foreground and manual-only. It writes immutable
staging evidence, never promotes rows to the formal point-in-time lake, never
changes strategy rules, and never runs a backtest. Current-view values are
assigned a conservative candidate known-at time of max(NOTICE_DATE,
UPDATE_DATE); this is a safety boundary, not a claim that revision history is
complete.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATUS_GLOB = PROJECT_ROOT / (
    "state/cnequity-shsz-2016-2025-local/meta/revisions/data/trading_status/"
    "4fe8622405a046c0b2535a048dee04ba/trade_date=*/part-merged.parquet"
)
API_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
REPORT_NAME = "RPT_F10_FINANCE_MAINFINADATA"
SOURCE_ID = "EASTMONEY_F10_PUBLIC_MAINFINADATA"
PRIMARY_START = "2016-01-04"
PRIMARY_END = "2025-12-31"
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
                "Referer": "https://emweb.securities.eastmoney.com/",
                "Accept": "application/json,text/plain,*/*",
            }
        )
        _THREAD.session = value
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--limit-symbols", type=int)
    return parser.parse_args()


def symbols_from_status(limit: int | None) -> list[str]:
    con = duckdb.connect()
    path = str(STATUS_GLOB).replace("'", "''")
    symbols = [
        row[0]
        for row in con.execute(
            f"""
            SELECT DISTINCT symbol
            FROM read_parquet('{path}', hive_partitioning=false)
            WHERE right(symbol, 3) IN ('.SH', '.SZ')
            ORDER BY symbol
            """
        ).fetchall()
    ]
    return symbols[:limit] if limit else symbols


def parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def conservative_known_at(row: dict[str, Any]) -> str | None:
    values = [parse_timestamp(row.get(name)) for name in ("NOTICE_DATE", "UPDATE_DATE")]
    present = [value for value in values if value is not None]
    return max(present).isoformat() if present else None


def request_symbol(symbol: str, timeout: float, max_attempts: int) -> dict[str, Any]:
    params = {
        "reportName": REPORT_NAME,
        "columns": "ALL",
        "filter": f'(SECUCODE="{symbol}")',
        "pageNumber": "1",
        "pageSize": "500",
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
        "source": "HSF10",
        "client": "PC",
    }
    errors: list[str] = []
    started = time.monotonic()
    for attempt in range(1, max_attempts + 1):
        retrieved_at = now()
        try:
            response = session().get(API_URL, params=params, timeout=timeout)
            body_sha256 = hashlib.sha256(response.content).hexdigest()
            response.raise_for_status()
            payload = response.json()
            if not payload.get("success"):
                raise RuntimeError(f"provider failure: {payload.get('message') or payload.get('code')}")
            result = payload.get("result") or {}
            pages = int(result.get("pages") or 0)
            data = result.get("data") or []
            if pages > 1:
                raise RuntimeError(f"unexpected pagination pages={pages} with pageSize=500")
            if not isinstance(data, list):
                raise RuntimeError("provider result.data is not a list")
            return {
                "symbol": symbol,
                "ok": True,
                "attempts": attempt,
                "retrieved_at": retrieved_at,
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "request_url": response.url,
                "http_status": response.status_code,
                "response_sha256": body_sha256,
                "provider_code": payload.get("code"),
                "provider_message": payload.get("message"),
                "provider_count": result.get("count"),
                "rows": data,
                "errors": errors,
            }
        except Exception as error:  # retained verbatim in the evidence file
            errors.append(f"attempt={attempt} {type(error).__name__}: {error}")
            if attempt < max_attempts:
                time.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))
    return {
        "symbol": symbol,
        "ok": False,
        "attempts": max_attempts,
        "retrieved_at": now(),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "rows": [],
        "errors": errors,
    }


def normalize(result: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in result["rows"]:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        row.update(
            {
                "_requested_symbol": result["symbol"],
                "_source_id": SOURCE_ID,
                "_source_uri": result.get("request_url"),
                "_retrieved_at": result["retrieved_at"],
                "_response_sha256": result.get("response_sha256"),
                "_known_at_conservative": conservative_known_at(raw),
                "_known_at_policy": "MAX_NOTICE_DATE_UPDATE_DATE_CURRENT_VIEW",
                "_formal_backtest_eligible": False,
                "_is_synthetic": False,
            }
        )
        normalized.append(row)
    return normalized


def main() -> int:
    args = parse_args()
    if not 1 <= args.workers <= 8:
        raise RuntimeError("workers must be between 1 and 8")
    output_dir = args.output_dir.resolve()
    raw_path = output_dir / "raw-responses.jsonl"
    normalized_path = output_dir / "mainfinadata-current-view.jsonl"
    parquet_path = output_dir / "mainfinadata-current-view.parquet"
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-eastmoney-mainfinadata.json"
    collisions = [path for path in (output_dir, raw_path, normalized_path, parquet_path, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))
    if not STATUS_GLOB.parent.parent.is_dir():
        raise RuntimeError(f"status revision root missing: {STATUS_GLOB.parent.parent}")

    symbols = symbols_from_status(args.limit_symbols)
    if not symbols:
        raise RuntimeError("empty SH/SZ universe")
    symbol_sha = hashlib.sha256("\n".join(symbols).encode()).hexdigest()
    descriptor = {
        "endpoint": API_URL,
        "report_name": REPORT_NAME,
        "page_size": 500,
        "source": "HSF10",
        "client": "PC",
        "universe_status_revision": "4fe8622405a046c0b2535a048dee04ba",
        "symbols": len(symbols),
        "symbols_sha256": symbol_sha,
        "workers": args.workers,
        "script_sha256": sha256_file(Path(__file__).resolve()),
    }
    input_sha = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
    started_at = now()
    started_monotonic = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=False)

    ok = 0
    empty = 0
    failed: list[dict[str, Any]] = []
    total_rows = 0
    with raw_path.open("w", encoding="utf-8") as raw_handle, normalized_path.open("w", encoding="utf-8") as data_handle:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for offset in range(0, len(symbols), args.workers):
                batch = symbols[offset : offset + args.workers]
                futures = {
                    symbol: pool.submit(request_symbol, symbol, args.timeout_seconds, args.max_attempts)
                    for symbol in batch
                }
                for symbol in batch:  # stable output ordering regardless of completion order
                    result = futures[symbol].result()
                    raw_handle.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
                    rows = normalize(result)
                    for row in rows:
                        data_handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                    total_rows += len(rows)
                    if result["ok"]:
                        ok += 1
                        empty += int(not rows)
                    else:
                        failed.append({"symbol": symbol, "errors": result["errors"]})
                raw_handle.flush()
                data_handle.flush()
                processed = min(offset + len(batch), len(symbols))
                if processed % 100 <= args.workers or processed == len(symbols):
                    print(
                        json.dumps(
                            {"processed": processed, "total": len(symbols), "ok": ok, "empty": empty, "failed": len(failed), "rows": total_rows},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

    if total_rows:
        con = duckdb.connect()
        source = str(normalized_path).replace("'", "''")
        target = str(parquet_path).replace("'", "''")
        con.execute(
            f"""
            COPY (
              SELECT * FROM read_json_auto(
                '{source}', format='newline_delimited', union_by_name=true,
                maximum_object_size=16777216
              )
            ) TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
            """
        )
        stats = con.execute(
            f"""
            SELECT count(*) AS rows,
                   count(DISTINCT SECUCODE) AS symbols,
                   min(TRY_CAST(REPORT_DATE AS DATE)) AS report_min,
                   max(TRY_CAST(REPORT_DATE AS DATE)) AS report_max,
                   count(*) FILTER (WHERE REPORT_TYPE = '年报') AS annual_rows,
                   count(*) FILTER (
                     WHERE TRY_CAST(REPORT_DATE AS DATE) BETWEEN DATE '2015-01-01' AND DATE '{PRIMARY_END}'
                   ) AS relevant_rows,
                   count(*) FILTER (WHERE _known_at_conservative IS NULL) AS missing_known_at,
                   count(*) FILTER (
                     WHERE TRY_CAST(UPDATE_DATE AS TIMESTAMP) > TRY_CAST(NOTICE_DATE AS TIMESTAMP)
                   ) AS update_after_notice,
                   count(*) FILTER (
                     WHERE TRY_CAST(_known_at_conservative AS TIMESTAMP) > TIMESTAMP '{PRIMARY_END} 23:59:59'
                   ) AS only_known_after_primary_window
            FROM read_parquet('{target}')
            """
        ).fetchone()
        names = (
            "rows", "symbols", "report_min", "report_max", "annual_rows", "relevant_rows",
            "missing_known_at", "update_after_notice", "only_known_after_primary_window",
        )
        profile = {
            name: value.isoformat() if hasattr(value, "isoformat") else value
            for name, value in zip(names, stats, strict=True)
        }
    else:
        profile = {"rows": 0, "symbols": 0}

    completed_at = now()
    artifacts = []
    for path in (raw_path, normalized_path, parquet_path):
        if path.exists():
            artifacts.append(
                {
                    "path": str(path.relative_to(PROJECT_ROOT)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_PUBLIC_FINANCIAL_INDICATOR_ACQUISITION",
        "status": "STAGING_SOURCE_SUCCESS_NOT_FORMAL" if not failed else "PARTIAL_STAGING_SOURCE_NOT_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "primary_window": {"start": PRIMARY_START, "end": PRIMARY_END},
        "source": {
            "source_id": SOURCE_ID,
            "endpoint": API_URL,
            "report_name": REPORT_NAME,
            "retrieval_type": "PUBLIC_NO_KEY_HTTP_GET",
        },
        "input": {**descriptor, "input_sha256": input_sha},
        "summary": {
            "symbols_requested": len(symbols),
            "symbols_succeeded": ok,
            "symbols_empty": empty,
            "symbols_failed": len(failed),
            "normalized_rows": total_rows,
            "profile": profile,
            "failures": failed,
        },
        "point_in_time_policy": {
            "candidate_known_at": "max(NOTICE_DATE, UPDATE_DATE)",
            "reason": "The endpoint is a current view; using the later timestamp prevents a restated value from entering before the visible update date.",
            "limitation": "The endpoint does not preserve every earlier published value or revision body. Earlier values remain unavailable rather than reconstructed.",
        },
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["QUALITY_GATE", "POINT_IN_TIME_COVERAGE"],
            "reasons": [
                "A current-view snapshot is not a complete historical revision archive.",
                "Provider public-use and redistribution terms must be recorded before promotion.",
                "Quality thresholds and industry-specific effective rules are not yet frozen.",
                "A sample must be reconciled to statutory CNInfo filings before formal use.",
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
        "event_type": "backtest-eastmoney-mainfinadata-acquired",
        "run_id": args.run_id,
        "status": receipt["status"],
        "scheduler_status": receipt["scheduler_status"],
        "input_sha256": input_sha,
        "artifact": {
            "path": str(receipt_path.relative_to(PROJECT_ROOT)),
            "sha256": sha256_file(receipt_path),
        },
        "blockers_preserved": receipt["formal_backtest_eligibility"]["blockers_preserved"],
        "created_at": completed_at,
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
