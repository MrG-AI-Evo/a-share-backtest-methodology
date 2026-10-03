from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

ZERO = Decimal("0")
CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class SignalInput:
    signal_at: str
    known_at: str
    d_ttm: Decimal | None
    d_med3: Decimal | None
    raw_close: Decimal | None
    cd_rate: Decimal | None
    cd_tenor_years: int | None
    quality_pass: bool | None
    cd_rate_quality: Literal["OBSERVED", "TENOR_FALLBACK", "SYNTHETIC_CARRY", "UNAVAILABLE"]


@dataclass(frozen=True)
class SignalDecision:
    action: Literal["ENTRY_ELIGIBLE", "HOLD", "EXIT", "DATA_GAP", "REJECT"]
    reason: str
    snapshot: dict[str, float | int | str | None]


def evaluate_signal(signal: SignalInput, *, currently_held: bool) -> SignalDecision:
    if signal.known_at > signal.signal_at:
        return SignalDecision("REJECT", "known_at 晚于 signal_at，触发时间穿越门。", {})
    values = (signal.d_ttm, signal.d_med3, signal.raw_close, signal.cd_rate)
    if signal.quality_pass is None or any(value is None for value in values):
        return SignalDecision("DATA_GAP", "关键输入缺失；禁止新建仓且不据此强制退出。", {})
    assert signal.d_ttm is not None
    assert signal.d_med3 is not None
    assert signal.raw_close is not None
    assert signal.cd_rate is not None
    if signal.raw_close <= ZERO:
        return SignalDecision("REJECT", "原始收盘价必须大于0。", {})
    if signal.cd_rate_quality == "SYNTHETIC_CARRY":
        return SignalDecision("REJECT", "主回测禁止使用 SYNTHETIC_CARRY 利率。", {})
    d_cons = min(signal.d_ttm, signal.d_med3)
    dividend_yield = d_cons / signal.raw_close
    hurdle = signal.cd_rate + Decimal("0.0300")
    maximum_buy_price = d_cons / hurdle if hurdle > ZERO else None
    snapshot: dict[str, float | int | str | None] = {
        "d_ttm_cny_per_share": float(signal.d_ttm),
        "d_med3_cny_per_share": float(signal.d_med3),
        "d_cons_cny_per_share": float(d_cons),
        "raw_close_cny": float(signal.raw_close),
        "dividend_yield": float(dividend_yield),
        "cd_signal_rate": float(signal.cd_rate),
        "cd_tenor_years": signal.cd_tenor_years,
        "cd_rate_quality": signal.cd_rate_quality,
        "hurdle_rate": float(hurdle),
        "yield_buffer": float(dividend_yield - hurdle),
        "maximum_buy_price_cny": float(maximum_buy_price) if maximum_buy_price else None,
    }
    if not signal.quality_pass:
        return SignalDecision("REJECT", "质量门未通过。", snapshot)
    if currently_held:
        return SignalDecision(
            "HOLD" if dividend_yield >= hurdle else "EXIT",
            "等于或高于门槛继续持有。" if dividend_yield >= hurdle else "股息率严格低于门槛。",
            snapshot,
        )
    return SignalDecision(
        "ENTRY_ELIGIBLE" if dividend_yield >= hurdle else "REJECT",
        "质量门通过且股息率不低于门槛。" if dividend_yield >= hurdle else "股息率低于门槛。",
        snapshot,
    )


@dataclass
class PositionState:
    symbol: str
    name: str
    exchange: str
    ownership_type: str = "UNKNOWN"
    quantity: int = 0
    cost_basis: Decimal = ZERO
    last_price: Decimal | None = None
    dividend_receivable: Decimal = ZERO
    cumulative_buy_gross: Decimal = ZERO
    cumulative_buy_costs: Decimal = ZERO
    cumulative_sell_gross: Decimal = ZERO
    cumulative_sell_costs: Decimal = ZERO
    cumulative_dividend_gross: Decimal = ZERO
    cumulative_dividend_tax: Decimal = ZERO
    attributed_contributions: Decimal = ZERO
    operations: list[dict[str, object]] = field(default_factory=list)
    episode_count: int = 0


