from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.adapters.public_data import AkshareSupplementAdapter, MootdxStockAdapter
from app.api.routes.backtests import router as backtests_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.health import router as health_router
from app.api.routes.paper import router as paper_router
from app.api.routes.pipeline import router as pipeline_router
from app.api.routes.runtime import router as runtime_router
from app.api.routes.stocks import router as stocks_router
from app.backtests.historical import HistoricalDataRepository
from app.backtests.repository import BacktestRepository
from app.backtests.service import BacktestService
from app.core.policy import load_policy
from app.core.settings import get_settings
from app.repositories.bar_cache import BarCacheRepository
from app.repositories.market_cache import MarketCacheRepository
from app.repositories.paper import PaperRepository
from app.repositories.research import ResearchRepository
from app.repositories.screening import ScreeningRepository
from app.runtime.repository import DeterministicRuntimeRepository
from app.runtime.service import DeterministicRuntimeService
from app.services.dashboard import DashboardService
from app.services.market import MarketService
from app.services.paper import PaperService
from app.services.stock import StockService
from app.sources.tencent import TencentStockSource

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout_seconds),
        follow_redirects=True,
    )
    policy = load_policy(settings)
    paper = PaperRepository(settings)
    await paper.initialize(policy.initial_cash_cny)
    market_cache = MarketCacheRepository(settings)
    await market_cache.initialize()
    bar_cache = BarCacheRepository(settings)
    await bar_cache.initialize()
    market = MarketService(settings, client, market_cache)
    research = ResearchRepository(settings)
    screening = ScreeningRepository(settings)
    app.state.market_service = market
    app.state.research_repository = research
    app.state.screening_repository = screening
    stock = StockService(
        TencentStockSource(client),
        bar_cache,
        [
            MootdxStockAdapter(enabled=settings.enable_mootdx),
            AkshareSupplementAdapter(enabled=settings.enable_akshare),
        ],
    )
    app.state.stock_service = stock
    app.state.paper_repository = paper
    paper_service = PaperService(settings, paper, research, stock)
    app.state.paper_service = paper_service
    app.state.dashboard_service = DashboardService(market, research, paper_service)
    backtest_service = BacktestService(
        settings,
        BacktestRepository(settings.backtest_database),
        HistoricalDataRepository(
            settings.backtest_analytics_database,
            settings.backtest_parquet_dir,
        ),
    )
    await backtest_service.initialize()
    app.state.backtest_service = backtest_service
    deterministic_runtime = DeterministicRuntimeService(
        settings,
        DeterministicRuntimeRepository(settings.deterministic_runtime_database),
        backtest_service,
        BacktestRepository(settings.backtest_database),
        paper,
        screening,
    )
    await deterministic_runtime.initialize()
    app.state.deterministic_runtime_service = deterministic_runtime
    yield
    await client.aclose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.web_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Request-ID", "Idempotency-Key"],
)
app.include_router(health_router)
app.include_router(dashboard_router, prefix=settings.api_prefix)
app.include_router(stocks_router, prefix=settings.api_prefix)
app.include_router(paper_router, prefix=settings.api_prefix)
app.include_router(pipeline_router, prefix=settings.api_prefix)
app.include_router(backtests_router, prefix=settings.api_prefix)
app.include_router(runtime_router, prefix=settings.api_prefix)
