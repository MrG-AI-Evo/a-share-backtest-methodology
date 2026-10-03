import type { Metadata } from "next";

import { BacktestsPage } from "@/components/BacktestsPage";

export const metadata: Metadata = { title: "十年回测 · A股操盘测试" };

export default function Page() {
  return <BacktestsPage />;
}
