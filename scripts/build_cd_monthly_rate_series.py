#!/usr/bin/env python3
"""Build a no-lookahead 2016-2025 monthly personal-CD signal series.

Agricultural Bank is preferred.  A different bank is selected only when no
eligible ABC target-tenor product overlaps the calendar month.  Six audited
gap observations close the residual monthly coverage.  This is a manual,
foreground-only data build and does not run a backtest.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import statistics
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVATIONS_PATH = PROJECT_ROOT / "data/backtests/staging/official-cd-normalized-20260911-000400-official-cd-archive-v8/observations.json"
PRICE_PATH = PROJECT_ROOT / "data/backtests/staging/nas-daily-20260910-155000-nas-daily-staging/shsz-daily-raw-bars-staging-v1.parquet"
START = date(2016, 1, 1)
END = date(2025, 12, 31)
BANK_PRIORITY = ("ABC", "ICBC", "BOC", "CCB")

# These records transcribe explicit historical observations from the saved
# source snapshots.  A point observation is usable only on/after known_at.
GAP_OBSERVATIONS = (
    {
        "months": ("2019-10",),
        "bank": "BANKWF",
        "bank_name": "潍坊银行",
        "tenor_years": 3,
        "annual_rate_decimal": 0.0418,
        "minimum_purchase_cny": 200000,
        "product_name": "潍坊银行第85期个人大额存单（3年期20万起点档）",
        "known_at": "2019-10-17",
        "valid_from": "2019-10-18",
        "valid_to": "2019-10-31",
        "source_slug": "bankwf-2019-10-3y",
        "source_tier": 1,
        "evidence_mode": "OFFICIAL_PRODUCT_POINT_OBSERVATION",
    },
    {
        "months": ("2020-05",),
        "bank": "NCBANK",
        "bank_name": "宁波通商银行",
        "tenor_years": 3,
        "annual_rate_decimal": 0.042,
        "minimum_purchase_cny": 200000,
        "product_name": "2020年第16-21期个人大额存单（3年期）",
        "known_at": "2020-05-08",
        "valid_from": "2020-05-08",
        "valid_to": "2020-05-31",
        "source_slug": "ncbank-2020-05-3y",
        "source_tier": 1,
        "evidence_mode": "OFFICIAL_PRODUCT_POINT_OBSERVATION",
    },
    {
        "months": ("2020-07", "2020-08"),
        "bank": "GRCB",
        "bank_name": "广州农村商业银行",
        "tenor_years": 3,
        "annual_rate_decimal": 0.04015,
        "minimum_purchase_cny": 200000,
        "product_name": "2020年第3期个人大额存单（3年期到期付息）",
        "known_at": "2020-05-27",
        "valid_from": "2020-06-01",
        "valid_to": "2020-08-31",
        "source_slug": "grcb-2020-06-08-3y",
        "source_tier": 1,
        "evidence_mode": "OFFICIAL_EXPLICIT_SALE_WINDOW",
    },
    {
        "months": ("2021-07",),
        "bank": "JNBANK",
        "bank_name": "济宁银行",
        "tenor_years": 3,
        "annual_rate_decimal": 0.0418,
        "minimum_purchase_cny": 200000,
        "product_name": "2021年第1期个人大额存单（3年期）",
        "known_at": "2021-01-06",
        "valid_from": "2021-01-06",
        "valid_to": "2021-12-31",
        "source_slug": "jnbank-2021-full-year-3y",
        "source_tier": 1,
        "evidence_mode": "OFFICIAL_EXPLICIT_SALE_WINDOW",
    },
    {
        "months": ("2022-12",),
        "bank": "ICBC",
        "bank_name": "中国工商银行",
        "tenor_years": 3,
        "annual_rate_decimal": 0.031,
        "minimum_purchase_cny": None,
        "product_name": "2022-12-26工行3年期个人大额存单当日观察",
        "known_at": "2022-12-26",
        "valid_from": "2022-12-26",
        "valid_to": "2022-12-26",
        "source_slug": "cnfin-2022-12-icbc-3y",
        "source_tier": 3,
        "evidence_mode": "AUTHORITATIVE_MEDIA_SAME_DAY_OBSERVATION",
    },
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return date.fromisoformat(str(value)[:10])


def month_keys() -> list[str]:
    return [f"{year:04d}-{month:02d}" for year in range(2016, 2026) for month in range(1, 13)]


def tenor_order(month: str) -> tuple[float, ...]:
    return (3.0,) if month < "2018-01" else (5.0, 3.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--supplement-receipt", required=True, type=Path)
    args = parser.parse_args()
    receipt_path = args.supplement_receipt.resolve()
    for path in (OBSERVATIONS_PATH, PRICE_PATH, receipt_path):
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"missing or empty input: {path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("summary", {}).get("failed") != 0:
        raise RuntimeError("supplement download is incomplete")
    source_by_slug = {record["slug"]: record for record in receipt["records"] if record.get("slug")}
    required_slugs = {row["source_slug"] for row in GAP_OBSERVATIONS}
    if not required_slugs.issubset(source_by_slug):
        raise RuntimeError(f"missing source snapshots: {sorted(required_slugs - source_by_slug.keys())}")

    observations = json.loads(OBSERVATIONS_PATH.read_text(encoding="utf-8"))
    con = duckdb.connect()
    trading_dates = [row[0] for row in con.execute(
        f"SELECT effective_date FROM read_parquet('{PRICE_PATH}') WHERE symbol='601288' "
        f"AND effective_date BETWEEN DATE '2016-01-04' AND DATE '2025-12-31' ORDER BY effective_date"
    ).fetchall()]
    trading_by_month: dict[str, list[date]] = {}
    for trading_date in trading_dates:
        trading_by_month.setdefault(trading_date.strftime("%Y-%m"), []).append(trading_date)

    output: list[dict[str, Any]] = []
    for month in month_keys():
        year, month_number = map(int, month.split("-"))
        month_start = date(year, month_number, 1)
        month_end = date(year, month_number, calendar.monthrange(year, month_number)[1])
        chosen: dict[str, Any] | None = None
        chosen_tenor: float | None = None
        for tenor in tenor_order(month):
            for bank in BANK_PRIORITY:
                candidates = []
                for row in observations:
                    if row.get("bank") != bank or row.get("customer_scope") != "PERSONAL":
                        continue
                    if float(row.get("tenor_years") or -1) != tenor or not row.get("observation_eligible_candidate"):
                        continue
                    valid_from = parse_date(row.get("valid_from"))
                    valid_to = parse_date(row.get("valid_to"))
                    known_at = parse_date(row.get("known_at_candidate"))
                    if not valid_from or not valid_to or not known_at:
                        continue
                    usable_from = max(valid_from, known_at, month_start)
                    if usable_from <= min(valid_to, month_end):
                        candidates.append((row, usable_from))
                if candidates:
                    candidates.sort(key=lambda item: (
                        float(item[0].get("minimum_purchase_cny")) if item[0].get("minimum_purchase_cny") is not None else float("inf"),
                        float(item[0]["annual_rate_decimal"]),
                        str(item[0].get("product_name") or ""),
                    ))
                    row, usable_from = candidates[0]
                    chosen = {
                        "bank": bank,
                        "bank_name": {"ABC": "中国农业银行", "ICBC": "中国工商银行", "BOC": "中国银行", "CCB": "中国建设银行"}[bank],
                        "annual_rate_decimal": row["annual_rate_decimal"],
                        "minimum_purchase_cny": row.get("minimum_purchase_cny"),
                        "product_name": row.get("product_name"),
                        "known_at": row.get("known_at_candidate"),
                        "valid_from": row.get("valid_from"),
                        "valid_to": row.get("valid_to"),
                        "source_uri": row.get("source_uri"),
                        "source_path": row.get("raw_file"),
                        "source_sha256": row.get("source_asset_sha256"),
                        "source_tier": 1,
                        "evidence_mode": "OFFICIAL_EXPLICIT_SALE_WINDOW",
                        "selection_reason": "ABC_PREFERRED" if bank == "ABC" else "ABC_MISSING_OTHER_BIG_FOUR_FALLBACK",
                        "usable_from": usable_from.isoformat(),
                    }
                    chosen_tenor = tenor
                    break
            if chosen:
                break
        if chosen is None:
            supplement = next((item for item in GAP_OBSERVATIONS if month in item["months"]), None)
            if supplement is None:
                raise RuntimeError(f"uncovered month without audited supplement: {month}")
            source = source_by_slug[supplement["source_slug"]]
            chosen = {
                **{key: value for key, value in supplement.items() if key not in {"months", "source_slug"}},
                "source_uri": source["source_uri"],
                "source_path": source["path"],
                "source_sha256": source["sha256"],
                "selection_reason": "ABC_MISSING_AUDITED_OTHER_BANK_FALLBACK",
                "usable_from": max(parse_date(supplement["known_at"]), parse_date(supplement["valid_from"]), month_start).isoformat(),
            }
            chosen_tenor = float(supplement["tenor_years"])

        dates = trading_by_month.get(month, [])
        usable_from_date = parse_date(chosen["usable_from"])
        signal_date = next((item for item in dates if item >= usable_from_date), None)
        if signal_date is None:
            raise RuntimeError(f"no trading checkpoint on/after known date in {month}")
        output.append({
            "month": month,
            "signal_date": signal_date.isoformat(),
            "rate_known_at_or_before_signal": parse_date(chosen["known_at"]) <= signal_date,
            "tenor_years": chosen_tenor,
            "tenor_fallback": month >= "2018-01" and chosen_tenor == 3.0,
            **chosen,
        })

    if len(output) != 120 or len({row["month"] for row in output}) != 120:
        raise RuntimeError("monthly series is not exactly 120 unique months")
    if not all(row["rate_known_at_or_before_signal"] for row in output):
        raise RuntimeError("future-data check failed")

    output_dir = PROJECT_ROOT / "data/backtests/staging" / args.run_id
    audit_path = PROJECT_ROOT / "data/audit" / f"{args.run_id}-monthly-cd-rate-series.json"
    if output_dir.exists() or audit_path.exists():
        raise RuntimeError("run_id already exists; outputs are immutable")
    output_dir.mkdir(parents=True)
    series_path = output_dir / "monthly-cd-rate-series.json"
    series_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rates = [float(row["annual_rate_decimal"]) for row in output]
    fallback_rows = [row for row in output if row["selection_reason"] != "ABC_PREFERRED"]
    result = {
        "schema_version": "1.0.0",
        "run_id": args.run_id,
        "workflow": "BACKTEST_MONTHLY_CD_RATE_DATA_BUILD",
        "status": "COMPLETE_120_MONTH_POINT_IN_TIME_SERIES_NON_FORMAL",
        "scheduler_status": "DISABLED_MANUAL_ONLY",
        "window": {"start": "2016-01", "end": "2025-12", "month_count": 120},
        "selection_policy": [
            "target tenor is 3Y in 2016-2017, then 5Y with 3Y fallback",
            "ABC is selected first when an eligible observed product overlaps the month",
            "another bank is used only when ABC has no eligible observed product in that month",
            "a source published during a month is usable only from its known_at date",
            "no carried, interpolated, averaged, or model-generated rate is used",
        ],
        "coverage": {
            "covered_months": len(output),
            "missing_months": 0,
            "abc_months": sum(row["bank"] == "ABC" for row in output),
            "other_bank_months": len(fallback_rows),
            "tier1_months": sum(row["source_tier"] == 1 for row in output),
            "tier3_months": sum(row["source_tier"] == 3 for row in output),
            "rate_min": min(rates),
            "rate_max": max(rates),
        },
        "fallback_months": [{key: row[key] for key in ("month", "signal_date", "bank", "bank_name", "tenor_years", "annual_rate_decimal", "source_tier", "source_uri")} for row in fallback_rows],
        "inputs": [
            {"path": str(OBSERVATIONS_PATH.relative_to(PROJECT_ROOT)), "sha256": sha256(OBSERVATIONS_PATH)},
            {"path": str(PRICE_PATH.relative_to(PROJECT_ROOT)), "sha256": sha256(PRICE_PATH)},
            {"path": str(receipt_path.relative_to(PROJECT_ROOT)), "sha256": sha256(receipt_path)},
        ],
        "artifact": {"path": str(series_path.relative_to(PROJECT_ROOT)), "sha256": sha256(series_path)},
        "formal_eligibility": {
            "eligible": False,
            "reason": "single-bank/fallback pilot series; one month uses a tier-3 same-day observation and formal Core20 still requires its frozen multi-bank contract",
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
    receipt_out = output_dir / "receipt.json"
    receipt_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps({
        "schema_version": "1.0.0",
        "event_type": "BACKTEST_MONTHLY_CD_RATE_SERIES_BUILT",
        "run_id": args.run_id,
        "status": result["status"],
        "scheduler_status": result["scheduler_status"],
        "receipt": {"path": str(receipt_out.relative_to(PROJECT_ROOT)), "sha256": sha256(receipt_out)},
        "created_at": result["created_at"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["coverage"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
