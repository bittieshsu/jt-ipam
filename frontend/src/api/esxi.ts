import { apiClient } from "./client";

/**
 * ESXi / vCenter 整合（vSphere SOAP）。
 * 路徑要自帶 /api/v1 首碼（client 的 baseURL 是 "/"）。
 */
export interface ESXiInstance {
  id: string;
  name: string;
  api_url: string;
  extra_api_urls?: string | null;
  username: string;
  enabled: boolean;
  verify_tls: boolean;
  sync_interval_seconds: number;
  scope_subnet_ids?: string[] | null;
  description?: string | null;
  cluster_id?: string | null;
  last_sync_at?: string | null;
  last_error?: string | null;
}

export interface ESXiDiagStep { step: string; ok: boolean; detail: string }

export const ESXi = {
  async list(): Promise<ESXiInstance[]> {
    const { data } = await apiClient.get("/api/v1/esxi");
    return data;
  },
  async create(payload: Record<string, unknown>): Promise<ESXiInstance> {
    const { data } = await apiClient.post("/api/v1/esxi", payload);
    return data;
  },
  async update(id: string, payload: Record<string, unknown>): Promise<ESXiInstance> {
    const { data } = await apiClient.patch(`/api/v1/esxi/${id}`, payload);
    return data;
  },
  async remove(id: string): Promise<void> {
    await apiClient.delete(`/api/v1/esxi/${id}`);
  },
  async test(id: string): Promise<{ ok: boolean; steps: ESXiDiagStep[] }> {
    const { data } = await apiClient.post(`/api/v1/esxi/${id}/test`);
    return data;
  },
  async sync(id: string): Promise<Record<string, number>> {
    const { data } = await apiClient.post(`/api/v1/esxi/${id}/sync`);
    return data;
  },
};
