from __future__ import annotations

from dataclasses import dataclass

from app.models import DashboardPayload, DataState, SourceMeta
from app.repositories.research import ResearchRepository
from app.services.market import MarketService
from app.services.paper import PaperService
from app.services.research_evaluation import qualified_latest_research


@dataclass
class DashboardResult:
    payload: DashboardPayload
    sources: list[SourceMeta]
    warnings: list[str]
    state: DataState


class DashboardService:
    def __init__(
        self,
        market: MarketService,
        research: ResearchRepository,
        paper: PaperService,
    ) -> None:
        self._market = market
        self._research = research
        self._paper = paper

    async def get(self) -> DashboardResult:
        market = await self._market.get_overview()
        research, research_warnings = qualified_latest_research(self._research)
        paper, valuation_warnings, valuation_sources = await self._paper.refresh_valuation()
        return DashboardResult(
            payload=DashboardPayload(market=market.overview, research=research, paper=paper),
            sources=[*market.sources, *valuation_sources],
            warnings=[*market.warnings, *research_warnings, *valuation_warnings],
            state=market.state,
        )
