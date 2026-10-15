import { apiClient } from "@/api/client";
import type { ServerMessage } from "@/utils/wsError";

// SNMP 收集（migration 0217）：管理 → 外部系統整合 → SNMP。路徑帶 /api/v1 前綴（baseURL 為 /），限 admin。
// 憑證的機密（社群字串、v3 密碼）只寫不讀：回應只有 has_*；更新時留空＝不變。

export type SnmpVersion = "v2c" | "v3";
export type SnmpSecurityLevel = "authPriv" | "authNoPriv";
export type SnmpCollector = "auto" | "agent" | "server";
export type SnmpSection = "arp" | "fdb" | "neighbors" | "interfaces";

export const SNMP_SECTIONS: SnmpSection[] = ["arp", "fdb", "neighbors", "interfaces"];
export const SNMP_AUTH_PROTOCOLS = ["SHA", "SHA-224", "SHA-256", "SHA-384", "SHA-512", "MD5"] as const;
export const SNMP_PRIV_PROTOCOLS = ["AES-128", "AES-192", "AES-256", "DES"] as const;
/** 各區段預設間隔與下限（秒），與後端 models/snmp.py 相同 */
export const SNMP_DEFAULT_INTERVALS: Record<SnmpSection, number> = {
  arp: 300, fdb: 900, neighbors: 3600, interfaces: 3600,
};
export const SNMP_MIN_INTERVALS: Record<SnmpSection, number> = {
  arp: 60, fdb: 300, neighbors: 900, interfaces: 900,
};
/** 測試一次最多試幾組憑證（都對同一台設備） */
export const SNMP_MAX_TEST_CREDENTIALS = 5;

export interface SnmpCredential {
  id: string;
  name: string;
  is_default: boolean;
  customer_id: string | null;
  customer_name: string | null;
  version: SnmpVersion;
  username: string | null;
  security_level: SnmpSecurityLevel | null;
  auth_protocol: string | null;
  priv_protocol: string | null;
  timeout_s: number;
  retries: number;
  description: string | null;
  has_community: boolean;
  has_auth_passphrase: boolean;
  has_priv_passphrase: boolean;
  target_count: number;
  created_at: string;
  updated_at: string;
}

export interface SnmpCredentialWrite {
  name?: string;
  is_default?: boolean;
  customer_id?: string | null;
  version?: SnmpVersion;
  username?: string | null;
  security_level?: SnmpSecurityLevel | null;
  auth_protocol?: string | null;
  priv_protocol?: string | null;
  timeout_s?: number;
  retries?: number;
  description?: string | null;
  /** 只寫不讀；更新時不送（或空）＝不變 */
  community?: string;
  auth_passphrase?: string;
  priv_passphrase?: string;
}

/** 一個區段的結果（收集記錄、測試、目標最後一次的摘要都是這個形狀） */
export interface SnmpSectionResult {
  status?: "ok" | "partial" | "error" | "absent" | string;
  rows?: number;
  new?: number;
  removed?: number;
  error?: string;
  elapsed_ms?: number | null;
  table?: string;
  truncated?: boolean;
  at?: string;
}

export interface SnmpSystemInfo {
  sys_name?: string | null;
  sys_descr?: string | null;
  sys_object_id?: string | null;
  sys_contact?: string | null;
  sys_location?: string | null;
  /** sysUpTime（TimeTicks，百分之一秒） */
  sys_uptime?: number | null;
}

