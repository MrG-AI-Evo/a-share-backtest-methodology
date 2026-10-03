from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

import duckdb
from jsonschema import Draft202012Validator, FormatChecker

from app.backtests.models import DatasetCoverage
from app.core.settings import PROJECT_ROOT

REQUIRED_DATASETS = (
    "prices",
    "security_master",
    "security_status",
    "dividends",
    "dividend_fiscal_years",
    "financial_facts",
    "corporate_actions",
    "cd_rates",
    "fee_rules",
)

TABLE_DDL = {
    "prices": """
        CREATE TABLE IF NOT EXISTS bt_prices (
            symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
            effective_date DATE NOT NULL, known_at TIMESTAMPTZ NOT NULL,
            open DECIMAL(18,4), high DECIMAL(18,4), low DECIMAL(18,4),
            close DECIMAL(18,4), volume BIGINT, amount DECIMAL(22,2),
            source_id VARCHAR NOT NULL, source_uri VARCHAR,
            is_synthetic BOOLEAN NOT NULL DEFAULT false
        )
    """,
    "security_master": """
        CREATE TABLE IF NOT EXISTS bt_security_master (
            symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
            effective_date DATE NOT NULL, known_at TIMESTAMPTZ NOT NULL,
            name VARCHAR NOT NULL, asset_type VARCHAR NOT NULL,
            ownership_type VARCHAR NOT NULL, listing_date DATE, delisting_date DATE,
            source_id VARCHAR NOT NULL, source_uri VARCHAR,
            is_synthetic BOOLEAN NOT NULL DEFAULT false
        )
    """,
    "security_status": """
        CREATE TABLE IF NOT EXISTS bt_security_status (
            symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
            effective_date DATE NOT NULL, known_at TIMESTAMPTZ NOT NULL,
            risk_status VARCHAR NOT NULL, trading_status VARCHAR NOT NULL,
            source_id VARCHAR NOT NULL, source_uri VARCHAR,
            is_synthetic BOOLEAN NOT NULL DEFAULT false
        )
    """,
    "dividends": """
        CREATE TABLE IF NOT EXISTS bt_dividends (
            symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
            effective_date DATE NOT NULL, known_at TIMESTAMPTZ NOT NULL,
            fiscal_year INTEGER NOT NULL, announcement_date DATE NOT NULL,
            ex_date DATE, pay_date DATE, cash_per_share DECIMAL(18,6) NOT NULL,
            dividend_kind VARCHAR NOT NULL, source_id VARCHAR NOT NULL, source_uri VARCHAR,
            is_synthetic BOOLEAN NOT NULL DEFAULT false
        )
    """,
    "dividend_fiscal_years": """
        CREATE TABLE IF NOT EXISTS bt_dividend_fiscal_years (
            symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
            effective_date DATE NOT NULL, known_at TIMESTAMPTZ NOT NULL,
            fiscal_year INTEGER NOT NULL, total_cash_per_share DECIMAL(18,6) NOT NULL,
            fully_resolved BOOLEAN NOT NULL,
            source_id VARCHAR NOT NULL, source_uri VARCHAR,
            is_synthetic BOOLEAN NOT NULL DEFAULT false
        )
    """,
    "financial_facts": """
        CREATE TABLE IF NOT EXISTS bt_financial_facts (
            symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
            effective_date DATE NOT NULL, known_at TIMESTAMPTZ NOT NULL,
            report_period DATE NOT NULL, profile VARCHAR NOT NULL,
            metric VARCHAR NOT NULL, value DECIMAL(24,8), unit VARCHAR NOT NULL,
            source_id VARCHAR NOT NULL, source_uri VARCHAR,
            is_synthetic BOOLEAN NOT NULL DEFAULT false
        )
    """,
    "corporate_actions": """
        CREATE TABLE IF NOT EXISTS bt_corporate_actions (
            symbol VARCHAR NOT NULL, exchange VARCHAR NOT NULL,
            effective_date DATE NOT NULL, known_at TIMESTAMPTZ NOT NULL,
            action_type VARCHAR NOT NULL, share_multiplier DECIMAL(18,8),
            cash_per_share DECIMAL(18,6), terms_json VARCHAR NOT NULL,
            source_id VARCHAR NOT NULL, source_uri VARCHAR,
            is_synthetic BOOLEAN NOT NULL DEFAULT false
        )
    """,
    "cd_rates": """
        CREATE TABLE IF NOT EXISTS bt_cd_rates (
            bank VARCHAR NOT NULL, tenor_years INTEGER NOT NULL,
            effective_date DATE NOT NULL, known_at TIMESTAMPTZ NOT NULL,
            effective_end_date DATE, annual_rate DECIMAL(12,8) NOT NULL,
            minimum_purchase_cny DECIMAL(22,2), issue_window VARCHAR,
            early_withdrawal_terms VARCHAR, source_id VARCHAR NOT NULL, source_uri VARCHAR,
            is_synthetic BOOLEAN NOT NULL DEFAULT false
        )
    """,
    "fee_rules": """
        CREATE TABLE IF NOT EXISTS bt_fee_rules (
            venue VARCHAR NOT NULL, side VARCHAR NOT NULL,
            effective_date DATE NOT NULL, known_at TIMESTAMPTZ NOT NULL,
            effective_end_date DATE, component VARCHAR NOT NULL,
            rate DECIMAL(16,10), minimum_cny DECIMAL(18,4),
            rule_json VARCHAR NOT NULL, source_id VARCHAR NOT NULL, source_uri VARCHAR,
            is_synthetic BOOLEAN NOT NULL DEFAULT false
        )
    """,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_dataset_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("数据清单根节点必须为对象")
    schema = json.loads(
        (PROJECT_ROOT / "schemas" / "backtest-dataset-manifest.schema.json").read_text("utf-8")
    )
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        where = ".".join(str(value) for value in errors[0].path) or "root"
        raise RuntimeError(f"数据清单不符合 Schema: {where}: {errors[0].message}")
    return cast(dict[str, Any], payload)


class HistoricalDataRepository:
    def __init__(
        self, database: Path, parquet_dir: Path, project_root: Path = PROJECT_ROOT
    ) -> None:
        self.database = database
        self.parquet_dir = parquet_dir
        self.project_root = project_root

    async def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with duckdb.connect(str(self.database)) as db:
            for ddl in TABLE_DDL.values():
                db.execute(ddl)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bt_dataset_manifests (
                    manifest_id VARCHAR PRIMARY KEY,
                    dataset VARCHAR NOT NULL,
                    parquet_path VARCHAR NOT NULL,
                    sha256 VARCHAR NOT NULL,
                    row_count BIGINT NOT NULL,
                    minimum_effective_date DATE,
                    maximum_effective_date DATE,
                    raw_data_mode VARCHAR NOT NULL,
                    validated_at TIMESTAMPTZ NOT NULL
                )
                """
            )

    async def coverage(self, start_date: str, end_date: str) -> list[DatasetCoverage]:
        return await asyncio.to_thread(self._coverage_sync, start_date, end_date)

    async def import_manifest(
        self,
        manifest_path: Path,
        *,
        allow_test_fixture: bool = False,
    ) -> dict[str, object]:
        manifest = load_dataset_manifest(manifest_path)
        if manifest["raw_data_mode"] != "REAL_POINT_IN_TIME" and not allow_test_fixture:
            raise RuntimeError("正式导入只接受 REAL_POINT_IN_TIME；测试夹具必须显式授权")
        return await asyncio.to_thread(self._import_manifest_sync, manifest)

    def _import_manifest_sync(self, manifest: dict[str, Any]) -> dict[str, object]:
        dataset = str(manifest["dataset"])
        if dataset not in REQUIRED_DATASETS:
            raise RuntimeError(f"不支持的数据集: {dataset}")
        if manifest["raw_data_mode"] == "REAL_POINT_IN_TIME":
            assertions = manifest.get("coverage_assertions")
            if not isinstance(assertions, dict):
                raise RuntimeError("正式数据清单必须包含 coverage_assertions")
            required_assertions = (
                "full_primary_window",
                "point_in_time",
                "no_synthetic",
                "entity_scope_complete",
                "calendar_complete",
            )
            failed = [name for name in required_assertions if assertions.get(name) is not True]
            if failed:
                raise RuntimeError(f"正式数据覆盖断言未通过: {', '.join(failed)}")
        parquet_path = (self.project_root / str(manifest["file_path"])).resolve()
        allowed_root = (self.project_root / "data" / "backtests" / "parquet").resolve()
        if parquet_path.parent != allowed_root:
            raise RuntimeError("Parquet 必须直接位于 data/backtests/parquet")
        if not parquet_path.is_file():
            raise RuntimeError(f"Parquet 不存在: {manifest['file_path']}")
        actual_hash = _sha256(parquet_path)
        if actual_hash != manifest["sha256"]:
            raise RuntimeError(
                f"Parquet 哈希不一致: expected={manifest['sha256']}; actual={actual_hash}"
            )
        table = f"bt_{dataset}"
        with duckdb.connect(str(self.database)) as db:
            duplicate = db.execute(
                "SELECT 1 FROM bt_dataset_manifests WHERE manifest_id = ?",
                [manifest["manifest_id"]],
            ).fetchone()
            if duplicate:
                raise RuntimeError("manifest_id 已导入，禁止重复追加")
            description = db.execute(
                "SELECT column_name FROM (DESCRIBE SELECT * FROM read_parquet(?))",
                [str(parquet_path)],
            ).fetchall()
            actual_columns = {str(row[0]) for row in description}
            expected_columns = {str(value) for value in manifest["columns"]}
            if actual_columns != expected_columns:
                raise RuntimeError(
                    "Parquet 列与清单不一致: "
                    f"missing={sorted(expected_columns - actual_columns)}; "
                    f"extra={sorted(actual_columns - expected_columns)}"
                )
            stats = db.execute(
                """
                SELECT count(*)::BIGINT, min(effective_date)::VARCHAR,
                       max(effective_date)::VARCHAR,
                       count(*) FILTER (WHERE known_at IS NULL OR source_id IS NULL)::BIGINT,
                       count(*) FILTER (WHERE is_synthetic)::BIGINT
                FROM read_parquet(?)
                """,
                [str(parquet_path)],
            ).fetchone()
            assert stats is not None
            actual_count = int(stats[0])
            actual_minimum = str(stats[1])
            actual_maximum = str(stats[2])
            invalid_point_in_time = int(stats[3])
            synthetic_count = int(stats[4])
            if actual_count != int(manifest["row_count"]):
                raise RuntimeError("Parquet 行数与清单不一致")
            if actual_minimum != manifest["minimum_effective_date"]:
                raise RuntimeError("Parquet 最早日期与清单不一致")
            if actual_maximum != manifest["maximum_effective_date"]:
                raise RuntimeError("Parquet 最晚日期与清单不一致")
            if invalid_point_in_time:
                raise RuntimeError("Parquet 存在缺失 known_at/source_id 的行")
            if manifest["raw_data_mode"] == "REAL_POINT_IN_TIME" and synthetic_count:
                raise RuntimeError("REAL_POINT_IN_TIME 数据禁止包含合成原始观察")
            db.execute("BEGIN TRANSACTION")
            try:
                db.execute(
                    f"INSERT INTO {table} BY NAME SELECT * FROM read_parquet(?)",
                    [str(parquet_path)],
                )
                db.execute(
                    """
                    INSERT INTO bt_dataset_manifests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        manifest["manifest_id"],
                        dataset,
                        str(parquet_path),
                        actual_hash,
                        actual_count,
                        actual_minimum,
                        actual_maximum,
                        manifest["raw_data_mode"],
                        manifest["created_at"],
                    ],
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return {
            "manifest_id": str(manifest["manifest_id"]),
            "dataset": dataset,
            "row_count": actual_count,
            "sha256": actual_hash,
            "raw_data_mode": str(manifest["raw_data_mode"]),
        }

    def _coverage_sync(self, start_date: str, end_date: str) -> list[DatasetCoverage]:
        result: list[DatasetCoverage] = []
        with duckdb.connect(str(self.database), read_only=True) as db:
            for dataset in REQUIRED_DATASETS:
                table = f"bt_{dataset}"
                row = db.execute(
                    f"""
                    SELECT count(*)::BIGINT, min(effective_date)::VARCHAR,
                           max(effective_date)::VARCHAR,
                           count(*) FILTER (
                               WHERE known_at IS NOT NULL AND source_id IS NOT NULL
                           )::BIGINT,
                           count(*) FILTER (WHERE is_synthetic)::BIGINT
                    FROM {table}
                    """
                ).fetchone()
                assert row is not None
                count = int(row[0])
                minimum = str(row[1]) if row[1] is not None else None
                maximum = str(row[2]) if row[2] is not None else None
                point_in_time = int(row[3])
                synthetic = int(row[4])
                manifest_row = db.execute(
                    """
                    SELECT count(*) FROM bt_dataset_manifests
                    WHERE dataset = ? AND raw_data_mode = 'REAL_POINT_IN_TIME'
                    """,
                    [dataset],
                ).fetchone()
                assert manifest_row is not None
                real_manifest_count = int(manifest_row[0])
                result.append(
                    DatasetCoverage(
                        dataset=dataset,
                        row_count=count,
                        minimum_effective_date=minimum,
                        maximum_effective_date=maximum,
                        point_in_time_row_count=point_in_time,
                        synthetic_row_count=synthetic,
                        ready_for_primary_window=(
                            count > 0
                            and point_in_time == count
                            and synthetic == 0
                            and real_manifest_count > 0
                            and minimum is not None
                            and maximum is not None
                            and minimum <= start_date
                            and maximum >= end_date
                        ),
                    )
                )
        return result
