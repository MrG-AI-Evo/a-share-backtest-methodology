from __future__ import annotations

import re
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.dependencies import get_stock_service
from app.core.time import iso_now
from app.models import ApiEnvelope, ApiMeta, SourceMeta
from app.repositories.research import ResearchRepository
from app.services.research_evaluation import evaluate_research_card
from app.services.stock import StockService, StockServiceError

router = APIRouter(prefix="/stocks", tags=["stocks"])
SYMBOL_PATTERN = re.compile(r"^\d{6}$")


def _symbol(value: str) -> str:
    if not SYMBOL_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail="股票代码必须为 6 位数字")
    return value


def _envelope(data: Any, source: SourceMeta, warnings: list[str] | None = None) -> ApiEnvelope:
    return ApiEnvelope(
        data=data,
        meta=ApiMeta(
            request_id=str(uuid4()),
            generated_at=iso_now(),
            data_state=source.state,
            sources=[source],
            warnings=warnings or [],
        ),
    )


@router.get("/{symbol}/quote", response_model=ApiEnvelope)
async def stock_quote(
    symbol: str,
    service: Annotated[StockService, Depends(get_stock_service)],
) -> ApiEnvelope:
    normalized = _symbol(symbol)
    try:
        quote, source = await service.quote(normalized)
    except StockServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _envelope(quote.model_dump(mode="json"), source)


@router.get("/{symbol}/bars", response_model=ApiEnvelope)
async def stock_bars(
    symbol: str,
    service: Annotated[StockService, Depends(get_stock_service)],
    period: Literal["day", "week", "month", "minute"] = "day",
    adjustment: Literal["qfq", "hfq", "none"] = "qfq",
    limit: Annotated[int, Query(ge=20, le=640)] = 320,
    minute_interval: Annotated[int, Query(ge=1, le=60)] = 5,
) -> ApiEnvelope:
    normalized = _symbol(symbol)
    if period == "minute" and minute_interval not in {1, 5, 15, 30, 60}:
        raise HTTPException(status_code=422, detail="分钟周期只支持 1/5/15/30/60")
    try:
        bars, source, warnings = await service.bars(
            normalized,
            period,
            adjustment,
            limit,
            minute_interval,
        )
    except StockServiceError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _envelope(bars.model_dump(mode="json"), source, warnings)


@router.get("/{symbol}/research/latest", response_model=ApiEnvelope)
async def stock_research(symbol: str, request: Request) -> ApiEnvelope:
    normalized = _symbol(symbol)
    repository = cast(ResearchRepository, request.app.state.research_repository)
    card, warnings = repository.latest_for(normalized)
    if card is not None:
        evaluation = evaluate_research_card(card)
        if not evaluation.passed:
            warnings.append(
                "最新正式研究卡未通过质量门，不得进入 AI 今日关注或模拟买入："
                + ", ".join(evaluation.hard_failures)
            )
    return ApiEnvelope(
        data=card,
        meta=ApiMeta(
            request_id=str(uuid4()),
            generated_at=iso_now(),
            data_state="live" if card else "unavailable",
            warnings=warnings,
        ),
    )


@router.get("/{symbol}/research", response_model=ApiEnvelope)
async def stock_research_history(symbol: str, request: Request) -> ApiEnvelope:
    normalized = _symbol(symbol)
    repository = cast(ResearchRepository, request.app.state.research_repository)
    cards, warnings = repository.history_for(normalized)
    for card in cards:
        evaluation = evaluate_research_card(card)
        if not evaluation.passed:
            warnings.append(
                f"研究卡 {evaluation.research_id} 未通过质量门："
                + ", ".join(evaluation.hard_failures)
            )
    return ApiEnvelope(
        data=cards,
        meta=ApiMeta(
            request_id=str(uuid4()),
            generated_at=iso_now(),
            data_state="live" if cards else "unavailable",
            warnings=warnings,
        ),
    )
