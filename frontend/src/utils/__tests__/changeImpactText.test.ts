/**
 * IP 變更評估的句子：後端只送代碼與參數，前端翻成句子。
 *
 * 代碼型的參數（整合種類、系統設定、線路欄位、IP 範圍用途）要翻成人看得懂的字，不可以露出
 * scan_agent、dns_servers 這種代碼；資料不足的「影響項目」用來分辨同類別的多筆，跟類別同名的不重複顯示。
 */
import { describe, expect, it } from "vitest";
import { createI18n } from "vue-i18n";
import zhTW from "@/i18n/zh-TW.json";
import enUS from "@/i18n/en-US.json";
import { gapAffected, reasonText } from "@/utils/changeImpact";

function tt(locale: "zh-TW" | "en-US") {
  const i18n = createI18n({ legacy: false, locale, messages: { "zh-TW": zhTW, "en-US": enUS } });
  return {
    t: (k: string, p?: Record<string, unknown>) => i18n.global.t(k, p ?? {}),
    te: (k: string) => i18n.global.te(k),
  };
}

describe("評估句子裡的代碼參數", () => {
  const { t, te } = tt("zh-TW");

  it("整合種類翻成產品名稱", () => {
    const s = reasonText(t, te, "INTEGRATION_ENDPOINT", { kind: "scan_agent", address: "198.51.100.10", field: "agent_url" });
    expect(s).toContain("掃描代理");
    expect(s).not.toContain("scan_agent");
  });

  it("系統設定與線路欄位翻成中文", () => {
    expect(reasonText(t, te, "SYSTEM_ENDPOINT", { setting: "ldap", address: "198.51.100.10" })).toContain("LDAP 伺服器");
    const c = reasonText(t, te, "CIRCUIT_ADDRESS", { circuit: "WAN-1", field: "dns_servers", address: "198.51.100.10" });
    expect(c).toContain("DNS 伺服器");
    expect(c).not.toContain("dns_servers");
  });

  it("沒填埠號的服務端點寫成「未指定埠」，有埠號的照原樣", () => {
    expect(reasonText(t, te, "SERVICE_ENDPOINT_ADDRESS", { service: "Wiki", address: "198.51.100.10", endpoint: "any" }))
      .toContain("未指定埠");
    expect(reasonText(t, te, "SERVICE_ENDPOINT_ADDRESS", { service: "Portal", address: "198.51.100.10", endpoint: "tcp/443" }))
      .toContain("tcp/443");
  });

  it("沒有翻譯的代碼照原樣顯示，不會整句空白", () => {
    expect(reasonText(t, te, "INTEGRATION_ENDPOINT", { kind: "future_thing", address: "198.51.100.10" }))
      .toContain("future_thing");
  });

  it("英文也翻", () => {
    const en = tt("en-US");
    expect(reasonText(en.t, en.te, "NEW_IP_IN_RANGE", { address: "198.51.100.80", range: "printers", purpose: "reserved" }))
      .toContain("reserved");
  });
});

describe("資料不足的影響項目", () => {
  const { t, te } = tt("zh-TW");

  it("同類別的兩筆權限不足分得出來", () => {
    expect(gapAffected(t, te, { category: "config", affected_analysis: "vpn_endpoint" })).toBe("VPN 端點");
    expect(gapAffected(t, te, { category: "config", affected_analysis: "circuit" })).toBe("線路");
  });

  it("跟類別同名、沒有值、沒有翻譯的都不顯示", () => {
    expect(gapAffected(t, te, { category: "dns", affected_analysis: "dns" })).toBe("");
    expect(gapAffected(t, te, { category: "dns", affected_analysis: null })).toBe("");
    expect(gapAffected(t, te, { category: "dns", affected_analysis: "something_new" })).toBe("");
  });
});

describe("舊位址仍有設備在用：來源代碼翻成看得懂的字（2026-10-08 防火牆 ARP 納入）", () => {
  it("zh-TW", () => {
    const { t, te } = tt("zh-TW");
    const s = reasonText(t, te, "IP_RECENTLY_ACTIVE",
                         { address: "198.51.100.10", sources: "arp:opnsense, scanner, vpn:fortigate", last_seen: "2026-10-08T08:00:00Z" });
    expect(s).toContain("ARP 表（OPNsense）");
    expect(s).toContain("VPN 連線（FortiGate）");
    expect(s).toContain("掃描代理");
    expect(s).not.toContain("arp:opnsense");
  });
  it("en-US", () => {
    const { t, te } = tt("en-US");
    const s = reasonText(t, te, "IP_RECENTLY_ACTIVE", { address: "198.51.100.10", sources: "arp:checkpoint", last_seen: "" });
    expect(s).toContain("ARP table (Check Point)");
  });
});
