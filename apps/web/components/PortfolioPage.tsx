"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";

import { approvePaperOrder, getPaperAccount, getStockQuote, getStockResearch, proposePaperOrder, rejectPaperOrder, simulatePaperFill } from "@/lib/api";
import { directionClass, formatMoney, formatNumber, formatPercent, formatTime } from "@/lib/format";
import type { ApiEnvelope, PaperAccountDetail, PaperOrder } from "@/lib/types";

const orderStatus: Record<PaperOrder["status"], string> = {
  PENDING_APPROVAL: "待人工批准", QUEUED: "排队待成交", PARTIAL: "部分成交", FILLED: "已模拟成交", REJECTED: "已拒绝", CANCELLED: "已取消", EXPIRED: "已过期",
};

export function PortfolioPage() {
  const [accountEnvelope, setAccountEnvelope] = useState<ApiEnvelope<PaperAccountDetail> | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [actionNotes, setActionNotes] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    try { setAccountEnvelope(await getPaperAccount()); setError(""); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "模拟账户读取失败"); }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void getPaperAccount(controller.signal)
      .then((result) => { setAccountEnvelope(result); setError(""); })
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : "模拟账户读取失败");
      });
    return () => controller.abort();
  }, []);

  const submitProposal = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const symbol = String(form.get("symbol") ?? "").trim();
    setBusy(true); setError(""); setMessage("正在核验行情、研究版本和风险预算…");
    try {
      const [quote, research] = await Promise.all([getStockQuote(symbol), getStockResearch(symbol)]);
      const side = String(form.get("side")) as "BUY" | "SELL";
      await proposePaperOrder({
        symbol,
        name: quote.data.name,
        side,
        order_type: "LIMIT_SIM",
        limit_price: Number(form.get("limit_price")),
        quantity: Number(form.get("quantity")),
        reason: String(form.get("reason") ?? "").trim(),
        research_version: side === "BUY" ? research.data?._file_path ?? null : null,
        expected_horizon: String(form.get("expected_horizon") ?? "").trim(),
        risk_notes: [String(form.get("risk_notes") ?? "").trim()],
        invalidation_conditions: [String(form.get("invalidation_conditions") ?? "").trim()],
        invalidation_price: side === "BUY" ? Number(form.get("invalidation_price")) : null,
      }, crypto.randomUUID());
      formElement.reset();
      setMessage("模拟提议已创建，仍需人工备注后批准；不会自动成交。 ");
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提议未通过规则门"); setMessage("");
    } finally { setBusy(false); }
  };

  const act = async (order: PaperOrder, action: "approve" | "reject" | "fill") => {
    const note = actionNotes[order.order_id]?.trim();
    if (action !== "fill" && (!note || note.length < 2)) { setError("批准或拒绝前请填写至少 2 个字的人工备注"); return; }
    setBusy(true); setError("");
    try {
      if (action === "approve") await approvePaperOrder(order.order_id, note!);
      if (action === "reject") await rejectPaperOrder(order.order_id, note!);
      if (action === "fill") await simulatePaperFill(order.order_id);
      setMessage(action === "fill" ? "已尝试用当前公开行情模拟成交。" : "人工决定已写入审计链。");
      await load();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "操作失败"); }
    finally { setBusy(false); }
  };

  const account = accountEnvelope?.data;
  if (!account) return <PageState title="正在读取模拟账户" error={error} onRetry={() => void load()} />;
  const { summary } = account;
  return (
    <main id="main-content" className="dashboard statePage" tabIndex={-1}>
      <section className="pageHeader"><div><p className="eyebrow">AUDITABLE PAPER PORTFOLIO</p><h1>模拟组合</h1><p className="pageSubtitle">所有买卖仅存在于本地账本；无券商连接、无自动实盘。</p></div><span className="safetyBadge">● 真实下单关闭</span></section>
      {(error || message) && <aside className={error ? "warningBanner errorBanner" : "infoBanner"} aria-live="polite">{error || message}</aside>}
      {accountEnvelope.meta.warnings.length > 0 && <aside className="warningBanner" role="status"><strong>估值与费用说明</strong><span>{accountEnvelope.meta.warnings.join("；")}</span></aside>}
      <section className="summaryGrid">
        <Summary label="总资产" value={formatMoney(summary.total_assets)} />
        <Summary label="现金" value={formatMoney(summary.cash)} />
        <Summary label="持仓市值" value={formatMoney(summary.market_value)} />
        <Summary label="总收益" value={formatPercent(summary.total_return_pct)} direction={summary.total_return_pct} />
        <Summary label="最大回撤" value={formatPercent(summary.max_drawdown_pct, false)} />
        <Summary label="当前仓位" value={formatPercent(summary.position_pct, false)} />
      </section>

      <section className="panel listPanel"><header className="sectionHeader"><div><p className="eyebrow">POSITIONS</p><h2>当前持仓</h2></div><span>{account.positions.length} 只</span></header>
        {account.positions.length ? <div className="dataTableWrap"><table className="dataTable"><thead><tr><th>标的</th><th>数量 / 可卖</th><th>成本 / 现价</th><th>市值</th><th>浮盈亏</th></tr></thead><tbody>{account.positions.map((position) => <tr key={position.symbol}><td><Link href={`/stocks/${position.symbol}`}><strong>{position.name}</strong><span>{position.symbol}</span></Link></td><td>{position.quantity} / {position.sellable_quantity}</td><td>{formatNumber(position.cost_price)} / {formatNumber(position.last_price)}</td><td>{formatMoney(position.market_value_cny)}</td><td className={`priceDirection--${directionClass(position.unrealized_pnl_pct)}`}>{formatMoney(position.unrealized_pnl_cny)} · {formatPercent(position.unrealized_pnl_pct)}</td></tr>)}</tbody></table></div> : <div className="emptyPanel"><span>◇</span><div><strong>尚未建仓</strong><p>这是正常初始状态。没有通过研究、风险和人工批准门的提议不会进入持仓。</p></div></div>}
      </section>

      <details className="panel proposalPanel"><summary>新建模拟交易提议</summary><p>低频入口：买入必须绑定该股票最新正式研究卡，并通过仓位、风险预算、整手与费用检查。</p>
        <form className="proposalForm" onSubmit={submitProposal}>
          <label><span>代码</span><input name="symbol" inputMode="numeric" autoComplete="off" spellCheck={false} pattern="\d{6}" maxLength={6} required /></label>
          <label><span>方向</span><select name="side" autoComplete="off"><option value="BUY">模拟买入</option><option value="SELL">模拟卖出</option></select></label>
          <label><span>限价</span><input name="limit_price" autoComplete="off" type="number" min="0.01" step="0.01" required /></label>
          <label><span>数量</span><input name="quantity" autoComplete="off" type="number" min="1" step="1" required /></label>
          <label><span>失效价（买入）</span><input name="invalidation_price" autoComplete="off" type="number" min="0.01" step="0.01" /></label>
          <label><span>预期周期</span><input name="expected_horizon" autoComplete="off" spellCheck={false} defaultValue="20_trading_days" required /></label>
          <label className="formSpan"><span>提议原因</span><textarea name="reason" autoComplete="off" minLength={4} required /></label>
          <label className="formSpan"><span>主要风险</span><textarea name="risk_notes" autoComplete="off" required /></label>
          <label className="formSpan"><span>逻辑失效条件</span><textarea name="invalidation_conditions" autoComplete="off" required /></label>
          <button className="primaryButton formSpan" type="submit" disabled={busy}>{busy ? "检查中" : "创建待批准提议"}</button>
        </form>
      </details>

      <section className="panel orderPanel"><header className="sectionHeader"><div><p className="eyebrow">ORDERS & APPROVALS</p><h2>订单与人工批准</h2></div><span>{account.orders.length} 笔</span></header>
        {account.orders.length ? <div className="orderList">{account.orders.map((order) => <article className="orderCard" key={order.order_id}><div className="orderMain"><span className={`sideBadge sideBadge--${order.side}`}>{order.side === "BUY" ? "买入" : "卖出"}</span><div><strong>{order.name} · {order.symbol}</strong><span>{order.quantity} 股 @ {formatNumber(order.limit_price)} · {orderStatus[order.status]}</span></div><time>{formatTime(order.created_at)}</time></div><p>{order.reason}</p><div className="checkList">{order.risk_checks.map((check) => <span className={`check check--${check.status}`} title={check.detail} key={check.check}>{check.status === "PASS" ? "✓" : check.status === "REVIEW" ? "!" : "×"} {check.check}</span>)}</div>{order.status === "PENDING_APPROVAL" && <div className="approvalRow"><input aria-label={`${order.name}人工决定备注`} name={`approval-note-${order.order_id}`} autoComplete="off" placeholder="人工决定备注（必填）…" value={actionNotes[order.order_id] ?? ""} onChange={(event) => setActionNotes((current) => ({ ...current, [order.order_id]: event.target.value }))} /><button type="button" onClick={() => void act(order, "approve")} disabled={busy}>批准排队</button><button className="dangerButton" type="button" onClick={() => void act(order, "reject")} disabled={busy}>拒绝</button></div>}{order.status === "QUEUED" && <div className="approvalRow"><span>最早：{formatTime(order.earliest_fill_at)}</span><button type="button" onClick={() => void act(order, "fill")} disabled={busy}>用当前行情模拟成交</button></div>}</article>)}</div> : <div className="emptyPanel"><p>暂无模拟订单。研究结论不会自动写入账本。</p></div>}
      </section>
      <footer className="dataFooter"><span>策略 {account.policy_version}</span><span>规则 {account.ruleset_version}</span><span>费用 {account.fee_schedule_version}</span><span>估值来源 {accountEnvelope.meta.sources.map((source) => source.provider).join(" / ") || "账本最近记录"}</span></footer>
    </main>
  );
}

function Summary({ label, value, direction }: { label: string; value: string; direction?: number }) { return <article className="summaryCard"><span>{label}</span><strong className={direction === undefined ? "" : `priceDirection--${directionClass(direction)}`}>{value}</strong></article>; }
function PageState({ title, error, onRetry }: { title: string; error: string; onRetry: () => void }) { return <main id="main-content" className="dashboard"><section className="connectionError"><h1>{error || title}</h1>{error && <button className="primaryButton" onClick={onRetry}>重试</button>}</section></main>; }
