"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";

import { getLatestScreening, getStockQuote, getWatchlist, removeWatchlist, saveWatchlist } from "@/lib/api";
import { formatTime } from "@/lib/format";
import type { ScreeningRun, WatchlistItem } from "@/lib/types";

const stages = ["自选", "硬筛", "快研", "重点研究", "深度尽调", "重点关注"] as const;

export function WatchlistPage() {
  const [items, setItems] = useState<WatchlistItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [removed, setRemoved] = useState<WatchlistItem | null>(null);
  const [screening, setScreening] = useState<ScreeningRun | null>(null);
  const [screeningWarning, setScreeningWarning] = useState("");

  const load = useCallback(async () => {
    try {
      const result = await getWatchlist();
      setItems(result.data);
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "自选读取失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.allSettled([getWatchlist(controller.signal), getLatestScreening(controller.signal)])
      .then(([watchlistResult, screeningResult]) => {
        if (watchlistResult.status === "fulfilled") {
          setItems(watchlistResult.value.data);
          setError("");
        } else if (!(watchlistResult.reason instanceof DOMException && watchlistResult.reason.name === "AbortError")) {
          setError(watchlistResult.reason instanceof Error ? watchlistResult.reason.message : "自选读取失败");
        }
        if (screeningResult.status === "fulfilled") {
          setScreening(screeningResult.value.data);
          setScreeningWarning(screeningResult.value.meta.warnings.join("；"));
        } else if (!(screeningResult.reason instanceof DOMException && screeningResult.reason.name === "AbortError")) {
          setScreeningWarning(screeningResult.reason instanceof Error ? screeningResult.reason.message : "筛选产物读取失败");
        }
      })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "自选读取失败");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, []);

  const add = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const symbol = String(form.get("symbol") ?? "").trim();
    if (!/^\d{6}$/.test(symbol)) {
      setError("请输入 6 位 A 股代码");
      return;
    }
    setMessage("正在核验代码与公开行情…");
    setError("");
    try {
      const quote = await getStockQuote(symbol);
      await saveWatchlist({
        symbol,
        name: quote.data.name,
        stage: String(form.get("stage") ?? "自选"),
        note: String(form.get("note") ?? "").trim(),
      });
      formElement.reset();
      setMessage(`${quote.data.name} 已加入；操作已写入审计链。`);
      setRemoved(null);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法保存自选");
      setMessage("");
    }
  };

  const remove = async (item: WatchlistItem) => {
    setItems((current) => current.filter((candidate) => candidate.symbol !== item.symbol));
    setRemoved(item);
    setMessage(`${item.name} 已移除，可撤销。`);
    try {
      await removeWatchlist(item.symbol);
    } catch (cause) {
      setItems((current) => [item, ...current]);
      setRemoved(null);
      setError(cause instanceof Error ? cause.message : "移除失败");
    }
  };

  const undo = async () => {
    if (!removed) return;
    try {
      await saveWatchlist({
        symbol: removed.symbol,
        name: removed.name,
        stage: removed.stage,
        note: removed.note,
      });
      setRemoved(null);
      setMessage(`${removed.name} 已恢复。`);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "撤销失败");
    }
  };

  return (
    <main id="main-content" className="dashboard statePage" tabIndex={-1}>
      <section className="pageHeader">
        <div><p className="eyebrow">WATCHLIST & FUNNEL</p><h1>自选与候选</h1><p className="pageSubtitle">只管理观察范围，不在这里批量调用 LLM。</p></div>
        <span className="pageCount">{items.length} 只</span>
      </section>

      <form className="panel compactForm" onSubmit={add}>
        <label><span>股票代码</span><input name="symbol" inputMode="numeric" autoComplete="off" spellCheck={false} maxLength={6} placeholder="例如 300476…" required /></label>
        <label><span>漏斗阶段</span><select name="stage" autoComplete="off" defaultValue="自选">{stages.map((stage) => <option key={stage}>{stage}</option>)}</select></label>
        <label className="formGrow"><span>观察备注</span><input name="note" autoComplete="off" maxLength={500} placeholder="为什么放进观察范围（可选）…" /></label>
        <button className="primaryButton" type="submit">核验并加入</button>
      </form>

      <div className="feedbackLine" aria-live="polite">
        <span className={error ? "formError" : ""}>{error || message || "代码会先经过真实公开行情核验。"}</span>
        {removed && <button type="button" onClick={undo}>撤销移除</button>}
      </div>

      <section className="panel listPanel" aria-labelledby="factor-candidates-title">
        <header className="sectionHeader">
          <div><p className="eyebrow">DETERMINISTIC 300 → 30</p><h2 id="factor-candidates-title">最新因子候选</h2></div>
          <span>{screening ? `${screening.factor_filter.length} 只 · ${formatTime(screening.as_of)}` : "未运行"}</span>
        </header>
        {screening?.factor_filter.length ? (
          <div className="dataTableWrap"><table className="dataTable"><thead><tr><th>排名</th><th>标的</th><th>行业</th><th>确定性评分</th><th>入选依据</th></tr></thead><tbody>
            {screening.factor_filter.map((candidate) => <tr key={candidate.symbol}>
              <td>#{candidate.rank}</td>
              <td><Link href={`/stocks/${candidate.symbol}`}><strong>{candidate.name}</strong><span>{candidate.symbol}</span></Link></td>
              <td>{candidate.industry ?? "UNKNOWN"}</td>
              <td>{candidate.score?.toFixed(2) ?? "—"}</td>
              <td>{candidate.reason_codes.join(" · ") || "—"}</td>
            </tr>)}
          </tbody></table></div>
        ) : <div className="emptyPanel"><span aria-hidden="true">◇</span><div><strong>尚无正式筛选产物</strong><p>{screeningWarning || "先由 Python 对全 A 股执行硬筛和因子评分；这里不会触发 LLM。"}</p></div></div>}
      </section>

      <section className="panel listPanel" aria-busy={loading}>
        {loading ? <div className="emptyPanel"><p>正在读取本地自选…</p></div> : items.length ? (
          <div className="dataTableWrap"><table className="dataTable"><thead><tr><th>标的</th><th>阶段</th><th>备注</th><th>更新时间</th><th><span className="srOnly">操作</span></th></tr></thead><tbody>
            {items.map((item) => <tr key={item.symbol}>
              <td><Link href={`/stocks/${item.symbol}`}><strong>{item.name}</strong><span>{item.symbol}</span></Link></td>
              <td><span className="stageBadge">{item.stage}</span></td>
              <td>{item.note || "—"}</td>
              <td>{formatTime(item.updated_at)}</td>
              <td><button className="textButton textButton--danger" type="button" onClick={() => void remove(item)}>移除</button></td>
            </tr>)}
          </tbody></table></div>
        ) : <div className="emptyPanel"><span aria-hidden="true">◇</span><div><strong>自选仍为空</strong><p>从一只真实代码开始；系统不会自动扩张分析范围。</p></div></div>}
      </section>
    </main>
  );
}
