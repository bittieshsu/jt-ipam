/**
 * 戰情室的純函式（vitest 直接測）。規範：JT Matrix v1.1（/root/jt-ipam-design，內部）。
 */

/**
 * 名稱的斷行點：FQDN 在「句點後」、英文在空白與既有的連字號後可以換行。
 * 回傳的片段串起來與原字串一字不差（只是在片段之間放 <wbr>），複製出來的值不變。
 * 句點留在前一行的結尾，避免新的一行以句點開頭。
 */
export function breakPoints(s: string): string[] {
  if (!s) return [];
  const out: string[] = [];
  let buf = "";
  for (const ch of s) {
    buf += ch;
    if (ch === "." || ch === "-" || ch === "/" || ch === "_") {
      out.push(buf);
      buf = "";
    }
  }
  if (buf) out.push(buf);
  return out;
}

export type Translate = (key: string, params?: Record<string, unknown>) => string;

/** 多久以前：{value, unit}（數字與單位分開排版：數字大、單位小） */
export function agoParts(seconds: number | null | undefined, t: Translate): { value: string; unit: string } {
  if (seconds == null || !Number.isFinite(seconds)) return { value: "—", unit: "" };
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return { value: String(s), unit: t("warroom.unit_s_ago") };
  if (s < 3600) return { value: String(Math.floor(s / 60)), unit: t("warroom.unit_min_ago") };
  if (s < 86400) return { value: String(Math.floor(s / 3600)), unit: t("warroom.unit_h_ago") };
  return { value: String(Math.floor(s / 86400)), unit: t("warroom.unit_d_ago") };
}

/** 兩位數頁碼：1 → "01" */
export function pad2(n: number): string {
  return String(Math.max(0, n)).padStart(2, "0");
}

export interface Step { key: string; page: number }

/**
 * 輪播的下一步。主題頁碼（01 到 07）與資料子頁碼分開：同一個主題最多播 maxSubpages 個子頁
 * （大型站台的「網段 01–04 / 28」不會一直霸佔輪播），播完換下一個主題、回到它的第 1 頁。
 * pages：這個主題目前回報的總頁數。
 */
export function nextStep(playlist: string[], cur: Step, pages: number, maxSubpages: number): Step {
  if (!playlist.length) return cur;
  const limit = Math.max(1, Math.min(pages, maxSubpages));
  if (cur.page < limit) return { key: cur.key, page: cur.page + 1 };
  const i = playlist.indexOf(cur.key);
  return { key: playlist[(i + 1 + playlist.length) % playlist.length], page: 1 };
}

/** 上一步（手動按左鍵）：同主題的上一個子頁；已是第 1 頁就到上一個主題的第 1 頁 */
export function prevStep(playlist: string[], cur: Step): Step {
  if (!playlist.length) return cur;
  if (cur.page > 1) return { key: cur.key, page: cur.page - 1 };
  const i = playlist.indexOf(cur.key);
  return { key: playlist[(i - 1 + playlist.length) % playlist.length], page: 1 };
}

/**
 * 手動換資料頁（↑ ↓、PgUp PgDn、右下箭頭）：在同一個主題內循環，不跳到別的主題
 * （10-22 使用者：「已在最後一頁按 down 或 pgdn 不要切到下一個主題，回到該主題的第一筆；
 * 反之 up 或 pgup 到第一筆再按，到該主題最後一筆」）。換主題用 ← →；自動輪播仍用 nextStep。
 */
export function pageStep(cur: Step, pages: number, delta: 1 | -1): Step {
  const n = Math.max(1, pages);
  const page = ((cur.page - 1 + delta + n) % n) + 1;
  return { key: cur.key, page };
}

/** 方格的欄數：一列最多 maxCols，格數少時不硬撐成很寬的一列 */
export function gridCols(cells: number, maxCols: number): number {
  return Math.max(1, Math.min(maxCols, cells));
}

/** 這一格跟上一次的資料比，變了沒（給「更新的格子短暫亮起」用） */
export function changedIndexes(prev: string[] | undefined, next: string[]): Set<number> {
  const out = new Set<number>();
  if (!prev || prev.length !== next.length) return out;   // 格數變了＝版面換了，不閃
  next.forEach((s, i) => { if (prev[i] !== s) out.add(i); });
  return out;
}

/** 換主題（↑ ↓、PageUp／PageDown）：直接到上一個或下一個主題的第 1 頁，不經過資料子頁 */
export function topicStep(playlist: string[], cur: Step, delta: 1 | -1): Step {
  if (!playlist.length) return cur;
  const i = playlist.indexOf(cur.key);
  return { key: playlist[(i + delta + playlist.length) % playlist.length], page: 1 };
}

/**
 * 數字鍵直接跳主題：按 3 就是頁尾編號 03 的主題。編號是播放順序（01＝第一個播的），
 * 播放清單可以在戰情室設定拖拉排序（10-22）；超過清單長度不動（回 null）。
 */
export function topicByNumber(playlist: string[], n: number): Step | null {
  const key = n >= 1 ? playlist[n - 1] : undefined;
  return key ? { key, page: 1 } : null;
}
