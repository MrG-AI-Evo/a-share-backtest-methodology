"use client";

import { useMemo } from "react";
import type { EChartsCoreOption } from "echarts";

import { EChart } from "@/components/EChart";

interface NetValueChartProps {
  totalReturnPct: number;
  asOf: string;
}

export function NetValueChart({ totalReturnPct, asOf }: NetValueChartProps) {
  const option = useMemo<EChartsCoreOption>(
    () => ({
      aria: { enabled: true, description: `模拟组合当前累计收益 ${totalReturnPct.toFixed(2)}%` },
      grid: { left: 8, right: 8, top: 14, bottom: 20, containLabel: true },
      tooltip: { trigger: "axis", confine: true, valueFormatter: (value: unknown) => `${Number(value).toFixed(2)}` },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: [new Date(asOf).toLocaleDateString("zh-CN")],
        axisLabel: { color: "#758194", fontSize: 10 },
        axisLine: { lineStyle: { color: "#273244" } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        min: (value: { min: number }) => Math.min(0.98, value.min - 0.01),
        max: (value: { max: number }) => Math.max(1.02, value.max + 0.01),
        axisLabel: { color: "#758194", fontSize: 10 },
        splitLine: { lineStyle: { color: "#1d2737" } },
      },
      series: [
        {
          name: "净值",
          type: "line",
          data: [1 + totalReturnPct / 100],
          symbol: "circle",
          symbolSize: 7,
          lineStyle: { color: "#f0b90b", width: 2 },
          itemStyle: { color: "#f0b90b" },
          areaStyle: { color: "rgba(240, 185, 11, 0.08)" },
        },
      ],
    }),
    [asOf, totalReturnPct],
  );
  return <EChart option={option} ariaLabel="模拟组合净值曲线" className="netValueChart" />;
}
