from __future__ import annotations

from typing import cast

from fastapi import Request

from app.services.dashboard import DashboardService
from app.services.market import MarketService
from app.services.paper import PaperService
from app.services.stock import StockService


def get_dashboard_service(request: Request) -> DashboardService:
    return cast(DashboardService, request.app.state.dashboard_service)


def get_market_service(request: Request) -> MarketService:
    return cast(MarketService, request.app.state.market_service)


def get_stock_service(request: Request) -> StockService:
    return cast(StockService, request.app.state.stock_service)


def get_paper_service(request: Request) -> PaperService:
    return cast(PaperService, request.app.state.paper_service)
