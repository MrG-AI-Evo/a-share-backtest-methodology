#!/usr/bin/env python3
"""Build an append-only, cross-checked dividend panel for a fixed-sample pilot.

The output remains mechanism-test evidence. Eastmoney rows are a provider's
latest view and the legacy CSMAR mirror has no proven redistribution licence or
original publication timestamp, so neither is promoted to formal PIT data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Asia/Shanghai")
POLICY = ROOT / "config/dividend-hurdle-quality-sample-11-v1.yaml"
PRICE = ROOT / "data/backtests/staging/nas-daily-20260910-155000-nas-daily-staging/shsz-daily-raw-bars-staging-v1.parquet"
EVENT_INPUTS = (
    ROOT / "data/backtests/staging/20260912-103000-eastmoney-dividend-events-prewindow-v1/shsz-dividend-events-candidate.parquet",
    ROOT / "data/backtests/staging/20260912-104000-eastmoney-dividend-events-prewindow-v2/shsz-dividend-events-candidate.parquet",
    ROOT / "data/backtests/staging/20260911-004100-eastmoney-dividend-events-candidate/shsz-dividend-events-candidate.parquet",
)
CSMAR = ROOT / "data/backtests/staging/third-party-csmar-legacy-quality-20260911/raw/公司研究系列/财务指标分析/股利分配/股利分配.csv"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def rel(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def write_json_once(path: Path, payload: object) -> None:
    if path.exists():
        raise RuntimeError(f"immutable output already exists: {path}")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--policy", type=Path, default=POLICY)
    args = parser.parse_args()
    output = ROOT / "data/backtests/staging" / args.run_id
    audit = ROOT / "data/audit" / f"{args.run_id}-dividend-panel.json"
    if output.exists() or audit.exists():
        raise RuntimeError("run_id already exists; outputs are immutable")
    policy_path = args.policy.resolve()
    for path in (policy_path, PRICE, CSMAR, *EVENT_INPUTS):
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"missing or empty input: {path}")

    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    symbols = [str(item["symbol"]) for item in policy["scope"]["symbols"]]
    if not symbols or len(set(symbols)) != len(symbols):
        raise RuntimeError("policy symbols must be non-empty and unique")
    sample_size = len(symbols)

    parquet_db = duckdb.connect()
    frames = [parquet_db.execute(f"SELECT * FROM read_parquet('{path}')").fetchdf() for path in EVENT_INPUTS]
    events = pd.concat(frames, ignore_index=True)
    events = events[events["security_code"].astype(str).isin(symbols)].copy()
    required = ["security_code", "security_name_abbr", "report_date", "plan_notice_date", "equity_record_date", "ex_dividend_date", "pretax_bonus_rmb", "assign_progress"]
    events = events[required]
    for column in ("report_date", "plan_notice_date", "equity_record_date", "ex_dividend_date"):
        events[column] = pd.to_datetime(events[column], errors="coerce").dt.date
    events["pretax_bonus_rmb"] = pd.to_numeric(events["pretax_bonus_rmb"], errors="coerce")
    events = events.dropna(subset=["security_code", "report_date", "plan_notice_date", "ex_dividend_date", "pretax_bonus_rmb"])
    events = events[events["assign_progress"].eq("实施分配")]
    events["cash_per_share"] = events["pretax_bonus_rmb"] / 10.0
    events["fiscal_year"] = events["report_date"].map(lambda value: value.year)
    events["known_at"] = events["plan_notice_date"].map(lambda value: f"{value.isoformat()}T15:00:00+08:00")
    events["resolved_known_at"] = events["ex_dividend_date"].map(lambda value: f"{value.isoformat()}T15:00:00+08:00")
    events["source_id"] = "EASTMONEY_RPT_SHAREBONUS_DET_CANDIDATE"
    events["formal_backtest_eligible"] = False
    events["is_synthetic"] = False
    events = events.sort_values(["security_code", "report_date", "ex_dividend_date", "cash_per_share"])
    events = events.drop_duplicates(["security_code", "report_date", "plan_notice_date", "ex_dividend_date", "cash_per_share"])

    first_prices = parquet_db.execute(
        f"SELECT symbol, effective_date FROM read_parquet('{PRICE}')"
    ).fetchdf()
    first_prices["effective_date"] = pd.to_datetime(first_prices["effective_date"], errors="raise").dt.date
    first_prices = first_prices[first_prices["symbol"].isin(symbols)].groupby("symbol")["effective_date"].min().to_dict()

    fiscal = events.groupby(["security_code", "security_name_abbr", "fiscal_year"], as_index=False).agg(
        total_cash_per_share=("cash_per_share", "sum"),
        resolved_known_at=("resolved_known_at", "max"),
        event_count=("cash_per_share", "size"),
    )
    fiscal["fully_resolved"] = True
    fiscal["resolution_basis"] = "LAST_IMPLEMENTED_EX_DATE_IN_PROVIDER_SNAPSHOT"
    fiscal["formal_backtest_eligible"] = False
    fiscal["is_synthetic"] = False

    csmar = pd.read_csv(CSMAR, dtype={"股票代码": str}, low_memory=False)
    csmar["股票代码"] = csmar["股票代码"].str.zfill(6)
    csmar["统计截止日期"] = pd.to_datetime(csmar["统计截止日期"], errors="coerce")
    csmar = csmar[
        csmar["股票代码"].isin(symbols)
        & csmar["统计截止日期"].dt.year.isin([2012, 2013, 2014])
        & csmar["统计截止日期"].dt.month.eq(12)
        & csmar["统计截止日期"].dt.day.eq(31)
        & csmar["报表类型编码"].eq("A")
    ].copy()
    csmar["fiscal_year"] = csmar["统计截止日期"].dt.year
    check = fiscal[fiscal["fiscal_year"].isin([2012, 2013, 2014])].merge(
        csmar[["股票代码", "fiscal_year", "每股税前现金股利"]],
        left_on=["security_code", "fiscal_year"],
        right_on=["股票代码", "fiscal_year"],
        how="outer",
    )
    check["absolute_difference"] = (check["total_cash_per_share"] - check["每股税前现金股利"]).abs()
    check["matched"] = check["absolute_difference"].le(1e-8)
    applicable = [symbol for symbol in symbols if first_prices.get(symbol) and first_prices[symbol] <= date(2016, 1, 4)]
    expected = {(symbol, year) for symbol in applicable for year in (2012, 2013, 2014)}
    observed = {(str(row.security_code), int(row.fiscal_year)) for row in check.itertuples() if bool(row.matched)}
    if observed != expected:
        raise RuntimeError(f"pre-window cross-check incomplete: missing={sorted(expected-observed)} extra={sorted(observed-expected)}")

    output.mkdir(parents=True, exist_ok=False)
    events_path = output / f"quality-sample-{sample_size}-dividend-events-candidate.parquet"
    fiscal_path = output / f"quality-sample-{sample_size}-dividend-fiscal-years-candidate.parquet"
    check_path = output / "prewindow-cross-check.json"
    receipt_path = output / "receipt.json"
    parquet_db.register("events_output", events)
    parquet_db.register("fiscal_output", fiscal)
    parquet_db.execute(f"COPY events_output TO '{events_path}' (FORMAT PARQUET)")
    parquet_db.execute(f"COPY fiscal_output TO '{fiscal_path}' (FORMAT PARQUET)")
    comparison_rows = []
    for row in check.sort_values(["security_code", "股票代码", "fiscal_year"], na_position="last").itertuples(index=False):
        comparison_rows.append({
            "symbol": str(row.security_code) if pd.notna(row.security_code) else str(row.股票代码),
            "fiscal_year": int(row.fiscal_year),
            "eastmoney_cash_per_share": None if pd.isna(row.total_cash_per_share) else float(row.total_cash_per_share),
            "nas_csmar_cash_per_share": None if pd.isna(row.每股税前现金股利) else float(row.每股税前现金股利),
            "matched": bool(row.matched),
            "classification": "MATCHED" if bool(row.matched) else "NOT_APPLICABLE_PRE_LISTING",
        })
    cross_check = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "window": [2012, 2013, 2014],
        "applicable_symbols": applicable,
        "not_applicable_pre_listing": sorted(set(symbols) - set(applicable)),
        "expected_matches": len(expected),
        "actual_matches": len(observed),
        "all_applicable_values_match": observed == expected,
        "rows": comparison_rows,
    }
    write_json_once(check_path, cross_check)
    inputs = [{"path": rel(path), "bytes": path.stat().st_size, "sha256": digest(path)} for path in (policy_path, PRICE, CSMAR, *EVENT_INPUTS)]
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_QUALITY_SAMPLE_DIVIDEND_PANEL_BUILD",
        "status": "MECHANISM_TEST_CANDIDATE_READY_NOT_FORMAL_PIT",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "created_at": datetime.now(TZ).isoformat(),
        "inputs": inputs,
        "outputs": [
            {"path": rel(events_path), "rows": len(events), "bytes": events_path.stat().st_size, "sha256": digest(events_path)},
            {"path": rel(fiscal_path), "rows": len(fiscal), "bytes": fiscal_path.stat().st_size, "sha256": digest(fiscal_path)},
            {"path": rel(check_path), "bytes": check_path.stat().st_size, "sha256": digest(check_path)},
        ],
        "coverage": {
            "symbols": len(set(events["security_code"])),
            "event_min_ex_date": str(events["ex_dividend_date"].min()),
            "event_max_ex_date": str(events["ex_dividend_date"].max()),
            "prewindow_exact_matches": len(observed),
            "prewindow_expected_matches": len(expected),
            "prelisting_not_applicable": sorted(set(symbols) - set(applicable)),
        },
        "limitations": [
            "Eastmoney rows are latest-view candidate records without a proven historical revision chain.",
            "The NAS CSMAR mirror is used only for value cross-checking; its licence and original publication timestamps are unproven.",
            "The panel is eligible only for the fixed 11-stock mechanism test, not the formal ten-year backtest.",
        ],
    }
    write_json_once(receipt_path, receipt)
    audit.parent.mkdir(parents=True, exist_ok=True)
    write_json_once(audit, {
        "schema_version": "1.0.0",
        "event_type": "BACKTEST_DIVIDEND_PREWINDOW_PANEL_BUILT",
        "run_id": args.run_id,
        "status": receipt["status"],
        "receipt": rel(receipt_path),
        "receipt_sha256": digest(receipt_path),
        "created_at": receipt["created_at"],
    })
    print(json.dumps({"run_id": args.run_id, "status": receipt["status"], "coverage": receipt["coverage"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
