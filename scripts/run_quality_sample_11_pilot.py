#!/usr/bin/env python3
"""Run a deterministic fixed-sample high-dividend mechanism-test pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb
import yaml
from run_601288_deterministic_pilot import fee_rule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "apps/api"
import sys

sys.path.insert(0, str(API_ROOT))

from app.backtests.runner import (
    CorporateActionDay,
    DeterministicHistoryRunner,
    SecurityDay,
    TradingDayInput,
)

START = date(2016, 1, 4)
END = date(2025, 12, 31)
PRICE_PATH = PROJECT_ROOT / "data/backtests/staging/nas-daily-20260910-155000-nas-daily-staging/shsz-daily-raw-bars-staging-v1.parquet"
CD_MONTHLY_PATH = PROJECT_ROOT / "data/backtests/staging/20260912-041500-cd-monthly-120m-v1/monthly-cd-rate-series.json"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "config/dividend-hurdle-quality-sample-11-v1.yaml"
DEFAULT_QUALITY_PATHS = (
    PROJECT_ROOT / "data/backtests/staging/20260911-231500-cninfo-quality-extraction-full-v2/annual-report-fact-candidates.jsonl",
    PROJECT_ROOT / "data/backtests/staging/20260912-035000-601288-missing-reports-extract/annual-report-fact-candidates.jsonl",
    PROJECT_ROOT / "data/backtests/staging/20260912-125000-quality-sample-2014-report-extract-v4/annual-report-fact-candidates.jsonl",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_once(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"immutable output already exists: {path}")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text_once(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"immutable output already exists: {path}")
    path.write_text(value, encoding="utf-8")


def as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value)[:10])


def number(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    raw = str(value).strip().replace(",", "").replace("%", "")
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    try:
        result = Decimal(raw)
        return -result if negative else result
    except InvalidOperation:
        return None


def annual_quality_rows(symbols: set[str], quality_paths: tuple[Path, ...]) -> dict[str, list[dict[str, Any]]]:
    by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for path in quality_paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            symbol = str(row.get("source", {}).get("symbol") or "")
            if symbol not in symbols:
                continue
            year = int(row["document"]["report_year"])
            profit = next(
                (number(item.get("raw_value")) for item in row.get("metric_candidates", []) if item.get("metric_id") == "net_profit_parent"),
                None,
            )
            audit_class = row.get("audit_opinion", {}).get("classification")
            concern = row.get("going_concern", {}).get("status")
            passed = bool(
                audit_class == "STANDARD_UNQUALIFIED"
                and concern not in {"MATERIAL_UNCERTAINTY_FOUND", "GOING_CONCERN_RISK_FOUND"}
                and profit is not None
                and profit > 0
            )
            normalized = {
                "symbol": symbol,
                "report_year": year,
                "known_at": row["source"]["known_at"],
                "passed": passed,
                "audit_classification": audit_class,
                "going_concern_status": concern,
                "parent_net_profit_positive": profit is not None and profit > 0,
                "source_pdf": row["source"]["path"],
                "source_pdf_sha256": row["source"]["sha256"],
            }
            key = (symbol, year)
            existing = by_key.get(key)
            if existing is None or normalized["known_at"] > existing["known_at"]:
                by_key[key] = normalized
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in by_key.values():
        source_path = Path(row["source_pdf"])
        if not source_path.is_file() or sha256(source_path) != row["source_pdf_sha256"]:
            raise RuntimeError(f"quality source missing or hash mismatch: {source_path}")
        result[row["symbol"]].append(row)
    for rows in result.values():
        rows.sort(key=lambda item: item["known_at"])
    return dict(result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    args = parser.parse_args()
    policy_path = args.policy.resolve()
    if not policy_path.is_file() or policy_path.stat().st_size == 0:
        raise RuntimeError(f"missing or empty policy: {policy_path}")
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    quality_paths = tuple(
        (PROJECT_ROOT / value).resolve()
        for value in policy.get("quality_inputs", {}).get("paths", ())
    ) or DEFAULT_QUALITY_PATHS
    dividend_config = policy.get("dividend_inputs") or {
        "events_path": "data/backtests/staging/20260911-004100-eastmoney-dividend-events-candidate/shsz-dividend-events-candidate.parquet"
    }
    dividend_path = (PROJECT_ROOT / dividend_config["events_path"]).resolve()
    fiscal_path = (PROJECT_ROOT / dividend_config["fiscal_years_path"]).resolve() if dividend_config.get("fiscal_years_path") else None
    required = (PRICE_PATH, dividend_path, CD_MONTHLY_PATH, policy_path, *quality_paths, *((fiscal_path,) if fiscal_path else ()))
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"missing or empty input: {path}")
    sample = policy["scope"]["symbols"]
    symbols = [str(item["symbol"]) for item in sample]
    names = {str(item["symbol"]): str(item["name"]) for item in sample}
    if not symbols or len(set(symbols)) != len(symbols):
        raise RuntimeError("sample must contain unique symbols")
    sample_size = len(symbols)

    output_dir = PROJECT_ROOT / "data/backtests/staging" / args.run_id
    result_path = output_dir / f"quality-sample-{sample_size}-result.json"
    events_path = output_dir / "ledger-events.jsonl"
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-quality-sample-{sample_size}.json"
    if output_dir.exists() or audit_path.exists():
        raise RuntimeError("run_id already exists; outputs are immutable")

    db = duckdb.connect()
    master_dates = [row[0] for row in db.execute(
        f"SELECT effective_date FROM read_parquet('{PRICE_PATH}') WHERE symbol='601288' AND effective_date BETWEEN ? AND ? ORDER BY effective_date",
        [START, END],
    ).fetchall()]
    if len(master_dates) != 2430 or master_dates[0] != START or master_dates[-1] != END:
        raise RuntimeError("master calendar is incomplete")
    price_rows = db.execute(
        f"SELECT symbol,effective_date,open,close,volume_shares,amount_cny FROM read_parquet('{PRICE_PATH}') "
        "WHERE symbol IN (SELECT * FROM UNNEST(?)) AND effective_date BETWEEN ? AND ? ORDER BY symbol,effective_date",
        [symbols, START, END],
    ).fetchall()
    prices: dict[str, dict[date, tuple[Any, ...]]] = defaultdict(dict)
    for symbol, trading_date, open_price, close_price, volume, amount in price_rows:
        prices[symbol][trading_date] = (open_price, close_price, volume, amount)
    absent = sorted(set(symbols) - set(prices))
    if absent:
        raise RuntimeError(f"symbols without prices: {absent}")

    dividend_rows = db.execute(
        f"SELECT security_code,report_date,plan_notice_date,equity_record_date,ex_dividend_date,pretax_bonus_rmb "
        f"FROM read_parquet('{dividend_path}') WHERE security_code IN (SELECT * FROM UNNEST(?)) "
        "AND ex_dividend_date IS NOT NULL AND plan_notice_date IS NOT NULL AND pretax_bonus_rmb IS NOT NULL ORDER BY security_code,report_date",
        [symbols],
    ).fetchall()
    dividends: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for symbol, report_date, known_at, record_date, ex_date, cash_per_10 in dividend_rows:
        dividends[symbol].append({
            "fiscal_year": report_date.year,
            "known_at": known_at,
            "record_date": record_date,
            "ex_date": ex_date,
            "cash": float(cash_per_10) / 10.0,
        })
    if missing_dividends := sorted(set(symbols) - set(dividends)):
        raise RuntimeError(f"symbols without dividends: {missing_dividends}")

    quality = annual_quality_rows(set(symbols), quality_paths)
    cd_rows = json.loads(CD_MONTHLY_PATH.read_text(encoding="utf-8"))
    if len(cd_rows) != 120 or len({row["month"] for row in cd_rows}) != 120:
        raise RuntimeError("monthly CD series is not exactly 120 unique months")
    cd_by_month = {row["month"]: row for row in cd_rows}
    checkpoint_dates = {as_date(row["signal_date"]) for row in cd_rows}
    if None in checkpoint_dates or not all(as_date(row["known_at"]) <= as_date(row["signal_date"]) for row in cd_rows):
        raise RuntimeError("monthly CD series failed future-data validation")

    fiscal_resolved: dict[str, dict[int, date]] = defaultdict(dict)
    fiscal_totals: dict[str, dict[int, float]] = defaultdict(dict)
    if fiscal_path:
        fiscal_rows = db.execute(
            f"SELECT security_code,fiscal_year,total_cash_per_share,resolved_known_at FROM read_parquet('{fiscal_path}') "
            "WHERE security_code IN (SELECT * FROM UNNEST(?)) AND fully_resolved=true ORDER BY security_code,fiscal_year",
            [symbols],
        ).fetchall()
        for symbol, year, total, resolved_known_at in fiscal_rows:
            fiscal_totals[symbol][int(year)] = float(total)
            resolved_date = as_date(resolved_known_at)
            if resolved_date is None:
                raise ValueError(
                    f"invalid resolved_known_at for {symbol} fiscal_year={int(year)}: "
                    f"{resolved_known_at!r}"
                )
            fiscal_resolved[symbol][int(year)] = resolved_date
    else:
        fiscal_rows = []
        fiscal_first_known: dict[str, dict[int, date]] = defaultdict(dict)
        for symbol, rows in dividends.items():
            for row in rows:
                year = row["fiscal_year"]
                fiscal_totals[symbol][year] = fiscal_totals[symbol].get(year, 0.0) + row["cash"]
                fiscal_first_known[symbol][year] = min(fiscal_first_known[symbol].get(year, row["known_at"]), row["known_at"])
            years = sorted(fiscal_totals[symbol])
            for year in years:
                later = [fiscal_first_known[symbol][other] for other in years if other > year]
                if later:
                    fiscal_resolved[symbol][year] = min(later)

    actions_by_date: dict[date, list[CorporateActionDay]] = defaultdict(list)
    for symbol, rows in dividends.items():
        for row in rows:
            if START <= row["ex_date"] <= END:
                known_at = f"{row['ex_date'].isoformat()}T09:30:00+08:00"
                evidence = ("EASTMONEY_DIVIDEND_CANDIDATE",)
                actions_by_date[row["ex_date"]].extend((
                    CorporateActionDay(symbol, "DIVIDEND_RECEIVABLE", known_at, cash_per_share=Decimal(str(row["cash"])), evidence_ids=evidence),
                    CorporateActionDay(symbol, "DIVIDEND_PAYMENT", known_at, evidence_ids=evidence),
                ))

    days: list[TradingDayInput] = []
    for trading_date in master_dates:
        signal_at = f"{trading_date.isoformat()}T15:00:00+08:00"
        cd_row = cd_by_month[trading_date.strftime("%Y-%m")]
        cd_available = trading_date >= as_date(cd_row["signal_date"])
        cd_rate = Decimal(str(cd_row["annual_rate_decimal"])) if cd_available else None
        cd_tenor = int(float(cd_row["tenor_years"])) if cd_available else None
        cd_quality = ("TENOR_FALLBACK" if cd_row.get("tenor_fallback") else "OBSERVED") if cd_available else "UNAVAILABLE"
        securities: list[SecurityDay] = []
        for symbol in symbols:
            quote = prices[symbol].get(trading_date)
            open_price, close_price, volume, amount = quote if quote else (None, None, None, None)
            ttm_start = trading_date - timedelta(days=365)
            d_ttm = sum(row["cash"] for row in dividends[symbol] if ttm_start < row["ex_date"] <= trading_date and row["known_at"] <= trading_date)
            resolved = sorted((year for year, known in fiscal_resolved[symbol].items() if known <= trading_date), reverse=True)[:3]
            d_med3 = sorted(fiscal_totals[symbol][year] for year in resolved)[1] if len(resolved) == 3 else None
            disclosed = [row for row in quality.get(symbol, []) if row["known_at"] <= signal_at]
            quality_pass = disclosed[-1]["passed"] if disclosed else None
            tradable = bool(quote and open_price and close_price and volume and volume > 0)
            securities.append(SecurityDay(
                symbol=symbol,
                name=names[symbol],
                exchange="SZSE" if symbol.startswith(("0", "3")) else "SSE",
                ownership_type="UNVERIFIED_SAMPLE_LABEL",
                open_price=Decimal(str(open_price)) if open_price else None,
                close_price=Decimal(str(close_price)) if close_price else None,
                tradable_open=tradable,
                tradable_close=tradable,
                risk_status_eligible=True,
                quality_pass=quality_pass,
                d_ttm=Decimal(str(d_ttm)),
                d_med3=Decimal(str(d_med3)) if d_med3 is not None else None,
                cd_rate=cd_rate,
                cd_tenor_years=cd_tenor,
                cd_rate_quality=cd_quality,  # type: ignore[arg-type]
                median_amount_20d_cny=Decimal(str(amount)) if amount else None,
                evidence_ids=("NAS_DAILY_RAW", "CROSSCHECKED_DIVIDEND_PANEL_CANDIDATE", "MONTHLY_CD_RATE_SERIES", "CNINFO_ANNUAL_REPORT"),
            ))
        days.append(TradingDayInput(
            trade_date=trading_date.isoformat(),
            is_first_trading_day_of_month=trading_date in checkpoint_dates,
            securities=tuple(securities),
            fee_rule=fee_rule(trading_date),
            buy_slippage_bps=Decimal(5),
            sell_slippage_bps=Decimal(5),
            corporate_actions=tuple(actions_by_date.get(trading_date, ())),
        ))

    runner = DeterministicHistoryRunner(
        args.run_id,
        policy["policy_version"],
        run_purpose="MECHANISM_TEST",
        scenario="BASE",
        raw_data_mode="REAL_POINT_IN_TIME",
        synthetic_raw_observation_count=0,
    )
    run = runner.run(days)
    terminal = run.checkpoints[-1]
    terminal_realized = round(sum(float(row["realized_pnl_cny"]) for row in terminal["security_ledgers"]), 2)
    terminal_unrealized = round(sum(float(row["unrealized_pnl_cny"]) for row in terminal["security_ledgers"]), 2)
    terminal["performance"]["realized_pnl_cny"] = terminal_realized
    terminal["performance"]["unrealized_pnl_cny"] = terminal_unrealized
    reconciliation = {str(row["name"]): float(row["difference"]) for row in terminal["reconciliation"]["checks"]}
    trades = [{"event": event["event_type"], "symbol": event.get("symbol"), "occurred_at": event["occurred_at"], **event["payload"]} for event in run.ledger.events if event["event_type"] in {"BUY_FILL", "SELL_FILL"}]
    event_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in run.ledger.events:
        event_counts[str(event.get("symbol") or "PORTFOLIO")][str(event["event_type"])] += 1
    by_symbol: list[dict[str, Any]] = []
    for item in sample:
        symbol = item["symbol"]
        ledger_row = next((row for row in terminal["security_ledgers"] if row["symbol"] == symbol), None)
        symbol_trades = [row for row in trades if row["symbol"] == symbol]
        by_symbol.append({
            "symbol": symbol,
            "name": item["name"],
            "first_price_date": min(prices[symbol]).isoformat(),
            "price_days": len(prices[symbol]),
            "dividend_events": len(dividends[symbol]),
            "quality_report_years": [row["report_year"] for row in quality.get(symbol, [])],
            "trade_count": len(symbol_trades),
            "trades": symbol_trades,
            "terminal_total_pnl_cny": ledger_row["total_pnl_cny"] if ledger_row else 0.0,
            "terminal_position_state": ledger_row["position_state"] if ledger_row else "NEVER_OPENED",
            "event_counts": dict(sorted(event_counts.get(symbol, {}).items())),
        })
    annual: list[dict[str, Any]] = []
    for year in range(2016, 2026):
        checkpoint = next(row for row in reversed(run.checkpoints) if str(row["as_of"]).startswith(str(year)))
        account = checkpoint["account_summary"]
        performance = checkpoint["performance"]
        annual.append({
            "year": year,
            "year_end_total_assets_cny": account["total_assets_cny"],
            "cumulative_external_contributions_cny": account["cumulative_external_contributions_cny"],
            "cumulative_external_withdrawals_cny": account["cumulative_external_withdrawals_cny"],
            "cumulative_pnl_cny": performance["total_pnl_cny"],
            "cumulative_simple_return": performance["simple_return"],
        })
    for index, row in enumerate(annual):
        prior_assets = 0.0 if index == 0 else float(annual[index - 1]["year_end_total_assets_cny"])
        prior_contributions = 0.0 if index == 0 else float(annual[index - 1]["cumulative_external_contributions_cny"])
        prior_withdrawals = 0.0 if index == 0 else float(annual[index - 1]["cumulative_external_withdrawals_cny"])
        current_contributions = float(row["cumulative_external_contributions_cny"])
        current_withdrawals = float(row["cumulative_external_withdrawals_cny"])
        row["annual_net_pnl_cny"] = round(
            float(row["year_end_total_assets_cny"])
            + (current_withdrawals - prior_withdrawals)
            - prior_assets
            - (current_contributions - prior_contributions),
            2,
        )

    result = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": f"BACKTEST_QUALITY_SAMPLE_{sample_size}_REAL_DATA_PILOT",
        "status": "COMPLETED_NON_FORMAL_REAL_DATA_MECHANISM_TEST",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "window": {"start": str(START), "end": str(END), "trading_days": len(days)},
        "sample": sample,
        "selection_bias_warning": "The fixed sample was selected using current-era coverage information, so this result cannot measure historical selection efficacy.",
        "result": {"trade_count": len(trades), "by_symbol": by_symbol, "annual": annual, "terminal": terminal},
        "data_coverage": {
            "cd_months": len(cd_rows),
            "cd_missing_months": 0,
            "symbols": len(symbols),
            "price_rows": len(price_rows),
            "dividend_rows": len(dividend_rows),
            "dividend_fiscal_rows": len(fiscal_rows),
            "quality_rows": sum(len(rows) for rows in quality.values()),
        },
        "formal_result_eligible": False,
        "formal_blockers": [
            "fixed sample was selected using a 2026 radar and has hindsight selection bias",
            "quality thresholds are conservative pilot rules and not the frozen formal industry-specific contract",
            "historical ST/suspension and full company-action evidence are not complete",
            "monthly CD series is a single-observation ABC-first fallback pilot, not the formal two-bank median series",
            "user-supplied NAS price license and known_at are not promoted to formal status",
        ],
        "audit": {
            "event_chain_ok": run.ledger.audit_chain_ok(),
            "terminal_reconciliation_status": terminal["reconciliation"]["status"],
            "cash_reconciliation_difference_cny": reconciliation["CASH_ROLLFORWARD"],
            "total_pnl_check_difference_cny": reconciliation["TOTAL_PNL"],
            "pending_order_count": len(run.pending_orders),
            "future_data_imputed": False,
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    write_text_once(events_path, "".join(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n" for event in run.ledger.events))
    result["audit"]["ledger_events"] = {
        "path": str(events_path.relative_to(PROJECT_ROOT)),
        "sha256": sha256(events_path),
        "event_count": len(run.ledger.events),
    }
    write_once(result_path, result)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "status": result["status"],
        "scheduler_status": result["scheduler_status"],
        "inputs": [{"path": str(path.relative_to(PROJECT_ROOT)), "sha256": sha256(path)} for path in required],
        "output": {"path": str(result_path.relative_to(PROJECT_ROOT)), "sha256": sha256(result_path)},
        "ledger_events": {"path": str(events_path.relative_to(PROJECT_ROOT)), "sha256": sha256(events_path)},
        "created_at": result["created_at"],
    }
    write_once(receipt_path, receipt)
    write_once(audit_path, {
        "schema_version": "1.0.0",
        "event_type": f"BACKTEST_QUALITY_SAMPLE_{sample_size}_COMPLETED",
        "run_id": args.run_id,
        "status": result["status"],
        "receipt": {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256(receipt_path)},
        "created_at": result["created_at"],
    })
    print(json.dumps({
        "run_id": args.run_id,
        "status": result["status"],
        "trade_count": len(trades),
        "terminal_performance": terminal["performance"],
        "audit": result["audit"],
        "by_symbol": [{key: row[key] for key in ("symbol", "name", "trade_count", "terminal_total_pnl_cny", "terminal_position_state")} for row in by_symbol],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
