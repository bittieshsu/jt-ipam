/** 終端機字級：SSH 與 PVE xterm 共用同一份（10-22 使用者：「pve xterm 要跟 ssh 一樣可以調整字大字小」）。 */
import { beforeEach, describe, expect, it } from "vitest";
import { FONT_KEY, FONT_MAX, FONT_MIN, useTerminalFontSize } from "../useTerminalFontSize";

describe("useTerminalFontSize", () => {
  beforeEach(() => localStorage.clear());

  it("預設 13，記在瀏覽器，另一個終端機讀到同一個值", () => {
    const a = useTerminalFontSize();
    expect(a.fontSize.value).toBe(13);
    a.step(2);
    expect(a.fontSize.value).toBe(15);
    expect(localStorage.getItem(FONT_KEY)).toBe("15");
    expect(useTerminalFontSize().fontSize.value).toBe(15);
  });

  it("不超出範圍，到邊界按鈕要反灰", () => {
    const a = useTerminalFontSize();
    a.step(100);
    expect(a.fontSize.value).toBe(FONT_MAX);
    expect(a.atMax.value).toBe(true);
    a.step(-100);
    expect(a.fontSize.value).toBe(FONT_MIN);
    expect(a.atMin.value).toBe(true);
  });

  it("變動時通知（讓終端機換字級並重新計算行列）", () => {
    const seen: number[] = [];
    const a = useTerminalFontSize((v) => seen.push(v));
    a.step(1);
    a.step(-1);
    expect(seen).toEqual([14, 13]);
  });
});
