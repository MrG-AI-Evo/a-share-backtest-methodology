from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import yaml

from app.core.settings import Settings
from app.core.time import iso_now
from app.models import (
    ScreeningCandidate,
    ScreeningExclusion,
    ScreeningRun,
    UniverseSecurity,
)


class ScreeningPolicyError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScreeningPolicy:
    version: str
    scoring_version: str
    hard_target: int
    factor_target: int
    minimum_history_days: int
    minimum_avg_amount_20d_cny: float
    allowed_data_quality: frozenset[str]
    allowed_trading_status: frozenset[str]
    allowed_risk_status: frozenset[str]
    supported_asset_type: str
    weights: dict[str, float]
    missing_value_penalty: float


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScreeningPolicyError(f"{name} 必须是对象")
    return value


def load_screening_policy(settings: Settings) -> ScreeningPolicy:
    try:
        root = _mapping(yaml.safe_load(settings.screening_policy_file.read_text("utf-8")), "policy")
        targets = _mapping(root["targets"], "targets")
        filters = _mapping(root["hard_filters"], "hard_filters")
        weights_raw = _mapping(root["weights"], "weights")
        weights = {str(key): float(value) for key, value in weights_raw.items()}
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise ScreeningPolicyError(f"无法加载筛选配置: {exc}") from exc
    expected = {"quality", "value", "trend", "liquidity", "catalyst", "risk"}
    if set(weights) != expected or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ScreeningPolicyError("筛选权重必须覆盖六个固定维度且合计为 1")
    hard_target = int(targets["hard_filter"])
    factor_target = int(targets["factor_filter"])
    if hard_target < factor_target or factor_target < 1:
        raise ScreeningPolicyError("硬筛目标必须大于等于因子筛目标")
    return ScreeningPolicy(
        version=str(root["policy_version"]),
        scoring_version=str(root["scoring_version"]),
        hard_target=hard_target,
        factor_target=factor_target,
        minimum_history_days=int(filters["minimum_history_days"]),
        minimum_avg_amount_20d_cny=float(filters["minimum_avg_amount_20d_cny"]),
        allowed_data_quality=frozenset(str(item) for item in filters["allowed_data_quality"]),
        allowed_trading_status=frozenset(str(item) for item in filters["allowed_trading_status"]),
        allowed_risk_status=frozenset(str(item) for item in filters["allowed_risk_status"]),
        supported_asset_type=str(filters["supported_asset_type"]),
        weights=weights,
        missing_value_penalty=float(root["missing_value_penalty"]),
    )


def _canonical_input(rows: list[UniverseSecurity]) -> bytes:
    payload = [row.model_dump(mode="json") for row in sorted(rows, key=lambda item: item.symbol)]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _rank_score(
    rows: list[UniverseSecurity],
    row: UniverseSecurity,
    field: str,
    *,
    higher_is_better: bool = True,
) -> float | None:
    values = sorted(float(value) for item in rows if (value := getattr(item, field)) is not None)
    raw = getattr(row, field)
    if raw is None or not values:
        return None
    value = float(raw)
    less = sum(1 for item in values if item < value)
    equal = sum(1 for item in values if item == value)
    percentile = ((less + 0.5 * equal) / len(values)) * 100
    return percentile if higher_is_better else 100 - percentile


def _factor(
    components: list[float | None],
    missing_penalty: float,
) -> tuple[float, bool]:
    present = [value for value in components if value is not None]
    if not present:
        return 0.0, True
    missing_ratio = (len(components) - len(present)) / len(components)
    score = sum(present) / len(present) - missing_penalty * missing_ratio
    return round(max(0.0, min(100.0, score)), 4), missing_ratio > 0


def _hard_failures(
    row: UniverseSecurity,
    policy: ScreeningPolicy,
    as_of: datetime,
) -> list[str]:
    reasons: list[str] = []
    if row.asset_type != policy.supported_asset_type:
        reasons.append("UNSUPPORTED_ASSET_TYPE")
    if row.data_quality not in policy.allowed_data_quality:
        reasons.append("DATA_QUALITY_BELOW_B")
    if row.trading_status not in policy.allowed_trading_status:
        reasons.append(f"TRADING_STATUS_{row.trading_status}")
    if row.risk_status not in policy.allowed_risk_status:
        reasons.append(f"RISK_STATUS_{row.risk_status}")
    if row.history_days < policy.minimum_history_days:
        reasons.append("INSUFFICIENT_HISTORY")
    if row.last_price is None:
        reasons.append("MISSING_PRICE")
    if row.avg_amount_20d_cny is None or row.avg_amount_20d_cny < policy.minimum_avg_amount_20d_cny:
        reasons.append("INSUFFICIENT_LIQUIDITY")
    try:
        row_as_of = datetime.fromisoformat(row.as_of)
        if row_as_of.tzinfo is None or row_as_of > as_of:
            reasons.append("INVALID_AS_OF")
    except ValueError:
        reasons.append("INVALID_AS_OF")
    return reasons


