from __future__ import annotations

from typing import cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from app.adapters.public_data import AkshareSupplementAdapter, MootdxStockAdapter
from app.adapters.research import research_adapter_health
from app.core.settings import get_settings
from app.core.time import iso_now
from app.models import ApiEnvelope, ApiMeta, DataState, PipelineStatus
from app.repositories.research import ResearchRepository
from app.repositories.screening import ScreeningRepository
from app.services.research_evaluation import evaluate_research_card

router = APIRouter(prefix="/pipeline", tags=["research-pipeline"])


def _envelope(
    data: object,
    state: DataState = "live",
    warnings: list[str] | None = None,
) -> ApiEnvelope:
    return ApiEnvelope(
        data=data,
        meta=ApiMeta(
            request_id=str(uuid4()),
            generated_at=iso_now(),
            data_state=state,
            warnings=warnings or [],
        ),
    )


@router.get("", response_model=ApiEnvelope)
async def pipeline_status(request: Request) -> ApiEnvelope:
    settings = get_settings()
    screening = cast(ScreeningRepository, request.app.state.screening_repository).latest()
    research = cast(ResearchRepository, request.app.state.research_repository)
    evaluations = []
    warnings: list[str] = []
    cards, import_warnings = research.latest()
    warnings.extend(import_warnings)
    for summary in cards:
        card, card_warnings = research.latest_for(summary.symbol)
        warnings.extend(card_warnings)
        if card:
            evaluations.append(evaluate_research_card(card))
    adapters = [
        MootdxStockAdapter(enabled=settings.enable_mootdx).health(),
        AkshareSupplementAdapter(enabled=settings.enable_akshare).health(),
        *research_adapter_health(),
    ]
    payload = PipelineStatus(
        latest_screening=screening,
        research_evaluations=evaluations,
        adapter_health=adapters,
    )
    state: DataState = "live" if screening is not None else "unavailable"
    if screening is None:
        warnings.append("尚无正式 300→30 筛选产物")
    return _envelope(payload.model_dump(mode="json"), state, warnings)


@router.get("/screening/latest", response_model=ApiEnvelope)
async def latest_screening(request: Request) -> ApiEnvelope:
    run = cast(ScreeningRepository, request.app.state.screening_repository).latest()
    return _envelope(
        run.model_dump(mode="json") if run else None,
        "live" if run else "unavailable",
        [] if run else ["尚无正式筛选产物；请先运行 screening:run"],
    )


@router.get("/research/{symbol}/evaluation", response_model=ApiEnvelope)
async def research_evaluation(symbol: str, request: Request) -> ApiEnvelope:
    if len(symbol) != 6 or not symbol.isdigit():
        raise HTTPException(status_code=422, detail="股票代码必须为 6 位数字")
    research = cast(ResearchRepository, request.app.state.research_repository)
    card, warnings = research.latest_for(symbol)
    if card is None:
        return _envelope(None, "unavailable", warnings or ["没有可评测的正式研究卡"])
    evaluation = evaluate_research_card(card)
    return _envelope(evaluation.model_dump(mode="json"), "live", warnings)
