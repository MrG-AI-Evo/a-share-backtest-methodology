from typing import Any

import pytest

from app.adapters.public_data import (
    AkshareSupplementAdapter,
    MootdxStockAdapter,
    OptionalAdapterUnavailable,
)


class FakeFrame:
    empty = False

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def to_dict(self, orient: str) -> list[dict[str, Any]]:
        assert orient == "records"
        return self._rows


class FakeMootdxClient:
    def bars(self, **_: object) -> FakeFrame:
        return FakeFrame(
            [
                {
                    "datetime": "2026-08-25 15:00:00",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 1234,
                    "amount": 100000,
                }
            ]
        )

    def close(self) -> None:
        return None


class FakeQuotes:
    @staticmethod
    def factory(**_: object) -> FakeMootdxClient:
        return FakeMootdxClient()


class FakeMootdx:
    __version__ = "test"
    Quotes = FakeQuotes


class FakeAkshare:
    __version__ = "test"

    @staticmethod
    def stock_zh_a_spot_em() -> FakeFrame:
        return FakeFrame(
            [
                {"涨跌幅": 1.2, "成交额": 100},
                {"涨跌幅": -0.5, "成交额": 200},
                {"涨跌幅": 0, "成交额": 300},
            ]
        )

    @staticmethod
    def stock_zh_a_hist(**_: object) -> FakeFrame:
        return FakeFrame(
            [
                {
                    "日期": "2026-08-25",
                    "开盘": 10,
                    "最高": 11,
                    "最低": 9,
                    "收盘": 10.5,
                    "成交量": 1234,
                    "成交额": 100000,
                }
            ]
        )


@pytest.mark.asyncio
async def test_mootdx_adapter_is_unadjusted_only_and_preserves_lineage() -> None:
    adapter = MootdxStockAdapter(FakeMootdx())
    result, source = await adapter.fetch_bars("600000", "day", "none", 20, 5)
    assert result.bars[0].volume_shares == 123_400
    assert source.provider == "mootdx / 通达信公开行情"
    with pytest.raises(OptionalAdapterUnavailable, match="只提供已核验的不复权"):
        await adapter.fetch_bars("600000", "day", "qfq", 20, 5)


@pytest.mark.asyncio
async def test_akshare_adapter_exposes_eastmoney_lineage() -> None:
    adapter = AkshareSupplementAdapter(FakeAkshare())
    breadth, breadth_source = await adapter.fetch_breadth()
    assert (breadth.advancing, breadth.declining, breadth.unchanged) == (1, 1, 1)
    assert "东方财富" in breadth_source.provider
    bars, bar_source = await adapter.fetch_bars("600000", "day", "qfq", 20, 5)
    assert bars.adjustment == "qfq"
    assert "东方财富" in bar_source.provider


@pytest.mark.asyncio
async def test_installed_adapter_can_be_configured_disabled() -> None:
    adapter = AkshareSupplementAdapter(FakeAkshare(), enabled=False)
    health = adapter.health()
    assert health.installed is True
    assert health.enabled is False
    assert health.mode == "DISABLED"
    with pytest.raises(OptionalAdapterUnavailable, match="配置关闭"):
        await adapter.fetch_breadth()
