"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getDecisions } from "@/lib/api";
import { formatNumber, formatTime } from "@/lib/format";
import type { DecisionHistory } from "@/lib/types";

export function DecisionsPage() {
  const [history, setHistory] = useState<DecisionHistory | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { void getDecisions().then((result) => setHistory(result.data)).catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "决策历史读取失败")); }, []);
  return <main id="main-content" className="dashboard statePage" tabIndex={-1}><section className="pageHeader"><div><p className="eyebrow">DECISION AUDIT TRAIL</p><h1>决策历史</h1><p className="pageSubtitle">研究、提议、人工决定、模拟成交与审计事件的只读时间线。</p></div></section>{error && <aside className="warningBanner errorBanner">{error}</aside>}{!history ? <div className="panel emptyPanel"><p>正在读取审计链…</p></div> : <div className="historyGrid"><section className="panel"><header className="sectionHeader"><div><p className="eyebrow">PAPER ORDERS</p><h2>模拟决策</h2></div><span>{history.orders.length} 笔</span></header>{history.orders.length ? <div className="timeline">{history.orders.map((order) => <article key={order.order_id}><span className={`timelineDot sideBadge--${order.side}`} /><div><time>{formatTime(order.created_at)}</time><h3><Link href={`/stocks/${order.symbol}`}>{order.name} · {order.symbol}</Link></h3><p>{order.side} {order.quantity} 股 @ {formatNumber(order.limit_price)} · {order.status}</p><small>研究：{order.research_version ?? "卖出/未绑定"}<br />规则：{order.ruleset_version}</small></div></article>)}</div> : <div className="emptyPanel"><p>暂无模拟决策。</p></div>}</section><section className="panel"><header className="sectionHeader"><div><p className="eyebrow">HASH-CHAINED AUDIT</p><h2>审计事件</h2></div><span>{history.audit_events.length} 条</span></header>{history.audit_events.length ? <div className="auditList">{history.audit_events.map((event) => <article key={event.event_id}><div><strong>{event.event_type}</strong><time>{formatTime(event.occurred_at)}</time></div><p>{Object.entries(event.payload).map(([key, value]) => `${key}: ${String(value)}`).join(" · ")}</p><code>{event.event_hash.slice(0, 16)}…</code></article>)}</div> : <div className="emptyPanel"><p>审计链尚无事件。</p></div>}</section></div>}</main>;
}
