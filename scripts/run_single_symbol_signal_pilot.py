#!/usr/bin/env python3
"""Run a deterministic, non-performance single-symbol signal pilot from staged real data."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRICE_PATH = (
    PROJECT_ROOT
    / "data/backtests/staging/nas-daily-20260910-155000-nas-daily-staging/shsz-daily-raw-bars-staging-v1.parquet"
)
DIVIDEND_PATH = (
    PROJECT_ROOT
    / "data/backtests/staging/20260911-004100-eastmoney-dividend-events-candidate/shsz-dividend-events-candidate.parquet"
)
CD_PATH = (
    PROJECT_ROOT
    / "data/backtests/staging/20260911-050000-cd-selection-candidate/monthly-selection-candidates.json"
)
POLICY_PATH = PROJECT_ROOT / "config/dividend-hurdle-backtest-v4.yaml"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def canonical_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def write_json_once(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"immutable output already exists: {path}")
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def classify(*, close: float | None, d_ttm: float, cd_rate: float | None) -> str:
    if close is None or close <= 0:
        return "BLOCKED_PRICE"
    if cd_rate is None:
        return "BLOCKED_CD_RATE"
    if d_ttm <= 0:
        return "DEFINITELY_INELIGIBLE_NO_TTM_DIVIDEND"
    if d_ttm / close < cd_rate + 0.03:
        return "DEFINITELY_INELIGIBLE_UPPER_BOUND"
    return "POTENTIAL_ENTRY_REQUIRES_D_MED3_AND_QUALITY_GATE"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--symbol", default="601288")
    parser.add_argument("--name", default="农业银行")
    args = parser.parse_args()

    for path in (PRICE_PATH, DIVIDEND_PATH, CD_PATH, POLICY_PATH):
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"required staged input missing or empty: {path}")
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    excluded = {str(item["symbol"]) for item in policy.get("universe_exclusions", [])}
    if args.symbol in excluded:
        raise RuntimeError(
            f"symbol is explicitly excluded by active policy: {args.symbol}"
        )

    output_dir = PROJECT_ROOT / "data/backtests/staging" / args.run_id
    report_path = output_dir / "single-symbol-signal-audit.json"
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-single-symbol-pilot.json"
    if output_dir.exists() or audit_path.exists():
        raise RuntimeError("run_id already exists; outputs are immutable")

    db = duckdb.connect()
    prices = {
        str(row[0]): float(row[1]) if row[1] is not None else None
        for row in db.execute(
            f"""
            SELECT effective_date::VARCHAR, close
            FROM read_parquet('{PRICE_PATH}')
            WHERE symbol = ? AND effective_date BETWEEN DATE '2016-01-04' AND DATE '2025-12-31'
            """,
            [args.symbol],
        ).fetchall()
    }
    dividends = [
        {
            "ex_date": date.fromisoformat(str(row[0])),
            "known_at": date.fromisoformat(str(row[1])),
            "cash_per_share": float(row[2]) / 10.0,
        }
        for row in db.execute(
            f"""
            SELECT ex_dividend_date::VARCHAR, known_at_candidate::VARCHAR, pretax_bonus_rmb
            FROM read_parquet('{DIVIDEND_PATH}')
            WHERE security_code = ? AND ex_dividend_date IS NOT NULL
              AND known_at_candidate IS NOT NULL AND pretax_bonus_rmb IS NOT NULL
            ORDER BY ex_dividend_date
            """,
            [args.symbol],
        ).fetchall()
    ]
    checkpoints = json.loads(CD_PATH.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        signal_date = date.fromisoformat(checkpoint["signal_date"])
        close = prices.get(signal_date.isoformat())
        included = [
            item
            for item in dividends
            if signal_date - timedelta(days=365) <= item["ex_date"] <= signal_date
            and item["known_at"] <= signal_date
        ]
        d_ttm = sum(float(item["cash_per_share"]) for item in included)
        cd_rate = (
            float(checkpoint["median_rate_decimal"])
            if checkpoint.get("covered")
            and checkpoint.get("median_rate_decimal") is not None
            else None
        )
        status = classify(close=close, d_ttm=d_ttm, cd_rate=cd_rate)
        rows.append(
            {
                "signal_date": signal_date.isoformat(),
                "raw_close_cny": close,
                "d_ttm_cny_per_share": round(d_ttm, 8),
                "upper_bound_dividend_yield": round(d_ttm / close, 10)
                if close and close > 0
                else None,
                "cd_rate": cd_rate,
                "hurdle_rate": round(cd_rate + 0.03, 10)
                if cd_rate is not None
                else None,
                "status": status,
                "dividend_event_count_ttm": len(included),
            }
        )

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    created_at = datetime.now(UTC).isoformat()
    report = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_SINGLE_SYMBOL_SIGNAL_PILOT",
        "status": "STAGING_SIGNAL_AUDIT_NOT_PERFORMANCE",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "symbol": args.symbol,
        "name": args.name,
        "window": {"start": "2016-01-04", "end": "2025-12-31"},
        "method": {
            "formula_checked": "D_ttm / raw_close >= R_cd_signal + 0.0300",
            "interpretation": "D_ttm yield is only an upper bound because D_cons=min(D_ttm,D_med3)",
            "quality_gate": "NOT_APPLIED_NOT_FROZEN",
            "d_med3": "NOT_APPLIED_NOT_FULLY_RESOLVED",
            "trades_or_performance": False,
            "future_data_imputation": False,
        },
        "summary": {
            "monthly_checkpoints": len(rows),
            "price_rows_in_window": len(prices),
            "dividend_events_for_symbol": len(dividends),
            "status_counts": counts,
        },
        "checkpoints": rows,
        "created_at": created_at,
    }
    write_json_once(report_path, report)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "status": report["status"],
        "scheduler_status": report["scheduler_status"],
        "created_at": created_at,
        "inputs": [
            {"path": str(path.relative_to(PROJECT_ROOT)), "sha256": digest(path)}
            for path in (PRICE_PATH, DIVIDEND_PATH, CD_PATH, POLICY_PATH)
        ],
        "output": {
            "path": str(report_path.relative_to(PROJECT_ROOT)),
            "sha256": digest(report_path),
        },
        "formal_backtest_eligibility": {
            "eligible": False,
            "reasons": [
                "D_med3 and the bank quality gate are not frozen",
                "58 of 120 monthly CD checkpoints are unavailable",
                "staged price and dividend datasets are not promoted to formal point-in-time inputs",
                "no trade, fee, ledger, PnL, or performance was generated",
            ],
        },
    }
    write_json_once(receipt_path, receipt)
    audit_core = {
        "event_id": f"{args.run_id}-audit",
        "run_id": args.run_id,
        "event_type": "BACKTEST_SINGLE_SYMBOL_SIGNAL_PILOT_COMPLETED",
        "actor": "deterministic-local-python",
        "occurred_at": created_at,
        "payload": {
            "symbol": args.symbol,
            "status": report["status"],
            "receipt_path": str(receipt_path.relative_to(PROJECT_ROOT)),
            "receipt_sha256": digest(receipt_path),
            "performance_generated": False,
        },
        "prev_event_hash": None,
    }
    write_json_once(
        audit_path, {**audit_core, "event_hash": canonical_hash(audit_core)}
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
