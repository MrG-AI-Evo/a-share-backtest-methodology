"use client";

import type { EChartsCoreOption } from "echarts";
import { useMemo } from "react";

import { EChart } from "@/components/EChart";
import type { PriceBar } from "@/lib/types";

export function StockChart({ bars, name }: { bars: PriceBar[]; name: string }) {
  const option = useMemo<EChartsCoreOption>(() => {
    const labels = bars.map((bar) => new Date(bar.timestamp).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }));
    const line = (key: keyof PriceBar) => bars.map((bar) => bar[key]);
    return {
      animation: false,
      backgroundColor: "transparent",
      aria: { enabled: true, description: `${name} K线、成交量、MACD 与 RSI 图` },
      axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#273244" } },
      tooltip: { trigger: "axis", confine: true, borderWidth: 1, borderColor: "#39475a", backgroundColor: "rgba(10,15,23,.96)", textStyle: { color: "#dce3eb", fontSize: 11 } },
      grid: [
        { left: 54, right: 18, top: 28, height: "48%" },
        { left: 54, right: 18, top: "57%", height: "10%" },
        { left: 54, right: 18, top: "71%", height: "11%" },
        { left: 54, right: 18, top: "86%", height: "8%" },
      ],
      xAxis: [0, 1, 2, 3].map((gridIndex) => ({ type: "category", gridIndex, data: labels, boundaryGap: true, axisLine: { lineStyle: { color: "#273244" } }, axisTick: { show: false }, axisLabel: { show: gridIndex === 3, color: "#758194", fontSize: 9 } })),
      yAxis: [0, 1, 2, 3].map((gridIndex) => ({ scale: true, gridIndex, splitNumber: 3, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: "#758194", fontSize: 9 }, splitLine: { lineStyle: { color: "#1d2737" } } })),
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1, 2, 3], start: bars.length > 120 ? 60 : 0, end: 100, zoomOnMouseWheel: true, moveOnMouseMove: true, preventDefaultMouseMove: false },
        { type: "slider", xAxisIndex: [0, 1, 2, 3], bottom: 2, height: 16, borderColor: "#273244", backgroundColor: "#111925", fillerColor: "rgba(240,185,11,.12)", handleStyle: { color: "#f0b90b" }, textStyle: { color: "#758194" } },
      ],
      series: [
        { name: "K线", type: "candlestick", xAxisIndex: 0, yAxisIndex: 0, data: bars.map((bar) => [bar.open, bar.close, bar.low, bar.high]), itemStyle: { color: "#f05355", color0: "#2bb673", borderColor: "#f05355", borderColor0: "#2bb673" } },
        { name: "MA5", type: "line", data: line("ma5"), xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#f0b90b" } },
        { name: "MA10", type: "line", data: line("ma10"), xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#55a4f3" } },
        { name: "MA20", type: "line", data: line("ma20"), xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#b174e8" } },
        { name: "BOLL上", type: "line", data: line("boll_upper"), xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, type: "dashed", color: "#69768a" } },
        { name: "BOLL下", type: "line", data: line("boll_lower"), xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, type: "dashed", color: "#69768a" } },
        { name: "成交量", type: "bar", data: bars.map((bar) => ({ value: bar.volume_shares, itemStyle: { color: bar.close >= bar.open ? "rgba(240,83,85,.65)" : "rgba(43,182,115,.65)" } })), xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 7 },
        { name: "MACD", type: "bar", data: bars.map((bar) => ({ value: bar.macd_hist, itemStyle: { color: (bar.macd_hist ?? 0) >= 0 ? "rgba(240,83,85,.75)" : "rgba(43,182,115,.75)" } })), xAxisIndex: 2, yAxisIndex: 2, barMaxWidth: 5 },
        { name: "DIF", type: "line", data: line("macd_dif"), xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { width: 1, color: "#f0b90b" } },
        { name: "DEA", type: "line", data: line("macd_dea"), xAxisIndex: 2, yAxisIndex: 2, symbol: "none", lineStyle: { width: 1, color: "#55a4f3" } },
        { name: "RSI14", type: "line", data: line("rsi14"), xAxisIndex: 3, yAxisIndex: 3, symbol: "none", lineStyle: { width: 1.4, color: "#b174e8" }, markLine: { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: "#39475a", type: "dashed" }, data: [{ yAxis: 30 }, { yAxis: 70 }] } },
      ],
    };
  }, [bars, name]);

  return <EChart option={option} ariaLabel={`${name}价格与技术指标图`} className="stockChart" />;
}
