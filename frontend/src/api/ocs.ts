import { apiClient } from "@/api/client";
import type { Paginated } from "@/types";

// OCS Inventory NG 整合。路徑帶 /api/v1 前綴（baseURL 為 /）。
// 與其他整合最大不同：帳密選用（OCS REST 預設無驗證）。

export interface OcsServer {
  id: string;
  name: string;
  source_type: string;
  base_url: string | null;
  enabled: boolean;
  verify_tls: boolean;
  api_username: string | null;
  /** 有沒有設密碼（後端不回明文） */
  has_password: boolean;
  sync_interval_seconds: number;
  stale_after_days: number;
  sync_networks: boolean;
  sync_bios: boolean;
  /** 預設關：軟體讓每台 ~2 KB → ~80 KB */
  sync_software: boolean;
  detected_version: string | null;
  last_sync_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  last_cost: Record<string, unknown> | null;
}

export interface OcsWrite {
  name: string;
  base_url: string;
  enabled?: boolean;
  verify_tls?: boolean;
  api_username?: string | null;
  api_password?: string;
  /** 明確清掉已存帳密（改回無驗證） */
  clear_credentials?: boolean;
  sync_interval_seconds?: number;
  stale_after_days?: number;
  sync_networks?: boolean;
  sync_bios?: boolean;
  sync_software?: boolean;
}

export interface OcsDiagnosis {
  base_url: string | null;
  source_type: string;
  reachable?: boolean;
  computer_count?: number | null;
  incremental?: boolean;
  auth_required?: boolean;
  /** true = 這套 OCS 沒帶憑證也讀得到 → 該警告站台 */
  unauthenticated_access?: boolean;
}

export async function listOcs(): Promise<Paginated<OcsServer>> {
  const { data } = await apiClient.get<Paginated<OcsServer>>("/api/v1/ocs", {
    params: { page: 1, page_size: 200 },
  });
  return data;
}

export async function createOcs(p: OcsWrite): Promise<OcsServer> {
  const { data } = await apiClient.post<OcsServer>("/api/v1/ocs", p);
  return data;
}

export async function updateOcs(id: string, p: Partial<OcsWrite>): Promise<OcsServer> {
  const { data } = await apiClient.patch<OcsServer>(`/api/v1/ocs/${id}`, p);
  return data;
}

export async function deleteOcs(id: string): Promise<void> {
  await apiClient.delete(`/api/v1/ocs/${id}`);
}

export async function testOcs(id: string): Promise<OcsDiagnosis> {
  const { data } = await apiClient.post<OcsDiagnosis>(`/api/v1/ocs/${id}/test`);
  return data;
}

export async function syncOcs(id: string): Promise<{ task_id: string }> {
  const { data } = await apiClient.post(`/api/v1/ocs/${id}/sync`);
  return data;
}
