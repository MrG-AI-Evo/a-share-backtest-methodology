from __future__ import annotations

from typing import Literal

import duckdb
import httpx

from app.adapters.public_data import OptionalAdapterUnavailable
from app.models import SourceMeta, StockBars, StockQuote
from app.repositories.bar_cache import BarCacheRepository
from app.services.indicators import calculate_indicators
from app.sources.tencent import TencentQuoteError, TencentStockSource


class StockServiceError(RuntimeError):
    pass


class StockService:
    def __init__(
        self,
        source: TencentStockSource,
        cache: BarCacheRepository,
        fallback_sources: list[object] | None = None,
    ) -> None:
        self._source = source
        self._cache = cache
        self._fallback_sources = fallback_sources or []

    async def quote(self, symbol: str) -> tuple[StockQuote, SourceMeta]:
        try:
            return await self._source.fetch_quote(symbol)
        except (httpx.HTTPError, TencentQuoteError) as exc:
            raise StockServiceError(str(exc)) from exc

    async def bars(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "minute"],
        adjustment: Literal["qfq", "hfq", "none"],
        limit: int,
        minute_interval: int,
    ) -> tuple[StockBars, SourceMeta, list[str]]:
        warnings: list[str] = []
        if period == "minute":
            adjustment = "none"
        try:
            result, source = await self._source.fetch_bars(
                symbol,
                period,
                adjustment,
                limit,
                minute_interval,
            )
            result = result.model_copy(update={"bars": calculate_indicators(result.bars)})
            try:
                await self._cache.upsert(
                    result.symbol,
                    result.period,
                    result.adjustment,
                    result.minute_interval or 0,
                    result.bars,
                    source.provider,
                )
            except (duckdb.Error, OSError) as exc:
                warnings.append(f"K线缓存写入失败：{exc}")
            return result, source, warnings
        except (httpx.HTTPError, TencentQuoteError, ValueError) as exc:
            upstream_error = str(exc)
            for fallback in self._fallback_sources:
                fetch = getattr(fallback, "fetch_bars", None)
                if not callable(fetch):
                    continue
                try:
                    result, source = await fetch(
                        symbol,
                        period,
                        adjustment,
                        limit,
                        minute_interval,
                    )
                    result = result.model_copy(update={"bars": calculate_indicators(result.bars)})
                    warnings.append(
                        f"腾讯 K 线失败，已字段级降级到 {source.provider}: {upstream_error}"
                    )
                    return result, source, warnings
                except OptionalAdapterUnavailable as fallback_exc:
                    warnings.append(str(fallback_exc))
            cached = await self._cache.load(
                symbol.split(".", 1)[0],
                period,
                adjustment,
                minute_interval if period == "minute" else 0,
                limit,
            )
            if cached is None:
                raise StockServiceError(str(exc)) from exc
            bars, fetched_at = cached
            warnings.append(f"公共 K 线源失败，已读取本地缓存：{exc}")
            result = StockBars(
                symbol=symbol.split(".", 1)[0],
                period=period,
                adjustment=adjustment,
                minute_interval=minute_interval if period == "minute" else None,
                bars=calculate_indicators(bars),
            )
            source = SourceMeta(
                provider="本地 DuckDB 缓存",
                fetched_at=fetched_at,
                source_timestamp=bars[-1].timestamp,
                state="stale",
                notes=["公开源失败后的只读降级"],
            )
            return result, source, warnings
