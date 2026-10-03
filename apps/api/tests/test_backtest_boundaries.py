from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.backtests.engine import DeterministicLedger
from app.backtests.performance import link_subperiod_returns, xirr
from app.backtests.runner import (
    CorporateActionDay,
    DeterministicHistoryRunner,
    FeeRule,
    SecurityDay,
    TradingDayInput,
)

FEE = FeeRule(
    commission_rate=Decimal("0.0003"),
    minimum_commission_cny=Decimal("5"),
    stamp_duty_sell_rate=Decimal("0.001"),
    transfer_fee_rate=Decimal("0.00001"),
)


def security(symbol: str = "601288", **changes: Any) -> SecurityDay:
    value = SecurityDay(
        symbol=symbol,
        name=f"标的{symbol}",
        exchange="SSE",
        ownership_type="CENTRAL_SOE",
        open_price=Decimal("10"),
        close_price=Decimal("10"),
        tradable_open=True,
        tradable_close=True,
        risk_status_eligible=True,
        quality_pass=True,
        d_ttm=Decimal("0.48"),
        d_med3=Decimal("0.50"),
        cd_rate=Decimal("0.018"),
        cd_tenor_years=5,
        cd_rate_quality="OBSERVED",
        median_amount_20d_cny=Decimal("100000000"),
        evidence_ids=("price", "dividend", "cd-rate"),
    )
    return replace(value, **changes)


def day(
    trade_date: str,
    *securities: SecurityDay,
    month_start: bool = False,
    actions: tuple[CorporateActionDay, ...] = (),
) -> TradingDayInput:
    return TradingDayInput(
        trade_date=trade_date,
        is_first_trading_day_of_month=month_start,
        securities=tuple(securities),
        fee_rule=FEE,
        buy_slippage_bps=Decimal("0"),
        sell_slippage_bps=Decimal("0"),
        corporate_actions=actions,
    )


def test_dh006_reentry_creates_independent_episode() -> None:
    ledger = DeterministicLedger("20260827-boundary-reentry")
    ledger.buy(
        symbol="601288",
        name="农业银行",
        exchange="SSE",
        price=Decimal("10"),
        quantity=1000,
        transaction_costs=Decimal("5"),
        occurred_at="2020-01-02T09:30:00+08:00",
        known_at="2020-01-02T09:30:00+08:00",
    )
    ledger.sell(
        symbol="601288",
        price=Decimal("11"),
        transaction_costs=Decimal("12"),
        occurred_at="2020-02-02T09:30:00+08:00",
        known_at="2020-02-02T09:30:00+08:00",
    )
    ledger.buy(
        symbol="601288",
        name="农业银行",
        exchange="SSE",
        price=Decimal("9"),
        quantity=1000,
        transaction_costs=Decimal("5"),
        occurred_at="2020-03-02T09:30:00+08:00",
        known_at="2020-03-02T09:30:00+08:00",
    )
    assert ledger.positions["601288"].episode_count == 2


def test_dh019_voluntary_rights_are_skipped_without_cash_or_shares() -> None:
    action = CorporateActionDay(
        symbol="601288",
        action_type="VOLUNTARY_RIGHTS_SKIPPED",
        known_at="2020-06-01T09:00:00+08:00",
    )
    runner = DeterministicHistoryRunner("20260827-boundary-rights", "policy-v2-test")
    result = runner.run(
        [
            day("2020-01-02", security(), month_start=True),
            day("2020-01-03", security()),
            day("2020-06-01", security(), actions=(action,)),
        ]
    )
    assert result.ledger.positions["601288"].quantity == 1000
    event = next(e for e in result.ledger.events if e["event_type"] == "CORPORATE_ACTION")
    assert event["payload"]["cash_delta_cny"] == 0.0  # type: ignore[index]


def test_dh020_dividend_tax_is_separate_once_and_ledger_closes() -> None:
    ledger = DeterministicLedger("20260827-boundary-tax")
    ledger.buy(
        symbol="601288",
        name="农业银行",
        exchange="SSE",
        price=Decimal("10"),
        quantity=1000,
        transaction_costs=Decimal("5"),
        occurred_at="2020-01-02T09:30:00+08:00",
        known_at="2020-01-02T09:30:00+08:00",
    )
    ledger.declare_dividend(
        symbol="601288",
        cash_per_share=Decimal("0.20"),
        occurred_at="2020-06-01T09:00:00+08:00",
        known_at="2020-06-01T09:00:00+08:00",
    )
    ledger.pay_dividend(
        symbol="601288",
        occurred_at="2020-06-10T09:00:00+08:00",
        known_at="2020-06-10T09:00:00+08:00",
    )
    ledger.sell(
        symbol="601288",
        price=Decimal("11"),
        transaction_costs=Decimal("16"),
        occurred_at="2020-06-11T09:30:00+08:00",
        known_at="2020-06-11T09:30:00+08:00",
    )
    ledger.apply_dividend_tax(
        symbol="601288",
        amount=Decimal("20"),
        occurred_at="2020-06-11T09:31:00+08:00",
        known_at="2020-06-11T09:31:00+08:00",
    )
    snapshot = ledger.snapshot(as_of="2020-06-11T15:00:00+08:00", policy_version="policy-v2-test")
    assert snapshot["reconciliation"]["status"] == "PASS"  # type: ignore[index]
    assert sum(e["event_type"] == "DIVIDEND_TAX" for e in ledger.events) == 1


