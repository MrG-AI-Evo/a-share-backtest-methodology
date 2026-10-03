from dataclasses import replace
from decimal import Decimal
from typing import Any

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
    date: str,
    *securities: SecurityDay,
    month_start: bool = False,
    actions: tuple[CorporateActionDay, ...] = (),
) -> TradingDayInput:
    return TradingDayInput(
        trade_date=date,
        is_first_trading_day_of_month=month_start,
        securities=tuple(securities),
        fee_rule=FEE,
        buy_slippage_bps=Decimal("0"),
        sell_slippage_bps=Decimal("0"),
        corporate_actions=actions,
    )


def test_dh001_dh005_entry_is_next_open_and_no_duplicate_holding() -> None:
    runner = DeterministicHistoryRunner("20260827-runner-entry-001", "policy-v2-test")
    result = runner.run(
        [
            day("2020-01-02", security(), month_start=True),
            day("2020-01-03", security()),
            day("2020-02-03", security(), month_start=True),
            day("2020-02-04", security()),
        ]
    )
    assert result.ledger.positions["601288"].quantity == 1000
    assert sum(event["event_type"] == "BUY_FILL" for event in result.ledger.events) == 1
    assert result.ledger.audit_chain_ok()


def test_dh007_dh008_open_above_limit_rejects_without_same_month_replacement() -> None:
    first = security("601288", median_amount_20d_cny=Decimal("200000000"))
    second = security("600900", median_amount_20d_cny=Decimal("100000000"))
    expensive = replace(first, open_price=Decimal("10.01"))
    runner = DeterministicHistoryRunner("20260827-runner-limit-001", "policy-v2-test")
    result = runner.run(
        [
            day("2020-01-02", first, second, month_start=True),
            day("2020-01-03", expensive, second),
        ]
    )
    rejected = [e for e in result.ledger.events if e["event_type"] == "ORDER_REJECTED"]
    assert any(e["payload"]["reason"] == "OPEN_PLUS_SLIPPAGE_ABOVE_LIMIT" for e in rejected)  # type: ignore[index]
    assert "601288" not in result.ledger.positions
    assert result.ledger.positions["600900"].quantity == 1000


def test_dh002_dh004_exit_waits_until_tradable_and_keeps_position() -> None:
    eligible = security()
    below = replace(eligible, close_price=Decimal("10.0001"))
    suspended = replace(below, open_price=None, tradable_open=False)
    runner = DeterministicHistoryRunner("20260827-runner-exit-001", "policy-v2-test")
    result = runner.run(
        [
            day("2020-01-02", eligible, month_start=True),
            day("2020-01-03", eligible),
            day("2020-01-06", below),
            day("2020-01-07", suspended),
            day("2020-01-08", below),
        ]
    )
    assert result.ledger.positions["601288"].quantity == 0
    assert any(event["event_type"] == "ORDER_QUEUED" for event in result.ledger.events)
    assert sum(event["event_type"] == "EXIT_SIGNAL" for event in result.ledger.events) == 1


def test_dh018_dh027_action_precedes_sell_and_adjusted_quantity_is_sold() -> None:
    eligible = security()
    below = replace(eligible, close_price=Decimal("10.0001"))
    split = CorporateActionDay(
        symbol="601288",
        action_type="SHARE_MULTIPLIER",
        known_at="2020-01-07T09:00:00+08:00",
        share_multiplier=Decimal("1.5"),
    )
    runner = DeterministicHistoryRunner("20260827-runner-action-001", "policy-v2-test")
    result = runner.run(
        [
            day("2020-01-02", eligible, month_start=True),
            day("2020-01-03", eligible),
            day("2020-01-06", below),
            day("2020-01-07", below, actions=(split,)),
        ]
    )
    sell = next(event for event in result.ledger.events if event["event_type"] == "SELL_FILL")
    assert sell["payload"]["quantity"] == 1500  # type: ignore[index]
    action_index = next(
        i
        for i, event in enumerate(result.ledger.events)
        if event["event_type"] == "CORPORATE_ACTION"
    )
    sell_index = result.ledger.events.index(sell)
    assert action_index < sell_index


def test_dh003_twenty_slots_reject_twenty_first_candidate() -> None:
    candidates = tuple(
        security(f"{600000 + index:06d}", median_amount_20d_cny=Decimal(100000000 - index))
        for index in range(21)
    )
    runner = DeterministicHistoryRunner("20260827-runner-slots-001", "policy-v2-test")
    result = runner.run(
        [
            day("2020-01-02", *candidates, month_start=True),
            day("2020-01-03", *candidates),
        ]
    )
    assert sum(position.quantity > 0 for position in result.ledger.positions.values()) == 20
    assert candidates[-1].symbol not in result.ledger.positions


def test_dh014_dh015_dividend_moves_receivable_to_cash_without_equity_jump() -> None:
    eligible = security()
    receivable = CorporateActionDay(
        symbol="601288",
        action_type="DIVIDEND_RECEIVABLE",
        known_at="2020-06-10T09:00:00+08:00",
        cash_per_share=Decimal("0.20"),
    )
    payment = CorporateActionDay(
        symbol="601288",
        action_type="DIVIDEND_PAYMENT",
        known_at="2020-06-20T09:00:00+08:00",
    )
    runner = DeterministicHistoryRunner("20260827-runner-dividend-001", "policy-v2-test")
    result = runner.run(
        [
            day("2020-01-02", eligible, month_start=True),
            day("2020-01-03", eligible),
            day("2020-06-10", eligible, actions=(receivable,)),
            day("2020-06-20", eligible, actions=(payment,)),
        ]
    )
    before = result.checkpoints[-2]["account_summary"]
    after = result.checkpoints[-1]["account_summary"]
    assert before["dividend_receivable_cny"] == 200.0  # type: ignore[index]
    assert after["dividend_receivable_cny"] == 0.0  # type: ignore[index]
    assert before["equity_cny"] == after["equity_cny"]  # type: ignore[index]
