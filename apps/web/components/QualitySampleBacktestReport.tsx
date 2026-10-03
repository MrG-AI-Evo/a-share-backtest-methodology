"use client";

import type { EChartsCoreOption } from "echarts";
import { useMemo } from "react";

import { EChart } from "@/components/EChart";
import { formatMoney, formatPercent } from "@/lib/format";
import type { QualitySampleBacktestReport as Report } from "@/lib/types";

const preciseMoney = new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function QualitySampleBacktestReport({ report }: { report: Report }) {
  const annualOption = useMemo<EChartsCoreOption>(() => ({
    aria: { enabled: true, description: "2016至2025年度净盈亏与累计盈亏" },
    grid: { left: 16, right: 18, top: 34, bottom: 24, containLabel: true },
    tooltip: { trigger: "axis", confine: true, valueFormatter: (value: unknown) => formatPreciseMoney(Number(value)) },
    legend: { top: 0, right: 0, textStyle: { color: "#8793a5", fontSize: 11 }, data: ["年度净盈亏", "累计盈亏"] },
    xAxis: { type: "category", data: report.annual.map((item) => String(item.year)), axisTick: { show: false }, axisLabel: { color: "#8793a5" }, axisLine: { lineStyle: { color: "#2b394b" } } },
    yAxis: { type: "value", axisLabel: { color: "#657286", formatter: (value: number) => `${Math.round(value / 1000)}k` }, splitLine: { lineStyle: { color: "#202b3a" } } },
    series: [
      { name: "年度净盈亏", type: "bar", barMaxWidth: 30, data: report.annual.map((item) => ({ value: item.annual_net_pnl_cny, itemStyle: { color: item.annual_net_pnl_cny > 0 ? "#f05355" : item.annual_net_pnl_cny < 0 ? "#2bb673" : "#657286", borderRadius: item.annual_net_pnl_cny >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) },
      { name: "累计盈亏", type: "line", data: report.annual.map((item) => item.cumulative_pnl_cny), symbolSize: 6, lineStyle: { color: "#f0b90b", width: 2 }, itemStyle: { color: "#f0b90b" } },
    ],
  }), [report.annual]);

  const rankedStocks = useMemo(() => [...report.stocks].sort((a, b) => b.total_pnl_cny - a.total_pnl_cny), [report.stocks]);
  const stockOption = useMemo<EChartsCoreOption>(() => ({
    aria: { enabled: true, description: "11只样本股票累计盈亏贡献" },
    grid: { left: 12, right: 48, top: 8, bottom: 12, containLabel: true },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, confine: true, valueFormatter: (value: unknown) => formatPreciseMoney(Number(value)) },
    xAxis: { type: "value", axisLabel: { color: "#657286", formatter: (value: number) => `${Math.round(value / 1000)}k` }, splitLine: { lineStyle: { color: "#202b3a" } } },
    yAxis: { type: "category", inverse: true, data: rankedStocks.map((item) => item.name), axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: "#cbd3dd", fontSize: 11 } },
    series: [{ type: "bar", barMaxWidth: 18, label: { show: true, position: "right", color: "#8793a5", fontSize: 10, formatter: ({ value }: { value: unknown }) => formatPreciseMoney(Number(value)) }, data: rankedStocks.map((item) => ({ value: item.total_pnl_cny, itemStyle: { color: item.total_pnl_cny > 0 ? "#f05355" : item.total_pnl_cny < 0 ? "#2bb673" : "#657286", borderRadius: 3 } })) }],
  }), [rankedStocks]);

  return (
    <section className="qualityReport" aria-labelledby="quality-report-title">
      <header className="qualityReportHeader">
        <div><p className="eyebrow">VERIFIED MECHANISM REPORT</p><h2 id="quality-report-title">高质量高股息 · 11股十年机制测试</h2><p>{report.source_run.start_date}—{report.source_run.end_date} · 截止 {report.source_run.as_of.slice(0, 10)}</p></div>
        <span className="qualityReportBadge">机制测试 · 非正式绩效</span>
      </header>

      <aside className="warningBanner qualityReportWarning"><strong>解释边界</strong><span>计算、账本和时间穿越检查已通过；样本由2026年视角选取，不能外推为全市场正式十年收益。</span></aside>

      <div className="qualityKpis" aria-label="组合关键指标">
        <Metric label="期末总资产" value={formatPreciseMoney(report.portfolio.total_assets_cny)} note={`现金 ${formatPreciseMoney(report.portfolio.cash_cny)}`} />
        <Metric label="总盈亏" value={formatPreciseMoney(report.portfolio.total_pnl_cny)} note={`已实现 ${formatPreciseMoney(report.portfolio.realized_pnl_cny)}`} direction={report.portfolio.total_pnl_cny} />
        <Metric label="累计外部投入" value={formatPreciseMoney(report.portfolio.cumulative_external_contributions_cny)} note="按需注资，不是固定本金" />
        <Metric label="XIRR" value={formatPercent(report.portfolio.xirr * 100)} note="资金加权年化收益" direction={report.portfolio.xirr} />
        <Metric label="TWR" value={formatPercent(report.portfolio.twr * 100)} note="时间加权累计收益" direction={report.portfolio.twr} />
        <Metric label="最大回撤" value={formatPercent(report.portfolio.maximum_drawdown * 100)} note="组合历史峰值至谷值" direction={report.portfolio.maximum_drawdown} />
        <Metric label="成交笔数" value={`${report.portfolio.trade_count} 笔`} note={`${report.portfolio.active_position_count} 只期末持仓`} />
        <Metric label="现金分红" value={formatPreciseMoney(report.portfolio.cumulative_dividend_gross_cny)} note={`交易费用 ${formatPreciseMoney(report.portfolio.transaction_costs_cny)}`} />
      </div>

      <div className="qualityCharts">
        <article className="panel qualityChartPanel"><header><div><p className="eyebrow">ANNUAL P&L</p><h3>年度盈亏与累计盈亏</h3></div><span>红正 · 绿负</span></header><EChart option={annualOption} ariaLabel="2016至2025年度净盈亏与累计盈亏图" className="qualityAnnualChart" /></article>
        <article className="panel qualityChartPanel"><header><div><p className="eyebrow">STOCK CONTRIBUTION</p><h3>个股盈亏贡献</h3></div><span>按累计盈亏排序</span></header><EChart option={stockOption} ariaLabel="11只样本股票累计盈亏贡献图" className="qualityStockChart" /></article>
      </div>

      <div className="qualityReportGrid">
        <article className="panel qualityTablePanel"><header className="sectionHeader"><div><p className="eyebrow">YEARLY LEDGER</p><h3>逐年账本</h3></div><span>10年</span></header><div className="dataTableWrap"><table className="dataTable"><thead><tr><th>年份</th><th>年末资产</th><th>年度盈亏</th><th>累计盈亏</th><th>累计收益</th></tr></thead><tbody>{report.annual.map((item) => <tr key={item.year}><td>{item.year}</td><td>{formatPreciseMoney(item.year_end_total_assets_cny)}</td><td className={`numberDirection numberDirection--${direction(item.annual_net_pnl_cny)}`}>{formatPreciseMoney(item.annual_net_pnl_cny)}</td><td>{formatPreciseMoney(item.cumulative_pnl_cny)}</td><td>{formatPercent(item.cumulative_simple_return * 100)}</td></tr>)}</tbody></table></div></article>
        <article className="panel qualityAuditPanel"><header className="sectionHeader"><div><p className="eyebrow">DATA & AUDIT</p><h3>数据质量与审计</h3></div><span>{report.audit.reconciliation_status}</span></header><dl><Audit label="大额存单利率" value={`${report.data_quality.cd_cross_check} · 缺失 ${report.data_quality.cd_missing_months}`} ok={report.data_quality.cd_missing_months === 0} /><Audit label="前置分红交叉核验" value={report.data_quality.prewindow_dividend_cross_check} ok /><Audit label="2014年报解析" value={report.data_quality.cninfo_2014_reports} ok /><Audit label="原始合成数据" value={`${report.data_quality.synthetic_raw_observation_count}`} ok={report.data_quality.synthetic_raw_observation_count === 0} /><Audit label="时间穿越检查" value={report.data_quality.time_travel_check} ok={report.data_quality.time_travel_check === "PASS"} /><Audit label="账本闭合" value={`${report.audit.reconciliation_status} · 差额 ${formatMoney(report.audit.cash_difference_cny)}`} ok={report.audit.reconciliation_status === "PASS"} /><Audit label="哈希链事件" value={`${report.audit.ledger_event_count.toLocaleString("zh-CN")} 条`} ok={report.audit.event_chain_ok} /></dl></article>
      </div>

        <article className="panel qualityTablePanel qualityStockLedger"><header className="sectionHeader"><div><p className="eyebrow">SECURITY RESULTS</p><h3>11只股票结果</h3></div><span>{report.stocks.length} 只</span></header><div className="dataTableWrap"><table className="dataTable"><thead><tr><th>股票</th><th>累计盈亏</th><th>成交</th><th>期末状态</th><th>行情天数</th><th>分红事件</th></tr></thead><tbody>{rankedStocks.map((item) => <tr key={item.symbol}><td><strong>{item.name}</strong><span>{item.symbol}</span></td><td className={`numberDirection numberDirection--${direction(item.total_pnl_cny)}`}>{formatPreciseMoney(item.total_pnl_cny)}</td><td>{item.trade_count} 笔</td><td>{stateLabel(item.position_state)}</td><td>{item.price_days.toLocaleString("zh-CN")}</td><td>{item.dividend_events}</td></tr>)}</tbody></table></div></article>

      <footer className="qualityReportFooter"><span>run_id · {report.source_run.run_id}</span><span>策略 · {report.source_run.policy_version}</span><span title={report.source_run.sha256}>结果哈希 · {report.source_run.sha256.slice(0, 16)}…</span></footer>
    </section>
  );
}

function Metric({ label, value, note, direction: valueDirection }: { label: string; value: string; note: string; direction?: number }) {
  return <article className="qualityMetric"><span>{label}</span><strong className={valueDirection === undefined ? undefined : `numberDirection numberDirection--${direction(valueDirection)}`}>{value}</strong><small>{note}</small></article>;
}

function Audit({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return <div><dt>{label}</dt><dd><span aria-hidden="true">{ok ? "✓" : "!"}</span>{value}</dd></div>;
}

function formatPreciseMoney(value: number): string { return preciseMoney.format(value); }
function direction(value: number): "up" | "down" | "flat" { return value > 0 ? "up" : value < 0 ? "down" : "flat"; }
function stateLabel(value: Report["stocks"][number]["position_state"]): string { return value === "OPEN" ? "持仓中" : value === "CLOSED" ? "已平仓" : "未触发建仓"; }
