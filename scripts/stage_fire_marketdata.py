#!/usr/bin/env python3
"""Stage the audited F.I.R.E. raw A-share release as non-formal Parquet.

This is a foreground, manual source-preparation command.  It deliberately
does not import into the formal backtest lake: the source's licence and
point-in-time coverage have not passed the project's formal-run gate.  The
output is useful only for field-level reconciliation and future source review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.feather as feather
import pyarrow.parquet as parquet


EXPECTED_ASSET_SHA256 = "38c40f84bb0a7f269b5775d8a80b712cf81138593f5bfb1bae0e91aff3e2228b"
SOURCE_ID = "fire-institute-fire-marketdata"
SOURCE_URI = "https://github.com/fire-institute/fire/releases/download/marketdata/AStockData.tar.gz"
REQUIRED_MEMBERS = (
    "fire_data/index.feather",
    "fire_data/columns.feather",
    "fire_data/open_dr.feather",
    "fire_data/high_dr.feather",
    "fire_data/low_dr.feather",
    "fire_data/close_dr.feather",
    "fire_data/volume_dr.feather",
    "fire_data/vwap_dr.feather",
    "fire_data/adj_factor.feather",
)
VALUE_FIELDS = ("open", "high", "low", "close", "volume", "vwap", "adj_factor")
SOURCE_FILES = {
    "open": "open_dr.feather",
    "high": "high_dr.feather",
    "low": "low_dr.feather",
    "close": "close_dr.feather",
    "volume": "volume_dr.feather",
    "vwap": "vwap_dr.feather",
    "adj_factor": "adj_factor.feather",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fail_if_exists(*paths: Path) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise RuntimeError("refusing to overwrite immutable staging output: " + ", ".join(existing))


def extract_required_members(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        names = set(bundle.getnames())
        missing = sorted(set(REQUIRED_MEMBERS) - names)
        if missing:
            raise RuntimeError(f"archive is missing required members: {', '.join(missing)}")
        for member in REQUIRED_MEMBERS:
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot read archive member: {member}")
            target = destination / member
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def field_values(source_dir: Path, field: str, symbols: list[str]) -> np.ndarray:
    table = feather.read_table(source_dir / SOURCE_FILES[field], columns=symbols)
    if table.num_columns != len(symbols):
        raise RuntimeError(f"unexpected column count in {SOURCE_FILES[field]}")
    values = np.column_stack(
        [table.column(symbol).to_numpy(zero_copy_only=False) for symbol in symbols]
    )
    return values.reshape(-1)


def build_table(
    dates: np.ndarray,
    symbols: list[str],
    source_dir: Path,
    asset_sha256: str,
) -> pa.Table:
    values = {field: field_values(source_dir, field, symbols) for field in VALUE_FIELDS}
    close = values["close"]
    valid = np.isfinite(close)
    if not valid.any():
        return pa.table({})

    row_count = len(dates) * len(symbols)
    if close.size != row_count:
        raise RuntimeError("raw field shape does not match the date/security axes")
    symbols_array = np.tile(np.asarray(symbols, dtype=str), len(dates))
    exchange_array = np.where(np.char.endswith(symbols_array, ".SH"), "SSE", "SZSE")
    date_array = np.repeat(dates, len(symbols))
    output: dict[str, Any] = {
        "symbol": pa.array(symbols_array[valid]),
        "exchange": pa.array(exchange_array[valid]),
        "trade_date": pa.array(date_array[valid]),
    }
    for field in VALUE_FIELDS:
        output[field] = pa.array(values[field][valid], type=pa.float64())
    count = int(valid.sum())
    output.update(
        {
            "source_id": pa.array([SOURCE_ID] * count),
            "source_uri": pa.array([SOURCE_URI] * count),
            "source_asset_sha256": pa.array([asset_sha256] * count),
            "license_status": pa.array(["REVIEW_REQUIRED"] * count),
            "known_at_status": pa.array(["UNAVAILABLE"] * count),
            "formal_backtest_eligible": pa.array([False] * count),
        }
    )
    return pa.table(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--symbol-batch-size", type=int, default=128)
    args = parser.parse_args()

    if args.symbol_batch_size < 1 or args.symbol_batch_size > 512:
        raise SystemExit("--symbol-batch-size must be between 1 and 512")
    archive = args.archive.resolve()
    output_dir = args.output_dir.resolve()
    parquet_path = output_dir / "fire-marketdata-raw-bars-v1.parquet"
    manifest_path = output_dir / "fire-marketdata-raw-bars-v1.staging.json"
    partial_path = parquet_path.with_suffix(".parquet.partial")
    if not archive.is_file():
        raise SystemExit(f"archive does not exist: {archive}")
    fail_if_exists(parquet_path, manifest_path, partial_path)

    asset_sha256 = sha256_file(archive)
    if asset_sha256 != EXPECTED_ASSET_SHA256:
        raise SystemExit(
            "raw archive SHA-256 does not match the audited release: "
            f"expected={EXPECTED_ASSET_SHA256}; actual={asset_sha256}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc)
    try:
        with tempfile.TemporaryDirectory(prefix="fire-stage-", dir=output_dir) as temporary:
            source_dir = Path(temporary)
            extract_required_members(archive, source_dir)
            date_table = feather.read_table(source_dir / "fire_data/index.feather")
            symbol_table = feather.read_table(source_dir / "fire_data/columns.feather")
            dates = np.asarray(date_table.column("trade_date").to_pylist(), dtype=str)
            symbols = symbol_table.column("stock_code").to_pylist()
            if not len(dates) or not len(symbols):
                raise RuntimeError("empty date or security axis")
            if any(not symbol.endswith((".SH", ".SZ")) for symbol in symbols):
                raise RuntimeError("unexpected non-SSE/SZSE symbol in F.I.R.E. staging source")

            writer: parquet.ParquetWriter | None = None
            staged_rows = 0
            try:
                for start in range(0, len(symbols), args.symbol_batch_size):
                    symbol_batch = symbols[start : start + args.symbol_batch_size]
                    table = build_table(dates, symbol_batch, source_dir / "fire_data", asset_sha256)
                    if not table.num_rows:
                        continue
                    if writer is None:
                        writer = parquet.ParquetWriter(
                            partial_path,
                            table.schema,
                            compression="zstd",
                            use_dictionary=["symbol", "exchange", "source_id", "license_status"],
                        )
                    writer.write_table(table, row_group_size=100_000)
                    staged_rows += table.num_rows
                    print(f"staged {min(start + len(symbol_batch), len(symbols))}/{len(symbols)} symbols", flush=True)
            finally:
                if writer is not None:
                    writer.close()
            if not staged_rows:
                raise RuntimeError("no valid raw close-price observations were staged")
            os.replace(partial_path, parquet_path)
    except Exception:
        # Keep a partial file as evidence for review; never silently delete it.
        raise

    metadata = parquet.read_metadata(parquet_path)
    manifest = {
        "schema_version": "1.0.0",
        "kind": "FIRE_MARKETDATA_STAGING_MANIFEST",
        "run_id": "20260910-005653-fire-marketdata-staging",
        "dataset": "raw_bars_staging",
        "dataset_version": "fire-marketdata-raw-bars-v1",
        "file_path": str(parquet_path),
        "sha256": sha256_file(parquet_path),
        "row_count": metadata.num_rows,
        "minimum_trade_date": str(dates[0]),
        "maximum_trade_date": str(dates[-1]),
        "security_count": len(symbols),
        "venue_counts": {
            "SSE": sum(symbol.endswith(".SH") for symbol in symbols),
            "SZSE": sum(symbol.endswith(".SZ") for symbol in symbols),
            "BSE": 0,
        },
        "fields": ["symbol", "exchange", "trade_date", *VALUE_FIELDS],
        "source": {
            "source_id": SOURCE_ID,
            "source_uri": SOURCE_URI,
            "asset_path": str(archive),
            "asset_sha256": asset_sha256,
            "license_status": "REVIEW_REQUIRED",
        },
        "point_in_time": {"known_at": "UNAVAILABLE", "formal_backtest_eligible": False},
        "formal_restrictions": [
            "STAGING_ONLY",
            "LICENSE_REVIEW_REQUIRED",
            "KNOWN_AT_UNAVAILABLE",
            "PRIMARY_WINDOW_INCOMPLETE",
            "BSE_NOT_COVERED",
            "NOT_IMPORTABLE_BY_backtest_data_import",
        ],
        "scheduler_status": "DISABLED",
        "started_at": started_at.isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
