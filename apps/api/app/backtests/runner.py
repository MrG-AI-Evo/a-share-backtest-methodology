from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from app.backtests.engine import (
    DeterministicLedger,
    SignalDecision,
    SignalInput,
    evaluate_signal,
    money,
)
from app.backtests.performance import link_subperiod_returns, xirr


@dataclass(frozen=True)
class FeeRule:
    commission_rate: Decimal
    minimum_commission_cny: Decimal
    stamp_duty_sell_rate: Decimal
    transfer_fee_rate: Decimal
    exchange_and_regulatory_rate: Decimal = Decimal("0")

    def costs(self, side: Literal["BUY", "SELL"], gross: Decimal) -> Decimal:
        commission = max(self.minimum_commission_cny, gross * self.commission_rate)
        stamp = gross * self.stamp_duty_sell_rate if side == "SELL" else Decimal("0")
        return money(
            commission
            + stamp
            + gross * self.transfer_fee_rate
            + gross * self.exchange_and_regulatory_rate
        )


@dataclass(frozen=True)
class SecurityDay:
    symbol: str
    name: str
    exchange: str
    ownership_type: str
    open_price: Decimal | None
    close_price: Decimal | None
    tradable_open: bool
    tradable_close: bool
    risk_status_eligible: bool
    quality_pass: bool | None
    d_ttm: Decimal | None
    d_med3: Decimal | None
    cd_rate: Decimal | None
    cd_tenor_years: int | None
    cd_rate_quality: Literal["OBSERVED", "TENOR_FALLBACK", "SYNTHETIC_CARRY", "UNAVAILABLE"]
    median_amount_20d_cny: Decimal | None
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CorporateActionDay:
    symbol: str
    action_type: Literal[
        "DIVIDEND_RECEIVABLE",
        "DIVIDEND_PAYMENT",
        "DIVIDEND_TAX",
        "SHARE_MULTIPLIER",
        "VOLUNTARY_RIGHTS_SKIPPED",
        "TERMINAL_RECOVERY",
    ]
    known_at: str
    cash_per_share: Decimal | None = None
    cash_amount: Decimal | None = None
    share_multiplier: Decimal | None = None
    unresolved: bool = False
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradingDayInput:
    trade_date: str
    is_first_trading_day_of_month: bool
    securities: tuple[SecurityDay, ...]
    fee_rule: FeeRule
    buy_slippage_bps: Decimal
    sell_slippage_bps: Decimal
    corporate_actions: tuple[CorporateActionDay, ...] = ()
    fee_rules_by_exchange: dict[str, FeeRule] = field(default_factory=dict)

    def fee_rule_for(self, exchange: str) -> FeeRule:
        return self.fee_rules_by_exchange.get(exchange, self.fee_rule)


@dataclass(frozen=True)
class PendingOrder:
    symbol: str
    side: Literal["BUY", "SELL"]
    signal_at: str
    limit_price: Decimal | None
    rank: int | None
    evidence_ids: tuple[str, ...]


@dataclass
class BacktestRunResult:
    ledger: DeterministicLedger
    checkpoints: list[dict[str, object]] = field(default_factory=list)
    pending_orders: list[PendingOrder] = field(default_factory=list)


