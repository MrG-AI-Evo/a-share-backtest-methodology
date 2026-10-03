import type { Metadata, Viewport } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { AppNav } from "@/components/AppNav";

import "./globals.css";

export const metadata: Metadata = {
  title: "A股操盘测试 · 本地投研终端",
  description: "公开行情、结构化研究和可审计模拟组合的本地展示终端",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  colorScheme: "dark",
  themeColor: "#0b1018",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <a className="skipLink" href="#main-content">跳到主要内容</a>
        <div className="appShell">
          <header className="topbar">
            <Link className="brand" href="/" aria-label="A股操盘测试首页">
              <span className="brandMark" aria-hidden="true">A</span>
              <span><strong>A股操盘测试</strong><small>RESEARCH SYSTEM</small></span>
            </Link>
            <AppNav />
            <div className="safetyBadge"><span aria-hidden="true">●</span> 仅模拟盘</div>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
