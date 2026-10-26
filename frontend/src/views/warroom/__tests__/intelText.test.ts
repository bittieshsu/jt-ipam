/**
 * AI 情報的系統摘要文字（10-26 事故：改句型時誤蓋到同名的類別標籤，左側小標變成「重大事件：，（），尚未解除。」）。
 * 守：類別標籤不是句型（沒有 {參數}）；三種語言組出來的句子沒有沒填到的參數、沒有空括號、冒號後面不是直接接逗號。
 */
import { describe, expect, it } from "vitest";
import { createI18n } from "vue-i18n";
import zh from "@/i18n/zh-TW.json";
import en from "@/i18n/en-US.json";
import ja from "@/i18n/ja-JP.json";
import { categoryLabel, eventText, itemText } from "../intelText";
import type { IntelEvent, IntelItem } from "@/api/warroom";

const LANGS = { "zh-TW": zh, "en-US": en, "ja-JP": ja } as const;
const at = new Date(Date.now() - 3600_000).toISOString();

const ITEMS: IntelItem[] = [
  { id: "1", fact_ids: [], category: "overview", state: "info", code: "overview", params: { at, registered_ips: 340, open_anomalies: 144 } },
  { id: "2", fact_ids: [], category: "ongoing", state: "ongoing", code: "ongoing_anomaly",
    params: { category: "ip_conflicts", ip: "198.51.100.80", label: "", first_seen_at: at, baseline: false } },
  { id: "3", fact_ids: [], category: "ongoing", state: "ongoing", code: "ongoing_anomaly",
    params: { category: "unauthorized_ips", ip: null, label: "printer-2f", first_seen_at: at, baseline: true } },
  { id: "4", fact_ids: [], category: "events", state: "new", code: "events_changed",
    params: { since: at, new: 1, resolved: 0, examples: [{ category: "ip_conflicts", label: "", ip: "198.51.100.80" }] } },
  { id: "5", fact_ids: [], category: "integrity", state: "watch", code: "integrity",
    params: { total: 9, stale: 1, failed: 0, never: 0, examples: [{ name: "Kea DHCP", state: "stale", last_success_at: at }] } },
];
const EVENTS: IntelEvent[] = [
  { id: "e1", category: "ip_conflicts", label: "", ip: "198.51.100.80", first_seen_at: at, state: "open" } as IntelEvent,
  { id: "e2", category: "arp_flux", label: "nas-07", ip: null, first_seen_at: at, state: "unknown" } as IntelEvent,
];

for (const [code, messages] of Object.entries(LANGS)) {
  describe(`AI 情報文字（${code}）`, () => {
    const i18n = createI18n({ legacy: false, locale: code, messages: { [code]: messages } });
    const t = i18n.global.t as unknown as (k: string, p?: Record<string, unknown>) => string;
    const te = i18n.global.te as unknown as (k: string) => boolean;
    const bad = (s: string) => /\{\w+\}|（）|\(\)|[：:]\s*[，,]|於\s*。/.test(s);

    it("類別標籤是名稱、不是句型", () => {
      const cats = (messages as { warroom: { intel: { cat: Record<string, string> } } }).warroom.intel.cat;
      for (const [k, v] of Object.entries(cats)) {
        expect(v, k).not.toMatch(/[{}]/);
        expect(v.length, k).toBeLessThan(24);
        expect(categoryLabel(k, t, te)).toBe(v);
      }
    });

    it("組出來的句子沒有空參數、空括號", () => {
      for (const it of ITEMS) expect(bad(itemText(it, t, te)), itemText(it, t, te)).toBe(false);
      for (const ev of EVENTS) expect(bad(eventText(ev, t, te)), eventText(ev, t, te)).toBe(false);
    });
  });
}
