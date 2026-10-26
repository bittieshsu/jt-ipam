import { describe, expect, it } from "vitest";
import { afterItem, charDelay, graphemes, pauseAfter, rhythm, seedOf, tokens } from "../intelType";

describe("AI 情報逐字播放", () => {
  it("依字素切分：組合字與 emoji 不會被切壞", () => {
    expect(graphemes("éa")).toEqual(["é", "a"]);
    expect(graphemes("👍🏽OK")).toEqual(["👍🏽", "O", "K"]);
    expect(graphemes("衝突")).toEqual(["衝", "突"]);
  });

  it("IP、CIDR、MAC、IPv6 是整塊識別碼（排版不拆行）", () => {
    const t = tokens("198.51.100.80 與 198.51.100.0/24、00:00:5e:00:53:21 還有 2001:db8::1 都是");
    expect(t.filter((x) => x.atomic).map((x) => x.text))
      .toEqual(["198.51.100.80", "198.51.100.0/24", "00:00:5e:00:53:21", "2001:db8::1"]);
    expect(t.map((x) => x.text).join("")).toBe("198.51.100.80 與 198.51.100.0/24、00:00:5e:00:53:21 還有 2001:db8::1 都是");
    expect(tokens("198.51.100.x 已遮罩").find((x) => x.atomic)?.text).toBe("198.51.100.x");
  });

  it("中文每秒約 12–18 字、英數 28–45 字，快慢倍率 0.7 / 1 / 1.4", () => {
    const seed = seedOf("a1");
    for (let i = 0; i < 200; i++) {
      const cps = 1000 / charDelay("中", i, { speed: "normal", rhythm: true, seed });
      expect(cps).toBeGreaterThanOrEqual(11.9);
      expect(cps).toBeLessThanOrEqual(18.8);
      const lat = 1000 / charDelay("a", i, { speed: "normal", rhythm: true, seed });
      expect(lat).toBeGreaterThanOrEqual(28);
      expect(lat).toBeLessThanOrEqual(45.1);
    }
    const n = charDelay("中", 3, { speed: "normal", rhythm: false, seed });
    expect(charDelay("中", 3, { speed: "fast", rhythm: false, seed })).toBeCloseTo(n / 1.4);
    expect(charDelay("中", 3, { speed: "slow", rhythm: false, seed })).toBeCloseTo(n / 0.7);
  });

  it("全形字（中日韓、全形標點）用中文速度，英數用英文速度", () => {
    const base = (g: string) => charDelay(g, 0, { speed: "normal", rhythm: false, seed: 0 });
    for (const g of ["中", "ひ", "カ", "한", "，", "（"]) expect(base(g), g).toBeCloseTo(1000 / 15);
    for (const g of ["a", "1", ".", "-"]) expect(base(g), g).toBeCloseTo(1000 / 36);
  });

  it("節奏是平滑的：相鄰兩個字的倍率差很小", () => {
    const s = seedOf("x");
    for (let i = 0; i < 100; i++) expect(Math.abs(rhythm(i + 1, s) - rhythm(i, s))).toBeLessThan(0.1);
  });

  it("標點停頓與讀完的停留", () => {
    const s = seedOf("p");
    const c = pauseAfter("，", 5, s);
    expect(c).toBeGreaterThanOrEqual(120);
    expect(c).toBeLessThanOrEqual(220);
    const p = pauseAfter("。", 5, s);
    expect(p).toBeGreaterThanOrEqual(350);
    expect(p).toBeLessThanOrEqual(650);
    expect(pauseAfter("字", 5, s)).toBe(0);
    expect(afterItem("短", 0)).toBeGreaterThanOrEqual(4000 + 1200);
    expect(afterItem("很長".repeat(500), 0.99)).toBeLessThanOrEqual(12000 + 2200);
  });
});
