import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.cli.soak_check import ENDPOINTS, _interrupted_summary, _write_summary, run_soak


def _healthy_payload(endpoint: str) -> dict[str, object]:
    if endpoint == "/health":
        return {
            "status": "ok",
            "real_trading_enabled": False,
            "llm_api_enabled": False,
        }
    if endpoint == "/api/v1/system":
        return {
            "data": {
                "live_broker_enabled": False,
                "automatic_trading_enabled": False,
                "llm_api_enabled": False,
                "ledger_reconciled": True,
                "ledger_reconciliation_errors": [],
                "latest_screening_run_id": "screening-run",
            }
        }
    if endpoint == "/api/v1/pipeline":
        return {
            "data": {
                "llm_api_enabled": False,
                "uzi_daily_full_market_enabled": False,
                "latest_screening": {
                    "hard_filter": [{"symbol": "600000"}],
                    "factor_filter": [{"symbol": "600000"}],
                },
            }
        }
    if endpoint == "/api/v1/backtests/readiness":
        return {
            "data": {
                "base_policy_sha256_verified": True,
                "engineering_ready": True,
                "formal_backtest_executable": False,
                "primary_window": {
                    "start_date": "2016-01-04",
                    "end_date": "2025-12-31",
                    "included_in_primary_performance": True,
                },
                "extension_window": {
                    "start_date": "2026-01-01",
                    "end_date": "2026-08-24",
                    "included_in_primary_performance": False,
                },
                "blockers": [{"blocker_id": "CD_HISTORY", "status": "OPEN"}],
                "safety": {
                    "real_broker_connected": False,
                    "real_orders_enabled": False,
                    "llm_api_enabled": False,
                    "synthetic_raw_data_allowed_in_formal_run": False,
                    "primary_and_extension_results_separated": True,
                },
            }
        }
    if endpoint == "/api/v1/backtests":
        return {
            "data": [
                {
                    "run_id": "blocked-run",
                    "status": "BLOCKED",
                    "performance_available": False,
                }
            ]
        }
    return {"data": {}}


def _mock_healthy_endpoints() -> None:
    for endpoint in ENDPOINTS:
        respx.get(f"http://local.test{endpoint}").mock(
            return_value=Response(200, json=_healthy_payload(endpoint))
        )


@respx.mock
async def test_soak_check_persists_every_probe(tmp_path: Path) -> None:
    _mock_healthy_endpoints()
    evidence = tmp_path / "soak.jsonl"
    result = await run_soak(
        "http://local.test",
        duration_seconds=0.02,
        interval_seconds=0.01,
        evidence_path=evidence,
    )
    lines = evidence.read_text("utf-8").splitlines()
    assert result["passed"] is True
    assert result["probe_count"] == len(lines)
    # Probe latency can legitimately consume the entire tiny test duration.
    assert len(lines) >= 1
    assert all(set(json.loads(line)["endpoints"].values()) == {200} for line in lines)
    assert all(json.loads(line)["semantic_failures"] == [] for line in lines)


@respx.mock
async def test_soak_check_fails_closed_on_any_endpoint_failure(tmp_path: Path) -> None:
    _mock_healthy_endpoints()
    respx.get("http://local.test/api/v1/system").mock(return_value=Response(503))
    result = await run_soak(
        "http://local.test",
        duration_seconds=0.01,
        interval_seconds=0.01,
        evidence_path=tmp_path / "failed.jsonl",
    )
    assert result["passed"] is False
    failure_count = result["failure_count"]
    assert isinstance(failure_count, int)
    assert failure_count >= 1


