import { describe, expect, it } from "vitest";
import { cellWidth, durationParts, eventTime, pctText, statusRowCells } from "../statsAvail";

describe("statusRowCells", () => {
  it("fills the counted cells and pads the rest as track", () => {
    const cells = statusRowCells("ok", 3, 8);
    expect(cells).toEqual(["ok", "ok", "ok", "track", "track", "track", "track", "track"]);
  });
  it("never exceeds the row and never goes negative", () => {
    expect(statusRowCells("ok", 12, 8)).toHaveLength(8);
    expect(statusRowCells("ok", -2, 4)).toEqual(["track", "track", "track", "track"]);
  });
  it("keeps one cell for a single item (the backend already rounds up)", () => {
    expect(statusRowCells("anomaly", 1, 160).filter((c) => c === "anomaly")).toHaveLength(1);
  });
});

describe("pctText", () => {
  it("shows a dash when there is no denominator, not 0", () => {
    expect(pctText(null)).toBe("—");
    expect(pctText(undefined)).toBe("—");
  });
  it("uses one decimal except at the ends", () => {
    expect(pctText(100)).toBe("100");
    expect(pctText(0)).toBe("0");
    expect(pctText(99.94)).toBe("99.9");
    expect(pctText(12.345)).toBe("12.3");
  });
});

describe("durationParts", () => {
  it("picks minutes, hours or days", () => {
    expect(durationParts(720)).toEqual({ key: "av_dur_m", n: 12 });
    expect(durationParts(10)).toEqual({ key: "av_dur_m", n: 1 });
    expect(durationParts(3 * 3600)).toEqual({ key: "av_dur_h", n: 3 });
    expect(durationParts(5400)).toEqual({ key: "av_dur_h", n: 1.5 });
    expect(durationParts(3 * 86400)).toEqual({ key: "av_dur_d", n: 3 });
  });
});

describe("cellWidth", () => {
  it("divides the free width evenly", () => {
    // 28 格、7 組、寬 700、格距 4、組距 12：700 - 108 - 72 = 520 → 18
    expect(cellWidth(28, 7, 700, 4, 12)).toBe(18);
  });
  it("stays readable for 90 days and does not balloon for few cells", () => {
    expect(cellWidth(90, 6, 700, 2, 8)).toBeGreaterThanOrEqual(4);
    expect(cellWidth(3, 1, 700, 4, 12)).toBe(36);
    expect(cellWidth(0, 0, 700, 4, 12)).toBe(0);
  });
});

describe("eventTime", () => {
  it("omits the date for today and adds it otherwise", () => {
    const now = new Date(2026, 9, 23, 18, 0);
    expect(eventTime(new Date(2026, 9, 23, 9, 5).toISOString(), now)).toMatch(/^09:05$/);
    expect(eventTime(new Date(2026, 9, 21, 13, 28).toISOString(), now)).toMatch(/^10\/21 13:28$/);
  });
});
