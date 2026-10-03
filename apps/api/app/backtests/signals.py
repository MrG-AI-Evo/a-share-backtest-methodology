from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Literal


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class CashDividendObservation:
    fiscal_year: int
    cash_per_share: Decimal
    known_at: str
    ex_date: date
    dividend_kind: Literal["ORDINARY", "SPECIAL"]


@dataclass(frozen=True)
class FiscalYearDividendResolution:
    fiscal_year: int
    total_cash_per_share: Decimal
    resolved_known_at: str


@dataclass(frozen=True)
class ConservativeDividend:
    d_ttm: Decimal
    d_med3: Decimal
    d_cons: Decimal
    resolved_fiscal_years: tuple[int, int, int]


def conservative_dividend(
    *,
    signal_at: str,
    cash_events: list[CashDividendObservation],
    fiscal_years: list[FiscalYearDividendResolution],
    include_special_dividends: bool = True,
) -> ConservativeDividend | None:
    signal_instant = _instant(signal_at)
    signal_date = signal_instant.date()
    cutoff = signal_date - timedelta(days=365)
    eligible_events = [
        item
        for item in cash_events
        if _instant(item.known_at) <= signal_instant
        and cutoff < item.ex_date <= signal_date
        and (include_special_dividends or item.dividend_kind != "SPECIAL")
    ]
    d_ttm = sum((item.cash_per_share for item in eligible_events), Decimal("0"))
    resolved = sorted(
        (item for item in fiscal_years if _instant(item.resolved_known_at) <= signal_instant),
        key=lambda item: item.fiscal_year,
        reverse=True,
    )[:3]
    if len(resolved) != 3:
        return None
    d_med3 = Decimal(str(median([item.total_cash_per_share for item in resolved])))
    return ConservativeDividend(
        d_ttm=d_ttm,
        d_med3=d_med3,
        d_cons=min(d_ttm, d_med3),
        resolved_fiscal_years=(
            resolved[0].fiscal_year,
            resolved[1].fiscal_year,
            resolved[2].fiscal_year,
        ),
    )


@dataclass(frozen=True)
class CDRateObservation:
    bank: Literal["ICBC", "ABC", "BOC", "CCB"]
    tenor_years: Literal[3, 5]
    annual_rate: Decimal
    known_at: str
    effective_start: date
    effective_end: date
    synthetic: bool = False


@dataclass(frozen=True)
class CDRateSignal:
    annual_rate: Decimal
    tenor_years: Literal[3, 5]
    quality: Literal["OBSERVED", "TENOR_FALLBACK"]
    banks: tuple[str, ...]


def aggregate_cd_rate(signal_at: str, observations: list[CDRateObservation]) -> CDRateSignal | None:
    signal_instant = _instant(signal_at)
    signal_date = signal_instant.date()
    preferred: Literal[3, 5] = 3 if signal_date <= date(2017, 12, 31) else 5
    tenors: tuple[Literal[3, 5], ...] = (3,) if preferred == 3 else (5, 3)
    for tenor in tenors:
        latest_by_bank: dict[str, CDRateObservation] = {}
        for item in observations:
            if (
                item.tenor_years != tenor
                or item.synthetic
                or _instant(item.known_at) > signal_instant
                or not item.effective_start <= signal_date <= item.effective_end
            ):
                continue
            previous = latest_by_bank.get(item.bank)
            if previous is None or item.effective_start > previous.effective_start:
                latest_by_bank[item.bank] = item
        if len(latest_by_bank) >= 2:
            ordered = sorted(latest_by_bank.values(), key=lambda item: item.bank)
            rate = Decimal(str(median([item.annual_rate for item in ordered])))
            return CDRateSignal(
                annual_rate=rate,
                tenor_years=tenor,
                quality="OBSERVED" if tenor == preferred else "TENOR_FALLBACK",
                banks=tuple(item.bank for item in ordered),
            )
    return None
