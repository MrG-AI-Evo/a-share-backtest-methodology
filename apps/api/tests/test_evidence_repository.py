from __future__ import annotations

from pathlib import Path

import pytest

from app.core.settings import Settings
from app.repositories.evidence import EvidenceBundleError, EvidenceBundleRepository


def _draft() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "bundle_id": "bundle-test-0001",
        "symbol": "600519",
        "name": "贵州茅台",
        "exchange": "SSE",
        "as_of": "2026-08-25T15:00:00+08:00",
        "data_cutoff": "2026-08-25T15:00:00+08:00",
        "created_at": "2026-08-25T15:01:00+08:00",
        "evidence": [
            {
                "evidence_id": "ev-market-1",
                "domain": "MARKET",
                "statement": "日线数据截至 2026-08-25 收盘。",
                "source_id": "mootdx:tdx-public",
                "source_tier": "P2",
                "source_uri": "tcp://public-tdx-server:7709",
                "published_at": None,
                "as_of": "2026-08-25T15:00:00+08:00",
                "retrieved_at": "2026-08-25T15:01:00+08:00",
                "quality": "B",
                "independent_event_id": None,
            }
        ],
        "conflicts": [],
        "missing_domains": ["FUNDAMENTAL"],
    }


def test_evidence_bundle_is_hashed_and_append_only(tmp_path: Path) -> None:
    settings = Settings(evidence_dir=tmp_path / "evidence")
    repository = EvidenceBundleRepository(settings)
    target = repository.import_draft(_draft())
    assert target.exists()
    assert repository.import_draft(_draft()) == target


def test_evidence_bundle_rejects_future_cutoff(tmp_path: Path) -> None:
    settings = Settings(evidence_dir=tmp_path / "evidence")
    repository = EvidenceBundleRepository(settings)
    draft = _draft()
    draft["data_cutoff"] = "2026-08-25T16:00:00+08:00"
    with pytest.raises(EvidenceBundleError, match="data_cutoff"):
        repository.import_draft(draft)
