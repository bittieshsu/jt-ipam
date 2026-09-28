import { apiClient } from "./client";

/**
 * IP 的「探測」（只有管理員）：由負責該子網路的掃描代理對這個 IP 做非侵入式識別。
 * 後端見 endpoints/ip_identify.py；結果經代理的工作佇列回來，前端輪詢。每次的結果都保留。
 */
export interface IdentifyPort {
  port: number; proto: string; state?: string; service?: string; product?: string;
  version?: string; extrainfo?: string; tunnel?: string; scripts?: Record<string, string>;
}
export interface IdentifySummary {
  device_type: string; os: string | null; vendor: string | null; names: string[];
  applications: string[]; services: string[]; evidence: string[]; nmap_available: boolean;
}
export interface IdentifyChanges {
  previous_job_id: string; previous_at: string;
  opened: string[]; closed: string[]; changed: { port: string; before: string; after: string }[];
}
export type IdentifyStatus = "pending" | "running" | "done" | "failed" | "expired";
/** 清單用的精簡版（不帶原始結果） */
export interface IdentifyBrief {
  job_id: string;
  status: IdentifyStatus;
  error?: string | null;
  error_code?: string | null;
  agent_name?: string | null;
  created_at: string; claimed_at?: string | null; finished_at?: string | null;
  summary?: IdentifySummary | null;
}
export interface IdentifyJob extends IdentifyBrief {
  result?: {
    target?: string;
    names?: { rdns?: string | null; netbios?: string | null; mdns?: string | null };
    nmap?: { available?: boolean; error?: string; ports?: IdentifyPort[]; mac?: string | null;
             mac_vendor?: string | null; os?: { name: string; accuracy: number }[] };
    elapsed?: number;
  } | null;
  /** 代理回報的目前階段：names（名稱查詢）→ scan（連接埠／服務／OS） */
  progress?: { stage?: string; elapsed?: number } | null;
  changes?: IdentifyChanges | null;
}

export async function startIdentify(addressId: string): Promise<{ job_id: string; agent_name: string; status: string }> {
  const { data } = await apiClient.post(`/api/v1/addresses/${addressId}/identify`);
  return data;
}

export async function identifyHistory(addressId: string): Promise<IdentifyBrief[]> {
  const { data } = await apiClient.get<{ items: IdentifyBrief[] }>(`/api/v1/addresses/${addressId}/identify/history`);
  return data.items;
}

export async function getIdentify(addressId: string, jobId: string): Promise<IdentifyJob> {
  const { data } = await apiClient.get<IdentifyJob>(`/api/v1/addresses/${addressId}/identify/${jobId}`);
  return data;
}
