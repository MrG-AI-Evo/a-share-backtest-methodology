import json
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from app.cli.seed_research_baselines import SECURITIES, build_baseline
from app.core.settings import Settings
from app.models import PaperFill, PaperOrder, PaperOrderCreate, RiskCheck, SourceMeta, StockQuote
from app.repositories.paper import PaperRepository
from app.repositories.research import ResearchRepository
from app.services.paper import PaperRuleError, PaperService
from app.services.research_evaluation import qualified_latest_research
from app.services.stock import StockService, StockServiceError


@pytest.fixture(autouse=True)
def _freeze_point_in_time_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the paper/research gate tests deterministic after fixture expiry."""
    frozen = datetime.fromisoformat("2026-08-26T08:00:00+08:00")
    monkeypatch.setattr("app.services.paper.now_shanghai", lambda: frozen)
    monkeypatch.setattr("app.repositories.paper.now_shanghai", lambda: frozen)


class _StaticStockService:
    async def quote(self, symbol: str) -> tuple[StockQuote, SourceMeta]:
        timestamp = "2026-08-25T15:00:00+08:00"
        exchange = "SSE" if symbol.startswith("6") else "SZSE"
        return (
            StockQuote(
                symbol=symbol,
                name=f"行情{symbol}",
                exchange=exchange,
                price=10,
                previous_close=10,
                open=10,
                high=10,
                low=10,
                change=0,
                change_pct=0,
                volume_shares=1_000_000,
                limit_up=11,
                limit_down=9,
                updated_at=timestamp,
            ),
            SourceMeta(
                provider="fixture-public-quote",
                fetched_at=timestamp,
                source_timestamp=timestamp,
                state="live",
            ),
        )


def _static_stock_service() -> StockService:
    return cast(StockService, _StaticStockService())


class _FailingStockService:
    async def quote(self, symbol: str) -> tuple[StockQuote, SourceMeta]:
        raise StockServiceError(f"{symbol} fixture unavailable")


def _failing_stock_service() -> StockService:
    return cast(StockService, _FailingStockService())


def _write_card(settings: Settings, payload: dict[str, object], filename: str) -> Path:
    directory = settings.research_dir / str(payload["symbol"])
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


async def _seed_position(
    repository: PaperRepository,
    symbol: str,
    quantity: int,
    filled_at: str = "2026-08-25T10:00:00+08:00",
) -> None:
    order_id = f"seed-{symbol}"
    order = PaperOrder(
        order_id=order_id,
        symbol=symbol,
        name=f"种子持仓{symbol}",
        side="BUY",
        order_type="LIMIT_SIM",
        limit_price=10,
        quantity=quantity,
        filled_quantity=0,
        status="PENDING_APPROVAL",
        reason="构造组合风险测试状态",
        research_version=f"data/research/{symbol}/seed.json",
        expected_horizon="20_trading_days",
        risk_notes=["测试"],
        invalidation_conditions=["测试"],
        invalidation_price=9,
        ruleset_version="test",
        fee_schedule_version="test",
        risk_checks=[RiskCheck(check="fixture", status="PASS", detail="test")],
        created_at=filled_at,
        earliest_fill_at=filled_at,
    )
    await repository.create_order(order, f"seed-position-{symbol}")
    await repository.update_order_status(order_id, "PENDING_APPROVAL", "QUEUED", "测试批准")
    gross = float(quantity * 10)
    fill = PaperFill(
        fill_id=f"seed-fill-{symbol}",
        order_id=order_id,
        filled_at=filled_at,
        price=10,
        quantity=quantity,
        gross_amount_cny=gross,
        commission_cny=5,
        stamp_duty_cny=0,
        transfer_fee_cny=round(gross * 0.00001, 2),
        slippage_cny=0,
        total_fees_cny=round(5 + gross * 0.00001, 2),
        source="fixture",
        source_timestamp=filled_at,
    )
    await repository.record_fill(order_id, fill)


def _actionable_request(payload: dict[str, object], research_path: str) -> PaperOrderCreate:
    return PaperOrderCreate(
        symbol=str(payload["symbol"]),
        name=str(payload["name"]),
        side="BUY",
        limit_price=10,
        quantity=700,
        reason="验证组合风险硬门",
        research_version=research_path,
        expected_horizon="20_trading_days",
        risk_notes=["仅用于单元测试"],
        invalidation_conditions=["测试失效条件"],
        invalidation_price=9.5,
    )


def test_contract_baseline_is_not_exposed_as_formal_research(tmp_path: Path) -> None:
    settings = Settings(research_dir=tmp_path / "research")
    payload = build_baseline(SECURITIES[0])
    _write_card(settings, payload, "baseline.json")
    repository = ResearchRepository(settings)

    cards, warnings = repository.latest()
    latest, latest_warnings = repository.latest_for(str(payload["symbol"]))
    history, history_warnings = repository.history_for(str(payload["symbol"]))
    baselines, baseline_warnings = repository.contract_baselines()

    assert cards == [] and latest is None and history == []
    assert warnings == latest_warnings == history_warnings == baseline_warnings == []
    assert [item["research_id"] for item in baselines] == [payload["research_id"]]


def test_schema_valid_card_with_broken_evidence_reference_is_quarantined(
    tmp_path: Path,
) -> None:
    settings = Settings(research_dir=tmp_path / "research")
    payload = build_baseline(SECURITIES[0])
    payload.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": "formal-broken-evidence-reference",
            "status": "观察",
        }
    )
    bull_case = cast(list[dict[str, object]], payload["bull_case"])
    bull_case[0]["evidence_ids"] = ["missing-evidence"]
    _write_card(settings, payload, "formal-broken.json")
    repository = ResearchRepository(settings)

    cards, warnings = repository.latest()
    history, history_warnings = repository.history_for(str(payload["symbol"]))

    assert cards == [] and history == []
    assert any("语义校验失败" in warning and "证据引用不存在" in warning for warning in warnings)
    assert any(
        "语义校验失败" in warning and "证据引用不存在" in warning for warning in history_warnings
    )


def test_failed_quality_card_is_not_an_ai_watch_candidate(tmp_path: Path) -> None:
    settings = Settings(research_dir=tmp_path / "research")
    payload = build_baseline(SECURITIES[0])
    bear_case = cast(list[dict[str, object]], payload["bear_case"])
    payload.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": "formal-one-sided-research",
            "status": "观察",
            "bear_case": bear_case[:1],
        }
    )
    _write_card(settings, payload, "formal-one-sided.json")
    repository = ResearchRepository(settings)

    formal, formal_warnings = repository.latest()
    qualified, quality_warnings = qualified_latest_research(repository)

    assert len(formal) == 1 and formal_warnings == []
    assert qualified == []
    assert any(
        "未通过质量门" in warning and "BEAR_CASE_LT_2" in warning for warning in quality_warnings
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "valid_until"),
    [
        ("谨慎", "2026-08-26T09:15:00+08:00"),
        ("观察", "2026-08-25T16:00:00+08:00"),
    ],
)
async def test_cautious_or_expired_research_cannot_open_paper_order(
    tmp_path: Path,
    status: str,
    valid_until: str,
) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "research",
    )
    payload = build_baseline(SECURITIES[0])
    payload.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": f"formal-{status}-{valid_until[:10]}",
            "status": status,
            "valid_until": valid_until,
        }
    )
    _write_card(settings, payload, "formal.json")
    research = ResearchRepository(settings)
    card, warnings = research.latest_for(str(payload["symbol"]))
    assert card is not None and not warnings

    paper = PaperRepository(settings)
    await paper.initialize(1_000_000)
    service = PaperService(settings, paper, research, _static_stock_service())
    request = PaperOrderCreate(
        symbol=str(payload["symbol"]),
        name=str(payload["name"]),
        side="BUY",
        limit_price=10,
        quantity=100,
        reason="验证研究隔离硬门",
        research_version=str(card["_file_path"]),
        expected_horizon="20_trading_days",
        risk_notes=["仅用于单元测试"],
        invalidation_conditions=["测试失效条件"],
        invalidation_price=9,
    )
    with pytest.raises(PaperRuleError, match="重点观察/观察"):
        await service.propose(request, f"research-isolation-{status}-{valid_until}")


@pytest.mark.asyncio
async def test_failed_quality_research_cannot_open_paper_order(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "research",
    )
    payload = build_baseline(SECURITIES[0])
    bear_case = cast(list[dict[str, object]], payload["bear_case"])
    payload.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": "formal-failed-quality-gate",
            "status": "观察",
            "bear_case": bear_case[:1],
        }
    )
    _write_card(settings, payload, "formal-failed-quality.json")
    research = ResearchRepository(settings)
    card, warnings = research.latest_for(str(payload["symbol"]))
    assert card is not None and not warnings

    paper = PaperRepository(settings)
    await paper.initialize(1_000_000)
    service = PaperService(settings, paper, research, _static_stock_service())
    request = PaperOrderCreate(
        symbol=str(payload["symbol"]),
        name=str(payload["name"]),
        side="BUY",
        limit_price=10,
        quantity=100,
        reason="验证研究质量硬门",
        research_version=str(card["_file_path"]),
        expected_horizon="20_trading_days",
        risk_notes=["仅用于单元测试"],
        invalidation_conditions=["测试失效条件"],
        invalidation_price=9,
    )
    with pytest.raises(PaperRuleError, match="研究质量门"):
        await service.propose(request, "research-quality-gate-test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "message"),
    [
        ("approval", "人工批准前研究门复核失败"),
        ("fill", "模拟成交前研究门复核失败"),
    ],
)
async def test_newer_research_blocks_stale_buy_before_approval_or_fill(
    tmp_path: Path,
    stage: str,
    message: str,
) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "research",
    )
    initial = build_baseline(SECURITIES[0])
    initial.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": "formal-actionable-initial",
            "status": "观察",
        }
    )
    _write_card(settings, initial, "formal-initial.json")
    research = ResearchRepository(settings)
    card, warnings = research.latest_for(str(initial["symbol"]))
    assert card is not None and not warnings

    paper = PaperRepository(settings)
    await paper.initialize(1_000_000)
    service = PaperService(settings, paper, research, _static_stock_service())
    request = PaperOrderCreate(
        symbol=str(initial["symbol"]),
        name=str(initial["name"]),
        side="BUY",
        limit_price=10,
        quantity=100,
        reason="验证批准与成交前研究门",
        research_version=str(card["_file_path"]),
        expected_horizon="20_trading_days",
        risk_notes=["仅用于单元测试"],
        invalidation_conditions=["测试失效条件"],
        invalidation_price=9,
    )
    order = await service.propose(request, f"research-recheck-{stage}")
    check_status = {check.check: check.status for check in order.risk_checks}
    assert check_status["gross_exposure_limit"] == "PASS"
    assert check_status["daily_loss_stop"] == "PASS"
    assert check_status["drawdown_freeze"] == "PASS"
    assert check_status["new_positions_per_day"] == "PASS"
    if stage == "fill":
        order = await service.approve(order.order_id, "初始研究仍有效，批准测试订单")

    replacement = build_baseline(SECURITIES[0])
    replacement.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": "formal-newer-cautious",
            "status": "谨慎",
            "as_of": "2026-08-25T15:30:00+08:00",
            "created_at": "2026-08-25T16:30:00+08:00",
            "supersedes_research_id": "formal-actionable-initial",
        }
    )
    _write_card(settings, replacement, "formal-newer.json")

    with pytest.raises(PaperRuleError, match=message):
        if stage == "approval":
            await service.approve(order.order_id, "尝试批准旧研究草案")
        else:
            await service.simulate_fill(order.order_id)


@pytest.mark.asyncio
async def test_gross_exposure_limit_blocks_new_proposal(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "research",
    )
    paper = PaperRepository(settings)
    await paper.initialize(100_000)
    await _seed_position(paper, "600000", 6_500)

    payload = build_baseline(SECURITIES[1])
    payload.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": "formal-gross-limit-proposal",
            "status": "观察",
        }
    )
    _write_card(settings, payload, "formal-gross-limit.json")
    research = ResearchRepository(settings)
    card, warnings = research.latest_for(str(payload["symbol"]))
    assert card is not None and not warnings
    service = PaperService(settings, paper, research, _static_stock_service())

    with pytest.raises(PaperRuleError, match="总仓位"):
        await service.propose(
            _actionable_request(payload, str(card["_file_path"])),
            "gross-exposure-proposal",
        )


@pytest.mark.asyncio
async def test_top5_concentration_limit_blocks_new_proposal(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "research",
    )
    paper = PaperRepository(settings)
    await paper.initialize(100_000)
    for suffix in range(5):
        await _seed_position(
            paper,
            f"60000{suffix}",
            1_000,
            "2026-08-24T10:00:00+08:00",
        )

    payload = build_baseline(SECURITIES[1])
    payload.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": "formal-top5-limit-proposal",
            "status": "观察",
        }
    )
    _write_card(settings, payload, "formal-top5-limit.json")
    research = ResearchRepository(settings)
    card, warnings = research.latest_for(str(payload["symbol"]))
    assert card is not None and not warnings
    service = PaperService(settings, paper, research, _static_stock_service())

    with pytest.raises(PaperRuleError, match="Top 5"):
        await service.propose(
            _actionable_request(payload, str(card["_file_path"])),
            "top5-concentration-proposal",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "message"),
    [
        ("approval", "人工批准前组合风控复核失败"),
        ("fill", "模拟成交前组合风控复核失败"),
    ],
)
async def test_portfolio_change_is_rechecked_before_approval_or_fill(
    tmp_path: Path,
    stage: str,
    message: str,
) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "research",
    )
    paper = PaperRepository(settings)
    await paper.initialize(100_000)
    payload = build_baseline(SECURITIES[1])
    payload.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": f"formal-portfolio-recheck-{stage}",
            "status": "观察",
        }
    )
    _write_card(settings, payload, f"formal-portfolio-{stage}.json")
    research = ResearchRepository(settings)
    card, warnings = research.latest_for(str(payload["symbol"]))
    assert card is not None and not warnings
    service = PaperService(settings, paper, research, _static_stock_service())
    order = await service.propose(
        _actionable_request(payload, str(card["_file_path"])),
        f"portfolio-recheck-{stage}",
    )
    if stage == "fill":
        order = await service.approve(order.order_id, "批准后再改变组合状态")
    await _seed_position(paper, "600000", 6_500)

    with pytest.raises(PaperRuleError, match=message):
        if stage == "approval":
            await service.approve(order.order_id, "尝试在组合变化后批准")
        else:
            await service.simulate_fill(order.order_id)


@pytest.mark.asyncio
async def test_incomplete_position_valuation_fails_closed_for_new_buy(tmp_path: Path) -> None:
    settings = Settings(
        state_dir=tmp_path / "state",
        research_dir=tmp_path / "research",
    )
    paper = PaperRepository(settings)
    await paper.initialize(100_000)
    await _seed_position(paper, "600000", 100)
    payload = build_baseline(SECURITIES[1])
    payload.update(
        {
            "record_type": "FORMAL_RESEARCH",
            "research_id": "formal-valuation-fail-closed",
            "status": "观察",
        }
    )
    _write_card(settings, payload, "formal-valuation-fail-closed.json")
    research = ResearchRepository(settings)
    card, warnings = research.latest_for(str(payload["symbol"]))
    assert card is not None and not warnings
    service = PaperService(settings, paper, research, _failing_stock_service())

    with pytest.raises(PaperRuleError, match="估值刷新失败"):
        await service.propose(
            _actionable_request(payload, str(card["_file_path"])),
            "valuation-fail-closed",
        )
