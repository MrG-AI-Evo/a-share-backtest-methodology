import type {
  ApiEnvelope,
  BacktestReadiness,
  BacktestRunDetail,
  BacktestRunSummary,
  QualitySampleBacktestReport,
  DashboardPayload,
  DecisionHistory,
  PaperAccountDetail,
  PaperOrder,
  PaperOrderCreate,
  PipelineStatus,
  ResearchCard,
  ScreeningRun,
  StockBars,
  StockQuote,
  SystemStatus,
  DeterministicRuntimeStatus,
  WatchlistItem,
} from "@/lib/types";

const browserApiBase = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";

export class ApiRequestError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
    this.name = "ApiRequestError";
  }
}

export async function getDashboard(signal?: AbortSignal): Promise<ApiEnvelope<DashboardPayload>> {
  const response = await fetch(`${browserApiBase}/dashboard`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    throw new ApiRequestError(`本地 API 返回 ${response.status}`, response.status);
  }
  return (await response.json()) as ApiEnvelope<DashboardPayload>;
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<ApiEnvelope<T>> {
  const response = await fetch(`${browserApiBase}${path}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    signal,
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new ApiRequestError(payload?.detail ?? `本地 API 返回 ${response.status}`, response.status);
  }
  return (await response.json()) as ApiEnvelope<T>;
}

async function writeJson<T>(
  path: string,
  method: "POST" | "DELETE",
  body?: unknown,
  idempotencyKey?: string,
): Promise<ApiEnvelope<T> | null> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const response = await fetch(`${browserApiBase}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new ApiRequestError(payload?.detail ?? `本地 API 返回 ${response.status}`, response.status);
  }
  if (response.status === 204) return null;
  return (await response.json()) as ApiEnvelope<T>;
}

export function getStockQuote(symbol: string, signal?: AbortSignal) {
  return getJson<StockQuote>(`/stocks/${symbol}/quote`, signal);
}

export function getStockBars(
  symbol: string,
  period: StockBars["period"],
  adjustment: StockBars["adjustment"],
  signal?: AbortSignal,
) {
  const params = new URLSearchParams({ period, adjustment, limit: period === "minute" ? "160" : "320", minute_interval: "5" });
  return getJson<StockBars>(`/stocks/${symbol}/bars?${params}`, signal);
}

export function getStockResearch(symbol: string, signal?: AbortSignal) {
  return getJson<ResearchCard | null>(`/stocks/${symbol}/research/latest`, signal);
}

export function getStockResearchHistory(symbol: string, signal?: AbortSignal) {
  return getJson<ResearchCard[]>(`/stocks/${symbol}/research`, signal);
}

export function getWatchlist(signal?: AbortSignal) {
  return getJson<WatchlistItem[]>("/watchlist", signal);
}

export function saveWatchlist(item: { symbol: string; name: string; stage: string; note: string }) {
  return writeJson<WatchlistItem>("/watchlist", "POST", item);
}

export function removeWatchlist(symbol: string) {
  return writeJson<never>(`/watchlist/${symbol}`, "DELETE");
}

export function getPaperAccount(signal?: AbortSignal) {
  return getJson<PaperAccountDetail>("/paper", signal);
}

export function proposePaperOrder(order: PaperOrderCreate, idempotencyKey: string) {
  return writeJson<PaperOrder>("/paper/orders", "POST", order, idempotencyKey);
}

export function approvePaperOrder(orderId: string, reason: string) {
  return writeJson<PaperOrder>(`/paper/orders/${orderId}/approve`, "POST", { reason });
}

export function rejectPaperOrder(orderId: string, reason: string) {
  return writeJson<PaperOrder>(`/paper/orders/${orderId}/reject`, "POST", { reason });
}

export function simulatePaperFill(orderId: string) {
  return writeJson<PaperOrder>(`/paper/orders/${orderId}/simulate-fill`, "POST");
}

export function getDecisions(signal?: AbortSignal) {
  return getJson<DecisionHistory>("/decisions", signal);
}

export function getSystemStatus(signal?: AbortSignal) {
  return getJson<SystemStatus>("/system", signal);
}

export function getDeterministicRuntimeStatus(signal?: AbortSignal) {
  return getJson<DeterministicRuntimeStatus>("/runtime", signal);
}

export function getRuntimeExceptionPackageUrl(runId: string): string {
  return `${browserApiBase}/runtime/runs/${encodeURIComponent(runId)}/exception-package`;
}

export function getPipelineStatus(signal?: AbortSignal) {
  return getJson<PipelineStatus>("/pipeline", signal);
}

export function getLatestScreening(signal?: AbortSignal) {
  return getJson<ScreeningRun | null>("/pipeline/screening/latest", signal);
}

export function getBacktestReadiness(signal?: AbortSignal) {
  return getJson<BacktestReadiness>("/backtests/readiness", signal);
}

export function getBacktestRuns(signal?: AbortSignal) {
  return getJson<BacktestRunSummary[]>("/backtests", signal);
}

export function getQualitySampleBacktestReport(signal?: AbortSignal) {
  return getJson<QualitySampleBacktestReport>("/backtests/reports/quality-sample-11", signal);
}

export function getBacktestDetail(
  runId: string,
  options: { eventOffset?: number; checkpointOffset?: number; limit?: number } = {},
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({
    event_offset: String(options.eventOffset ?? 0),
    checkpoint_offset: String(options.checkpointOffset ?? 0),
    event_limit: String(options.limit ?? 100),
    checkpoint_limit: String(options.limit ?? 100),
  });
  return getJson<BacktestRunDetail>(`/backtests/${encodeURIComponent(runId)}?${query}`, signal);
}
