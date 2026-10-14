/**
 * 依內容量欄寬：值都很短的欄（名稱、狀態、數字、時間）不必跟長欄平分寬度，省下的空間留給長欄。
 * 從異常偵測頁的作法抽出來共用（使用者 2026-10-14：DNS 比對群組「名稱、狀態都還有空間可以縮」）。
 * 欄寬仍可拖拉調整（全站表格都可以）。
 */
let ctx: CanvasRenderingContext2D | null | undefined;
const cache = new Map<string, number>();

/** 文字在畫面上的寬度（px）；量不到（測試環境沒有 canvas）時用字數估：全形字 1 個字寬、半形約 0.6 */
export function textWidth(s: string, px = 14): number {
  const key = `${px}|${s}`;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  if (ctx === undefined) {
    try {
      ctx = typeof document !== "undefined" ? document.createElement("canvas").getContext("2d") : null;
    } catch { ctx = null; }
  }
  let w: number;
  if (ctx) {
    ctx.font = `${px}px ${getComputedStyle(document.body).fontFamily}`;
    w = ctx.measureText(s).width;
  } else {
    w = [...s].reduce((sum, ch) => sum + (ch.charCodeAt(0) > 0x2e80 ? px : px * 0.6), 0);
  }
  if (cache.size > 2000) cache.clear();
  cache.set(key, w);
  return w;
}

export const COL_PAD = 26;      // 儲存格左右內距（一般尺寸的表格）
export const SORT_ICON = 30;    // 標頭的排序圖示與拖拉把手
// size="small" 的表格內距較小（左右各 8px）：照一般尺寸算會每欄多出十幾 px，欄一多就得左右捲動
export const COL_PAD_DENSE = 18;
export const SORT_ICON_DENSE = 22;

/** 一欄的寬度：標頭（含排序圖示）與各列內容取最寬，夾在 min～max 之間。`extra` 是每格另外的寬度（標籤內距等）；
 *  `dense` 給 size="small" 的表格用 */
export function fitWidth(header: string, values: Iterable<string>, opts: { min?: number; max?: number; px?: number;
  extra?: number; sortable?: boolean; dense?: boolean } = {}): number {
  const { min = 64, max = 360, px = 14, extra = 0, sortable = true, dense = false } = opts;
  const pad = dense ? COL_PAD_DENSE : COL_PAD;
  let w = textWidth(header) + (sortable ? (dense ? SORT_ICON_DENSE : SORT_ICON) : 0);
  let n = 0;
  for (const v of values) {
    w = Math.max(w, textWidth(v || "—", px) + extra);
    if (++n >= 300) break;          // 列很多時只量前 300 列
  }
  return Math.round(Math.min(max, Math.max(min, w + pad)));
}

/** 表格總寬：欄位寬度加總（超過畫面時左右捲動） */
export function columnsWidth(cols: readonly { width?: unknown; minWidth?: unknown }[]): number {
  return cols.reduce((sum, c) => sum + (Number(c.width) || Number(c.minWidth) || 120), 0);
}
