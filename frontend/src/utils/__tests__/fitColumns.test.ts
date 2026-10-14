import { describe, expect, it } from "vitest";
import { columnsWidth, fitWidth, textWidth } from "../fitColumns";

describe("fitColumns", () => {
  it("短欄依內容：標頭比內容寬時以標頭為準，含排序圖示與內距", () => {
    const w = fitWidth("名稱", ["ucs-dns"]);
    expect(w).toBeLessThan(140);
    expect(w).toBeGreaterThanOrEqual(64);
    expect(w).toBeGreaterThanOrEqual(Math.round(textWidth("名稱") + 30 + 26));
  });
  it("長內容夾在上限、空值算成「—」", () => {
    expect(fitWidth("說明", ["x".repeat(500)], { max: 300 })).toBe(300);
    expect(fitWidth("差異", ["", ""], { sortable: false })).toBe(64);
  });
  it("標籤這類每格有額外內距時一起算", () => {
    expect(fitWidth("狀態", ["不一致"], { px: 12, extra: 40 })).toBeGreaterThan(fitWidth("狀態", ["不一致"], { px: 12 }));
  });
  it("總寬＝各欄 width（沒有就用 minWidth）加總", () => {
    expect(columnsWidth([{ width: 100 }, { minWidth: 200 }, {}])).toBe(420);
  });
});
