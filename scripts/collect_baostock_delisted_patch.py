#!/usr/bin/env python3
"""Collect a small, explicit set of missing delisted SH/SZ daily bars.

This is a manual-only staging collector. It never mutates the user-supplied
NAS archive, the formal backtest lake, rules, positions, orders, or ledgers.
Every run is append-only and emits a receipt plus an audit event.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import baostock as bs
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
    "turn,tradestatus,pctChg,isST"
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_symbol_end(value: str) -> tuple[str, str]:
    try:
        symbol, end_date = value.split(":", 1)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected BAOSTOCK_CODE:YYYY-MM-DD") from error
    if not symbol.startswith(("sh.", "sz.")):
        raise argparse.ArgumentTypeError("only sh./sz. symbols are allowed")
    try:
        datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as error:
        raise argparse.ArgumentTypeError("end date must be YYYY-MM-DD") from error
    return symbol, end_date


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--start-date", default="2016-01-04")
    parser.add_argument("--symbol-end", action="append", required=True, type=parse_symbol_end)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "backtests" / "staging",
    )
    parser.add_argument("--audit-root", type=Path, default=PROJECT_ROOT / "data" / "audit")
    return parser.parse_args()


def collect(symbol: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, dict[str, str]]:
    response = bs.query_history_k_data_plus(
        symbol,
        FIELDS,
        start_date=start_date,
        end_date=end_date,
        frequency="d",
        adjustflag="3",
    )
    if response.error_code != "0":
        raise RuntimeError(f"{symbol}: {response.error_code} {response.error_msg}")
    rows: list[list[str]] = []
    while response.next():
        rows.append(response.get_row_data())
    if response.error_code != "0":
        raise RuntimeError(f"{symbol}: {response.error_code} {response.error_msg}")
    if not rows:
        raise RuntimeError(f"{symbol}: EMPTY_RESPONSE")
    frame = pd.DataFrame(rows, columns=response.fields)
    frame["effective_date"] = pd.to_datetime(frame.pop("date"), errors="raise").dt.date
    frame["symbol"] = frame.pop("code").str.split(".", regex=False).str[-1]
    frame["exchange"] = "SSE" if symbol.startswith("sh.") else "SZSE"
    for source, target in {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "preclose": "prev_close",
        "volume": "volume_shares",
        "amount": "amount_cny",
        "turn": "turnover_pct",
        "pctChg": "pct_change",
    }.items():
        frame[target] = pd.to_numeric(frame.pop(source), errors="coerce")
    frame["volume_lots"] = frame["volume_shares"] / 100.0
    frame["is_trading"] = frame.pop("tradestatus").eq("1")
    frame["is_st"] = frame.pop("isST").eq("1")
    frame["adjustment_flag"] = frame.pop("adjustflag")
    frame["source_id"] = "BAOSTOCK_PUBLIC_API"
    frame["source_uri"] = "baostock.query_history_k_data_plus"
    frame["retrieved_at"] = utc_now()
    frame["known_at"] = None
    frame["known_at_status"] = "UNAVAILABLE_RETRIEVED_HISTORICALLY"
    frame["license_status"] = "PUBLIC_SOURCE_TERMS_REVIEW_REQUIRED"
    frame["formal_backtest_eligible"] = False
    frame["is_synthetic"] = False
    columns = [
        "symbol", "exchange", "effective_date", "open", "high", "low", "close",
        "prev_close", "volume_lots", "volume_shares", "amount_cny", "turnover_pct",
        "pct_change", "is_trading", "is_st", "adjustment_flag", "source_id",
        "source_uri", "retrieved_at", "known_at", "known_at_status", "license_status",
        "formal_backtest_eligible", "is_synthetic",
    ]
    result = frame[columns].sort_values(["symbol", "effective_date"]).reset_index(drop=True)
    return result, {
        "symbol": symbol,
        "requested_end_date": end_date,
        "first_date": str(result["effective_date"].min()),
        "last_date": str(result["effective_date"].max()),
        "rows": str(len(result)),
    }


def main() -> int:
    args = parse_args()
    if datetime.strptime(args.start_date, "%Y-%m-%d") is None:  # pragma: no cover
        raise RuntimeError("invalid start date")
    output_dir = args.output_root.resolve() / f"baostock-delisted-{args.run_id}"
    parquet_path = output_dir / "missing-delisted-daily-bars.parquet"
    receipt_path = output_dir / "receipt.json"
    audit_path = args.audit_root.resolve() / f"{args.run_id}-baostock-delisted-patch.json"
    collisions = [path for path in (output_dir, parquet_path, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))

    input_payload = {
        "run_id": args.run_id,
        "start_date": args.start_date,
        "symbol_end": args.symbol_end,
        "fields": FIELDS,
        "script_sha256": sha256_file(Path(__file__).resolve()),
    }
    started_at = utc_now()
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    frames: list[pd.DataFrame] = []
    coverage: list[dict[str, str]] = []
    try:
        for symbol, end_date in args.symbol_end:
            frame, detail = collect(symbol, args.start_date, end_date)
            if detail["last_date"] != end_date:
                raise RuntimeError(
                    f"{symbol}: last row {detail['last_date']} does not match expected delist date {end_date}"
                )
            frames.append(frame)
            coverage.append(detail)
    finally:
        bs.logout()

    result = pd.concat(frames, ignore_index=True)
    duplicate_count = int(result.duplicated(["symbol", "effective_date"]).sum())
    if duplicate_count:
        raise RuntimeError(f"duplicate symbol/date rows: {duplicate_count}")
    output_dir.mkdir(parents=True, exist_ok=False)
    args.audit_root.resolve().mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pandas(result, preserve_index=False), parquet_path, compression="zstd")
    completed_at = utc_now()
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_SOURCE_PATCH",
        "status": "STAGING_ONLY_NOT_FORMAL_BACKTEST_INPUT",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "source": {
            "source_id": "BAOSTOCK_PUBLIC_API",
            "source_uri": "baostock.query_history_k_data_plus",
            "package_version": getattr(bs, "__version__", "UNKNOWN"),
            "retrieved_at": started_at,
        },
        "input": {**input_payload, "input_sha256": canonical_json_sha256(input_payload)},
        "coverage": coverage,
        "quality": {
            "row_count": len(result),
            "symbol_count": int(result["symbol"].nunique()),
            "duplicate_symbol_date_rows": duplicate_count,
            "missing_close_rows": int(result["close"].isna().sum()),
            "non_trading_rows": int((~result["is_trading"]).sum()),
            "st_rows": int(result["is_st"].sum()),
        },
        "output": {
            "parquet": str(parquet_path.relative_to(PROJECT_ROOT)),
            "sha256": sha256_file(parquet_path),
        },
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["POINT_IN_TIME_COVERAGE"],
            "reasons": [
                "This patch is isolated staging evidence and has not passed source-license review.",
                "Historical retrieval time does not prove original market-publication availability.",
            ],
        },
        "started_at": started_at,
        "completed_at": completed_at,
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = {
        "schema_version": "1.0.0",
        "event_type": "BACKTEST_SOURCE_PATCH_COLLECTED",
        "run_id": args.run_id,
        "status": receipt["status"],
        "input_sha256": receipt["input"]["input_sha256"],
        "receipt": str(receipt_path.relative_to(PROJECT_ROOT)),
        "created_at": completed_at,
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        raise SystemExit(1)
