from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.time import iso_now
from app.models import ResearchCardSummary, ResearchEvaluation
from app.repositories.research import ResearchRepository

EVALUATOR_VERSION = "research-eval-2026-08-25-v1"


def evaluate_research_card(card: dict[str, Any]) -> ResearchEvaluation:
    hard_failures: list[str] = []
    warnings: list[str] = []

    def items(key: str) -> list[Any]:
        value = card.get(key)
        return value if isinstance(value, list) else []

    evidence = items("evidence")
    evidence_by_id = {
        str(item.get("evidence_id")): item for item in evidence if isinstance(item, dict)
    }

    def claim_refs(key: str) -> list[str]:
        value = card.get(key)
        if not isinstance(value, list):
            return []
        return [
            str(item)
            for claim in value
            if isinstance(claim, dict)
            for item in claim.get("evidence_ids", [])
        ]

    bull_claims = items("bull_case")
    bear_claims = items("bear_case")
    core_claims = items("core_thesis")
    bull_count = len(bull_claims)
    bear_count = len(bear_claims)
    if bull_count < 2:
        hard_failures.append("BULL_CASE_LT_2")
    if bear_count < 2:
        hard_failures.append("BEAR_CASE_LT_2")
    all_refs = claim_refs("core_thesis") + claim_refs("bull_case") + claim_refs("bear_case")
    unknown_refs = sorted(set(all_refs) - set(evidence_by_id))
    if unknown_refs:
        hard_failures.append("UNKNOWN_EVIDENCE_REFERENCE")

    fact_refs = [
        str(item)
        for fact in items("confirmed_facts")
        if isinstance(fact, dict)
        for item in fact.get("evidence_ids", [])
    ]
    fact_count = len(items("confirmed_facts"))
    if fact_count and len(fact_refs) < fact_count:
        hard_failures.append("UNSUPPORTED_CONFIRMED_FACT")
    source_domains = {
        str(item.get("source_id", "")).split(":", 1)[0]
        for item in evidence
        if isinstance(item, dict) and item.get("quality") in {"A", "B", "C"}
    }
    source_domains.discard("")
    if len(source_domains) < 2:
        hard_failures.append("EVIDENCE_DOMAINS_LT_2")
    if not card.get("invalidation_conditions"):
        hard_failures.append("MISSING_INVALIDATION")
    if not card.get("risk_flags"):
        hard_failures.append("MISSING_RISK")

    as_of_raw = card.get("as_of")
    future_evidence = 0
    try:
        as_of = datetime.fromisoformat(str(as_of_raw))
        for item in evidence:
            if not isinstance(item, dict):
                continue
            evidence_as_of = datetime.fromisoformat(str(item["as_of"]))
            if evidence_as_of > as_of:
                future_evidence += 1
    except (KeyError, TypeError, ValueError):
        hard_failures.append("INVALID_TIME")
    if future_evidence:
        hard_failures.append("FUTURE_EVIDENCE")

    strong_claim_items = [
        claim for claim in [*core_claims, *bull_claims, *bear_claims] if isinstance(claim, dict)
    ]
    strong_claims = len(strong_claim_items)
    supported_claims = sum(
        1
        for claim in strong_claim_items
        if claim.get("evidence_ids")
        and all(str(item) in evidence_by_id for item in claim["evidence_ids"])
    )
    claim_coverage_pct = (
        min(100.0, supported_claims / strong_claims * 100) if strong_claims else 0.0
    )
    if claim_coverage_pct < 95:
        hard_failures.append("STRONG_CLAIM_COVERAGE_LT_95")
    inference_items = items("ai_inferences")
    inference_count = len(inference_items)
    countered_inferences = sum(
        1 for item in inference_items if isinstance(item, dict) and item.get("counter_evidence_ids")
    )
    if inference_count and countered_inferences == 0:
        warnings.append("所有 AI 推断均未记录反证")

    score = 100.0
    score -= 15 * len(set(hard_failures))
    score -= 3 * len(warnings)
    metrics: dict[str, float | int | bool] = {
        "bull_case_count": bull_count,
        "bear_case_count": bear_count,
        "evidence_count": len(evidence),
        "evidence_domain_count": len(source_domains),
        "strong_claim_coverage_pct": round(claim_coverage_pct, 2),
        "future_evidence_count": future_evidence,
        "has_invalidation": bool(card.get("invalidation_conditions")),
        "has_risk": bool(card.get("risk_flags")),
    }
    return ResearchEvaluation(
        research_id=str(card.get("research_id", "UNKNOWN")),
        evaluated_at=iso_now(),
        evaluator_version=EVALUATOR_VERSION,
        passed=not hard_failures,
        score=max(0.0, round(score, 2)),
        hard_failures=sorted(set(hard_failures)),
        warnings=warnings,
        metrics=metrics,
    )


def qualified_latest_research(
    repository: ResearchRepository,
) -> tuple[list[ResearchCardSummary], list[str]]:
    summaries, warnings = repository.latest()
    qualified: list[ResearchCardSummary] = []
    for summary in summaries:
        card, card_warnings = repository.latest_for(summary.symbol)
        warnings.extend(card_warnings)
        if card is None:
            continue
        evaluation = evaluate_research_card(card)
        if evaluation.passed:
            qualified.append(summary)
        else:
            warnings.append(
                f"研究卡 {evaluation.research_id} 未通过质量门，未进入 AI 今日关注"
                f"（{', '.join(evaluation.hard_failures)}）"
            )
    return qualified, list(dict.fromkeys(warnings))
