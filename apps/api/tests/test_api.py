from typing import Any, cast

import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_health_forbids_trading_and_llm_api() -> None:
    transport = httpx.ASGITransport(app=cast(Any, app))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["real_trading_enabled"] is False
    assert payload["llm_api_enabled"] is False
