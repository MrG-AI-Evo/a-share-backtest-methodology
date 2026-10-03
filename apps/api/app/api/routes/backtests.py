from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from jsonschema import Draft202012Validator, FormatChecker

from app.backtests.service import BacktestService
from app.core.time import iso_now
from app.models import ApiEnvelope, ApiMeta

router = APIRouter(prefix="/backtests", tags=["backtests"])
PROJECT_ROOT = Path(__file__).resolve().parents[5]
QUALITY_SAMPLE_REPORT = PROJECT_ROOT / "reports/backtests/quality-sample-11-latest.json"
QUALITY_SAMPLE_SCHEMA = PROJECT_ROOT / "schemas/quality-sample-backtest-report.schema.json"


def _envelope(data: object, warnings: list[str] | None = None) -> ApiEnvelope:
    return ApiEnvelope(
        data=data,
        meta=ApiMeta(
            request_id=str(uuid4()),
            generated_at=iso_now(),
            data_state="live",
            warnings=warnings or [],
        ),
    )


@router.get("", response_model=ApiEnvelope)
async def list_backtests(request: Request) -> ApiEnvelope:
    service = cast(BacktestService, request.app.state.backtest_service)
    runs = await service.list_runs()
    return _envelope([item.model_dump(mode="json") for item in runs])


@router.get("/readiness", response_model=ApiEnvelope)
async def backtest_readiness(request: Request) -> ApiEnvelope:
    service = cast(BacktestService, request.app.state.backtest_service)
    readiness = await service.readiness()
    warnings = []
    if not readiness.formal_backtest_executable:
        warnings.append("正式十年回测被硬门阻断；未生成任何绩效结果")
    return _envelope(readiness.model_dump(mode="json"), warnings)


@router.get("/reports/quality-sample-11", response_model=ApiEnvelope)
async def quality_sample_report() -> ApiEnvelope:
    if not QUALITY_SAMPLE_REPORT.is_file():
        raise HTTPException(status_code=404, detail="尚未生成11股机制测试可视化报告")
    try:
        payload = json.loads(QUALITY_SAMPLE_REPORT.read_text("utf-8"))
        schema = json.loads(QUALITY_SAMPLE_SCHEMA.read_text("utf-8"))
        errors = sorted(
            Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
            key=lambda item: list(item.path),
        )
        if errors:
            raise ValueError(errors[0].message)
        source = (
            PROJECT_ROOT
            / "data/backtests/staging"
            / payload["source_run"]["run_id"]
            / "quality-sample-11-result.json"
        )
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != payload["source_run"]["sha256"]:
            raise ValueError("source result hash mismatch")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="11股机制测试报告未通过完整性校验") from exc
    return _envelope(payload, ["该报告仅用于机制测试，存在事后样本选择偏差，不属于正式十年绩效。"])


@router.get("/{run_id}", response_model=ApiEnvelope)
async def backtest_detail(
    run_id: str,
    request: Request,
    event_offset: int = Query(0, ge=0),
    event_limit: int = Query(100, ge=1, le=500),
    checkpoint_offset: int = Query(0, ge=0),
    checkpoint_limit: int = Query(100, ge=1, le=500),
) -> ApiEnvelope:
    service = cast(BacktestService, request.app.state.backtest_service)
    detail = await service.detail(
        run_id,
        event_offset=event_offset,
        event_limit=event_limit,
        checkpoint_offset=checkpoint_offset,
        checkpoint_limit=checkpoint_limit,
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="未找到该回测运行")
    return _envelope(detail.model_dump(mode="json"))
