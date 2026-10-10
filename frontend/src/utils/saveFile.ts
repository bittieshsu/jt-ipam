/**
 * 瀏覽器端產生的檔案一律從這裡存檔，順便回報一筆匯出稽核（POST /api/v1/export-events）。
 *
 * 表格、報告、拓樸圖、機櫃圖都是在瀏覽器裡用已經取得（而且已依權限過濾）的資料組成的，
 * 伺服器看不到「有人把這份資料存成檔案帶走了」。這裡讓每一次存檔都回報：哪個畫面、格式、幾列。
 * 是瀏覽器自己回報的 —— 補的是可追溯性，不是防止外流的控制（後端 export_events.py 有說明）。
 *
 * 伺服器端產生的下載（子網路 CSV、系統匯出、憑證、報告 PDF、IP 變更評估）伺服器自己會記，
 * 呼叫時帶 `audited: true`，不要重複回報。
 *
 * 守門：src/utils/__tests__/saveFileOnly.test.ts（其他地方不可以自己 createObjectURL 存檔）。
 */

export interface ExportEvent {
  source: string;
  format: string;
  rows: number;
  filename: string;
}

export interface SaveMeta {
  /** 哪個畫面／哪份資料（例：「devices」、「topology」、「rack-R01」） */
  source: string;
  /** 匯出了幾列（圖片之類沒有列數就不填） */
  rows?: number;
  /** 伺服器端已經記過稽核的下載 → 不再回報 */
  audited?: boolean;
}

type Logger = (e: ExportEvent) => void;
let logger: Logger | null = null;

/** main.ts 註冊實際送出的函式；單元測試不註冊就不會打 API。 */
export function setExportLogger(fn: Logger | null): void {
  logger = fn;
}

export function formatOf(filename: string): string {
  const m = /\.([A-Za-z0-9]{1,8})$/.exec(filename);
  return m ? m[1].toLowerCase() : "bin";
}

/** 只回報、不存檔（列印成 PDF 這種不經過下載的匯出用）。 */
export function logExport(source: string, filename: string, rows = 0): void {
  try {
    logger?.({ source: (source || "export").slice(0, 120), format: formatOf(filename), rows,
               filename: filename.slice(0, 200) });
  } catch { /* 回報失敗不影響存檔 */ }
}

export function saveBlob(filename: string, data: Blob | Uint8Array, mime: string, meta: SaveMeta): void {
  if (!meta.audited) logExport(meta.source, filename, meta.rows ?? 0);
  const blob = data instanceof Blob ? data : new Blob([data as BlobPart], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // 立刻 revoke 在部分瀏覽器會讓下載中斷，延後釋放
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}
