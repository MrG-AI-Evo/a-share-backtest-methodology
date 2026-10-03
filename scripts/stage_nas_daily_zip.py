#!/usr/bin/env python3
"""Stream user-supplied NAS daily-bar ZIP data into immutable staging Parquet.

This foreground command never extracts the source ZIP and never writes to the
formal backtest lake. ``known_at`` remains unavailable, so its result cannot
be used for formal backtest performance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ID = "USER_SUPPLIED_NAS_DAILY_ZIP"
MEMBER_PATTERN = re.compile(r"^(?P<code>\d{6})\.(?P<venue>SH|SZ|BJ)\.csv$")
OUTPUT_COLUMNS = (
    "symbol", "exchange", "effective_date", "open", "high", "low", "close", "prev_close",
    "volume_lots", "volume_shares", "amount_cny", "adjustment_factor", "upper_limit", "lower_limit",
    "source_id", "source_uri", "source_asset_sha256", "raw_member", "raw_member_crc32",
    "retrieved_at", "known_at", "known_at_status", "license_status", "formal_backtest_eligible",
    "is_synthetic",
)
REQUIRED_INPUT_COLUMNS = ("股票代码", "交易日", "开盘价", "最高价", "最低价", "收盘价", "成交量（手）", "成交额（千元）")
INPUT_COLUMN_MAP = {
    "股票代码": "symbol", "交易日": "effective_date", "开盘价": "open", "最高价": "high",
    "最低价": "low", "收盘价": "close", "昨收价": "prev_close", "成交量（手）": "volume_lots",
    "成交额（千元）": "amount_cny", "复权因子": "adjustment_factor", "当日涨停价": "upper_limit",
    "当日跌停价": "lower_limit",
}
PARQUET_SCHEMA = pa.schema([
    ("symbol", pa.string()), ("exchange", pa.string()), ("effective_date", pa.date32()),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()), ("close", pa.float64()),
    ("prev_close", pa.float64()), ("volume_lots", pa.float64()), ("volume_shares", pa.float64()),
    ("amount_cny", pa.float64()), ("adjustment_factor", pa.float64()), ("upper_limit", pa.float64()),
    ("lower_limit", pa.float64()), ("source_id", pa.string()), ("source_uri", pa.string()),
    ("source_asset_sha256", pa.string()), ("raw_member", pa.string()), ("raw_member_crc32", pa.string()),
    ("retrieved_at", pa.string()), ("known_at", pa.string()), ("known_at_status", pa.string()),
    ("license_status", pa.string()), ("formal_backtest_eligible", pa.bool_()), ("is_synthetic", pa.bool_()),
])


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def fail_if_exists(*paths: Path) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise RuntimeError("refusing to overwrite immutable staging output: " + ", ".join(existing))


def input_members(bundle: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str, str]]:
    members: list[tuple[zipfile.ZipInfo, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for info in bundle.infolist():
        match = MEMBER_PATTERN.match(Path(info.filename).name)
        if not match or match.group("venue") == "BJ":
            continue
        symbol = match.group("code")
        exchange = "SSE" if match.group("venue") == "SH" else "SZSE"
        if (symbol, exchange) in seen:
            raise RuntimeError(f"ZIP 内存在重复证券文件: {symbol}.{match.group('venue')}")
        seen.add((symbol, exchange))
        members.append((info, symbol, exchange))
    if not members:
        raise RuntimeError("ZIP 内没有可用的沪深日线 CSV")
    return sorted(members, key=lambda item: (item[2], item[1]))


def number_column(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[name], errors="coerce")


def normalize_member(
    frame: pd.DataFrame, *, symbol: str, exchange: str, info: zipfile.ZipInfo, archive: Path,
    asset_sha256: str, retrieved_at: str, start: pd.Timestamp, end: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, int]]:
    missing = sorted(set(REQUIRED_INPUT_COLUMNS) - set(frame.columns))
    if missing:
        raise RuntimeError("缺少必要列: " + ", ".join(missing))
    data = pd.DataFrame(index=frame.index)
    data["symbol"] = symbol
    data["exchange"] = exchange
    data["effective_date"] = pd.to_datetime(frame["交易日"].astype(str), format="%Y%m%d", errors="coerce")
    for input_name, output_name in INPUT_COLUMN_MAP.items():
        if output_name not in {"symbol", "effective_date"}:
            data[output_name] = number_column(frame, input_name)
    data["volume_shares"] = data["volume_lots"] * 100.0
    data["amount_cny"] = data["amount_cny"] * 1000.0
    data["source_id"] = SOURCE_ID
    data["source_uri"] = archive.as_uri()
    data["source_asset_sha256"] = asset_sha256
    data["raw_member"] = info.filename
    data["raw_member_crc32"] = f"{info.CRC:08x}"
    data["retrieved_at"] = retrieved_at
    data["known_at"] = None
    data["known_at_status"] = "UNAVAILABLE"
    data["license_status"] = "USER_SUPPLIED_LICENSE_REVIEW_REQUIRED"
    data["formal_backtest_eligible"] = False
    data["is_synthetic"] = False
    in_window = data["effective_date"].notna() & (data["effective_date"] >= start) & (data["effective_date"] <= end)
    data = data.loc[in_window].copy()
    invalid_close = data["close"].isna() | (data["close"] <= 0)
    ohlc_violation = (
        data["high"].notna() & data["low"].notna() & data["open"].notna() & data["close"].notna()
        & ((data["high"] < data[["open", "close"]].max(axis=1)) | (data["low"] > data[["open", "close"]].min(axis=1)))
    )
    negative_volume = data["volume_lots"].notna() & (data["volume_lots"] < 0)
    report = {
        "rows_in_window": len(data), "invalid_or_missing_close_rows_dropped": int(invalid_close.sum()),
        "ohlc_relation_anomalies_retained": int(ohlc_violation.sum()),
        "negative_volume_anomalies_retained": int(negative_volume.sum()),
    }
    return data.loc[~invalid_close, list(OUTPUT_COLUMNS)].sort_values("effective_date"), report


def table_from_frame(frame: pd.DataFrame) -> pa.Table:
    values: dict[str, Any] = {}
    for field in PARQUET_SCHEMA:
        values[field.name] = (
            pa.array(frame[field.name].dt.date.tolist(), type=field.type)
            if field.name == "effective_date"
            else pa.array(frame[field.name].tolist(), type=field.type, from_pandas=True)
        )
    return pa.Table.from_pydict(values, schema=PARQUET_SCHEMA)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--start-date", default="2016-01-04")
    parser.add_argument("--end-date", default="2025-12-31")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--audit-dir", type=Path, default=PROJECT_ROOT / "data" / "audit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    archive = args.archive.expanduser().resolve()
    if not archive.is_file():
        raise RuntimeError(f"原始 ZIP 不存在: {archive}")
    start, end = pd.Timestamp(args.start_date), pd.Timestamp(args.end_date)
    if start > end:
        raise RuntimeError("start-date 不得晚于 end-date")
    output_dir = (args.output_dir or PROJECT_ROOT / "data" / "backtests" / "staging" / f"nas-daily-{args.run_id}").resolve()
    parquet_path, receipt_path, errors_path = (output_dir / "shsz-daily-raw-bars-staging-v1.parquet", output_dir / "receipt.json", output_dir / "errors.json")
    audit_path = args.audit_dir.resolve() / f"{args.run_id}-nas-daily-zip-staging.json"
    partial_path = parquet_path.with_suffix(".parquet.partial")
    fail_if_exists(output_dir, parquet_path, receipt_path, errors_path, audit_path, partial_path)
    asset_sha256, started_at, retrieved_at = sha256_file(archive), utc_now(), utc_now()
    output_dir.mkdir(parents=True, exist_ok=False)
    args.audit_dir.resolve().mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, str]] = []
    member_receipts: list[dict[str, Any]] = []
    totals = {"csv_members_shsz": 0, "csv_members_bj_ignored": 0, "rows_written": 0, "invalid_or_missing_close_rows_dropped": 0, "ohlc_relation_anomalies_retained": 0, "negative_volume_anomalies_retained": 0}
    dates: list[pd.Timestamp] = []
    writer: pq.ParquetWriter | None = None
    try:
        with zipfile.ZipFile(archive) as bundle:
            all_csv_count = sum(Path(info.filename).suffix.lower() == ".csv" for info in bundle.infolist())
            members = input_members(bundle)
            totals["csv_members_shsz"], totals["csv_members_bj_ignored"] = len(members), all_csv_count - len(members)
            for index, (info, symbol, exchange) in enumerate(members, start=1):
                try:
                    with bundle.open(info) as source:
                        raw = pd.read_csv(source, encoding="utf-8-sig", dtype=str)
                    normalized, report = normalize_member(raw, symbol=symbol, exchange=exchange, info=info, archive=archive, asset_sha256=asset_sha256, retrieved_at=retrieved_at, start=start, end=end)
                    if not normalized.empty:
                        if writer is None:
                            writer = pq.ParquetWriter(partial_path, PARQUET_SCHEMA, compression="zstd")
                        writer.write_table(table_from_frame(normalized), row_group_size=100_000)
                        totals["rows_written"] += len(normalized)
                        dates.extend([normalized["effective_date"].min(), normalized["effective_date"].max()])
                    for key in ("invalid_or_missing_close_rows_dropped", "ohlc_relation_anomalies_retained", "negative_volume_anomalies_retained"):
                        totals[key] += report[key]
                    member_receipts.append({"symbol": symbol, "exchange": exchange, "member": info.filename, **report})
                except Exception as error:
                    errors.append({"symbol": symbol, "exchange": exchange, "member": info.filename, "error": f"{type(error).__name__}: {error}"})
                print(json.dumps({"index": index, "total": len(members), "symbol": symbol, "rows_written": totals["rows_written"], "errors": len(errors)}, ensure_ascii=False), flush=True)
    finally:
        if writer is not None:
            writer.close()
    errors_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if totals["rows_written"] == 0 or not partial_path.is_file():
        raise RuntimeError("没有写入有效沪深日线；已保留错误清单，拒绝生成空暂存数据集")
    os.replace(partial_path, parquet_path)
    metadata = pq.read_metadata(parquet_path)
    receipt = {
        "schema_version": "1.0.0", "run_id": args.run_id, "kind": "USER_SUPPLIED_NAS_DAILY_ZIP_STAGING",
        "status": "STAGING_ONLY_NOT_FORMAL_BACKTEST_INPUT", "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "requested_window": {"start_date": args.start_date, "end_date": args.end_date},
        "source": {"source_id": SOURCE_ID, "archive_path": str(archive), "archive_sha256": asset_sha256, "archive_format": "ZIP_STREAMED_NO_EXTRACTION", "license_status": "USER_SUPPLIED_LICENSE_REVIEW_REQUIRED"},
        "point_in_time": {"known_at": "UNAVAILABLE", "formal_backtest_eligible": False},
        "formal_restrictions": ["STAGING_ONLY", "LICENSE_REVIEW_REQUIRED", "KNOWN_AT_UNAVAILABLE", "NO_HISTORICAL_ST_OR_DELISTING_PROOF", "NO_DIVIDENDS_OR_FINANCIAL_PUBLICATION_HISTORY", "NOT_IMPORTABLE_BY_backtest_data_import"],
        "output": {"parquet": str(parquet_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(parquet_path), "row_count": metadata.num_rows, "minimum_effective_date": min(dates).date().isoformat(), "maximum_effective_date": max(dates).date().isoformat(), "columns": list(OUTPUT_COLUMNS), "errors": str(errors_path.relative_to(PROJECT_ROOT)), "failed_members": len(errors)},
        "quality_summary": totals, "member_receipts": member_receipts, "scheduler_status": "DISABLED_MANUAL_ONLY", "started_at": started_at, "created_at": utc_now(),
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.write_text(json.dumps({"schema_version": "1.0.0", "event_type": "BACKTEST_SOURCE_STAGED", "run_id": args.run_id, "status": receipt["status"], "source_asset_sha256": asset_sha256, "receipt": str(receipt_path.relative_to(PROJECT_ROOT)), "created_at": receipt["created_at"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
