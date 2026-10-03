from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DataState = Literal["live", "delayed", "stale", "unavailable", "demo"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceMeta(StrictModel):
    provider: str
    source_url: str | None = None
    fetched_at: str
    source_timestamp: str | None = None
    state: DataState
    age_seconds: int | None = None
    notes: list[str] = Field(default_factory=list)


class AdapterHealth(StrictModel):
    adapter_id: str
    role: str
    installed: bool
    enabled: bool
    mode: Literal["RUNTIME", "MANUAL_BRIDGE", "DISABLED"]
    version: str | None = None
    upstream_commit: str | None = None
    notes: list[str] = Field(default_factory=list)


class ApiMeta(StrictModel):
    request_id: str
    generated_at: str
    data_state: DataState
    sources: list[SourceMeta] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ApiEnvelope(StrictModel):
    data: Any
    meta: ApiMeta
    error: dict[str, Any] | None = None


class IndexQuote(StrictModel):
    symbol: str
    name: str
    price: float
    change: float
    change_pct: float
    amount_cny: float | None = None
    market_status: str | None = None
    updated_at: str


class MarketBreadth(StrictModel):
    advancing: int | None = None
    declining: int | None = None
    unchanged: int | None = None
    limit_up: int | None = None
    limit_down: int | None = None
    total_amount_cny: float | None = None
    previous_amount_cny: float | None = None
    amount_change_pct: float | None = None


class SectorPerformance(StrictModel):
    name: str
    kind: Literal["industry", "concept"]
    change_pct: float
    amount_cny: float | None = None
    heat_score: float | None = None


class MarketOverview(StrictModel):
    indices: list[IndexQuote]
    breadth: MarketBreadth
    sectors: list[SectorPerformance]
    trading_phase: str


class PaperSummary(StrictModel):
    base_currency: Literal["CNY"] = "CNY"
    initial_cash: float
    total_assets: float
    cash: float
    market_value: float
    total_return_pct: float
    daily_pnl: float
    max_drawdown_pct: float
    position_pct: float
    holding_count: int
    as_of: str


class ResearchCardSummary(StrictModel):
    symbol: str
    name: str
    as_of: str
    status: Literal["重点观察", "观察", "谨慎", "排除"]
    score: float
    confidence: float
    summary: str
    maximum_risk: str | None = None
    file_path: str
    valid_until: str | None = None
    data_freshness: dict[str, Any] = Field(default_factory=dict)


class DashboardPayload(StrictModel):
    market: MarketOverview
    research: list[ResearchCardSummary]
    paper: PaperSummary


class RiskCheck(StrictModel):
    check: str
    status: Literal["PASS", "FAIL", "REVIEW"]
    detail: str


class PaperFill(StrictModel):
    fill_id: str
    order_id: str
    filled_at: str
    price: float
    quantity: int
    gross_amount_cny: float
    commission_cny: float
    stamp_duty_cny: float
    transfer_fee_cny: float
    slippage_cny: float
    total_fees_cny: float
    source: str
    source_timestamp: str


class PaperOrderCreate(StrictModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1, max_length=40)
    side: Literal["BUY", "SELL"]
    order_type: Literal["LIMIT_SIM"] = "LIMIT_SIM"
    limit_price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    reason: str = Field(min_length=4, max_length=1000)
    research_version: str | None = None
    expected_horizon: str = Field(min_length=1, max_length=80)
    risk_notes: list[str] = Field(min_length=1)
    invalidation_conditions: list[str] = Field(min_length=1)
    invalidation_price: float | None = Field(default=None, gt=0)


class PaperOrder(StrictModel):
    order_id: str
    account_id: Literal["paper-v1"] = "paper-v1"
    symbol: str
    name: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["LIMIT_SIM"]
    limit_price: float
    quantity: int
    filled_quantity: int
    status: Literal[
        "PENDING_APPROVAL",
        "QUEUED",
        "PARTIAL",
        "FILLED",
        "REJECTED",
        "CANCELLED",
        "EXPIRED",
    ]
    reason: str
    research_version: str | None
    expected_horizon: str
    risk_notes: list[str]
    invalidation_conditions: list[str]
    invalidation_price: float | None
    ruleset_version: str
    fee_schedule_version: str
    risk_checks: list[RiskCheck]
    created_at: str
    earliest_fill_at: str
    approved_at: str | None = None
    rejected_at: str | None = None
    rejection_reason: str | None = None
    fills: list[PaperFill] = Field(default_factory=list)


class PaperPosition(StrictModel):
    symbol: str
    name: str
    quantity: int
    sellable_quantity: int
    cost_price: float
    last_price: float
    market_value_cny: float
    unrealized_pnl_cny: float
    unrealized_pnl_pct: float
    updated_at: str


class NavPoint(StrictModel):
    as_of: str
    total_assets: float
    cash: float
    market_value: float
    daily_pnl: float


class PaperAccountDetail(StrictModel):
    summary: PaperSummary
    positions: list[PaperPosition]
    orders: list[PaperOrder]
    nav: list[NavPoint]
    policy_version: str
    ruleset_version: str
    fee_schedule_version: str
    fee_notes: list[str]
    live_broker_enabled: Literal[False] = False
    automatic_trading_enabled: Literal[False] = False


class OrderAction(StrictModel):
    reason: str = Field(min_length=2, max_length=500)


class WatchlistCreate(StrictModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1, max_length=40)
    stage: Literal["自选", "硬筛", "快研", "重点研究", "深度尽调", "重点关注"] = "自选"
    note: str = Field(default="", max_length=500)


class WatchlistItem(StrictModel):
    symbol: str
    name: str
    stage: str
    note: str
    added_at: str
    updated_at: str


class AuditEvent(StrictModel):
    event_id: str
    run_id: str
    event_type: str
    actor: str
    occurred_at: str
    payload: dict[str, Any]
    prev_event_hash: str | None
    event_hash: str


class SystemStatus(StrictModel):
    api_version: str
    environment: str
    ruleset_version: str
    rules_status: str
    policy_version: str
    fee_schedule_version: str
    research_card_count: int
    research_import_warnings: list[str]
    paper_database_bytes: int
    analytics_database_bytes: int
    data_sources: list[SourceMeta] = Field(default_factory=list)
    market_warnings: list[str] = Field(default_factory=list)
    adapter_health: list[AdapterHealth] = Field(default_factory=list)
    latest_screening_run_id: str | None = None
    latest_screening_as_of: str | None = None
    latest_factor_candidate_count: int = 0
    research_evaluation_pass_count: int = 0
    research_evaluation_fail_count: int = 0
    ledger_reconciled: bool = False
    ledger_reconciliation_errors: list[str] = Field(default_factory=list)
    live_broker_enabled: Literal[False] = False
    automatic_trading_enabled: Literal[False] = False
    llm_api_enabled: Literal[False] = False


class StockQuote(StrictModel):
    symbol: str
    name: str
    exchange: Literal["SSE", "SZSE", "BSE"]
    price: float
    previous_close: float
    open: float
    high: float
    low: float
    change: float
    change_pct: float
    volume_shares: float
    amount_cny: float | None = None
    turnover_pct: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    total_market_cap_cny: float | None = None
    float_market_cap_cny: float | None = None
    limit_up: float | None = None
    limit_down: float | None = None
    updated_at: str


class PriceBar(StrictModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume_shares: float
    amount_cny: float | None = None
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    macd_dif: float | None = None
    macd_dea: float | None = None
    macd_hist: float | None = None
    rsi14: float | None = None
    boll_mid: float | None = None
    boll_upper: float | None = None
    boll_lower: float | None = None


class StockBars(StrictModel):
    symbol: str
    period: Literal["day", "week", "month", "minute"]
    adjustment: Literal["qfq", "hfq", "none"]
    minute_interval: int | None = None
    bars: list[PriceBar]


class UniverseSecurity(StrictModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    exchange: Literal["SSE", "SZSE", "BSE"]
    industry: str | None = None
    asset_type: Literal["ORDINARY_A_SHARE", "OTHER"] = "ORDINARY_A_SHARE"
    risk_status: Literal["NORMAL", "ST", "DELISTING", "UNKNOWN"] = "UNKNOWN"
    trading_status: Literal["NORMAL", "SUSPENDED", "UNKNOWN"] = "UNKNOWN"
    listing_date: str | None = None
    history_days: int = Field(ge=0)
    last_price: float | None = Field(default=None, gt=0)
    avg_amount_20d_cny: float | None = Field(default=None, ge=0)
    turnover_20d_pct: float | None = Field(default=None, ge=0)
    roe_ttm_pct: float | None = None
    operating_cashflow_profit_ratio: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    momentum_20d_pct: float | None = None
    momentum_60d_pct: float | None = None
    relative_strength_20d_pct: float | None = None
    volatility_20d_pct: float | None = Field(default=None, ge=0)
    catalyst_score: float | None = Field(default=None, ge=0, le=100)
    risk_score: float | None = Field(default=None, ge=0, le=100)
    data_quality: Literal["A", "B", "C", "D", "F"]
    as_of: str
    source_ids: list[str] = Field(min_length=1)


class UniverseCoverage(StrictModel):
    listed_count_by_exchange: dict[str, int]
    quote_count_by_exchange: dict[str, int]
    enriched_count_by_exchange: dict[str, int]
    quote_coverage_by_exchange: dict[str, float]
    screenable_count: int = Field(ge=0)
    complete_listing: bool
    formal_screening_eligible: bool


class UniverseSnapshot(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    workflow: Literal["UNIVERSE_COLLECTION"] = "UNIVERSE_COLLECTION"
    as_of: str
    retrieved_at: str
    coverage: UniverseCoverage
    sources: list[SourceMeta]
    warnings: list[str] = Field(default_factory=list)
    rows: list[UniverseSecurity]
    artifact_path: str | None = None


class ScreeningCandidate(StrictModel):
    symbol: str
    name: str
    exchange: str
    industry: str | None = None
    stage: Literal["HARD_FILTER", "FACTOR_FILTER"]
    rank: int
    score: float | None = None
    factor_scores: dict[str, float] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)


class ScreeningExclusion(StrictModel):
    symbol: str
    name: str
    reason_codes: list[str] = Field(min_length=1)


class ScreeningRun(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    workflow: Literal["SCREENING"] = "SCREENING"
    as_of: str
    created_at: str
    universe_count: int = Field(ge=0)
    hard_filter_target: int = Field(ge=1)
    factor_filter_target: int = Field(ge=1)
    scoring_version: str
    policy_version: str
    input_hash: str
    hard_filter: list[ScreeningCandidate]
    factor_filter: list[ScreeningCandidate]
    exclusions: list[ScreeningExclusion]
    warnings: list[str] = Field(default_factory=list)
    artifact_path: str | None = None


class ScreeningInvalidation(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    invalidation_id: str
    run_id: str
    invalidated_at: str
    reason: str = Field(min_length=10)
    replacement_run_id: str | None = None


class ResearchEvaluation(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    research_id: str
    evaluated_at: str
    evaluator_version: str
    passed: bool
    score: float = Field(ge=0, le=100)
    hard_failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metrics: dict[str, float | int | bool]


class PipelineStatus(StrictModel):
    latest_screening: ScreeningRun | None
    research_evaluations: list[ResearchEvaluation]
    adapter_health: list[AdapterHealth]
    llm_api_enabled: Literal[False] = False
    uzi_daily_full_market_enabled: Literal[False] = False


class CorporateAction(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    action_id: str = Field(min_length=8)
    symbol: str = Field(pattern=r"^\d{6}$")
    action_type: Literal["CASH_DIVIDEND", "SHARE_MULTIPLIER", "PRICE_FACTOR"]
    announced_at: str
    effective_date: str
    cash_per_share_cny: float | None = Field(default=None, ge=0)
    share_multiplier: float | None = Field(default=None, gt=0)
    price_factor: float = Field(gt=0)
    source_id: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    retrieved_at: str
    quality: Literal["A", "B"]
    notes: list[str] = Field(default_factory=list)