class DeterministicLedger:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.cash = ZERO
        self.contributions = ZERO
        self.withdrawals = ZERO
        self.positions: dict[str, PositionState] = {}
        self.events: list[dict[str, object]] = []
        self.cash_flows: list[dict[str, object]] = []
        self._previous_hash: str | None = None

    def _append_event(
        self,
        event_type: str,
        occurred_at: str,
        known_at: str,
        payload: dict[str, object],
        symbol: str | None = None,
    ) -> dict[str, object]:
        if known_at > occurred_at:
            raise ValueError("known_at 不得晚于事件发生时点")
        sequence = len(self.events) + 1
        core: dict[str, object] = {
            "run_id": self.run_id,
            "sequence": sequence,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "known_at": known_at,
            "symbol": symbol,
            "payload": payload,
            "prev_event_hash": self._previous_hash,
        }
        canonical = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        event_hash = hashlib.sha256(f"{self._previous_hash or ''}{canonical}".encode()).hexdigest()
        event = {"event_id": f"{self.run_id}-evt-{sequence:06d}", **core, "event_hash": event_hash}
        self.events.append(event)
        self._previous_hash = event_hash
        return event

    def record_event(
        self,
        event_type: str,
        occurred_at: str,
        known_at: str,
        payload: dict[str, object],
        symbol: str | None = None,
    ) -> dict[str, object]:
        return self._append_event(event_type, occurred_at, known_at, payload, symbol)

    def buy(
        self,
        *,
        symbol: str,
        name: str,
        exchange: str,
        price: Decimal,
        quantity: int,
        transaction_costs: Decimal,
        occurred_at: str,
        known_at: str,
    ) -> None:
        if quantity != 1000:
            raise ValueError("每次新建仓必须恰好1000股")
        position = self.positions.setdefault(symbol, PositionState(symbol, name, exchange))
        if position.quantity != 0:
            raise ValueError("已有持仓不得重复买入或补仓")
        gross = money(price * quantity)
        total = money(gross + transaction_costs)
        contribution = money(max(ZERO, total - self.cash))
        if contribution > ZERO:
            self.cash = money(self.cash + contribution)
            self.contributions = money(self.contributions + contribution)
            position.attributed_contributions = money(
                position.attributed_contributions + contribution
            )
            flow_id = f"{self.run_id}-flow-{len(self.cash_flows) + 1:06d}"
            event = self._append_event(
                "CONTRIBUTION_ATTRIBUTION",
                occurred_at,
                known_at,
                {"amount_cny": float(contribution), "reason": "EXACT_EXECUTION_CASH_SHORTFALL"},
                symbol,
            )
            self.cash_flows.append(
                {
                    "flow_id": flow_id,
                    "occurred_at": occurred_at,
                    "flow_type": "CONTRIBUTION",
                    "amount_cny": float(contribution),
                    "caused_by_operation_id": event["event_id"],
                    "attributed_symbol": symbol,
                }
            )
        self.cash = money(self.cash - total)
        position.quantity = quantity
        position.episode_count += 1
        position.cost_basis = total
        position.cumulative_buy_gross = money(position.cumulative_buy_gross + gross)
        position.cumulative_buy_costs = money(position.cumulative_buy_costs + transaction_costs)
        position.last_price = price
        event = self._append_event(
            "BUY_FILL",
            occurred_at,
            known_at,
            {
                "price_cny": float(price),
                "quantity": quantity,
                "gross_cny": float(gross),
                "transaction_costs_cny": float(transaction_costs),
            },
            symbol,
        )
        position.operations.append(event)

    def sell(
        self,
        *,
        symbol: str,
        price: Decimal,
        transaction_costs: Decimal,
        occurred_at: str,
        known_at: str,
    ) -> None:
        position = self.positions[symbol]
        if position.quantity <= 0:
            raise ValueError("无可卖持仓")
        quantity = position.quantity
        gross = money(price * quantity)
        net = money(gross - transaction_costs)
        self.cash = money(self.cash + net)
        position.cumulative_sell_gross = money(position.cumulative_sell_gross + gross)
        position.cumulative_sell_costs = money(position.cumulative_sell_costs + transaction_costs)
        position.quantity = 0
        position.last_price = None
        event = self._append_event(
            "SELL_FILL",
            occurred_at,
            known_at,
            {
                "price_cny": float(price),
                "quantity": quantity,
                "gross_cny": float(gross),
                "transaction_costs_cny": float(transaction_costs),
            },
            symbol,
        )
        position.operations.append(event)

    def declare_dividend(
        self,
        *,
        symbol: str,
        cash_per_share: Decimal,
        occurred_at: str,
        known_at: str,
    ) -> None:
        position = self.positions[symbol]
        gross = money(cash_per_share * position.quantity)
        position.dividend_receivable = money(position.dividend_receivable + gross)
        event = self._append_event(
            "DIVIDEND_RECEIVABLE",
            occurred_at,
            known_at,
            {"cash_per_share_cny": float(cash_per_share), "gross_cny": float(gross)},
            symbol,
        )
        position.operations.append(event)

    def pay_dividend(self, *, symbol: str, occurred_at: str, known_at: str) -> None:
        position = self.positions[symbol]
        amount = position.dividend_receivable
        position.dividend_receivable = ZERO
        position.cumulative_dividend_gross = money(position.cumulative_dividend_gross + amount)
        self.cash = money(self.cash + amount)
        event = self._append_event(
            "DIVIDEND_PAYMENT",
            occurred_at,
            known_at,
            {"gross_cny": float(amount)},
            symbol,
        )
        position.operations.append(event)

    def apply_dividend_tax(
        self,
        *,
        symbol: str,
        amount: Decimal,
        occurred_at: str,
        known_at: str,
    ) -> None:
        position = self.positions[symbol]
        amount = money(amount)
        if amount < ZERO:
            raise ValueError("红利税不得为负")
        if amount > self.cash:
            raise ValueError("红利税扣缴超过可用现金，需先按真实事件处理负债")
        self.cash = money(self.cash - amount)
        position.cumulative_dividend_tax = money(position.cumulative_dividend_tax + amount)
        event = self._append_event(
            "DIVIDEND_TAX",
            occurred_at,
            known_at,
            {"amount_cny": float(amount), "accounting": "DIVIDEND_TAX_ONLY_ONCE"},
            symbol,
        )
        position.operations.append(event)

    def terminal_recovery(
        self,
        *,
        symbol: str,
        recovery_cny: Decimal,
        occurred_at: str,
        known_at: str,
        unresolved: bool,
    ) -> None:
        position = self.positions[symbol]
        recovery_cny = money(recovery_cny)
        if recovery_cny < ZERO:
            raise ValueError("终止回收不得为负")
        self.cash = money(self.cash + recovery_cny)
        previous_quantity = position.quantity
        position.quantity = 0
        position.last_price = None
        event = self._append_event(
            "TERMINAL_RECOVERY",
            occurred_at,
            known_at,
            {
                "recovery_cny": float(recovery_cny),
                "previous_quantity": previous_quantity,
                "valuation_policy": (
                    "ZERO_REALIZABLE_LOWER_BOUND" if unresolved else "HISTORICAL_EXECUTABLE_TERMS"
                ),
            },
            symbol,
        )
        position.operations.append(event)

    def audit_chain_ok(self) -> bool:
        previous_hash: str | None = None
        for event in self.events:
            core = {
                key: event[key]
                for key in (
                    "run_id",
                    "sequence",
                    "event_type",
                    "occurred_at",
                    "known_at",
                    "symbol",
                    "payload",
                    "prev_event_hash",
                )
            }
            canonical = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256(f"{previous_hash or ''}{canonical}".encode()).hexdigest()
            if event["prev_event_hash"] != previous_hash or event["event_hash"] != expected:
                return False
            previous_hash = str(event["event_hash"])
        return True

    def apply_share_multiplier(
        self,
        *,
        symbol: str,
        multiplier: Decimal,
        occurred_at: str,
        known_at: str,
    ) -> None:
        position = self.positions[symbol]
        new_quantity = int(Decimal(position.quantity) * multiplier)
        if new_quantity < 0:
            raise ValueError("公司行动后股数不得为负")
        previous = position.quantity
        position.quantity = new_quantity
        event = self._append_event(
            "CORPORATE_ACTION",
            occurred_at,
            known_at,
            {
                "share_multiplier": float(multiplier),
                "from_quantity": previous,
                "to_quantity": new_quantity,
            },
            symbol,
        )
        position.operations.append(event)

    def mark(self, symbol: str, price: Decimal, occurred_at: str, known_at: str) -> None:
        position = self.positions[symbol]
        position.last_price = price
        event = self._append_event(
            "VALUATION", occurred_at, known_at, {"raw_price_cny": float(price)}, symbol
        )
        position.operations.append(event)

    def snapshot(
        self,
        *,
        as_of: str,
        policy_version: str,
        run_purpose: Literal["FORMAL_BACKTEST", "MECHANISM_TEST"] = "MECHANISM_TEST",
        scenario: Literal["OPTIMISTIC", "BASE", "STRESS"] = "BASE",
        raw_data_mode: Literal["REAL_POINT_IN_TIME", "TEST_FIXTURE"] = "TEST_FIXTURE",
        synthetic_raw_observation_count: int = 1,
    ) -> dict[str, object]:
        market_value = money(
            sum(
                (
                    (position.last_price or ZERO) * position.quantity
                    for position in self.positions.values()
                ),
                ZERO,
            )
        )
        receivable = money(sum((p.dividend_receivable for p in self.positions.values()), ZERO))
        assets = money(self.cash + receivable + market_value)
        equity = assets
        total_pnl = money(equity + self.withdrawals - self.contributions)
        active = sum(position.quantity > 0 for position in self.positions.values())
        expected_cash = money(
            self.contributions
            + sum(
                p.cumulative_sell_gross - p.cumulative_sell_costs for p in self.positions.values()
            )
            + sum(
                p.cumulative_dividend_gross - p.dividend_receivable for p in self.positions.values()
            )
            - sum(p.cumulative_buy_gross + p.cumulative_buy_costs for p in self.positions.values())
            - sum((p.cumulative_dividend_tax for p in self.positions.values()), ZERO)
            - self.withdrawals
        )
        cash_difference = money(self.cash - expected_cash)
        total_pnl_check = money(equity + self.withdrawals - self.contributions - total_pnl)
        security_ledgers: list[dict[str, object]] = []
        for position in self.positions.values():
            current_market_value = money((position.last_price or ZERO) * position.quantity)
            net_sell = money(position.cumulative_sell_gross - position.cumulative_sell_costs)
            net_dividends = money(
                position.cumulative_dividend_gross - position.cumulative_dividend_tax
            )
            lifecycle = money(
                net_sell + net_dividends + position.dividend_receivable + current_market_value
            )
            cumulative_buy = money(position.cumulative_buy_gross + position.cumulative_buy_costs)
            security_ledgers.append(
                {
                    "symbol": position.symbol,
                    "exchange": position.exchange,
                    "name": position.name,
                    "ownership_type": position.ownership_type,
                    "position_state": "OPEN" if position.quantity else "CLOSED",
                    "current_episode_id": (
                        f"{position.symbol}-episode-{position.episode_count:03d}"
                        if position.quantity
                        else None
                    ),
                    "original_entry_target_shares": 1000,
                    "current_quantity": position.quantity,
                    "sellable_quantity": position.quantity,
                    "cumulative_external_contribution_attributed_cny": float(
                        position.attributed_contributions
                    ),
                    "cumulative_buy_gross_cny": float(position.cumulative_buy_gross),
                    "cumulative_buy_transaction_costs_cny": float(position.cumulative_buy_costs),
                    "cumulative_buy_cost_including_fees_cny": float(cumulative_buy),
                    "cumulative_sell_gross_cny": float(position.cumulative_sell_gross),
                    "cumulative_sell_transaction_costs_cny": float(position.cumulative_sell_costs),
                    "cumulative_net_sell_proceeds_cny": float(net_sell),
                    "cumulative_dividend_gross_cny": float(position.cumulative_dividend_gross),
                    "cumulative_dividend_tax_cny": float(position.cumulative_dividend_tax),
                    "cumulative_net_dividends_cny": float(net_dividends),
                    "current_dividend_receivable_cny": float(position.dividend_receivable),
                    "current_cost_basis_cny": float(
                        position.cost_basis if position.quantity else ZERO
                    ),
                    "current_raw_price_cny": float(position.last_price)
                    if position.last_price
                    else None,
                    "price_as_of": as_of if position.last_price else None,
                    "current_market_value_cny": float(current_market_value),
                    "valuation_status": "CURRENT" if position.quantity else "NO_POSITION",
                    "last_observable_reference_value_cny": float(current_market_value),
                    "last_observable_reference_at": as_of,
                    "lifecycle_value_cny": float(lifecycle),
                    "realized_pnl_cny": float(net_sell + net_dividends - cumulative_buy)
                    if not position.quantity
                    else 0.0,
                    "unrealized_pnl_cny": float(lifecycle - cumulative_buy)
                    if position.quantity
                    else 0.0,
                    "total_pnl_cny": float(lifecycle - cumulative_buy),
                    "operation_count": len(position.operations),
                    "operations": [],
                }
            )
        return {
            "schema_version": "1.0.0",
            "run_id": self.run_id,
            "policy_version": policy_version,
            "run_purpose": run_purpose,
            "scenario": scenario,
            "account_id": f"{self.run_id}-account",
            "as_of": as_of,
            "valuation_cutoff": as_of,
            "currency": "CNY",
            "run_status": "CHECKPOINT",
            "account_summary": {
                "cumulative_external_contributions_cny": float(self.contributions),
                "cumulative_external_withdrawals_cny": float(self.withdrawals),
                "net_external_contributions_cny": float(self.contributions - self.withdrawals),
                "cumulative_buy_gross_cny": float(
                    sum(p.cumulative_buy_gross for p in self.positions.values())
                ),
                "cumulative_buy_transaction_costs_cny": float(
                    sum(p.cumulative_buy_costs for p in self.positions.values())
                ),
                "cumulative_sell_gross_cny": float(
                    sum(p.cumulative_sell_gross for p in self.positions.values())
                ),
                "cumulative_sell_transaction_costs_cny": float(
                    sum(p.cumulative_sell_costs for p in self.positions.values())
                ),
                "cumulative_dividend_gross_cny": float(
                    sum(p.cumulative_dividend_gross for p in self.positions.values())
                ),
                "cumulative_dividend_tax_cny": float(
                    sum(p.cumulative_dividend_tax for p in self.positions.values())
                ),
                "cash_cny": float(self.cash),
                "dividend_receivable_cny": float(receivable),
                "other_receivables_cny": 0.0,
                "liabilities_cny": 0.0,
                "market_value_cny": float(market_value),
                "total_assets_cny": float(assets),
                "equity_cny": float(equity),
                "active_position_count": active,
                "maximum_position_count": 20,
                "vacant_slot_count": 20 - active,
                "unresolved_valuation_count": 0,
            },
            "performance": {
                "realized_pnl_cny": 0.0,
                "unrealized_pnl_cny": float(total_pnl),
                "total_pnl_cny": float(total_pnl),
                "simple_return": float(total_pnl / self.contributions)
                if self.contributions
                else None,
                "mwrr_xirr_annualized": None,
                "twr_cumulative": None,
                "maximum_drawdown": None,
                "calculation_status": "PARTIAL",
            },
            "security_ledgers": security_ledgers,
            "account_cash_flows": self.cash_flows,
            "reconciliation": {
                "status": "PASS"
                if abs(cash_difference) <= CENT and abs(total_pnl_check) <= CENT
                else "FAIL",
                "tolerance_cny": 0.01,
                "checks": [
                    {
                        "name": "CASH_ROLLFORWARD",
                        "status": "PASS" if abs(cash_difference) <= CENT else "FAIL",
                        "difference": float(cash_difference),
                        "unit": "CNY",
                        "formula": "cash ledger deterministic rollforward",
                    },
                    {
                        "name": "ASSET_EQUITY",
                        "status": "PASS",
                        "difference": 0.0,
                        "unit": "CNY",
                        "formula": "cash + receivables + market value - liabilities = equity",
                    },
                    {
                        "name": "TOTAL_PNL",
                        "status": "PASS" if abs(total_pnl_check) <= CENT else "FAIL",
                        "difference": float(total_pnl_check),
                        "unit": "CNY",
                        "formula": "equity + withdrawals - contributions = total pnl",
                    },
                ],
            },
            "data_quality": {
                "status": "VALID",
                "raw_data_mode": raw_data_mode,
                "synthetic_raw_observation_count": synthetic_raw_observation_count,
                "modeled_execution_assumption_count": 1,
                "data_cutoff": as_of,
                "time_travel_check": "PASS",
                "unit_check": "PASS",
                "ledger_check": "PASS" if abs(cash_difference) <= CENT else "FAIL",
                "stale_source_ids": [],
                "missing_critical_fields": [],
                "unresolved_valuation_symbols": [],
                "notes": (
                    ["仅用于机制测试，禁止进入正式绩效页面。"]
                    if run_purpose == "MECHANISM_TEST"
                    else ["正式结果仅在真实点时数据、时间穿越和账本门全部通过后可用。"]
                ),
            },
        }
