#!/usr/bin/env python3
"""Build a private static audit explorer from deterministic backtest artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

VISIBLE = {"ENTRY_SIGNAL", "EXIT_SIGNAL", "BUY_FILL", "SELL_FILL", "DIVIDEND_RECEIVABLE", "DIVIDEND_PAYMENT", "CONTRIBUTION_ATTRIBUTION", "DATA_GAP"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact(event: dict, signal: dict | None = None) -> dict:
    item = {key: event.get(key) for key in ("sequence", "event_id", "event_hash", "prev_event_hash", "event_type", "occurred_at", "known_at", "payload")}
    item["type"] = item.pop("event_type")
    if signal:
        item["causal_signal"] = {
            "event_id": signal["event_id"], "event_hash": signal["event_hash"],
            "occurred_at": signal["occurred_at"], "signal": signal.get("payload", {}).get("signal", {}),
        }
    return item


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = json.loads(args.result.read_text(encoding="utf-8"))
    events = [json.loads(line) for line in args.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    ledger_audit = result["audit"]["ledger_events"]
    if not events or len(events) != ledger_audit["event_count"]:
        raise SystemExit("ledger count does not match result audit")
    if sha256(args.ledger) != ledger_audit["sha256"]:
        raise SystemExit("ledger SHA-256 does not match result audit")
    terminal = result["result"]["terminal"]
    if not result["audit"]["event_chain_ok"] or result["audit"]["terminal_reconciliation_status"] != "PASS":
        raise SystemExit("source run failed deterministic audit")

    ledgers = {row["symbol"]: row for row in terminal["security_ledgers"]}
    latest_signal: dict[str, dict[str, dict]] = defaultdict(dict)
    counts: dict[str, Counter] = defaultdict(Counter)
    year_counts: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    last_valuation: dict[str, dict[str, dict]] = defaultdict(dict)
    visible: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for event in events:
        symbol = event.get("symbol")
        if not symbol:
            continue
        year, kind = event["occurred_at"][:4], event["event_type"]
        counts[symbol][kind] += 1
        year_counts[symbol][year][kind] += 1
        if kind == "VALUATION":
            last_valuation[symbol][year] = {"occurred_at": event["occurred_at"], "raw_price_cny": event.get("payload", {}).get("raw_price_cny")}
            continue
        if kind in {"ENTRY_SIGNAL", "EXIT_SIGNAL"}:
            latest_signal[symbol][kind] = event
        if kind in VISIBLE:
            signal = latest_signal[symbol].get("ENTRY_SIGNAL" if kind == "BUY_FILL" else "EXIT_SIGNAL") if kind in {"BUY_FILL", "SELL_FILL"} else None
            visible[symbol][year].append(compact(event, signal))

    stocks = []
    for row in result["result"]["by_symbol"]:
        symbol, ledger = row["symbol"], ledgers.get(row["symbol"], {})
        years = {}
        for year in map(str, range(2016, 2026)):
            c = year_counts[symbol][year]
            years[year] = {
                "counts": dict(sorted(c.items())), "trade_count": c["BUY_FILL"] + c["SELL_FILL"],
                "signal_count": c["ENTRY_SIGNAL"] + c["EXIT_SIGNAL"], "data_gap_count": c["DATA_GAP"],
                "dividend_payment_count": c["DIVIDEND_PAYMENT"], "last_valuation": last_valuation[symbol].get(year),
                "events": visible[symbol][year],
            }
        keys = ("current_quantity", "current_market_value_cny", "current_cost_basis_cny", "cumulative_external_contribution_attributed_cny", "cumulative_buy_gross_cny", "cumulative_buy_transaction_costs_cny", "cumulative_sell_gross_cny", "cumulative_sell_transaction_costs_cny", "cumulative_dividend_gross_cny", "realized_pnl_cny", "unrealized_pnl_cny", "total_pnl_cny")
        stocks.append({
            "symbol": symbol, "name": row["name"], "first_price_date": row["first_price_date"], "price_days": row["price_days"],
            "dividend_events": row["dividend_events"], "quality_report_years": row["quality_report_years"], "trade_count": row["trade_count"],
            "total_pnl_cny": row["terminal_total_pnl_cny"], "position_state": row["terminal_position_state"],
            "event_counts": dict(sorted(counts[symbol].items())), "ledger_summary": {key: ledger.get(key) for key in keys}, "years": years,
        })
    report = {
        "schema_version": "1.0.0", "title": "高股息质量样本 · 十年回测审计册", "run_id": result["run_id"],
        "status": result["status"], "formal_result_eligible": result["formal_result_eligible"],
        "selection_bias_warning": result["selection_bias_warning"], "window": result["window"], "created_at": result["created_at"],
        "source_integrity": {"result_sha256": sha256(args.result), "ledger_sha256": sha256(args.ledger), "last_event_hash": events[-1]["event_hash"], "event_chain_ok": result["audit"]["event_chain_ok"], "reconciliation": result["audit"]["terminal_reconciliation_status"], "cash_difference_cny": result["audit"]["cash_reconciliation_difference_cny"], "pnl_difference_cny": result["audit"]["total_pnl_check_difference_cny"], "future_data_imputed": result["audit"]["future_data_imputed"], "ledger_events": len(events)},
        "coverage": result["data_coverage"], "account": terminal["account_summary"], "performance": terminal["performance"],
        "annual": result["result"]["annual"], "stocks": sorted(stocks, key=lambda item: (-item["total_pnl_cny"], item["symbol"])),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("window.REPORT_DATA=" + json.dumps(report, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    print(json.dumps({"stocks": len(stocks), "trades": sum(s["trade_count"] for s in stocks), "visible_events": sum(len(y["events"]) for s in stocks for y in s["years"].values()), "bytes": args.output.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
