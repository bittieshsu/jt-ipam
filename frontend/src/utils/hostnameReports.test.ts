import { describe, expect, it } from "vitest";
import type { HostnameReport } from "@/api/hostname";
import { daysSince, differsFrom, reporterLabel, shortName } from "./hostnameReports";

const t = (k: string) => k;
const rep = (over: Partial<HostnameReport>): HostnameReport => ({
  kind: "scan_agent", label: "agent-east", detail: null, hostname: "FILESRV01",
  first_seen_at: null, last_seen_at: null, ...over,
});

describe("hostnameReports", () => {
  it("names the reporting agent or integration", () => {
    expect(reporterLabel(t, rep({}))).toBe("agent-east");
    expect(reporterLabel(t, rep({ kind: "integration", label: "fw-1", detail: "lease" }))).toBe("fw-1 (lease)");
  });

  it("does not guess a name for old or deleted reporters", () => {
    expect(reporterLabel(t, rep({ kind: "legacy", label: null }))).toBe("hostnameSrc.reporter_legacy");
    expect(reporterLabel(t, rep({ kind: "unknown", label: null }))).toBe("hostnameSrc.reporter_unknown");
  });

  it("treats case and FQDN vs short name as the same name", () => {
    expect(shortName("FileSrv01.example.com")).toBe("filesrv01");
    expect(differsFrom("FILESRV01", "filesrv01.example.com")).toBe(false);
    // NetBIOS 名稱最多 15 字：被截斷或另外設定時，和 DNS 名稱不同要標出來
    expect(differsFrom("FILESERVER-2019", "fileserver-2019-prod.example.com")).toBe(true);
    expect(differsFrom("anything", null)).toBe(false);
  });

  it("counts whole days since the last report", () => {
    const now = Date.parse("2026-10-11T12:00:00Z");
    expect(daysSince("2026-10-11T02:00:00Z", now)).toBe(0);
    expect(daysSince("2026-10-08T11:00:00Z", now)).toBe(3);
    expect(daysSince(null, now)).toBe(0);
    expect(daysSince("garbage", now)).toBe(0);
  });
});
