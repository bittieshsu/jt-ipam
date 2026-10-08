import { apiClient } from "@/api/client";
import { LONG_OP_TIMEOUT_MS } from "@/api/integrations";
import type { Paginated } from "@/types";

// Check Point（R81.20，Management API 唯讀）。整合單位是管理伺服器：一台管多個閘道與政策套件。

export interface CheckPointServer {
  id: string;
  name: string;
  api_url: string;
  verify_tls: boolean;
  auth_mode: "api_key" | "password";
  username: string | null;
  has_secret: boolean;
  domains: string[] | null;
  packages: string[] | null;
  enabled: boolean;
  sync_interval_seconds: number;
  sync_objects: boolean;
  sync_policies: boolean;
  sync_nat: boolean;
  scope_subnet_ids: string[] | null;
  api_version: string | null;
  description: string | null;
  last_sync_at: string | null;
  last_error: string | null;
  last_summary: {
    gateways?: number; objects?: number; rules?: number; nat?: number; domains?: number;
    api_version?: string | null; truncated?: string[]; errors?: Record<string, string>;
  } | null;
}

export interface CheckPointWrite {
  name: string;
  api_url: string;
  verify_tls?: boolean;
  auth_mode?: "api_key" | "password";
  username?: string | null;
  secret?: string;
  domains?: string[];
  packages?: string[];
  enabled?: boolean;
  sync_interval_seconds?: number;
  sync_objects?: boolean;
  sync_policies?: boolean;
  sync_nat?: boolean;
  scope_subnet_ids?: string[];
  description?: string;
}

export interface CheckPointTestDomain {
  domain: string; version: string | null; gateways: number | null; hosts: number | null;
  networks: number | null; groups: number | null; packages: number | null; errors?: Record<string, string>;
}
export interface CheckPointTestResult { version: string | null; domains: CheckPointTestDomain[] }

export interface CheckPointRule {
  id: string; domain: string; package: string; layer: string; section: string | null; rule_number: number;
  name: string | null; action: string | null; enabled: boolean; source: string | null; destination: string | null;
  service: string | null; source_negate: boolean; destination_negate: boolean; install_on: string | null;
  comments: string | null; hits: number | null; last_hit_at: string | null;
}
export interface CheckPointObject {
  id: string; domain: string; name: string; type: string; value: string | null; members: string[];
  comments: string | null;
}
export interface CheckPointGateway {
  domain: string; uid: string; name: string; type: string | null; ipv4_address: string | null; version: string | null;
}
export interface PageOf<T> { total: number; page: number; page_size: number; items: T[] }

const BASE = "/api/v1/checkpoint/servers";

export async function listCheckPoint(): Promise<Paginated<CheckPointServer>> {
  return (await apiClient.get<Paginated<CheckPointServer>>(BASE, { params: { page: 1, page_size: 200 } })).data;
}
export async function createCheckPoint(p: CheckPointWrite): Promise<CheckPointServer> {
  return (await apiClient.post<CheckPointServer>(BASE, p)).data;
}
export async function updateCheckPoint(id: string, p: Partial<CheckPointWrite>): Promise<CheckPointServer> {
  return (await apiClient.patch<CheckPointServer>(`${BASE}/${id}`, p)).data;
}
export async function deleteCheckPoint(id: string): Promise<void> {
  await apiClient.delete(`${BASE}/${id}`);
}
export async function testCheckPoint(id: string): Promise<CheckPointTestResult> {
  // 每個網域都要登入一次；連不上的主機要等到後端的連線逾時才有原因可講
  return (await apiClient.post<CheckPointTestResult>(`${BASE}/${id}/test`, undefined,
                                                     { timeout: LONG_OP_TIMEOUT_MS })).data;
}
export async function syncCheckPoint(id: string): Promise<{ task_id: string }> {
  return (await apiClient.post(`${BASE}/${id}/sync`)).data;
}
export async function listCheckPointRules(id: string,
  params: { q?: string; page: number; page_size: number; rule_id?: string }):
  Promise<PageOf<CheckPointRule>> {
  return (await apiClient.get<PageOf<CheckPointRule>>(`${BASE}/${id}/rules`, { params })).data;
}
export async function listCheckPointObjects(id: string,
  params: { q?: string; page: number; page_size: number; name?: string }):
  Promise<PageOf<CheckPointObject>> {
  return (await apiClient.get<PageOf<CheckPointObject>>(`${BASE}/${id}/objects`, { params })).data;
}
export async function listCheckPointGateways(id: string): Promise<CheckPointGateway[]> {
  return (await apiClient.get<CheckPointGateway[]>(`${BASE}/${id}/gateways`)).data;
}
