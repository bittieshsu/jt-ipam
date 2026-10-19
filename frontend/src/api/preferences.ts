import { apiClient } from "@/api/client";

/** 儀表板「近 14 日 稽核 / IP 異動 趨勢」的個人設定（0219）。空值＝全部計入 */
export type TrendEventGroup = "status" | "mac" | "hostname" | "identity" | "edited" | "lifecycle";
export const TREND_EVENT_GROUPS: TrendEventGroup[] = ["status", "mac", "hostname", "identity", "edited", "lifecycle"];
export interface DashboardTrendPrefs {
  exclude_subnet_ids: string[];
  exclude_dhcp: boolean;
  event_groups: TrendEventGroup[] | null;
}

export interface UserPreferences {
  locale: "zh-TW" | "en-US" | "ja-JP";
  theme: "light" | "dark" | "auto";
  timezone: string;
  calendar: "gregorian" | "minguo";
  page_size: number;
  // 每張表要顯示哪些欄位 (key 由 view 自訂，e.g. "addresses")
  table_columns: Record<string, string[]> | null;
  // Dashboard 常用子網路 UUID 清單
  pinned_subnet_ids: string[] | null;
  // 通用釘選：{namespace: [id,...]}（機房 / 地點 / 機櫃…）
  pinned: Record<string, string[]> | null;
  // SFTP 檔案清單：資料夾優先（與檔案總管一致）或與檔案一起排（像 ls）
  sftp_sort_dirs_first: boolean;
  dashboard?: { trend?: DashboardTrendPrefs | null } | null;
}

export async function getPreferences(): Promise<UserPreferences> {
  const { data } = await apiClient.get<UserPreferences>("/api/v1/me/preferences");
  return data;
}

export async function updatePreferences(
  patch: Partial<UserPreferences>,
): Promise<UserPreferences> {
  const { data } = await apiClient.patch<UserPreferences>("/api/v1/me/preferences", patch);
  return data;
}
