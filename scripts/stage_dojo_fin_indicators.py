#!/usr/bin/env python3
"""Stage a fixed AlphaDojo financial-indicator snapshot for SH/SZ audit.

This manual-only task does not schedule work, call an LLM, mutate the formal
backtest lake, or mark any formal data gate closed.  It keeps the provider
symbol, normalizes .SS to .SH, preserves publication dates, and emits an
append-only candidate parquet plus a machine-readable receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_ID = "AlphaDojo/dojo_fin_indicators"
DATASET_REVISION = "0226a4c8beb44f0b78afdd75aa63509e19f9068f"
PRIMARY_START = pd.Timestamp("2016-01-04")
PRIMARY_END = pd.Timestamp("2025-12-31")

KEEP_COLUMNS = [
    "symbol",
    "report_type",
    "report_date",
    "std_report_date",
    "public_date",
    "security_name",
    "org_type",
    "currency",
    "total_operating_revenue",
    "net_profit_attr_parent",
    "net_profit_deducted",
    "roe_weighted",
    "roe_deducted",
    "roa",
    "net_margin",
    "gross_margin",
    "ocf_to_rev",
    "current_ratio",
    "quick_ratio",
    "cash_flow_ratio",
    "debt_asset_ratio",
    "equity_multiplier",
    "capital_adequacy_ratio",
    "core_tier1_capital_ratio",
    "npl_ratio",
    "loan_provision_ratio",
    "liquidity_coverage_ratio",
    "net_funding_ratio",
    "solvency_adequacy_ratio",
    "broker_net_capital",
    "net_capital_to_assets",
    "net_capital_liabilities",
    "net_assets_liabilities",
    "pe_ttm",
    "pb_ttm",
    "dividend_rate",
    "divi_ratio",
    "report_year",
    "fiscal_year",
    "kline_t",
    "close",
    "total_market_cap",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    output_dir = args.output_dir.resolve()
    output_path = output_dir / "shsz-fin-indicators-candidate.parquet"
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-dojo-fin-indicators-stage.json"
    collisions = [path for path in (output_dir, output_path, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))
    if not input_path.is_file() or input_path.stat().st_size == 0:
        raise RuntimeError(f"input is missing or empty: {input_path}")

    parquet = pq.ParquetFile(input_path)
    missing = sorted(set(KEEP_COLUMNS) - set(parquet.schema_arrow.names))
    if missing:
        raise RuntimeError(f"required columns missing: {missing}")
    frame = parquet.read(columns=KEEP_COLUMNS).to_pandas()
    source_rows = len(frame)
    frame["provider_symbol"] = frame["symbol"].astype("string")
    frame["symbol"] = frame["provider_symbol"].str.replace(r"\.SS$", ".SH", regex=True)
    frame["report_date"] = pd.to_datetime(frame["report_date"], errors="coerce")
    frame["public_date"] = pd.to_datetime(frame["public_date"], errors="coerce")
    frame = frame[
        frame["symbol"].str.match(r"^\d{6}\.(SH|SZ)$", na=False)
        & frame["report_date"].between(PRIMARY_START, PRIMARY_END, inclusive="both")
    ].copy()
    frame["known_at_candidate"] = frame["public_date"]
    frame["point_in_time_status"] = "UNVERIFIED_CURRENT_SNAPSHOT_WITH_PUBLIC_DATE"
    frame["formal_backtest_eligible"] = False
    frame["dataset_id"] = DATASET_ID
    frame["dataset_revision"] = DATASET_REVISION
    frame = frame.sort_values(["symbol", "report_date", "report_type", "public_date"], na_position="last")

    output_dir.mkdir(parents=True, exist_ok=False)
    frame.to_parquet(output_path, index=False, compression="zstd")
    input_sha256 = sha256_file(input_path)
    output_sha256 = sha256_file(output_path)
    dated = frame.dropna(subset=["public_date"])
    public_before_report = int((dated["public_date"] < dated["report_date"]).sum())
    pk_duplicates = int(frame.duplicated(["symbol", "report_type", "report_date"], keep=False).sum())
    coverage = {
        "source_rows": source_rows,
        "shsz_primary_window_rows": len(frame),
        "symbols": int(frame["symbol"].nunique()),
        "report_date_min": None if frame.empty else frame["report_date"].min().date().isoformat(),
        "report_date_max": None if frame.empty else frame["report_date"].max().date().isoformat(),
        "public_date_min": None if dated.empty else dated["public_date"].min().date().isoformat(),
        "public_date_max": None if dated.empty else dated["public_date"].max().date().isoformat(),
        "public_date_non_null": int(frame["public_date"].notna().sum()),
        "public_date_missing": int(frame["public_date"].isna().sum()),
        "public_before_report": public_before_report,
        "duplicate_primary_key_rows": pk_duplicates,
        "exchange_rows": frame["symbol"].str[-2:].value_counts().sort_index().to_dict(),
        "org_type_rows": {str(key): int(value) for key, value in frame["org_type"].fillna("UNKNOWN").value_counts().items()},
    }
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_EXTERNAL_FINANCIAL_INDICATORS_STAGE",
        "status": "STAGING_ONLY_NOT_FORMAL_BACKTEST_INPUT",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "dataset": {"id": DATASET_ID, "revision": DATASET_REVISION, "license_tag": "apache-2.0"},
        "inputs": [{"path": str(input_path), "bytes": input_path.stat().st_size, "sha256": input_sha256}],
        "outputs": [{"path": str(output_path.relative_to(PROJECT_ROOT)), "rows": len(frame), "sha256": output_sha256}],
        "coverage": coverage,
        "limitations": [
            "Observed SH/SZ report coverage starts in 2023, so 2016-2022 remains missing.",
            "The dataset card does not identify field-level original sources or restatement/version semantics.",
            "A publication date alone does not prove the metric value is the original point-in-time value.",
            "Rows with missing public_date cannot enter a point-in-time decision.",
            "Duplicate symbol/report_type/report_date rows require deterministic conflict resolution before use.",
        ],
        "created_at": now(),
    }
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "event_type": "BACKTEST_DOJO_FIN_INDICATORS_STAGED",
                "run_id": args.run_id,
                "status": receipt["status"],
                "receipt": str(receipt_path.relative_to(PROJECT_ROOT)),
                "input_hash": input_sha256,
                "result_hash": output_sha256,
                "scheduler_status": "DISABLED_MANUAL_ONLY",
                "created_at": receipt["created_at"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"run_id": args.run_id, **coverage}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
