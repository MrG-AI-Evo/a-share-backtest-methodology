from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
from datetime import datetime
from typing import Any, Literal

from app.core.time import SHANGHAI, iso_now
from app.models import AdapterHealth, MarketBreadth, PriceBar, SourceMeta, StockBars
from app.sources.tencent import TencentQuoteError, provider_symbol


class OptionalAdapterUnavailable(RuntimeError):
    pass


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None or bool(getattr(frame, "empty", True)):
        raise OptionalAdapterUnavailable("上游返回空表")
    result = frame.to_dict(orient="records")
    if not isinstance(result, list):
        raise OptionalAdapterUnavailable("上游表格无法转换为 records")
    return [item for item in result if isinstance(item, dict)]


def _float(value: Any) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value)
        parsed = datetime.fromisoformat(raw.replace("/", "-"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.isoformat(timespec="minutes")


class MootdxStockAdapter:
    """Optional mootdx fallback for unadjusted bars; never hides its TDX lineage."""

    adapter_id = "mootdx"

    def __init__(self, module: Any | None = None, *, enabled: bool = True) -> None:
        self._module = module
        self._enabled = enabled

    def health(self) -> AdapterHealth:
        try:
            module = self._module or importlib.import_module("mootdx")
            version = str(getattr(module, "__version__", ""))
            if not version:
                try:
                    version = importlib.metadata.version("mootdx")
                except importlib.metadata.PackageNotFoundError:
                    version = "UNKNOWN"
            installed = True
        except ImportError:
            version = None
            installed = False
        return AdapterHealth(
            adapter_id=self.adapter_id,
            role="PUBLIC_TDX_UNADJUSTED_BARS",
            installed=installed,
            enabled=installed and self._enabled,
            mode="RUNTIME" if installed and self._enabled else "DISABLED",
            version=version,
            upstream_commit="96c3d42",
            notes=[
                "只作为不复权 K 线备援；复权口径未知时拒绝冒充 qfq/hfq",
                "通达信公开服务器并非交易所直连或 Level-2",
                *([] if self._enabled else ["已由本地配置显式关闭"]),
            ],
        )

    def _fetch_sync(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "minute"],
        limit: int,
        minute_interval: int,
    ) -> StockBars:
        try:
            module = self._module or importlib.import_module("mootdx")
            quotes_module = (
                importlib.import_module("mootdx.quotes") if self._module is None else module
            )
            quotes = quotes_module.Quotes
            client = quotes.factory(market="std", multithread=False, heartbeat=False)
        except (ImportError, AttributeError, TypeError) as exc:
            raise OptionalAdapterUnavailable(f"mootdx 未安装或接口不兼容: {exc}") from exc
        frequency: int | str
        if period == "day":
            frequency = 9
        elif period == "week":
            frequency = 5
        elif period == "month":
            frequency = 6
        else:
            frequency = {1: 8, 5: 0, 15: 1, 30: 2, 60: 3}.get(minute_interval, 0)
        try:
            frame = client.bars(symbol=symbol, frequency=frequency, start=0, offset=limit)
            records = _records(frame)
        except Exception as exc:
            raise OptionalAdapterUnavailable(f"mootdx K 线读取失败: {exc}") from exc
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        bars: list[PriceBar] = []
        for row in records:
            opened = _float(row.get("open"))
            high = _float(row.get("high"))
            low = _float(row.get("low"))
            close = _float(row.get("close"))
            volume = _float(row.get("vol") if row.get("vol") is not None else row.get("volume"))
            stamp = row.get("datetime") if row.get("datetime") is not None else row.get("date")
            if None in (opened, high, low, close, volume) or stamp is None:
                continue
            assert opened is not None and high is not None and low is not None
            assert close is not None and volume is not None
            bars.append(
                PriceBar(
                    timestamp=_timestamp(stamp),
                    open=opened,
                    high=high,
                    low=low,
                    close=close,
                    volume_shares=volume * 100,
                    amount_cny=_float(row.get("amount")),
                )
            )
        if not bars:
            raise OptionalAdapterUnavailable("mootdx 没有可用 K 线记录")
        return StockBars(
            symbol=symbol,
            period=period,
            adjustment="none",
            minute_interval=minute_interval if period == "minute" else None,
            bars=bars,
        )

    async def fetch_bars(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "minute"],
        adjustment: Literal["qfq", "hfq", "none"],
        limit: int,
        minute_interval: int,
    ) -> tuple[StockBars, SourceMeta]:
        if not self._enabled:
            raise OptionalAdapterUnavailable("mootdx 已由本地配置关闭")
        provider_symbol(symbol)
        if adjustment != "none" and period != "minute":
            raise OptionalAdapterUnavailable("mootdx 备援只提供已核验的不复权口径")
        result = await asyncio.to_thread(
            self._fetch_sync,
            symbol.split(".", 1)[0],
            period,
            limit,
            minute_interval,
        )
        return result, SourceMeta(
            provider="mootdx / 通达信公开行情",
            source_url="tcp://public-tdx-server:7709",
            fetched_at=iso_now(),
            source_timestamp=result.bars[-1].timestamp,
            state="delayed",
            notes=["公开 TCP 行情备援", "仅不复权；非 Level-2"],
        )


