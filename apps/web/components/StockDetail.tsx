"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { StatusPill } from "@/components/StatusPill";
import { StockChart } from "@/components/StockChart";
import { getStockBars, getStockQuote, getStockResearchHistory } from "@/lib/api";
import { directionClass, formatAmount, formatNumber, formatPercent, formatTime } from "@/lib/format";
import type { ApiEnvelope, ResearchCard, StockBars, StockQuote } from "@/lib/types";

type Period = StockBars["period"];
type Adjustment = StockBars["adjustment"];

const periods: Array<{ value: Period; label: string }> = [
  { value: "minute", label: "5分钟" },
  { value: "day", label: "日K" },
  { value: "week", label: "周K" },
  { value: "month", label: "月K" },
];

export function StockDetail({ symbol }: { symbol: string }) {
  const [quote, setQuote] = useState<ApiEnvelope<StockQuote> | null>(null);
  const [bars, setBars] = useState<ApiEnvelope<StockBars> | null>(null);
  const [research, setResearch] = useState<ApiEnvelope<ResearchCard[]> | null>(null);
  const [period, setPeriod] = useState<Period>("day");
  const [adjustment, setAdjustment] = useState<Adjustment>("qfq");
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [barsError, setBarsError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const refreshQuote = () => {
      void getStockQuote(symbol, controller.signal)
        .then((payload) => { setQuote(payload); setQuoteError(null); })
        .catch((cause: unknown) => {
          if (cause instanceof DOMException && cause.name === "AbortError") return;
          setQuoteError(cause instanceof Error ? cause.message : "行情加载失败");
        });
    };
    refreshQuote();
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") refreshQuote();
    }, 15_000);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [symbol]);

  useEffect(() => {
    const controller = new AbortController();
    void getStockBars(symbol, period, period === "minute" ? "none" : adjustment, controller.signal)
      .then((payload) => { setBars(payload); setBarsError(null); })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setBarsError(cause instanceof Error ? cause.message : "K线加载失败");
      });
    return () => controller.abort();
  }, [adjustment, period, symbol]);

  useEffect(() => {
    const controller = new AbortController();
    void getStockResearchHistory(symbol, controller.signal).then(setResearch).catch(() => setResearch(null));
    return () => controller.abort();
  }, [symbol]);

  const currentQuote = quote?.data;
  const direction = directionClass(currentQuote?.change_pct ?? 0);
  return (
    <main id="main-content" className="dashboard stockDetail" tabIndex={-1}>
      <div className="breadcrumb"><Link href="/stocks">个股</Link><span>/</span><span>{symbol}</span></div>
      <section className="stockHero" aria-labelledby="stock-title">
        <div>
          <p className="eyebrow">{currentQuote?.exchange ?? "A-SHARE"} · {symbol}</p>
          <h1 id="stock-title">{currentQuote?.name ?? symbol}</h1>
          {currentQuote ? (
            <div className="stockPriceLine">
              <strong>{formatNumber(currentQuote.price)}</strong>
              <span className={`priceDirection--${direction}`}>
                {direction === "up" ? "↑" : direction === "down" ? "↓" : "—"} {formatNumber(currentQuote.change)} · {formatPercent(currentQuote.change_pct)}
              </span>
            </div>
          ) : <p className="pageSubtitle">{quoteError ?? "正在读取公开行情…"}</p>}
        </div>
        <div className="heroMeta">
          {quote && <StatusPill state={quote.meta.data_state} />}
          <span>{formatTime(currentQuote?.updated_at)}</span>
          <small>来源：{quote?.meta.sources[0]?.provider ?? "等待连接"}</small>
        </div>
      </section>

      {currentQuote && (
        <section className="quoteMetrics" aria-label="行情摘要">
          <QuoteMetric label="今开" value={formatNumber(currentQuote.open)} />
          <QuoteMetric label="最高" value={formatNumber(currentQuote.high)} />
          <QuoteMetric label="最低" value={formatNumber(currentQuote.low)} />
          <QuoteMetric label="成交额" value={formatAmount(currentQuote.amount_cny)} />
          <QuoteMetric label="换手率" value={formatPercent(currentQuote.turnover_pct, false)} />
          <QuoteMetric label="市盈率" value={formatNumber(currentQuote.pe_ttm)} />
          <QuoteMetric label="市净率" value={formatNumber(currentQuote.pb)} />
          <QuoteMetric label="总市值" value={formatAmount(currentQuote.total_market_cap_cny)} />
          <QuoteMetric label="流通市值" value={formatAmount(currentQuote.float_market_cap_cny)} />
        </section>
      )}

      <section className="panel chartPanel" aria-labelledby="chart-title">
        <header className="chartToolbar">
          <div><p className="eyebrow">PRICE & INDICATORS</p><h2 id="chart-title">行情图表</h2></div>
          <div className="segmentedControl" aria-label="K线周期">
            {periods.map((item) => <button key={item.value} className={period === item.value ? "active" : undefined} onClick={() => setPeriod(item.value)} aria-pressed={period === item.value}>{item.label}</button>)}
          </div>
          <div className="segmentedControl" aria-label="复权方式">
            {([ ["qfq", "前复权"], ["hfq", "后复权"], ["none", "不复权"] ] as const).map(([value, label]) => <button key={value} disabled={period === "minute"} className={adjustment === value ? "active" : undefined} onClick={() => setAdjustment(value)} aria-pressed={adjustment === value}>{label}</button>)}
          </div>
        </header>
        {bars?.data.bars.length ? <StockChart bars={bars.data.bars} name={currentQuote?.name ?? symbol} /> : <ChartEmpty message={barsError ?? "正在加载 K 线…"} />}
        <footer className="chartLegend"><span><i className="legendUp" />上涨</span><span><i className="legendDown" />下跌</span><span>指标由 Python 本地计算</span><span>{bars ? `${bars.data.adjustment === "qfq" ? "前复权" : bars.data.adjustment === "hfq" ? "后复权" : "不复权"} · ${formatTime(bars.meta.sources[0]?.source_timestamp)}` : ""}</span></footer>
      </section>

      <ResearchCardPanel envelope={research} symbol={symbol} />
      <footer className="dataFooter"><span>公开数据只用于本地研究验证</span><span>不构成投资建议 · 无真实下单能力</span></footer>
    </main>
  );
}

