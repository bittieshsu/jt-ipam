// @vitest-environment jsdom
/**
 * 大指標翻牌（使用者 10-26）：變大時新數字由下往上翻、變小時由上往下翻；只翻有變的那幾位；降低動畫時直接換。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import { ref } from "vue";
import WrMetric from "../WrMetric.vue";

const i18n = createI18n({ legacy: false, locale: "en", messages: { en: { warroom: { no_access_short: "No access", ai_no_result: "No result" } } } });

function make(value: number | string | null, reduce = false) {
  return mount(WrMetric, {
    props: { value, label: "Open anomalies" },
    global: { plugins: [i18n], provide: { wrReduceMotion: ref(reduce) } },
  });
}

describe("WrMetric 翻牌", () => {
  it("變大：由下往上翻，只翻變了的位數", async () => {
    const w = make(144);
    expect(w.find(".wr-d").exists()).toBe(false);
    await w.setProps({ value: 150 });
    const v = w.find("[data-testid=wr-metric-v]");
    expect(v.classes()).toContain("roll-up");
    const flips = w.findAll(".wr-d");
    expect(flips.map((f) => f.find(".wr-d__n").text())).toEqual(["5", "0"]);     // 百位的 1 沒變不翻
    expect(flips.map((f) => f.find(".wr-d__o").text())).toEqual(["4", "4"]);
  });

  it("變小：由上往下翻；位數變少也正確", async () => {
    const w = make(1000);
    await w.setProps({ value: 999 });
    expect(w.find("[data-testid=wr-metric-v]").classes()).toContain("roll-down");
    expect(w.findAll(".wr-d__n").map((x) => x.text()).join("")).toBe("999");
  });

  it("降低動畫：直接換、不翻", async () => {
    const w = make(3, true);
    await w.setProps({ value: 4 });
    expect(w.find(".wr-d").exists()).toBe(false);
    expect(w.find("[data-testid=wr-metric-v]").text()).toBe("4");
  });

  it("沒有權限、沒有結果不翻也不顯示 0", async () => {
    expect(make(null).text()).toContain("No access");
    expect(make("—").text()).toContain("No result");
  });
});
