import { apiClient } from "@/api/client";
import type { ExportEvent } from "@/utils/saveFile";

/** 瀏覽器端存檔時回報一筆匯出稽核（見 utils/saveFile）。送不出去就算了，不影響存檔。 */
export function reportExportEvent(e: ExportEvent): void {
  void apiClient.post("/api/v1/export-events", e).catch(() => {});
}
