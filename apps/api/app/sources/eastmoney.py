from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any, Literal

import httpx

from app.core.time import iso_now
from app.models import MarketBreadth, SectorPerformance, SourceMeta
from app.services.market_metrics import calculate_sector_heat

EASTMONEY_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EASTMONEY_GROUP_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
MARKET_GROUP_SECIDS = "1.000001,0.399106,0.899050"


class EastmoneyDataError(RuntimeError):
    pass


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise EastmoneyDataError("东方财富响应缺少 data")
    diff = data.get("diff")
    if isinstance(diff, dict):
        diff = list(diff.values())
    if not isinstance(diff, list):
        raise EastmoneyDataError("东方财富响应缺少 diff")
    return [row for row in diff if isinstance(row, dict)]


def _float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def parse_market_breadth(payload: dict[str, Any]) -> MarketBreadth:
    rows = _rows(payload)
    if not rows:
        raise EastmoneyDataError("东方财富市场宽度为空")

    def total(field: str) -> int | None:
        values = [_int(row.get(field)) for row in rows]
        usable = [value for value in values if value is not None]
        return sum(usable) if usable else None

    amounts = [_float(row.get("f6")) for row in rows]
    usable_amounts = [amount for amount in amounts if amount is not None]
    return MarketBreadth(
        advancing=total("f104"),
        declining=total("f105"),
        unchanged=total("f106"),
        limit_up=None,
        limit_down=None,
        total_amount_cny=sum(usable_amounts) if usable_amounts else None,
    )


def parse_sector_rows(
    payload: dict[str, Any],
    kind: Literal["industry", "concept"],
) -> list[SectorPerformance]:
    sectors: list[SectorPerformance] = []
    for row in _rows(payload):
        name = row.get("f14")
        change_pct = _float(row.get("f3"))
        if not isinstance(name, str) or change_pct is None:
            continue
        amount = _float(row.get("f6"))
        sectors.append(
            SectorPerformance(
                name=name,
                kind=kind,
                change_pct=change_pct,
                amount_cny=amount,
                heat_score=calculate_sector_heat(change_pct, amount),
            )
        )
    return sectors


class EastmoneyMarketSource:
    provider = "东方财富"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._headers = {
            "Referer": "https://quote.eastmoney.com/center/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon Mac OS X) AppleWebKit/537.36",
        }
        self._request_lock = asyncio.Lock()
        self._cooldown_until = 0.0

    async def _json(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        async with self._request_lock:
            remaining = self._cooldown_until - monotonic()
            if remaining > 0:
                raise EastmoneyDataError(f"东方财富源处于冷却期（约 {remaining:.0f} 秒）")
            try:
                response = await self._client.get(url, params=params, headers=self._headers)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                self._cooldown_until = monotonic() + 300
                raise EastmoneyDataError(f"东方财富请求失败: {exc}") from exc
            if not isinstance(payload, dict) or payload.get("rc") != 0:
                self._cooldown_until = monotonic() + 300
                raise EastmoneyDataError("东方财富返回非成功状态")
            return payload

    async def fetch_breadth(self) -> tuple[MarketBreadth, SourceMeta]:
        params = {
            "fltt": "2",
            "invt": "2",
            "fields": "f2,f3,f6,f12,f14,f104,f105,f106",
            "secids": MARKET_GROUP_SECIDS,
        }
        payload = await self._json(EASTMONEY_GROUP_URL, params)
        breadth = parse_market_breadth(payload)
        fetched_at = iso_now()
        return breadth, SourceMeta(
            provider=self.provider,
            source_url=str(httpx.URL(EASTMONEY_GROUP_URL, params=params)),
            fetched_at=fetched_at,
            source_timestamp=None,
            state="delayed",
            notes=[
                "涨跌家数按上证综指、深证综指、北证50成分口径汇总",
                "该轻量口径可能含少量非A股成分；V1 显式标记，不作为交易规则依据",
                "涨停/跌停家数需要逐证券日规则，未验证前返回空值",
            ],
        )

    async def _fetch_sector_rank(
        self,
        kind: Literal["industry", "concept"],
        descending: bool,
    ) -> list[SectorPerformance]:
        params = {
            "pn": "1",
            "pz": "10",
            "po": "1" if descending else "0",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:90+t:2+f:!50" if kind == "industry" else "m:90+t:3+f:!50",
            "fields": "f3,f6,f12,f14",
        }
        payload = await self._json(EASTMONEY_LIST_URL, params)
        return parse_sector_rows(payload, kind)

    async def fetch_sectors(self) -> tuple[list[SectorPerformance], SourceMeta]:
        tasks = [
            self._fetch_sector_rank("industry", True),
            self._fetch_sector_rank("industry", False),
            self._fetch_sector_rank("concept", True),
            self._fetch_sector_rank("concept", False),
        ]
        ranked = await asyncio.gather(*tasks)
        deduplicated: dict[tuple[str, str], SectorPerformance] = {}
        for group in ranked:
            for sector in group:
                deduplicated[(sector.kind, sector.name)] = sector
        fetched_at = iso_now()
        return list(deduplicated.values()), SourceMeta(
            provider=self.provider,
            source_url=EASTMONEY_LIST_URL,
            fetched_at=fetched_at,
            source_timestamp=None,
            state="delayed",
            notes=["行业与概念分别抓取涨幅/跌幅前10；热度为本地确定性公式，非源站字段"],
        )
