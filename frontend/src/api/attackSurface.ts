/**
 * 對外開放服務清單的 AI 分析（使用者 2026-10-19）。
 *
 * 模型要跑幾十秒，nginx 對 /api/ 只等 30 秒 —— 後端走 SSE＋保活、邊寫邊送。
 * 用 fetch 而非 EventSource（後者不支援 POST，也帶不了 Authorization）。
 */
import { apiClient } from "@/api/client";
import { getAccessToken } from "@/api/token";
import { srvText } from "@/utils/wsError";

export interface SurfaceFacts {
  total: number; targets: number;
  by_via: Record<string, number>; by_firewall: Record<string, number>; by_protocol: Record<string, number>;
  unregistered: unknown[]; sensitive: unknown[]; offline: unknown[]; no_wazuh: unknown[];
  device_kinds: Record<string, number>;
}
export interface SurfaceAiEvent {
  type: "content" | "thinking" | "done" | "error";
  text?: string; elapsed?: number; model?: string; facts?: SurfaceFacts; disclaimer?: string;
  code?: string | null; params?: Record<string, unknown> | null; detail?: string | null;
}

/** `keys`：畫面上（篩選後）那些列的 key；後端照 key 重挑，不採信前端送來的內容。 */
export async function analyzeSurfaceStream(
  keys: string[] | null,
  onEvent: (ev: SurfaceAiEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const base = import.meta.env.VITE_API_BASE_URL || "";
  const token = getAccessToken();
  const resp = await fetch(`${base}/api/v1/anomalies/attack-surface/analyze/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(keys ? { keys } : {}),
    signal,
  });
  if (!resp.ok || !resp.body) {
    let msg = `HTTP ${resp.status}`;
    try {
      const d = (await resp.json())?.detail;
      msg = typeof d === "string" ? d : d && typeof d === "object" ? srvText(d, msg) : msg;
    } catch { /* 非 JSON */ }
    throw new Error(msg);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const raw = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const line = raw.split("\n").find((l) => l.startsWith("data:"));   // 保活是 `: keepalive`，沒有 data
      if (!line) continue;
      const txt = line.slice(5).trim();
      if (!txt) continue;
      try { onEvent(JSON.parse(txt) as SurfaceAiEvent); } catch { /* 壞掉的片段跳過 */ }
    }
  }
}

// ── 外部 DNS 解析（FQDN 頁籤；2026-10-19）──

export interface ExternalDnsAnswer {
  resolver: string; rcode: string; a: string[]; aaaa: string[]; cname: string[];
  ttl: number | null; authoritative: boolean; error: string | null;
}
export interface ExternalDnsName {
  status: string; addresses: string[]; owners: Record<string, string | null>;
  answers: ExternalDnsAnswer[]; checked_at: string | null;
}
export interface ExternalDnsView {
  configured: boolean; public_domains: string[]; last_run_at: string | null;
  last_summary: Record<string, unknown> | null; names: Record<string, ExternalDnsName>;
}
export async function getExternalDns(): Promise<ExternalDnsView> {
  const { data } = await apiClient.get<ExternalDnsView>("/api/v1/anomalies/attack-surface/external-dns");
  return data;
}
export async function runExternalDns(): Promise<{ task_id: string }> {
  const { data } = await apiClient.post<{ task_id: string }>("/api/v1/anomalies/attack-surface/external-dns/run");
  return data;
}
