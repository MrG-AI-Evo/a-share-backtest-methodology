from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from app.core.settings import Settings


class PolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RiskPolicy:
    risk_per_trade_pct_nav: float
    initial_position_max_pct_nav: float
    single_stock_hard_max_pct_nav: float
    top5_max_pct_nav: float
    gross_exposure_max_pct_nav: float
    risk_off_exposure_max_pct_nav: float
    max_new_positions_per_day: int
    stop_new_buys_daily_loss_pct: float
    risk_off_drawdown_pct: float
    freeze_drawdown_pct: float


@dataclass(frozen=True)
class FeePolicy:
    version: str
    commission_rate: float
    commission_minimum_cny: float
    stamp_duty_sell_rate: float
    transfer_fee_both_sides_rate: float
    slippage_bps: float
    notes: list[str]


@dataclass(frozen=True)
class PaperPolicy:
    version: str
    initial_cash_cny: float
    risk: RiskPolicy
    fees: FeePolicy


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyError(f"{name} 必须是对象")
    return value


def load_policy(settings: Settings) -> PaperPolicy:
    try:
        payload = yaml.safe_load(settings.policy_file.read_text("utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"无法读取模拟盘策略配置: {exc}") from exc
    root = _mapping(payload, "policy")
    risk = _mapping(root.get("risk"), "risk")
    execution = _mapping(root.get("execution"), "execution")
    fees = _mapping(execution.get("fees"), "execution.fees")
    if execution.get("live_broker_enabled") is not False:
        raise PolicyError("策略配置禁止启用真实券商")
    if execution.get("automatic_trading_enabled") is not False:
        raise PolicyError("策略配置禁止启用自动交易")
    fee_version = execution.get("fee_schedule_version")
    policy_version = root.get("policy_version")
    if not isinstance(fee_version, str) or not isinstance(policy_version, str):
        raise PolicyError("策略与费用版本不能为空")
    return PaperPolicy(
        version=policy_version,
        initial_cash_cny=float(root["initial_cash_cny"]),
        risk=RiskPolicy(
            risk_per_trade_pct_nav=float(risk["risk_per_trade_pct_nav"]),
            initial_position_max_pct_nav=float(risk["initial_position_max_pct_nav"]),
            single_stock_hard_max_pct_nav=float(risk["single_stock_hard_max_pct_nav"]),
            top5_max_pct_nav=float(risk["top5_max_pct_nav"]),
            gross_exposure_max_pct_nav=float(risk["gross_exposure_max_pct_nav"]),
            risk_off_exposure_max_pct_nav=float(risk["risk_off_exposure_max_pct_nav"]),
            max_new_positions_per_day=int(risk["max_new_positions_per_day"]),
            stop_new_buys_daily_loss_pct=float(risk["stop_new_buys_daily_loss_pct"]),
            risk_off_drawdown_pct=float(risk["risk_off_drawdown_pct"]),
            freeze_drawdown_pct=float(risk["freeze_drawdown_pct"]),
        ),
        fees=FeePolicy(
            version=fee_version,
            commission_rate=float(fees["commission_rate"]),
            commission_minimum_cny=float(fees["commission_minimum_cny"]),
            stamp_duty_sell_rate=float(fees["stamp_duty_sell_rate"]),
            transfer_fee_both_sides_rate=float(fees["transfer_fee_both_sides_rate"]),
            slippage_bps=float(execution["default_slippage_bps"]),
            notes=[
                str(fees["commission_basis"]),
                str(fees["stamp_duty_basis"]),
                str(fees["transfer_fee_basis"]),
            ],
        ),
    )


def load_ruleset_meta(settings: Settings) -> tuple[str, str]:
    try:
        payload = yaml.safe_load(settings.rule_file.read_text("utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"无法读取 A 股规则库: {exc}") from exc
    root = _mapping(payload, "rules")
    version = root.get("ruleset_version")
    status = root.get("status")
    if not isinstance(version, str) or not isinstance(status, str):
        raise PolicyError("规则库版本或状态缺失")
    return version, status
