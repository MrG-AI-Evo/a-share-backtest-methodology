"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { getBacktestDetail } from "@/lib/api";
import { formatMoney, formatPercent, formatTime } from "@/lib/format";
import type { BacktestRunDetail } from "@/lib/types";

export function BacktestDetailPage({ runId }: { runId: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [detail, setDetail] = useState<BacktestRunDetail | null>(null);
  const [error, setError] = useState("");
  const eventPage = pageNumber(searchParams.get("eventPage"));
  const checkpointPage = pageNumber(searchParams.get("checkpointPage"));
  const pageSize = 100;

  useEffect(() => {
    const controller = new AbortController();
    void getBacktestDetail(
      runId,
      { eventOffset: eventPage * pageSize, checkpointOffset: checkpointPage * pageSize, limit: pageSize },
      controller.signal,
    )
      .then((result) => setDetail(result.data))
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "运行详情读取失败");
      });
    return () => controller.abort();
  }, [checkpointPage, eventPage, runId]);

  function setPage(kind: "eventPage" | "checkpointPage", page: number) {
    const next = new URLSearchParams(searchParams.toString());
    if (page <= 0) next.delete(kind); else next.set(kind, String(page));
    router.push(`/backtests/${encodeURIComponent(runId)}${next.size ? `?${next}` : ""}`);
  }

  return (
    <main id="main-content" className="dashboard statePage" tabIndex={-1}>
      <nav className="breadcrumb" aria-label="面包屑"><Link href="/backtests">← 十年回测</Link><span>/</span><span>{runId}</span></nav>
      {error && <section className="connectionError" role="alert"><span aria-hidden="true">!</span><h1>无法读取运行详情</h1><p>{error}</p></section>}
      {!detail && !error && <section className="panel emptyPanel" aria-live="polite"><p>正在读取不可变运行记录与哈希链…</p></section>}
      {detail && (
        <>
          <section className="pageHeader backtestDetailHeader"><div><p className="eyebrow">BACKTEST RUN</p><h1>{detail.run.run_id}</h1><p className="pageSubtitle">{detail.run.summary}</p></div><span className={`gateState gateState--${detail.run.status}`}>{detail.run.status}</span></section>
          {!detail.run.performance_available && <aside className="warningBanner"><strong>没有绩效结果</strong><span>该运行在执行前被阻断，未生成收益率、净值或模拟持仓。</span></aside>}
          <section className="summaryGrid backtestSummary"><Summary label="用途" value={detail.run.purpose} note={detail.run.segment} /><Summary label="情景" value={detail.run.scenario} note="显式执行假设" /><Summary label="阻塞" value={`${detail.run.blocker_count}`} note="硬门未关闭" /><Summary label="绩效" value={detail.run.performance_available ? "可用" : "未生成"} note="禁止替代结果" /><Summary label="审计链" value={detail.audit_chain_valid ? "完整" : "异常"} note={`${detail.events.length} 条事件`} /><Summary label="数据截止" value={detail.run.data_cutoff ?? "未进入数据"} note={formatTime(detail.run.requested_at)} /><Summary label="策略" value={detail.run.policy_version.includes("draft") ? "draft v2" : "frozen"} note={detail.run.policy_version} /></section>
          {detail.latest_checkpoint && <AccountSnapshot checkpoint={detail.latest_checkpoint} />}
          <div className="backtestGrid">
            <section className="panel backtestPanel"><header className="sectionHeader"><div><p className="eyebrow">BLOCKERS</p><h2>阻断快照</h2></div><span>{detail.blockers.length} 项</span></header><div className="backtestBlockerList">{detail.blockers.map((item) => <article key={item.blocker_id}><div><code>{item.blocker_id}</code><span className={`gateState gateState--${item.status}`}>{item.status}</span></div><p>{item.description}</p>{item.evidence.length > 0 && <small>{item.evidence.join(" · ")}</small>}</article>)}</div></section>
            <section className="panel backtestPanel"><header className="sectionHeader"><div><p className="eyebrow">HASH-CHAINED EVENTS</p><h2>审计事件与证据输入</h2></div><span>{detail.event_count} 条</span></header><div className="auditList">{detail.events.map((event) => <article key={event.event_id}><div><strong>{event.event_type}{event.symbol ? ` · ${event.symbol}` : ""}</strong><time>{formatTime(event.occurred_at)}</time></div><p>序号 {event.sequence} · known_at {formatTime(event.known_at)}</p><details><summary>查看公式输入与事件载荷</summary><pre>{JSON.stringify(event.payload, null, 2)}</pre></details><code title={event.event_hash}>{event.event_hash.slice(0, 20)}…</code></article>)}</div><Pager label="审计事件" page={eventPage} pageSize={pageSize} total={detail.event_count} onChange={(page) => setPage("eventPage", page)} /></section>
          </div>
          <section className="panel backtestPanel backtestRuns"><header className="sectionHeader"><div><p className="eyebrow">CHECKPOINTS</p><h2>账户与绩效检查点</h2></div><span>{detail.checkpoint_count} 个</span></header>{detail.checkpoint_count === 0 ? <div className="emptyPanel"><span aria-hidden="true">∅</span><div><strong>没有检查点</strong><p>执行前已阻断，因此没有生成账户快照或绩效曲线。</p></div></div> : <><div className="dataTableWrap"><table className="dataTable"><thead><tr><th>序号</th><th>时点</th><th>权益</th><th>总收益</th><th>账本闭合</th><th>数据质量</th></tr></thead><tbody>{detail.checkpoints.map((checkpoint) => <tr key={checkpoint.sequence}><td>{checkpoint.sequence}</td><td>{formatTime(checkpoint.as_of)}</td><td>{formatMoney(numberValue(checkpoint.account_summary.equity_cny))}</td><td>{formatPercent(decimalPercent(checkpoint.performance.simple_return))}</td><td>{checkpoint.reconciliation_status}</td><td>{checkpoint.data_quality_status}</td></tr>)}</tbody></table></div><Pager label="检查点" page={checkpointPage} pageSize={pageSize} total={detail.checkpoint_count} onChange={(page) => setPage("checkpointPage", page)} /></>}</section>
        </>
      )}
    </main>
  );
}

