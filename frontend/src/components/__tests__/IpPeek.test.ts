/**
 * 懸停摘要裡的「狀態」與「實際狀態」。
 *
 * 客戶回報：看到 `active` 和 `unknown` 兩個標籤黏在一起，不知道那是什麼意思。
 * 那其實是兩個不同的欄位（登記的用途 vs 量到的存活），卻擠在同一列、而且直接印
 * 英文原始值 —— 全站其他地方都是翻譯過的。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { createI18n } from "vue-i18n";
import IpPeek from "../IpPeek.vue";
import zhTW from "@/i18n/zh-TW.json";

function render(data: Record<string, unknown>) {
  const i18n = createI18n({ legacy: false, locale: "zh-TW", messages: { "zh-TW": zhTW } });
  return mount(IpPeek, { props: { ip: "192.168.1.129", data }, global: { plugins: [i18n] } });
}

describe("IpPeek 的狀態顯示", () => {
  it("兩個欄位分開標示，不是黏在一起的兩個標籤", () => {
    const w = render({ state: "active", effective_status: "unknown" });
    const rows = w.findAll(".peek-row").map((r) => r.text());
    expect(rows.some((t) => t.includes("狀態") && t.includes("使用中"))).toBe(true);
    expect(rows.some((t) => t.includes("實際狀態") && t.includes("未知"))).toBe(true);
  });

  it("不把英文原始值直接印在畫面上", () => {
    const t = render({ state: "active", effective_status: "unknown" }).text();
    expect(t).not.toContain("active");
    expect(t).not.toContain("unknown");
  });

  it("帶來源的實際狀態會翻譯前段、保留來源", () => {
    // "online (scanner)" —— 來源是給人查證用的，不能翻掉也不能丟掉
    const t = render({ state: "active", effective_status: "online (scanner)" }).text();
    expect(t).toContain("上線");
    expect(t).toContain("scanner");
  });

  it("沒有實際狀態時不畫那一列", () => {
    const w = render({ state: "reserved" });
    expect(w.text()).toContain("保留");
    expect(w.text()).not.toContain("實際狀態");
  });

  it("遇到沒收錄的值就原樣顯示，不憑空造字", () => {
    expect(render({ state: "quarantined" }).text()).toContain("quarantined");
  });
});
