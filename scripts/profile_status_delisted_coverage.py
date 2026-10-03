#!/usr/bin/env python3
"""Create an immutable audit of historical ST/status and delisted-bar coverage.

This is a foreground, manual-only evidence task. It reads already staged data,
does not contact any network service, does not promote data into the formal
lake, and does not run a backtest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIMARY_START = "2016-01-04"
PRIMARY_END = "2025-12-31"
STATUS_GLOB = PROJECT_ROOT / (
    "state/cnequity-shsz-2016-2025-local/meta/revisions/data/trading_status/"
    "4fe8622405a046c0b2535a048dee04ba/trade_date=*/part-merged.parquet"
)
STATUS_MANIFEST = PROJECT_ROOT / (
    "state/cnequity-shsz-2016-2025-local/meta/revisions/trading_status/"
    "00000001-4fe8622405a046c0b2535a048dee04ba.json"
)
DELISTED_IDENTITY = PROJECT_ROOT / (
    "state/cnequity-shsz-2016-2025-local/meta/quality/evidence/"
    "delisted_security_identity/baostock-v1.json"
)
NAS_DAILY = PROJECT_ROOT / (
    "data/backtests/staging/nas-daily-20260910-155000-nas-daily-staging/"
    "shsz-daily-raw-bars-staging-v1.parquet"
)
NAS_RECEIPT = PROJECT_ROOT / (
    "data/backtests/staging/nas-daily-20260910-155000-nas-daily-staging/receipt.json"
)
PATCH_DAILY = PROJECT_ROOT / (
    "data/backtests/staging/baostock-delisted-20260911-023000-baostock-delisted-patch/"
    "missing-delisted-daily-bars.parquet"
)
PATCH_RECEIPT = PATCH_DAILY.parent / "receipt.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quote(path: Path) -> str:
    return str(path).replace("'", "''")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-status-delisted-coverage.json"
    collisions = [path for path in (output_dir, receipt_path, audit_path) if path.exists()]
    if collisions:
        raise RuntimeError("refusing to overwrite immutable artifacts: " + ", ".join(map(str, collisions)))

    fixed_inputs = (STATUS_MANIFEST, DELISTED_IDENTITY, NAS_DAILY, NAS_RECEIPT, PATCH_DAILY, PATCH_RECEIPT)
    invalid = [path for path in fixed_inputs if not path.is_file() or path.stat().st_size == 0]
    status_parts = sorted(STATUS_GLOB.parent.parent.glob("trade_date=*/part-merged.parquet"))
    if invalid or len(status_parts) != 120:
        details = [str(path) for path in invalid]
        if len(status_parts) != 120:
            details.append(f"status partitions expected=120 actual={len(status_parts)}")
        raise RuntimeError("missing or incomplete inputs: " + ", ".join(details))

    identity = json.loads(DELISTED_IDENTITY.read_text(encoding="utf-8"))
    delisted = identity.get("delisted_symbols")
    if not isinstance(delisted, dict):
        raise RuntimeError("delisted identity evidence lacks delisted_symbols mapping")
    in_window = {
        symbol: end_date
        for symbol, end_date in delisted.items()
        if PRIMARY_START <= str(end_date) <= PRIMARY_END
    }
    if not in_window:
        raise RuntimeError("no delisted identities fall inside the primary window")

    con = duckdb.connect()
    con.execute("CREATE TABLE delisted_identity(symbol VARCHAR, formal_end_date DATE)")
    con.executemany("INSERT INTO delisted_identity VALUES (?, ?)", list(in_window.items()))
    status_source = f"read_parquet('{quote(STATUS_GLOB)}', hive_partitioning=false)"
    nas_source = f"read_parquet('{quote(NAS_DAILY)}', hive_partitioning=false)"
    patch_source = f"read_parquet('{quote(PATCH_DAILY)}', hive_partitioning=false)"

    status_values = con.execute(
        f"""
        SELECT count(*), count(DISTINCT symbol), min(trade_date), max(trade_date),
               count(*) FILTER (WHERE risk_warning),
               count(*) FILTER (WHERE risk_warning IS NULL),
               count(*) FILTER (WHERE is_trading IS NULL),
               count(*) FILTER (WHERE NOT is_trading),
               count(*) FILTER (WHERE status <> 'normal' OR status IS NULL),
               coalesce(sum(extra_rows), 0)
        FROM {status_source},
        LATERAL (SELECT 0 AS extra_rows)
        """
    ).fetchone()
    status_duplicates = con.execute(
        f"""
        SELECT coalesce(sum(n - 1), 0)
        FROM (
          SELECT symbol, trade_date, count(*) AS n
          FROM {status_source}
          GROUP BY 1, 2 HAVING count(*) > 1
        )
        """
    ).fetchone()[0]

    delisted_rows = con.execute(
        f"""
        WITH nas AS (
          SELECT symbol || '.' || CASE exchange WHEN 'SSE' THEN 'SH' ELSE 'SZ' END AS symbol,
                 min(effective_date) AS first_date, max(effective_date) AS last_date,
                 count(*) AS rows
          FROM {nas_source}
          GROUP BY 1
        ), patch AS (
          SELECT symbol || '.' || CASE exchange WHEN 'SSE' THEN 'SH' ELSE 'SZ' END AS symbol,
                 min(effective_date) AS first_date, max(effective_date) AS last_date,
                 count(*) AS rows
          FROM {patch_source}
          WHERE is_trading AND close IS NOT NULL
          GROUP BY 1
        )
        SELECT d.symbol, d.formal_end_date,
               n.first_date, n.last_date, coalesce(n.rows, 0),
               p.first_date, p.last_date, coalesce(p.rows, 0),
               (n.symbol IS NOT NULL OR p.symbol IS NOT NULL) AS any_coverage,
               greatest(n.last_date, p.last_date) AS latest_available_date
        FROM delisted_identity d
        LEFT JOIN nas n USING (symbol)
        LEFT JOIN patch p USING (symbol)
        ORDER BY d.symbol
        """
    ).fetchall()
    columns = (
        "symbol", "formal_end_date", "nas_first_date", "nas_last_date", "nas_rows",
        "patch_first_date", "patch_last_date", "patch_rows", "any_coverage", "latest_available_date",
    )
    delisted_detail = [
        {name: value.isoformat() if hasattr(value, "isoformat") else value for name, value in zip(columns, row, strict=True)}
        for row in delisted_rows
    ]
    covered = [row for row in delisted_detail if row["any_coverage"]]
    beyond_end = [
        row for row in delisted_detail
        if row["latest_available_date"] and row["latest_available_date"] > row["formal_end_date"]
    ]
    missing = [row for row in delisted_detail if not row["any_coverage"]]

    input_files = []
    for path in fixed_inputs:
        input_files.append({
            "path": str(path.relative_to(PROJECT_ROOT)),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    input_descriptor = {
        "primary_window": {"start": PRIMARY_START, "end": PRIMARY_END},
        "status_revision_id": "4fe8622405a046c0b2535a048dee04ba",
        "status_content_digest": "b5045168e449e09eb30ceccf01ced052e33f94dd344261fad1099c5465a1ef11",
        "files": input_files,
        "script_sha256": sha256_file(Path(__file__).resolve()),
    }
    input_sha = hashlib.sha256(json.dumps(input_descriptor, sort_keys=True).encode()).hexdigest()

    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_STATUS_AND_DELISTED_COVERAGE_PROFILE",
        "status": "STAGING_COVERAGE_COMPLETE_NOT_FORMAL_INPUT" if not missing and not beyond_end else "STAGING_COVERAGE_REQUIRES_REVIEW",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "primary_window": input_descriptor["primary_window"],
        "input": {**input_descriptor, "input_sha256": input_sha},
        "historical_status": {
            "rows": status_values[0],
            "symbols": status_values[1],
            "first_date": status_values[2].isoformat(),
            "last_date": status_values[3].isoformat(),
            "risk_warning_rows": status_values[4],
            "null_risk_warning_rows": status_values[5],
            "null_is_trading_rows": status_values[6],
            "explicit_non_trading_rows": status_values[7],
            "non_normal_or_null_status_rows": status_values[8],
            "duplicate_symbol_date_rows": status_duplicates,
            "interpretation": "BaoStock returns trading observations. Absence on an exchange trading day must be evaluated by the engine; this table is not explicit suspended-day evidence.",
        },
        "delisted_daily_bars": {
            "identity_symbols_total": len(delisted),
            "identity_symbols_delisted_in_primary_window": len(delisted_detail),
            "symbols_with_any_daily_bar_coverage": len(covered),
            "symbols_without_daily_bar_coverage": len(missing),
            "symbols_with_bars_after_formal_end_date": len(beyond_end),
            "patch_symbols": [row["symbol"] for row in delisted_detail if row["patch_rows"]],
            "missing": missing,
            "after_end_review": beyond_end,
            "details": delisted_detail,
        },
        "formal_backtest_eligibility": {
            "eligible": False,
            "blockers_preserved": ["POINT_IN_TIME_COVERAGE"],
            "reasons": [
                "Coverage evidence does not by itself approve user-supplied data licensing.",
                "Formal suspension-day derivation still requires the versioned exchange calendar and no-bar rule.",
                "Corporate-action, disclosure and financial point-in-time inputs must be reconciled separately.",
            ],
        },
        "created_at": now(),
    }
    output_dir.mkdir(parents=True, exist_ok=False)
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = {
        "schema_version": "1.0.0",
        "event_type": "backtest-status-delisted-coverage-profile",
        "run_id": args.run_id,
        "status": receipt["status"],
        "scheduler_status": receipt["scheduler_status"],
        "input_sha256": input_sha,
        "artifact": {
            "path": str(receipt_path.relative_to(PROJECT_ROOT)),
            "sha256": sha256_file(receipt_path),
        },
        "blockers_preserved": receipt["formal_backtest_eligibility"]["blockers_preserved"],
        "created_at": receipt["created_at"],
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