class DeterministicHistoryRunner:
    def __init__(
        self,
        run_id: str,
        policy_version: str,
        *,
        run_purpose: Literal["FORMAL_BACKTEST", "MECHANISM_TEST"] = "MECHANISM_TEST",
        scenario: Literal["OPTIMISTIC", "BASE", "STRESS"] = "BASE",
        raw_data_mode: Literal["REAL_POINT_IN_TIME", "TEST_FIXTURE"] = "TEST_FIXTURE",
        synthetic_raw_observation_count: int = 1,
    ) -> None:
        self.run_id = run_id
        self.policy_version = policy_version
        self.run_purpose = run_purpose
        self.scenario = scenario
        self.raw_data_mode = raw_data_mode
        self.synthetic_raw_observation_count = synthetic_raw_observation_count

    def run(self, days: list[TradingDayInput]) -> BacktestRunResult:
        ledger = DeterministicLedger(self.run_id)
        pending: list[PendingOrder] = []
        checkpoints: list[dict[str, object]] = []
        for day in days:
            by_symbol = {security.symbol: security for security in day.securities}
            open_at = f"{day.trade_date}T09:30:00+08:00"
            close_at = f"{day.trade_date}T15:00:00+08:00"
            self._apply_actions(ledger, day.corporate_actions, open_at)
            pending = self._execute_pending(ledger, pending, by_symbol, day, open_at)
            for symbol, position in ledger.positions.items():
                security = by_symbol.get(symbol)
                if (
                    position.quantity
                    and security
                    and security.close_price
                    and security.tradable_close
                ):
                    ledger.mark(symbol, security.close_price, close_at, close_at)
            queued_exits = self._queue_exits(ledger, pending, day.securities, close_at)
            pending.extend(queued_exits)
            if day.is_first_trading_day_of_month:
                pending.extend(self._queue_entries(ledger, pending, day.securities, close_at))
            checkpoints.append(
                ledger.snapshot(
                    as_of=close_at,
                    policy_version=self.policy_version,
                    run_purpose=self.run_purpose,
                    scenario=self.scenario,
                    raw_data_mode=self.raw_data_mode,
                    synthetic_raw_observation_count=self.synthetic_raw_observation_count,
                )
            )
        self._finalize_performance(ledger, checkpoints)
        return BacktestRunResult(ledger=ledger, checkpoints=checkpoints, pending_orders=pending)

    @staticmethod
    def _finalize_performance(
        ledger: DeterministicLedger, checkpoints: list[dict[str, object]]
    ) -> None:
        if not checkpoints:
            return
        period_returns: list[Decimal] = []
        unit_index = Decimal("1")
        peak = unit_index
        maximum_drawdown = Decimal("0")
        previous_equity = Decimal("0")
        previous_contributions = Decimal("0")
        previous_withdrawals = Decimal("0")
        for checkpoint in checkpoints:
            account = checkpoint["account_summary"]
            assert isinstance(account, dict)
            equity = Decimal(str(account["equity_cny"]))
            contributions = Decimal(str(account["cumulative_external_contributions_cny"]))
            withdrawals = Decimal(str(account["cumulative_external_withdrawals_cny"]))
            external_in = contributions - previous_contributions
            external_out = withdrawals - previous_withdrawals
            denominator = previous_equity + external_in - external_out
            if denominator > 0:
                period_return = equity / denominator - Decimal("1")
                period_returns.append(period_return)
                unit_index *= Decimal("1") + period_return
                peak = max(peak, unit_index)
                maximum_drawdown = min(maximum_drawdown, unit_index / peak - Decimal("1"))
            previous_equity = equity
            previous_contributions = contributions
            previous_withdrawals = withdrawals
        terminal = checkpoints[-1]
        terminal_account = terminal["account_summary"]
        performance = terminal["performance"]
        assert isinstance(terminal_account, dict)
        assert isinstance(performance, dict)
        investor_flows = [
            (
                date.fromisoformat(str(flow["occurred_at"])[:10]),
                -Decimal(str(flow["amount_cny"])),
            )
            for flow in ledger.cash_flows
            if flow["flow_type"] == "CONTRIBUTION"
        ]
        terminal_date = date.fromisoformat(str(terminal["as_of"])[:10])
        terminal_equity = Decimal(str(terminal_account["equity_cny"]))
        performance["mwrr_xirr_annualized"] = xirr(
            investor_flows,
            terminal_date=terminal_date,
            terminal_equity=terminal_equity,
        )
        performance["twr_cumulative"] = link_subperiod_returns(period_returns)
        performance["maximum_drawdown"] = float(maximum_drawdown)
        performance["calculation_status"] = "COMPLETE"

    def _apply_actions(
        self,
        ledger: DeterministicLedger,
        actions: tuple[CorporateActionDay, ...],
        occurred_at: str,
    ) -> None:
        for action in actions:
            if action.symbol not in ledger.positions:
                continue
            if action.action_type == "SHARE_MULTIPLIER" and action.share_multiplier is not None:
                ledger.apply_share_multiplier(
                    symbol=action.symbol,
                    multiplier=action.share_multiplier,
                    occurred_at=occurred_at,
                    known_at=action.known_at,
                )
            elif action.action_type == "DIVIDEND_RECEIVABLE" and action.cash_per_share is not None:
                ledger.declare_dividend(
                    symbol=action.symbol,
                    cash_per_share=action.cash_per_share,
                    occurred_at=occurred_at,
                    known_at=action.known_at,
                )
            elif action.action_type == "DIVIDEND_PAYMENT":
                ledger.pay_dividend(
                    symbol=action.symbol, occurred_at=occurred_at, known_at=action.known_at
                )
            elif action.action_type == "DIVIDEND_TAX" and action.cash_amount is not None:
                ledger.apply_dividend_tax(
                    symbol=action.symbol,
                    amount=action.cash_amount,
                    occurred_at=occurred_at,
                    known_at=action.known_at,
                )
            elif action.action_type == "TERMINAL_RECOVERY":
                ledger.terminal_recovery(
                    symbol=action.symbol,
                    recovery_cny=action.cash_amount or Decimal("0"),
                    occurred_at=occurred_at,
                    known_at=action.known_at,
                    unresolved=action.unresolved,
                )
            elif action.action_type == "VOLUNTARY_RIGHTS_SKIPPED":
                ledger.record_event(
                    "CORPORATE_ACTION",
                    occurred_at,
                    action.known_at,
                    {"action": "VOLUNTARY_RIGHTS_SKIPPED", "cash_delta_cny": 0.0},
                    action.symbol,
                )

    def _execute_pending(
        self,
        ledger: DeterministicLedger,
        pending: list[PendingOrder],
        by_symbol: dict[str, SecurityDay],
        day: TradingDayInput,
        occurred_at: str,
    ) -> list[PendingOrder]:
        remaining: list[PendingOrder] = []
        ordered = sorted(
            pending, key=lambda item: (0 if item.side == "SELL" else 1, item.rank or 0)
        )
        for order in ordered:
            security = by_symbol.get(order.symbol)
            if security is None or not security.tradable_open or security.open_price is None:
                ledger.record_event(
                    "ORDER_QUEUED",
                    occurred_at,
                    occurred_at,
                    {"side": order.side, "reason": "UNTRADABLE_OPEN"},
                    order.symbol,
                )
                remaining.append(order)
                continue
            if order.side == "SELL":
                if (
                    order.symbol not in ledger.positions
                    or ledger.positions[order.symbol].quantity == 0
                ):
                    continue
                fill_price = security.open_price * (
                    Decimal("1") - day.sell_slippage_bps / Decimal("10000")
                )
                gross = money(fill_price * ledger.positions[order.symbol].quantity)
                fee_rule = day.fee_rule_for(security.exchange)
                ledger.sell(
                    symbol=order.symbol,
                    price=fill_price,
                    transaction_costs=fee_rule.costs("SELL", gross),
                    occurred_at=occurred_at,
                    known_at=occurred_at,
                )
                continue
            if len([p for p in ledger.positions.values() if p.quantity > 0]) >= 20:
                ledger.record_event(
                    "ORDER_REJECTED",
                    occurred_at,
                    occurred_at,
                    {"side": "BUY", "reason": "NO_VACANT_SLOT"},
                    order.symbol,
                )
                continue
            if order.symbol in ledger.positions and ledger.positions[order.symbol].quantity > 0:
                continue
            fill_price = security.open_price * (
                Decimal("1") + day.buy_slippage_bps / Decimal("10000")
            )
            if order.limit_price is None or fill_price > order.limit_price:
                ledger.record_event(
                    "ORDER_REJECTED",
                    occurred_at,
                    occurred_at,
                    {
                        "side": "BUY",
                        "reason": "OPEN_PLUS_SLIPPAGE_ABOVE_LIMIT",
                        "fill_price": float(fill_price),
                        "limit_price": float(order.limit_price) if order.limit_price else None,
                    },
                    order.symbol,
                )
                continue
            gross = money(fill_price * 1000)
            fee_rule = day.fee_rule_for(security.exchange)
            ledger.buy(
                symbol=security.symbol,
                name=security.name,
                exchange=security.exchange,
                price=fill_price,
                quantity=1000,
                transaction_costs=fee_rule.costs("BUY", gross),
                occurred_at=occurred_at,
                known_at=occurred_at,
            )
            ledger.positions[security.symbol].ownership_type = security.ownership_type
        return remaining

    def _decision(
        self, security: SecurityDay, signal_at: str, currently_held: bool
    ) -> SignalDecision:
        return evaluate_signal(
            SignalInput(
                signal_at=signal_at,
                known_at=signal_at,
                d_ttm=security.d_ttm,
                d_med3=security.d_med3,
                raw_close=security.close_price,
                cd_rate=security.cd_rate,
                cd_tenor_years=security.cd_tenor_years,
                quality_pass=security.quality_pass and security.risk_status_eligible,
                cd_rate_quality=security.cd_rate_quality,
            ),
            currently_held=currently_held,
        )

    def _queue_exits(
        self,
        ledger: DeterministicLedger,
        pending: list[PendingOrder],
        securities: tuple[SecurityDay, ...],
        signal_at: str,
    ) -> list[PendingOrder]:
        by_symbol = {item.symbol: item for item in securities}
        already_pending = {item.symbol for item in pending if item.side == "SELL"}
        orders: list[PendingOrder] = []
        for symbol, position in ledger.positions.items():
            if position.quantity <= 0:
                continue
            if symbol in already_pending:
                continue
            security = by_symbol.get(symbol)
            if security is None:
                ledger.record_event(
                    "DATA_GAP", signal_at, signal_at, {"reason": "MISSING_SECURITY_DAY"}, symbol
                )
                continue
            decision = self._decision(security, signal_at, currently_held=True)
            if decision.action == "EXIT":
                ledger.record_event(
                    "EXIT_SIGNAL", signal_at, signal_at, {"signal": decision.snapshot}, symbol
                )
                orders.append(
                    PendingOrder(symbol, "SELL", signal_at, None, None, security.evidence_ids)
                )
            elif decision.action == "DATA_GAP":
                ledger.record_event(
                    "DATA_GAP", signal_at, signal_at, {"reason": decision.reason}, symbol
                )
        return orders

    def _queue_entries(
        self,
        ledger: DeterministicLedger,
        pending: list[PendingOrder],
        securities: tuple[SecurityDay, ...],
        signal_at: str,
    ) -> list[PendingOrder]:
        occupied = sum(position.quantity > 0 for position in ledger.positions.values())
        pending_buys = {order.symbol for order in pending if order.side == "BUY"}
        vacancies = max(0, 20 - occupied - len(pending_buys))
        candidates: list[tuple[Decimal, Decimal, str, SecurityDay, SignalDecision]] = []
        for security in securities:
            if (
                security.symbol in ledger.positions
                and ledger.positions[security.symbol].quantity > 0
            ):
                continue
            if security.symbol in pending_buys:
                continue
            decision = self._decision(security, signal_at, currently_held=False)
            if decision.action == "DATA_GAP":
                ledger.record_event(
                    "DATA_GAP", signal_at, signal_at, {"reason": decision.reason}, security.symbol
                )
                continue
            if decision.action != "ENTRY_ELIGIBLE":
                continue
            buffer = Decimal(str(decision.snapshot["yield_buffer"]))
            liquidity = security.median_amount_20d_cny or Decimal("0")
            candidates.append((buffer, liquidity, security.symbol, security, decision))
        candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
        result: list[PendingOrder] = []
        for rank, (_, _, _, security, decision) in enumerate(candidates[:vacancies], start=1):
            maximum = decision.snapshot["maximum_buy_price_cny"]
            assert maximum is not None
            ledger.record_event(
                "ENTRY_SIGNAL",
                signal_at,
                signal_at,
                {"rank": rank, "signal": decision.snapshot},
                security.symbol,
            )
            result.append(
                PendingOrder(
                    security.symbol,
                    "BUY",
                    signal_at,
                    Decimal(str(maximum)),
                    rank,
                    security.evidence_ids,
                )
            )
        return result
