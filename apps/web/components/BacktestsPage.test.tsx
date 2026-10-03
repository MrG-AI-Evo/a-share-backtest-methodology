import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { BacktestsPage } from "@/components/BacktestsPage";

vi.mock("@/components/EChart", () => ({
  EChart: ({ ariaLabel }: { ariaLabel: string }) => <div role="img" aria-label={ariaLabel} />,
}));

vi.mock("@/lib/api", () => ({
  getBacktestReadiness: vi.fn().mockResolvedValue({
    data: {
      policy_id: "dividend-hurdle-core20",
      policy_version: "dividend-hurdle-core20-2026-08-27-draft-v2",
      policy_status: "DRAFT_NOT_EXECUTABLE",
      base_policy_sha256_verified: true,
      primary_window: { label: "TEN_COMPLETE_YEARS", start_date: "2016-01-04", end_date: "2025-12-31", included_in_primary_performance: true },
      extension_window: { label: "2026_OUT_OF_SAMPLE_OBSERVATION", start_date: "2026-01-01", end_date: "2026-08-24", included_in_primary_performance: false },
      formal_backtest_executable: false,
      engineering_ready: false,
      blockers: [{ blocker_id: "CD_HISTORY", category: "EXTERNAL_DATA", status: "OPEN", description: "历史利率数据不完整", evidence: ["rows=0"] }],
      dataset_coverage: [{ dataset: "cd_rates", row_count: 0, minimum_effective_date: null, maximum_effective_date: null, point_in_time_row_count: 0, synthetic_row_count: 0, ready_for_primary_window: false }],
      safety: { real_orders_enabled: false },
      checked_at: "2026-08-27T10:00:00+08:00",
    },
    meta: { data_state: "live" },
  }),
  getBacktestRuns: vi.fn().mockResolvedValue({ data: [], meta: { data_state: "live" } }),
  getQualitySampleBacktestReport: vi.fn().mockResolvedValue({
    data: {
      schema_version: "1.0.0", report_id: "quality-sample-11-test", status: "MECHANISM_TEST_ONLY", generated_at: "2026-09-12T03:04:03Z",
      source_run: { run_id: "pilot-v5", sha256: "a".repeat(64), policy_version: "pilot-v2", start_date: "2016-01-04", end_date: "2025-12-31", as_of: "2025-12-31T15:00:00+08:00" },
      portfolio: { cumulative_external_contributions_cny: 31117.97, total_assets_cny: 62554.5, cash_cny: 22334.5, market_value_cny: 40220, total_pnl_cny: 31436.53, realized_pnl_cny: 4124.31, unrealized_pnl_cny: 27312.22, simple_return: 1.0102, xirr: .0933, twr: 1.2361, maximum_drawdown: -.1873, trade_count: 41, active_position_count: 1, cumulative_dividend_gross_cny: 11260, transaction_costs_cny: 464.47 },
      annual: [{ year: 2016, year_end_total_assets_cny: 24357.22, cumulative_external_contributions_cny: 18816.34, cumulative_external_withdrawals_cny: 0, cumulative_pnl_cny: 5540.88, cumulative_simple_return: .2944, annual_net_pnl_cny: 5540.88 }],
      stocks: [{ symbol: "601288", name: "农业银行", trade_count: 10, total_pnl_cny: 959.15, position_state: "CLOSED", price_days: 2430, dividend_events: 15 }],
      data_quality: { cd_months: 120, cd_missing_months: 0, symbols: 11, price_rows: 25537, dividend_rows: 151, dividend_fiscal_rows: 137, quality_rows: 113, cd_cross_check: "120/120 months", prewindow_dividend_cross_check: "27/27 exact matches", cninfo_2014_reports: "9/9 parsed", synthetic_raw_observation_count: 0, time_travel_check: "PASS" },
      audit: { event_chain_ok: true, reconciliation_status: "PASS", cash_difference_cny: 0, pnl_difference_cny: 0, future_data_imputed: false, ledger_event_count: 2139, ledger_sha256: "b".repeat(64) },
      formal_blockers: ["hindsight selection bias"],
    },
    meta: { data_state: "live" },
  }),
}));

test("shows separated windows and blocks fake performance", async () => {
  render(<BacktestsPage />);
  expect(await screen.findByText("正式回测尚未执行")).toBeInTheDocument();
  expect(screen.getByText("2016-01-04 → 2025-12-31")).toBeInTheDocument();
  expect(screen.getByText("2026-01-01 → 2026-08-24")).toBeInTheDocument();
  expect(screen.getByText(/不会用假数据/)).toBeInTheDocument();
  expect(screen.getByText("高质量高股息 · 11股十年机制测试")).toBeInTheDocument();
  expect(screen.getByText("41 笔")).toBeInTheDocument();
  expect(screen.getByText("尚无运行记录")).toBeInTheDocument();
});
