#!/usr/bin/env python3
"""Catalog NAS daily indicator archives without extracting or copying them.

The command reads each daily Parquet member in-place, excludes BSE, preserves
archive/member hashes, and writes only the first observed SH/SZ session of each
month plus a current stock-basic snapshot into immutable staging.  The source
has no accepted licence or historical knowledge timestamp, so every output is
candidate-only and must not enter a formal backtest.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "USER_SUPPLIED_NAS_DAILY_INDICATOR_ZIPS"
REQUIRED_COLUMNS = {
    "code",
    "date",
    "close",
    "turnover_rate",
    "turnover_rate_f",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ratio",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
    "log_mv",
    "log_cmv",
}
VALUE_COLUMNS = sorted(REQUIRED_COLUMNS - {"code", "date"})


def now() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def relative(path: Path) -> str:
    return str(path.resolve().relative_to(PROJECT_ROOT))


def ensure_new(*paths: Path) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise RuntimeError("refusing to overwrite immutable output: " + ", ".join(existing))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--start-date", default="2016-01-04")
    parser.add_argument("--end-date", default="2025-12-31")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--audit-dir", type=Path, default=PROJECT_ROOT / "data" / "audit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = args.source_root.expanduser().resolve()
    indicator_root = source_root / "A股日线" / "1d_feature" / "stock_indicator"
    stock_basic_path = source_root / "A股日线" / "stock_basic.parquet"
    readme_path = source_root / "README.md"
    for path in (source_root, indicator_root, stock_basic_path, readme_path):
        if not path.exists() or (path.is_file() and path.stat().st_size == 0):
            raise RuntimeError(f"required source missing or empty: {path}")

    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    if start > end:
        raise RuntimeError("start-date must not be after end-date")
    years = list(range(start.year, end.year + 1))
    archives = [indicator_root / f"{year}.zip" for year in years]
    missing = [str(path) for path in archives if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise RuntimeError("indicator archives missing or empty: " + ", ".join(missing))

    output_dir = (
        args.output_dir
        or PROJECT_ROOT / "data" / "backtests" / "staging" / f"nas-indicators-{args.run_id}"
    ).resolve()
    monthly_path = output_dir / "shsz-monthly-first-session-indicators-candidate.parquet"
    basic_path = output_dir / "shsz-stock-basic-current-snapshot-candidate.parquet"
    receipt_path = output_dir / "receipt.json"
    audit_path = args.audit_dir.resolve() / f"{args.run_id}-nas-indicator-catalog.json"
    ensure_new(output_dir, monthly_path, basic_path, receipt_path, audit_path)
    output_dir.mkdir(parents=True, exist_ok=False)
    args.audit_dir.resolve().mkdir(parents=True, exist_ok=True)

    started_at = now()
    archive_records: list[dict[str, Any]] = []
    monthly_frames: list[pd.DataFrame] = []
    null_counts: Counter[str] = Counter()
    exchange_rows: Counter[str] = Counter()
    unique_symbols: set[str] = set()
    total_rows = 0
    bse_rows_excluded = 0
    duplicate_symbol_date_rows = 0
    malformed_members: list[dict[str, Any]] = []

    for archive in archives:
        archive_hash = sha256_file(archive)
        year_first_by_month: dict[str, tuple[str, pd.DataFrame, str]] = {}
        member_count = 0
        year_rows = 0
        with zipfile.ZipFile(archive) as bundle:
            names = sorted(name for name in bundle.namelist() if name.endswith(".parquet"))
            if not names:
                raise RuntimeError(f"archive has no parquet members: {archive}")
            for name in names:
                raw = bundle.read(name)
                try:
                    table = pq.read_table(io.BytesIO(raw))
                    frame = table.to_pandas()
                except Exception as error:
                    malformed_members.append(
                        {"archive": str(archive), "member": name, "error": f"{type(error).__name__}: {error}"}
                    )
                    continue
                missing_columns = sorted(REQUIRED_COLUMNS - set(frame.columns))
                if missing_columns:
                    malformed_members.append(
                        {"archive": str(archive), "member": name, "error": "missing columns: " + ", ".join(missing_columns)}
                    )
                    continue
                member_count += 1
                dates = pd.to_datetime(frame["date"].astype(str), format="%Y%m%d", errors="coerce")
                in_window = dates.notna() & (dates >= start) & (dates <= end)
                frame = frame.loc[in_window].copy()
                if frame.empty:
                    continue
                frame["effective_date"] = dates.loc[in_window].dt.date
                frame["exchange"] = frame["code"].str[-2:].map({"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"})
                bse_rows_excluded += int((frame["exchange"] == "BSE").sum())
                frame = frame[frame["exchange"].isin(["SSE", "SZSE"])].copy()
                if frame.empty:
                    continue
                frame["symbol"] = frame["code"].str[:6]
                duplicate_symbol_date_rows += int(frame.duplicated(["symbol", "exchange", "effective_date"]).sum())
                total_rows += len(frame)
                year_rows += len(frame)
                unique_symbols.update(frame["code"].astype(str))
                exchange_rows.update(frame["exchange"].astype(str))
                for column in VALUE_COLUMNS:
                    null_counts[column] += int(frame[column].isna().sum())
                day = str(frame["effective_date"].iloc[0])
                month = day[:7]
                if month not in year_first_by_month or day < year_first_by_month[month][0]:
                    frame["source_id"] = SOURCE_ID
                    frame["source_archive"] = str(archive)
                    frame["source_archive_sha256"] = archive_hash
                    frame["source_member"] = name
                    frame["source_member_sha256"] = sha256_bytes(raw)
                    frame["known_at"] = None
                    frame["known_at_status"] = "UNAVAILABLE"
                    frame["license_status"] = "USER_SUPPLIED_LICENSE_REVIEW_REQUIRED"
                    frame["formal_backtest_eligible"] = False
                    year_first_by_month[month] = (day, frame, name)
        monthly_frames.extend(item[1] for item in year_first_by_month.values())
        archive_records.append(
            {
                "year": int(archive.stem),
                "path": str(archive),
                "bytes": archive.stat().st_size,
                "sha256": archive_hash,
                "parquet_members_read": member_count,
                "shsz_rows_read": year_rows,
                "monthly_first_sessions_selected": len(year_first_by_month),
            }
        )
        print(json.dumps(archive_records[-1], ensure_ascii=False), flush=True)

    if malformed_members:
        raise RuntimeError(f"{len(malformed_members)} malformed Parquet members; refusing partial catalog")
    if not monthly_frames:
        raise RuntimeError("no monthly SH/SZ indicator rows selected")
    monthly = pd.concat(monthly_frames, ignore_index=True)
    keep_columns = [
        "symbol",
        "exchange",
        "effective_date",
        *VALUE_COLUMNS,
        "source_id",
        "source_archive",
        "source_archive_sha256",
        "source_member",
        "source_member_sha256",
        "known_at",
        "known_at_status",
        "license_status",
        "formal_backtest_eligible",
    ]
    monthly = monthly[keep_columns].sort_values(["effective_date", "exchange", "symbol"])
    pq.write_table(pa.Table.from_pandas(monthly, preserve_index=False), monthly_path, compression="zstd")

    basic = pq.read_table(stock_basic_path).to_pandas()
    basic = basic[basic["exchange"].isin(["SSE", "SZSE"])].copy()
    basic["source_id"] = "USER_SUPPLIED_NAS_STOCK_BASIC_CURRENT_SNAPSHOT"
    basic["source_asset"] = str(stock_basic_path)
    basic["source_asset_sha256"] = sha256_file(stock_basic_path)
    basic["known_at"] = None
    basic["known_at_status"] = "CURRENT_SNAPSHOT_ONLY"
    basic["license_status"] = "USER_SUPPLIED_LICENSE_REVIEW_REQUIRED"
    basic["formal_backtest_eligible"] = False
    pq.write_table(pa.Table.from_pandas(basic, preserve_index=False), basic_path, compression="zstd")

    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_SOURCE_EVIDENCE_CATALOG",
        "status": "STAGING_ONLY_NOT_FORMAL_BACKTEST_INPUT",
        "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "requested_window": {"start_date": args.start_date, "end_date": args.end_date},
        "source": {
            "source_id": SOURCE_ID,
            "root": str(source_root),
            "readme": str(readme_path),
            "readme_sha256": sha256_file(readme_path),
            "stock_basic": str(stock_basic_path),
            "archive_records": archive_records,
            "archive_access": "READ_IN_PLACE_NO_EXTRACTION_NO_FULL_COPY",
            "license_status": "USER_SUPPLIED_LICENSE_REVIEW_REQUIRED",
        },
        "coverage": {
            "daily_shsz_rows_read": total_rows,
            "unique_shsz_symbols": len(unique_symbols),
            "exchange_rows": dict(sorted(exchange_rows.items())),
            "bse_rows_excluded": bse_rows_excluded,
            "duplicate_symbol_date_rows": duplicate_symbol_date_rows,
            "null_counts": dict(sorted(null_counts.items())),
            "monthly_first_session_rows": len(monthly),
            "monthly_first_session_count": int(monthly["effective_date"].nunique()),
            "minimum_effective_date": str(monthly["effective_date"].min()),
            "maximum_effective_date": str(monthly["effective_date"].max()),
            "current_stock_basic_shsz_rows": len(basic),
            "current_stock_basic_delisted_rows": int((basic["list_status"] == "D").sum()),
        },
        "outputs": {
            "monthly_indicators": {
                "path": relative(monthly_path),
                "bytes": monthly_path.stat().st_size,
                "sha256": sha256_file(monthly_path),
            },
            "stock_basic_current_snapshot": {
                "path": relative(basic_path),
                "bytes": basic_path.stat().st_size,
                "sha256": sha256_file(basic_path),
            },
        },
        "formal_assessment": {
            "formal_gate_closed": False,
            "status": "CANDIDATE_CROSSCHECK_ONLY",
            "useful_for": [
                "historical valuation/dividend-yield/share-count cross-checks",
                "monthly candidate diagnostics",
                "current instrument/controller sensitivity labels",
            ],
            "not_sufficient_for": [
                "historical point-in-time financial statement facts",
                "historical controller identity",
                "historical ST/trading-status proof",
                "formal backtest input until licence and knowledge-time are accepted",
            ],
        },
        "started_at": started_at,
        "completed_at": now(),
    }
    receipt["input_hash"] = hashlib.sha256(
        json.dumps(
            {
                "archives": {item["path"]: item["sha256"] for item in archive_records},
                "readme": receipt["source"]["readme_sha256"],
                "stock_basic": basic["source_asset_sha256"].iloc[0],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "event_type": "BACKTEST_NAS_INDICATOR_ARCHIVES_CATALOGED",
                "run_id": args.run_id,
                "status": receipt["status"],
                "input_hash": receipt["input_hash"],
                "receipt": relative(receipt_path),
                "scheduler_status": receipt["scheduler_status"],
                "created_at": receipt["completed_at"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
