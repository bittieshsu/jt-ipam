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
  /** Recog 指紋比中的硬體型號／系列 */
  model?: string | null;
  /** 摘要用了哪一版 Recog 指紋庫；沒安裝是 null */
  recog?: string | null;
  applications: string[]; services: string[]; evidence: string[]; nmap_available: boolean;
  /** 探測時主機完全沒有回應（沒有開或關的埠、沒有 MAC 回應、沒有 OS 指紋） */
  no_response?: boolean;
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

/**
 * 探測的對象：IPAM 裡的一筆 IP 記錄，或（IPAM 沒有記錄的）管理網段內的位址 ——
 * 後者給異常偵測的「未授權 IP」用，後端會確認位址在管理的子網路內、由那個子網路的代理執行。
 */
export type IdentifyTarget = { addressId: string } | { ip: string };

function base(target: IdentifyTarget | string): string {
  if (typeof target === "string") return `/api/v1/addresses/${target}/identify`;
  return "addressId" in target
    ? `/api/v1/addresses/${target.addressId}/identify`
    : `/api/v1/identify/ip/${encodeURIComponent(target.ip)}`;
}

export async function startIdentify(target: IdentifyTarget | string): Promise<{ job_id: string; agent_name: string; status: string }> {
  const { data } = await apiClient.post(base(target));
  return data;
}

export async function identifyHistory(target: IdentifyTarget | string): Promise<IdentifyBrief[]> {
  const { data } = await apiClient.get<{ items: IdentifyBrief[] }>(`${base(target)}/history`);
  return data.items;
}

export async function getIdentify(target: IdentifyTarget | string, jobId: string): Promise<IdentifyJob> {
  const { data } = await apiClient.get<IdentifyJob>(`${base(target)}/${jobId}`);
  return data;
}

/** 以位址探測時的標題資訊；已經登記的位址會帶 address_id（畫面改用那筆記錄的探測頁） */
export interface IdentifyIpTarget {
  ip: string; subnet_id: string; subnet_cidr: string; agent_name: string | null; address_id: string | null;
  arp_last_seen?: string | null; arp_source?: string | null;
  /** 0＝IPAM 沒有記錄；>1＝重複記錄（不是未登記） */
  record_count: number;
}
export async function getIdentifyIpTarget(ip: string): Promise<IdentifyIpTarget> {
  const { data } = await apiClient.get<IdentifyIpTarget>(`/api/v1/identify/ip/${encodeURIComponent(ip)}`);
  return data;
}
