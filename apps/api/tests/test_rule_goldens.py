from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from app.cli.seed_research_baselines import SECURITIES, build_baseline
from app.core.settings import PROJECT_ROOT, Settings
from app.core.time import SHANGHAI
from app.models import UniverseSecurity
from app.repositories.research import ResearchRepository
from app.services.paper import _lot_rule
from app.services.research_evaluation import evaluate_research_card
from app.services.screening import ScreeningService


def _edge_row(symbol: str, exchange: str = "SSE") -> UniverseSecurity:
    return UniverseSecurity(
        symbol=symbol,
        name=f"边界样本{symbol}",
        exchange=exchange,
        industry="测试行业",
        risk_status="NORMAL",
        trading_status="NORMAL",
        history_days=240,
        last_price=10,
        avg_amount_20d_cny=80_000_000,
        turnover_20d_pct=2,
        data_quality="B",
        as_of="2026-08-25T15:00:00+08:00",
        source_ids=["fixture:point-in-time"],
    )


def test_screening_edge_cases_fail_closed(tmp_path: Path) -> None:
    rows = [
        _edge_row("600001").model_copy(update={"history_days": 20}),
        _edge_row("600002").model_copy(update={"data_quality": "C"}),
        _edge_row("600003").model_copy(update={"asset_type": "OTHER"}),
        _edge_row("830001", "BSE"),
    ]
    service = ScreeningService(Settings(screening_dir=tmp_path / "screening"))
    run = service.run(
        rows,
        datetime(2026, 8, 25, 15, 0, tzinfo=SHANGHAI),
        "20260825-150000-screening-edge",
    )
    reasons = {item.symbol: set(item.reason_codes) for item in run.exclusions}
    assert "INSUFFICIENT_HISTORY" in reasons["600001"]
    assert "DATA_QUALITY_BELOW_B" in reasons["600002"]
    assert "UNSUPPORTED_ASSET_TYPE" in reasons["600003"]
    assert [item.symbol for item in run.hard_filter] == ["830001"]


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600519", (100, 100, "SSE_MAIN")),
        ("000001", (100, 100, "SZSE_MAIN")),
        ("300750", (100, 100, "SZSE_CHINEXT")),
        ("688981", (200, 1, "SSE_STAR")),
        ("830001", (100, 1, "BSE")),
    ],
)
def test_lot_rules_cover_a_share_venues(symbol: str, expected: tuple[int, int, str]) -> None:
    assert _lot_rule(symbol) == expected


def test_formal_research_baselines_are_cautious_and_pass_contract(tmp_path: Path) -> None:
    settings = Settings(research_dir=tmp_path / "research", state_dir=tmp_path / "state")
    for security in SECURITIES:
        directory = settings.research_dir / security["symbol"]
        directory.mkdir(parents=True)
        (directory / "fixture.json").write_text(json.dumps(build_baseline(security)), "utf-8")
    cards, warnings = ResearchRepository(settings).contract_baselines()
    assert not warnings
    assert len(cards) >= 10
    evaluated = []
    for card in cards:
        assert card["status"] == "谨慎"
        assert "不进入模拟盘" in card["summary"]
        evaluated.append(evaluate_research_card(card))
    assert all(item.passed and item.score >= 95 for item in evaluated)


def test_golden_manifest_has_30_unique_automated_cases() -> None:
    payload = yaml.safe_load((PROJECT_ROOT / "evals" / "golden-cases.yaml").read_text("utf-8"))
    assert payload["status"] == "AUTOMATED"
    cases = payload["cases"]
    assert len(cases) == 30
    assert len({item["id"] for item in cases}) == 30
    assert {item["category"] for item in cases} == {"SCREENING", "ORDER", "RESEARCH", "DATA"}
