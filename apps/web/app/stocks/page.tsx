import type { Metadata } from "next";

import { StockSearch } from "@/components/StockSearch";

export const metadata: Metadata = { title: "个股查询 · A股操盘测试" };

export default function StocksPage() {
  return (
    <main id="main-content" className="dashboard stockLookupPage" tabIndex={-1}>
      <section className="pageHeader">
        <div>
          <p className="eyebrow">STOCK RESEARCH</p>
          <h1>个股研究终端</h1>
          <p className="pageSubtitle">公开行情、复权 K 线、本地技术指标与结构化研究卡</p>
        </div>
      </section>
      <section className="panel stockLookupPanel">
        <h2>打开一只股票</h2>
        <p>输入代码后读取本地 API。没有正式研究卡时只显示明确空态，不会自动生成结论。</p>
        <StockSearch />
      </section>
    </main>
  );
}
