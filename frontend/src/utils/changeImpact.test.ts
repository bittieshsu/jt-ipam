import { describe, expect, it } from "vitest";
import { createI18n } from "vue-i18n";
import zh from "@/i18n/zh-TW.json";
import { reasonText } from "./changeImpact";

const i18n = createI18n({ legacy: false, locale: "zh-TW", messages: { "zh-TW": zh } });
const t = i18n.global.t as unknown as (k: string, p?: Record<string, unknown>) => string;
const te = i18n.global.te as unknown as (k: string) => boolean;

describe("reasonText", () => {
  it("translates a single source code, not only the plural list (2026-10-25: 「最近有人在用（scanner，…）」)", () => {
    const s = reasonText(t, te, "NEW_IP_RECENTLY_SEEN", { address: "198.51.100.121", source: "scanner", mac: "00:00:5e:00:53:21" });
    expect(s).not.toContain("scanner");
    expect(s).toContain("掃描代理");
  });
  it("keeps an unknown single source as it is", () => {
    expect(reasonText(t, te, "NEW_IP_RECENTLY_SEEN", { address: "198.51.100.9", source: "mystery", mac: "" })).toContain("mystery");
  });
});
