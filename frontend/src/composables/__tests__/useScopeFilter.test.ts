import { describe, it, expect } from "vitest";
import { nextTick, ref } from "vue";
import { useScopeFilter } from "../useScopeFilter";

const rows = ref([
  { ip: "a", section_id: "s1", section_name: "HQ", subnet_id: "n1", subnet_cidr: "10.0.1.0/24", customer_id: "c1", customer_name: "ACME" },
  { ip: "b", section_id: "s1", section_name: "HQ", subnet_id: "n2", subnet_cidr: "10.0.2.0/24", customer_id: null, customer_name: null },
  { ip: "c", section_id: "s2", section_name: "Branch", subnet_id: "n3", subnet_cidr: "10.1.0.0/24", customer_id: "c1", customer_name: "ACME" },
]);

describe("useScopeFilter", () => {
  it("選項來自資料、去重並排序", () => {
    const f = useScopeFilter(rows);
    expect(f.sectionOpts.value.map((o) => o.label)).toEqual(["Branch", "HQ"]);
    expect(f.customerOpts.value).toEqual([{ value: "c1", label: "ACME" }]);
  });

  it("三個條件同時成立才留下", () => {
    const f = useScopeFilter(rows);
    f.customer.value = "c1";
    expect(f.filtered.value.map((r) => r.ip)).toEqual(["a", "c"]);
    f.section.value = "s1";
    expect(f.filtered.value.map((r) => r.ip)).toEqual(["a"]);
    expect(f.active.value).toBe(true);
  });

  it("選了區段，子網路選單只列該區段的；原本選的不在裡面就清掉", async () => {
    const f = useScopeFilter(rows);
    f.subnet.value = "n3";
    f.section.value = "s1";
    await nextTick();
    expect(f.subnetOpts.value.map((o) => o.value)).toEqual(["n1", "n2"]);
    expect(f.subnet.value).toBeNull();
  });

  // 「有沒有在線上」（2026-09-27 使用者要求）：跟 IP 清單燈號同一套規則（classifyAddressLiveness）
  it("依上線狀態篩選：規則與 IP 清單的燈號相同，選項只列資料裡有的", () => {
    const now = Date.now();
    const iso = (minAgo: number) => new Date(now - minAgo * 60_000).toISOString();
    const live = ref([
      { ip: "on", last_seen_scanner: iso(1) },                       // 剛掃到 → 上線
      { ip: "off", last_seen_scanner: iso(60 * 24 * 3) },            // 三天前 → 離線
      { ip: "never" },                                               // 沒有任何證據、有在掃 → 離線
      { ip: "noscan", subnet_scan_enabled: false },                  // 子網路沒掃 → 未知，不是離線
      { ip: "fw", arp_seen: { "arp:opnsense": iso(2) } },            // 只有沒被勾選的來源 → 不算
    ]);
    const f = useScopeFilter(live);
    expect(f.statusOpts.value.map((o) => o.value)).toEqual(["online", "offline", "unknown"]);
    f.status.value = "online";
    expect(f.filtered.value.map((r) => r.ip)).toEqual(["on"]);
    expect(f.active.value).toBe(true);
    f.status.value = "offline";
    expect(f.filtered.value.map((r) => r.ip)).toEqual(["off", "never", "fw"]);
    f.status.value = "unknown";
    expect(f.filtered.value.map((r) => r.ip)).toEqual(["noscan"]);
  });
});
