from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import duckdb

from app.backtests.runner import (
    CorporateActionDay,
    FeeRule,
    SecurityDay,
    TradingDayInput,
)
from app.backtests.signals import (
    CashDividendObservation,
    CDRateObservation,
    FiscalYearDividendResolution,
    aggregate_cd_rate,
    conservative_dividend,
)


class HistoricalFrameError(RuntimeError):
    pass


@dataclass(frozen=True)
class HistoricalFrameSet:
    days: list[TradingDayInput]
    minimum_trade_date: str
    maximum_trade_date: str
    security_day_count: int
    evidence_source_ids: tuple[str, ...]


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _decimal(value: object | None) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


class HistoricalFrameBuilder:
    """Build deterministic daily inputs exclusively from imported point-in-time tables."""

    def __init__(self, database: Path) -> None:
        self.database = database

    def build(
        self,
        start_date: str,
        end_date: str,
        excluded_symbols: frozenset[str] = frozenset(),
    ) -> HistoricalFrameSet:
        with duckdb.connect(str(self.database), read_only=True) as db:
            exclusion_sql = ""
            query_parameters: list[object] = [start_date, end_date]
            if excluded_symbols:
                placeholders = ", ".join("?" for _ in excluded_symbols)
                exclusion_sql = f" AND symbol NOT IN ({placeholders})"
                query_parameters.extend(sorted(excluded_symbols))
            prices = db.execute(
                f"""
                SELECT symbol, exchange, effective_date::VARCHAR, known_at::VARCHAR,
                       open, close, amount, source_id,
                       median(amount) OVER (
                           PARTITION BY symbol, exchange ORDER BY effective_date
                           ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                       ) AS amount_med20
                FROM bt_prices
                WHERE effective_date BETWEEN ?::DATE AND ?::DATE
                  AND is_synthetic = false
                  {exclusion_sql}
                ORDER BY effective_date, symbol, exchange
                """,
                query_parameters,
            ).fetchall()
            if not prices:
                raise HistoricalFrameError("主窗口没有真实未复权行情，禁止执行回测")
            masters = self._latest_rows(
                db,
                "bt_security_master",
                "symbol, exchange, effective_date::VARCHAR, known_at::VARCHAR, name, asset_type, "
                "ownership_type, listing_date::VARCHAR, delisting_date::VARCHAR, source_id",
                end_date,
            )
            statuses = self._latest_rows(
                db,
                "bt_security_status",
                "symbol, exchange, effective_date::VARCHAR, known_at::VARCHAR, risk_status, "
                "trading_status, source_id",
                end_date,
            )
            quality = self._quality_rows(db, end_date)
            cash_dividends, fiscal_years = self._dividend_rows(db, end_date)
            cd_rates = self._cd_rows(db, end_date)
            actions = self._action_rows(db, start_date, end_date)
            fee_rows = self._fee_rows(db, end_date)

        master_by_key = self._group(masters)
        status_by_key = self._group(statuses)
        quality_by_key = self._group(quality)
        prices_by_date: dict[str, list[tuple[object, ...]]] = defaultdict(list)
        sources: set[str] = set()
        for row in prices:
            prices_by_date[str(row[2])].append(row)
            sources.add(str(row[7]))

        ordered_dates = sorted(prices_by_date)
        first_by_month: set[str] = set()
        seen_months: set[str] = set()
        for trade_date in ordered_dates:
            month = trade_date[:7]
            if month not in seen_months:
                first_by_month.add(trade_date)
                seen_months.add(month)

        days: list[TradingDayInput] = []
        for trade_date in ordered_dates:
            signal_at = f"{trade_date}T15:30:00+08:00"
            securities: list[SecurityDay] = []
            for row in prices_by_date[trade_date]:
                symbol, exchange = str(row[0]), str(row[1])
                if _instant(row[3]) > datetime.fromisoformat(signal_at):
                    raise HistoricalFrameError(f"{trade_date} {symbol} 行情 known_at 晚于信号时点")
                key = (symbol, exchange)
                master = self._as_of(master_by_key.get(key, []), trade_date, signal_at)
                status = self._as_of(status_by_key.get(key, []), trade_date, signal_at)
                quality_row = self._as_of(quality_by_key.get(key, []), trade_date, signal_at)
                dividend = conservative_dividend(
                    signal_at=signal_at,
                    cash_events=cash_dividends.get(key, []),
                    fiscal_years=fiscal_years.get(key, []),
                )
                cd = aggregate_cd_rate(signal_at, cd_rates)
                master_ok = master is not None and str(master[5]) == "ORDINARY_A_SHARE"
                risk_ok = bool(
                    status is not None
                    and str(status[4])
                    not in {
                        "ST",
                        "STAR_ST",
                        "DELISTING_PERIOD",
                        "SUSPENDED_FROM_LISTING",
                        "UNSUPPORTED_ASSET",
                    }
                    and str(status[5]) == "TRADABLE"
                )
                quality_pass = (
                    bool(Decimal(str(quality_row[6])))
                    if master_ok and status is not None and quality_row is not None
                    else None
                )
                evidence = {str(row[7])}
                for candidate, index in ((master, 8), (status, 6), (quality_row, 8)):
                    if candidate is not None:
                        evidence.add(str(candidate[index]))
                securities.append(
                    SecurityDay(
                        symbol=symbol,
                        name=str(master[4]) if master is not None else symbol,
                        exchange=exchange,
                        ownership_type=str(master[6]) if master is not None else "UNKNOWN",
                        open_price=_decimal(row[4]),
                        close_price=_decimal(row[5]),
                        tradable_open=risk_ok and row[4] is not None,
                        tradable_close=risk_ok and row[5] is not None,
                        risk_status_eligible=risk_ok,
                        quality_pass=quality_pass,
                        d_ttm=dividend.d_ttm if dividend else None,
                        d_med3=dividend.d_med3 if dividend else None,
                        cd_rate=cd.annual_rate if cd else None,
                        cd_tenor_years=cd.tenor_years if cd else None,
                        cd_rate_quality=cd.quality if cd else "UNAVAILABLE",
                        median_amount_20d_cny=_decimal(row[8]),
                        evidence_ids=tuple(sorted(evidence)),
                    )
                )
                sources.update(evidence)
            venues = {security.exchange for security in securities}
            venue_fees: dict[str, FeeRule] = {}
            buy_slippage: Decimal | None = None
            sell_slippage: Decimal | None = None
            fee_sources: set[str] = set()
            for venue in venues:
                rule, buy_bps, sell_bps, venue_sources = self._fee_as_of(
                    fee_rows, trade_date, signal_at, venue
                )
                venue_fees[venue] = rule
                fee_sources.update(venue_sources)
                if buy_slippage is None:
                    buy_slippage, sell_slippage = buy_bps, sell_bps
                elif buy_slippage != buy_bps or sell_slippage != sell_bps:
                    raise HistoricalFrameError(
                        f"{trade_date} 各交易所滑点档案不一致；当前日级输入无法无损表达"
                    )
            if not venue_fees or buy_slippage is None or sell_slippage is None:
                raise HistoricalFrameError(f"{trade_date} 缺少费用与滑点规则")
            fee_rule = next(iter(venue_fees.values()))
            sources.update(fee_sources)
            days.append(
                TradingDayInput(
                    trade_date=trade_date,
                    is_first_trading_day_of_month=trade_date in first_by_month,
                    securities=tuple(securities),
                    fee_rule=fee_rule,
                    buy_slippage_bps=buy_slippage,
                    sell_slippage_bps=sell_slippage,
                    corporate_actions=tuple(actions.get(trade_date, [])),
                    fee_rules_by_exchange=venue_fees,
                )
            )
        return HistoricalFrameSet(
            days=days,
            minimum_trade_date=ordered_dates[0],
            maximum_trade_date=ordered_dates[-1],
            security_day_count=sum(len(day.securities) for day in days),
            evidence_source_ids=tuple(sorted(sources)),
        )

    @staticmethod
    def _latest_rows(
        db: duckdb.DuckDBPyConnection, table: str, columns: str, end_date: str
    ) -> list[tuple[object, ...]]:
        return cast(
            list[tuple[object, ...]],
            db.execute(
                f"SELECT {columns} FROM {table} "
                "WHERE effective_date <= ?::DATE AND is_synthetic = false "
                "ORDER BY symbol, exchange, effective_date, known_at",
                [end_date],
            ).fetchall(),
        )

    @staticmethod
    def _quality_rows(db: duckdb.DuckDBPyConnection, end_date: str) -> list[tuple[object, ...]]:
        return cast(
            list[tuple[object, ...]],
            db.execute(
                """
                SELECT symbol, exchange, effective_date::VARCHAR, known_at::VARCHAR,
                       profile, metric, value, unit, source_id
                FROM bt_financial_facts
                WHERE effective_date <= ?::DATE AND metric = 'QUALITY_GATE_PASS'
                  AND is_synthetic = false
                ORDER BY symbol, exchange, effective_date, known_at
                """,
                [end_date],
            ).fetchall(),
        )

    @staticmethod
    def _group(
        rows: list[tuple[object, ...]],
    ) -> dict[tuple[str, str], list[tuple[object, ...]]]:
        grouped: dict[tuple[str, str], list[tuple[object, ...]]] = defaultdict(list)
        for row in rows:
            grouped[(str(row[0]), str(row[1]))].append(row)
        return grouped

    @staticmethod
    def _as_of(
        rows: list[tuple[object, ...]], trade_date: str, signal_at: str
    ) -> tuple[object, ...] | None:
        result: tuple[object, ...] | None = None
        for row in rows:
            if str(row[2]) <= trade_date and _instant(row[3]) <= datetime.fromisoformat(signal_at):
                result = row
            elif str(row[2]) > trade_date:
                break
        return result

    @staticmethod
    def _dividend_rows(
        db: duckdb.DuckDBPyConnection, end_date: str
    ) -> tuple[
        dict[tuple[str, str], list[CashDividendObservation]],
        dict[tuple[str, str], list[FiscalYearDividendResolution]],
    ]:
        events: dict[tuple[str, str], list[CashDividendObservation]] = defaultdict(list)
        for row in db.execute(
            """
            SELECT symbol, exchange, fiscal_year, cash_per_share, known_at::VARCHAR,
                   effective_date::VARCHAR, dividend_kind
            FROM bt_dividends
            WHERE effective_date <= ?::DATE AND is_synthetic = false
            """,
            [end_date],
        ).fetchall():
            events[(str(row[0]), str(row[1]))].append(
                CashDividendObservation(
                    fiscal_year=int(row[2]),
                    cash_per_share=Decimal(str(row[3])),
                    known_at=_iso(row[4]),
                    ex_date=date.fromisoformat(str(row[5])),
                    dividend_kind=cast(Any, str(row[6])),
                )
            )
        years: dict[tuple[str, str], list[FiscalYearDividendResolution]] = defaultdict(list)
        for row in db.execute(
            """
            SELECT symbol, exchange, fiscal_year, total_cash_per_share, known_at::VARCHAR
            FROM bt_dividend_fiscal_years
            WHERE effective_date <= ?::DATE AND fully_resolved = true
              AND is_synthetic = false
            """,
            [end_date],
        ).fetchall():
            years[(str(row[0]), str(row[1]))].append(
                FiscalYearDividendResolution(
                    fiscal_year=int(row[2]),
                    total_cash_per_share=Decimal(str(row[3])),
                    resolved_known_at=_iso(row[4]),
                )
            )
        return events, years

    @staticmethod
    def _cd_rows(db: duckdb.DuckDBPyConnection, end_date: str) -> list[CDRateObservation]:
        result: list[CDRateObservation] = []
        for row in db.execute(
            """
            SELECT bank, tenor_years, annual_rate, known_at::VARCHAR,
                   effective_date::VARCHAR, effective_end_date::VARCHAR
            FROM bt_cd_rates
            WHERE effective_date <= ?::DATE AND effective_end_date IS NOT NULL
              AND is_synthetic = false
            """,
            [end_date],
        ).fetchall():
            result.append(
                CDRateObservation(
                    bank=cast(Any, str(row[0])),
                    tenor_years=cast(Any, int(row[1])),
                    annual_rate=Decimal(str(row[2])),
                    known_at=_iso(row[3]),
                    effective_start=date.fromisoformat(str(row[4])),
                    effective_end=date.fromisoformat(str(row[5])),
                    synthetic=False,
                )
            )
        return result

    @staticmethod
    def _action_rows(
        db: duckdb.DuckDBPyConnection, start_date: str, end_date: str
    ) -> dict[str, list[CorporateActionDay]]:
        result: dict[str, list[CorporateActionDay]] = defaultdict(list)
        rows = db.execute(
            """
            SELECT symbol, effective_date::VARCHAR, action_type, known_at::VARCHAR,
                   share_multiplier, cash_per_share, terms_json
            FROM bt_corporate_actions
            WHERE effective_date BETWEEN ?::DATE AND ?::DATE
              AND is_synthetic = false
            ORDER BY effective_date, symbol
            """,
            [start_date, end_date],
        ).fetchall()
        allowed = {
            "DIVIDEND_RECEIVABLE",
            "DIVIDEND_PAYMENT",
            "DIVIDEND_TAX",
            "SHARE_MULTIPLIER",
            "VOLUNTARY_RIGHTS_SKIPPED",
            "TERMINAL_RECOVERY",
        }
        for row in rows:
            action_type = str(row[2])
            if action_type not in allowed:
                raise HistoricalFrameError(f"不支持的公司行动类型: {action_type}")
            terms = json.loads(str(row[6]))
            result[str(row[1])].append(
                CorporateActionDay(
                    symbol=str(row[0]),
                    action_type=cast(Any, action_type),
                    known_at=_iso(row[3]),
                    cash_per_share=_decimal(row[5]),
                    cash_amount=_decimal(terms.get("cash_amount_cny")),
                    share_multiplier=_decimal(row[4]),
                    unresolved=bool(terms.get("unresolved", False)),
                    evidence_ids=tuple(str(value) for value in terms.get("evidence_ids", [])),
                )
            )
        return result

    @staticmethod
    def _fee_rows(db: duckdb.DuckDBPyConnection, end_date: str) -> list[tuple[object, ...]]:
        return cast(
            list[tuple[object, ...]],
            db.execute(
                """
                SELECT venue, side, effective_date::VARCHAR, known_at::VARCHAR,
                       effective_end_date::VARCHAR, component, rate, minimum_cny,
                       rule_json, source_id
                FROM bt_fee_rules
                WHERE effective_date <= ?::DATE AND is_synthetic = false
                ORDER BY effective_date, known_at
                """,
                [end_date],
            ).fetchall(),
        )

    @staticmethod
    def _fee_as_of(
        rows: list[tuple[object, ...]], trade_date: str, signal_at: str, venue: str
    ) -> tuple[FeeRule, Decimal, Decimal, set[str]]:
        active = [
            row
            for row in rows
            if str(row[0]) in {venue, "ALL"}
            and str(row[2]) <= trade_date
            and (row[4] is None or trade_date <= str(row[4]))
            and _instant(row[3]) <= datetime.fromisoformat(signal_at)
        ]
        latest: dict[tuple[str, str], tuple[object, ...]] = {}
        for row in active:
            latest[(str(row[1]), str(row[5]))] = row

        def rate(side: str, component: str, *, required: bool = True) -> Decimal:
            row = latest.get((side, component)) or latest.get(("BOTH", component))
            if row is None or row[6] is None:
                if required:
                    raise HistoricalFrameError(f"{trade_date} 缺少有效费用规则 {side}/{component}")
                return Decimal("0")
            return Decimal(str(row[6]))

        commission_buy = rate("BUY", "BROKER_COMMISSION")
        commission_sell = rate("SELL", "BROKER_COMMISSION")
        if commission_buy != commission_sell:
            raise HistoricalFrameError("当前执行器要求买卖佣金率一致；费用档案存在冲突")
        commission_row = latest.get(("BUY", "BROKER_COMMISSION")) or latest.get(
            ("BOTH", "BROKER_COMMISSION")
        )
        assert commission_row is not None
        minimum = _decimal(commission_row[7])
        if minimum is None:
            raise HistoricalFrameError(f"{trade_date} 缺少最低佣金")
        rule_json = json.loads(str(commission_row[8]))
        sources = {str(row[9]) for row in latest.values()}
        return (
            FeeRule(
                commission_rate=commission_buy,
                minimum_commission_cny=minimum,
                stamp_duty_sell_rate=rate("SELL", "STAMP_DUTY"),
                transfer_fee_rate=rate("BOTH", "TRANSFER_FEE"),
                exchange_and_regulatory_rate=rate(
                    "BOTH", "EXCHANGE_AND_REGULATORY_FEES", required=False
                ),
            ),
            Decimal(str(rule_json["buy_slippage_bps"])),
            Decimal(str(rule_json["sell_slippage_bps"])),
            sources,
        )
