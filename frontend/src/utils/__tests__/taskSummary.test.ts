import { describe, expect, it } from "vitest";
import { formatSummary, summaryChips, taskResultText } from "@/utils/taskSummary";

const t = (key: string, p?: Record<string, unknown>) => (p ? `${key}:${JSON.stringify(p)}` : key);

describe("taskSummary", () => {
  it("phpIPAM 搬移的摘要不會丟錯（以前迴圈變數叫 t，蓋掉了翻譯函式）", () => {
    const out = formatSummary("phpipam.migration",
      { tables: { sections: { inserted: 2, updated: 1, errored: 0 }, subnets: { skipped: 5 } } }, t);
    expect(out).toContain("sections");
    expect(out).toContain("common.added_n");
    expect(out).not.toContain("subnets");   // 只 skip 的不列
  });

  it("失敗講錯誤，沒有錯誤訊息也要講清楚是失敗；執行中沒有結論", () => {
    expect(taskResultText({ kind: "dns.sync", status: "failed", summary: null, error: "DNSAdapterError: 401" }, t))
      .toBe("DNSAdapterError: 401");
    expect(taskResultText({ kind: "dns.sync", status: "failed", summary: null, error: null }, t))
      .toBe("tasks.summary.failed_no_detail");
    expect(taskResultText({ kind: "dns.sync", status: "running", summary: null, error: null }, t)).toBe("");
  });

  it("成功講摘要", () => {
    const out = taskResultText({ kind: "dns.sync", status: "succeeded", error: null,
                                 summary: { pulled_zones: 2, pulled_records: 10 } }, t);
    expect(out).toContain("zones 2");
    expect(out).toContain("records 10");
  });
});

describe("summaryChips", () => {
  const t = (k: string, p?: Record<string, unknown>) => (p ? `${k}${JSON.stringify(p)}` : k);
  it("gives the non-count kinds their own tags instead of a sentence", () => {
    expect(summaryChips("rustdesk.sync", { peers: 18, online: 5, matched: 6 }, t)?.map((c) => c.label))
      .toEqual(["tasks.chip.peers 18", "tasks.chip.online 5", "tasks.chip.matched 6"]);
    expect(summaryChips("geoip.refresh", { results: { "GeoLite2-City": { ok: true }, "GeoLite2-ASN": { ok: false } } }, t))
      .toEqual([{ label: "GeoLite2-City ✓", type: "success" }, { label: "GeoLite2-ASN ✗", type: "error" }]);
    expect(summaryChips("recog.refresh", { status: "updated", previous: "3.3.0", release: "3.3.2", fingerprints: 4674 }, t)?.[0].label)
      .toBe("3.3.0 → 3.3.2");
    expect(summaryChips("ip.identify", { device_type: "server", os: "Ubuntu Linux", ports: 3 }, t)?.map((c) => c.label))
      .toEqual(["identify.type.server", "Ubuntu Linux", "tasks.chip.ports 3"]);
  });
  it("returns null for kinds that use the generic counts", () => {
    expect(summaryChips("opnsense.sync", { details: [] }, t)).toBeNull();
  });
});

describe("summaryChips for kinds that used to show four zeros", () => {
  const t = (k: string) => k;
  it("OpenStack, IP change assessment and external DNS get their own tags", () => {
    expect(summaryChips("openstack.sync", { vms: 41, ports: 53, subnets: 7, ipam_linked: 1, pending_subnets: 5 }, t)
      ?.map((c) => c.label)).toEqual(["tasks.chip.vms 41", "tasks.chip.nics 53", "tasks.chip.subnets_os 7",
                                      "tasks.chip.ipam_linked 1", "tasks.chip.pending_subnets 5"]);
    expect(summaryChips("impact.run", { findings: 5, change_required: 4, blockers: 0, gaps: 19 }, t)?.[1])
      .toEqual({ label: "tasks.chip.change_required 4", type: "warning" });
    expect(summaryChips("external_dns.run", { names: 2, changed: 0, failed: 2 }, t)?.[2])
      .toEqual({ label: "tasks.chip.failed 2", type: "error" });
  });
});
