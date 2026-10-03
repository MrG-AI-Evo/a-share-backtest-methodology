from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends

from app.api.dependencies import get_dashboard_service, get_market_service
from app.core.time import iso_now
from app.models import ApiEnvelope, ApiMeta
from app.services.dashboard import DashboardService
from app.services.market import MarketService

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_model=ApiEnvelope)
async def dashboard(
    service: Annotated[DashboardService, Depends(get_dashboard_service)],
) -> ApiEnvelope:
    result = await service.get()
    return ApiEnvelope(
        data=result.payload.model_dump(mode="json"),
        meta=ApiMeta(
            request_id=str(uuid4()),
            generated_at=iso_now(),
            data_state=result.state,
            sources=result.sources,
            warnings=result.warnings,
        ),
    )


@router.get("/market/overview", response_model=ApiEnvelope)
async def market_overview(
    service: Annotated[MarketService, Depends(get_market_service)],
) -> ApiEnvelope:
    result = await service.get_overview()
    return ApiEnvelope(
        data=result.overview.model_dump(mode="json"),
        meta=ApiMeta(
            request_id=str(uuid4()),
            generated_at=iso_now(),
            data_state=result.state,
            sources=result.sources,
            warnings=result.warnings,
        ),
    )
