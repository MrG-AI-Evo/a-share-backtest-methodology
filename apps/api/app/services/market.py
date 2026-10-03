from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic

import duckdb
import httpx

from app.adapters.public_data import AkshareSupplementAdapter, OptionalAdapterUnavailable
from app.core.settings import Settings
from app.core.time import iso_now
from app.models import (
    DataState,
    IndexQuote,
    MarketBreadth,
    MarketOverview,
    SectorPerformance,
    SourceMeta,
)
from app.repositories.market_cache import MarketCacheRepository
from app.sources.eastmoney import EastmoneyMarketSource
from app.sources.sina import SinaMarketSource
from app.sources.tencent import TencentQuoteSource


@dataclass
class MarketResult:
    overview: MarketOverview
    sources: list[SourceMeta]
    warnings: list[str]
    state: DataState


class MarketService:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        cache: MarketCacheRepository,
    ) -> None:
        self._settings = settings
        self._quote_source = TencentQuoteSource(client)
        self._market_source = EastmoneyMarketSource(client)
        self._sector_fallback = SinaMarketSource(client)
        self._breadth_fallback = AkshareSupplementAdapter(enabled=settings.enable_akshare)
        self._cache = cache
        self._lock = asyncio.Lock()
        self._cached_at = 0.0
        self._cached: MarketResult | None = None

    async def get_overview(self) -> MarketResult:
        if self._cached and monotonic() - self._cached_at < self._settings.market_cache_seconds:
            return self._cached
        async with self._lock:
            if self._cached and monotonic() - self._cached_at < self._settings.market_cache_seconds:
                return self._cached
            result = await self._fetch()
            self._cached = result
            self._cached_at = monotonic()
            return result

    def health_snapshot(self) -> tuple[list[SourceMeta], list[str]]:
        if self._cached is None:
            return [], ["市场服务尚未执行首次刷新"]
        return self._cached.sources, self._cached.warnings

    async def _fetch(self) -> MarketResult:
        warnings: list[str] = []
        sources: list[SourceMeta] = []
        indices: list[IndexQuote] = []
        breadth = MarketBreadth()
        sectors: list[SectorPerformance] = []
        quote_state: DataState = "unavailable"

        try:
            indices, quote_source = await self._quote_source.fetch_indices()
            sources.append(quote_source)
            quote_state = quote_source.state
        except (httpx.HTTPError, RuntimeError) as exc:
            sources.append(
                SourceMeta(
                    provider="腾讯财经",
                    fetched_at=iso_now(),
                    state="unavailable",
                    notes=[str(exc)],
                )
            )
            warnings.append("指数公开行情暂不可用")

        try:
            breadth, breadth_source = await self._market_source.fetch_breadth()
            sources.append(breadth_source)
        except (httpx.HTTPError, RuntimeError) as exc:
            sources.append(
                SourceMeta(
                    provider="东方财富·市场宽度",
                    fetched_at=iso_now(),
                    state="unavailable",
                    notes=[str(exc)],
                )
            )
            try:
                breadth, breadth_source = await self._breadth_fallback.fetch_breadth()
                sources.append(breadth_source)
                warnings.append("东方财富直连宽度不可用，已降级到 AKShare 包装接口（同源血缘）")
            except OptionalAdapterUnavailable as fallback_exc:
                sources.append(
                    SourceMeta(
                        provider="AKShare / 东方财富·市场宽度",
                        fetched_at=iso_now(),
                        state="unavailable",
                        notes=[str(fallback_exc)],
                    )
                )
                warnings.append("市场宽度公开源暂不可用，已保留空值")

        try:
            sectors, sector_source = await self._market_source.fetch_sectors()
            sources.append(sector_source)
        except (httpx.HTTPError, RuntimeError) as exc:
            sources.append(
                SourceMeta(
                    provider="东方财富·板块",
                    fetched_at=iso_now(),
                    state="unavailable",
                    notes=[str(exc)],
                )
            )
            try:
                sectors, sector_source = await self._sector_fallback.fetch_sectors()
                sources.append(sector_source)
                warnings.append("东方财富板块源不可用，已降级到新浪财经")
            except (httpx.HTTPError, RuntimeError) as fallback_exc:
                sources.append(
                    SourceMeta(
                        provider="新浪财经·板块",
                        fetched_at=iso_now(),
                        state="unavailable",
                        notes=[str(fallback_exc)],
                    )
                )
                warnings.append("板块公开源暂不可用，已保留空值")

        overview = MarketOverview(
            indices=indices,
            breadth=breadth,
            sectors=sectors,
            trading_phase="UNKNOWN",
        )
        if indices:
            source_timestamp = next(
                (source.source_timestamp for source in sources if source.source_timestamp),
                None,
            )
            try:
                await self._cache.append(overview, source_timestamp)
            except (duckdb.Error, OSError) as exc:
                warnings.append(f"本地行情缓存写入失败：{exc}")
            return MarketResult(
                overview=overview,
                sources=sources,
                warnings=warnings,
                state=quote_state,
            )

        cached = await self._cache.latest()
        if cached is not None:
            cached_overview, cached_source_time, cached_at = cached
            sources.append(
                SourceMeta(
                    provider="本地 DuckDB 缓存",
                    fetched_at=cached_at,
                    source_timestamp=cached_source_time,
                    state="stale",
                    notes=["公共源失败后的只读降级；页面必须显示过期状态"],
                )
            )
            warnings.append("公共行情不可用，已降级到最近一次本地缓存")
            return MarketResult(cached_overview, sources, warnings, "stale")

        warnings.append("没有可用缓存；没有用演示数据冒充实时行情")
        return MarketResult(overview, sources, warnings, "unavailable")
