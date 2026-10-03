from __future__ import annotations

import asyncio
import json
from typing import Any, Literal

import httpx

from app.core.time import iso_now
from app.models import SectorPerformance, SourceMeta
from app.services.market_metrics import calculate_sector_heat

SINA_SECTOR_URL = "https://money.finance.sina.com.cn/q/view/newFLJK.php"


class SinaDataError(RuntimeError):
    pass


def _optional_float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_sector_text(
    text: str,
    kind: Literal["industry", "concept"],
) -> list[SectorPerformance]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise SinaDataError("新浪板块响应格式异常")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SinaDataError("新浪板块响应不是有效 JSONP") from exc
    if not isinstance(payload, dict):
        raise SinaDataError("新浪板块响应缺少板块字典")

    sectors: list[SectorPerformance] = []
    for raw in payload.values():
        if not isinstance(raw, str):
            continue
        fields = raw.split(",")
        if len(fields) < 9:
            continue
        name = fields[1].strip()
        change_pct = _optional_float(fields[5])
        amount_cny = _optional_float(fields[8])
        if not name or change_pct is None:
            continue
        sectors.append(
            SectorPerformance(
                name=name,
                kind=kind,
                change_pct=change_pct,
                amount_cny=amount_cny,
                heat_score=calculate_sector_heat(change_pct, amount_cny),
            )
        )
    if not sectors:
        raise SinaDataError("新浪板块响应没有可用记录")
    return sectors


class SinaMarketSource:
    provider = "新浪财经"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._headers = {
            "Referer": "https://finance.sina.com.cn/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Apple Silicon Mac OS X) AppleWebKit/537.36",
        }

    async def _fetch_kind(
        self,
        kind: Literal["industry", "concept"],
    ) -> list[SectorPerformance]:
        param = "industry" if kind == "industry" else "class"
        try:
            response = await self._client.get(
                SINA_SECTOR_URL,
                params={"param": param},
                headers=self._headers,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SinaDataError(f"新浪财经请求失败: {exc}") from exc
        response.encoding = "gb18030"
        return parse_sector_text(response.text, kind)

    async def fetch_sectors(self) -> tuple[list[SectorPerformance], SourceMeta]:
        industry, concept = await asyncio.gather(
            self._fetch_kind("industry"),
            self._fetch_kind("concept"),
        )
        ranked: list[SectorPerformance] = []
        for group in (industry, concept):
            sorted_group = sorted(group, key=lambda item: item.change_pct, reverse=True)
            ranked.extend(sorted_group[:10])
            ranked.extend(sorted_group[-10:])
        deduplicated = {(sector.kind, sector.name): sector for sector in ranked}
        return list(deduplicated.values()), SourceMeta(
            provider=self.provider,
            source_url=SINA_SECTOR_URL,
            fetched_at=iso_now(),
            source_timestamp=None,
            state="delayed",
            notes=["东方财富不可用时的板块降级源", "热度为本地确定性公式，非源站字段"],
        )
