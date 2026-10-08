import { apiClient } from "@/api/client";
import { LONG_OP_TIMEOUT_MS } from "@/api/integrations";
import type { Paginated } from "@/types";

// Technitium DNS Server 的 DHCP（DNS 那一半是「DNS 伺服器」頁的 technitium 類型）。

export interface TechnitiumDhcpServer {
  id: string;
  name: string;
  api_url: string;
  verify_tls: boolean;
  has_token: boolean;
  enabled: boolean;
  sync_scopes: boolean;
  sync_leases: boolean;
  sync_interval_seconds: number;
  description: string | null;
  scope_subnet_ids: string[] | null;
  last_sync_at: string | null;
  last_error: string | null;
  last_summary: {
    version?: string; server?: string; scopes?: number; pools?: number; reservations?: number;
    leases?: number; lease_rows?: number; scopes_truncated?: boolean;
  } | null;
}

export interface TechnitiumDhcpWrite {
  name: string;
  api_url: string;
  verify_tls?: boolean;
  token?: string;
  enabled?: boolean;
  sync_scopes?: boolean;
  sync_leases?: boolean;
  sync_interval_seconds?: number;
  description?: string;
  scope_subnet_ids?: string[];
}

export interface TechnitiumTestResult {
  version: string; server: string; user: string; scopes: number; enabled_scopes: number; leases: number;
  can_modify: boolean;
}

export interface TechnitiumScope {
  name: string; enabled: boolean; subnet_cidr: string | null; start_ip: string; end_ip: string;
  exclusions: { start: string; end: string }[]; router: string | null; dns_servers: string[];
  ntp_servers: string[]; wins_servers: string[]; domain_name: string | null; lease_seconds: number | null;
  reservations: number; synced_at: string | null;
}

const BASE = "/api/v1/technitium-dhcp/servers";

export async function listTechnitium(): Promise<Paginated<TechnitiumDhcpServer>> {
  return (await apiClient.get<Paginated<TechnitiumDhcpServer>>(BASE, { params: { page: 1, page_size: 200 } })).data;
}
export async function createTechnitium(p: TechnitiumDhcpWrite): Promise<TechnitiumDhcpServer> {
  return (await apiClient.post<TechnitiumDhcpServer>(BASE, p)).data;
}
export async function updateTechnitium(id: string, p: Partial<TechnitiumDhcpWrite>): Promise<TechnitiumDhcpServer> {
  return (await apiClient.patch<TechnitiumDhcpServer>(`${BASE}/${id}`, p)).data;
}
export async function deleteTechnitium(id: string): Promise<void> {
  await apiClient.delete(`${BASE}/${id}`);
}
export async function testTechnitium(id: string): Promise<TechnitiumTestResult> {
  // 連不上的主機要等到後端 30 秒的連線逾時才有原因可講
  return (await apiClient.post<TechnitiumTestResult>(`${BASE}/${id}/test`, undefined,
                                                     { timeout: LONG_OP_TIMEOUT_MS })).data;
}
export async function syncTechnitium(id: string): Promise<{ task_id: string }> {
  return (await apiClient.post(`${BASE}/${id}/sync`)).data;
}
export async function listTechnitiumScopes(id: string): Promise<TechnitiumScope[]> {
  return (await apiClient.get<TechnitiumScope[]>(`${BASE}/${id}/scopes`)).data;
}