class AkshareSupplementAdapter:
    """Optional AKShare wrapper with explicit underlying-source lineage."""

    adapter_id = "akshare"

    def __init__(self, module: Any | None = None, *, enabled: bool = True) -> None:
        self._module = module
        self._enabled = enabled

    def health(self) -> AdapterHealth:
        try:
            module = self._module or importlib.import_module("akshare")
            version = str(getattr(module, "__version__", ""))
            if not version:
                try:
                    version = importlib.metadata.version("akshare")
                except importlib.metadata.PackageNotFoundError:
                    version = "UNKNOWN"
            installed = True
        except ImportError:
            version = None
            installed = False
        return AdapterHealth(
            adapter_id=self.adapter_id,
            role="UNIFIED_PUBLIC_DATA_SUPPLEMENT",
            installed=installed,
            enabled=installed and self._enabled,
            mode="RUNTIME" if installed and self._enabled else "DISABLED",
            version=version,
            notes=[
                "stock_zh_a_hist 的实际底层为东方财富，不能作为独立二源",
                "只在主源失败时启用；空表和字段漂移均 fail closed",
                *([] if self._enabled else ["已安装但由本地配置关闭，不参与运行时降级"]),
            ],
        )

    def _module_or_raise(self) -> Any:
        if not self._enabled:
            raise OptionalAdapterUnavailable("AKShare 已由本地配置关闭")
        try:
            return self._module or importlib.import_module("akshare")
        except ImportError as exc:
            raise OptionalAdapterUnavailable("AKShare 未安装") from exc

    def _breadth_sync(self) -> MarketBreadth:
        module = self._module_or_raise()
        try:
            rows = _records(module.stock_zh_a_spot_em())
        except Exception as exc:
            raise OptionalAdapterUnavailable(f"AKShare/东方财富全市场快照失败: {exc}") from exc
        changes = [_float(row.get("涨跌幅")) for row in rows]
        usable = [value for value in changes if value is not None]
        amounts = [_float(row.get("成交额")) for row in rows]
        return MarketBreadth(
            advancing=sum(1 for value in usable if value > 0),
            declining=sum(1 for value in usable if value < 0),
            unchanged=sum(1 for value in usable if value == 0),
            limit_up=None,
            limit_down=None,
            total_amount_cny=sum(value for value in amounts if value is not None) or None,
        )

    async def fetch_breadth(self) -> tuple[MarketBreadth, SourceMeta]:
        result = await asyncio.to_thread(self._breadth_sync)
        return result, SourceMeta(
            provider="AKShare / 东方财富",
            source_url="https://push2.eastmoney.com/",
            fetched_at=iso_now(),
            state="delayed",
            notes=[
                "AKShare 统一接口补充；底层仍为东方财富，不计作独立二源",
                "涨跌停家数缺少逐证券当日参考参数，保持空值",
            ],
        )

    def _bars_sync(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "minute"],
        adjustment: Literal["qfq", "hfq", "none"],
        limit: int,
    ) -> StockBars:
        if period == "minute":
            raise OptionalAdapterUnavailable("AKShare 分钟接口未纳入 V1 稳定契约")
        module = self._module_or_raise()
        adjust = "" if adjustment == "none" else adjustment
        period_name = {"day": "daily", "week": "weekly", "month": "monthly"}[period]
        try:
            frame = module.stock_zh_a_hist(
                symbol=symbol,
                period=period_name,
                start_date="19900101",
                end_date="22220101",
                adjust=adjust,
            )
            rows = _records(frame)[-limit:]
        except Exception as exc:
            raise OptionalAdapterUnavailable(f"AKShare/东方财富 K 线失败: {exc}") from exc
        bars: list[PriceBar] = []
        for row in rows:
            opened = _float(row.get("开盘"))
            high = _float(row.get("最高"))
            low = _float(row.get("最低"))
            close = _float(row.get("收盘"))
            volume = _float(row.get("成交量"))
            if None in (opened, high, low, close, volume) or row.get("日期") is None:
                continue
            assert opened is not None and high is not None and low is not None
            assert close is not None and volume is not None
            bars.append(
                PriceBar(
                    timestamp=_timestamp(row["日期"]),
                    open=opened,
                    high=high,
                    low=low,
                    close=close,
                    volume_shares=volume * 100,
                    amount_cny=_float(row.get("成交额")),
                )
            )
        if not bars:
            raise OptionalAdapterUnavailable("AKShare/东方财富没有可解析 K 线")
        return StockBars(
            symbol=symbol,
            period=period,
            adjustment=adjustment,
            bars=bars,
        )

    async def fetch_bars(
        self,
        symbol: str,
        period: Literal["day", "week", "month", "minute"],
        adjustment: Literal["qfq", "hfq", "none"],
        limit: int,
        minute_interval: int,
    ) -> tuple[StockBars, SourceMeta]:
        del minute_interval
        try:
            provider_symbol(symbol)
        except TencentQuoteError as exc:
            raise OptionalAdapterUnavailable(str(exc)) from exc
        result = await asyncio.to_thread(
            self._bars_sync,
            symbol.split(".", 1)[0],
            period,
            adjustment,
            limit,
        )
        return result, SourceMeta(
            provider="AKShare / 东方财富",
            source_url="https://push2his.eastmoney.com/",
            fetched_at=iso_now(),
            source_timestamp=result.bars[-1].timestamp,
            state="delayed",
            notes=["AKShare 统一接口补充；已显式保留东方财富血缘"],
        )
