/**
 * 未納管位址（IPAM 沒有記錄、但看得到在用）的共用顯示：誰看到的。
 * 指示計、IP 清單、未納管位址頁三處共用，說法才會一致。
 */
import { evidenceVendorLabel } from "@/utils/investigateSections";

type T = (key: string, params?: Record<string, unknown>) => string;

export function unmanagedSourceLabel(t: T, src: string): string {
  if (src === "scanner") return t("anomaly.seen_scanner");
  if (src.startsWith("arp:")) return t("anomaly.seen_arp_vendor", { vendor: evidenceVendorLabel(src.slice(4), t) });
  return src;
}

export function unmanagedSources(t: T, sources: string[]): string {
  return sources.map((s) => unmanagedSourceLabel(t, s)).join("、");
}