function QuoteMetric({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function ChartEmpty({ message }: { message: string }) {
  return <div className="chartEmpty"><span aria-hidden="true">◇</span><p>{message}</p></div>;
}

function ResearchCardPanel({ envelope, symbol }: { envelope: ApiEnvelope<ResearchCard[]> | null; symbol: string }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selectedIndex = envelope?.data.findIndex((item) => item.research_id === selectedId) ?? -1;
  const card = envelope?.data[selectedIndex >= 0 ? selectedIndex : 0];
  const isHistorical = selectedIndex > 0;
  if (!card) {
    return <section className="panel researchCardPanel"><header className="researchHeader"><div><p className="eyebrow">AI RESEARCH CARD</p><h2>AI 研究卡</h2></div><span className="researchStatus">尚未研究</span></header><div className="emptyPanel"><span aria-hidden="true">◇</span><div><strong>没有通过 schema 校验的正式研究卡</strong><p>ChatGPT 完成研究后，追加保存到 data/research/{symbol}/时间戳.json；旧版本禁止覆盖。</p></div></div></section>;
  }
  return (
    <section className="panel researchCardPanel" aria-labelledby="research-card-title">
      <header className="researchHeader">
        <div><p className="eyebrow">AI RESEARCH CARD · {card.horizon}</p><h2 id="research-card-title">AI 研究卡</h2><p>{isHistorical ? "历史研究" : "最新研究"} · {formatTime(card.as_of)} · 有效至 {formatTime(card.valid_until)}</p>{envelope && envelope.data.length > 1 && <label className="researchVersion"><span>研究版本</span><select value={selectedId ?? card.research_id} onChange={(event) => setSelectedId(event.target.value)}>{envelope.data.map((item, index) => <option key={item.research_id} value={item.research_id}>{index === 0 ? "最新 · " : "历史 · "}{formatTime(item.as_of)} · {item.status} · {item.score.toFixed(0)}</option>)}</select></label>}</div>
        <div className="researchScore"><strong>{card.score.toFixed(0)}</strong><span>/ 100</span><b>{card.status}</b><small>置信度 {Math.round(card.confidence * 100)}%</small></div>
      </header>
      {envelope.meta.warnings.length > 0 && <div className="warningBanner" role="status"><strong>研究质量或导入告警</strong><span>{envelope.meta.warnings.join("；")}</span></div>}
      <p className="researchSummary">{card.summary}</p>
      <div className="thesisGrid">
        <ResearchList title="核心逻辑" tone="neutral" items={card.core_thesis.map((item) => item.text)} />
        <ResearchList title="多头证据" tone="fact" items={card.bull_case.map((item) => item.text)} />
        <ResearchList title="空头证据" tone="risk" items={card.bear_case.map((item) => item.text)} />
        <ResearchList title="催化剂" tone="inference" items={card.catalysts.map((item) => `${item.text}${item.verified ? "（已验证）" : "（待验证）"}`)} />
      </div>
      <div className="evidenceLayers">
        <ResearchList title="🟢 已确认事实" tone="fact" items={card.confirmed_facts.map((item) => item.text)} />
        <ResearchList title="🟡 AI 分析 / 推断" tone="inference" items={card.ai_inferences.map((item) => `${item.text}（${Math.round(item.confidence * 100)}%）`)} />
        <ResearchList title="🔴 风险 / 未验证" tone="risk" items={[...card.risk_flags.map((item) => `${item.text} [${item.severity}]`), ...card.unverified_items.map((item) => `${item.text}；验证：${item.verification_action}`)]} />
      </div>
      <div className="invalidationBlock"><h3>逻辑失效条件</h3>{card.invalidation_conditions.map((item) => <div key={item.condition}><strong>{item.condition}</strong><span>观察：{item.observable}</span><b>动作：{item.action}</b></div>)}</div>
      <footer className="researchFooter"><span>研究 ID：{card.research_id}</span><span>评分：{card.scoring_version ?? "UNKNOWN"}</span><span>规则：{card.ruleset_version ?? "UNKNOWN"}</span><span>{card._file_path}</span></footer>
    </section>
  );
}

function ResearchList({ title, tone, items }: { title: string; tone: "neutral" | "fact" | "inference" | "risk"; items: string[] }) {
  return <section className={`researchList researchList--${tone}`}><h3>{title}</h3>{items.length ? <ul>{items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ul> : <p>暂无</p>}</section>;
}
