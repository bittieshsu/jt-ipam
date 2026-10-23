import { describe, expect, it } from "vitest";
import { fmtUsagePct, gaugeArc, usageLevel, usagePct } from "../subnetUsageGauge";

describe("dashboard subnet usage gauges (customer 2026-10-23)", () => {
  it("uses the same thresholds as the subnet list", () => {
    expect(usageLevel(0)).toBe("success");
    expect(usageLevel(74.99)).toBe("success");
    expect(usageLevel(75)).toBe("warning");
    expect(usageLevel(89.99)).toBe("warning");
    expect(usageLevel(90)).toBe("error");
    expect(usageLevel(100)).toBe("error");
  });

  it("does not round a tiny but real usage down to 0%", () => {
    expect(fmtUsagePct(0)).toBe("0%");
    expect(fmtUsagePct(0.00003)).toBe("<0.1%");   // 5 used in a /8
    expect(fmtUsagePct(0.39)).toBe("0.4%");
    expect(fmtUsagePct(4)).toBe("4%");
    expect(fmtUsagePct(42.86)).toBe("43%");
    expect(fmtUsagePct(100)).toBe("100%");
    expect(fmtUsagePct(Number.NaN)).toBe("0%");
  });

  it("computes the ratio from the counts, not the two-decimal used_pct", () => {
    expect(usagePct(5, 1_048_574)).toBeGreaterThan(0);          // 5 used in a /12 (used_pct rounds to 0)
    expect(fmtUsagePct(usagePct(5, 1_048_574))).toBe("<0.1%");
    expect(usagePct(6, 6)).toBe(100);
    expect(usagePct(3, 0)).toBe(0);
  });

  it("draws at least a sliver when something is used", () => {
    expect(gaugeArc(0, 0)).toBe(0);
    expect(gaugeArc(0.00003, 5)).toBe(1.5);
    expect(gaugeArc(50, 3)).toBe(50);
    expect(gaugeArc(120, 3)).toBe(100);
  });
});
