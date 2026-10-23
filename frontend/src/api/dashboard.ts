import { apiClient } from "@/api/client";

export interface TopSubnet {
  subnet_id: string;
  cidr: string;
  description: string | null;
  section_id: string;
  customer_id: string | null;
  customer_label: string | null;
  used: number;
  total: number;
  used_pct: number;
}

export interface DashboardOverview {
  sections: number;
  subnets: number;
  addresses: number;
  total_capacity: number;
  used: number;
  used_pct: number;
  ipv4_capacity: number;
  ipv4_used: number;
  ipv4_used_pct: number;
  ipv6_subnets: number;
  status: { online: number; offline: number; unknown: number };
  status_sources: string[];
  top_full_subnets: TopSubnet[];
  pinned_subnets: TopSubnet[];
  section_heat: {
    section_id: string;
    name: string;
    subnet_count: number;
    total_hosts: number;
    used: number;
    used_pct: number;
    avg_subnet_pct: number;
    bands: { full: number; high: number; mid: number; low: number };
  }[];
  audit_24h: number;
  devices: number;
  racks: number;
  locations: number;
  vms: number;
  device_types: { type: string; count: number }[];
  customer_resources: { customer_id: string | null; label: string; subnets: number; ips: number; devices: number }[];
  activity_trend: { day: string; audit: number; ip_changes: number }[];
  /** 這個人對 IP 異動趨勢套用了哪些排除 */
  activity_trend_filter?: { active: boolean; excluded_subnets: number; exclude_dhcp: boolean; event_groups: string[] | null };
  rack_usage: { rack_id: string; name: string; used_u: number; total_u: number; pct: number; kind?: string }[];
}

/** 「IP 使用率」卡片的一個子網路（數字跟子網路清單頁同一個定義） */
export interface UsageSubnet {
  subnet_id: string;
  cidr: string;
  description: string | null;
  customer_label: string | null;
  ip_version: 4 | 6;
  used: number;
  total: number;
  used_pct: number;
}

export interface SubnetUsageCards {
  /** pinned＝使用者自選（pinned["usage_subnets"]）；auto＝沒選，自動挑使用率最高的 */
  mode: "pinned" | "auto";
  max: number;
  items: UsageSubnet[];
}

export async function getSubnetUsageCards(): Promise<SubnetUsageCards> {
  const { data } = await apiClient.get<SubnetUsageCards>("/api/v1/dashboard/subnet-usage");
  return data;
}

export async function getOverview(): Promise<DashboardOverview> {
  const { data } = await apiClient.get<DashboardOverview>("/api/v1/dashboard/overview");
  return data;
}
