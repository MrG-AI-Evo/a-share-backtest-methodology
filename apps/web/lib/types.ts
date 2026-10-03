export type DataState = "live" | "delayed" | "stale" | "unavailable" | "demo";

export interface SourceMeta {
  provider: string;
  source_url: string | null;
  fetched_at: string;
  source_timestamp: string | null;
  state: DataState;
  age_seconds: number | null;
  notes: string[];
}

export interface ApiMeta {
  request_id: string;
  generated_at: string;
  data_state: DataState;
  sources: SourceMeta[];
  warnings: string[];
}

export interface IndexQuote {
  symbol: string;
  name: string;
  price: number;
  change: number;
  change_pct: number;
  amount_cny: number | null;
  market_status: string | null;
  updated_at: string;
}

export interface MarketBreadth {
  advancing: number | null;
  declining: number | null;
  unchanged: number | null;
  limit_up: number | null;
  limit_down: number | null;
  total_amount_cny: number | null;
  previous_amount_cny: number | null;
  amount_change_pct: number | null;
}

export interface SectorPerformance {
  name: string;
  kind: "industry" | "concept";
  change_pct: number;
  amount_cny: number | null;
  heat_score: number | null;
}

export interface MarketOverview {
  indices: IndexQuote[];
  breadth: MarketBreadth;
  sectors: SectorPerformance[];
  trading_phase: string;
}

export interface ResearchCardSummary {
  symbol: string;
  name: string;
  as_of: string;
  status: "重点观察" | "观察" | "谨慎" | "排除";
  score: number;
  confidence: number;
  summary: string;
  maximum_risk: string | null;
  file_path: string;
  valid_until: string | null;
  data_freshness: Record<string, unknown>;
}

export interface PaperSummary {
  base_currency: "CNY";
  initial_cash: number;
  total_assets: number;
  cash: number;
  market_value: number;
  total_return_pct: number;
  daily_pnl: number;
  max_drawdown_pct: number;
  position_pct: number;
  holding_count: number;
  as_of: string;
}

export interface DashboardPayload {
  market: MarketOverview;
  research: ResearchCardSummary[];
  paper: PaperSummary;
}

export interface StockQuote {
  symbol: string;
  name: string;
  exchange: "SSE" | "SZSE" | "BSE";
  price: number;
  previous_close: number;
  open: number;
  high: number;
  low: number;
  change: number;
  change_pct: number;
  volume_shares: number;
  amount_cny: number | null;
  turnover_pct: number | null;
  pe_ttm: number | null;
  pb: number | null;
  total_market_cap_cny: number | null;
  float_market_cap_cny: number | null;
  limit_up: number | null;
  limit_down: number | null;
  updated_at: string;
}

export interface PriceBar {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume_shares: number;
  amount_cny: number | null;
  ma5: number | null;
  ma10: number | null;
  ma20: number | null;
  ma60: number | null;
  macd_dif: number | null;
  macd_dea: number | null;
  macd_hist: number | null;
  rsi14: number | null;
  boll_mid: number | null;
  boll_upper: number | null;
  boll_lower: number | null;
}

export interface StockBars {
  symbol: string;
  period: "day" | "week" | "month" | "minute";
  adjustment: "qfq" | "hfq" | "none";
  minute_interval: number | null;
  bars: PriceBar[];
}

export interface ResearchClaim {
  text: string;
  evidence_ids: string[];
  confidence: number;
}

