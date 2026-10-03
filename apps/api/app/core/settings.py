from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.local"),
        env_prefix="ASHARE_",
        extra="ignore",
    )

    app_name: str = "A股操盘测试 API"
    app_version: str = "0.1.0"
    environment: str = "local"
    timezone: str = "Asia/Shanghai"
    api_prefix: str = "/api/v1"
    web_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    state_dir: Path = PROJECT_ROOT / "state"
    research_dir: Path = PROJECT_ROOT / "data" / "research"
    screening_dir: Path = PROJECT_ROOT / "data" / "screening"
    screening_invalidation_dir: Path = PROJECT_ROOT / "data" / "screening-invalidations"
    evidence_dir: Path = PROJECT_ROOT / "data" / "evidence"
    normalized_dir: Path = PROJECT_ROOT / "data" / "normalized"
    corporate_action_dir: Path = PROJECT_ROOT / "data" / "corporate-actions"
    schema_dir: Path = PROJECT_ROOT / "schemas"
    rule_file: Path = PROJECT_ROOT / "rules" / "a-share-rules.yaml"
    trading_calendar_file: Path = PROJECT_ROOT / "rules" / "trading-calendar-2026.yaml"
    policy_file: Path = PROJECT_ROOT / "config" / "policy.yaml"
    screening_policy_file: Path = PROJECT_ROOT / "config" / "screening-v1.yaml"
    backtest_policy_file: Path = PROJECT_ROOT / "config" / "dividend-hurdle-backtest-v4.yaml"
    deterministic_runtime_config_file: Path = (
        PROJECT_ROOT / "config" / "deterministic-runtime-v1.yaml"
    )
    backtest_manifest_dir: Path = PROJECT_ROOT / "data" / "backtests" / "manifests"
    backtest_parquet_dir: Path = PROJECT_ROOT / "data" / "backtests" / "parquet"
    request_timeout_seconds: float = 6.0
    market_cache_seconds: int = 8
    allow_demo_data: bool = False
    enable_mootdx: bool = True
    enable_akshare: bool = False

    @property
    def app_database(self) -> Path:
        return self.state_dir / "app.sqlite3"

    @property
    def paper_database(self) -> Path:
        return self.state_dir / "paper.sqlite3"

    @property
    def analytics_database(self) -> Path:
        return self.state_dir / "analytics.duckdb"

    @property
    def backtest_database(self) -> Path:
        return self.state_dir / "backtests.sqlite3"

    @property
    def backtest_analytics_database(self) -> Path:
        return self.state_dir / "backtests.duckdb"

    @property
    def deterministic_runtime_database(self) -> Path:
        return self.state_dir / "deterministic-runtime.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
