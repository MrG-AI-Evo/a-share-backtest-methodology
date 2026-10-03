"use client";

import { useEffect, useState } from "react";

import { StatusPill } from "@/components/StatusPill";
import { getDeterministicRuntimeStatus, getRuntimeExceptionPackageUrl, getSystemStatus } from "@/lib/api";
import { formatBytes, formatMoney, formatTime } from "@/lib/format";
import type { AdapterHealth, DeterministicRuntimeStatus, RuntimeTaskDefinition, SystemStatus } from "@/lib/types";

export function SystemPage() {
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [runtime, setRuntime] = useState<DeterministicRuntimeStatus | null>(null);
  const [error, setError] = useState("");
  const [runtimeError, setRuntimeError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void Promise.allSettled([getSystemStatus(controller.signal), getDeterministicRuntimeStatus(controller.signal)])
      .then(([systemResult, runtimeResult]) => {
        if (systemResult.status === "fulfilled") {
          setSystem(systemResult.value.data);
        }
        if (runtimeResult.status === "fulfilled") {
          setRuntime(runtimeResult.value.data);
        }
        if (systemResult.status === "rejected" && !(systemResult.reason instanceof DOMException && systemResult.reason.name === "AbortError")) {
          setError(systemResult.reason instanceof Error ? systemResult.reason.message : "系统状态读取失败");
        }
        if (runtimeResult.status === "rejected" && !(runtimeResult.reason instanceof DOMException && runtimeResult.reason.name === "AbortError")) {
          setRuntimeError(runtimeResult.reason instanceof Error ? runtimeResult.reason.message : "确定性运行时状态读取失败");
        }
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "系统状态读取失败");
      });
    return () => controller.abort();
  }, []);

  const warnings = system
    ? [...system.market_warnings, ...system.research_import_warnings, ...system.ledger_reconciliation_errors]
    : [];

  return (
    <main id="main-content" className="dashboard statePage" tabIndex={-1}>
      <section className="pageHeader">
        <div>
          <p className="eyebrow">LOCAL SYSTEM HEALTH</p>
          <h1>系统状态</h1>
          <p className="pageSubtitle">本地服务、公开源、规则、筛选、研究桥梁与账本状态；不展示任何凭据。</p>
        </div>
      </section>

      {error && <aside className="warningBanner errorBanner">{error}</aside>}
      {!system ? (
        <div className="panel emptyPanel"><p>正在检查本地系统…</p></div>
      ) : (
        <>
          <section className="summaryGrid systemSummary" aria-label="系统关键状态">
            <Summary label="研究卡" value={`${system.research_card_count}`} note="通过 Schema" />
            <Summary label="因子候选" value={`${system.latest_factor_candidate_count}`} note={system.latest_screening_as_of ? formatTime(system.latest_screening_as_of) : "尚未筛选"} />
            <Summary label="研究评测" value={`${system.research_evaluation_pass_count} / ${system.research_evaluation_fail_count}`} note="通过 / 未通过" />
            <Summary label="账本闭合" value={system.ledger_reconciled ? "通过" : "异常"} note="现金·持仓·成交·哈希链" />
            <Summary label="实盘权限" value="关闭" note="硬编码 false" />
            <Summary label="模型 API" value="关闭" note="V1 不接入" />
          </section>

          <RuntimePanel runtime={runtime} error={runtimeError} />

          <div className="historyGrid">
            <section className="panel">
              <header className="sectionHeader">
                <div><p className="eyebrow">DATA SOURCES</p><h2>数据源健康</h2></div>
                <span>{system.data_sources.length} 个</span>
              </header>
              <div className="sourceList">
                {system.data_sources.length ? system.data_sources.map((source, index) => (
                  <article key={`${source.provider}-${index}`}>
                    <div><strong>{source.provider}</strong><StatusPill state={source.state} /></div>
                    <p>源时间 {formatTime(source.source_timestamp)} · 抓取 {formatTime(source.fetched_at)}</p>
                    <small>{source.notes.join("；") || "—"}</small>
                  </article>
                )) : <div className="emptyPanel"><p>市场服务尚未刷新。</p></div>}
              </div>
            </section>

            <section className="panel">
              <header className="sectionHeader">
                <div><p className="eyebrow">ADAPTER CONTRACTS</p><h2>工具与研究桥梁</h2></div>
                <span>{system.adapter_health.length} 个</span>
              </header>
              <div className="sourceList">
                {system.adapter_health.map((adapter) => <AdapterRow key={adapter.adapter_id} adapter={adapter} />)}
              </div>
            </section>

            <section className="panel">
              <header className="sectionHeader"><div><p className="eyebrow">VERSIONS</p><h2>规则与版本</h2></div></header>
              <dl className="versionList">
                <div><dt>API</dt><dd>v{system.api_version} · {system.environment}</dd></div>
                <div><dt>规则库</dt><dd>{system.ruleset_version}</dd></div>
                <div><dt>规则状态</dt><dd>{system.rules_status}</dd></div>
                <div><dt>策略版本</dt><dd>{system.policy_version}</dd></div>
                <div><dt>费用版本</dt><dd>{system.fee_schedule_version}</dd></div>
                <div><dt>筛选运行</dt><dd>{system.latest_screening_run_id ?? "尚无正式产物"}</dd></div>
              </dl>
            </section>

            <section className="panel">
              <header className="sectionHeader"><div><p className="eyebrow">LOCAL STORAGE & SAFETY</p><h2>存储与硬边界</h2></div></header>
              <dl className="versionList">
                <div><dt>模拟账本</dt><dd>{formatBytes(system.paper_database_bytes)} · SQLite</dd></div>
                <div><dt>行情分析</dt><dd>{formatBytes(system.analytics_database_bytes)} · DuckDB</dd></div>
                <div><dt>真实券商</dt><dd>{system.live_broker_enabled ? "异常开启" : "关闭"}</dd></div>
                <div><dt>自动交易</dt><dd>{system.automatic_trading_enabled ? "异常开启" : "关闭"}</dd></div>
                <div><dt>模型 API</dt><dd>{system.llm_api_enabled ? "异常开启" : "关闭"}</dd></div>
              </dl>
              {warnings.length > 0 && <aside className="warningBanner"><span>{warnings.join("；")}</span></aside>}
            </section>
          </div>
        </>
      )}
    </main>
  );
}

