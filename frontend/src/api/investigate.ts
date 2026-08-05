import { apiClient } from "./client";

/**
 * 調查模式：一個位址的完整線索。
 *
 * baseURL 是 "/"，所以路徑要自己帶 /api/v1 首碼（漏掉會打到 SPA 路徑，GET 拿回 index.html）。
 */
export async function investigate(ip: string, narrative = false, lang = "zh-TW") {
  const { data } = await apiClient.get("/api/v1/investigate", {
    params: { ip, narrative, lang },
  });
  return data as {
    dossier: any;
    narrative: string | null;
    narrative_error: string | null;
  };
}
