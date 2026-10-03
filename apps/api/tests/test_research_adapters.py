import json
from pathlib import Path
from typing import Any

import pytest

from app.adapters.research import ResearchAdapterError, TradingAgentsAdapter, UZIAdapter
from app.core.settings import Settings


def _claim(identifier: str, evidence_id: str = "ev-001") -> dict[str, Any]:
    return {
        "claim_id": identifier,
        "claim_type": "INFERENCE",
        "text": "结构化测试论点",
        "evidence_ids": [evidence_id],
        "counter_evidence_ids": ["ev-002"],
        "confidence": 0.6,
    }


def _bundle() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "bundle_id": "bundle-0001",
        "symbol": "600000",
        "name": "浦发银行",
        "exchange": "SSE",
        "as_of": "2026-08-25T15:00:00+08:00",
        "data_cutoff": "2026-08-25T15:00:00+08:00",
        "created_at": "2026-08-25T16:00:00+08:00",
        "evidence": [
            {
                "evidence_id": identifier,
                "domain": domain,
                "statement": "结构化测试证据",
                "source_id": "FIXTURE",
                "source_tier": "P0",
                "source_uri": f"local://fixture/{identifier}",
                "published_at": "2026-08-25T14:00:00+08:00",
                "as_of": "2026-08-25T14:00:00+08:00",
                "retrieved_at": "2026-08-25T15:10:00+08:00",
                "quality": "A",
                "independent_event_id": identifier,
                "content_hash": character * 64,
            }
            for identifier, domain, character in (
                ("ev-001", "FILING", "a"),
                ("ev-002", "MARKET", "b"),
            )
        ],
        "conflicts": [],
        "missing_domains": [],
        "content_hash": "c" * 64,
    }


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    return path


def test_trading_agents_adapter_enforces_seven_roles_and_evidence(tmp_path: Path) -> None:
    roles = [
        "MARKET",
        "FUNDAMENTAL",
        "NEWS",
        "SENTIMENT",
        "POLICY",
        "HOT_MONEY",
        "UNLOCK_REDUCTION",
    ]
    result: dict[str, Any] = {
        "schema_version": "1.0.0",
        "run_id": "adapter-run-0001",
        "bundle_id": "bundle-0001",
        "symbol": "600000",
        "as_of": "2026-08-25T15:00:00+08:00",
        "adapter_version": "test-v1",
        "roles": [
            {"role": role, "claims": [_claim(f"claim-{index}")], "unknowns": []}
            for index, role in enumerate(roles)
        ],
        "bull_case": [_claim("bull-1"), _claim("bull-2")],
        "bear_case": [_claim("bear-1"), _claim("bear-2")],
        "risk_debate": [_claim("risk-1"), _claim("risk-2"), _claim("risk-3")],
        "manager_synthesis": [_claim("manager-1")],
    }
    adapter = TradingAgentsAdapter(Settings())
    bundle_path = _write(tmp_path / "bundle.json", _bundle())
    result_path = _write(tmp_path / "result.json", result)
    assert adapter.import_result(bundle_path, result_path)["symbol"] == "600000"
    result["bull_case"][0] = _claim("bull-bad", "ev-missing")
    _write(result_path, result)
    with pytest.raises(ResearchAdapterError, match="未知证据"):
        adapter.import_result(bundle_path, result_path)


def test_uzi_adapter_rejects_non_top5_or_automatic_trigger(tmp_path: Path) -> None:
    result = {
        "schema_version": "1.0.0",
        "run_id": "uzi-run-0001",
        "bundle_id": "bundle-0001",
        "symbol": "600000",
        "as_of": "2026-08-25T15:00:00+08:00",
        "adapter_version": "test-v1",
        "manual_trigger": True,
        "candidate_rank": 3,
        "questions": ["财务质量是否可持续？"],
        "findings": [
            {
                "text": "测试发现",
                "classification": "INFERENCE",
                "evidence_ids": ["ev-001"],
                "confidence": 0.6,
            }
        ],
        "new_evidence_ids": [],
        "duplicate_evidence_ids": ["ev-001"],
        "conflicting_evidence_ids": [],
        "remaining_unknowns": [],
    }
    adapter = UZIAdapter(Settings())
    bundle_path = _write(tmp_path / "bundle.json", _bundle())
    result_path = _write(tmp_path / "result.json", result)
    assert adapter.import_result(bundle_path, result_path)["candidate_rank"] == 3
    result["candidate_rank"] = 6
    _write(result_path, result)
    with pytest.raises(ResearchAdapterError, match="maximum"):
        adapter.import_result(bundle_path, result_path)
