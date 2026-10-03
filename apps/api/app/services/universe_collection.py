from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import datetime, time

import httpx

from app.core.settings import Settings
from app.core.time import SHANGHAI, iso_now, now_shanghai
from app.core.trading_calendar import load_trading_calendar
from app.models import UniverseCoverage, UniverseSecurity, UniverseSnapshot
from app.services.screening import load_screening_policy
from app.sources.universe import (
    BseOfficialListSource,
    DailyHistory,
    ListedSecurity,
    MootdxUniverseSource,
    SinaBseHistorySource,
    SnapshotQuote,
    TencentBatchQuoteSource,
    UniverseSourceError,
    universe_source_meta,
)


class UniverseCollectionError(RuntimeError):
    pass


def _risk_status(name: str) -> str:
    upper = name.upper()
    if "退" in name:
        return "DELISTING"
    if "ST" in upper:
        return "ST"
    return "NORMAL"


def _trading_status(quote: SnapshotQuote | None) -> str:
    if quote is None or quote.price is None:
        return "UNKNOWN"
    if quote.amount_cny is None:
        return "SUSPENDED"
    return "NORMAL"


def _momentum(closes: list[float], days: int) -> float | None:
    if len(closes) <= days or closes[-days - 1] <= 0:
        return None
    return round((closes[-1] / closes[-days - 1] - 1) * 100, 4)


def _volatility(closes: list[float]) -> float | None:
    window = closes[-21:]
    if len(window) < 21:
        return None
    returns = [window[index] / window[index - 1] - 1 for index in range(1, len(window))]
    return round(statistics.stdev(returns) * math.sqrt(252) * 100, 4)


def _average_amount(history: DailyHistory) -> float | None:
    values = [value for value in history.amounts_cny[-20:] if value is not None]
    if len(values) < 20:
        return None
    return round(sum(values) / len(values), 2)


