from __future__ import annotations

import re
from typing import Annotated, Any, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from app.adapters.public_data import AkshareSupplementAdapter, MootdxStockAdapter
from app.adapters.research import research_adapter_health
from app.api.dependencies import get_paper_service
from app.core.policy import load_policy, load_ruleset_meta
from app.core.settings import get_settings
from app.core.time import iso_now
from app.models import (
    ApiEnvelope,
    ApiMeta,
    DataState,
    OrderAction,
    PaperOrderCreate,
    SourceMeta,
    SystemStatus,
    WatchlistCreate,
)
from app.repositories.paper import PaperLedgerError, PaperRepository
from app.repositories.research import ResearchRepository
from app.repositories.screening import ScreeningRepository
from app.services.market import MarketService
from app.services.paper import PaperRuleError, PaperService
from app.services.research_evaluation import evaluate_research_card

router = APIRouter(tags=["paper-and-local-state"])
SYMBOL_PATTERN = re.compile(r"^\d{6}$")


def _envelope(
    data: Any,
    warnings: list[str] | None = None,
    sources: list[SourceMeta] | None = None,
    data_state: DataState = "live",
) -> ApiEnvelope:
    return ApiEnvelope(
        data=data,
        meta=ApiMeta(
            request_id=str(uuid4()),
            generated_at=iso_now(),
            data_state=data_state,
            warnings=warnings or [],
            sources=sources or [],
        ),
    )


def _verify_local_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin not in get_settings().web_origins:
        raise HTTPException(status_code=403, detail="本地写操作需要受信任的 Web Origin")


def _paper_repository(request: Request) -> PaperRepository:
    return cast(PaperRepository, request.app.state.paper_repository)


@router.get("/paper", response_model=ApiEnvelope)
async def paper_account(
    service: Annotated[PaperService, Depends(get_paper_service)],
) -> ApiEnvelope:
    detail, warnings, sources = await service.detail()
    data_state: DataState = (
        "stale"
        if warnings
        else ("delayed" if any(source.state == "delayed" for source in sources) else "live")
    )
    return _envelope(
        detail.model_dump(mode="json"),
        warnings,
        sources,
        data_state,
    )


@router.post("/paper/orders", response_model=ApiEnvelope, status_code=status.HTTP_201_CREATED)
async def propose_order(
    payload: PaperOrderCreate,
    request: Request,
    service: Annotated[PaperService, Depends(get_paper_service)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=12)],
) -> ApiEnvelope:
    _verify_local_origin(request)
    try:
        order = await service.propose(payload, idempotency_key)
    except PaperRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PaperLedgerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _envelope(order.model_dump(mode="json"))


@router.post("/paper/orders/{order_id}/approve", response_model=ApiEnvelope)
async def approve_order(
    order_id: str,
    payload: OrderAction,
    request: Request,
    service: Annotated[PaperService, Depends(get_paper_service)],
) -> ApiEnvelope:
    _verify_local_origin(request)
    try:
        order = await service.approve(order_id, payload.reason)
    except PaperRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PaperLedgerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _envelope(order.model_dump(mode="json"))


@router.post("/paper/orders/{order_id}/reject", response_model=ApiEnvelope)
async def reject_order(
    order_id: str,
    payload: OrderAction,
    request: Request,
    service: Annotated[PaperService, Depends(get_paper_service)],
) -> ApiEnvelope:
    _verify_local_origin(request)
    try:
        order = await service.reject(order_id, payload.reason)
    except PaperLedgerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _envelope(order.model_dump(mode="json"))


@router.post("/paper/orders/{order_id}/simulate-fill", response_model=ApiEnvelope)
async def simulate_fill(
    order_id: str,
    request: Request,
    service: Annotated[PaperService, Depends(get_paper_service)],
) -> ApiEnvelope:
    _verify_local_origin(request)
    try:
        order = await service.simulate_fill(order_id)
    except PaperRuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _envelope(order.model_dump(mode="json"))


@router.get("/watchlist", response_model=ApiEnvelope)
async def watchlist(request: Request) -> ApiEnvelope:
    items = await _paper_repository(request).watchlist()
    return _envelope([item.model_dump(mode="json") for item in items])


@router.post("/watchlist", response_model=ApiEnvelope, status_code=status.HTTP_201_CREATED)
async def save_watchlist(payload: WatchlistCreate, request: Request) -> ApiEnvelope:
    _verify_local_origin(request)
    item = await _paper_repository(request).upsert_watchlist(payload)
    return _envelope(item.model_dump(mode="json"))


@router.delete("/watchlist/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watchlist(symbol: str, request: Request) -> Response:
    _verify_local_origin(request)
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise HTTPException(status_code=422, detail="股票代码必须为 6 位数字")
    try:
        await _paper_repository(request).remove_watchlist(symbol)
    except PaperLedgerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/decisions", response_model=ApiEnvelope)
async def decisions(request: Request) -> ApiEnvelope:
    repository = _paper_repository(request)
    return _envelope(
        {
            "orders": [order.model_dump(mode="json") for order in await repository.orders()],
            "audit_events": [
                event.model_dump(mode="json") for event in await repository.audit_events()
            ],
        }
    )


@router.get("/system", response_model=ApiEnvelope)
async def system_status(request: Request) -> ApiEnvelope:
    settings = get_settings()
    policy = load_policy(settings)
    ruleset_version, rules_status = load_ruleset_meta(settings)
    research = cast(ResearchRepository, request.app.state.research_repository)
    cards, warnings = research.latest()
    market = cast(MarketService, request.app.state.market_service)
    sources, market_warnings = market.health_snapshot()
    screening = cast(ScreeningRepository, request.app.state.screening_repository).latest()
    reconciliation = await _paper_repository(request).reconcile()
    evaluations = []
    for summary in cards:
        card, _ = research.latest_for(summary.symbol)
        if card:
            evaluations.append(evaluate_research_card(card))
    payload = SystemStatus(
        api_version=settings.app_version,
        environment=settings.environment,
        ruleset_version=ruleset_version,
        rules_status=rules_status,
        policy_version=policy.version,
        fee_schedule_version=policy.fees.version,
        research_card_count=len(cards),
        research_import_warnings=warnings,
        paper_database_bytes=(
            settings.paper_database.stat().st_size if settings.paper_database.exists() else 0
        ),
        analytics_database_bytes=(
            settings.analytics_database.stat().st_size
            if settings.analytics_database.exists()
            else 0
        ),
        data_sources=sources,
        market_warnings=market_warnings,
        adapter_health=[
            MootdxStockAdapter(enabled=settings.enable_mootdx).health(),
            AkshareSupplementAdapter(enabled=settings.enable_akshare).health(),
            *research_adapter_health(),
        ],
        latest_screening_run_id=screening.run_id if screening else None,
        latest_screening_as_of=screening.as_of if screening else None,
        latest_factor_candidate_count=len(screening.factor_filter) if screening else 0,
        research_evaluation_pass_count=sum(1 for item in evaluations if item.passed),
        research_evaluation_fail_count=sum(1 for item in evaluations if not item.passed),
        ledger_reconciled=bool(reconciliation["reconciled"]),
        ledger_reconciliation_errors=cast(list[str], reconciliation["errors"]),
    )
    return _envelope(payload.model_dump(mode="json"), [*warnings, *market_warnings])
