from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

from app.core.time import SHANGHAI, iso_now, now_shanghai
from app.models import DataState, IndexQuote, PriceBar, SourceMeta, StockBars, StockQuote

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q={symbols}"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_MINUTE_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"


@dataclass(frozen=True)
class IndexSpec:
    provider_symbol: str
    symbol: str
    fallback_name: str


INDEX_SPECS = (
    IndexSpec("sh000001", "000001.SH", "上证指数"),
    IndexSpec("sz399001", "399001.SZ", "深证成指"),
    IndexSpec("sz399006", "399006.SZ", "创业板指"),
    IndexSpec("sh000688", "000688.SH", "科创50"),
    IndexSpec("sh000300", "000300.SH", "沪深300"),
    IndexSpec("sh000852", "000852.SH", "中证1000"),
)


class TencentQuoteError(RuntimeError):
    pass


def provider_symbol(symbol: str) -> tuple[str, Literal["SSE", "SZSE", "BSE"]]:
    code = symbol.split(".", 1)[0]
    if len(code) != 6 or not code.isdigit():
        raise TencentQuoteError("股票代码必须为 6 位数字")
    if code.startswith(("4", "8", "92")):
        return f"bj{code}", "BSE"
    if code.startswith(("5", "6", "9")):
        return f"sh{code}", "SSE"
    return f"sz{code}", "SZSE"


def classify_quote_freshness(age_seconds: int) -> DataState:
    if age_seconds <= 300:
        return "live"
    if age_seconds <= 86_400:
        return "delayed"
    return "stale"


def _number(parts: list[str], index: int, default: float = 0.0) -> float:
    if index >= len(parts) or not parts[index]:
        return default
    try:
        return float(parts[index])
    except ValueError:
        return default


def _normalize_timestamp(raw: str) -> str:
    try:
        parsed = datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        return iso_now()


def _optional_positive(parts: list[str], index: int, multiplier: float = 1.0) -> float | None:
    value = _number(parts, index)
    return round(value * multiplier, 4) if value > 0 else None


def parse_tencent_stock_quote(payload: bytes, symbol: str) -> StockQuote:
    expected_provider, exchange = provider_symbol(symbol)
    text = payload.decode("gb18030", errors="replace")
    statement = next(
        (item for item in text.split(";") if item.strip().startswith(f"v_{expected_provider}=")),
        None,
    )
    if statement is None:
        raise TencentQuoteError("腾讯行情响应缺少目标股票")
    _, quoted = statement.split("=", 1)
    parts = quoted.strip().strip('"').split("~")
    if len(parts) < 50 or not parts[1]:
        raise TencentQuoteError("腾讯个股行情字段不完整")
    return StockQuote(
        symbol=parts[2],
        name=parts[1],
        exchange=exchange,
        price=_number(parts, 3),
        previous_close=_number(parts, 4),
        open=_number(parts, 5),
        high=_number(parts, 33),
        low=_number(parts, 34),
        change=_number(parts, 31),
        change_pct=_number(parts, 32),
        volume_shares=_number(parts, 36) * 100,
        amount_cny=_optional_positive(parts, 37, 10_000),
        turnover_pct=_optional_positive(parts, 38),
        pe_ttm=_optional_positive(parts, 39),
        pb=_optional_positive(parts, 46),
        total_market_cap_cny=_optional_positive(parts, 45, 100_000_000),
        float_market_cap_cny=_optional_positive(parts, 44, 100_000_000),
        limit_up=_optional_positive(parts, 47),
        limit_down=_optional_positive(parts, 48),
        updated_at=_normalize_timestamp(parts[30]),
    )


def _bar_timestamp(raw: str, period: str) -> str:
    if period == "minute":
        parsed = datetime.strptime(raw, "%Y%m%d%H%M").replace(tzinfo=SHANGHAI)
    else:
        parsed = datetime.strptime(raw, "%Y-%m-%d").replace(
            hour=15,
            minute=0,
            tzinfo=SHANGHAI,
        )
    return parsed.isoformat(timespec="minutes")


def parse_tencent_bars(rows: Any, period: str) -> list[PriceBar]:
    if not isinstance(rows, list):
        raise TencentQuoteError("腾讯 K 线响应缺少数据数组")
    bars: list[PriceBar] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            bars.append(
                PriceBar(
                    timestamp=_bar_timestamp(str(row[0]), period),
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume_shares=float(row[5]) * 100,
                )
            )
        except (TypeError, ValueError):
            continue
    if not bars:
        raise TencentQuoteError("腾讯 K 线响应没有可解析记录")
    return bars


