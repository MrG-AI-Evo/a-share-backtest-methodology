#!/usr/bin/env python3
"""Build a conservative CNInfo annual-report download plan for quality research.

The planner is deterministic and foreground/manual-only. It creates a superset
of symbols that could ever clear the strategy's absolute 3 percentage-point
floor using already staged dividend, price and historical risk-status data.
It does not apply an unfrozen quality gate, generate trades, or download files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIMARY_START = "2016-01-04"
PRIMARY_END = "2025-12-31"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--price-parquet", type=Path, required=True)
    parser.add_argument("--dividend-parquet", type=Path, required=True)
    parser.add_argument("--status-glob", required=True)
    parser.add_argument("--cninfo-index-parquet", type=Path, required=True)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    audit_path = PROJECT_ROOT / "data" / "audit" / f"{args.run_id}-cninfo-quality-download-plan.json"
    if output_dir.exists() or audit_path.exists():
        raise RuntimeError("refusing to overwrite immutable output")
    price = args.price_parquet.resolve()
    dividend = args.dividend_parquet.resolve()
    cninfo = args.cninfo_index_parquet.resolve()
    for path in (price, dividend, cninfo):
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f"missing or empty input: {path}")
    status_glob = str((PROJECT_ROOT / args.status_glob).resolve())

    con = duckdb.connect()
    for name, path in (("prices", price), ("dividends", dividend), ("cninfo", cninfo)):
        sql_path = str(path).replace("'", "''")
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{sql_path}')")
    status_sql_glob = status_glob.replace("'", "''")
    con.execute(
        f"CREATE VIEW statuses AS SELECT * FROM read_parquet('{status_sql_glob}', hive_partitioning=false)"
    )
    con.execute(
        f"""
        CREATE TEMP TABLE checkpoints AS
        SELECT min(trade_date) AS signal_date
        FROM statuses
        WHERE trade_date BETWEEN DATE '{PRIMARY_START}' AND DATE '{PRIMARY_END}'
        GROUP BY year(trade_date), month(trade_date)
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE dividend_events AS
        SELECT DISTINCT
          security_code AS symbol,
          ex_dividend_date AS ex_date,
          pretax_bonus_rmb / 10.0 AS cash_dividend_per_share,
          known_at_candidate
        FROM dividends
        WHERE ex_dividend_date IS NOT NULL
          AND pretax_bonus_rmb IS NOT NULL
          AND pretax_bonus_rmb >= 0
          AND known_at_candidate IS NOT NULL
        """
    )
    con.execute(
        """
        CREATE TEMP TABLE rough_hits AS
        WITH ttm AS (
          SELECT c.signal_date, d.symbol, sum(d.cash_dividend_per_share) AS d_ttm
          FROM checkpoints c
          JOIN dividend_events d
            ON d.ex_date BETWEEN c.signal_date - INTERVAL 365 DAY AND c.signal_date
           AND d.known_at_candidate <= c.signal_date
          GROUP BY c.signal_date, d.symbol
        )
        SELECT
          t.signal_date,
          t.symbol,
          t.d_ttm,
          p.close,
          t.d_ttm / p.close AS rough_yield
        FROM ttm t
        JOIN prices p ON p.symbol=t.symbol AND p.effective_date=t.signal_date
        JOIN statuses s ON s.symbol=t.symbol || '.' || CASE p.exchange WHEN 'SSE' THEN 'SH' ELSE 'SZ' END
                       AND s.trade_date=t.signal_date
        WHERE p.close > 0
          AND s.is_trading
          AND NOT s.risk_warning
          AND t.d_ttm / p.close >= 0.0300
        """
    )
    symbol_rows = con.execute(
        """
        SELECT symbol, min(signal_date), max(signal_date), count(*), max(rough_yield)
        FROM rough_hits
        GROUP BY symbol
        ORDER BY symbol
        """
    ).fetchall()
    plan_rows = con.execute(
        """
        SELECT
          c.symbol,
          c.issuer_name,
          c.announcement_id,
          c.known_at_conservative,
          c.title,
          c.pdf_url,
          c.adjunct_size_kib_candidate,
          c.response_sha256
        FROM cninfo c
        JOIN (SELECT DISTINCT symbol FROM rough_hits) h USING(symbol)
        WHERE regexp_matches(c.title, '20[0-9]{2}年年度报告')
          AND NOT regexp_matches(c.title, '摘要|英文|取消')
          AND c.pdf_url IS NOT NULL
          AND CAST(c.known_at_conservative AS DATE) BETWEEN DATE '2016-01-01' AND DATE '2025-12-31'
        ORDER BY c.symbol, c.known_at_conservative, c.announcement_id
        """
    ).fetchall()

    output_dir.mkdir(parents=True, exist_ok=False)
    symbols_path = output_dir / "rough-dividend-superset.json"
    plan_path = output_dir / "cninfo-annual-report-download-plan.jsonl"
    symbols = [
        {
            "symbol": row[0],
            "first_hit": str(row[1]),
            "last_hit": str(row[2]),
            "hit_months": row[3],
            "max_rough_yield": row[4],
        }
        for row in symbol_rows
    ]
    symbols_path.write_text(json.dumps(symbols, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with plan_path.open("w", encoding="utf-8") as handle:
        for row in plan_rows:
            handle.write(
                json.dumps(
                    {
                        "symbol": row[0],
                        "issuer_name": row[1],
                        "announcement_id": row[2],
                        "known_at": row[3].isoformat() if row[3] else None,
                        "title": row[4],
                        "pdf_url": row[5],
                        "size_kib_candidate": row[6],
                        "index_response_sha256": row[7],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    estimated_kib = sum(int(row[6] or 0) for row in plan_rows)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_CNINFO_QUALITY_PDF_DOWNLOAD_PLANNING",
        "status": "DOWNLOAD_PLAN_ONLY_NOT_FORMAL_INPUT",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "scope": "SSE_AND_SZSE_ONLY_BSE_EXCLUDED",
        "selection": {
            "purpose": "overinclusive data-acquisition planning only",
            "rule": "monthly D_ttm / raw_close >= 0.0300, trading and non-ST",
            "why_superset": "R_cd_signal is nonnegative and D_cons <= D_ttm, so formal qualifiers cannot exceed this rough floor when inputs are consistent",
            "not_applied": ["unfrozen quality gate", "D_med3", "bank-rate hurdle", "portfolio capacity", "trading decisions"],
        },
        "summary": {
            "monthly_checkpoints": 120,
            "symbols_in_rough_superset": len(symbol_rows),
            "annual_report_files_planned": len(plan_rows),
            "estimated_download_kib_from_index": estimated_kib,
            "estimated_download_gib_from_index": round(estimated_kib / 1024 / 1024, 3),
        },
        "inputs": [
            {"path": str(price.relative_to(PROJECT_ROOT)), "sha256": digest(price)},
            {"path": str(dividend.relative_to(PROJECT_ROOT)), "sha256": digest(dividend)},
            {"path": str(cninfo.relative_to(PROJECT_ROOT)), "sha256": digest(cninfo)},
            {"path_glob": args.status_glob},
        ],
        "outputs": {
            "symbols": {"path": str(symbols_path.relative_to(PROJECT_ROOT)), "sha256": digest(symbols_path)},
            "download_plan": {"path": str(plan_path.relative_to(PROJECT_ROOT)), "sha256": digest(plan_path)},
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "event_type": "BACKTEST_CNINFO_QUALITY_PDF_DOWNLOAD_PLANNED",
                "run_id": args.run_id,
                "status": receipt["status"],
                "scheduler_status": receipt["scheduler_status"],
                "receipt": {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": digest(receipt_path)},
                "created_at": receipt["created_at"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
