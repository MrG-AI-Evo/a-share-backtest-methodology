#!/usr/bin/env python3
"""Manually collect public SH/SZ daily bars into an auditable staging area.

This is a source-acquisition utility, not a backtest input generator.  It is
deliberately unable to write to data/backtests/parquet or to start a backtest.
The historical universe comes from the separately audited F.I.R.E. raw
staging file, so currently delisted symbols are not silently lost just because
they are absent from a live quote list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRE_UNIVERSE = (
    PROJECT_ROOT
    / "data/backtests/staging/fire-20250910/fire-marketdata-raw-bars-v1.parquet"
)
OUTPUT_ROOT = PROJECT_ROOT / "data/backtests/staging"
TOOLKIT_REPOSITORY = "https://github.com/tiantianlaolao/astock-data-toolkit"
TOOLKIT_COMMIT = "80dbdba675201163b0d6ab36633971093f3b4be6"
TOOLKIT_LICENSE = "MIT"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fail_if_exists(*paths: Path) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise RuntimeError("追加式运行拒绝覆盖已有产物: " + ", ".join(existing))


def exchange_for(symbol: str) -> str:
    if symbol.startswith(("600", "601", "603", "605", "688", "689")):
        return "SSE"
    if symbol.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZSE"
    raise ValueError(f"非沪深股票代码，拒绝采集: {symbol}")


def akshare_symbol(symbol: str, exchange: str) -> str:
    return f"{'sh' if exchange == 'SSE' else 'sz'}{symbol}"


def read_historical_symbols() -> list[str]:
    if not FIRE_UNIVERSE.is_file():
        raise RuntimeError(f"缺少已审计的历史证券底稿: {FIRE_UNIVERSE}")
    table = pq.read_table(FIRE_UNIVERSE, columns=["symbol"])
    values = sorted({str(value).zfill(6) for value in table.column("symbol").to_pylist()})
    return [symbol for symbol in values if exchange_for(symbol) in {"SSE", "SZSE"}]


def normalize_bars(frame: pd.DataFrame, symbol: str, exchange: str, retrieved_at: str) -> pd.DataFrame:
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"{symbol} 返回缺少字段: {sorted(missing)}")
    normalized = frame.copy()
    normalized["effective_date"] = pd.to_datetime(normalized["date"], errors="coerce").dt.date
    normalized = normalized.dropna(subset=["effective_date"])
    for column in ("open", "high", "low", "close", "volume", "amount"):
        if column in normalized.columns:
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        elif column == "amount":
            normalized[column] = None
    normalized["symbol"] = symbol
    normalized["exchange"] = exchange
    normalized["source_id"] = "AKSHARE_TENCENT_PUBLIC"
    normalized["source_uri"] = "akshare.stock_zh_a_daily/tencent"
    normalized["retrieved_at"] = retrieved_at
    normalized["adjustment"] = "UNADJUSTED_REQUESTED"
    normalized["known_at"] = None
    normalized["is_synthetic"] = False
    columns = [
        "symbol", "exchange", "effective_date", "open", "high", "low", "close", "volume",
        "amount", "source_id", "source_uri", "retrieved_at", "adjustment", "known_at",
        "is_synthetic",
    ]
    return normalized[columns].sort_values("effective_date").reset_index(drop=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="手动采集沪深公开日线至隔离暂存区")
    parser.add_argument("--run-id", required=True, help="不可重复的人工运行标识")
    parser.add_argument("--start-date", default="2021-01-01")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--limit", type=int, default=0, help="仅采集前 N 只；0 为历史底稿的全部沪深股票")
    parser.add_argument("--interval-seconds", type=float, default=0.45)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 0 or args.interval_seconds < 0:
        raise RuntimeError("limit 和 interval-seconds 必须为非负数")
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    if start > end:
        raise RuntimeError("start-date 不得晚于 end-date")
    output_dir = OUTPUT_ROOT / f"akshare-tencent-{args.run_id}"
    output_dir.mkdir(parents=True, exist_ok=False)
    output_path = output_dir / "shsz-daily-public-staging.parquet"
    errors_path = output_dir / "errors.json"
    receipt_path = output_dir / "receipt.json"
    fail_if_exists(output_path, errors_path, receipt_path)

    symbols = read_historical_symbols()
    if args.limit:
        symbols = symbols[: args.limit]
    if not symbols:
        raise RuntimeError("没有可采集的沪深证券")

    retrieved_at = utc_now()
    errors: list[dict[str, str]] = []
    rows_written = 0
    writer: pq.ParquetWriter | None = None
    partial_path = output_path.with_suffix(".parquet.partial")
    try:
        for index, symbol in enumerate(symbols, start=1):
            exchange = exchange_for(symbol)
            try:
                raw = ak.stock_zh_a_daily(
                    symbol=akshare_symbol(symbol, exchange),
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="",
                )
                if raw is None or raw.empty:
                    errors.append({"symbol": symbol, "error": "EMPTY_RESPONSE"})
                else:
                    data = normalize_bars(raw, symbol, exchange, retrieved_at)
                    data = data[
                        (data["effective_date"] >= start.date()) & (data["effective_date"] <= end.date())
                    ]
                    if data.empty:
                        errors.append({"symbol": symbol, "error": "NO_ROWS_IN_REQUESTED_RANGE"})
                    else:
                        table = pa.Table.from_pandas(data, preserve_index=False)
                        if writer is None:
                            writer = pq.ParquetWriter(partial_path, table.schema, compression="zstd")
                        writer.write_table(table)
                        rows_written += len(data)
            except Exception as error:  # Source failures are evidence, not silent skips.
                errors.append({"symbol": symbol, "error": f"{type(error).__name__}: {error}"})
            print(json.dumps({"symbol": symbol, "index": index, "total": len(symbols), "rows_written": rows_written}, ensure_ascii=False), flush=True)
            if index < len(symbols) and args.interval_seconds:
                time.sleep(args.interval_seconds)
    finally:
        if writer is not None:
            writer.close()

    if rows_written == 0 or not partial_path.is_file():
        partial_path.unlink(missing_ok=True)
        errors_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError("未获得任何有效日线；错误清单已保存，拒绝生成空数据集")
    os.replace(partial_path, output_path)
    errors_path.write_text(json.dumps(errors, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    metadata = pq.read_metadata(output_path)
    receipt: dict[str, Any] = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "kind": "PUBLIC_MARKETDATA_STAGING",
        "status": "STAGING_ONLY_NOT_FORMAL_BACKTEST_INPUT",
        "scope": "SSE_AND_SZSE_ONLY",
        "requested_window": {"start_date": args.start_date, "end_date": args.end_date},
        "retrieved_at": retrieved_at,
        "source_lineage": {
            "collector_reference": {"repository": TOOLKIT_REPOSITORY, "commit": TOOLKIT_COMMIT, "license": TOOLKIT_LICENSE},
            "runtime_adapter": "AKShare stock_zh_a_daily",
            "underlying_source_claim": "Tencent public daily endpoint",
        },
        "limitations": [
            "known_at is unavailable; point-in-time assertion is false",
            "this collection cannot prove historical ST, delisting, financial publication, dividends, or corporate actions",
            "no formal import or performance calculation is permitted from this output",
        ],
        "output": {
            "parquet": str(output_path.relative_to(PROJECT_ROOT)),
            "sha256": sha256_file(output_path),
            "row_count": metadata.num_rows,
            "errors": str(errors_path.relative_to(PROJECT_ROOT)),
            "failed_or_empty_symbols": len(errors),
        },
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
