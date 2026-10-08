import type { RouteLocationRaw } from "vue-router";

/**
 * 儀表板上方的數字卡點下去要到對應的清單（使用者 2026-10-08）。
 * 沒權限進的頁不做成可點：稽核頁限管理員，點了只會被導回首頁。
 */
export function kpiTarget(key: string, isAdmin: boolean): RouteLocationRaw | null {
  switch (key) {
    case "sections": return { name: "sections" };
    case "subnets":
    case "capacity":   // IPv4 容量＝各子網路大小加總
    case "ipv6":       return { name: "subnets" };
    case "used":       return { name: "addresses" };
    case "audit":      return isAdmin ? { name: "audit", query: { since: "24h" } } : null;
    default:           return null;
  }
}

/** 稽核頁的 `?since=24h`／`?since=7d`：換成 ISO 時間；看不懂的就當沒給（不猜）。 */
export function auditSinceFromQuery(raw: unknown, now = Date.now()): { label: string; since: string } | null {
  if (typeof raw !== "string") return null;
  const m = /^(\d{1,3})([hd])$/.exec(raw);
  if (!m) return null;
  const n = Number(m[1]);
  const hours = m[2] === "d" ? n * 24 : n;
  if (n <= 0 || hours > 24 * 366) return null;
  return { label: raw, since: new Date(now - hours * 3600_000).toISOString() };
}
