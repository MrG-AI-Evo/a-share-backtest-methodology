from __future__ import annotations

import asyncio
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import uuid4

from app.core.policy import PaperPolicy, load_policy, load_ruleset_meta
from app.core.settings import Settings
from app.core.time import SHANGHAI, iso_now, now_shanghai
from app.core.trading_calendar import CalendarCoverageError, load_trading_calendar
from app.models import (
    PaperAccountDetail,
    PaperFill,
    PaperOrder,
    PaperOrderCreate,
    PaperPosition,
    PaperSummary,
    RiskCheck,
    SourceMeta,
    StockQuote,
)
from app.repositories.paper import PaperLedgerError, PaperRepository
from app.repositories.research import ResearchRepository
from app.services.research_evaluation import evaluate_research_card
from app.services.stock import StockService, StockServiceError


class PaperRuleError(RuntimeError):
    pass


def _money(value: Decimal | float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _lot_rule(symbol: str) -> tuple[int, int, str]:
    if symbol.startswith(("4", "8", "92")):
        return 100, 1, "BSE"
    if symbol.startswith(("688", "689")):
        return 200, 1, "SSE_STAR"
    if symbol.startswith("3"):
        return 100, 100, "SZSE_CHINEXT"
    if symbol.startswith(("5", "6", "9")):
        return 100, 100, "SSE_MAIN"
    return 100, 100, "SZSE_MAIN"


def _is_regular_session(moment: datetime) -> bool:
    if moment.weekday() >= 5:
        return False
    current = moment.timetz().replace(tzinfo=None)
    return time(9, 30) <= current <= time(11, 30) or time(13, 0) <= current <= time(15, 0)


class PaperService:
    def __init__(
        self,
        settings: Settings,
        repository: PaperRepository,
        research: ResearchRepository,
        stock: StockService,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._research = research
        self._stock = stock
        self._policy = load_policy(settings)
        self._ruleset_version, self._rules_status = load_ruleset_meta(settings)
        self._calendar = load_trading_calendar(settings)

    @property
    def policy(self) -> PaperPolicy:
        return self._policy

    def _buy_research_gate(
        self,
        symbol: str,
        research_version: str | None,
    ) -> tuple[bool, str, list[str]]:
        card, warnings = self._research.latest_for(symbol)
        expected_path = str(card.get("_file_path")) if card else None
        try:
            valid_until = datetime.fromisoformat(str(card["valid_until"])) if card else None
            not_expired = valid_until is not None and valid_until > now_shanghai()
        except (KeyError, TypeError, ValueError):
            not_expired = False
        actionable_status = card is not None and str(card.get("status")) in {"重点观察", "观察"}
        quality_passed = card is not None and evaluate_research_card(card).passed
        version_is_latest = research_version == expected_path
        passed = actionable_status and quality_passed and not_expired and version_is_latest
        if passed:
            return True, f"绑定最新研究 {expected_path}", warnings
        return (
            False,
            "买入必须绑定通过研究质量门、最新、未过期且状态为‘重点观察/观察’的正式研究卡",
            warnings,
        )

    async def _buy_portfolio_checks(
        self,
        symbol: str,
        planned_quantity: int,
        risk_quantity: int,
        price: float,
        invalidation_price: float | None,
    ) -> list[RiskCheck]:
        summary, valuation_warnings, _ = await self.refresh_valuation()
        positions = await self._repository.positions()
        current = next((position for position in positions if position.symbol == symbol), None)
        planned_value = planned_quantity * price
        checks: list[RiskCheck] = [
            RiskCheck(
                check="valuation_freshness",
                status="FAIL" if valuation_warnings else "PASS",
                detail=(
                    "；".join(valuation_warnings[:2])
                    if valuation_warnings
                    else "全部现有持仓已按可追溯公开行情刷新估值"
                ),
            )
        ]

        invalidation_ok = invalidation_price is not None and invalidation_price < price
        checks.append(
            RiskCheck(
                check="invalidation_price",
                status="PASS" if invalidation_ok else "FAIL",
                detail="买入必须提供低于当前计划价的可观察失效价",
            )
        )
        if invalidation_ok and invalidation_price is not None:
            risk_budget = summary.total_assets * (self._policy.risk.risk_per_trade_pct_nav / 100)
            risk_shares = int(risk_budget / (price - invalidation_price))
            checks.append(
                RiskCheck(
                    check="risk_budget",
                    status="PASS" if risk_quantity <= risk_shares else "FAIL",
                    detail=(
                        f"{self._policy.risk.risk_per_trade_pct_nav:.2f}%净值风险预算"
                        f"最多约 {risk_shares} 股"
                    ),
                )
            )

        current_value = current.market_value_cny if current else 0.0
        position_limit_pct = (
            self._policy.risk.single_stock_hard_max_pct_nav
            if current
            else self._policy.risk.initial_position_max_pct_nav
        )
        position_ok = current_value + planned_value <= summary.total_assets * (
            position_limit_pct / 100
        )
        checks.append(
            RiskCheck(
                check="position_limit",
                status="PASS" if position_ok else "FAIL",
                detail=f"本次后标的市值不得超过净值 {position_limit_pct:.0f}%",
            )
        )

        gross_limit_pct = (
            self._policy.risk.risk_off_exposure_max_pct_nav
            if summary.max_drawdown_pct >= self._policy.risk.risk_off_drawdown_pct
            else self._policy.risk.gross_exposure_max_pct_nav
        )
        gross_ok = summary.market_value + planned_value <= summary.total_assets * (
            gross_limit_pct / 100
        )
        checks.append(
            RiskCheck(
                check="gross_exposure_limit",
                status="PASS" if gross_ok else "FAIL",
                detail=f"本次后总仓位不得超过净值 {gross_limit_pct:.0f}%",
            )
        )

        projected_values = [
            (
                position.market_value_cny + planned_value
                if position.symbol == symbol
                else position.market_value_cny
            )
            for position in positions
        ]
        if current is None:
            projected_values.append(planned_value)
        top5_value = sum(sorted(projected_values, reverse=True)[:5])
        top5_limit_pct = self._policy.risk.top5_max_pct_nav
        checks.append(
            RiskCheck(
                check="top5_concentration_limit",
                status=(
                    "PASS"
                    if top5_value <= summary.total_assets * (top5_limit_pct / 100)
                    else "FAIL"
                ),
                detail=f"本次后 Top 5 持仓合计不得超过净值 {top5_limit_pct:.0f}%",
            )
        )

        estimated_fees = self._estimate_fees("BUY", planned_value)
        checks.append(
            RiskCheck(
                check="cash_and_fees",
                status=("PASS" if planned_value + estimated_fees <= summary.cash else "FAIL"),
                detail=f"计划占用约 ¥{planned_value + estimated_fees:,.2f}",
            )
        )

        daily_base = summary.total_assets - summary.daily_pnl
        daily_pnl_pct = summary.daily_pnl / daily_base * 100 if daily_base > 0 else -100.0
        daily_limit = self._policy.risk.stop_new_buys_daily_loss_pct
        checks.append(
            RiskCheck(
                check="daily_loss_stop",
                status="PASS" if daily_pnl_pct > -daily_limit else "FAIL",
                detail=(f"当日损益 {daily_pnl_pct:.2f}%；跌至 -{daily_limit:.2f}% 时停止新买入"),
            )
        )

        drawdown_limit = self._policy.risk.freeze_drawdown_pct
        checks.append(
            RiskCheck(
                check="drawdown_freeze",
                status=("PASS" if summary.max_drawdown_pct < drawdown_limit else "FAIL"),
                detail=(
                    f"当前最大回撤 {summary.max_drawdown_pct:.2f}%；"
                    f"达到 {drawdown_limit:.2f}% 时冻结新买入"
                ),
            )
        )

        opened_today = await self._repository.new_positions_opened_on(now_shanghai().date())
        new_position_ok = current is not None or (
            opened_today < self._policy.risk.max_new_positions_per_day
        )
        checks.append(
            RiskCheck(
                check="new_positions_per_day",
                status="PASS" if new_position_ok else "FAIL",
                detail=(
                    f"当日已新开 {opened_today} 只；上限 "
                    f"{self._policy.risk.max_new_positions_per_day} 只"
                ),
            )
        )
        return checks

    async def refresh_valuation(
        self,
    ) -> tuple[PaperSummary, list[str], list[SourceMeta]]:
        positions = await self._repository.positions()
        if not positions:
            return await self._repository.summary(), [], []
        warnings: list[str] = []
        sources: list[SourceMeta] = []
        prices: dict[str, tuple[float, str]] = {}
        semaphore = asyncio.Semaphore(4)

        async def fetch(
            position: PaperPosition,
        ) -> tuple[PaperPosition, StockQuote | None, SourceMeta | None, str | None]:
            async with semaphore:
                try:
                    quote, source = await self._stock.quote(position.symbol)
                    quote_time = datetime.fromisoformat(quote.updated_at)
                    position_time = datetime.fromisoformat(position.updated_at)
                    if quote_time.tzinfo is None or position_time.tzinfo is None:
                        raise ValueError("估值时间必须包含时区")
                    quote_time = quote_time.astimezone(SHANGHAI)
                    position_time = position_time.astimezone(SHANGHAI)
                    if quote_time < position_time:
                        return position, None, None, "公开行情早于账本现价，保留旧估值"
                    if source.state in {"stale", "unavailable", "demo"}:
                        return position, None, source, f"公开行情状态为 {source.state}"
                    return position, quote, source, None
                except (StockServiceError, TypeError, ValueError) as exc:
                    return position, None, None, f"估值刷新失败：{exc}"

        for position, quote, source, warning in await asyncio.gather(
            *(fetch(position) for position in positions)
        ):
            if warning:
                warnings.append(f"{position.symbol} {warning}")
                continue
            if quote is None or source is None:
                warnings.append(f"{position.symbol} 估值刷新未返回完整结果")
                continue
            prices[position.symbol] = (quote.price, quote.updated_at)
            sources.append(source)
        if len(prices) != len(positions):
            missing = sorted({item.symbol for item in positions} - set(prices))
            if missing:
                warnings.append(f"持仓估值不完整：{', '.join(missing)}")
        as_of = now_shanghai().replace(second=0, microsecond=0).isoformat(timespec="seconds")
        if prices:
            summary = await self._repository.mark_to_market(prices, as_of)
        else:
            summary = await self._repository.summary()
        unique_sources = list(
            {
                (source.provider, source.source_timestamp, source.state): source
                for source in sources
            }.values()
        )
        return summary, list(dict.fromkeys(warnings)), unique_sources

    async def _assert_buy_order_gates(self, order: PaperOrder, stage: str) -> None:
        research_passed, research_detail, _ = self._buy_research_gate(
            order.symbol,
            order.research_version,
        )
        if not research_passed:
            raise PaperRuleError(f"{stage}研究门复核失败：{research_detail}")
        remaining_quantity = order.quantity - order.filled_quantity
        checks = await self._buy_portfolio_checks(
            order.symbol,
            remaining_quantity,
            order.quantity,
            order.limit_price,
            order.invalidation_price,
        )
        failures = [check.detail for check in checks if check.status == "FAIL"]
        if failures:
            raise PaperRuleError(f"{stage}组合风控复核失败：{'；'.join(failures)}")

    async def detail(self) -> tuple[PaperAccountDetail, list[str], list[SourceMeta]]:
        summary, warnings, sources = await self.refresh_valuation()
        return (
            PaperAccountDetail(
                summary=summary,
                positions=await self._repository.positions(),
                orders=await self._repository.orders(),
                nav=await self._repository.nav(),
                policy_version=self._policy.version,
                ruleset_version=self._ruleset_version,
                fee_schedule_version=self._policy.fees.version,
                fee_notes=self._policy.fees.notes,
            ),
            warnings,
            sources,
        )

    async def propose(self, request: PaperOrderCreate, idempotency_key: str) -> PaperOrder:
        checks: list[RiskCheck] = []
        minimum, increment, venue = _lot_rule(request.symbol)
        quantity_valid = (
            request.quantity >= minimum and (request.quantity - minimum) % increment == 0
        )
        checks.append(
            RiskCheck(
                check="quantity_lot",
                status="PASS" if quantity_valid else "FAIL",
                detail=f"{venue} 最低 {minimum} 股、之后按 {increment} 股递增",
            )
        )
        try:
            earliest_fill_at = self._calendar.next_open(now_shanghai()).isoformat(
                timespec="seconds"
            )
            calendar_status: Literal["PASS", "FAIL", "REVIEW"] = "PASS"
            calendar_detail = f"交易日历 {self._calendar.version} 已覆盖下一可交易日"
        except CalendarCoverageError as exc:
            earliest_fill_at = iso_now()
            calendar_status = "FAIL"
            calendar_detail = str(exc)
        checks.append(
            RiskCheck(
                check="trading_calendar",
                status=calendar_status,
                detail=calendar_detail,
            )
        )
        tick_valid = abs(request.limit_price * 100 - round(request.limit_price * 100)) < 1e-6
        checks.append(
            RiskCheck(
                check="tick_size",
                status="PASS" if tick_valid else "FAIL",
                detail="A股普通股票 V1 仅接受 0.01 元价格档位",
            )
        )
        checks.append(
            RiskCheck(
                check="ruleset_version",
                status=(
                    "PASS" if self._rules_status == "PAPER_V1_FAIL_CLOSED_BASELINE" else "FAIL"
                ),
                detail=f"{self._ruleset_version} / {self._rules_status}",
            )
        )

        positions = await self._repository.positions()
        current = next(
            (position for position in positions if position.symbol == request.symbol),
            None,
        )
        if request.side == "BUY":
            research_ok, research_detail, research_warnings = self._buy_research_gate(
                request.symbol,
                request.research_version,
            )
            checks.append(
                RiskCheck(
                    check="research_version",
                    status="PASS" if research_ok else "FAIL",
                    detail=research_detail,
                )
            )
            if research_warnings:
                checks.append(
                    RiskCheck(
                        check="research_import",
                        status="REVIEW",
                        detail="；".join(research_warnings[:2]),
                    )
                )
            checks.extend(
                await self._buy_portfolio_checks(
                    request.symbol,
                    request.quantity,
                    request.quantity,
                    request.limit_price,
                    request.invalidation_price,
                )
            )
            checks.append(
                RiskCheck(
                    check="fee_assumption",
                    status="REVIEW",
                    detail=f"佣金为模拟假设；费用版本 {self._policy.fees.version}",
                )
            )
        else:
            sellable = current.sellable_quantity if current else 0
            checks.append(
                RiskCheck(
                    check="t_plus_one_sellable",
                    status="PASS" if request.quantity <= sellable else "FAIL",
                    detail=f"当前按 lot 账本可卖 {sellable} 股",
                )
            )

        failures = [check.detail for check in checks if check.status == "FAIL"]
        if failures:
            raise PaperRuleError("；".join(failures))
        created_at = iso_now()
        order = PaperOrder(
            order_id=str(uuid4()),
            symbol=request.symbol,
            name=request.name,
            side=request.side,
            order_type=request.order_type,
            limit_price=request.limit_price,
            quantity=request.quantity,
            filled_quantity=0,
            status="PENDING_APPROVAL",
            reason=request.reason,
            research_version=request.research_version,
            expected_horizon=request.expected_horizon,
            risk_notes=request.risk_notes,
            invalidation_conditions=request.invalidation_conditions,
            invalidation_price=request.invalidation_price,
            ruleset_version=self._ruleset_version,
            fee_schedule_version=self._policy.fees.version,
            risk_checks=checks,
            created_at=created_at,
            earliest_fill_at=earliest_fill_at,
        )
        return await self._repository.create_order(order, idempotency_key)

    async def approve(self, order_id: str, reason: str) -> PaperOrder:
        order = await self._repository.get_order(order_id)
        if order is None:
            raise PaperRuleError("模拟订单不存在")
        if order.side == "BUY":
            await self._assert_buy_order_gates(order, "人工批准前")
        return await self._repository.update_order_status(
            order_id,
            "PENDING_APPROVAL",
            "QUEUED",
            reason,
        )

    async def reject(self, order_id: str, reason: str) -> PaperOrder:
        return await self._repository.update_order_status(
            order_id,
            "PENDING_APPROVAL",
            "REJECTED",
            reason,
        )

    def _estimate_fees(self, side: str, gross: float) -> float:
        commission = max(
            gross * self._policy.fees.commission_rate,
            self._policy.fees.commission_minimum_cny,
        )
        stamp = gross * self._policy.fees.stamp_duty_sell_rate if side == "SELL" else 0
        transfer = gross * self._policy.fees.transfer_fee_both_sides_rate
        return _money(commission + stamp + transfer)

    async def simulate_fill(self, order_id: str) -> PaperOrder:
        order = await self._repository.get_order(order_id)
        if order is None:
            raise PaperRuleError("模拟订单不存在")
        if order.status not in {"QUEUED", "PARTIAL"}:
            raise PaperRuleError("订单尚未人工批准或已经结束")
        if order.side == "BUY":
            await self._assert_buy_order_gates(order, "模拟成交前")
        moment = now_shanghai()
        earliest_fill_at = datetime.fromisoformat(order.earliest_fill_at).astimezone(SHANGHAI)
        if moment < earliest_fill_at:
            raise PaperRuleError(
                f"A股 T+1/订单时序门尚未开放，最早模拟成交时间为 {order.earliest_fill_at}"
            )
        if not _is_regular_session(moment):
            raise PaperRuleError("只在A股常规竞价时段使用当日公开行情模拟成交")
        try:
            quote, source = await self._stock.quote(order.symbol)
        except StockServiceError as exc:
            raise PaperRuleError(f"成交行情不可用: {exc}") from exc
        if source.state != "live" or quote.updated_at[:10] != moment.date().isoformat():
            raise PaperRuleError("行情不是当日实时状态，订单继续排队")
        if quote.volume_shares <= 0 or quote.limit_up is None or quote.limit_down is None:
            raise PaperRuleError("停牌/涨跌停参考参数无法可靠确认，拒绝模拟成交")
        if order.side == "BUY" and quote.price >= quote.limit_up - 0.005:
            raise PaperRuleError("当前触及涨停且无可成交队列证据，订单继续排队")
        if order.side == "SELL" and quote.price <= quote.limit_down + 0.005:
            raise PaperRuleError("当前触及跌停且无可成交队列证据，订单继续排队")
        slippage_rate = self._policy.fees.slippage_bps / 10_000
        raw_price = quote.price * (1 + slippage_rate if order.side == "BUY" else 1 - slippage_rate)
        execution_price = round(raw_price + 1e-9, 2)
        if order.side == "BUY" and execution_price > order.limit_price:
            raise PaperRuleError("含滑点模拟价高于买入限价，订单继续排队")
        if order.side == "SELL" and execution_price < order.limit_price:
            raise PaperRuleError("含滑点模拟价低于卖出限价，订单继续排队")
        remaining_quantity = order.quantity - order.filled_quantity
        _, increment, _ = _lot_rule(order.symbol)
        max_participation = int(quote.volume_shares * 0.05)
        if increment > 1:
            max_participation = (max_participation // increment) * increment
        fill_quantity = min(remaining_quantity, max_participation)
        if fill_quantity <= 0:
            raise PaperRuleError("按 5% 成交量参与上限，本轮没有可模拟成交数量")
        if order.side == "BUY":
            portfolio_checks = await self._buy_portfolio_checks(
                order.symbol,
                fill_quantity,
                order.quantity,
                execution_price,
                order.invalidation_price,
            )
            portfolio_failures = [
                check.detail for check in portfolio_checks if check.status == "FAIL"
            ]
            if portfolio_failures:
                raise PaperRuleError(f"模拟成交前组合风控复核失败：{'；'.join(portfolio_failures)}")
        gross = _money(execution_price * fill_quantity)
        commission = _money(
            max(
                gross * self._policy.fees.commission_rate,
                self._policy.fees.commission_minimum_cny,
            )
        )
        stamp = _money(
            gross * self._policy.fees.stamp_duty_sell_rate if order.side == "SELL" else 0
        )
        transfer = _money(gross * self._policy.fees.transfer_fee_both_sides_rate)
        slippage = _money(abs(execution_price - quote.price) * fill_quantity)
        fill = PaperFill(
            fill_id=str(uuid4()),
            order_id=order.order_id,
            filled_at=iso_now(),
            price=execution_price,
            quantity=fill_quantity,
            gross_amount_cny=gross,
            commission_cny=commission,
            stamp_duty_cny=stamp,
            transfer_fee_cny=transfer,
            slippage_cny=slippage,
            total_fees_cny=_money(commission + stamp + transfer),
            source=source.provider,
            source_timestamp=quote.updated_at,
        )
        try:
            return await self._repository.record_fill(order_id, fill)
        except PaperLedgerError as exc:
            raise PaperRuleError(str(exc)) from exc
