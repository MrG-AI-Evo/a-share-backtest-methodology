import type { Metadata } from "next";

import { PortfolioPage } from "@/components/PortfolioPage";

export const metadata: Metadata = { title: "模拟组合 · A股操盘测试" };
export default function Page() { return <PortfolioPage />; }
