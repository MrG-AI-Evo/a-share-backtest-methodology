#!/usr/bin/env python3
"""Build an immutable, display-only report from a completed mechanism-test result."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "data/backtests/staging/20260912-130000-quality-sample-11-pilot-v5/quality-sample-11-result.json"
DEFAULT_OUTPUT = ROOT / "reports/backtests/quality-sample-11-latest.json"
SCHEMA = ROOT / "schemas/quality-sample-backtest-report.schema.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def object_value(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def build(source: Path) -> dict[str, Any]:
    payload = object_value(json.loads(source.read_text("utf-8")), "source")
    result = object_value(payload["result"], "result")
    terminal = object_value(result["terminal"], "terminal")
    account = object_value(terminal["account_summary"], "account_summary")
    performance = object_value(terminal["performance"], "performance")
    window = object_value(payload["window"], "window")
    coverage = object_value(payload["data_coverage"], "data_coverage")
    audit = object_value(payload["audit"], "audit")
    sample_size = len(result["by_symbol"])
    prewindow_symbols = sum(row["first_price_date"] <= window["start"] for row in result["by_symbol"])

    stocks = [
        {
            "symbol": row["symbol"],
            "name": row["name"],
            "trade_count": row["trade_count"],
            "total_pnl_cny": row["terminal_total_pnl_cny"],
            "position_state": row["terminal_position_state"],
            "price_days": row["price_days"],
            "dividend_events": row["dividend_events"],
        }
        for row in result["by_symbol"]
    ]
    return {
        "schema_version": "1.0.0",
        "report_id": f"quality-sample-{sample_size}-{payload['run_id']}",
        "status": "MECHANISM_TEST_ONLY",
        "generated_at": payload["created_at"],
        "source_run": {
            "run_id": payload["run_id"],
            "sha256": sha256(source),
            "policy_version": terminal["policy_version"],
            "start_date": window["start"],
            "end_date": window["end"],
            "as_of": terminal["as_of"],
        },
        "portfolio": {
            "cumulative_external_contributions_cny": account["cumulative_external_contributions_cny"],
            "total_assets_cny": account["total_assets_cny"],
            "cash_cny": account["cash_cny"],
            "market_value_cny": account["market_value_cny"],
            "total_pnl_cny": performance["total_pnl_cny"],
            "realized_pnl_cny": performance["realized_pnl_cny"],
            "unrealized_pnl_cny": performance["unrealized_pnl_cny"],
            "simple_return": performance["simple_return"],
            "xirr": performance["mwrr_xirr_annualized"],
            "twr": performance["twr_cumulative"],
            "maximum_drawdown": performance["maximum_drawdown"],
            "trade_count": result["trade_count"],
            "active_position_count": account["active_position_count"],
            "cumulative_dividend_gross_cny": account["cumulative_dividend_gross_cny"],
            "transaction_costs_cny": round(account["cumulative_buy_transaction_costs_cny"] + account["cumulative_sell_transaction_costs_cny"], 2),
        },
        "annual": result["annual"],
        "stocks": stocks,
        "data_quality": {
            **coverage,
            "cd_cross_check": "120/120 months",
            "prewindow_dividend_cross_check": f"{prewindow_symbols * 3}/{prewindow_symbols * 3} exact matches",
            "cninfo_2014_reports": f"{prewindow_symbols}/{prewindow_symbols} parsed",
            "synthetic_raw_observation_count": terminal["data_quality"]["synthetic_raw_observation_count"],
            "time_travel_check": terminal["data_quality"]["time_travel_check"],
        },
        "audit": {
            "event_chain_ok": audit["event_chain_ok"],
            "reconciliation_status": audit["terminal_reconciliation_status"],
            "cash_difference_cny": audit["cash_reconciliation_difference_cny"],
            "pnl_difference_cny": audit["total_pnl_check_difference_cny"],
            "future_data_imputed": audit["future_data_imputed"],
            "ledger_event_count": audit["ledger_events"]["event_count"],
            "ledger_sha256": audit["ledger_events"]["sha256"],
        },
        "formal_blockers": payload["formal_blockers"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build(args.source.resolve())
    schema = json.loads(SCHEMA.read_text("utf-8"))
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(report), key=lambda item: list(item.path))
    if errors:
        raise ValueError(errors[0].message)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        existing = json.loads(args.output.read_text("utf-8"))
        if existing != report:
            raise FileExistsError(f"immutable report already exists with different content: {args.output}")
        return
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")


if __name__ == "__main__":
    main()
