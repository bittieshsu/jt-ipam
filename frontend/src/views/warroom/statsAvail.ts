/**
 * 08 IP 統計與 10 IP 可用性追蹤的畫面計算（純函式；vitest 在 __tests__/statsAvail.test.ts）。
 * 數字與格數都由後端算好（services/warroom/stats.py、availability.py），這裡只負責排版需要的換算。
 */

/** 狀態方格：有值的格子在前，其餘補成底軌（同一個尺度下各列的長度可以直接比較）。 */
export function statusRowCells(state: string, filled: number, rowCells: number): string[] {
  const n = Math.max(0, Math.min(filled, rowCells));
  return [...Array(n).fill(state), ...Array(Math.max(rowCells - n, 0)).fill("track")];
}

/** 百分比顯示：一位小數；100 與 0 不帶小數；沒有分母是「—」（不是 0%）。 */
export function pctText(p: number | null | undefined): string {
  if (p == null || Number.isNaN(p)) return "—";
  if (p >= 100) return "100";
  if (p <= 0) return "0";
  return p.toFixed(1);
}

/** 持續時間 → i18n 鍵與數字：一小時內用分鐘，兩天內用小時，再久用日。 */
export function durationParts(seconds: number): { key: "av_dur_m" | "av_dur_h" | "av_dur_d"; n: number } {
  const s = Math.max(0, seconds);
  if (s < 3600) return { key: "av_dur_m", n: Math.max(1, Math.round(s / 60)) };
  if (s < 48 * 3600) return { key: "av_dur_h", n: Math.round(s / 360) / 10 };
  return { key: "av_dur_d", n: Math.round(s / 8640) / 10 };
}

/**
 * 歷史格的寬度：可用寬度扣掉格距與群組間距後平均分。最少 4px（90 日也排得下），最多 36px。
 * groups：表頭有幾組（組與組之間多留 groupGap）。
 */
export function cellWidth(cells: number, groups: number, width: number, gap: number, groupGap: number): number {
  if (cells <= 0) return 0;
  const free = width - gap * (cells - 1) - groupGap * Math.max(groups - 1, 0);
  return Math.max(4, Math.min(36, Math.floor(free / cells)));
}

/** 事件時間：今天的只寫時:分，其他日子加上月/日（電視上不靠滑鼠看完整時間）。 */
export function eventTime(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  const hm = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  if (sameDay) return hm;
  const md = `${String(d.getMonth() + 1).padStart(2, "0")}/${String(d.getDate()).padStart(2, "0")}`;
  return `${md} ${hm}`;
}
