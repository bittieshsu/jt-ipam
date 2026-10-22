/**
 * 戰情室按鍵只有一份（10-22 使用者：設定頁的按鍵表漏了 H，「以後要同步，這邊顯示跟電視牆要一樣」）。
 * 守的是：
 * 1. 列出來的每個鍵都真的有作用；有作用的每個鍵都有列出來
 * 2. 電視的按鍵說明與戰情室設定頁都從同一份清單畫，不可以再各自手寫 <kbd>
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { HANDLED_KEYS, keyAction, warroomKeyRows } from "../warroomKeys";

const DISPLAY_TO_EVENT: Record<string, string[]> = {
  "←": ["ArrowLeft"], "→": ["ArrowRight"], "↑": ["ArrowUp"], "↓": ["ArrowDown"],
  PgUp: ["PageUp"], PgDn: ["PageDown"], Space: [" "], Esc: ["Escape"], H: ["h", "H"],
};

function listedEventKeys(topics: number): Set<string> {
  const out = new Set<string>();
  for (const row of warroomKeyRows(topics)) {
    if (row.range) { for (let i = 1; i <= topics; i++) out.add(String(i)); continue; }
    for (const k of row.keys) for (const e of DISPLAY_TO_EVENT[k] ?? [k]) out.add(e);
  }
  return out;
}

describe("戰情室按鍵清單", () => {
  it("列出來的每個鍵都有作用", () => {
    for (const k of listedEventKeys(7)) expect(keyAction(k), k).not.toBeNull();
  });

  it("有作用的每個鍵都有列出來（含 H、Home、Esc）", () => {
    const listed = listedEventKeys(9);
    for (const k of HANDLED_KEYS) expect(listed.has(k), k).toBe(true);
    for (const k of ["h", "H", "Home", "Escape"]) expect(listed.has(k), k).toBe(true);
  });

  it("看按鍵說明只用 H，問號不再有作用（10-22 使用者：「改 H 就好，不要用問號」）", () => {
    expect(keyAction("h")).toBe("help");
    expect(keyAction("H")).toBe("help");
    expect(keyAction("?")).toBeNull();
    expect(listedEventKeys(7).has("?")).toBe(false);
  });

  it("數字範圍跟著主題數量", () => {
    expect(warroomKeyRows(5)[0]).toMatchObject({ range: true, keys: ["1", "5"] });
  });

  it("電視說明與設定頁都用這份清單畫，不自己寫 <kbd>", () => {
    for (const f of ["../../views/warroom/WarRoom.vue", "../../views/WarroomSettings.vue"]) {
      const src = readFileSync(resolve(__dirname, f), "utf8");
      expect(src, f).toContain("warroomKeyRows(");
      expect(src, f).not.toMatch(/<kbd>[^<{]+<\/kbd>/);
    }
  });
});
