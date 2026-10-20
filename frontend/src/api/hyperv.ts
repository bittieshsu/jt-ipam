import { apiClient } from "./client";

/**
 * Hyper-V 整合（WinRM＋PowerShell；直連主機或經 SCVMM）。
 * 路徑要自帶 /api/v1 首碼（client 的 baseURL 是 "/"）。
 */
export type HyperVMode = "hosts" | "scvmm";
/** WinRM 驗證：Kerberos 時帳號寫 user@domain（或填領域）、主機要寫 FQDN */
export type HyperVAuth = "ntlm" | "kerberos";

export interface HyperVInstance {
  id: string;
  name: string;
  mode: HyperVMode;
  host: string;
  extra_hosts?: string | null;
  port: number;
  use_ssl: boolean;
  verify_tls: boolean;
  username: string;
  auth_method: HyperVAuth;
  kerberos_realm?: string | null;
  kerberos_kdc?: string | null;
  enabled: boolean;
  sync_interval_seconds: number;
  scope_subnet_ids?: string[] | null;
  auto_create_ips: boolean;
  description?: string | null;
  cluster_id?: string | null;
  last_sync_at?: string | null;
  last_error?: string | null;
}

export interface HyperVDiagStep {
  step: string;
  ok: boolean;
  detail: string;
  code?: string;
  params?: Record<string, unknown>;
}

/** 新增／編輯送出的欄位（StrictModel：一律送齊，留空送 null） */
export interface HyperVPayload {
  name: string;
  mode: HyperVMode;
  host: string;
  /** hosts：同一個叢集的其他節點；scvmm：備援 VMM 伺服器（換行或逗號分隔） */
  extra_hosts: string | null;
  port: number;
  use_ssl: boolean;
  verify_tls: boolean;
  username: string;
  /** 編輯時留空＝不變更 */
  password?: string;
  auth_method: HyperVAuth;
  /** 帳號寫 DOMAIN\\user 時要填；寫 user@domain 可以留空 */
  kerberos_realm: string | null;
  /** 留空＝查 DNS SRV；填了就用填的（換行或逗號分隔） */
  kerberos_kdc: string | null;
  enabled: boolean;
  sync_interval_seconds: number;
  scope_subnet_ids: string[] | null;
  auto_create_ips: boolean;
  description: string | null;
}

export const HyperV = {
  /** 這台伺服器能不能用 Kerberos（缺 krb5-user 或 pykerberos 就不行） */
  async capabilities(): Promise<{ kerberos: boolean; missing: string[] }> {
    const { data } = await apiClient.get("/api/v1/hyperv/capabilities");
    return data;
  },
  async list(): Promise<HyperVInstance[]> {
    const { data } = await apiClient.get("/api/v1/hyperv");
    return data;
  },
  async create(payload: HyperVPayload): Promise<HyperVInstance> {
    const { data } = await apiClient.post("/api/v1/hyperv", payload);
    return data;
  },
  async update(id: string, payload: Partial<HyperVPayload>): Promise<HyperVInstance> {
    const { data } = await apiClient.patch(`/api/v1/hyperv/${id}`, payload);
    return data;
  },
  async remove(id: string): Promise<void> {
    await apiClient.delete(`/api/v1/hyperv/${id}`);
  },
  async test(id: string): Promise<{ ok: boolean; steps: HyperVDiagStep[] }> {
    const { data } = await apiClient.post(`/api/v1/hyperv/${id}/test`);
    return data;
  },
  /** 背景作業：回 task_id，用 trackTask 顯示進度 */
  async sync(id: string): Promise<{ task_id: string; status: string; queued_at: string }> {
    const { data } = await apiClient.post(`/api/v1/hyperv/${id}/sync`);
    return data;
  },
};
