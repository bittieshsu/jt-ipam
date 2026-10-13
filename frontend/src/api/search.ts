import { apiClient } from "@/api/client";

export interface SearchHit {
  /** section / subnet / ip_address / device / vlan / vm / vm_record（沒有 IP 記錄的 VM）/ vpn / … */
  type: string;
  id: string;
  label: string;
  sublabel: string | null;
  score: number;
}

export interface SearchResponse {
  detected: string;
  results: SearchHit[];
  /** 結果的排列順序（全站搜尋的設定）；舊後端沒有這個欄位 */
  order?: string[];
}

export async function search(q: string, limitPerType = 8): Promise<SearchResponse> {
  const { data } = await apiClient.get<SearchResponse>("/api/v1/search", {
    params: { q, limit_per_type: limitPerType },
  });
  return data;
}

/** 全站搜尋的設定（管理 → 系統設定 → 全站搜尋）：隨打即找、搜哪些類型與欄位、結果的排列順序 */
export interface SearchTypeSetting {
  type: string;
  enabled: boolean;
  fields: string[];
}
export interface SearchSettings {
  instant: boolean;
  types: SearchTypeSetting[];
  /** 每個類型可以比對的欄位（允許清單） */
  catalog: Record<string, string[]>;
}

let cached: Promise<SearchSettings> | null = null;

/** 搜尋框用：整個分頁共用一份，管理員存檔後呼叫 invalidateSearchSettings() 重抓 */
export function loadSearchSettings(): Promise<SearchSettings> {
  if (!cached) {
    cached = apiClient.get<SearchSettings>("/api/v1/system/search-settings").then((r) => r.data)
      .catch((e) => { cached = null; throw e; });
  }
  return cached;
}
export function invalidateSearchSettings(): void { cached = null; }

export async function getSearchSettings(): Promise<SearchSettings> {
  const { data } = await apiClient.get<SearchSettings>("/api/v1/system/search-settings");
  return data;
}
export async function putSearchSettings(body: Pick<SearchSettings, "instant" | "types">): Promise<SearchSettings> {
  const { data } = await apiClient.put<SearchSettings>("/api/v1/system/search-settings", body);
  invalidateSearchSettings();
  return data;
}
