#!/usr/bin/env python3
"""Evaluate deterministic within-bank CD product-selection candidates.

This is a foreground-only diagnostic. It never creates a formal signal series and
does not change the strategy policy. The output exists to quantify how much of the
monthly hurdle can be supported once a user-approved within-bank selection rule is
frozen.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import statistics
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BANKS = ("ICBC", "ABC", "BOC", "CCB")
PRIMARY_START = date(2016, 1, 4)
PRIMARY_END = date(2025, 12, 31)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument(
        "--trading-status-glob",
        required=True,
        help="Glob matching immutable BaoStock trading-status Parquet partitions",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def as_date(value: object) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def tenor_order(signal_date: date) -> tuple[float, ...]:
    if signal_date <= date(2017, 12, 31):
        return (3.0,)
    return (5.0, 3.0)


def candidate_rows(rows: list[dict[str, Any]], bank: str, tenor: float, signal_date: date) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        if row.get("bank") != bank or row.get("customer_scope") != "PERSONAL":
            continue
        if float(row.get("tenor_years") or -1) != tenor:
            continue
        if not row.get("observation_eligible_candidate"):
            continue
        valid_from = as_date(row.get("valid_from"))
        valid_to = as_date(row.get("valid_to"))
        known_at = as_date(row.get("known_at_candidate"))
        if not valid_from or not valid_to or not known_at:
            continue
        if not (valid_from <= signal_date <= valid_to and known_at <= signal_date):
            continue
        if row.get("annual_rate_decimal") is None:
            continue
        result.append(row)
    return result


def choose(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    # Candidate rule only: lowest disclosed purchase threshold, then the lowest
    # rate (conservative if several products share that threshold), then stable
    # text/source tie-breakers. Missing thresholds sort last.
    return min(
        rows,
        key=lambda row: (
            float(row["minimum_purchase_cny"]) if row.get("minimum_purchase_cny") is not None else float("inf"),
            float(row["annual_rate_decimal"]),
            str(row.get("product_name") or ""),
            str(row.get("source_uri") or ""),
        ),
    )


def main() -> int:
    args = parse_args()
    observations_path = args.observations.resolve()
    calendar_glob = str((PROJECT_ROOT / args.trading_status_glob).resolve())
    calendar_paths = sorted(Path(path).resolve() for path in glob.glob(calendar_glob))
    output_dir = args.output_dir.resolve()
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-cd-selection-candidate-audit.json"
    if output_dir.exists() or audit_path.exists():
        raise RuntimeError("refusing to overwrite immutable output")
    if not observations_path.is_file() or observations_path.stat().st_size == 0:
        raise RuntimeError(f"missing or empty input: {observations_path}")
    if not calendar_paths:
        raise RuntimeError(f"trading-status glob matched no files: {calendar_glob}")
    for path in calendar_paths:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"missing or empty input: {path}")

    rows = json.loads(observations_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise RuntimeError("observations must be a JSON array")

    con = duckdb.connect()
    calendar_sql_path = calendar_glob.replace("'", "''")
    checkpoints = [
        value[0]
        for value in con.execute(
            f"""
            SELECT min(CAST(trade_date AS DATE)) AS signal_date
            FROM read_parquet('{calendar_sql_path}', hive_partitioning=false)
            WHERE CAST(trade_date AS DATE) BETWEEN DATE '{PRIMARY_START}' AND DATE '{PRIMARY_END}'
            GROUP BY year(CAST(trade_date AS DATE)), month(CAST(trade_date AS DATE))
            ORDER BY signal_date
            """
        ).fetchall()
    ]

    results: list[dict[str, Any]] = []
    for signal_date in checkpoints:
        record: dict[str, Any] = {
            "signal_date": signal_date.isoformat(),
            "covered": False,
            "chosen_tenor_years": None,
            "median_rate_decimal": None,
            "selected": [],
            "candidate_counts": {},
        }
        for tenor in tenor_order(signal_date):
            selected: list[dict[str, Any]] = []
            for bank in BANKS:
                candidates = candidate_rows(rows, bank, tenor, signal_date)
                record["candidate_counts"][f"{bank}:{tenor:g}y"] = len(candidates)
                chosen = choose(candidates)
                if chosen is None:
                    continue
                selected.append(
                    {
                        "bank": bank,
                        "product_name": chosen.get("product_name"),
                        "annual_rate_decimal": chosen.get("annual_rate_decimal"),
                        "minimum_purchase_cny": chosen.get("minimum_purchase_cny"),
                        "valid_from": chosen.get("valid_from"),
                        "valid_to": chosen.get("valid_to"),
                        "source_uri": chosen.get("source_uri"),
                        "source_asset_sha256": chosen.get("source_asset_sha256"),
                    }
                )
            if len(selected) >= 2:
                record["covered"] = True
                record["chosen_tenor_years"] = tenor
                record["median_rate_decimal"] = statistics.median(
                    float(item["annual_rate_decimal"]) for item in selected
                )
                record["selected"] = selected
                break
        results.append(record)

    covered = sum(bool(row["covered"]) for row in results)
    output_dir.mkdir(parents=True, exist_ok=False)
    results_path = output_dir / "monthly-selection-candidates.json"
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_CD_WITHIN_BANK_SELECTION_CANDIDATE_AUDIT",
        "status": "POLICY_CANDIDATE_ONLY_NOT_FORMAL_SIGNAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "primary_window": {"start": str(PRIMARY_START), "end": str(PRIMARY_END)},
        "candidate_rule": [
            "personal products with source-explicit sale windows only",
            "product known no later than the signal date",
            "lowest disclosed minimum purchase amount",
            "lowest annual rate for ties as a conservative choice",
            "stable product-name and source-URI tie-breakers",
        ],
        "summary": {
            "checkpoint_count": len(results),
            "covered_checkpoint_count": covered,
            "uncovered_checkpoint_count": len(results) - covered,
            "coverage_ratio": round(covered / len(results), 6) if results else 0,
            "uncovered_signal_dates": [row["signal_date"] for row in results if not row["covered"]],
        },
        "inputs": [
            {"path": str(observations_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(observations_path)},
            {
                "path_glob": args.trading_status_glob,
                "file_count": len(calendar_paths),
                "aggregate_sha256": hashlib.sha256(
                    "\n".join(
                        f"{path.relative_to(PROJECT_ROOT)}:{sha256_file(path)}" for path in calendar_paths
                    ).encode("utf-8")
                ).hexdigest(),
            },
        ],
        "formal_backtest_eligibility": {
            "eligible": False,
            "reasons": [
                "The within-bank product-selection rule is not frozen in the versioned strategy policy.",
                "This diagnostic does not promote any source or observation to a formal input.",
                "Uncovered checkpoints remain explicitly unavailable; no value is carried or imputed.",
            ],
        },
        "artifacts": {"results": str(results_path.relative_to(PROJECT_ROOT))},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "schema_version": "1.0.0",
        "event_type": "BACKTEST_CD_SELECTION_POLICY_CANDIDATE_AUDITED",
        "run_id": args.run_id,
        "status": receipt["status"],
        "scheduler_status": receipt["scheduler_status"],
        "receipt": {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256_file(receipt_path)},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
