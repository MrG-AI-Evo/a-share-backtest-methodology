from datetime import date
from decimal import Decimal

from app.backtests.signals import (
    CashDividendObservation,
    CDRateObservation,
    FiscalYearDividendResolution,
    aggregate_cd_rate,
    conservative_dividend,
)


def test_dh013_dh016_dh017_dividend_is_point_in_time_and_includes_zero_and_special() -> None:
    events = [
        CashDividendObservation(
            2019, Decimal("0.20"), "2020-06-01T09:00:00+08:00", date(2020, 6, 10), "ORDINARY"
        ),
        CashDividendObservation(
            2019, Decimal("0.10"), "2020-06-01T09:00:00+08:00", date(2020, 6, 10), "SPECIAL"
        ),
        CashDividendObservation(
            2020, Decimal("0.50"), "2021-01-01T09:00:00+08:00", date(2021, 1, 10), "ORDINARY"
        ),
    ]
    years = [
        FiscalYearDividendResolution(2019, Decimal("0.30"), "2020-06-01T09:00:00+08:00"),
        FiscalYearDividendResolution(2018, Decimal("0"), "2019-06-01T09:00:00+08:00"),
        FiscalYearDividendResolution(2017, Decimal("0.20"), "2018-06-01T09:00:00+08:00"),
    ]
    result = conservative_dividend(
        signal_at="2020-06-10T15:00:00+08:00", cash_events=events, fiscal_years=years
    )
    assert result is not None
    assert result.d_ttm == Decimal("0.30")
    assert result.d_med3 == Decimal("0.20")
    assert result.d_cons == Decimal("0.20")
    without_special = conservative_dividend(
        signal_at="2020-06-10T15:00:00+08:00",
        cash_events=events,
        fiscal_years=years,
        include_special_dividends=False,
    )
    assert without_special is not None
    assert without_special.d_ttm == Decimal("0.20")


def test_dh026_future_known_dividend_and_future_fiscal_resolution_are_excluded() -> None:
    events = [
        CashDividendObservation(
            2019, Decimal("0.30"), "2020-06-11T09:00:00+08:00", date(2020, 6, 10), "ORDINARY"
        )
    ]
    years = [
        FiscalYearDividendResolution(2019, Decimal("0.30"), "2020-06-11T09:00:00+08:00"),
        FiscalYearDividendResolution(2018, Decimal("0.20"), "2019-06-01T09:00:00+08:00"),
        FiscalYearDividendResolution(2017, Decimal("0.10"), "2018-06-01T09:00:00+08:00"),
    ]
    assert (
        conservative_dividend(
            signal_at="2020-06-10T15:00:00+08:00", cash_events=events, fiscal_years=years
        )
        is None
    )


def test_dh009_dh011_dh012_cd_rate_requires_two_real_banks_and_falls_back() -> None:
    signal_at = "2020-01-02T15:00:00+08:00"
    observations = [
        CDRateObservation(
            "ICBC", 5, Decimal("0.04"), signal_at, date(2020, 1, 1), date(2020, 3, 31), True
        ),
        CDRateObservation(
            "ABC", 3, Decimal("0.031"), signal_at, date(2020, 1, 1), date(2020, 3, 31)
        ),
        CDRateObservation(
            "BOC", 3, Decimal("0.033"), signal_at, date(2020, 1, 1), date(2020, 3, 31)
        ),
    ]
    result = aggregate_cd_rate(signal_at, observations)
    assert result is not None
    assert result.tenor_years == 3
    assert result.quality == "TENOR_FALLBACK"
    assert result.annual_rate == Decimal("0.032")
    assert aggregate_cd_rate(signal_at, observations[:2]) is None