export interface ResearchCard {
  schema_version: "1.0.0";
  research_id: string;
  symbol: string;
  name: string;
  exchange: "SSE" | "SZSE" | "BSE";
  as_of: string;
  created_at: string;
  horizon: string;
  status: "重点观察" | "观察" | "谨慎" | "排除";
  score: number;
  confidence: number;
  summary: string;
  core_thesis: ResearchClaim[];
  bull_case: ResearchClaim[];
  bear_case: ResearchClaim[];
  catalysts: Array<{ text: string; window: string | null; evidence_ids: string[]; verified: boolean }>;
  risk_flags: Array<{ text: string; severity: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"; evidence_ids: string[] }>;
  invalidation_conditions: Array<{ condition: string; observable: string; action: string }>;
  confirmed_facts: Array<{ text: string; as_of: string; evidence_ids: string[] }>;
  ai_inferences: Array<{ text: string; confidence: number; supporting_evidence_ids: string[]; counter_evidence_ids: string[] }>;
  unverified_items: Array<{ text: string; impact: string; verification_action: string; deadline: string | null }>;
  evidence: Array<{ evidence_id: string; title: string; source_id: string; source_uri: string; published_at: string | null; as_of: string; retrieved_at: string; quality: string; excerpt?: string | null }>;
  data_freshness: Record<string, { as_of: string; retrieved_at: string; stale: boolean; source_ids: string[]; notes?: string }>;
  model_notes: string;
  scoring_version?: string | null;
  ruleset_version?: string | null;
  valid_until?: string | null;
  _file_path?: string;
}

export interface ApiEnvelope<T> {
  data: T;
  meta: ApiMeta;
  error: Record<string, unknown> | null;
}

export interface BacktestWindow {
  label: string;
  start_date: string;
  end_date: string;
  included_in_primary_performance: boolean;
}

export interface BacktestBlocker {
  blocker_id: string;
  category: "EXTERNAL_DATA" | "RULE_AND_POINT_IN_TIME_DATA" | "LOCAL_ENGINEERING" | "USER_DECISION" | "DATA_COVERAGE";
  status: "OPEN" | "IN_PROGRESS" | "CLOSED";
  description: string;
  evidence: string[];
}

export interface DatasetCoverage {
  dataset: string;
  row_count: number;
  minimum_effective_date: string | null;
  maximum_effective_date: string | null;
  point_in_time_row_count: number;
  synthetic_row_count: number;
  ready_for_primary_window: boolean;
}

export interface BacktestReadiness {
  policy_id: string;
  policy_version: string;
  policy_status: string;
  base_policy_sha256_verified: boolean;
  primary_window: BacktestWindow;
  extension_window: BacktestWindow;
  formal_backtest_executable: boolean;
  engineering_ready: boolean;
  blockers: BacktestBlocker[];
  dataset_coverage: DatasetCoverage[];
  safety: Record<string, boolean>;
  checked_at: string;
}

export interface BacktestRunSummary {
  run_id: string;
  policy_version: string;
  purpose: "FORMAL_BACKTEST" | "MECHANISM_TEST" | "READINESS_PREFLIGHT";
  scenario: "OPTIMISTIC" | "BASE" | "STRESS";
  segment: "PRIMARY_TEN_YEAR" | "EXTENSION_2026" | "MECHANISM_ONLY";
  status: "PREFLIGHT" | "BLOCKED" | "RUNNING" | "COMPLETE" | "FAILED";
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
  data_cutoff: string | null;
  blocker_count: number;
  performance_available: boolean;
  summary: string;
}

export interface BacktestAuditEvent {
  event_id: string;
  run_id: string;
  sequence: number;
  event_type: string;
  occurred_at: string;
  known_at: string;
  symbol: string | null;
  payload: Record<string, unknown>;
  prev_event_hash: string | null;
  event_hash: string;
}

export interface BacktestCheckpoint {
  sequence: number;
  as_of: string;
  account_summary: Record<string, unknown>;
  performance: Record<string, unknown>;
  reconciliation_status: "PASS" | "FAIL" | "NOT_RUN";
  data_quality_status: "VALID" | "STALE" | "DEGRADED" | "INVALID" | "NOT_RUN";
  security_ledgers: Array<Record<string, unknown>>;
  account_cash_flows: Array<Record<string, unknown>>;
}

export interface BacktestRunDetail {
  run: BacktestRunSummary;
  blockers: BacktestBlocker[];
  checkpoints: BacktestCheckpoint[];
  checkpoint_count: number;
  latest_checkpoint: BacktestCheckpoint | null;
  events: BacktestAuditEvent[];
  event_count: number;
  audit_chain_valid: boolean;
  policy_snapshot: Record<string, unknown>;
}

export interface QualitySampleBacktestReport {
  schema_version: "1.0.0";
  report_id: string;
  status: "MECHANISM_TEST_ONLY";
  generated_at: string;
  source_run: {
    run_id: string;
    sha256: string;
    policy_version: string;
    start_date: string;
    end_date: string;
    as_of: string;
  };
  portfolio: {
    cumulative_external_contributions_cny: number;
    total_assets_cny: number;
    cash_cny: number;
    market_value_cny: number;
    total_pnl_cny: number;
    realized_pnl_cny: number;
    unrealized_pnl_cny: number;
    simple_return: number;
    xirr: number;
    twr: number;
    maximum_drawdown: number;
    trade_count: number;
    active_position_count: number;
    cumulative_dividend_gross_cny: number;
    transaction_costs_cny: number;
  };
  annual: Array<{
    year: number;
    year_end_total_assets_cny: number;
    cumulative_external_contributions_cny: number;
    cumulative_external_withdrawals_cny: number;
    cumulative_pnl_cny: number;
    cumulative_simple_return: number;
    annual_net_pnl_cny: number;
  }>;
  stocks: Array<{
    symbol: string;
    name: string;
    trade_count: number;
    total_pnl_cny: number;
    position_state: "OPEN" | "CLOSED" | "NEVER_OPENED";
    price_days: number;
    dividend_events: number;
  }>;
  data_quality: {
    cd_months: number;
    cd_missing_months: number;
    symbols: number;
    price_rows: number;
    dividend_rows: number;
    dividend_fiscal_rows: number;
    quality_rows: number;
    cd_cross_check: string;
    prewindow_dividend_cross_check: string;
    cninfo_2014_reports: string;
    synthetic_raw_observation_count: number;
    time_travel_check: string;
  };
  audit: {
    event_chain_ok: boolean;
    reconciliation_status: string;
    cash_difference_cny: number;
    pnl_difference_cny: number;
    future_data_imputed: boolean;
    ledger_event_count: number;
    ledger_sha256: string;
  };
  formal_blockers: string[];
}

export interface RiskCheck {
  check: string;
  status: "PASS" | "FAIL" | "REVIEW";
  detail: string;
}

export interface PaperFill {
  fill_id: string;
  order_id: string;
  filled_at: string;
  price: number;
  quantity: number;
  gross_amount_cny: number;
  commission_cny: number;
  stamp_duty_cny: number;
  transfer_fee_cny: number;
  slippage_cny: number;
  total_fees_cny: number;
  source: string;
  source_timestamp: string;
}

export interface PaperOrder {
  order_id: string;
  account_id: "paper-v1";
  symbol: string;
  name: string;
  side: "BUY" | "SELL";
  order_type: "LIMIT_SIM";
  limit_price: number;
  quantity: number;
  filled_quantity: number;
  status: "PENDING_APPROVAL" | "QUEUED" | "PARTIAL" | "FILLED" | "REJECTED" | "CANCELLED" | "EXPIRED";
  reason: string;
  research_version: string | null;
  expected_horizon: string;
  risk_notes: string[];
  invalidation_conditions: string[];
  invalidation_price: number | null;
  ruleset_version: string;
  fee_schedule_version: string;
  risk_checks: RiskCheck[];
  created_at: string;
  earliest_fill_at: string;
  approved_at: string | null;
  rejected_at: string | null;
  rejection_reason: string | null;
  fills: PaperFill[];
}

export interface PaperPosition {
  symbol: string;
  name: string;
  quantity: number;
  sellable_quantity: number;
  cost_price: number;
  last_price: number;
  market_value_cny: number;
  unrealized_pnl_cny: number;
  unrealized_pnl_pct: number;
  updated_at: string;
}

export interface NavPoint {
  as_of: string;
  total_assets: number;
  cash: number;
  market_value: number;
  daily_pnl: number;
}

export interface PaperAccountDetail {
  summary: PaperSummary;
  positions: PaperPosition[];
  orders: PaperOrder[];
  nav: NavPoint[];
  policy_version: string;
  ruleset_version: string;
  fee_schedule_version: string;
  fee_notes: string[];
  live_broker_enabled: false;
  automatic_trading_enabled: false;
}

export interface PaperOrderCreate {
  symbol: string;
  name: string;
  side: "BUY" | "SELL";
  order_type: "LIMIT_SIM";
  limit_price: number;
  quantity: number;
  reason: string;
  research_version: string | null;
  expected_horizon: string;
  risk_notes: string[];
  invalidation_conditions: string[];
  invalidation_price: number | null;
}

export interface WatchlistItem {
  symbol: string;
  name: string;
  stage: string;
  note: string;
  added_at: string;
  updated_at: string;
}

export interface AuditEvent {
  event_id: string;
  run_id: string;
  event_type: string;
  actor: string;
  occurred_at: string;
  payload: Record<string, unknown>;
  prev_event_hash: string | null;
  event_hash: string;
}

export interface DecisionHistory {
  orders: PaperOrder[];
  audit_events: AuditEvent[];
}

export interface SystemStatus {
  api_version: string;
  environment: string;
  ruleset_version: string;
  rules_status: string;
  policy_version: string;
  fee_schedule_version: string;
  research_card_count: number;
  research_import_warnings: string[];
  paper_database_bytes: number;
  analytics_database_bytes: number;
  data_sources: SourceMeta[];
  market_warnings: string[];
  adapter_health: AdapterHealth[];
  latest_screening_run_id: string | null;
  latest_screening_as_of: string | null;
  latest_factor_candidate_count: number;
  research_evaluation_pass_count: number;
  research_evaluation_fail_count: number;
  ledger_reconciled: boolean;
  ledger_reconciliation_errors: string[];
  live_broker_enabled: false;
  automatic_trading_enabled: false;
  llm_api_enabled: false;
}

export type DeterministicTaskId = "DATA_UPDATE_VALIDATE" | "DAILY_EXIT_CHECK" | "MONTHLY_CANDIDATE_SCREEN" | "NEXT_OPEN_PAPER_FILL" | "CORPORATE_ACTIONS_LEDGER" | "ANNUAL_PNL_SNAPSHOT" | "EXCEPTION_EVIDENCE_PACKAGE";

export interface DeterministicException {
  code: string;
  severity: "INFO" | "WARNING" | "BLOCKER" | "CRITICAL";
  message: string;
  evidence_refs: string[];
  suggested_human_action: string;
}

export interface DeterministicTaskRun {
  schema_version: "1.0.0";
  run_id: string;
  task_id: DeterministicTaskId;
  execution_mode: "MANUAL_COMMAND";
  status: "COMPLETE" | "BLOCKED" | "FAILED";
  requested_at: string;
  finished_at: string;
  execution_duration_ms: number;
  as_of: string;
  data_cutoff: string | null;
  runtime_version: string;
  ruleset_version: string;
  policy_version: string;
  input_hash: string;
  counts: Record<string, number | null>;
  blockers: string[];
  exceptions: DeterministicException[];
  summary: string;
  artifact_hash: string;
  artifact_path: string | null;
  exception_package_path: string | null;
}

export interface RuntimeTaskDefinition {
  task_id: DeterministicTaskId;
  enabled: false;
  manual_command_allowed: true;
  cadence: string;
  trigger: string;
  description: string;
  latest_run: DeterministicTaskRun | null;
}

export interface AnnualPnl {
  year: number;
  year_start_total_assets_cny: number;
  year_end_total_assets_cny: number;
  annual_external_contributions_cny: number;
  annual_external_withdrawals_cny: number;
  cumulative_external_contributions_cny: number;
  cumulative_external_withdrawals_cny: number;
  annual_net_pnl_cny: number;
  cumulative_pnl_cny: number;
  attribution: Record<string, number | null>;
}

export interface DeterministicRuntimeStatus {
  runtime_id: string;
  runtime_version: string;
  status: "DISABLED";
  execution_mode: "MANUAL_ONLY";
  scheduler_enabled: false;
  llm_api_enabled: false;
  web_ai_chat_enabled: false;
  real_broker_enabled: false;
  automatic_trading_enabled: false;
  activation_gate: Record<string, unknown>;
  tasks: RuntimeTaskDefinition[];
  annual_pnl: AnnualPnl[];
  current_blockers: string[];
  checked_at: string;
}

export interface AdapterHealth {
  adapter_id: string;
  role: string;
  installed: boolean;
  enabled: boolean;
  mode: "RUNTIME" | "MANUAL_BRIDGE" | "DISABLED";
  version: string | null;
  upstream_commit: string | null;
  notes: string[];
}

export interface ScreeningCandidate {
  symbol: string;
  name: string;
  exchange: string;
  industry: string | null;
  stage: "HARD_FILTER" | "FACTOR_FILTER";
  rank: number;
  score: number | null;
  factor_scores: Record<string, number>;
  reason_codes: string[];
}

export interface ScreeningRun {
  schema_version: "1.0.0";
  run_id: string;
  workflow: "SCREENING";
  as_of: string;
  created_at: string;
  universe_count: number;
  hard_filter_target: number;
  factor_filter_target: number;
  scoring_version: string;
  policy_version: string;
  input_hash: string;
  hard_filter: ScreeningCandidate[];
  factor_filter: ScreeningCandidate[];
  exclusions: Array<{ symbol: string; name: string; reason_codes: string[] }>;
  warnings: string[];
  artifact_path: string | null;
}

export interface ResearchEvaluation {
  schema_version: "1.0.0";
  research_id: string;
  evaluated_at: string;
  evaluator_version: string;
  passed: boolean;
  score: number;
  hard_failures: string[];
  warnings: string[];
  metrics: Record<string, number | boolean>;
}

export interface PipelineStatus {
  latest_screening: ScreeningRun | null;
  research_evaluations: ResearchEvaluation[];
  adapter_health: AdapterHealth[];
  llm_api_enabled: false;
  uzi_daily_full_market_enabled: false;
}