export interface SnmpTarget {
  id: string;
  device_id: string;
  device_name: string | null;
  customer_id: string | null;
  credential_id: string;
  credential_name: string | null;
  credential_version: SnmpVersion | null;
  ip_override: string | null;
  mgmt_ip: string | null;
  port: number;
  collect_arp: boolean;
  collect_fdb: boolean;
  collect_neighbors: boolean;
  collect_interfaces: boolean;
  intervals: Partial<Record<SnmpSection, number>> | null;
  effective_intervals: Partial<Record<SnmpSection, number>>;
  collector: SnmpCollector;
  agent_id: string | null;
  effective_agent_id: string | null;
  effective_agent_name: string | null;
  collector_subnet_id: string | null;
  collector_subnet_cidr: string | null;
  collector_error: ServerMessage | null;
  enabled: boolean;
  auto_create_ips: boolean;
  description: string | null;
  last_run_at: string | null;
  last_ok_at: string | null;
  last_error: string | null;
  last_error_code: string | null;
  last_summary: {
    system?: SnmpSystemInfo;
    sections?: Partial<Record<SnmpSection, SnmpSectionResult>>;
  } | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SnmpTargetWrite {
  /** 建立時二擇一：裝置，或一筆 IP（子網路頁「對閘道啟用」：沒有裝置時後端替它建一台 router） */
  device_id?: string;
  ip_address_id?: string;
  /** 空＝預設憑證 */
  credential_id?: string | null;
  port?: number;
  collect_arp?: boolean;
  collect_fdb?: boolean;
  collect_neighbors?: boolean;
  collect_interfaces?: boolean;
  collector?: SnmpCollector;
  agent_id?: string | null;
  enabled?: boolean;
  auto_create_ips?: boolean;
  ip_override?: string | null;
  intervals?: Partial<Record<SnmpSection, number>> | null;
  description?: string | null;
}

export interface SnmpBulkResult {
  created: number;
  updated: number;
  skipped: (ServerMessage & { device_id: string; device_name?: string })[];
}

export interface SnmpJobStart {
  job_id: string;
  run_id: string;
  agent_id: string;
  agent_name: string | null;
  ip: string | null;
  expires_at: string;
}

export interface SnmpTestItem {
  credential_id: string | null;
  credential_name?: string | null;
  ok: boolean;
  elapsed_ms: number | null;
  error: string | null;
  system: SnmpSystemInfo | null;
  sections: Partial<Record<SnmpSection, SnmpSectionResult>>;
}

export interface SnmpRun {
  id: string;
  target_id: string;
  kind: "test" | "collect" | string;
  trigger: "manual" | "scheduled" | string;
  result: "running" | "success" | "partial" | "failed" | string;
  agent_id: string | null;
  agent_name: string | null;
  job_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  sections: Partial<Record<SnmpSection, SnmpSectionResult>> | null;
  info: { system?: SnmpSystemInfo; credentials?: SnmpTestItem[] } | null;
  error_code: string | null;
  error: string | null;
  /** 只有「所有目標」的記錄清單才有 */
  device_id?: string;
  device_name?: string | null;
}

export type SnmpJobStatus = "pending" | "running" | "done" | "failed" | "expired";

export interface SnmpJob {
  job_id: string;
  kind: "snmp_test" | "snmp_collect";
  status: SnmpJobStatus | string;
  error: string | null;
  result: { items: SnmpTestItem[] } | null;
  run: SnmpRun | null;
  created_at: string | null;
  claimed_at: string | null;
  finished_at: string | null;
}

export interface SnmpCollectorPreview {
  ip: string | null;
  collector: SnmpCollector;
  agent_id: string | null;
  agent_name: string | null;
  capable: boolean;
  subnet_id: string | null;
  subnet_cidr: string | null;
  error: ServerMessage | null;
}

export interface SnmpAgentInfo {
  agent_id: string;
  name: string;
  is_local: boolean;
  enabled: boolean;
  agent_version: string | null;
  capable: boolean;
  tool_installed: boolean;
  tool_version: string | null;
  version_ok: boolean;
  target_count: number;
  last_seen_at: string | null;
}

export interface SnmpNeighbor {
  id: string;
  target_id: string;
  device_id: string;
  device_name: string | null;
  protocol: string;
  local_port: string | null;
  remote_sysname: string | null;
  remote_port: string | null;
  remote_chassis_id: string | null;
  remote_mac: string | null;
  remote_mgmt_address: string | null;
  remote_platform: string | null;
  remote_device_id: string | null;
  remote_device_name: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
}

const BASE = "/api/v1/snmp";

// ─────────────── 憑證 ───────────────
export async function listSnmpCredentials(): Promise<SnmpCredential[]> {
  const { data } = await apiClient.get<{ items: SnmpCredential[] }>(`${BASE}/credentials`);
  return data.items ?? [];
}

export async function createSnmpCredential(p: SnmpCredentialWrite): Promise<SnmpCredential> {
  const { data } = await apiClient.post<SnmpCredential>(`${BASE}/credentials`, p);
  return data;
}

export async function updateSnmpCredential(id: string, p: SnmpCredentialWrite): Promise<SnmpCredential> {
  const { data } = await apiClient.patch<SnmpCredential>(`${BASE}/credentials/${id}`, p);
  return data;
}

export async function deleteSnmpCredential(id: string): Promise<void> {
  await apiClient.delete(`${BASE}/credentials/${id}`);
}

// ─────────────── 收集目標 ───────────────
export interface SnmpTargetQuery {
  device_id?: string;
  credential_id?: string;
  agent_id?: string;
  q?: string;
  page?: number;
  page_size?: number;
}

export async function listSnmpTargets(
  params: SnmpTargetQuery = {},
): Promise<{ items: SnmpTarget[]; total: number; page: number; page_size: number }> {
  const { data } = await apiClient.get(`${BASE}/targets`, {
    params: { page: 1, page_size: 500, ...params, q: params.q || undefined },
  });
  return data;
}

/** 逐頁抓完（收集目標是網路設備，量不大；但不可以只拿第一頁就當全部） */
export async function listAllSnmpTargets(params: Omit<SnmpTargetQuery, "page" | "page_size"> = {}): Promise<SnmpTarget[]> {
  const out: SnmpTarget[] = [];
  for (let page = 1; page <= 200; page += 1) {
    const r = await listSnmpTargets({ ...params, page, page_size: 500 });
    out.push(...r.items);
    if (r.items.length < 500 || out.length >= r.total) break;
  }
  return out;
}

/** 這台裝置的收集目標（每台最多一筆）；沒有就回 null */
export async function getSnmpTargetForDevice(deviceId: string): Promise<SnmpTarget | null> {
  const r = await listSnmpTargets({ device_id: deviceId, page_size: 1 });
  return r.items[0] ?? null;
}

export async function getSnmpTarget(id: string): Promise<SnmpTarget> {
  const { data } = await apiClient.get<SnmpTarget>(`${BASE}/targets/${id}`);
  return data;
}

export async function createSnmpTarget(p: SnmpTargetWrite): Promise<SnmpTarget> {
  const { data } = await apiClient.post<SnmpTarget>(`${BASE}/targets`, p);
  return data;
}

export async function updateSnmpTarget(id: string, p: SnmpTargetWrite): Promise<SnmpTarget> {
  const { data } = await apiClient.patch<SnmpTarget>(`${BASE}/targets/${id}`, p);
  return data;
}

export async function deleteSnmpTarget(id: string): Promise<void> {
  await apiClient.delete(`${BASE}/targets/${id}`);
}

export async function bulkSnmpTargets(p: {
  device_ids: string[]; credential_id?: string | null;
  collect_arp?: boolean; collect_fdb?: boolean; collect_neighbors?: boolean; collect_interfaces?: boolean;
  collector?: SnmpCollector; agent_id?: string | null; enabled?: boolean;
}): Promise<SnmpBulkResult> {
  const { data } = await apiClient.post<SnmpBulkResult>(`${BASE}/targets/bulk`, p);
  return data;
}

// ─────────────── 測試／立即收集（代理工作通道） ───────────────
export async function startSnmpTest(targetId: string, credentialIds?: string[]): Promise<SnmpJobStart> {
  const body = credentialIds?.length
    ? { credential_ids: credentialIds.slice(0, SNMP_MAX_TEST_CREDENTIALS) } : {};
  const { data } = await apiClient.post<SnmpJobStart>(`${BASE}/targets/${targetId}/test`, body);
  return data;
}

export async function startSnmpCollect(targetId: string): Promise<SnmpJobStart> {
  const { data } = await apiClient.post<SnmpJobStart>(`${BASE}/targets/${targetId}/collect`);
  return data;
}

export async function getSnmpJob(jobId: string): Promise<SnmpJob> {
  const { data } = await apiClient.get<SnmpJob>(`${BASE}/jobs/${jobId}`);
  return data;
}

// ─────────────── 收集記錄 ───────────────
export async function listSnmpTargetRuns(
  targetId: string, params: { limit?: number; kind?: "test" | "collect" } = {},
): Promise<SnmpRun[]> {
  const { data } = await apiClient.get<{ items: SnmpRun[] }>(`${BASE}/targets/${targetId}/runs`, {
    params: { limit: params.limit ?? 100, kind: params.kind || undefined },
  });
  return data.items ?? [];
}

export async function listSnmpRuns(
  params: { limit?: number; result?: string | null; kind?: string | null } = {},
): Promise<SnmpRun[]> {
  const { data } = await apiClient.get<{ items: SnmpRun[] }>(`${BASE}/runs`, {
    params: { limit: params.limit ?? 300, result: params.result || undefined, kind: params.kind || undefined },
  });
  return data.items ?? [];
}

// ─────────────── 收集者判定、代理能力、鄰居 ───────────────
export async function previewSnmpCollector(params: {
  device_id?: string | null; ip?: string | null; collector?: SnmpCollector; agent_id?: string | null;
}): Promise<SnmpCollectorPreview> {
  const { data } = await apiClient.get<SnmpCollectorPreview>(`${BASE}/collectors`, {
    params: {
      device_id: params.ip ? undefined : (params.device_id || undefined),
      ip: params.ip || undefined,
      collector: params.collector ?? "auto",
      agent_id: params.collector === "agent" ? (params.agent_id || undefined) : undefined,
    },
  });
  return data;
}

export async function listSnmpAgents(): Promise<{ items: SnmpAgentInfo[]; min_agent_version: string }> {
  const { data } = await apiClient.get(`${BASE}/agents`);
  return { items: data.items ?? [], min_agent_version: data.min_agent_version ?? "" };
}

export async function listSnmpNeighbors(
  params: { target_id?: string; device_id?: string; page?: number; page_size?: number } = {},
): Promise<{ items: SnmpNeighbor[]; total: number }> {
  const { data } = await apiClient.get(`${BASE}/neighbors`, {
    params: { page: 1, page_size: 200, ...params },
  });
  return { items: data.items ?? [], total: data.total ?? 0 };
}
