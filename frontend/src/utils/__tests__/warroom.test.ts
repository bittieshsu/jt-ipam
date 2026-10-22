/** 戰情室純函式：名稱斷行不改字串、多久以前、兩層分頁的輪播、更新的格子。 */
import { describe, expect, it } from "vitest";
import { agoParts, breakPoints, changedIndexes, gridCols, nextStep, pad2, pageStep, prevStep, topicByNumber, topicStep } from "../warroom";

const t = (k: string) => ({ "warroom.unit_s_ago": "秒前", "warroom.unit_min_ago": "分鐘前",
  "warroom.unit_h_ago": "小時前", "warroom.unit_d_ago": "天前" } as Record<string, string>)[k] ?? k;

describe("breakPoints", () => {
  it("FQDN 在句點後斷，串回來一字不差、句點不會出現在行首", () => {
    const s = "customer-portal.production.ap-southeast.example.com";
    const parts = breakPoints(s);
    expect(parts.join("")).toBe(s);
    expect(parts.every((p, i) => i === 0 || !p.startsWith("."))).toBe(true);
    expect(parts).toContain("production.");
  });
  it("沒有分隔字元的長字串維持一段（交給 CSS 最後備援斷行）", () => {
    expect(breakPoints("scannerproductionapsoutheast01")).toEqual(["scannerproductionapsoutheast01"]);
  });
  it("中文與空白原樣", () => {
    expect(breakPoints("台北南港研發中心／核心網段掃描代理").join("")).toBe("台北南港研發中心／核心網段掃描代理");
    expect(breakPoints("")).toEqual([]);
  });
});

describe("agoParts", () => {
  it("數字與單位分開", () => {
    expect(agoParts(30, t)).toEqual({ value: "30", unit: "秒前" });
    expect(agoParts(18 * 60 + 5, t)).toEqual({ value: "18", unit: "分鐘前" });
    expect(agoParts(7200, t)).toEqual({ value: "2", unit: "小時前" });
    expect(agoParts(3 * 86400, t)).toEqual({ value: "3", unit: "天前" });
    expect(agoParts(null, t)).toEqual({ value: "—", unit: "" });
  });
});

describe("輪播", () => {
  const pl = ["overview", "capacity", "collection"];
  it("同主題最多播 maxSubpages 頁，再換下一個主題的第 1 頁", () => {
    expect(nextStep(pl, { key: "capacity", page: 1 }, 7, 2)).toEqual({ key: "capacity", page: 2 });
    expect(nextStep(pl, { key: "capacity", page: 2 }, 7, 2)).toEqual({ key: "collection", page: 1 });
    expect(nextStep(pl, { key: "collection", page: 1 }, 1, 2)).toEqual({ key: "overview", page: 1 });
  });
  it("上一步", () => {
    expect(prevStep(pl, { key: "capacity", page: 2 })).toEqual({ key: "capacity", page: 1 });
    expect(prevStep(pl, { key: "overview", page: 1 })).toEqual({ key: "collection", page: 1 });
  });
  it("頁碼兩位數", () => {
    expect(pad2(3)).toBe("03");
    expect(pad2(28)).toBe("28");
  });
});

describe("方格", () => {
  it("欄數不超過上限、格數少時不撐寬", () => {
    expect(gridCols(254, 32)).toBe(32);
    expect(gridCols(5, 12)).toBe(5);
  });
  it("只有同樣格數時才比出變動", () => {
    expect([...changedIndexes(["a", "b", "c"], ["a", "x", "c"])]).toEqual([1]);
    expect(changedIndexes(["a"], ["a", "b"]).size).toBe(0);
    expect(changedIndexes(undefined, ["a"]).size).toBe(0);
  });
});

describe("按鍵換主題", () => {
  const all = ["overview", "capacity", "topology", "services", "anomalies", "assets", "collection"];
  const pl = ["overview", "capacity", "collection"];
  it("↑ ↓ 直接換主題、回到第 1 頁", () => {
    expect(topicStep(pl, { key: "capacity", page: 3 }, 1)).toEqual({ key: "collection", page: 1 });
    expect(topicStep(pl, { key: "overview", page: 1 }, -1)).toEqual({ key: "collection", page: 1 });
  });
  it("數字鍵照頁尾編號跳：編號是播放順序（清單可以拖拉排序，10-22）", () => {
    expect(topicByNumber(pl, 3)).toEqual({ key: "collection", page: 1 });
    expect(topicByNumber(pl, 2)).toEqual({ key: "capacity", page: 1 });
    expect(topicByNumber(["collection", "overview"], 1)).toEqual({ key: "collection", page: 1 });
    expect(topicByNumber(pl, 4)).toBeNull();             // 播放清單只有三個
    expect(topicByNumber(pl, 0)).toBeNull();
    expect(all).toHaveLength(7);
  });
});

describe("手動換資料頁在同一個主題內循環（10-22 使用者）", () => {
  // 「已在最後一頁按 down 或 pgdn 不要切到下一個主題，回到該主題的第一筆；反之 up 或 pgup 到第一筆再按，到該主題最後一筆」
  it("最後一頁往下回到第 1 頁；第 1 頁往上到最後一頁；主題不變", () => {
    expect(pageStep({ key: "anomalies", page: 3 }, 5, 1)).toEqual({ key: "anomalies", page: 4 });
    expect(pageStep({ key: "anomalies", page: 5 }, 5, 1)).toEqual({ key: "anomalies", page: 1 });
    expect(pageStep({ key: "anomalies", page: 1 }, 5, -1)).toEqual({ key: "anomalies", page: 5 });
    expect(pageStep({ key: "anomalies", page: 2 }, 5, -1)).toEqual({ key: "anomalies", page: 1 });
  });
  it("只有一頁：停在原地", () => {
    expect(pageStep({ key: "overview", page: 1 }, 1, 1)).toEqual({ key: "overview", page: 1 });
    expect(pageStep({ key: "overview", page: 1 }, 1, -1)).toEqual({ key: "overview", page: 1 });
  });
  it("自動輪播照舊：播完最後一頁換下一個主題", () => {
    expect(nextStep(["a", "b"], { key: "a", page: 2 }, 2, 5)).toEqual({ key: "b", page: 1 });
  });
});
