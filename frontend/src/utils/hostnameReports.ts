import type { HostnameReport } from "@/api/hostname";

type T = (key: string, params?: Record<string, unknown>) => string;

/** 回報者的顯示名稱：代理／整合的名字；升級前的舊資料、已刪除的那台不拿 id 去猜 */
export function reporterLabel(t: T, r: HostnameReport): string {
  const base = r.label
    ?? (r.kind === "legacy" ? t("hostnameSrc.reporter_legacy") : t("hostnameSrc.reporter_unknown"));
  return r.detail ? `${base} (${r.detail})` : base;
}

/** 比對用的短名：NetBIOS 給 FILESRV01、DNS 給 filesrv01.example.com，兩者算同一個名字 */
export function shortName(n: string | null | undefined): string {
  return (n ?? "").trim().toLowerCase().split(".")[0];
}

/** 名字是否和目前採用的不同（大小寫、FQDN 與短名的差別不算） */
export function differsFrom(name: string, effective: string | null | undefined): boolean {
  return !!effective && shortName(name) !== shortName(effective);
}

/** 距今幾天（整天，無效時間回 0） */
export function daysSince(iso: string | null | undefined, now: number = Date.now()): number {
  const ts = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(ts) ? Math.max(0, Math.floor((now - ts) / 86_400_000)) : 0;
}
