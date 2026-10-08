import { apiClient } from "@/api/client";
import { LONG_OP_TIMEOUT_MS } from "@/api/integrations";
import type { pdfPayload } from "@/utils/reportExport";

/** 報告排成 PDF（後端內嵌中文字型）；大一點的報告要好幾秒，用長逾時 */
export async function renderReportPdf(payload: ReturnType<typeof pdfPayload>): Promise<Blob> {
  return (await apiClient.post("/api/v1/reports/pdf", payload,
                               { responseType: "blob", timeout: LONG_OP_TIMEOUT_MS })).data;
}
