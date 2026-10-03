from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import httpx

from app.core.time import iso_now
from app.models import SourceMeta, StockQuote
from app.sources.tencent import parse_tencent_stock_quote, provider_symbol


class UniverseSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class ListedSecurity:
    symbol: str
    name: str
    exchange: Literal["SSE", "SZSE", "BSE"]
    listing_date: str | None = None
    industry: str | None = None


@dataclass(frozen=True)
class SnapshotQuote:
    symbol: str
    price: float | None
    amount_cny: float | None
    updated_at: str


@dataclass(frozen=True)
class DailyHistory:
    dates: list[date]
    closes: list[float]
    amounts_cny: list[float | None]
    source_id: str
    amount_is_proxy: bool = False


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None or bool(getattr(frame, "empty", True)):
        return []
    payload = frame.to_dict(orient="records")
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clean_name(value: Any) -> str:
    return str(value or "").replace("\x00", "").strip()


class MootdxUniverseSource:
    source_id = "mootdx:tdx-public"
    _prefixes = {
        "SZSE": ("000", "001", "002", "003", "300", "301"),
        "SSE": ("600", "601", "603", "605", "688", "689"),
    }

    def __init__(self) -> None:
        try:
            quotes_module = importlib.import_module("mootdx.quotes")
            self._client = quotes_module.Quotes.factory(
                market="std",
                multithread=False,
                heartbeat=False,
                timeout=15,
            )
        except (ImportError, AttributeError, TypeError) as exc:
            raise UniverseSourceError(f"mootdx 初始化失败: {exc}") from exc

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def list_securities(self) -> list[ListedSecurity]:
        output: list[ListedSecurity] = []
        for market, exchange in ((0, "SZSE"), (1, "SSE")):
            try:
                rows = _records(self._client.stocks(market=market))
            except Exception as exc:
                raise UniverseSourceError(f"mootdx {exchange} 证券列表失败: {exc}") from exc
            prefixes = self._prefixes[exchange]
            for row in rows:
                code = str(row.get("code") or "")
                name = _clean_name(row.get("name"))
                if len(code) == 6 and code.startswith(prefixes) and name:
                    output.append(
                        ListedSecurity(
                            symbol=code,
                            name=name,
                            exchange=exchange,  # type: ignore[arg-type]
                        )
                    )
        unique = {item.symbol: item for item in output}
        if not any(item.exchange == "SSE" for item in unique.values()):
            raise UniverseSourceError("mootdx 上交所普通 A 股列表为空")
        if not any(item.exchange == "SZSE" for item in unique.values()):
            raise UniverseSourceError("mootdx 深交所普通 A 股列表为空")
        return sorted(unique.values(), key=lambda item: item.symbol)

    def snapshot_quotes(self, symbols: list[str]) -> dict[str, SnapshotQuote]:
        output: dict[str, SnapshotQuote] = {}
        pending = list(symbols)
        for batch_size in (60, 30, 10):
            for start in range(0, len(pending), batch_size):
                batch = pending[start : start + batch_size]
                try:
                    rows = _records(self._client.quotes(symbol=batch))
                except Exception as exc:
                    raise UniverseSourceError(f"mootdx 批量行情失败: {exc}") from exc
                for row in rows:
                    code = str(row.get("code") or "")
                    if code not in batch:
                        continue
                    output[code] = SnapshotQuote(
                        symbol=code,
                        price=_float(row.get("price")),
                        amount_cny=_float(row.get("amount")),
                        updated_at=str(row.get("servertime") or "UNKNOWN"),
                    )
            pending = [symbol for symbol in pending if symbol not in output]
            if not pending:
                break
        return output

    def daily_history(self, symbol: str, limit: int, cutoff: date) -> DailyHistory:
        try:
            rows = _records(self._client.bars(symbol=symbol, frequency=9, start=0, offset=limit))
        except Exception as exc:
            raise UniverseSourceError(f"mootdx {symbol} 日线失败: {exc}") from exc
        parsed: list[tuple[date, float, float | None]] = []
        for row in rows:
            raw_date = row.get("datetime") or row.get("date")
            close = _float(row.get("close"))
            if raw_date is None or close is None:
                continue
            try:
                day = datetime.fromisoformat(str(raw_date).replace("/", "-")).date()
            except ValueError:
                continue
            if day <= cutoff:
                parsed.append((day, close, _float(row.get("amount"))))
        parsed.sort(key=lambda item: item[0])
        return DailyHistory(
            dates=[item[0] for item in parsed],
            closes=[item[1] for item in parsed],
            amounts_cny=[item[2] for item in parsed],
            source_id=f"{self.source_id}:bars:none:{symbol}",
        )

    def benchmark_history(self, limit: int, cutoff: date) -> DailyHistory:
        try:
            rows = _records(
                self._client.index_bars(symbol="000300", frequency=9, start=0, offset=limit)
            )
        except Exception as exc:
            raise UniverseSourceError(f"mootdx 沪深300日线失败: {exc}") from exc
        parsed: list[tuple[date, float]] = []
        for row in rows:
            raw_date = row.get("datetime") or row.get("date")
            close = _float(row.get("close"))
            if raw_date is None or close is None:
                continue
            try:
                day = datetime.fromisoformat(str(raw_date).replace("/", "-")).date()
            except ValueError:
                continue
            if day <= cutoff:
                parsed.append((day, close))
        parsed.sort(key=lambda item: item[0])
        return DailyHistory(
            dates=[item[0] for item in parsed],
            closes=[item[1] for item in parsed],
            amounts_cny=[None] * len(parsed),
            source_id=f"{self.source_id}:index-bars:000300",
        )


