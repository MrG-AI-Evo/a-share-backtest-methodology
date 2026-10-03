import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { StockDetail } from "@/components/StockDetail";

interface StockPageProps {
  params: Promise<{ symbol: string }>;
}

export async function generateMetadata({ params }: StockPageProps): Promise<Metadata> {
  const { symbol } = await params;
  return { title: `${symbol} · 个股研究 · A股操盘测试` };
}

export default async function StockPage({ params }: StockPageProps) {
  const { symbol } = await params;
  if (!/^\d{6}$/.test(symbol)) notFound();
  return <StockDetail symbol={symbol} />;
}
