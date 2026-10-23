/**
 * 儀表板「IP 使用率」卡片（逐子網路的小圓環）用的純函式。
 *
 * 顏色門檻跟子網路清單頁的使用率長條一致（≥ 90% 紅、≥ 75% 橘、其餘綠）。
 */

export type UsageLevel = "success" | "warning" | "error";

/** 使用率 → 狀態（跟 Subnets.vue 的 NProgress status 同一組門檻） */
export function usageLevel(pct: number): UsageLevel {
  if (pct >= 90) return "error";
  if (pct >= 75) return "warning";
  return "success";
}

/** 已用／總數的百分比（不四捨五入；後端的 used_pct 只到小數兩位，大網段裡用了幾個會變成 0） */
export function usagePct(used: number, total: number): number {
  return total > 0 ? (used / total) * 100 : 0;
}

/**
 * 百分比顯示：10% 以上取整數；以下保留一位小數；比 0.1% 還小但不是 0 → 「<0.1%」。
 * 大網段（/8）裡只用了幾個位址時，四捨五入成「0%」會讓人以為完全沒用。
 */
export function fmtUsagePct(pct: number): string {
  if (!Number.isFinite(pct) || pct <= 0) return "0%";
  if (pct >= 100) return "100%";
  if (pct >= 10) return `${Math.round(pct)}%`;
  if (pct < 0.1) return "<0.1%";
  return `${(Math.round(pct * 10) / 10).toString()}%`;
}

/** 圓環畫多長（0–100）。有用到但比例極小時至少畫一點點，看得出「不是空的」。 */
export function gaugeArc(pct: number, used: number): number {
  if (used <= 0 || !Number.isFinite(pct) || pct <= 0) return 0;
  return Math.min(100, Math.max(pct, 1.5));
}