@respx.mock
async def test_soak_check_fails_closed_on_unsafe_semantics_with_http_200(tmp_path: Path) -> None:
    _mock_healthy_endpoints()
    payload = _healthy_payload("/api/v1/system")
    data = payload["data"]
    assert isinstance(data, dict)
    data["live_broker_enabled"] = True
    data["ledger_reconciled"] = False
    respx.get("http://local.test/api/v1/system").mock(return_value=Response(200, json=payload))
    result = await run_soak(
        "http://local.test",
        duration_seconds=0.01,
        interval_seconds=0.01,
        evidence_path=tmp_path / "unsafe.jsonl",
    )
    assert result["passed"] is False
    assert result["status_failure_count"] == 0
    semantic_failures = result["semantic_failure_count"]
    assert isinstance(semantic_failures, int)
    assert semantic_failures >= 2


@respx.mock
async def test_soak_check_fails_closed_on_backtest_safety_or_fake_performance(
    tmp_path: Path,
) -> None:
    _mock_healthy_endpoints()
    readiness = _healthy_payload("/api/v1/backtests/readiness")
    readiness_data = readiness["data"]
    assert isinstance(readiness_data, dict)
    safety = readiness_data["safety"]
    assert isinstance(safety, dict)
    safety["real_orders_enabled"] = True
    respx.get("http://local.test/api/v1/backtests/readiness").mock(
        return_value=Response(200, json=readiness)
    )
    respx.get("http://local.test/api/v1/backtests").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "run_id": "blocked-run",
                        "status": "BLOCKED",
                        "performance_available": True,
                    }
                ]
            },
        )
    )
    result = await run_soak(
        "http://local.test",
        duration_seconds=0.01,
        interval_seconds=0.01,
        evidence_path=tmp_path / "unsafe-backtest.jsonl",
    )
    assert result["passed"] is False
    assert result["status_failure_count"] == 0
    assert isinstance(result["semantic_failure_count"], int)
    assert result["semantic_failure_count"] >= 2


@respx.mock
async def test_soak_check_includes_web_list_and_detail_pages(tmp_path: Path) -> None:
    _mock_healthy_endpoints()
    respx.get("http://web.test/").mock(return_value=Response(200, text="dashboard"))
    respx.get("http://web.test/backtests").mock(return_value=Response(200, text="backtests"))
    respx.get("http://web.test/backtests/blocked-run").mock(
        return_value=Response(200, text="blocked detail")
    )
    evidence = tmp_path / "web-soak.jsonl"
    result = await run_soak(
        "http://local.test",
        duration_seconds=0.01,
        interval_seconds=0.01,
        evidence_path=evidence,
        web_base_url="http://web.test",
        web_paths=("/", "/backtests", "/backtests/blocked-run"),
    )
    assert result["passed"] is True
    probe = json.loads(evidence.read_text("utf-8").splitlines()[0])
    assert probe["endpoints"]["WEB:/"] == 200
    assert probe["endpoints"]["WEB:/backtests"] == 200
    assert probe["endpoints"]["WEB:/backtests/blocked-run"] == 200


@respx.mock
async def test_soak_evidence_is_append_only_per_run_id(tmp_path: Path) -> None:
    _mock_healthy_endpoints()
    evidence = tmp_path / "existing.jsonl"
    evidence.write_text('{"existing": true}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="禁止覆盖"):
        await run_soak(
            "http://local.test",
            duration_seconds=0.01,
            interval_seconds=0.01,
            evidence_path=evidence,
        )


def test_interrupted_and_duplicate_summary_can_never_pass(tmp_path: Path) -> None:
    evidence = tmp_path / "interrupted.jsonl"
    evidence.write_text(
        json.dumps(
            {
                "at": "2026-08-25T18:30:00+08:00",
                "endpoints": {
                    "/health": 200,
                    "/api/v1/system": 200,
                    "/api/v1/pipeline": 200,
                    "/api/v1/dashboard": 200,
                    "/api/v1/paper": 200,
                },
                "semantic_failures": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary_path = tmp_path / "interrupted.summary.json"
    summary = _interrupted_summary(evidence, "interrupted-run", summary_path, 86_400, 60)
    assert summary["passed"] is False
    assert summary["interrupted"] is True
    _write_summary(summary_path, summary)
    with pytest.raises(RuntimeError, match="禁止覆盖"):
        _write_summary(summary_path, summary)
