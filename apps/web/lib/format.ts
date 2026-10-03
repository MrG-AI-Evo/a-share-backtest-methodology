const moneyFormatter = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  maximumFractionDigits: 0,
});

const numberFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });

export function formatMoney(value: number | null | undefined): string {
  return value == null ? "—" : moneyFormatter.format(value);
}

export function formatAmount(value: number | null | undefined): string {
  if (value == null) return "—";
  if (Math.abs(value) >= 100_000_000) return `${numberFormatter.format(value / 100_000_000)} 亿`;
  if (Math.abs(value) >= 10_000) return `${numberFormatter.format(value / 10_000)} 万`;
  return numberFormatter.format(value);
}

export function formatBytes(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value >= 1024 ** 3) return `${numberFormatter.format(value / 1024 ** 3)} GB`;
  if (value >= 1024 ** 2) return `${numberFormatter.format(value / 1024 ** 2)} MB`;
  if (value >= 1024) return `${numberFormatter.format(value / 1024)} KB`;
  return `${value} B`;
}

export function formatPercent(value: number | null | undefined, sign = true): string {
  if (value == null) return "—";
  const prefix = sign && value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(2)}%`;
}

export function formatNumber(value: number | null | undefined): string {
  return value == null ? "—" : numberFormatter.format(value);
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function directionClass(value: number): "up" | "down" | "flat" {
  return value > 0 ? "up" : value < 0 ? "down" : "flat";
}