def test_dh021_same_open_sells_before_buy_and_reuses_cash() -> None:
    held = security("601288")
    replacement = security("600900", open_price=Decimal("9"), close_price=Decimal("9"))
    held_below = replace(held, open_price=Decimal("11"), close_price=Decimal("10.0001"))
    runner = DeterministicHistoryRunner("20260827-boundary-cash-reuse", "policy-v2-test")
    result = runner.run(
        [
            day("2020-01-02", held, month_start=True),
            day("2020-01-03", held),
            day("2020-02-03", held_below, replacement, month_start=True),
            day("2020-02-04", held_below, replacement),
        ]
    )
    fills = [
        event
        for event in result.ledger.events
        if event["event_type"] in {"SELL_FILL", "BUY_FILL"}
        and str(event["occurred_at"]).startswith("2020-02-04")
    ]
    assert [event["event_type"] for event in fills] == ["SELL_FILL", "BUY_FILL"]
    assert result.ledger.contributions == Decimal("10005.10")
    assert result.ledger.positions["600900"].quantity == 1000


def test_dh022_exact_shortfall_and_dh023_zero_denominator() -> None:
    empty = DeterministicLedger("20260827-boundary-empty")
    empty_snapshot = empty.snapshot(as_of="2020-01-01T15:00:00+08:00", policy_version="policy")
    assert empty_snapshot["performance"]["simple_return"] is None  # type: ignore[index]
    ledger = DeterministicLedger("20260827-boundary-shortfall")
    ledger.cash = Decimal("3000")
    ledger.buy(
        symbol="601288",
        name="农业银行",
        exchange="SSE",
        price=Decimal("10"),
        quantity=1000,
        transaction_costs=Decimal("10"),
        occurred_at="2020-01-02T09:30:00+08:00",
        known_at="2020-01-02T09:30:00+08:00",
    )
    assert ledger.contributions == Decimal("7010.00")


def test_dh024_no_forced_liquidation_and_dh025_unknown_terminal_zero_lower_bound() -> None:
    ledger = DeterministicLedger("20260827-boundary-terminal")
    ledger.buy(
        symbol="601288",
        name="农业银行",
        exchange="SSE",
        price=Decimal("10"),
        quantity=1000,
        transaction_costs=Decimal("5"),
        occurred_at="2020-01-02T09:30:00+08:00",
        known_at="2020-01-02T09:30:00+08:00",
    )
    ledger.mark("601288", Decimal("12"), "2020-12-31T15:00:00+08:00", "2020-12-31T15:00:00+08:00")
    assert ledger.positions["601288"].quantity == 1000
    assert not any(e["event_type"] == "SELL_FILL" for e in ledger.events)
    ledger.terminal_recovery(
        symbol="601288",
        recovery_cny=Decimal("0"),
        occurred_at="2021-01-04T09:30:00+08:00",
        known_at="2021-01-04T09:30:00+08:00",
        unresolved=True,
    )
    event = ledger.events[-1]
    assert event["payload"]["valuation_policy"] == "ZERO_REALIZABLE_LOWER_BOUND"  # type: ignore[index]


def test_dh028_effective_fee_rule_changes_are_applied_per_trade_date() -> None:
    old = FeeRule(Decimal("0.0003"), Decimal("5"), Decimal("0.001"), Decimal("0"))
    new = FeeRule(Decimal("0.0003"), Decimal("5"), Decimal("0.0005"), Decimal("0"))
    gross = Decimal("100000")
    assert old.costs("SELL", gross) - new.costs("SELL", gross) == Decimal("50.00")
    assert old.costs("BUY", gross) == new.costs("BUY", gross)


def test_mwrr_and_twr_are_cash_flow_aware() -> None:
    rate = xirr(
        [(date(2020, 1, 1), Decimal("-100"))],
        terminal_date=date(2021, 1, 1),
        terminal_equity=Decimal("110"),
    )
    assert rate == pytest.approx(0.10, abs=0.001)
    assert link_subperiod_returns([Decimal("0.10"), Decimal("-0.05")]) == pytest.approx(0.045)