function AccountSnapshot({ checkpoint }: { checkpoint: BacktestRunDetail["checkpoints"][number] }) {
  const account = checkpoint.account_summary;
  const performance = checkpoint.performance;
  return <>
    <section className="summaryGrid backtestMetrics" aria-label="组合绩效与资金指标">
      <Summary label="即时权益" value={formatMoney(numberValue(account.equity_cny))} note={`现金 ${formatMoney(numberValue(account.cash_cny))}`} />
      <Summary label="累计外部投入" value={formatMoney(numberValue(account.cumulative_external_contributions_cny))} note={`净投入 ${formatMoney(numberValue(account.net_external_contributions_cny))}`} />
      <Summary label="总盈亏" value={formatMoney(numberValue(performance.total_pnl_cny))} note={formatPercent(decimalPercent(performance.simple_return))} />
      <Summary label="MWRR / XIRR" value={formatPercent(decimalPercent(performance.mwrr_xirr_annualized))} note="资金加权年化" />
      <Summary label="TWR" value={formatPercent(decimalPercent(performance.twr_cumulative))} note="时间加权累计" />
      <Summary label="最大回撤" value={formatPercent(decimalPercent(performance.maximum_drawdown), false)} note="基于单位化收益链" />
      <Summary label="当前市值" value={formatMoney(numberValue(account.market_value_cny))} note={`${String(account.active_position_count ?? 0)} / ${String(account.maximum_position_count ?? 20)} 席位`} />
      <Summary label="累计费用" value={formatMoney(sum(numberValue(account.cumulative_buy_transaction_costs_cny), numberValue(account.cumulative_sell_transaction_costs_cny), numberValue(account.cumulative_dividend_tax_cny)))} note={`红利税 ${formatMoney(numberValue(account.cumulative_dividend_tax_cny))}`} />
    </section>
    <section className="panel backtestPanel backtestRuns"><header className="sectionHeader"><div><p className="eyebrow">SECURITY SUB-LEDGERS</p><h2>单股子账</h2></div><span>{checkpoint.security_ledgers.length} 只</span></header><div className="dataTableWrap"><table className="dataTable"><thead><tr><th>股票</th><th>状态 / 股数</th><th>累计买入含费</th><th>净卖出</th><th>税后分红</th><th>当前市值</th><th>生命周期价值</th><th>累计盈亏</th></tr></thead><tbody>{checkpoint.security_ledgers.map((item) => <tr key={String(item.symbol)}><td><strong>{String(item.name)}</strong><span>{String(item.symbol)} · {String(item.ownership_type)}</span></td><td>{String(item.position_state)} · {String(item.current_quantity)}</td><td>{formatMoney(numberValue(item.cumulative_buy_cost_including_fees_cny))}</td><td>{formatMoney(numberValue(item.cumulative_net_sell_proceeds_cny))}</td><td>{formatMoney(numberValue(item.cumulative_net_dividends_cny))}</td><td>{formatMoney(numberValue(item.current_market_value_cny))}</td><td>{formatMoney(numberValue(item.lifecycle_value_cny))}</td><td>{formatMoney(numberValue(item.total_pnl_cny))}</td></tr>)}</tbody></table></div></section>
    {checkpoint.account_cash_flows.length > 0 && <section className="panel backtestPanel backtestRuns"><header className="sectionHeader"><div><p className="eyebrow">EXTERNAL CASH FLOWS</p><h2>外部资金流</h2></div><span>{checkpoint.account_cash_flows.length} 笔</span></header><div className="dataTableWrap"><table className="dataTable"><thead><tr><th>时间</th><th>类型</th><th>金额</th><th>归因股票</th><th>操作</th></tr></thead><tbody>{checkpoint.account_cash_flows.map((flow) => <tr key={String(flow.flow_id)}><td>{formatTime(String(flow.occurred_at))}</td><td>{String(flow.flow_type)}</td><td>{formatMoney(numberValue(flow.amount_cny))}</td><td>{String(flow.attributed_symbol ?? "—")}</td><td><code>{String(flow.caused_by_operation_id)}</code></td></tr>)}</tbody></table></div></section>}
  </>;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function decimalPercent(value: unknown): number | null {
  const number = numberValue(value);
  return number == null ? null : number * 100;
}

function sum(...values: Array<number | null>): number | null {
  return values.every((value) => value == null) ? null : values.reduce<number>((total, value) => total + (value ?? 0), 0);
}

function pageNumber(value: string | null): number {
  const parsed = Number.parseInt(value ?? "0", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
}

function Pager({ label, page, pageSize, total, onChange }: { label: string; page: number; pageSize: number; total: number; onChange: (page: number) => void }) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  return <nav className="backtestPager" aria-label={`${label}分页`}><button type="button" disabled={page === 0} onClick={() => onChange(page - 1)}>上一页</button><span>第 {page + 1} / {pageCount} 页</span><button type="button" disabled={page + 1 >= pageCount} onClick={() => onChange(page + 1)}>下一页</button></nav>;
}

function Summary({ label, value, note }: { label: string; value: string; note: string }) {
  return <article className="summaryCard"><span>{label}</span><strong>{value}</strong><small title={note}>{note}</small></article>;
}
