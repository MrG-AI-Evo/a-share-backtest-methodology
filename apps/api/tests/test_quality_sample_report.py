"""Report integrity tests use temporary synthetic contracts, never old market results."""

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from app.api.routes import backtests
from app.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["valid", "missing", "invalid_schema", "tampered", "missing_source"]
)
async def test_quality_report_without_external_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    run_id = "mechanism-fixture-only"
    report = tmp_path / "report.json"
    source = tmp_path / "data/backtests/staging" / run_id / "quality-sample-11-result.json"
    source.parent.mkdir(parents=True)
    raw = b'{"fixture":true,"not_market_data":true}'
    source.write_bytes(raw)
    payload: dict[str, Any] = {
        "schema_version": "1.0.0",
        "report_id": "synthetic-contract-test",
        "status": "MECHANISM_TEST_ONLY",
        "generated_at": "2020-01-02T15:00:00+08:00",
        "source_run": {
            "run_id": run_id,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "policy_version": "TEST_FIXTURE",
            "start_date": "2020-01-02",
            "end_date": "2020-01-02",
            "as_of": "2020-01-02T15:00:00+08:00",
        },
        "portfolio": {},
        "annual": [],
        "stocks": [{"symbol": "TEST_FIXTURE"}],
        "data_quality": {"fixture_only": True},
        "audit": {"fixture_only": True},
        "formal_blockers": ["TEST_FIXTURE_NOT_PERFORMANCE"],
    }
    if mode == "invalid_schema":
        payload["status"] = "FORMAL_RESULT"
    if mode != "missing":
        report.write_text(json.dumps(payload), "utf-8")
    if mode == "tampered":
        source.write_bytes(b'{"fixture":true,"changed":true}')
    if mode == "missing_source":
        source.unlink()
    monkeypatch.setattr(backtests, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(backtests, "QUALITY_SAMPLE_REPORT", report)
    transport = httpx.ASGITransport(app=cast(Any, app))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/backtests/reports/quality-sample-11")
    expected = 200 if mode == "valid" else 404 if mode == "missing" else 503
    assert response.status_code == expected
    if mode == "valid":
        assert response.json()["data"] == payload
        assert response.json()["meta"]["warnings"]
    else:
        assert "data" not in response.json()
