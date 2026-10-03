from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.time import iso_now

router = APIRouter(tags=["system"])


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    return {
        "status": "ok",
        "service": "a-share-terminal-api",
        "version": request.app.version,
        "timestamp": iso_now(),
        "real_trading_enabled": False,
        "llm_api_enabled": False,
    }
