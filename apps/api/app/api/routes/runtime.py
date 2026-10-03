from __future__ import annotations

from typing import cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.core.settings import PROJECT_ROOT
from app.core.time import iso_now
from app.models import ApiEnvelope, ApiMeta
from app.runtime.service import DeterministicRuntimeService

router = APIRouter(prefix="/runtime", tags=["deterministic-runtime"])


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
async def runtime_status(request: Request) -> ApiEnvelope:
    service = cast(DeterministicRuntimeService, request.app.state.deterministic_runtime_service)
    status = await service.status()
    return _envelope(
        status.model_dump(mode="json"),
        ["默认 DISABLED：页面只读；不会启用 scheduler、LaunchAgent、cron、heartbeat 或模型调用。"],
    )


@router.get("/runs", response_model=ApiEnvelope)
async def runtime_runs(request: Request) -> ApiEnvelope:
    service = cast(DeterministicRuntimeService, request.app.state.deterministic_runtime_service)
    return _envelope(
        [item.model_dump(mode="json") for item in await service.repository.list_runs()]
    )


@router.get("/runs/{run_id}", response_model=ApiEnvelope)
async def runtime_run_detail(run_id: str, request: Request) -> ApiEnvelope:
    service = cast(DeterministicRuntimeService, request.app.state.deterministic_runtime_service)
    task_run = await service.repository.get(run_id)
    if task_run is None:
        raise HTTPException(status_code=404, detail="未找到确定性任务运行")
    return _envelope(task_run.model_dump(mode="json"))


@router.get("/runs/{run_id}/exception-package", response_class=FileResponse)
async def runtime_exception_package(run_id: str, request: Request) -> FileResponse:
    service = cast(DeterministicRuntimeService, request.app.state.deterministic_runtime_service)
    task_run = await service.repository.get(run_id)
    if task_run is None or not task_run.exception_package_path:
        raise HTTPException(status_code=404, detail="未找到确定性任务例外证据包")
    path = (PROJECT_ROOT / task_run.exception_package_path).resolve()
    if PROJECT_ROOT not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="例外证据包不可读取")
    return FileResponse(
        path,
        media_type="application/json",
        filename=f"{run_id}-exception-evidence.json",
    )
