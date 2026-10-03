import type { Metadata } from "next";

import { DecisionsPage } from "@/components/DecisionsPage";

export const metadata: Metadata = { title: "决策历史 · A股操盘测试" };
export default function Page() { return <DecisionsPage />; }
