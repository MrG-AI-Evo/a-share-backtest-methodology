import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from app.backtests.historical import HistoricalDataRepository


@pytest.mark.asyncio
async def test_parquet_import_validates_hash_columns_and_fixture_is_not_formal(
    tmp_path: Path,
) -> None:
    parquet_dir = tmp_path / "data" / "backtests" / "parquet"
    parquet_dir.mkdir(parents=True)
    parquet_path = parquet_dir / "prices-fixture.parquet"
    with duckdb.connect() as db:
        db.execute(
            """
            CREATE TABLE fixture AS SELECT
                '601288'::VARCHAR AS symbol, 'SSE'::VARCHAR AS exchange,
                '2016-01-04'::DATE AS effective_date,
                '2016-01-04T15:01:00+08:00'::TIMESTAMPTZ AS known_at,
                3.10::DECIMAL(18,4) AS open, 3.20::DECIMAL(18,4) AS high,
                3.00::DECIMAL(18,4) AS low, 3.15::DECIMAL(18,4) AS close,
                1000000::BIGINT AS volume, 3150000::DECIMAL(22,2) AS amount,
                'fixture'::VARCHAR AS source_id, 'local://fixture'::VARCHAR AS source_uri,
                false::BOOLEAN AS is_synthetic
            """
        )
        db.execute("COPY fixture TO ? (FORMAT PARQUET)", [str(parquet_path)])
    digest = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    columns = [
        "symbol",
        "exchange",
        "effective_date",
        "known_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
        "source_id",
        "source_uri",
        "is_synthetic",
    ]
    manifest = {
        "schema_version": "1.0.0",
        "manifest_id": "20160104-prices-test-001",
        "dataset": "prices",
        "raw_data_mode": "TEST_FIXTURE",
        "file_path": "data/backtests/parquet/prices-fixture.parquet",
        "sha256": digest,
        "row_count": 1,
        "minimum_effective_date": "2016-01-04",
        "maximum_effective_date": "2016-01-04",
        "columns": columns,
        "source_ids": ["fixture"],
        "license": {
            "usage_scope": "LOCAL_RESEARCH_BACKTEST",
            "redistribution_allowed": False,
            "evidence_uri": "local://fixture-license",
        },
        "retrieved_at": "2026-08-27T10:00:00+08:00",
        "created_at": "2026-08-27T10:00:00+08:00",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    repository = HistoricalDataRepository(
        tmp_path / "state" / "backtests.duckdb",
        parquet_dir,
        project_root=tmp_path,
    )
    await repository.initialize()
    with pytest.raises(RuntimeError, match="测试夹具"):
        await repository.import_manifest(manifest_path)
    result = await repository.import_manifest(manifest_path, allow_test_fixture=True)
    assert result["row_count"] == 1
    coverage = await repository.coverage("2016-01-04", "2025-12-31")
    prices = next(item for item in coverage if item.dataset == "prices")
    assert prices.row_count == 1
    assert prices.ready_for_primary_window is False
    with pytest.raises(RuntimeError, match="禁止重复"):
        await repository.import_manifest(manifest_path, allow_test_fixture=True)


@pytest.mark.asyncio
async def test_parquet_import_rejects_hash_mismatch(tmp_path: Path) -> None:
    parquet_dir = tmp_path / "data" / "backtests" / "parquet"
    parquet_dir.mkdir(parents=True)
    parquet_path = parquet_dir / "prices-fixture.parquet"
    parquet_path.write_bytes(b"not parquet")
    manifest = {
        "schema_version": "1.0.0",
        "manifest_id": "20160104-prices-test-bad",
        "dataset": "prices",
        "raw_data_mode": "TEST_FIXTURE",
        "file_path": "data/backtests/parquet/prices-fixture.parquet",
        "sha256": "0" * 64,
        "row_count": 1,
        "minimum_effective_date": "2016-01-04",
        "maximum_effective_date": "2016-01-04",
        "columns": ["symbol", "exchange", "effective_date", "known_at", "source_id"],
        "source_ids": ["fixture"],
        "license": {
            "usage_scope": "LOCAL_RESEARCH_BACKTEST",
            "redistribution_allowed": False,
            "evidence_uri": "local://fixture",
        },
        "retrieved_at": "2026-08-27T10:00:00+08:00",
        "created_at": "2026-08-27T10:00:00+08:00",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    repository = HistoricalDataRepository(tmp_path / "state.duckdb", parquet_dir, tmp_path)
    await repository.initialize()
    with pytest.raises(RuntimeError, match="哈希不一致"):
        await repository.import_manifest(manifest_path, allow_test_fixture=True)