class ScreeningService:
    def __init__(self, settings: Settings) -> None:
        self._policy = load_screening_policy(settings)

    @property
    def policy(self) -> ScreeningPolicy:
        return self._policy

    def run(
        self,
        rows: list[UniverseSecurity],
        as_of: datetime,
        run_id: str,
    ) -> ScreeningRun:
        if as_of.tzinfo is None:
            raise ScreeningPolicyError("筛选 as_of 必须带时区")
        symbols = [row.symbol for row in rows]
        if len(symbols) != len(set(symbols)):
            raise ScreeningPolicyError("全市场输入包含重复股票代码")

        eligible: list[UniverseSecurity] = []
        exclusions: list[ScreeningExclusion] = []
        for row in rows:
            reasons = _hard_failures(row, self._policy, as_of)
            if reasons:
                exclusions.append(
                    ScreeningExclusion(symbol=row.symbol, name=row.name, reason_codes=reasons)
                )
            else:
                eligible.append(row)

        eligible.sort(key=lambda item: (-(item.avg_amount_20d_cny or 0), item.symbol))
        stage_one = eligible[: self._policy.hard_target]
        hard_candidates = [
            ScreeningCandidate(
                symbol=row.symbol,
                name=row.name,
                exchange=row.exchange,
                industry=row.industry,
                stage="HARD_FILTER",
                rank=index,
                reason_codes=["HARD_FILTER_PASS"],
            )
            for index, row in enumerate(stage_one, start=1)
        ]

        scored: list[tuple[UniverseSecurity, float, dict[str, float], list[str]]] = []
        for row in stage_one:
            quality, quality_missing = _factor(
                [
                    _rank_score(stage_one, row, "roe_ttm_pct"),
                    _rank_score(stage_one, row, "operating_cashflow_profit_ratio"),
                ],
                self._policy.missing_value_penalty,
            )
            value, value_missing = _factor(
                [
                    _rank_score(stage_one, row, "pe_ttm", higher_is_better=False)
                    if row.pe_ttm and row.pe_ttm > 0
                    else None,
                    _rank_score(stage_one, row, "pb", higher_is_better=False)
                    if row.pb and row.pb > 0
                    else None,
                ],
                self._policy.missing_value_penalty,
            )
            trend, trend_missing = _factor(
                [
                    _rank_score(stage_one, row, "momentum_20d_pct"),
                    _rank_score(stage_one, row, "momentum_60d_pct"),
                    _rank_score(stage_one, row, "relative_strength_20d_pct"),
                ],
                self._policy.missing_value_penalty,
            )
            liquidity, liquidity_missing = _factor(
                [
                    _rank_score(stage_one, row, "avg_amount_20d_cny"),
                    _rank_score(stage_one, row, "turnover_20d_pct"),
                ],
                self._policy.missing_value_penalty,
            )
            catalyst, catalyst_missing = _factor(
                [row.catalyst_score], self._policy.missing_value_penalty
            )
            risk, risk_missing = _factor(
                [
                    100 - row.risk_score if row.risk_score is not None else None,
                    _rank_score(stage_one, row, "volatility_20d_pct", higher_is_better=False),
                ],
                self._policy.missing_value_penalty,
            )
            factor_scores = {
                "quality": quality,
                "value": value,
                "trend": trend,
                "liquidity": liquidity,
                "catalyst": catalyst,
                "risk": risk,
            }
            score = sum(
                factor_scores[name] * weight for name, weight in self._policy.weights.items()
            )
            missing = any(
                (
                    quality_missing,
                    value_missing,
                    trend_missing,
                    liquidity_missing,
                    catalyst_missing,
                    risk_missing,
                )
            )
            reason_codes = ["FACTOR_SCORE_V1"]
            if missing:
                reason_codes.append("MISSING_FACTOR_PENALTY_APPLIED")
            scored.append((row, round(score, 4), factor_scores, reason_codes))

        scored.sort(key=lambda item: (-item[1], item[0].symbol))
        factor_candidates = [
            ScreeningCandidate(
                symbol=row.symbol,
                name=row.name,
                exchange=row.exchange,
                industry=row.industry,
                stage="FACTOR_FILTER",
                rank=index,
                score=score,
                factor_scores=factors,
                reason_codes=reasons,
            )
            for index, (row, score, factors, reasons) in enumerate(
                scored[: self._policy.factor_target], start=1
            )
        ]
        warnings: list[str] = []
        if len(stage_one) < self._policy.hard_target:
            warnings.append("硬门合格标的少于 300；系统未降低门槛凑数")
        if len(factor_candidates) < self._policy.factor_target:
            warnings.append("可评分标的少于 30；系统未扩大 LLM 预算")
        return ScreeningRun(
            run_id=run_id,
            as_of=as_of.isoformat(timespec="seconds"),
            created_at=iso_now(),
            universe_count=len(rows),
            hard_filter_target=self._policy.hard_target,
            factor_filter_target=self._policy.factor_target,
            scoring_version=self._policy.scoring_version,
            policy_version=self._policy.version,
            input_hash=hashlib.sha256(_canonical_input(rows)).hexdigest(),
            hard_filter=hard_candidates,
            factor_filter=factor_candidates,
            exclusions=exclusions,
            warnings=warnings,
        )
