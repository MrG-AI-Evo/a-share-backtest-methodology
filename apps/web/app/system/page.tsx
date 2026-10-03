import type { Metadata } from "next";

import { SystemPage } from "@/components/SystemPage";

export const metadata: Metadata = { title: "系统状态 · A股操盘测试" };
export default function Page() { return <SystemPage />; }
