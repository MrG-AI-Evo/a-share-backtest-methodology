import json
from datetime import date
from pathlib import Path

import pytest

from app.core.settings import Settings
from app.models import (
    SourceMeta,
    UniverseCoverage,
    UniverseSecurity,
    UniverseSnapshot,
)
from app.repositories.universe import UniverseArtifactError, UniverseRepository
from app.services.universe_collection import (
    _average_amount,
    _momentum,
    _risk_status,
    _trading_status,
    _volatility,
)
from app.sources.universe import BseOfficialListSource, DailyHistory, SnapshotQuote


def test_universe_metrics_and_statuses_are_deterministic() -> None:
    closes = [float(value) for value in range(100, 181)]
    history = DailyHistory(
        dates=[date(2026, 1, 1)] * len(closes),
        closes=closes,
        amounts_cny=[60_000_000.0] * len(closes),
        source_id="fixture:bars",
    )
    assert _risk_status("*ST样本") == "ST"
    assert _risk_status("样本退") == "DELISTING"
    assert _risk_status("正常样本") == "NORMAL"
    assert _trading_status(None) == "UNKNOWN"
    assert _trading_status(SnapshotQuote("600000", 10.0, None, "15:00")) == "SUSPENDED"
    assert _momentum(closes, 20) == pytest.approx(12.5)
    assert _average_amount(history) == 60_000_000
    assert _volatility(closes) is not None


def test_bse_official_jsonp_parser_fails_closed() -> None:
    parsed = BseOfficialListSource._parse('null([{"totalPages":1,"content":[]}])')
    assert parsed["totalPages"] == 1
    with pytest.raises(RuntimeError, match="不是预期 JSONP"):
        BseOfficialListSource._parse("<html>blocked</html>")


def test_universe_snapshot_is_append_only_and_schema_validated(tmp_path: Path) -> None:
    settings = Settings(
        normalized_dir=tmp_path / "data" / "normalized",
        schema_dir=Path(__file__).resolve().parents[3] / "schemas",
    )
    source = SourceMeta(
        provider="fixture",
        source_url="https://example.test",
        fetched_at="2026-08-25T16:00:00+08:00",
        source_timestamp="2026-08-25T15:00:00+08:00",
        state="delayed",
    )
    row = UniverseSecurity(
        symbol="600000",
        name="浦发银行",
        exchange="SSE",
        risk_status="NORMAL",
        trading_status="NORMAL",
        history_days=160,
        last_price=10,
        avg_amount_20d_cny=100_000_000,
        data_quality="B",
        as_of="2026-08-25T15:00:00+08:00",
        source_ids=["fixture"],
    )
    snapshot = UniverseSnapshot(
        run_id="20260825-150000-universe-test",
        as_of="2026-08-25T15:00:00+08:00",
        retrieved_at="2026-08-25T16:00:00+08:00",
        coverage=UniverseCoverage(
            listed_count_by_exchange={"SSE": 1, "SZSE": 1, "BSE": 1},
            quote_count_by_exchange={"SSE": 1, "SZSE": 1, "BSE": 1},
            enriched_count_by_exchange={"SSE": 1, "SZSE": 0, "BSE": 0},
            quote_coverage_by_exchange={"SSE": 1, "SZSE": 1, "BSE": 1},
            screenable_count=1,
            complete_listing=True,
            formal_screening_eligible=False,
        ),
        sources=[source, source, source, source],
        rows=[row],
    )
    repository = UniverseRepository(settings)
    persisted = repository.append(snapshot)
    assert persisted.artifact_path is not None
    payload = json.loads(
        (settings.normalized_dir / "universe" / "2026-08-25" / f"{snapshot.run_id}.json").read_text(
            "utf-8"
        )
    )
    assert payload["coverage"]["formal_screening_eligible"] is False
    with pytest.raises(UniverseArtifactError, match="禁止覆盖"):
        repository.append(snapshot)
