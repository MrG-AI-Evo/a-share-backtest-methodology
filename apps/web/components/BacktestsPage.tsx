"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { QualitySampleBacktestReport } from "@/components/QualitySampleBacktestReport";
import { getBacktestReadiness, getBacktestRuns, getQualitySampleBacktestReport } from "@/lib/api";
import { formatTime } from "@/lib/format";
import type { BacktestReadiness, BacktestRunSummary, DataState, QualitySampleBacktestReport as QualityReport } from "@/lib/types";

type ViewState = "loading" | "ready" | "error";

export function BacktestsPage() {
  const [readiness, setReadiness] = useState<BacktestReadiness | null>(null);
  const [runs, setRuns] = useState<BacktestRunSummary[]>([]);
  const [qualityReport, setQualityReport] = useState<QualityReport | null>(null);
  const [viewState, setViewState] = useState<ViewState>("loading");
  const [dataState, setDataState] = useState<DataState>("live");
  const [error, setError] = useState("");

  const load = useCallback(async (signal?: AbortSignal) => {
    setViewState("loading");
    setError("");
    try {
      const [readinessResult, runsResult, reportResult] = await Promise.all([
        getBacktestReadiness(signal),
        getBacktestRuns(signal),
        getQualitySampleBacktestReport(signal),
      ]);
      setReadiness(readinessResult.data);
      setRuns(runsResult.data);
      setQualityReport(reportResult.data);
      setDataState(readinessResult.meta.data_state);
      setViewState("ready");
    } catch (cause: unknown) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setError(cause instanceof Error ? cause.message : "回测状态读取失败");
      setViewState("error");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      getBacktestReadiness(controller.signal),
      getBacktestRuns(controller.signal),
      getQualitySampleBacktestReport(controller.signal),
    ])
      .then(([readinessResult, runsResult, reportResult]) => {
        setReadiness(readinessResult.data);
        setRuns(runsResult.data);
        setQualityReport(reportResult.data);
        setDataState(readinessResult.meta.data_state);
        setViewState("ready");
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "回测状态读取失败");
        setViewState("error");
      });
    return () => controller.abort();
  }, []);

  return (
    <main id="main-content" className="dashboard statePage" tabIndex={-1}>
      <section className="pageHeader">
        <div>
          <p className="eyebrow">DETERMINISTIC BACKTEST</p>
          <h1>十年回测</h1>
          <p className="pageSubtitle">先验证数据、规则与账本，再生成收益。页面不计算金融数值。</p>
        </div>
        <button className="refreshButton" type="button" onClick={() => void load()} disabled={viewState === "loading"}>
          {viewState === "loading" ? "检查中" : "重新检查"}
        </button>
      </section>

      {viewState === "error" && (
        <section className="connectionError" role="alert">
          <span aria-hidden="true">!</span><h1>无法读取回测服务</h1><p>{error}</p>
          <button className="primaryButton" type="button" onClick={() => void load()}>重试</button>
        </section>
      )}

      {viewState === "loading" && !readiness && (
        <section className="panel emptyPanel" aria-live="polite"><p>正在校验策略哈希、数据覆盖和安全开关…</p></section>
      )}

      {readiness && viewState !== "error" && (
        <>
          {dataState === "stale" && <aside className="warningBanner">当前展示的是最近一次可用检查结果，数据可能已陈旧。</aside>}
          {!readiness.formal_backtest_executable && (
            <aside className="warningBanner backtestGate" role="status">
              <strong>正式回测尚未执行</strong>
              <span>仍有 {readiness.blockers.length} 项硬阻塞。系统不会用假数据、事后数据或未来函数生成替代收益。</span>
            </aside>
          )}

          {qualityReport && <QualitySampleBacktestReport report={qualityReport} />}

          <section className="summaryGrid backtestSummary" aria-label="回测关键状态">
            <Summary label="策略状态" value={readiness.policy_status} note={readiness.policy_version} />
            <Summary label="主回测" value="10 个完整年份" note={`${readiness.primary_window.start_date} → ${readiness.primary_window.end_date}`} />
            <Summary label="扩展观察" value="独立隔离" note={`${readiness.extension_window.start_date} → ${readiness.extension_window.end_date}`} />
            <Summary label="工程状态" value={readiness.engineering_ready ? "已就绪" : "建设中"} note="引擎·账本·API·Web" />
            <Summary label="正式可执行" value={readiness.formal_backtest_executable ? "是" : "否"} note="所有硬门同时关闭" />
            <Summary label="基线哈希" value={readiness.base_policy_sha256_verified ? "一致" : "异常"} note={formatTime(readiness.checked_at)} />
          </section>

          <div className="backtestGrid">
            <section className="panel backtestPanel">
              <header className="sectionHeader"><div><p className="eyebrow">EXECUTION GATES</p><h2>执行阻塞</h2></div><span>{readiness.blockers.length} 项</span></header>
              <div className="backtestBlockerList">
                {readiness.blockers.map((blocker) => (
                  <article key={blocker.blocker_id}>
                    <div><code>{blocker.blocker_id}</code><span className={`gateState gateState--${blocker.status}`}>{blocker.status}</span></div>
                    <p>{blocker.description}</p>
                    {blocker.evidence.length > 0 && <small>{blocker.evidence.join(" · ")}</small>}
                  </article>
                ))}
              </div>
            </section>

            <section className="panel backtestPanel">
              <header className="sectionHeader"><div><p className="eyebrow">POINT-IN-TIME COVERAGE</p><h2>真实历史数据覆盖</h2></div><span>{readiness.dataset_coverage.length} 类</span></header>
              <div className="dataTableWrap">
                <table className="dataTable backtestCoverageTable">
                  <thead><tr><th>数据集</th><th>行数</th><th>覆盖区间</th><th>合成</th><th>状态</th></tr></thead>
                  <tbody>{readiness.dataset_coverage.map((item) => (
                    <tr key={item.dataset}>
                      <td><strong>{item.dataset}</strong></td><td>{item.row_count.toLocaleString("zh-CN")}</td>
                      <td>{item.minimum_effective_date ?? "—"} → {item.maximum_effective_date ?? "—"}</td>
                      <td>{item.synthetic_row_count}</td>
                      <td><span className={`gateState gateState--${item.ready_for_primary_window ? "CLOSED" : "OPEN"}`}>{item.ready_for_primary_window ? "READY" : "BLOCKED"}</span></td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </section>
          </div>

          <section className="panel backtestPanel backtestRuns">
            <header className="sectionHeader"><div><p className="eyebrow">AUDITABLE RUNS</p><h2>运行记录</h2></div><span>{runs.length} 个</span></header>
            {runs.length === 0 ? (
              <div className="emptyPanel"><span aria-hidden="true">∅</span><div><strong>尚无运行记录</strong><p>就绪预检或正式回测启动后才会追加记录；机制夹具不会进入绩效页面。</p></div></div>
            ) : (
              <div className="dataTableWrap"><table className="dataTable backtestRunTable"><thead><tr><th>运行</th><th>区间</th><th>状态</th><th>阻塞</th><th>绩效</th><th>时间</th></tr></thead><tbody>{runs.map((run) => (
                <tr key={run.run_id}><td><Link href={`/backtests/${run.run_id}`}><strong>{run.run_id}</strong><span>{run.purpose}</span></Link></td><td>{run.segment}</td><td><span className={`gateState gateState--${run.status}`}>{run.status}</span></td><td>{run.blocker_count}</td><td>{run.performance_available ? "可用" : "未生成"}</td><td>{formatTime(run.requested_at)}</td></tr>
              ))}</tbody></table></div>
            )}
          </section>

          <footer className="dataFooter"><span>只读终端 · 无券商连接 · 无真实订单</span><span>主结果与 2026 扩展观察严格分离</span></footer>
        </>
      )}
    </main>
  );
}

function Summary({ label, value, note }: { label: string; value: string; note: string }) {
  return <article className="summaryCard"><span>{label}</span><strong>{value}</strong><small title={note}>{note}</small></article>;
}