class BseOfficialListSource:
    source_id = "bse-official:nqxxCnzq"
    url = "https://www.bse.cn/nqxxController/nqxxCnzq.do"

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    @staticmethod
    def _parse(text: str) -> dict[str, Any]:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end <= start:
            raise UniverseSourceError("北交所股票列表响应不是预期 JSONP")
        try:
            payload = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise UniverseSourceError(f"北交所股票列表 JSONP 解析失败: {exc}") from exc
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise UniverseSourceError("北交所股票列表响应根节点异常")
        return payload[0]

    def list_securities(self) -> tuple[list[ListedSecurity], str | None]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/110 Safari/537.36"
            )
        }
        form = {
            "page": "0",
            "typejb": "T",
            "xxfcbj[]": "2",
            "xxzqdm": "",
            "sortfield": "xxzqdm",
            "sorttype": "asc",
        }
        try:
            response = self._client.post(self.url, data=form, headers=headers)
            response.raise_for_status()
            first = self._parse(response.text)
            total_pages = int(first.get("totalPages") or 0)
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise UniverseSourceError(f"北交所官方股票列表失败: {exc}") from exc
        if total_pages < 1:
            raise UniverseSourceError("北交所官方股票列表页数为 0")
        records: list[dict[str, Any]] = []
        report_dates: set[str] = set()
        for page in range(total_pages):
            form["page"] = str(page)
            try:
                response = self._client.post(self.url, data=form, headers=headers)
                response.raise_for_status()
                node = self._parse(response.text)
            except httpx.HTTPError as exc:
                raise UniverseSourceError(f"北交所官方股票列表第 {page} 页失败: {exc}") from exc
            content = node.get("content")
            if not isinstance(content, list):
                raise UniverseSourceError(f"北交所官方股票列表第 {page} 页缺少 content")
            records.extend(row for row in content if isinstance(row, dict))
        output: list[ListedSecurity] = []
        for row in records:
            code = str(row.get("xxzqdm") or "")
            name = _clean_name(row.get("xxzqjc"))
            if not (len(code) == 6 and code.startswith("920") and name):
                continue
            listing_raw = str(row.get("fxssrq") or "")
            listing_date = None
            if len(listing_raw) == 8 and listing_raw.isdigit():
                listing_date = f"{listing_raw[:4]}-{listing_raw[4:6]}-{listing_raw[6:]}"
            report_raw = str(row.get("xxjsrq") or "")
            if len(report_raw) == 8 and report_raw.isdigit():
                report_dates.add(f"{report_raw[:4]}-{report_raw[4:6]}-{report_raw[6:]}")
            output.append(
                ListedSecurity(
                    symbol=code,
                    name=name,
                    exchange="BSE",
                    listing_date=listing_date,
                    industry=_clean_name(row.get("xxhyzl")) or None,
                )
            )
        unique = {item.symbol: item for item in output}
        if not unique:
            raise UniverseSourceError("北交所官方接口没有普通上市股票")
        report_date = max(report_dates) if report_dates else None
        return sorted(unique.values(), key=lambda item: item.symbol), report_date


