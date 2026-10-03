from datetime import datetime
from pathlib import Path

import pytest

from app.core.settings import Settings
from app.core.time import SHANGHAI
from app.models import ScreeningInvalidation, UniverseSecurity
from app.repositories.screening import ScreeningArtifactError, ScreeningRepository
from app.services.screening import ScreeningService


def _row(index: int) -> UniverseSecurity:
    symbol = f"{index + 600000:06d}"
    return UniverseSecurity(
        symbol=symbol,
        name=f"样本{index}",
        exchange="SSE",
        industry=f"行业{index % 10}",
        risk_status="NORMAL",
        trading_status="NORMAL",
        history_days=240,
        last_price=10 + index / 100,
        avg_amount_20d_cny=60_000_000 + index * 1_000_000,
        turnover_20d_pct=1 + index % 20,
        roe_ttm_pct=5 + index % 25,
        operating_cashflow_profit_ratio=0.5 + (index % 30) / 20,
        pe_ttm=8 + index % 40,
        pb=0.8 + (index % 20) / 10,
        momentum_20d_pct=-10 + index % 30,
        momentum_60d_pct=-15 + index % 45,
        relative_strength_20d_pct=-8 + index % 25,
        volatility_20d_pct=10 + index % 20,
        catalyst_score=float(index % 100),
        risk_score=float(index % 50),
        data_quality="B",
        as_of="2026-08-25T15:00:00+08:00",
        source_ids=["fixture:market", "fixture:fundamental"],
    )


def test_screening_is_deterministic_and_enforces_300_to_30(tmp_path: Path) -> None:
    settings = Settings(
        screening_dir=tmp_path / "data" / "screening",
        screening_invalidation_dir=tmp_path / "data" / "screening-invalidations",
    )
    rows = [_row(index) for index in range(340)]
    rows[0] = rows[0].model_copy(update={"risk_status": "ST"})
    rows[1] = rows[1].model_copy(update={"trading_status": "SUSPENDED"})
    rows[2] = rows[2].model_copy(update={"avg_amount_20d_cny": 1_000_000})
    service = ScreeningService(settings)
    as_of = datetime(2026, 8, 25, 15, 0, tzinfo=SHANGHAI)
    first = service.run(rows, as_of, "20260825-150000-screening-test")
    second = service.run(list(reversed(rows)), as_of, "20260825-150001-screening-test")
    assert len(first.hard_filter) == 300
    assert len(first.factor_filter) == 30
    assert first.input_hash == second.input_hash
    assert [item.symbol for item in first.factor_filter] == [
        item.symbol for item in second.factor_filter
    ]
    reasons = {reason for item in first.exclusions for reason in item.reason_codes}
    assert {"RISK_STATUS_ST", "TRADING_STATUS_SUSPENDED", "INSUFFICIENT_LIQUIDITY"} <= reasons

    repository = ScreeningRepository(settings)
    persisted = repository.append(first)
    assert persisted.artifact_path is not None
    assert repository.latest() is not None
    with pytest.raises(ScreeningArtifactError, match="禁止覆盖"):
        repository.append(first)

    repository.invalidate(
        ScreeningInvalidation(
            invalidation_id="20260825-screening-invalidation-test",
            run_id=first.run_id,
            invalidated_at="2026-08-25T16:00:00+08:00",
            reason="测试验证追加失效记录后 latest 会跳过旧 run",
        )
    )
    assert repository.latest() is None
