/**
 * IP 清單共用欄位（子網路詳細頁與 IP 位址頁共用）：算法只有一份，兩頁的「最後出現」「失聯」「DHCP」一致。
 */
import { describe, expect, it } from "vitest";
import { dhcpInfoFor, isProbed, lastSeenText, staleDays, staleDaysLabel, toDhcpRanges } from "../ipColumns";

const t = (k: string, p?: Record<string, unknown>) => (p ? `${k}(${JSON.stringify(p)})` : k);
const day = 86400000;

describe("ipColumns", () => {
  it("last seen takes the newest of scanner, LibreNMS and ARP", () => {
    const iso = (ms: number) => new Date(ms).toISOString();
    const now = Date.now();
    const r: any = { last_seen_scanner: iso(now - 3 * day), last_seen_librenms: null, last_seen_arp: iso(now - day) };
    expect(lastSeenText(r)).not.toBe("—");
    expect(lastSeenText({} as any)).toBe("—");
    // 失聯天數不算 ARP（只看掃描代理與 LibreNMS）
    expect(staleDays(r)).toBe(3);
  });

  it("stale label says not probed, never seen, or n days", () => {
    expect(staleDaysLabel(t, { exclude_from_ping: true } as any)).toBe("stale.not_probed");
    expect(staleDaysLabel(t, {} as any)).toBe("stale.never");
    expect(isProbed({ subnet_scan_enabled: false } as any)).toBe(false);
    const r: any = { last_seen_librenms: new Date(Date.now() - 2 * day).toISOString() };
    expect(staleDaysLabel(t, r)).toBe('stale.n_days({"n":2})');
  });

  it("finds the DHCP range of an IPv4 address and labels manual pools", () => {
    const ranges = toDhcpRanges([
      { id: "1", source_type: "windows_dhcp", source_id: "x", source_name: "dc01", subnet_cidr: null,
        start_ip: "192.0.2.100", end_ip: "192.0.2.150", family: 4, source: "windows" },
      { id: "2", source_type: "manual", source_id: "y", source_name: "LAN", subnet_cidr: null,
        start_ip: "192.0.2.200", end_ip: "192.0.2.210", family: 4, source: "manual" },
      { id: "3", source_type: "kea_dhcp", source_id: "z", subnet_cidr: null,
        start_ip: "2001:db8::10", end_ip: "2001:db8::20", family: 6, source: "kea" },
    ], "手動");
    expect(ranges).toHaveLength(2);
    expect(dhcpInfoFor(ranges, "192.0.2.120/32")?.server).toBe("dc01");
    expect(dhcpInfoFor(ranges, "192.0.2.205")?.source).toBe("手動");
    expect(dhcpInfoFor(ranges, "192.0.2.180")).toBeNull();
    expect(dhcpInfoFor(ranges, null)).toBeNull();
  });
});
