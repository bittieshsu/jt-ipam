import { describe, expect, it } from "vitest";
import { kpiTarget, auditSinceFromQuery } from "../dashboardKpiTarget";

describe("dashboard KPI cards link to their pages (user 2026-10-08)", () => {
  it("each count opens the list it counts", () => {
    expect(kpiTarget("sections", true)).toEqual({ name: "sections" });
    expect(kpiTarget("subnets", false)).toEqual({ name: "subnets" });
    expect(kpiTarget("used", false)).toEqual({ name: "addresses" });
    expect(kpiTarget("capacity", false)).toEqual({ name: "subnets" });
    expect(kpiTarget("ipv6", false)).toEqual({ name: "subnets" });
  });
  it("the 24h audit count opens the audit log narrowed to the last 24 hours, admins only", () => {
    expect(kpiTarget("audit", true)).toEqual({ name: "audit", query: { since: "24h" } });
    // 稽核頁限管理員：沒權限的卡片不做成可點（點了只會被導回首頁）
    expect(kpiTarget("audit", false)).toBeNull();
  });
  it("unknown cards are not links", () => {
    expect(kpiTarget("nope", true)).toBeNull();
  });
});

describe("audit page ?since=", () => {
  const now = Date.parse("2026-10-08T12:00:00Z");
  it("understands hours and days", () => {
    expect(auditSinceFromQuery("24h", now)).toEqual({ label: "24h", since: "2026-10-07T12:00:00.000Z" });
    expect(auditSinceFromQuery("7d", now)).toEqual({ label: "7d", since: "2026-10-01T12:00:00.000Z" });
  });
  it("ignores anything else instead of guessing", () => {
    expect(auditSinceFromQuery(undefined, now)).toBeNull();
    expect(auditSinceFromQuery("abc", now)).toBeNull();
    expect(auditSinceFromQuery("0h", now)).toBeNull();
    expect(auditSinceFromQuery("99999d", now)).toBeNull();
    expect(auditSinceFromQuery(["24h"], now)).toBeNull();
  });
});
