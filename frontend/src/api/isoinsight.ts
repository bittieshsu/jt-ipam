import { apiClient } from "@/api/client";
import { LONG_OP_TIMEOUT_MS } from "@/api/integrations";
import type { Paginated } from "@/types";

// ISOinsight 整合：登入 ISOinsight 的 HTTP(S) 介面讀 DHCP 租約（唯讀，不改設備設定）。
// 密碼寫入後不回傳（只有 has_password）；Cookie／Token 不存也不回。

export type IsoResult = "success" | "partial" | "failed" | "skipped" | "running";
export type LeaseState = "active" | "expired" | "not_started" | "invalid_period" | "unknown";
export type Bucket = "created" | "updated" | "unchanged" | "observed_only" | "expired" | "unknown_time"
  | "unmatched" | "conflicts";

export interface IsoInsightSource {
  id: string;
  name: string;
  base_url: string;
  product_version: string | null;
  login_path: string;
  login_method: "GET" | "POST";
  post_format: "form" | "json";
  username: string;
  has_password: boolean;
  username_param: string;
  password_param: string;
  auth_mode: "cookie" | "token";
  token_path: string | null;
  token_header: string;
  token_prefix: string;
  lease_path: string;
  verify_tls: boolean;
  source_timezone: string;
  customer_id: string | null;
  scope_subnet_ids: string[];
  create_ips: boolean;
  enabled: boolean;
  schedule_enabled: boolean;
  sync_interval_seconds: number;
  connect_timeout_seconds: number;
  request_timeout_seconds: number;
  job_timeout_seconds: number;
  max_response_mib: number;
  max_rows: number;
  description: string | null;
  config_version: number;
  last_test_ok_at: string | null;
  preview_ok_at: string | null;
  auth_hold: boolean;
  retry_after_until: string | null;
  last_attempt_at: string | null;
  last_fetch_ok_at: string | null;
  last_commit_at: string | null;
  last_full_success_at: string | null;
  last_result: IsoResult | null;
  last_error_code: string | null;
  last_error: string | null;
  last_summary: (Partial<Record<Bucket, number>> & {
    fetched?: number; valid?: number; invalid?: number; duplicates?: number; scope_excluded?: number;
  }) | null;
  running: boolean;
  run_on: "server";
  created_at: string;
  updated_at: string;
}

export type IsoInsightWrite = Partial<Omit<IsoInsightSource,
  "id" | "has_password" | "config_version" | "last_test_ok_at" | "preview_ok_at" | "auth_hold" | "retry_after_until"
  | "last_attempt_at" | "last_fetch_ok_at" | "last_commit_at" | "last_full_success_at" | "last_result"
  | "last_error_code" | "last_error" | "last_summary" | "running" | "run_on" | "created_at" | "updated_at">> & {
  password?: string;
  timezone_confirmed?: boolean;
};

export interface IsoStage {
  stage: string;
  method?: string;
  path?: string;
  status?: number;
  elapsed_ms?: number;
  error?: string;
  login_method?: string;
  post_format?: string;
  cookies?: string[];
  token_found?: boolean;
  content_type?: string | null;
  bytes?: number;
  rows?: number;
  valid?: number;
  warnings?: string[];
  result?: string;
}

export interface IsoProbeSummary {
  fetched: number; valid: number; invalid: number; duplicates: number;
  quality: Record<string, number>; warnings: string[] | null;
}

export interface IsoPreviewRow {
  ip: string; mac: string | null; name: string | null; start: string | null; end: string | null;
  start_raw: string | null; end_raw: string | null; state: LeaseState; quality: string[]; raw_count: number;
  bucket: string; reason: string | null; match_status: string; subnet_cidr: string | null;
  ip_address_id: string | null;
}

