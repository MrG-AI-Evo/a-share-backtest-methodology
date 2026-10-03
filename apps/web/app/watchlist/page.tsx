import type { Metadata } from "next";

import { WatchlistPage } from "@/components/WatchlistPage";

export const metadata: Metadata = { title: "自选与候选 · A股操盘测试" };
export default function Page() { return <WatchlistPage />; }