class UniverseCollectionService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._policy = load_screening_policy(settings)

    def _validate_cutoff(self, as_of: datetime) -> datetime:
        if as_of.tzinfo is None:
            raise UniverseCollectionError("全市场采集 as_of 必须带时区")
        local = as_of.astimezone(SHANGHAI)
        now = now_shanghai()
        if local.date() != now.date():
            raise UniverseCollectionError("V1 实时采集只允许今天的 point-in-time 截止时点")
        calendar = load_trading_calendar(self._settings)
        if not calendar.is_trading_day(local.date()):
            raise UniverseCollectionError("全市场正式采集必须在交易日执行")
        if local.timetz().replace(tzinfo=None) < time(15, 0) or now.time() < time(15, 0):
            raise UniverseCollectionError("正式 300→30 采集只在 15:00 收盘后执行")
        if local > now:
            raise UniverseCollectionError("as_of 不能晚于当前时间")
        return local

    def collect(
        self,
        as_of: datetime,
        run_id: str,
        *,
        history_limit: int = 160,
        max_history_requests: int = 520,
        screenable_buffer: int = 40,
    ) -> UniverseSnapshot:
        cutoff = self._validate_cutoff(as_of)
        if history_limit < self._policy.minimum_history_days + 1:
            raise UniverseCollectionError("history_limit 不足以计算硬门和 60 日动量")
        if max_history_requests < self._policy.hard_target:
            raise UniverseCollectionError("max_history_requests 不能小于 300")
        warnings: list[str] = []
        http_client = httpx.Client(timeout=20, follow_redirects=True)
        tdx: MootdxUniverseSource | None = None
        try:
            tdx = MootdxUniverseSource()
            mainland = tdx.list_securities()
            mainland_quotes = tdx.snapshot_quotes([item.symbol for item in mainland])
            benchmark = tdx.benchmark_history(history_limit, cutoff.date())
            bse, bse_report_date = BseOfficialListSource(http_client).list_securities()
            tencent = TencentBatchQuoteSource(http_client)
            mainland_missing = [
                item.symbol for item in mainland if item.symbol not in mainland_quotes
            ]
            mainland_fallback_details = tencent.fetch(mainland_missing)
            mainland_fallback_symbols = set(mainland_fallback_details)
            for symbol, fallback_quote in mainland_fallback_details.items():
                mainland_quotes[symbol] = SnapshotQuote(
                    symbol=symbol,
                    price=fallback_quote.price if fallback_quote.price > 0 else None,
                    amount_cny=fallback_quote.amount_cny,
                    updated_at=fallback_quote.updated_at,
                )
            if mainland_fallback_symbols:
                warnings.append(
                    f"{len(mainland_fallback_symbols)} 只沪深股票快照由腾讯字段级降级补齐"
                )
            bse_details = tencent.fetch([item.symbol for item in bse])
            bse_quotes = {
                symbol: SnapshotQuote(
                    symbol=symbol,
                    price=bse_quote.price if bse_quote.price > 0 else None,
                    amount_cny=bse_quote.amount_cny,
                    updated_at=bse_quote.updated_at,
                )
                for symbol, bse_quote in bse_details.items()
            }
            listed = [*mainland, *bse]
            quotes = {**mainland_quotes, **bse_quotes}
            if len({item.symbol for item in listed}) != len(listed):
                raise UniverseCollectionError("三交易所上市清单存在重复代码")

            def quote_sort_key(item: ListedSecurity) -> tuple[float, str]:
                symbol = item.symbol
                quote = quotes.get(symbol)
                return (-((quote.amount_cny if quote else None) or 0), symbol)

            candidates = sorted(
                listed,
                key=quote_sort_key,
            )
            detail_symbols = [item.symbol for item in candidates[:max_history_requests]]
            details = tencent.fetch(detail_symbols)
            bse_history = SinaBseHistorySource(http_client)
            benchmark_momentum = _momentum(benchmark.closes, 20)
            enriched: dict[str, UniverseSecurity] = {}
            screenable_count = 0
            history_errors = 0
            for item in candidates[:max_history_requests]:
                snapshot_quote = quotes.get(item.symbol)
                risk = _risk_status(item.name)
                trading = _trading_status(snapshot_quote)
                if risk != "NORMAL" or trading != "NORMAL":
                    continue
                try:
                    history = (
                        bse_history.daily_history(item.symbol, history_limit, cutoff.date())
                        if item.exchange == "BSE"
                        else tdx.daily_history(item.symbol, history_limit, cutoff.date())
                    )
                except UniverseSourceError:
                    history_errors += 1
                    continue
                if not history.dates or history.dates[-1] != cutoff.date():
                    history_errors += 1
                    continue
                avg_amount = _average_amount(history)
                detail = details.get(item.symbol)
                momentum_20 = _momentum(history.closes, 20)
                source_ids = [
                    "mootdx:tdx-public:list"
                    if item.exchange != "BSE"
                    else "bse-official:nqxxCnzq:list",
                    history.source_id,
                    (
                        "tencent:public-quote:fallback"
                        if item.symbol in mainland_fallback_symbols
                        else "mootdx:tdx-public:snapshot"
                    )
                    if item.exchange != "BSE"
                    else "tencent:public-quote:snapshot",
                    "tencent:public-quote:candidate-details",
                ]
                if history.amount_is_proxy:
                    source_ids.append("CALCULATION:sina-typical-price-volume-amount-proxy")
                row = UniverseSecurity(
                    symbol=item.symbol,
                    name=item.name,
                    exchange=item.exchange,
                    industry=item.industry,
                    risk_status=risk,
                    trading_status=trading,
                    listing_date=item.listing_date,
                    history_days=len(history.dates),
                    last_price=snapshot_quote.price if snapshot_quote else None,
                    avg_amount_20d_cny=avg_amount,
                    # 腾讯这里只提供当日换手率，不能冒充 20 日换手率。
                    turnover_20d_pct=None,
                    pe_ttm=detail.pe_ttm if detail else None,
                    pb=detail.pb if detail else None,
                    momentum_20d_pct=momentum_20,
                    momentum_60d_pct=_momentum(history.closes, 60),
                    relative_strength_20d_pct=(
                        round(momentum_20 - benchmark_momentum, 4)
                        if momentum_20 is not None and benchmark_momentum is not None
                        else None
                    ),
                    volatility_20d_pct=_volatility(history.closes),
                    data_quality="B",
                    as_of=cutoff.isoformat(timespec="seconds"),
                    source_ids=source_ids,
                )
                enriched[item.symbol] = row
                if (
                    row.history_days >= self._policy.minimum_history_days
                    and (row.avg_amount_20d_cny or 0) >= self._policy.minimum_avg_amount_20d_cny
                ):
                    screenable_count += 1
                if screenable_count >= self._policy.hard_target + screenable_buffer:
                    break
            if history_errors:
                warnings.append(f"{history_errors} 个候选历史行情失败或未更新到截止日")
            rows: list[UniverseSecurity] = []
            for item in listed:
                ready = enriched.get(item.symbol)
                if ready is not None:
                    rows.append(ready)
                    continue
                snapshot_quote = quotes.get(item.symbol)
                rows.append(
                    UniverseSecurity(
                        symbol=item.symbol,
                        name=item.name,
                        exchange=item.exchange,
                        industry=item.industry,
                        risk_status=_risk_status(item.name),
                        trading_status=_trading_status(snapshot_quote),
                        listing_date=item.listing_date,
                        history_days=0,
                        last_price=snapshot_quote.price if snapshot_quote else None,
                        avg_amount_20d_cny=None,
                        data_quality="C",
                        as_of=cutoff.isoformat(timespec="seconds"),
                        source_ids=[
                            "mootdx:tdx-public:list"
                            if item.exchange != "BSE"
                            else "bse-official:nqxxCnzq:list",
                            "mootdx:tdx-public:snapshot"
                            if item.exchange != "BSE"
                            and item.symbol not in mainland_fallback_symbols
                            else "tencent:public-quote:fallback"
                            if item.exchange != "BSE"
                            else "tencent:public-quote:snapshot",
                        ],
                    )
                )
            rows.sort(key=lambda item: item.symbol)
            listed_counts: Counter[str] = Counter(item.exchange for item in listed)
            quote_counts: Counter[str] = Counter(
                item.exchange for item in listed if item.symbol in quotes
            )
            enriched_counts: Counter[str] = Counter(row.exchange for row in enriched.values())
            coverage_rates = {
                exchange: round(quote_counts[exchange] / listed_counts[exchange], 6)
                if listed_counts[exchange]
                else 0.0
                for exchange in ("SSE", "SZSE", "BSE")
            }
            exchanges = ("SSE", "SZSE", "BSE")
            complete_listing = all(listed_counts[exchange] > 0 for exchange in exchanges)
            formal = (
                complete_listing
                and all(coverage_rates[exchange] >= 0.95 for exchange in ("SSE", "SZSE", "BSE"))
                and screenable_count >= self._policy.hard_target
                and bse_report_date == cutoff.date().isoformat()
            )
            if bse_report_date != cutoff.date().isoformat():
                warnings.append("北交所官方列表报告日与筛选截止日不一致")
            if screenable_count < self._policy.hard_target:
                warnings.append("具备完整历史与流动性数据的股票少于 300，禁止正式筛选")
            sources = [
                universe_source_meta(
                    "mootdx / 通达信公开行情",
                    "tcp://public-tdx-server:7709",
                    cutoff.isoformat(timespec="seconds"),
                    ["沪深普通 A 股列表、批量行情、不复权日线和沪深300基准"],
                ),
                universe_source_meta(
                    "北京证券交易所官网",
                    BseOfficialListSource.url,
                    f"{bse_report_date}T15:00:00+08:00" if bse_report_date else None,
                    ["北交所上市股票代码、简称、上市日期和行业"],
                ),
                universe_source_meta(
                    "腾讯财经",
                    "https://qt.gtimg.cn/",
                    cutoff.isoformat(timespec="seconds"),
                    ["北交所快照、沪深缺失快照字段级降级及候选估值/换手率补充"],
                ),
                universe_source_meta(
                    "新浪财经",
                    "https://quotes.sina.cn/",
                    cutoff.isoformat(timespec="seconds"),
                    ["北交所不复权日线；20日成交额为典型价×成交量代理计算"],
                ),
            ]
            return UniverseSnapshot(
                run_id=run_id,
                as_of=cutoff.isoformat(timespec="seconds"),
                retrieved_at=iso_now(),
                coverage=UniverseCoverage(
                    listed_count_by_exchange={
                        exchange: listed_counts[exchange] for exchange in exchanges
                    },
                    quote_count_by_exchange={
                        exchange: quote_counts[exchange] for exchange in exchanges
                    },
                    enriched_count_by_exchange={
                        exchange: enriched_counts[exchange] for exchange in exchanges
                    },
                    quote_coverage_by_exchange=coverage_rates,
                    screenable_count=screenable_count,
                    complete_listing=complete_listing,
                    formal_screening_eligible=formal,
                ),
                sources=sources,
                warnings=warnings,
                rows=rows,
            )
        except UniverseSourceError as exc:
            raise UniverseCollectionError(str(exc)) from exc
        finally:
            if tdx is not None:
                tdx.close()
            http_client.close()
