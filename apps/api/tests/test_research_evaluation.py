import json
from copy import deepcopy
from typing import Any, cast

from app.core.settings import PROJECT_ROOT
from app.services.research_evaluation import evaluate_research_card


def _passing_card() -> dict[str, Any]:
    card = cast(
        dict[str, Any],
        json.loads((PROJECT_ROOT / "templates" / "research-card.example.json").read_text()),
    )
    card["bull_case"].append(
        {"text": "第二条多头论点", "evidence_ids": ["ev-001"], "confidence": 0.6}
    )
    card["bear_case"].append(
        {"text": "第二条空头论点", "evidence_ids": ["ev-002"], "confidence": 0.6}
    )
    card["evidence"][0]["source_id"] = "CNINFO:FILING"
    card["evidence"][1]["source_id"] = "TENCENT:MARKET"
    return card


def test_research_evaluator_passes_balanced_supported_card() -> None:
    result = evaluate_research_card(_passing_card())
    assert result.passed is True
    assert result.metrics["strong_claim_coverage_pct"] == 100


def test_research_evaluator_rejects_future_and_one_sided_card() -> None:
    card = deepcopy(_passing_card())
    card["bear_case"] = []
    card["evidence"][0]["as_of"] = "2027-01-01T00:00:00+08:00"
    result = evaluate_research_card(card)
    assert result.passed is False
    assert "BEAR_CASE_LT_2" in result.hard_failures
    assert "FUTURE_EVIDENCE" in result.hard_failures
