/**
 * IP 清單共用的欄位計算與儲存格：子網路詳細頁的 IP 表與「IP 位址」頁共用（2026-10-12）。
 *
 * 以前 DHCP、裝置、最後出現、失聯這幾欄只寫在子網路詳細頁裡，IP 位址頁的欄位選單就缺了它們；
 * 補上時抽到這裡，兩頁用同一份算法，不會再出現「改了一頁、另一頁照舊」。
 */
import { h, type VNodeChild } from "vue";
import { NTag, NTooltip } from "naive-ui";
import type { DhcpPoolRange } from "@/api/integrations";
import type { IPAddress } from "@/types";
import { fmtDateTime } from "@/utils/datetime";

type T = (key: string, params?: Record<string, unknown>) => string;

export interface DhcpRangeInfo { a: number; b: number; server: string; source: string; start: string; end: string }

export function ipv4ToInt(ip: string): number | null {
  const m = ip.trim().split(".");
  if (m.length !== 4) return null;
  let n = 0;
  for (const p of m) {
    const o = Number(p);
    if (!Number.isInteger(o) || o < 0 || o > 255) return null;
    n = n * 256 + o;
  }
  return n >>> 0;
}

/** DHCP 發放範圍 → 比對用的整數區間（目前只比 IPv4）。`manualLabel`：子網路裡手動定義的集區顯示成什麼。 */
export function toDhcpRanges(rows: DhcpPoolRange[], manualLabel: string): DhcpRangeInfo[] {
  const out: DhcpRangeInfo[] = [];
  for (const r of rows) {
    const a = ipv4ToInt(r.start_ip), b = ipv4ToInt(r.end_ip);
    if (a == null || b == null) continue;
    out.push({
      a: Math.min(a, b), b: Math.max(a, b),
      server: r.source_name || "—",
      // 子網路裡手動定義的 DHCP 集區（issue #40）不是哪一台伺服器同步回來的
      source: r.source === "manual" ? manualLabel : (r.source || "").toUpperCase(),
      start: r.start_ip, end: r.end_ip,
    });
  }
  return out;
}

export function dhcpInfoFor(ranges: DhcpRangeInfo[], ip: string | null | undefined): DhcpRangeInfo | null {
  if (!ip) return null;
  const n = ipv4ToInt(String(ip).split("/")[0]);
  if (n == null) return null;
  return ranges.find((r) => n >= r.a && n <= r.b) ?? null;
}

/** DHCP 欄：在發放範圍內就標 DHCP，滑過去看是哪台、哪一段。 */
export function renderDhcpCell(t: T, info: DhcpRangeInfo | null): VNodeChild {
  if (!info) return "";
  return h(NTooltip, { delay: 0 }, {
    trigger: () => h(NTag, { size: "tiny", type: "warning", bordered: false }, { default: () => "DHCP" }),
    default: () => h("div", { style: "max-width:260px;line-height:1.5" }, [
      h("div", t("addresses.dhcp_pool_hint")),
      h("div", { style: "margin-top:4px" }, [
        h("strong", `${t("addresses.dhcp_server")}：`),
        `${info.server}${info.source ? ` (${info.source})` : ""}`,
      ]),
      h("div", `${t("addresses.dhcp_range")}：${info.start} – ${info.end}`),
    ]),
  });
}

/** 最後出現：掃描代理、LibreNMS、ARP 三者最新的那個（DNS 不算：AdGuard 設定裡有＝每輪都是現在）。 */
export function lastSeenText(r: IPAddress): string {
  const arr = [r.last_seen_scanner, r.last_seen_librenms,
    (r as { last_seen_arp?: string | null }).last_seen_arp].filter(Boolean) as string[];
  if (!arr.length) return "—";
  return fmtDateTime(arr.sort().reverse()[0]);   // 轉本地時區
}

/** 失聯天數：只看掃描代理與 LibreNMS（有 DNS 改寫的 IP 不可以因此永遠不失聯）。從沒看過回 null。 */
export function staleDays(r: IPAddress): number | null {
  const arr = [r.last_seen_scanner, r.last_seen_librenms].filter(Boolean) as string[];
  if (!arr.length) return null;
  const max = Math.max(...arr.map((s) => new Date(s).getTime()));
  return Math.floor((Date.now() - max) / 86400000);
}

/** 這個位址有沒有在被探測（排除 ping 或子網路沒開掃描就沒有）。 */
export function isProbed(r: IPAddress): boolean {
  return !r.exclude_from_ping && r.subnet_scan_enabled !== false;
}

export function staleDaysLabel(t: T, r: IPAddress): string {
  if (!isProbed(r)) return t("stale.not_probed");
  const d = staleDays(r);
  if (d === null) return t("stale.never");
  return t("stale.n_days", { n: d });
}

/** 裝置欄：連到裝置詳細頁；沒有綁裝置顯示「—」。 */
export function renderDeviceLink(r: IPAddress, open: (id: string) => void): VNodeChild {
  if (!r.device_id) return "—";
  return h("a", {
    href: "#",
    style: "color: var(--primary-color, #18a058); text-decoration: none;",
    onClick: (e: MouseEvent) => { e.preventDefault(); e.stopPropagation(); open(r.device_id as string); },
  }, r.device_name || (String(r.device_id).slice(0, 8) + "…"));
}