def parse_tencent_indices(
    payload: bytes,
    specs: Iterable[IndexSpec] = INDEX_SPECS,
) -> list[IndexQuote]:
    text = payload.decode("gb18030", errors="replace")
    by_provider = {spec.provider_symbol: spec for spec in specs}
    quotes: list[IndexQuote] = []

    for statement in text.split(";"):
        if '="' not in statement:
            continue
        variable, quoted = statement.split("=", 1)
        provider_symbol = variable.strip().removeprefix("v_")
        spec = by_provider.get(provider_symbol)
        if spec is None:
            continue
        parts = quoted.strip().strip('"').split("~")
        if len(parts) < 6:
            continue
        updated_at = _normalize_timestamp(parts[30]) if len(parts) > 30 else iso_now()
        amount_wan_cny = _number(parts, 37, default=0.0)
        quotes.append(
            IndexQuote(
                symbol=spec.symbol,
                name=parts[1] or spec.fallback_name,
                price=_number(parts, 3),
                change=_number(parts, 31),
                change_pct=_number(parts, 32),
                amount_cny=amount_wan_cny * 10_000 if amount_wan_cny else None,
                market_status=None,
                updated_at=updated_at,
            )
        )

    parsed_provider_symbols = {
        f"sh{quote.symbol[:6]}" if quote.symbol.endswith(".SH") else f"sz{quote.symbol[:6]}"
        for quote in quotes
    }
    missing = set(by_provider) - parsed_provider_symbols
    if not quotes:
        raise TencentQuoteError("腾讯行情响应中没有可解析的指数数据")
    if missing:
        raise TencentQuoteError(f"腾讯行情响应缺少指数: {', '.join(sorted(missing))}")
    return quotes


class TencentQuoteSource:
    provider = "腾讯财经"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def fetch_indices(self) -> tuple[list[IndexQuote], SourceMeta]:
        symbols = ",".join(spec.provider_symbol for spec in INDEX_SPECS)
        url = TENCENT_QUOTE_URL.format(symbols=symbols)
        response = await self._client.get(url, headers={"Referer": "https://finance.qq.com/"})
        response.raise_for_status()
        quotes = parse_tencent_indices(response.content)
        fetched_at = iso_now()
        source_timestamp = max(quote.updated_at for quote in quotes)
        quote_time = datetime.fromisoformat(source_timestamp)
        age_seconds = max(0, int((now_shanghai() - quote_time).total_seconds()))
        state = classify_quote_freshness(age_seconds)
        return quotes, SourceMeta(
            provider=self.provider,
            source_url=url,
            fetched_at=fetched_at,
            source_timestamp=source_timestamp,
            state=state,
            age_seconds=age_seconds,
            notes=["公开行情；非交易所直连，不属于 Level-2"],
        )


class TencentStockSource:
    provider = "腾讯财经"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._headers = {"Referer": "https://finance.qq.com/"}

    def _meta(self, url: str, source_timestamp: str) -> SourceMeta:
        source_time = datetime.fromisoformat(source_timestamp)
        age_seconds = max(0, int((now_shanghai() - source_time).total_seconds()))
        return SourceMeta(
            provider=self.provider,
            source_url=url,
            fetched_at=iso_now(),
            source_timestamp=source_timestamp,
            state=classify_quote_freshness(age_seconds),
            age_seconds=age_seconds,
            notes=["公开行情；非交易所直连，不属于 Level-2"],
        )

    async def fetch_quote(self, symbol: str) -> tuple[StockQuote, SourceMeta]:
        provider_code, _ = provider_symbol(symbol)
        url = TENCENT_QUOTE_URL.format(symbols=provider_code)
        response = await self._client.get(url, headers=self._headers)
        response.raise_for_status()
        quote = parse_tencent_stock_quote(response.content, symbol)
        return quote, self._meta(url, quote.updated_at)

    async def fetch_bars(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "minute"],
        adjustment: Literal["qfq", "hfq", "none"],
        limit: int,
        minute_interval: int,
    ) -> tuple[StockBars, SourceMeta]:
        provider_code, _ = provider_symbol(symbol)
        if period == "minute":
            key = f"m{minute_interval}"
            params = {"param": f"{provider_code},{key},,{limit}"}
            url = str(httpx.URL(TENCENT_MINUTE_KLINE_URL, params=params))
            response = await self._client.get(
                TENCENT_MINUTE_KLINE_URL,
                params=params,
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("data", {}).get(provider_code, {}).get(key)
            bars = parse_tencent_bars(rows, "minute")
            actual_adjustment: Literal["qfq", "hfq", "none"] = "none"
        else:
            adjust_param = "" if adjustment == "none" else adjustment
            params = {"param": f"{provider_code},{period},,,{limit},{adjust_param}"}
            url = str(httpx.URL(TENCENT_KLINE_URL, params=params))
            response = await self._client.get(
                TENCENT_KLINE_URL,
                params=params,
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()
            node = payload.get("data", {}).get(provider_code, {})
            key = period if adjustment == "none" else f"{adjustment}{period}"
            rows = node.get(key) or node.get(period)
            bars = parse_tencent_bars(rows, period)
            actual_adjustment = adjustment
        latest = bars[-1].timestamp
        result = StockBars(
            symbol=symbol.split(".", 1)[0],
            period=period,
            adjustment=actual_adjustment,
            minute_interval=minute_interval if period == "minute" else None,
            bars=bars,
        )
        return result, self._meta(url, latest)
