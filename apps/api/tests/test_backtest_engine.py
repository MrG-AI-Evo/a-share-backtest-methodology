import json
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.backtests.engine import DeterministicLedger, SignalInput, evaluate_signal
from app.core.settings import PROJECT_ROOT


def _signal(**updates: object) -> SignalInput:
    values: dict[str, object] = {
        "signal_at": "2020-01-02T15:00:00+08:00",
        "known_at": "2020-01-02T14:00:00+08:00",
        "d_ttm": Decimal("0.48"),
        "d_med3": Decimal("0.50"),
        "raw_close": Decimal("10.00"),
        "cd_rate": Decimal("0.018"),
        "cd_tenor_years": 5,
        "quality_pass": True,
        "cd_rate_quality": "OBSERVED",
    }
    values.update(updates)
    return SignalInput(**values)  # type: ignore[arg-type]


def test_signal_equality_is_entry_and_hold_but_strictly_below_exits() -> None:
    equality = evaluate_signal(_signal(), currently_held=False)
    assert equality.action == "ENTRY_ELIGIBLE"
    assert equality.snapshot["dividend_yield"] == pytest.approx(0.048)
    assert evaluate_signal(_signal(), currently_held=True).action == "HOLD"
    below = evaluate_signal(_signal(raw_close=Decimal("10.0001")), currently_held=True)
    assert below.action == "EXIT"


def test_signal_fails_closed_for_missing_future_or_synthetic_inputs() -> None:
    assert evaluate_signal(_signal(cd_rate=None), currently_held=False).action == "DATA_GAP"
    assert (
        evaluate_signal(_signal(known_at="2020-01-03T09:00:00+08:00"), currently_held=False).action
        == "REJECT"
    )
    assert (
        evaluate_signal(_signal(cd_rate_quality="SYNTHETIC_CARRY"), currently_held=False).action
        == "REJECT"
    )


def test_ledger_exact_contribution_corporate_action_dividend_and_reconciliation() -> None:
    ledger = DeterministicLedger("20260827-100422-mechanism-ledger")
    ledger.buy(
        symbol="601288",
        name="农业银行",
        exchange="SSE",
        price=Decimal("10"),
        quantity=1000,
        transaction_costs=Decimal("10"),
        occurred_at="2020-01-03T09:30:00+08:00",
        known_at="2020-01-03T09:30:00+08:00",
    )
    assert ledger.contributions == Decimal("10010.00")
    assert ledger.cash == Decimal("0.00")
    with pytest.raises(ValueError, match="不得重复买入"):
        ledger.buy(
            symbol="601288",
            name="农业银行",
            exchange="SSE",
            price=Decimal("9"),
            quantity=1000,
            transaction_costs=Decimal("5"),
            occurred_at="2020-02-03T09:30:00+08:00",
            known_at="2020-02-03T09:30:00+08:00",
        )
    ledger.apply_share_multiplier(
        symbol="601288",
        multiplier=Decimal("1.5"),
        occurred_at="2020-06-01T09:00:00+08:00",
        known_at="2020-06-01T09:00:00+08:00",
    )
    assert ledger.positions["601288"].quantity == 1500
    ledger.declare_dividend(
        symbol="601288",
        cash_per_share=Decimal("0.20"),
        occurred_at="2020-06-10T09:00:00+08:00",
        known_at="2020-06-10T09:00:00+08:00",
    )
    ledger.pay_dividend(
        symbol="601288",
        occurred_at="2020-06-20T09:00:00+08:00",
        known_at="2020-06-20T09:00:00+08:00",
    )
    assert ledger.cash == Decimal("300.00")
    ledger.mark(
        "601288",
        Decimal("8"),
        "2020-12-31T15:00:00+08:00",
        "2020-12-31T15:00:00+08:00",
    )
    snapshot = ledger.snapshot(
        as_of="2020-12-31T15:00:00+08:00",
        policy_version="dividend-hurdle-core20-2026-08-27-draft-v2",
    )
    assert snapshot["reconciliation"]["status"] == "PASS"  # type: ignore[index]
    schema = json.loads(
        (PROJECT_ROOT / "schemas" / "backtest-ledger-snapshot.schema.json").read_text("utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(snapshot)


def test_ledger_enforces_exact_target_quantity_and_time_consistency() -> None:
    ledger = DeterministicLedger("20260827-100422-mechanism-boundary")
    with pytest.raises(ValueError, match="1000"):
        ledger.buy(
            symbol="601288",
            name="农业银行",
            exchange="SSE",
            price=Decimal("10"),
            quantity=900,
            transaction_costs=Decimal("5"),
            occurred_at="2020-01-03T09:30:00+08:00",
            known_at="2020-01-03T09:30:00+08:00",
        )
    with pytest.raises(ValueError, match="known_at"):
        ledger.buy(
            symbol="601288",
            name="农业银行",
            exchange="SSE",
            price=Decimal("10"),
            quantity=1000,
            transaction_costs=Decimal("5"),
            occurred_at="2020-01-03T09:30:00+08:00",
            known_at="2020-01-04T09:30:00+08:00",
        )
