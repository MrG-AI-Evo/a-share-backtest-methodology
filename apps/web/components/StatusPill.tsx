import type { DataState } from "@/lib/types";

const labels: Record<DataState, string> = {
  live: "公开行情已更新",
  delayed: "延迟行情",
  stale: "数据已过期",
  unavailable: "行情不可用",
  demo: "演示数据",
};

export function StatusPill({ state }: { state: DataState }) {
  return (
    <span className={`statusPill statusPill--${state}`} role="status">
      <span className="statusDot" aria-hidden="true" />
      {labels[state]}
    </span>
  );
}
