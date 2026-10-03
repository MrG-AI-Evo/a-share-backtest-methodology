import type { Metadata } from "next";

import { BacktestDetailPage } from "@/components/BacktestDetailPage";

export const metadata: Metadata = { title: "回测详情 · A股操盘测试" };

export default async function Page({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  return <BacktestDetailPage runId={runId} />;
}
