"use client";

import * as echarts from "echarts";
import { useEffect, useRef } from "react";

interface EChartProps {
  option: echarts.EChartsCoreOption;
  ariaLabel: string;
  className?: string;
}

export function EChart({ option, ariaLabel, className }: EChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const chart = echarts.init(container, undefined, { renderer: "canvas" });
    chart.setOption({ ...option, animation: false }, { notMerge: true });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(container);
    return () => {
      observer.disconnect();
      chart.dispose();
    };
  }, [option]);

  return <div ref={containerRef} className={className} role="img" aria-label={ariaLabel} />;
}