export interface IsoTestResult {
  ok: boolean;
  applied: false;
  run_id: string;
  error_code: string | null;
  error?: string;
  stage?: string;
  http_status?: number | null;
  params?: Record<string, unknown>;
  stages: IsoStage[];
  summary?: IsoProbeSummary;
  anonymous?: { status: number | null; readable: boolean; error_code: string | null };
  counts?: Record<Bucket, number>;
  scope_excluded?: number;
  rows?: IsoPreviewRow[];
  rows_total?: number;
}

export interface IsoRun {
  id: string; kind: "sync" | "test" | "preview"; trigger: "manual" | "scheduled";
  started_at: string; finished_at: string | null; duration_ms: number | null; result: IsoResult;
  stage: string | null; http_status: number | null; error_code: string | null; error_detail: string | null;
  login_method: string | null; auth_mode: string | null;
  fetched: number; valid: number; invalid: number; duplicates: number; created: number; updated: number;
  unchanged: number; observed_only: number; expired: number; unknown_time: number; unmatched: number;
  conflicts: number; quality: Record<string, number> | null; warnings: string[] | null;
}

export interface IsoLease {
  id: string; source_id: string; source_name: string | null; ip: string; mac: string | null; name: string | null;
  start_at: string | null; end_at: string | null; start_raw: string | null; end_raw: string | null;
  state: LeaseState; quality: string[]; raw_count: number; match_status: string; subnet_id: string | null;
  subnet_cidr: string | null; ip_address_id: string | null; first_observed_at: string; lease_observed_at: string;
}

export interface IsoLeasePage {
  items: IsoLease[]; total: number; page: number; page_size: number;
  sources: { id: string; name: string; last_commit_at: string | null; last_fetch_ok_at: string | null;
             last_result: IsoResult | null }[];
}

const BASE = "/api/v1/isoinsight";

export async function listSources(): Promise<Paginated<IsoInsightSource>> {
  return (await apiClient.get<Paginated<IsoInsightSource>>(`${BASE}/sources`,
    { params: { page: 1, page_size: 500 } })).data;
}
export async function createSource(p: IsoInsightWrite): Promise<IsoInsightSource> {
  return (await apiClient.post<IsoInsightSource>(`${BASE}/sources`, p)).data;
}
export async function updateSource(id: string, p: IsoInsightWrite): Promise<IsoInsightSource> {
  return (await apiClient.patch<IsoInsightSource>(`${BASE}/sources/${id}`, p)).data;
}
export async function deleteSource(id: string): Promise<void> {
  await apiClient.delete(`${BASE}/sources/${id}`);
}
// 測試與預覽要等設備回應（連線逾時、整次工作上限都可能比前端預設的 15 秒長）
export async function testSource(id: string, anonymousCheck = false): Promise<IsoTestResult> {
  return (await apiClient.post<IsoTestResult>(`${BASE}/sources/${id}/test`, { anonymous_check: anonymousCheck },
    { timeout: LONG_OP_TIMEOUT_MS })).data;
}
export async function previewSource(id: string): Promise<IsoTestResult> {
  return (await apiClient.post<IsoTestResult>(`${BASE}/sources/${id}/preview`, undefined,
    { timeout: LONG_OP_TIMEOUT_MS })).data;
}
export async function syncSource(id: string): Promise<{ task_id: string }> {
  return (await apiClient.post(`${BASE}/sources/${id}/sync`)).data;
}
export async function listRuns(id: string, page = 1, pageSize = 50): Promise<Paginated<IsoRun>> {
  return (await apiClient.get<Paginated<IsoRun>>(`${BASE}/sources/${id}/runs`,
    { params: { page, page_size: pageSize } })).data;
}
export async function listLeases(params: {
  source_id?: string | null; q?: string; mac?: string; state?: LeaseState | null; match_status?: string | null;
  page?: number; page_size?: number;
}): Promise<IsoLeasePage> {
  const clean = Object.fromEntries(Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== ""));
  return (await apiClient.get<IsoLeasePage>(`${BASE}/leases`, { params: clean })).data;
}
