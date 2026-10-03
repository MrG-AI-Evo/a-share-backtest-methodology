from datetime import timedelta
from pathlib import Path

import pytest

from app.core.settings import Settings
from app.core.time import now_shanghai
from app.models import CorporateAction, PaperFill, PaperOrder, RiskCheck
from app.repositories.paper import PaperRepository


def _order() -> PaperOrder:
    return PaperOrder(
        order_id="advanced-buy-1",
        symbol="600000",
        name="浦发银行",
        side="BUY",
        order_type="LIMIT_SIM",
        limit_price=10,
        quantity=100,
        filled_quantity=0,
        status="PENDING_APPROVAL",
        reason="高级账本黄金场景",
        research_version="data/research/600000/test.json",
        expected_horizon="20_trading_days",
        risk_notes=["测试"],
        invalidation_conditions=["测试失效"],
        invalidation_price=9,
        ruleset_version="test",
        fee_schedule_version="test",
        risk_checks=[RiskCheck(check="fixture", status="PASS", detail="test")],
        created_at="2026-08-24T15:00:00+08:00",
        earliest_fill_at="2026-08-25T09:30:00+08:00",
    )


def _fill(identifier: str, quantity: int, timestamp: str) -> PaperFill:
    gross = float(10 * quantity)
    return PaperFill(
        fill_id=identifier,
        order_id="advanced-buy-1",
        filled_at=timestamp,
        price=10,
        quantity=quantity,
        gross_amount_cny=gross,
        commission_cny=1,
        stamp_duty_cny=0,
        transfer_fee_cny=0,
        slippage_cny=0,
        total_fees_cny=1,
        source="fixture",
        source_timestamp=timestamp,
    )


@pytest.mark.asyncio
async def test_partial_fills_and_corporate_actions_keep_ledger_closed(tmp_path: Path) -> None:
    repository = PaperRepository(Settings(state_dir=tmp_path))
    await repository.initialize(100_000)
    await repository.create_order(_order(), "advanced-idempotency-1")
    await repository.update_order_status("advanced-buy-1", "PENDING_APPROVAL", "QUEUED", "人工确认")
    partial = await repository.record_fill(
        "advanced-buy-1", _fill("advanced-fill-1", 40, "2026-08-25T10:00:00+08:00")
    )
    assert partial.status == "PARTIAL"
    assert partial.filled_quantity == 40
    completed = await repository.record_fill(
        "advanced-buy-1", _fill("advanced-fill-2", 60, "2026-08-25T10:05:00+08:00")
    )
    assert completed.status == "FILLED"
    assert completed.filled_quantity == 100

    dividend = CorporateAction(
        action_id="dividend-0001",
        symbol="600000",
        action_type="CASH_DIVIDEND",
        announced_at="2026-08-24T18:00:00+08:00",
        effective_date="2026-08-26",
        cash_per_share_cny=0.5,
        price_factor=0.95,
        source_id="SSE",
        source_uri="https://www.sse.com.cn/fixture/dividend",
        retrieved_at="2026-08-24T18:10:00+08:00",
        quality="A",
    )
    split = CorporateAction(
        action_id="split-0000001",
        symbol="600000",
        action_type="SHARE_MULTIPLIER",
        announced_at="2026-08-24T18:00:00+08:00",
        effective_date="2026-08-27",
        share_multiplier=1.2,
        price_factor=1 / 1.2,
        source_id="SSE",
        source_uri="https://www.sse.com.cn/fixture/split",
        retrieved_at="2026-08-24T18:10:00+08:00",
        quality="A",
    )
    await repository.apply_corporate_action(dividend)
    await repository.apply_corporate_action(split)
    positions = await repository.positions()
    assert positions[0].quantity == 120
    assert (await repository.reconcile())["reconciled"] is True


@pytest.mark.asyncio
async def test_mark_to_market_updates_positions_nav_and_daily_pnl(tmp_path: Path) -> None:
    repository = PaperRepository(Settings(state_dir=tmp_path))
    await repository.initialize(100_000)
    fill_at = (now_shanghai() + timedelta(minutes=1)).isoformat(timespec="seconds")
    mark_at = (now_shanghai() + timedelta(minutes=2)).isoformat(timespec="seconds")
    await repository.create_order(_order(), "mark-to-market-order")
    await repository.update_order_status("advanced-buy-1", "PENDING_APPROVAL", "QUEUED", "人工确认")
    await repository.record_fill(
        "advanced-buy-1",
        _fill("mark-to-market-fill", 100, fill_at),
    )

    summary = await repository.mark_to_market(
        {"600000": (12.0, mark_at)},
        mark_at,
    )
    position = (await repository.positions())[0]
    nav = (await repository.nav())[-1]

    assert position.last_price == 12
    assert position.updated_at == mark_at
    assert summary.market_value == pytest.approx(1_200)
    assert summary.total_assets == pytest.approx(100_199)
    assert summary.daily_pnl == pytest.approx(199)
    assert nav.total_assets == pytest.approx(summary.total_assets)
    assert await repository.new_positions_opened_on(now_shanghai().date()) == 1
