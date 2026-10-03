"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { NetValueChart } from "@/components/NetValueChart";
import { StatusPill } from "@/components/StatusPill";
import { getDashboard } from "@/lib/api";
import { directionClass, formatAmount, formatMoney, formatNumber, formatPercent, formatTime } from "@/lib/format";
import type { ApiEnvelope, DashboardPayload, MarketBreadth, SectorPerformance } from "@/lib/types";

type LoadState = "loading" | "ready" | "refreshing" | "error";

const breadthLabels: Array<[keyof MarketBreadth, string]> = [
  ["advancing", "上涨"],
  ["declining", "下跌"],
  ["unchanged", "平盘"],
  ["limit_up", "涨停"],
  ["limit_down", "跌停"],
];

export function Dashboard() {
  const [envelope, setEnvelope] = useState<ApiEnvelope<DashboardPayload> | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [error, setError] = useState<string | null>(null);
  const [sectorView, setSectorView] = useState<"industry" | "concept" | "decliners">("industry");

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await getDashboard(signal);
      setEnvelope(next);
      setError(null);
      setLoadState("ready");
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setError(cause instanceof Error ? cause.message : "无法连接本地 API");
      setLoadState("error");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void getDashboard(controller.signal)
      .then((next) => {
        setEnvelope(next);
        setError(null);
        setLoadState("ready");
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "无法连接本地 API");
        setLoadState("error");
      });
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 15_000);
    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, [load]);

  const refresh = () => {
    setLoadState("refreshing");
    void load();
  };

  const retry = () => {
    setLoadState("loading");
    void load();
  };

  if (!envelope && loadState === "loading") return <DashboardSkeleton />;
  if (!envelope) return <ConnectionError message={error} onRetry={retry} />;

  const { market, paper, research } = envelope.data;
  const source = envelope.meta.sources[0];
  const sectorGroups: Record<typeof sectorView, SectorPerformance[]> = {
    industry: market.sectors.filter((sector) => sector.kind === "industry").sort((a, b) => b.change_pct - a.change_pct).slice(0, 10),
    concept: market.sectors.filter((sector) => sector.kind === "concept").sort((a, b) => b.change_pct - a.change_pct).slice(0, 10),
    decliners: [...market.sectors].sort((a, b) => a.change_pct - b.change_pct).slice(0, 10),
  };
  const visibleSectors = sectorGroups[sectorView];
  const sourceNames = envelope.meta.sources
    .filter((item) => item.state !== "unavailable")
    .map((item) => item.provider)
    .join(" · ");
  return (
    <main id="main-content" className="dashboard" tabIndex={-1}>
      <section className="pageHeader" aria-labelledby="dashboard-title">
        <div>
          <p className="eyebrow">LOCAL RESEARCH TERMINAL</p>
          <h1 id="dashboard-title">市场驾驶舱</h1>
          <p className="pageSubtitle">公开行情 · 结构化研究 · 可审计模拟组合</p>
        </div>
        <div className="headerStatus">
          <StatusPill state={envelope.meta.data_state} />
          <button className="refreshButton" onClick={refresh} disabled={loadState === "refreshing"}>
            {loadState === "refreshing" ? "刷新中…" : "刷新"}
          </button>
          <span className="timestamp">{formatTime(source?.source_timestamp ?? envelope.meta.generated_at)}</span>
        </div>
      </section>

      {envelope.meta.warnings.length > 0 && (
        <aside className="warningBanner" aria-label="数据提示">
          <strong>数据提示</strong>
          <span>{envelope.meta.warnings.join("；")}</span>
        </aside>
      )}

      <section className="indexGrid" aria-label="主要指数">
        {market.indices.length ? (
          market.indices.map((quote) => {
            const direction = directionClass(quote.change_pct);
            return (
              <article className="indexCard" key={quote.symbol}>
                <div className="cardTopline">
                  <h2>{quote.name}</h2>
                  <span>{quote.symbol}</span>
                </div>
                <div className="indexMain">
                  <strong>{formatNumber(quote.price)}</strong>
                  <span className={`priceDirection priceDirection--${direction}`}>
                    {direction === "up" ? "↑" : direction === "down" ? "↓" : "—"} {formatPercent(quote.change_pct)}
                  </span>
                </div>
                <div className="cardMeta"><span>成交额</span><b>{formatAmount(quote.amount_cny)}</b></div>
              </article>
            );
          })
        ) : (
          <EmptyPanel title="指数行情暂不可用" description="请检查本地 API 与网络；系统没有用样例数值替代真实行情。" />
        )}
      </section>

      <div className="dashboardGrid">
        <section className="panel breadthPanel" aria-labelledby="breadth-title">
          <PanelHeader title="市场宽度" eyebrow="MARKET BREADTH" id="breadth-title" />
          <div className="breadthGrid">
            {breadthLabels.map(([key, label]) => (
              <div className="metric" key={key}><span>{label}</span><strong>{formatNumber(market.breadth[key])}</strong></div>
            ))}
          </div>
          <div className="turnoverRow">
            <Metric label="全市场成交额" value={formatAmount(market.breadth.total_amount_cny)} />
            <Metric label="昨日成交额" value={formatAmount(market.breadth.previous_amount_cny)} />
            <Metric label="成交额变化" value={formatPercent(market.breadth.amount_change_pct)} />
          </div>
        </section>

        <section className="panel sectorPanel" aria-labelledby="sector-title">
          <PanelHeader title="板块动能" eyebrow="SECTOR MOMENTUM" id="sector-title" />
          {market.sectors.length ? (
            <>
              <div className="sectorTabs" role="group" aria-label="板块排行口径">
                {([
                  ["industry", "行业涨幅"],
                  ["concept", "概念涨幅"],
                  ["decliners", "跌幅榜"],
                ] as const).map(([value, label]) => (
                  <button
                    key={value}
                    aria-pressed={sectorView === value}
                    className={sectorView === value ? "active" : ""}
                    onClick={() => setSectorView(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="sectorColumns" aria-hidden="true"><span>板块</span><span>涨跌</span><span>成交额</span><span>热度</span></div>
              <ol className="sectorList">
                {visibleSectors.map((sector, index) => (
                  <li key={`${sector.kind}-${sector.name}`}>
                    <span className="rank">{String(index + 1).padStart(2, "0")}</span>
                    <strong title={sector.name}>{sector.name}</strong>
                    <b className={`priceDirection--${directionClass(sector.change_pct)}`}>{formatPercent(sector.change_pct)}</b>
                    <span className="sectorAmount">{formatAmount(sector.amount_cny)}</span>
                    <span className="sectorHeat">{sector.heat_score?.toFixed(0) ?? "—"}</span>
                  </li>
                ))}
              </ol>
            </>
          ) : (
            <EmptyPanel compact title="板块数据待接入" description="该区域会明确标注来源、热度口径与更新时间。" />
          )}
        </section>

        <section className="panel focusPanel" aria-labelledby="focus-title">
          <PanelHeader title="AI 今日关注" eyebrow="RESEARCH WATCHLIST" id="focus-title" trailing={`${research.length} 只`} />
          {research.length ? (
            <div className="researchTableWrap">
              <table className="researchTable">
                <thead><tr><th>标的</th><th>状态</th><th>评分</th><th>核心逻辑</th><th>最大风险</th><th>研究时间</th></tr></thead>
                <tbody>
                  {research.map((card) => (
                    <tr key={card.symbol}>
                      <td data-label="标的"><Link href={`/stocks/${card.symbol}`}><strong>{card.name}</strong><span>{card.symbol}</span></Link></td>
                      <td data-label="状态"><span className={`researchStatus researchStatus--${card.status}`}>{card.status}</span></td>
                      <td data-label="评分"><b className="score">{card.score.toFixed(0)}</b><span className="confidence">置信 {Math.round(card.confidence * 100)}%</span></td>
                      <td data-label="核心逻辑">{card.summary}</td>
                      <td data-label="最大风险" className="riskText">{card.maximum_risk ?? "—"}</td>
                      <td data-label="研究时间">{formatTime(card.as_of)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyPanel title="还没有正式研究卡" description="ChatGPT 完成研究后，将通过追加式 JSON 文件进入这里；旧版本不会被覆盖。" />
          )}
        </section>

        <section className="panel accountPanel" aria-labelledby="account-title">
          <PanelHeader title="模拟账户" eyebrow="PAPER PORTFOLIO" id="account-title" trailing={`${paper.position_pct.toFixed(0)}% 仓位`} />
          <div className="accountHero">
            <div><span>当前总资产</span><strong>{formatMoney(paper.total_assets)}</strong><b className={`priceDirection--${directionClass(paper.total_return_pct)}`}>{formatPercent(paper.total_return_pct)}</b></div>
            <dl>
              <MetricDl label="现金" value={formatMoney(paper.cash)} />
              <MetricDl label="持仓市值" value={formatMoney(paper.market_value)} />
              <MetricDl label="当日收益" value={formatMoney(paper.daily_pnl)} />
              <MetricDl label="最大回撤" value={formatPercent(paper.max_drawdown_pct, false)} />
              <MetricDl label="持仓数量" value={`${paper.holding_count} 只`} />
              <MetricDl label="初始资金" value={formatMoney(paper.initial_cash)} />
            </dl>
          </div>
          <NetValueChart totalReturnPct={paper.total_return_pct} asOf={paper.as_of} />
        </section>
      </div>

      <footer className="dataFooter">
        <span>可用来源：{sourceNames || "无可用行情源"}</span>
        <span>抓取：{formatTime(source?.fetched_at)}</span>
        <span>本地模拟盘 · 非投资建议 · 无实盘下单能力</span>
      </footer>
    </main>
  );
}

function PanelHeader({ title, eyebrow, id, trailing }: { title: string; eyebrow: string; id: string; trailing?: string }) {
  return <header className="panelHeader"><div><p>{eyebrow}</p><h2 id={id}>{title}</h2></div>{trailing && <span>{trailing}</span>}</header>;
}

function Metric({ label, value }: { label: string; value: string }) { return <div className="turnoverMetric"><span>{label}</span><strong>{value}</strong></div>; }

function MetricDl({ label, value }: { label: string; value: string }) { return <div><dt>{label}</dt><dd>{value}</dd></div>; }

function EmptyPanel({ title, description, compact = false }: { title: string; description: string; compact?: boolean }) {
  return <div className={`emptyPanel${compact ? " emptyPanel--compact" : ""}`}><span aria-hidden="true">◇</span><div><strong>{title}</strong><p>{description}</p></div></div>;
}

function DashboardSkeleton() {
  return <main id="main-content" className="dashboard" aria-busy="true"><section className="pageHeader"><div><p className="eyebrow">LOCAL RESEARCH TERMINAL</p><h1>市场驾驶舱</h1><p className="pageSubtitle">正在连接本地数据服务…</p></div></section><div className="skeletonGrid">{Array.from({ length: 6 }, (_, index) => <div className="skeletonCard" key={index} />)}</div></main>;
}

function ConnectionError({ message, onRetry }: { message: string | null; onRetry: () => void }) {
  return <main id="main-content" className="dashboard"><section className="connectionError"><span aria-hidden="true">!</span><p className="eyebrow">LOCAL API OFFLINE</p><h1>本地数据服务未连接</h1><p>{message ?? "请先启动 FastAPI 服务。"}</p><button className="primaryButton" onClick={onRetry}>重新连接</button><small>不会回退到伪造行情，也不会连接任何券商。</small></section></main>;
}
