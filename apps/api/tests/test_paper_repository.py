import json
from datetime import date
from pathlib import Path
from typing import Literal

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.core.settings import PROJECT_ROOT, Settings
from app.models import PaperFill, PaperOrder, RiskCheck
from app.repositories.paper import PaperLedgerError, PaperRepository


def _order(
    order_id: str,
    side: Literal["BUY", "SELL"],
    created_at: str,
) -> PaperOrder:
    return PaperOrder(
        order_id=order_id,
        symbol="300476",
        name="胜宏科技",
        side=side,
        order_type="LIMIT_SIM",
        limit_price=100,
        quantity=100,
        filled_quantity=0,
        status="PENDING_APPROVAL",
        reason="验证模拟账本",
        research_version="data/research/300476/verified.json" if side == "BUY" else None,
        expected_horizon="20_trading_days",
        risk_notes=["测试风险"],
        invalidation_conditions=["跌破失效价"],
        invalidation_price=90 if side == "BUY" else None,
        ruleset_version="test-rules",
        fee_schedule_version="test-fees",
        risk_checks=[RiskCheck(check="fixture", status="PASS", detail="test")],
        created_at=created_at,
        earliest_fill_at=created_at,
    )


def _fill(
    order_id: str,
    fill_id: str,
    filled_at: str,
    side: Literal["BUY", "SELL"],
) -> PaperFill:
    gross = 10_000.0
    fees = 5.1 if side == "BUY" else 10.1
    return PaperFill(
        fill_id=fill_id,
        order_id=order_id,
        filled_at=filled_at,
        price=100,
        quantity=100,
        gross_amount_cny=gross,
        commission_cny=5,
        stamp_duty_cny=5 if side == "SELL" else 0,
        transfer_fee_cny=0.1,
        slippage_cny=0,
        total_fees_cny=fees,
        source="fixture",
        source_timestamp=filled_at,
    )


@pytest.mark.asyncio
async def test_paper_ledger_is_idempotent_t_plus_one_and_hash_chained(tmp_path: Path) -> None:
    settings = Settings(state_dir=tmp_path)
    repository = PaperRepository(settings)
    await repository.initialize(100_000)

    buy = _order("buy-0001", "BUY", "2026-08-25T10:00:00+08:00")
    created = await repository.create_order(buy, "idempotency-buy-1")
    order_schema = json.loads(
        (PROJECT_ROOT / "schemas" / "paper-order.schema.json").read_text("utf-8")
    )
    Draft202012Validator(order_schema, format_checker=FormatChecker()).validate(
        created.model_dump(mode="json")
    )
    duplicate = await repository.create_order(buy, "idempotency-buy-1")
    assert duplicate.order_id == created.order_id
    conflicting = buy.model_copy(update={"quantity": 200, "order_id": "buy-conflict"})
    with pytest.raises(PaperLedgerError, match="幂等键"):
        await repository.create_order(conflicting, "idempotency-buy-1")

    await repository.update_order_status("buy-0001", "PENDING_APPROVAL", "QUEUED", "人工确认")
    await repository.record_fill(
        "buy-0001",
        _fill("buy-0001", "fill-buy-1", "2026-08-25T10:01:00+08:00", "BUY"),
    )
    same_day = await repository.positions(as_of=date(2026, 8, 25))
    next_day = await repository.positions(as_of=date(2026, 8, 26))
    assert same_day[0].sellable_quantity == 0
    assert next_day[0].sellable_quantity == 100

    sell_same_day = _order("sell-0001", "SELL", "2026-08-25T14:00:00+08:00")
    await repository.create_order(sell_same_day, "idempotency-sell-1")
    await repository.update_order_status("sell-0001", "PENDING_APPROVAL", "QUEUED", "人工确认")
    with pytest.raises(PaperLedgerError, match=r"T\+1"):
        await repository.record_fill(
            "sell-0001",
            _fill("sell-0001", "fill-sell-1", "2026-08-25T14:01:00+08:00", "SELL"),
        )

    await repository.record_fill(
        "sell-0001",
        _fill("sell-0001", "fill-sell-2", "2026-08-26T10:01:00+08:00", "SELL"),
    )
    assert (await repository.summary()).cash == pytest.approx(99_984.8)
    assert await repository.positions(as_of=date(2026, 8, 27)) == []
    assert (await repository.reconcile())["reconciled"] is True

    events = list(reversed(await repository.audit_events()))
    assert len(events) == 6
    for previous, current in zip(events, events[1:], strict=False):
        assert current.prev_event_hash == previous.event_hash