function RuntimePanel({ runtime, error }: { runtime: DeterministicRuntimeStatus | null; error: string }) {
  if (error) {
    return <section className="panel runtimePanel"><aside className="warningBanner errorBanner">确定性运行时状态不可用：{error}</aside></section>;
  }
  if (!runtime) {
    return <section className="panel runtimePanel"><p>正在读取确定性任务状态…</p></section>;
  }
  const blockedTasks = runtime.tasks.filter((task) => task.latest_run?.status === "BLOCKED").length;
  return (
    <section className="panel runtimePanel" aria-label="低Token确定性后台运行">
      <header className="sectionHeader">
        <div>
          <p className="eyebrow">LOW-TOKEN DETERMINISTIC RUNTIME</p>
          <h2>本地任务控制面板</h2>
        </div>
        <span>默认 {runtime.status}</span>
      </header>
      <div className="runtimeIntro">
        <strong>无后台模型调用</strong>
        <p>所有入口仅允许手动运行；没有 scheduler、LaunchAgent、cron、heartbeat、Web AI 聊天或自动交易。</p>
      </div>
      <section className="runtimeSummary" aria-label="确定性运行时摘要">
        <Summary label="任务入口" value={`${runtime.tasks.length}`} note="全部默认禁用" />
        <Summary label="调度器" value={runtime.scheduler_enabled ? "异常开启" : "关闭"} note="需用户另行批准" />
        <Summary label="最近阻断" value={`${blockedTasks}`} note="只生成证据包" />
        <Summary label="当前硬门" value={`${runtime.current_blockers.length}`} note="正式回测仍被保护" />
      </section>
      <section className="runtimeBlockers" aria-label="当前回测阻断项">
        <h3>当前阻断项</h3>
        {runtime.current_blockers.length === 0 ? <p>当前没有由运行时报告的阻断项。</p> : (
          <ul>{runtime.current_blockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul>
        )}
      </section>
      <div className="runtimeGrid">
        <section className="runtimeSection">
          <h3>确定性任务</h3>
          <div className="runtimeTaskList">
            {runtime.tasks.map((task) => <RuntimeTaskRow key={task.task_id} task={task} />)}
          </div>
        </section>
        <section className="runtimeSection">
          <h3>年度盈亏</h3>
          {runtime.annual_pnl.length === 0 ? (
            <div className="emptyPanel"><p>没有正式主回测检查点；年度与累计盈亏保持空值。</p></div>
          ) : (
            <div className="dataTableWrap"><table className="dataTable runtimeAnnualTable"><thead><tr><th>年份</th><th>年末资产</th><th>年度净盈亏</th><th>累计盈亏</th><th>归因</th></tr></thead><tbody>{runtime.annual_pnl.map((item) => <tr key={item.year}><td>{item.year}</td><td>{formatMoney(item.year_end_total_assets_cny)}</td><td>{formatMoney(item.annual_net_pnl_cny)}</td><td>{formatMoney(item.cumulative_pnl_cny)}</td><td><details><summary>查看</summary><dl className="runtimeAttribution"><div><dt>分红（税前）</dt><dd>{formatMoney(item.attribution.dividend_gross_cny)}</dd></div><div><dt>费用</dt><dd>{formatMoney((item.attribution.buy_transaction_costs_cny ?? 0) + (item.attribution.sell_transaction_costs_cny ?? 0))}</dd></div><div><dt>税费</dt><dd>{formatMoney(item.attribution.dividend_tax_cny)}</dd></div><div><dt>滑点</dt><dd>{item.attribution.slippage_cny === null ? "未独立记录（已反映于成交价）" : formatMoney(item.attribution.slippage_cny)}</dd></div></dl></details></td></tr>)}</tbody></table></div>
          )}
          <p className="runtimeFormula">年度净盈亏 = 年末总资产 + 当年外部取出 − 年初总资产 − 当年外部注入。费用、税费、分红与滑点仅作归因，不重复计入。</p>
        </section>
      </div>
    </section>
  );
}

function RuntimeTaskRow({ task }: { task: RuntimeTaskDefinition }) {
  const latest = task.latest_run;
  return (
    <article>
      <div className="runtimeTaskHeader"><strong>{task.task_id}</strong><span className={latest?.status === "FAILED" ? "runtimeState runtimeState--error" : "runtimeState"}>{latest?.status ?? "DISABLED"}</span></div>
      <p>{task.description}</p>
      <small>{task.cadence} · {latest ? `最近 ${formatTime(latest.finished_at)} · 截止 ${latest.data_cutoff ?? "未知"}` : "尚未手动运行"}</small>
      {latest && <details><summary>运行证据与例外</summary><dl className="runtimeEvidence"><div><dt>耗时</dt><dd>{latest.execution_duration_ms} ms</dd></div><div><dt>输入哈希</dt><dd>{latest.input_hash}</dd></div><div><dt>规则版本</dt><dd>{latest.ruleset_version} · {latest.policy_version}</dd></div><div><dt>处理结果</dt><dd>股票 {latest.counts.stocks_processed ?? 0} · 候选 {latest.counts.candidates ?? 0} · 买 {latest.counts.buy_count ?? 0} · 卖 {latest.counts.sell_count ?? 0}</dd></div><div><dt>证据包</dt><dd>{latest.exception_package_path ? <a href={getRuntimeExceptionPackageUrl(latest.run_id)} download>导出 JSON</a> : "—"}</dd></div></dl>{latest.exceptions.length > 0 && <ul className="runtimeExceptions">{latest.exceptions.map((item) => <li key={item.code}><strong>{item.code}</strong> · {item.message}</li>)}</ul>}</details>}
    </article>
  );
}

function AdapterRow({ adapter }: { adapter: AdapterHealth }) {
  const state = adapter.enabled ? "live" : adapter.mode === "DISABLED" ? "unavailable" : "delayed";
  return (
    <article>
      <div><strong>{adapter.adapter_id}</strong><StatusPill state={state} /></div>
      <p>{adapter.role} · {adapter.mode} · {adapter.version ?? "版本待锁定"}</p>
      <small>{adapter.notes.join("；") || (adapter.installed ? "已安装" : "未安装")}</small>
    </article>
  );
}

function Summary({ label, value, note }: { label: string; value: string; note: string }) {
  return <article className="summaryCard"><span>{label}</span><strong>{value}</strong><small>{note}</small></article>;
}