class TencentBatchQuoteSource:
    source_id = "tencent:public-quote"
    url = "https://qt.gtimg.cn/q={symbols}"

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def fetch(self, symbols: list[str]) -> dict[str, StockQuote]:
        output: dict[str, StockQuote] = {}
        for start in range(0, len(symbols), 60):
            batch = symbols[start : start + 60]
            provider_codes = [provider_symbol(symbol)[0] for symbol in batch]
            url = self.url.format(symbols=",".join(provider_codes))
            try:
                response = self._client.get(
                    url,
                    headers={"Referer": "https://finance.qq.com/"},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise UniverseSourceError(f"腾讯批量行情失败: {exc}") from exc
            for symbol in batch:
                try:
                    output[symbol] = parse_tencent_stock_quote(response.content, symbol)
                except (RuntimeError, ValueError):
                    continue
        return output


class SinaBseHistorySource:
    source_id = "sina:cn-market-kline"
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{provider}=/"
        "CN_MarketDataService.getKLineData"
    )

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def daily_history(self, symbol: str, limit: int, cutoff: date) -> DailyHistory:
        provider = provider_symbol(symbol)[0]
        try:
            response = self._client.get(
                self.url.format(provider=provider),
                params={"symbol": provider, "scale": "240", "ma": "no", "datalen": limit},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UniverseSourceError(f"新浪北交所 {symbol} 日线失败: {exc}") from exc
        start = response.text.find("([")
        end = response.text.rfind("])")
        if start < 0 or end <= start:
            raise UniverseSourceError(f"新浪北交所 {symbol} 日线响应格式异常")
        try:
            rows = json.loads(response.text[start + 1 : end + 1])
        except json.JSONDecodeError as exc:
            raise UniverseSourceError(f"新浪北交所 {symbol} 日线解析失败: {exc}") from exc
        parsed: list[tuple[date, float, float]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    day = date.fromisoformat(str(row["day"]))
                    high = float(row["high"])
                    low = float(row["low"])
                    close = float(row["close"])
                    volume = float(row["volume"])
                except (KeyError, TypeError, ValueError):
                    continue
                if day <= cutoff and min(high, low, close, volume) > 0:
                    amount_proxy = ((high + low + close) / 3) * volume
                    parsed.append((day, close, amount_proxy))
        parsed.sort(key=lambda item: item[0])
        return DailyHistory(
            dates=[item[0] for item in parsed],
            closes=[item[1] for item in parsed],
            amounts_cny=[item[2] for item in parsed],
            source_id=f"{self.source_id}:bars:none:{symbol}",
            amount_is_proxy=True,
        )


def universe_source_meta(
    provider: str,
    url: str,
    source_timestamp: str | None,
    notes: list[str],
) -> SourceMeta:
    return SourceMeta(
        provider=provider,
        source_url=url,
        fetched_at=iso_now(),
        source_timestamp=source_timestamp,
        state="delayed",
        notes=notes,
    )
