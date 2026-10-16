/** 作業系統分類的顯示名稱依介面語言挑（日文以前一律退回英文）。 */
import { describe, expect, it } from "vitest";
import { osDisplayLabel, osFamilyLabel, type OsFamily } from "../scanProbes";

const fams: OsFamily[] = [
  { key: "network", label_en: "Network device", label_zh: "網路裝置", label_ja: "ネットワーク機器" },
  { key: "linux", label_en: "Linux", label_zh: "Linux" },
];

describe("osFamilyLabel", () => {
  it("依語言挑名稱", () => {
    expect(osFamilyLabel(fams, "network", "zh-TW")).toBe("網路裝置");
    expect(osFamilyLabel(fams, "network", "en-US")).toBe("Network device");
    expect(osFamilyLabel(fams, "network", "ja-JP")).toBe("ネットワーク機器");
  });
  it("後端沒給日文時退回英文", () => {
    expect(osFamilyLabel(fams, "linux", "ja-JP")).toBe("Linux");
  });
});

describe("osDisplayLabel", () => {
  const fams = [
    { key: "linux", label_en: "Linux", label_zh: "Linux", label_ja: "Linux" },
    { key: "unknown", label_en: "Unknown", label_zh: "未知", label_ja: "不明" },
  ] as never;
  it("shows the raw OS text when the family is unknown (an unlisted distro reads better than 未知)", () => {
    expect(osDisplayLabel(fams, "unknown", "SomeVendorOS 1.0", "zh-TW")).toBe("SomeVendorOS 1.0");
    expect(osDisplayLabel(fams, null, "SomeVendorOS 1.0", "zh-TW")).toBe("SomeVendorOS 1.0");
  });
  it("keeps the family label when the family is known or there is no raw text", () => {
    expect(osDisplayLabel(fams, "linux", "Bazzite", "zh-TW")).toBe("Linux");
    expect(osDisplayLabel(fams, "unknown", "", "zh-TW")).toBe("未知");
  });
});
