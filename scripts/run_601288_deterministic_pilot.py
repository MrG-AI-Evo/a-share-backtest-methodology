#!/usr/bin/env python3
"""Run the immutable Agricultural Bank single-stock real-data pilot.

This foreground-only task intentionally stays outside the formal all-market result
set.  It uses staged user-supplied raw prices, point-in-time dividend notices,
officially archived CD observations and annual-report facts.  Missing CD days fail
closed; no rate or market datum is imputed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import yaml
from pypdf import PdfReader

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps/api"))

from app.backtests.runner import (  # noqa: E402
    CorporateActionDay,
    DeterministicHistoryRunner,
    FeeRule,
    SecurityDay,
    TradingDayInput,
)

SYMBOL = "601288"
NAME = "农业银行"
START = date(2016, 1, 4)
END = date(2025, 12, 31)
PRICE_PATH = PROJECT_ROOT / "data/backtests/staging/nas-daily-20260910-155000-nas-daily-staging/shsz-daily-raw-bars-staging-v1.parquet"
DIVIDEND_PATH = PROJECT_ROOT / "data/backtests/staging/20260911-004100-eastmoney-dividend-events-candidate/shsz-dividend-events-candidate.parquet"
CD_PATH = PROJECT_ROOT / "data/backtests/staging/official-cd-normalized-20260911-000400-official-cd-archive-v8/observations.json"
POLICY_PATH = PROJECT_ROOT / "config/dividend-hurdle-backtest-v4.yaml"
DEFAULT_PILOT_POLICY_PATH = PROJECT_ROOT / "config/dividend-hurdle-601288-pilot-v1.yaml"
QUALITY_PATHS = (
    PROJECT_ROOT / "data/backtests/staging/20260911-231500-cninfo-quality-extraction-full-v2/annual-report-fact-candidates.jsonl",
    PROJECT_ROOT / "data/backtests/staging/20260912-035000-601288-missing-reports-extract/annual-report-fact-candidates.jsonl",
)


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_once(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"immutable output already exists: {path}")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value)[:10])


def money(value: float) -> float:
    return round(value + 1e-10, 2)


def cd_signal(
    rows: list[dict[str, Any]], signal_date: date, bank_filter: str | None
) -> tuple[Decimal | None, int | None, str]:
    tenors = (3.0,) if signal_date <= date(2017, 12, 31) else (5.0, 3.0)
    for tenor in tenors:
        selected: list[float] = []
        banks = (bank_filter,) if bank_filter else ("ICBC", "ABC", "BOC", "CCB")
        for bank in banks:
            candidates: list[dict[str, Any]] = []
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
                if valid_from <= signal_date <= valid_to and known_at <= signal_date and row.get("annual_rate_decimal") is not None:
                    candidates.append(row)
            if candidates:
                chosen = min(
                    candidates,
                    key=lambda row: (
                        float(row["minimum_purchase_cny"]) if row.get("minimum_purchase_cny") is not None else float("inf"),
                        float(row["annual_rate_decimal"]),
                        str(row.get("product_name") or ""),
                        str(row.get("source_uri") or ""),
                    ),
                )
                selected.append(float(chosen["annual_rate_decimal"]))
        minimum_bank_count = 1 if bank_filter else 2
        if len(selected) >= minimum_bank_count:
            quality = "OBSERVED" if (signal_date <= date(2017, 12, 31) or tenor == 5.0) else "TENOR_FALLBACK"
            return Decimal(str(statistics.median(selected))), int(tenor), quality
    return None, None, "UNAVAILABLE"


def read_quality_reports() -> list[dict[str, Any]]:
    reports: dict[int, dict[str, Any]] = {}
    for path in QUALITY_PATHS:
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("source", {}).get("symbol") == SYMBOL:
                reports[int(row["document"]["report_year"])] = row
    result: list[dict[str, Any]] = []
    for year, row in sorted(reports.items()):
        pdf_path = Path(row["source"]["path"])
        if not pdf_path.is_file() or sha256(pdf_path) != row["source"]["sha256"]:
            raise RuntimeError(f"annual report missing or hash mismatch: {pdf_path}")
        text = "".join((page.extract_text() or "") for page in PdfReader(pdf_path).pages[:20])
        compact = text.replace(" ", "").replace("\n", "").replace("％", "%")

        def metric(pattern: str) -> float | None:
            match = re.search(pattern, compact)
            return float(match.group(1)) if match else None

        npl = metric(r"不良贷款率8(\d\.\d{2})")
        provision = metric(r"拨备覆盖率9(\d{3}\.\d{2})")
        capital = metric(r"(?<!一级)资本充足率1(\d{2}\.\d{2})")
        profit = next(
            (
                float(item["raw_value"].replace(",", "").replace("(", "-").replace(")", ""))
                for item in row.get("metric_candidates", [])
                if item.get("metric_id") == "net_profit_parent"
            ),
            None,
        )
        audit_ok = row.get("audit_opinion", {}).get("classification") == "STANDARD_UNQUALIFIED" or "出具无保留意见的审计报告" in compact
        # The project gate is absence of a disclosed material uncertainty, not
        # presence of one exact boilerplate sentence in the first twenty pages.
        # The extractor scans the complete report and records any such finding.
        going_concern_ok = row.get("going_concern", {}).get("status") not in {
            "MATERIAL_UNCERTAINTY_FOUND",
            "GOING_CONCERN_RISK_FOUND",
        }
        passed = bool(
            audit_ok
            and going_concern_ok
            and profit is not None
            and profit > 0
            and npl is not None
            and npl <= 5.0
            and provision is not None
            and provision >= 150.0
            and capital is not None
            and capital >= 10.5
        )
        result.append(
            {
                "report_year": year,
                "known_at": row["source"]["known_at"],
                "passed": passed,
                "audit_unqualified": audit_ok,
                "going_concern_basis_found": going_concern_ok,
                "net_profit_parent_positive": profit is not None and profit > 0,
                "npl_ratio_pct": npl,
                "provision_coverage_pct": provision,
                "capital_adequacy_pct": capital,
                "source_pdf_sha256": row["source"]["sha256"],
            }
        )
    return result


def fee_rule(day: date) -> FeeRule:
    return FeeRule(
        commission_rate=Decimal("0.0003"),
        minimum_commission_cny=Decimal("5"),
        stamp_duty_sell_rate=Decimal("0.001") if day < date(2023, 8, 28) else Decimal("0.0005"),
        transfer_fee_rate=Decimal("0.00002") if day < date(2022, 4, 29) else Decimal("0.00001"),
        exchange_and_regulatory_rate=Decimal("0.0000487") if day < date(2023, 8, 28) else Decimal("0.0000341"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--pilot-policy", type=Path, default=DEFAULT_PILOT_POLICY_PATH)
    parser.add_argument("--monthly-cd-series", type=Path)
    args = parser.parse_args()
    pilot_policy_path = args.pilot_policy.resolve()
    monthly_cd_path = args.monthly_cd_series.resolve() if args.monthly_cd_series else None
    required = (PRICE_PATH, DIVIDEND_PATH, CD_PATH, POLICY_PATH, pilot_policy_path, *QUALITY_PATHS)
    if monthly_cd_path:
        required = (*required, monthly_cd_path)
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"missing or empty input: {path}")
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    pilot_policy = yaml.safe_load(pilot_policy_path.read_text(encoding="utf-8"))
    cd_bank = str(pilot_policy["cd_signal"].get("bank") or "ABC_THEN_OTHER_BANK")
    if SYMBOL in {str(item["symbol"]) for item in policy.get("universe_exclusions", [])}:
        raise RuntimeError(f"{SYMBOL} is excluded by policy")

    output_dir = PROJECT_ROOT / "data/backtests/staging" / args.run_id
    result_path = output_dir / "agricultural-bank-single-stock-result.json"
    receipt_path = output_dir / "receipt.json"
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-single-stock-result.json"
    if output_dir.exists() or audit_path.exists():
        raise RuntimeError("run_id already exists; outputs are immutable")

    db = duckdb.connect()
    price_rows = db.execute(
        f"""SELECT effective_date, open, close, volume_shares, amount_cny
        FROM read_parquet('{PRICE_PATH}') WHERE symbol=? AND effective_date BETWEEN ? AND ?
        ORDER BY effective_date""",
        [SYMBOL, START, END],
    ).fetchall()
    dividend_rows = [
        {
            "fiscal_year": row[0].year,
            "plan_known": row[1],
            "record_date": row[2],
            "ex_date": row[3],
            "cash": float(row[4]) / 10.0,
        }
        for row in db.execute(
            f"""SELECT report_date, plan_notice_date, equity_record_date, ex_dividend_date, pretax_bonus_rmb
            FROM read_parquet('{DIVIDEND_PATH}') WHERE security_code=? AND ex_dividend_date IS NOT NULL
            AND plan_notice_date IS NOT NULL AND pretax_bonus_rmb IS NOT NULL ORDER BY report_date""",
            [SYMBOL],
        ).fetchall()
    ]
    if len(price_rows) != 2430 or price_rows[0][0] != START or price_rows[-1][0] != END:
        raise RuntimeError("price window is not the expected complete 2016-2025 trading series")
    cd_rows = json.loads(CD_PATH.read_text(encoding="utf-8"))
    quality_reports = read_quality_reports()

    fiscal_totals: dict[int, float] = {}
    first_known_by_fiscal: dict[int, date] = {}
    for row in dividend_rows:
        fiscal_totals[row["fiscal_year"]] = fiscal_totals.get(row["fiscal_year"], 0.0) + row["cash"]
        first_known_by_fiscal[row["fiscal_year"]] = min(
            first_known_by_fiscal.get(row["fiscal_year"], row["plan_known"]), row["plan_known"]
        )
    resolved_at: dict[int, date] = {}
    fiscal_years = sorted(fiscal_totals)
    for fiscal_year in fiscal_years:
        later = [first_known_by_fiscal[y] for y in fiscal_years if y > fiscal_year]
        if later:
            resolved_at[fiscal_year] = min(later)

    price_dates = [row[0] for row in price_rows]
    first_month_dates = {(d.year, d.month): d for d in reversed(price_dates)}
    monthly_cd_rows: list[dict[str, Any]] = []
    monthly_cd_by_month: dict[str, dict[str, Any]] = {}
    monthly_checkpoint_dates: set[date] = set()
    if monthly_cd_path:
        monthly_cd_rows = json.loads(monthly_cd_path.read_text(encoding="utf-8"))
        if len(monthly_cd_rows) != 120 or len({row["month"] for row in monthly_cd_rows}) != 120:
            raise RuntimeError("monthly CD input is not exactly 120 unique months")
        for row in monthly_cd_rows:
            signal_date = as_date(row.get("signal_date"))
            known_at = as_date(row.get("known_at"))
            if not signal_date or not known_at or known_at > signal_date:
                raise RuntimeError(f"monthly CD future-data violation: {row.get('month')}")
            monthly_cd_by_month[str(row["month"])] = row
            monthly_checkpoint_dates.add(signal_date)

        def monthly_signal(trade_date: date) -> tuple[Decimal | None, int | None, str]:
            row = monthly_cd_by_month.get(trade_date.strftime("%Y-%m"))
            if not row or trade_date < as_date(row["signal_date"]):
                return None, None, "UNAVAILABLE"
            tenor = int(float(row["tenor_years"]))
            quality = "TENOR_FALLBACK" if row.get("tenor_fallback") else "OBSERVED"
            return Decimal(str(row["annual_rate_decimal"])), tenor, quality

        cd_cache = {d: monthly_signal(d) for d in price_dates}
    else:
        cd_cache = {d: cd_signal(cd_rows, d, cd_bank) for d in price_dates}
    cd_covered = sum(value[0] is not None for value in cd_cache.values())

    actions_by_date: dict[date, tuple[CorporateActionDay, ...]] = {}
    for row in dividend_rows:
        ex_date = row["ex_date"]
        if START <= ex_date <= END:
            known_at = f"{ex_date.isoformat()}T09:30:00+08:00"
            actions_by_date[ex_date] = (
                CorporateActionDay(SYMBOL, "DIVIDEND_RECEIVABLE", known_at, cash_per_share=Decimal(str(row["cash"])), evidence_ids=("EASTMONEY_DIVIDEND_CANDIDATE",)),
                CorporateActionDay(SYMBOL, "DIVIDEND_PAYMENT", known_at, evidence_ids=("EASTMONEY_DIVIDEND_CANDIDATE",)),
            )

    days: list[TradingDayInput] = []
    for trade_date, open_price, close_price, volume_shares, amount_cny in price_rows:
        signal_at = f"{trade_date.isoformat()}T15:00:00+08:00"
        ttm_start = trade_date - timedelta(days=365)
        d_ttm = sum(
            row["cash"] for row in dividend_rows
            if ttm_start < row["ex_date"] <= trade_date and row["plan_known"] <= trade_date
        )
        resolved = sorted(
            (year for year, known in resolved_at.items() if known <= trade_date), reverse=True
        )[:3]
        d_med3 = statistics.median(fiscal_totals[year] for year in resolved) if len(resolved) == 3 else None
        available_quality = [
            report for report in quality_reports if report["known_at"] <= signal_at
        ]
        quality_pass = available_quality[-1]["passed"] if available_quality else None
        rate, tenor, rate_quality = cd_cache[trade_date]
        tradable = bool(open_price and close_price and volume_shares and volume_shares > 0)
        days.append(
            TradingDayInput(
                trade_date=trade_date.isoformat(),
                is_first_trading_day_of_month=(
                    trade_date in monthly_checkpoint_dates
                    if monthly_cd_path
                    else first_month_dates[(trade_date.year, trade_date.month)] == trade_date
                ),
                securities=(
                    SecurityDay(
                        symbol=SYMBOL,
                        name=NAME,
                        exchange="SSE",
                        ownership_type="CENTRAL_SOE",
                        open_price=Decimal(str(open_price)) if open_price else None,
                        close_price=Decimal(str(close_price)) if close_price else None,
                        tradable_open=tradable,
                        tradable_close=tradable,
                        risk_status_eligible=True,
                        quality_pass=quality_pass,
                        d_ttm=Decimal(str(d_ttm)),
                        d_med3=Decimal(str(d_med3)) if d_med3 is not None else None,
                        cd_rate=rate,
                        cd_tenor_years=tenor,
                        cd_rate_quality=rate_quality,  # type: ignore[arg-type]
                        median_amount_20d_cny=Decimal(str(amount_cny)) if amount_cny else None,
                        evidence_ids=("NAS_DAILY_RAW", "EASTMONEY_DIVIDEND_CANDIDATE", "MONTHLY_CD_RATE_SERIES" if monthly_cd_path else "OFFICIAL_CD_ARCHIVE", "CNINFO_ANNUAL_REPORT"),
                    ),
                ),
                fee_rule=fee_rule(trade_date),
                buy_slippage_bps=Decimal("5"),
                sell_slippage_bps=Decimal("5"),
                corporate_actions=actions_by_date.get(trade_date, ()),
            )
        )

    runner = DeterministicHistoryRunner(
        args.run_id,
        pilot_policy["policy_version"],
        run_purpose="MECHANISM_TEST",
        scenario="BASE",
        raw_data_mode="REAL_POINT_IN_TIME",
        synthetic_raw_observation_count=0,
    )
    run = runner.run(days)
    terminal = run.checkpoints[-1]
    terminal_security_ledgers = terminal["security_ledgers"]
    terminal_realized = money(
        sum(
            float(item["total_pnl_cny"])
            for item in terminal_security_ledgers
            if item["position_state"] == "CLOSED"
        )
    )
    terminal_unrealized = money(
        sum(
            float(item["total_pnl_cny"])
            for item in terminal_security_ledgers
            if item["position_state"] == "OPEN"
        )
    )
    terminal["performance"]["realized_pnl_cny"] = terminal_realized
    terminal["performance"]["unrealized_pnl_cny"] = terminal_unrealized
    annual = []
    for year in range(2016, 2026):
        checkpoint = next(item for item in reversed(run.checkpoints) if str(item["as_of"]).startswith(str(year)))
        account = checkpoint["account_summary"]
        performance = checkpoint["performance"]
        annual.append(
            {
                "year": year,
                "year_end_total_assets_cny": account["total_assets_cny"],
                "cumulative_external_contributions_cny": account["cumulative_external_contributions_cny"],
                "cumulative_pnl_cny": performance["total_pnl_cny"],
                "cumulative_simple_return": performance["simple_return"],
            }
        )
    for index, row in enumerate(annual):
        prior_assets = 0.0 if index == 0 else float(annual[index - 1]["year_end_total_assets_cny"])
        prior_contrib = 0.0 if index == 0 else float(annual[index - 1]["cumulative_external_contributions_cny"])
        current_contrib = float(row["cumulative_external_contributions_cny"])
        row["annual_net_pnl_cny"] = money(float(row["year_end_total_assets_cny"]) - prior_assets - (current_contrib - prior_contrib))

    trades = [
        {
            "event": event["event_type"],
            "occurred_at": event["occurred_at"],
            **event["payload"],
        }
        for event in run.ledger.events
        if event["event_type"] in {"BUY_FILL", "SELL_FILL"}
    ]
    reconciliation_checks = {
        str(item["name"]): float(item["difference"])
        for item in terminal["reconciliation"]["checks"]
    }
    result = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_SINGLE_STOCK_REAL_DATA_PILOT",
        "status": "COMPLETED_NON_FORMAL_REAL_DATA_PILOT",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "symbol": SYMBOL,
        "name": NAME,
        "window": {"start": str(START), "end": str(END), "trading_days": len(days)},
        "result": {
            "trade_count": len(trades),
            "trades": trades,
            "terminal": terminal,
            "annual": annual,
        },
        "data_coverage": {
            "price_days": len(price_rows),
            "dividend_events": len(dividend_rows),
            "quality_reports": quality_reports,
            "cd_covered_trading_days": cd_covered,
            "cd_total_trading_days": len(price_dates),
            "cd_coverage_ratio": round(cd_covered / len(price_dates), 6),
            "cd_signal_bank": cd_bank,
            "cd_month_count": len(monthly_cd_rows) if monthly_cd_path else None,
            "cd_other_bank_months": sum(row["bank"] != "ABC" for row in monthly_cd_rows) if monthly_cd_path else None,
            "missing_cd_behavior": pilot_policy["cd_signal"]["missing_observation_behavior"],
        },
        "pilot_assumptions": {
            "broker_commission": "0.03% each side, minimum CNY 5",
            "slippage": "5 bps adverse, embedded in fill price",
            "statutory_fees": "date-effective stamp duty, transfer and exchange handling schedule",
            "dividend_payment": "credited on ex-date because staged dataset lacks a separate pay date",
            "dividend_tax": "no tax accrued unless a sale occurs within the statutory holding-period windows",
            "bank_quality_gate": "annual-report unqualified audit, no extracted material going-concern uncertainty, positive profit, capital>=10.5%, NPL<=5%, provision>=150%",
            "fiscal_dividend_resolution": "a fiscal year becomes complete only when the next fiscal year's first dividend disclosure is known",
        },
        "formal_result_eligible": False,
        "formal_blockers": [
            "user-supplied NAS price license and known_at are not promoted to formal status",
            "monthly rate pilot uses ABC first and another bank only for ABC-missing months; it is not the formal frozen multi-bank median contract",
            "one monthly rate observation (2022-12 ICBC) is supported by a same-day tier-3 financial-news source rather than a bank archive",
            "broker commission and 5 bps slippage are disclosed pilot assumptions",
            "dividend pay date is conservatively represented by ex-date in this pilot",
            "single-stock pilot is not the formal all-market Core20 result",
        ],
        "audit": {
            "event_chain_ok": run.ledger.audit_chain_ok(),
            "terminal_reconciliation_status": terminal["reconciliation"]["status"],
            "terminal_cash_reconciliation_difference_cny": reconciliation_checks["CASH_ROLLFORWARD"],
            "terminal_total_pnl_check_difference_cny": reconciliation_checks["TOTAL_PNL"],
            "pending_order_count": len(run.pending_orders),
            "future_data_imputed": False,
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    write_once(result_path, result)
    receipt = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "status": result["status"],
        "scheduler_status": result["scheduler_status"],
        "inputs": [{"path": str(path.relative_to(PROJECT_ROOT)), "sha256": sha256(path)} for path in required],
        "output": {"path": str(result_path.relative_to(PROJECT_ROOT)), "sha256": sha256(result_path)},
        "created_at": result["created_at"],
    }
    write_once(receipt_path, receipt)
    audit_core = {
        "event_id": f"{args.run_id}-audit",
        "run_id": args.run_id,
        "event_type": "BACKTEST_SINGLE_STOCK_REAL_DATA_PILOT_COMPLETED",
        "actor": "deterministic-local-python",
        "occurred_at": result["created_at"],
        "payload": {"symbol": SYMBOL, "status": result["status"], "receipt_sha256": sha256(receipt_path)},
        "prev_event_hash": None,
    }
    audit_core["event_hash"] = hashlib.sha256(json.dumps(audit_core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    write_once(audit_path, audit_core)
    print(json.dumps({"result": result["result"], "data_coverage": result["data_coverage"], "audit": result["audit"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
