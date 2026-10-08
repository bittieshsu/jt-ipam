import { describe, expect, it } from "vitest";
import { fillHeight } from "../usePageFill";

describe("fillHeight：滿版圖的高度", () => {
  it("一般情況：可視高度扣掉圖上方的工具列與下方的留白", () => {
    expect(fillHeight({ visible: 664, above: 70, below: 29, min: 480, laidOut: true })).toBe(565);
  });

  it("還沒排版（分頁藏著、正在切換）時不量，等下一次（e2e 2026-10-08：量到全 0 算出 1720px）", () => {
    expect(fillHeight({ visible: 664, above: 0, below: -1056, min: 480, laidOut: false })).toBeNull();
  });

  it("版面不一致（外框底部在圖的底部之上）時不量", () => {
    expect(fillHeight({ visible: 664, above: 70, below: -1056, min: 480, laidOut: true })).toBeNull();
  });

  it("再怎麼算都不會比可視高度高：圖比畫面高，評估目標就可能在畫面外", () => {
    expect(fillHeight({ visible: 664, above: -400, below: 10, min: 480, laidOut: true })).toBe(664);
  });

  it("小視窗仍保有最低高度", () => {
    expect(fillHeight({ visible: 300, above: 70, below: 29, min: 480, laidOut: true })).toBe(480);
  });
});
