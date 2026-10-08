import { apiClient } from "@/api/client";
import { LONG_OP_TIMEOUT_MS } from "@/api/integrations";

// Check Point 第二階段：閘道的 Gaia API（DHCP 設定；選用：寫死的指令讀 ARP 表與租約）。

export interface GaiaTarget {
  id: string;
  server_id: string;
  domain: string;
  gateway_uid: string | null;
  name: string;
  gaia_url: string;
  username: string;
  has_secret: boolean;
  verify_tls: boolean;
  enabled: boolean;
  sync_interval_seconds: number;
  sync_dhcp: boolean;
  allow_scripts: boolean;
  sync_arp: boolean;
  sync_leases: boolean;
  scope_subnet_ids: string[] | null;
  api_version: string | null;
  last_sync_at: string | null;
  last_error: string | null;
  last_summary: {
    api_version?: string | null; dhcp_subnets?: number; pools?: number; arp_rows?: number; arp_matched?: number;
    leases?: number; lease_matched?: number; errors?: Record<string, string>;
    /** 讀不到但不算錯：no_permission／unsupported／no_lease_file／dhcp_off */
    skipped?: Partial<Record<"arp" | "leases", string>>;
  } | null;
  description: string | null;
}

export interface GaiaTargetWrite {
  name: string;
  gaia_url: string;
  username: string;
  secret?: string;
  gateway_uid?: string | null;
  domain?: string;
  verify_tls?: boolean;
  enabled?: boolean;
  sync_interval_seconds?: number;
  sync_dhcp?: boolean;
  allow_scripts?: boolean;
  sync_arp?: boolean;
  sync_leases?: boolean;
  scope_subnet_ids?: string[];
  description?: string;
}

export interface GaiaTestResult {
  version: string | null;
  dhcp_subnets: number | null;
  scripts: "not_enabled" | "ok" | "denied" | "failed" | "unexpected_output";
  errors?: Record<string, string>;
}

export interface GaiaDhcpSubnet {
  subnet_cidr: string; enabled: boolean; default_gateway: string | null; dns_servers: string[];
  domain_name: string | null; default_lease: number | null; max_lease: number | null;
  pools: { start: string; end: string; include: string; enabled: boolean }[]; synced_at: string | null;
}

export async function listGaiaTargets(serverId: string): Promise<GaiaTarget[]> {
  return (await apiClient.get<GaiaTarget[]>(`/api/v1/checkpoint/servers/${serverId}/gaia-targets`)).data;
}
export async function createGaiaTarget(serverId: string, p: GaiaTargetWrite): Promise<GaiaTarget> {
  return (await apiClient.post<GaiaTarget>(`/api/v1/checkpoint/servers/${serverId}/gaia-targets`, p)).data;
}
export async function updateGaiaTarget(id: string, p: Partial<GaiaTargetWrite>): Promise<GaiaTarget> {
  return (await apiClient.patch<GaiaTarget>(`/api/v1/checkpoint/gaia-targets/${id}`, p)).data;
}
export async function deleteGaiaTarget(id: string): Promise<void> {
  await apiClient.delete(`/api/v1/checkpoint/gaia-targets/${id}`);
}
export async function testGaiaTarget(id: string): Promise<GaiaTestResult> {
  // 連不上的閘道要等到後端的連線逾時才有原因可講；有開指令時還要等指令跑完
  return (await apiClient.post<GaiaTestResult>(`/api/v1/checkpoint/gaia-targets/${id}/test`, undefined,
                                               { timeout: LONG_OP_TIMEOUT_MS })).data;
}
export async function syncGaiaTarget(id: string): Promise<{ task_id: string }> {
  return (await apiClient.post(`/api/v1/checkpoint/gaia-targets/${id}/sync`)).data;
}
export async function listGaiaDhcp(id: string): Promise<GaiaDhcpSubnet[]> {
  return (await apiClient.get<GaiaDhcpSubnet[]>(`/api/v1/checkpoint/gaia-targets/${id}/dhcp`)).data;
}
